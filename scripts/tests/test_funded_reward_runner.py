import gzip
import json
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from funded_batch_fixture import A, B, ASSET, POOL, START, END, GENESIS, fixture, state, algod, indexer
from funded_reward_batch import prepare_batch
from funded_reward_runner import (EvidenceArchive, FundedRunner, block_header, canonical_bytes,
    prepare_opening, round_at_or_before, verify_batch_evidence)
from reward_ledger import digest, timestamp


def reader(path):
    if path == '/v2/status':
        return {'last-round': 201}
    if '/blocks/' in path:
        r = int(path.split('/blocks/')[1].split('?')[0])
        if r == 99:
            at = timestamp(START) - timedelta(seconds=3)
        elif 102 <= r <= 199:
            at = timestamp(START) + (timestamp(END) - timestamp(START)) * ((r - 100) / 100)
        else:
            return algod(path)
        return {'block': {'rnd': r, 'ts': int(at.timestamp()), 'gen': 'mainnet-v1.0', 'gh': GENESIS}}
    return algod(path)


def filtered_indexer(path):
    result = indexer(path)
    if path != '/health':
        query = parse_qs(urlsplit(path).query)
        result['transactions'] = [t for t in result['transactions']
            if int(query['min-round'][0]) <= t['confirmed-round'] <= int(query['max-round'][0])]
    return result


class RunnerFixture:
    def __init__(self, output):
        self.current = state()
        self.current['last_confirmed_round'] = '100'
        self.objects, self.calls = {}, []
        self.archive_fault = self.seal_timeout = self.lease_busy = self.defer_publication = False
        self.snapshot_override = None
        self.close_timeout = False
        def write(key, data):
            self.calls.append('archive')
            if self.archive_fault:
                raise RuntimeError('archive unavailable')
            self.objects.setdefault(key, data)
        self.archive = EvidenceArchive(write, lambda key: self.objects[key], output)
        self.runner = FundedRunner(self.rpc, self.publisher, reader, filtered_indexer, self.archive, wait=lambda _: None)

    def rpc(self, name, args):
        self.calls.append(name)
        if name == 'get_funded_reward_state':
            return deepcopy(self.current)
        if name == 'get_funded_stake_export':
            return fixture()['bundle']
        if name == 'acquire_funded_reward_lease' and self.lease_busy:
            raise RuntimeError('lease busy')
        if name == 'seal_funded_reward_batch':
            payload = args['p_payload']
            self.current['pending'] = {'id': digest(payload), 'payload': payload, 'credits': [
                {'wallet': w, 'previous': self.current['credits'].get(w, '0'),
                    'cumulative': str(int(self.current['credits'].get(w, '0')) + int(a)),
                    'receipt': None, 'attempts': []} for w, a in payload['new_rewards'].items()]}
            if self.seal_timeout:
                raise RuntimeError('response lost after seal')
        if name == 'open_funded_reward_period':
            self.current['period'] = {'checkpoint': args['p_checkpoint'], 'start': args['p_snapshot']['at']}
        if name == 'close_empty_funded_reward_period':
            p = self.current['period']
            assert p['budget_atomic'] == '0' and self.current['pending'] is None
            assert args['p_period'] == p['id'] and args['p_revision'] == p['revision']
            p.update(closed=True, end=p['cursor'], revision=str(int(p['revision']) + 1))
            if self.close_timeout:
                raise RuntimeError('response lost after empty close')

    def publisher(self, pool, preview):
        self.calls.append('preview' if preview else 'publish')
        if preview:
            return {'snapshot': deepcopy(self.snapshot_override or fixture()['snapshot'])}
        pending = self.current['pending']
        verify_batch_evidence(pool, pending, self.archive.load(pool, pending['payload']['evidence_digest']))
        if self.defer_publication:
            return {'status': 'pending'}
        for credit in pending['credits']:
            self.current['credits'][credit['wallet']] = credit['cumulative']
        self.current['confirmed_allocated'] = str(sum(int(v) for v in self.current['credits'].values()))
        self.current['period'].update(cursor=pending['payload']['end'], revision='1',
            checkpoint=pending['payload']['checkpoint_after'])
        self.current['pending'] = None
        return {'status': 'confirmed'}


class RunnerTests(unittest.TestCase):
    def setUp(self):
        # TMPDIR is supplied by CI or the caller's temporary directory.
        self.temp = tempfile.TemporaryDirectory(prefix='funded-runner-')
        self.addCleanup(self.temp.cleanup)

    def test_preview_calculates_from_real_history_without_remote_writes_or_publication(self):
        f = RunnerFixture(self.temp.name)
        before = deepcopy(f.current)
        result = f.runner.run_pool(POOL)
        self.assertEqual(result['issued_atomic'], '500')
        self.assertEqual(f.current, before)
        self.assertEqual(f.objects, {})
        self.assertNotIn('publish', f.calls)
        self.assertNotIn('acquire_funded_reward_lease', f.calls)

    def test_verified_archive_precedes_sealing_and_publication(self):
        f = RunnerFixture(self.temp.name)
        result = f.runner.run_pool(POOL, publish=True)
        self.assertEqual(result['action'], 'confirmed')
        self.assertEqual(f.current['credits'], {A: '310', B: '290'})
        self.assertLess(f.calls.index('archive'), f.calls.index('seal_funded_reward_batch'))
        self.assertLess(f.calls.index('seal_funded_reward_batch'), f.calls.index('publish'))
        self.assertEqual(len(f.objects), 1)

    def test_failed_archive_or_busy_lease_prevents_publication(self):
        for fault in ('archive_fault', 'lease_busy'):
            f = RunnerFixture(Path(self.temp.name) / fault)
            setattr(f, fault, True)
            with self.assertRaises(RuntimeError):
                f.runner.run_pool(POOL, publish=True)
            self.assertIsNone(f.current['pending'])
            self.assertNotIn('publish', f.calls)

    def test_lost_seal_response_resumes_the_saved_batch_without_recalculating(self):
        f = RunnerFixture(self.temp.name)
        f.seal_timeout = True
        with self.assertRaisesRegex(RuntimeError, 'response lost'):
            f.runner.run_pool(POOL, publish=True)
        self.assertIsNotNone(f.current['pending'])
        sealed = deepcopy(f.current['pending'])
        f.seal_timeout = False
        f.calls.clear()
        f.runner.run_pool(POOL, publish=True)
        self.assertEqual(f.current['credits'], {A: '310', B: '290'})
        self.assertEqual(f.calls.count('publish'), 1)
        self.assertNotIn('archive', f.calls)
        self.assertNotIn('get_funded_stake_export', f.calls)
        self.assertEqual(f.current['period']['cursor'], sealed['payload']['end'])

    def test_corrupt_or_changed_archive_stops_a_resumed_batch(self):
        f = RunnerFixture(self.temp.name)
        f.seal_timeout = True
        with self.assertRaises(RuntimeError):
            f.runner.run_pool(POOL, publish=True)
        key = next(iter(f.objects))
        f.objects[key] = gzip.compress(b'{}', mtime=0)
        with self.assertRaisesRegex(ValueError, 'checksum'):
            f.runner.run_pool(POOL, publish=True)
        self.assertNotIn('publish', f.calls)

    def test_pending_publication_is_bounded_and_preserves_the_batch(self):
        f = RunnerFixture(self.temp.name)
        f.seal_timeout = True
        with self.assertRaises(RuntimeError):
            f.runner.run_pool(POOL, publish=True)
        before = deepcopy(f.current)
        f.defer_publication = True
        with self.assertRaisesRegex(RuntimeError, 'still pending'):
            f.runner.finish_pending(POOL, f.current, max_passes=2)
        self.assertEqual(f.current, before)
        self.assertEqual(f.calls.count('publish'), 2)

    def test_first_opening_uses_the_actual_snapshot_and_preserves_unpaid_reserves(self):
        current = state(); current.update(period=None, last_confirmed_round='100')
        result = prepare_opening(POOL, current, fixture()['bundle'], fixture()['snapshot'], reader, filtered_indexer)
        self.assertEqual(result['snapshot']['at'], START)
        self.assertEqual(result['evidence']['proposal']['budget_atomic'], '1000')
        self.assertEqual(result['checkpoint']['holdings'], {A: {str(ASSET): '1'}, B: {}})
        self.assertEqual(result['checkpoint']['opening_evidence_digest'], digest(result['evidence']))
        f = RunnerFixture(self.temp.name); f.current = current
        self.assertEqual(f.runner.run_pool(POOL, publish=True)['action'], 'open_period')
        self.assertNotIn('publish', f.calls)
        self.assertLess(f.calls.index('archive'), f.calls.index('open_funded_reward_period'))

    def test_empty_period_waits_for_recognised_funding_without_writes(self):
        f = RunnerFixture(self.temp.name)
        f.current['period']['budget_atomic'] = '0'
        f.snapshot_override = {**fixture()['snapshot'], 'deposited': '100', 'balance': '5000'}
        before = deepcopy(f.current)
        self.assertEqual(f.runner.run_pool(POOL, publish=True)['action'], 'await_funding')
        self.assertEqual(f.current, before)
        self.assertNotIn('acquire_funded_reward_lease', f.calls)
        self.assertEqual(f.objects, {})

    def test_first_empty_opening_waits_instead_of_creating_an_empty_month(self):
        f = RunnerFixture(self.temp.name)
        f.current['period'] = None
        f.snapshot_override = {**fixture()['snapshot'], 'deposited': '100', 'balance': '100'}
        self.assertEqual(f.runner.run_pool(POOL, publish=True)['action'], 'await_funding')
        self.assertIsNone(f.current['period'])
        self.assertNotIn('get_funded_stake_export', f.calls)

    def test_fresh_funding_opens_this_month_after_verifying_empty_history(self):
        f = RunnerFixture(self.temp.name)
        f.current['period']['budget_atomic'] = '0'
        f.snapshot_override = {**fixture()['snapshot'], 'at': END, 'round': '200'}
        before = deepcopy(f.current['credits'])
        preview = f.runner.run_pool(POOL, publish=False)
        self.assertEqual(preview['action'], 'restart_empty_period')
        self.assertEqual(preview['available_atomic'], '1000')
        self.assertNotIn('acquire_funded_reward_lease', f.calls)
        result = f.runner.run_pool(POOL, publish=True)
        self.assertEqual(result['action'], 'open_period')
        self.assertEqual(result['start'], END)
        self.assertEqual(result['budget_atomic'], '1000')
        self.assertEqual(f.current['credits'], before)
        self.assertLess(f.calls.index('publish'), f.calls.index('close_empty_funded_reward_period'))
        self.assertLess(f.calls.index('close_empty_funded_reward_period'), f.calls.index('open_funded_reward_period'))

    def test_lost_empty_close_response_resumes_opening_without_reallocating(self):
        f = RunnerFixture(self.temp.name)
        f.current['period']['budget_atomic'] = '0'
        f.snapshot_override = {**fixture()['snapshot'], 'at': END, 'round': '200'}
        before = deepcopy(f.current['credits'])
        f.close_timeout = True
        with self.assertRaisesRegex(RuntimeError, 'response lost after empty close'):
            f.runner.run_pool(POOL, publish=True)
        self.assertTrue(f.current['period']['closed'])
        f.calls.clear()
        result = f.runner.run_pool(POOL, publish=True)
        self.assertEqual(result['action'], 'open_period')
        self.assertEqual(f.current['credits'], before)
        self.assertNotIn('publish', f.calls)
        self.assertNotIn('close_empty_funded_reward_period', f.calls)

    def test_opening_after_month_end_verifies_gap_holdings_without_backdating(self):
        end = '2026-02-01T00:00:00Z'
        current = state(); current.update(last_confirmed_round='300')
        current['period'].update(closed=True, cursor=end, end=end)
        current['period']['checkpoint'].update(round='300', at=end, block_at=end,
            next_block_at='2026-02-01T00:00:03Z', holdings={A: {}, B: {str(ASSET): '1'}})
        snapshot = {**fixture()['snapshot'], 'at': '2026-02-01T00:00:30Z', 'round': '310'}
        bundle = fixture()['bundle']; bundle['exported_at'] = snapshot['at']
        def gap_reader(path):
            if '/blocks/' in path:
                r = int(path.split('/blocks/')[1].split('?')[0])
                return {'block': {'rnd': r, 'ts': int(timestamp(end).timestamp()) + 3 * (r - 300),
                    'gen': 'mainnet-v1.0', 'gh': GENESIS}}
            return reader(path)
        result = prepare_opening(POOL, current, bundle, snapshot, gap_reader, filtered_indexer)
        self.assertEqual(result['evidence']['proposal']['start'], snapshot['at'])
        self.assertEqual(result['checkpoint']['previous_checkpoint_digest'], current['period']['checkpoint_digest'])
        current['period']['checkpoint']['holdings'][A] = {str(ASSET): '1'}
        with self.assertRaisesRegex(ValueError, 'holdings differ'):
            prepare_opening(POOL, current, bundle, snapshot, gap_reader, filtered_indexer)

    def test_boundary_search_handles_midnight_between_blocks_and_repeated_timestamps(self):
        seconds = [0, 3, 3, 7, 10]
        def blocks(path):
            r = int(path.split('/blocks/')[1].split('?')[0])
            return {'block': {'rnd': r, 'ts': int(timestamp(START).timestamp()) + seconds[r - 1],
                'gen': 'mainnet-v1.0', 'gh': GENESIS}}
        self.assertEqual(round_at_or_before(blocks, '2026-01-01T00:00:03Z', 1, 4), 3)
        self.assertEqual(round_at_or_before(blocks, '2026-01-01T00:00:05Z', 1, 4), 3)
        with self.assertRaises(ValueError):
            round_at_or_before(blocks, '2026-01-01T00:00:10Z', 1, 4)

    def test_archive_retries_are_identical_and_changed_sealed_rewards_are_rejected(self):
        f = RunnerFixture(self.temp.name)
        proposal = prepare_batch(f.current, fixture()['bundle'], reader, filtered_indexer, 200, END)
        identity = f.archive.save(POOL, proposal['evidence'], True)
        self.assertEqual(f.archive.save(POOL, proposal['evidence'], True), identity)
        f.rpc('seal_funded_reward_batch', {'p_payload': proposal['payload']})
        for field in ('recipient', 'previous', 'award'):
            pending = deepcopy(f.current['pending'])
            if field == 'recipient':
                pending['credits'][0]['wallet'] = 'changed'
            elif field == 'previous':
                pending['credits'][0]['previous'] = '59'
            else:
                pending['payload']['new_rewards'][A] = '249'
            with self.assertRaises(ValueError):
                verify_batch_evidence(POOL, pending, proposal['evidence'])

    def test_postgres_timestamp_precision_does_not_change_the_evidence_boundary(self):
        f = RunnerFixture(self.temp.name)
        for key in ('start', 'end', 'cursor'):
            f.current['period'][key] = timestamp(f.current['period'][key]).isoformat(timespec='microseconds').replace('+00:00', 'Z')
        self.assertEqual(f.runner.run_pool(POOL, publish=True)['action'], 'confirmed')

    def test_a_pool_with_no_registered_wallets_keeps_its_budget_unallocated(self):
        current = state(); current.update(period=None, last_confirmed_round='100')
        bundle = fixture()['bundle']; bundle['pools'][0]['stake_events'] = []
        result = prepare_opening(POOL, current, bundle, fixture()['snapshot'], reader, filtered_indexer)
        self.assertEqual(result['checkpoint']['holdings'], {})
        self.assertEqual(result['evidence']['proposal']['budget_atomic'], '1000')


if __name__ == '__main__':
    unittest.main()
