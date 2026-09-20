import copy
from datetime import datetime, timezone
import sys
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from algosdk import account
from verified_holdings import (account_snapshot, read_transactions, merge_histories,
    asset_changes, rewind_holdings, eligibility_segments, verified_window)

A, B, C = [account.generate_account()[1] for _ in range(3)]
ASSET = 42


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def transfer(tx_id, round_number, sender=A, receiver=B, amount=1, **extra):
    return {'id': tx_id, 'confirmed-round': round_number, 'round-time': round_number,
            'sender': sender, 'tx-type': 'axfer',
            'asset-transfer-transaction': {'asset-id': ASSET, 'amount': amount, 'receiver': receiver, **extra}}


def holding(wallet, amount, round_number=14):
    return account_snapshot({'address': wallet, 'round': round_number,
        'total-assets-opted-in': 1, 'assets': [{'asset-id': ASSET, 'amount': amount}]})


def baseline(wallet, amount, event_id=1):
    return {'id': event_id, 'stake_id': wallet, 'wallet_address': wallet, 'event_type': 'baseline',
        'occurred_at': iso(1), 'amount_before': '0', 'amount_after': str(amount),
        'active_before': False, 'active_after': True}


class VerifiedHoldingsTests(unittest.TestCase):
    def test_short_pages_follow_tokens_and_deduplicate_identical_overlap(self):
        first, second = transfer('first', 11), transfer('second', 12)
        pages = [{'current-round': 14, 'transactions': [first], 'next-token': 'p2'},
                 {'current-round': 15, 'transactions': [first, second]}]
        paths = []
        def read(path):
            paths.append(path)
            return pages.pop(0)
        self.assertEqual(len(read_transactions(read, A, 10, 14)), 2)
        query = parse_qs(urlparse(paths[1]).query)
        self.assertEqual(query['next'], ['p2'])
        self.assertEqual(query['min-round'], ['11'])
        self.assertEqual(query['max-round'], ['14'])
        self.assertNotIn('tx-type', query)

    def test_lagged_and_incomplete_pagination_fails(self):
        for page in [
            {'current-round': 13, 'transactions': []},
            {'current-round': 14, 'transactions': [], 'next-token': 'again'},
            {'current-round': 14, 'transactions': [transfer('outside', 15)]},
        ]:
            with self.assertRaises(ValueError): read_transactions(lambda _: page, A, 10, 14)
        with self.assertRaises(ValueError):
            read_transactions(lambda _: {'current-round': 14, 'transactions': [transfer('same', 11)], 'next-token': 'again'}, A, 10, 14)

    def test_conflicting_duplicate_parent_is_rejected(self):
        first = transfer('first', 11)
        changed = transfer('first', 11, amount=2)
        self.assertEqual(merge_histories([[first], [first]]), [first])
        with self.assertRaises(ValueError): merge_histories([[first], [changed]])
        with self.assertRaises(ValueError): asset_changes([first, first], [ASSET])

    def test_complete_snapshot_requires_every_opted_in_asset(self):
        self.assertEqual(account_snapshot({'address': A, 'round': 14, 'total-assets-opted-in': 0})['holdings'], {})
        source = {'address': A, 'round': 14, 'total-assets-opted-in': 2,
                  'assets': [{'asset-id': ASSET, 'amount': 1}]}
        with self.assertRaises(ValueError): account_snapshot(source)
        source['assets'].append(source['assets'][0])
        with self.assertRaises(ValueError): account_snapshot(source)
        source['total-assets-opted-in'] = 1; source['assets'].pop(); source['assets'][0]['amount'] = 1.0
        with self.assertRaises(ValueError): account_snapshot(source)

    def test_nested_inner_transfer_is_counted_once_across_wallet_searches(self):
        inner = transfer('inner-unused', 11)
        parent = {'id': 'parent', 'confirmed-round': 11, 'round-time': 11, 'sender': C, 'tx-type': 'appl',
                  'inner-txns': [{'tx-type': 'appl', 'sender': C, 'inner-txns': [inner]}]}
        changes = asset_changes(merge_histories([[parent], [parent]]), [ASSET])
        self.assertEqual(changes[0]['deltas'][(A, ASSET)], -1)
        self.assertEqual(changes[0]['deltas'][(B, ASSET)], 1)

    def test_close_remainder_and_effective_clawback_sender(self):
        close = transfer('close', 11, amount=3, **{'close-to': C, 'close-amount': 7})
        changes = asset_changes([close], [ASSET])
        self.assertEqual(changes[0]['deltas'], {(A, ASSET): -10, (B, ASSET): 3, (C, ASSET): 7})
        clawback = transfer('clawback', 12, sender=C, receiver=B, amount=2)
        clawback['asset-transfer-transaction']['sender'] = A
        change = asset_changes([clawback], [ASSET])[0]
        self.assertEqual(change['deltas'], {(A, ASSET): -2, (B, ASSET): 2})

    def test_creation_and_verified_destruction_supply(self):
        create = {'id': 'create', 'confirmed-round': 11, 'round-time': 11, 'sender': A, 'tx-type': 'acfg',
                  'created-asset-index': ASSET, 'asset-config-transaction': {'asset-id': 0, 'params': {'total': 7}}}
        destroy = {'id': 'destroy', 'confirmed-round': 12, 'round-time': 12, 'sender': A, 'tx-type': 'acfg',
                   'asset-config-transaction': {'asset-id': ASSET}}
        with self.assertRaises(ValueError): asset_changes([create, destroy], [ASSET])
        changes = asset_changes([create, destroy], [ASSET], {ASSET: 7})
        self.assertEqual(changes[0]['deltas'][(A, ASSET)], 7)
        self.assertEqual(changes[1]['deltas'][(A, ASSET)], -7)
        self.assertEqual(rewind_holdings(holding(A, 0), changes, 10, [ASSET]), {ASSET: 0})

    def test_nft_move_changes_eligibility_without_double_counting(self):
        changes = asset_changes([transfer('out', 11), transfer('back', 12, B, A)], [ASSET])
        initial = {A: rewind_holdings(holding(A, 1), changes, 10, [ASSET]),
                   B: rewind_holdings(holding(B, 0), changes, 10, [ASSET])}
        result = eligibility_segments([baseline(A, 1), baseline(B, 1, 2)], initial, changes, {ASSET: 0}, True, 10, iso(10), iso(14))
        self.assertEqual([row['weights'] for row in result], [{A: '1'}, {B: '1'}, {A: '1'}])
        self.assertTrue(all(len(row['weights']) == 1 for row in result))

    def test_fungible_holdings_cap_stale_recorded_stakes(self):
        changes = asset_changes([transfer('half', 11, amount=50)], [ASSET])
        initial = {A: {ASSET: 100}, B: {ASSET: 0}}
        result = eligibility_segments([baseline(A, 500), baseline(B, 500, 2)], initial, changes, {ASSET: 2}, False, 10, iso(10), iso(12))
        self.assertEqual(result[0]['weights'], {A: '1'})
        self.assertEqual(result[1]['weights'], {A: '0.5', B: '0.5'})

    def test_rewind_stops_at_each_accounts_own_round(self):
        changes = asset_changes([transfer('out', 11), transfer('later-back', 13, B, A)], [ASSET])
        self.assertEqual(rewind_holdings(holding(A, 0, 12), changes, 10, [ASSET]), {ASSET: 1})
        self.assertEqual(rewind_holdings(holding(B, 0, 14), changes, 10, [ASSET]), {ASSET: 0})

    def test_history_must_agree_with_preceding_sealed_checkpoint(self):
        with self.assertRaises(ValueError): rewind_holdings(holding(A, 1), [], 10, [ASSET], {ASSET: 2})
        self.assertEqual(rewind_holdings(holding(A, 1), [], 10, [ASSET], {str(ASSET): '1'}), {ASSET: 1})
        with self.assertRaises(ValueError):
            rewind_holdings(holding(B, 0), asset_changes([transfer('impossible', 11)], [ASSET]), 10, [ASSET])

    def test_stake_changes_apply_only_after_their_event(self):
        increase = {**baseline(A, 2, 2), 'event_type': 'increase', 'occurred_at': iso(11),
                    'amount_before': '1', 'active_before': True}
        result = eligibility_segments([baseline(A, 1), increase], {A: {ASSET: 10}}, [], {ASSET: 0}, False, 10, iso(10), iso(12))
        self.assertEqual([row['weights'] for row in result], [{A: '1'}, {A: '2'}])
        increase['amount_before'] = '3'
        with self.assertRaises(ValueError): eligibility_segments([baseline(A, 1), increase], {A: {ASSET: 10}}, [], {ASSET: 0}, False, 10, iso(10), iso(12))

    def test_duplicate_nft_and_missing_wallet_snapshots_fail(self):
        events = [baseline(A, 1), baseline(B, 1, 2)]
        with self.assertRaises(ValueError): eligibility_segments(events, {A: {ASSET: 1}}, [], {ASSET: 0}, True, 10, iso(10), iso(12))
        with self.assertRaises(ValueError): eligibility_segments(events, {A: {ASSET: 1}, B: {ASSET: 1}}, [], {ASSET: 0}, True, 10, iso(10), iso(12))

    def test_incomplete_and_malformed_chain_records_fail(self):
        malformed = transfer('missing', 11); del malformed['asset-transfer-transaction']
        with self.assertRaises(ValueError): asset_changes([malformed], [ASSET])
        malformed = transfer('bad-amount', 11, amount=1.0)
        with self.assertRaises(ValueError): asset_changes([malformed], [ASSET])
        malformed = transfer('bad-close', 11, **{'close-amount': 1})
        with self.assertRaises(ValueError): asset_changes([malformed], [ASSET])
        malformed = transfer('bad-time', 11); malformed['inner-txns'] = [transfer('inner', 12)]
        with self.assertRaises(ValueError): asset_changes([malformed], [ASSET])
        event = baseline(A, 1); event['active_before'] = 0
        with self.assertRaises(ValueError): eligibility_segments([event], {A: {ASSET: 1}}, [], {ASSET: 0}, True, 10, iso(10), iso(12))

    def test_provider_integration_uses_fixed_snapshots_and_waits_for_indexer(self):
        out, back = transfer('out', 11), transfer('back', 13, B, A)
        waits, account_reads = [], []
        def algod(path):
            if '/blocks/' in path:
                round_number = int(path.split('/blocks/')[1].split('?')[0])
                return {'block': {'rnd': round_number, 'ts': round_number, 'gen': 'mainnet-v1.0',
                    'gh': 'wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8='}}
            wallet = path.rsplit('/', 1)[1]
            account_reads.append(wallet)
            return {'address': wallet, 'round': 14, 'total-assets-opted-in': 1,
                    'assets': [{'asset-id': ASSET, 'amount': int(wallet == A)}]}
        def indexer(path):
            if path == '/health':
                return {'round': 14 if waits else 13}
            return {'current-round': 14, 'transactions': [out, back]}
        result = verified_window(algod, indexer, [A, B], [ASSET], 10, 12, wait=waits.append)
        self.assertEqual(len(account_reads), 2)
        self.assertEqual(waits, [0.5])
        self.assertEqual(result['initial_holdings'], {A: {ASSET: 1}, B: {ASSET: 0}})
        self.assertEqual(result['end_holdings'], {A: {ASSET: 0}, B: {ASSET: 1}})
        self.assertEqual(len(result['changes']), 1)
        self.assertEqual(len(result['evidence']['transactions']), 2)
        self.assertEqual(len(result['evidence_digest']), 64)
        with self.assertRaises(ValueError):
            verified_window(algod, lambda _: {'round': 0}, [A, B], [ASSET], 10, 12, wait=lambda _: None)

    def test_accounting_can_end_between_blocks_or_advance_within_one_block(self):
        def algod(path):
            if '/blocks/' in path:
                round_number = int(path.split('/blocks/')[1].split('?')[0])
                return {'block': {'rnd': round_number, 'ts': round_number * 3, 'gen': 'mainnet-v1.0',
                    'gh': 'wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8='}}
            return {'address': A, 'round': 15, 'total-assets-opted-in': 1,
                'assets': [{'asset-id': ASSET, 'amount': 2}]}
        def indexer(path):
            return {'round': 15} if path == '/health' else {'current-round': 15, 'transactions': []}
        window = verified_window(algod, indexer, [A], [ASSET], 10, 12,
            accounting_start=iso(31), accounting_end=iso(37))
        self.assertEqual(window['start'], iso(31))
        self.assertEqual(window['end'], iso(37))
        self.assertEqual(window['evidence']['block_times'], [30, 36])
        self.assertEqual(window['evidence']['following_block_times'], [33, 39])
        within = verified_window(algod, indexer, [A], [ASSET], 10, 10,
            accounting_start=iso(31), accounting_end=iso(32))
        self.assertEqual(within['end_holdings'], {A: {ASSET: 2}})
        self.assertEqual(within['changes'], [])
        for start, end in [(29, 37), (33, 37), (31, 39), (31, 35)]:
            with self.assertRaisesRegex(ValueError, 'adjacent blocks'):
                verified_window(algod, indexer, [A], [ASSET], 10, 12,
                    accounting_start=iso(start), accounting_end=iso(end))
        def wrong_network(path):
            result = algod(path)
            if 'block' in result: result['block']['gh'] = 'wrong'
            return result
        with self.assertRaisesRegex(ValueError, 'network'):
            verified_window(wrong_network, indexer, [A], [ASSET], 10, 12)


if __name__ == '__main__':
    unittest.main()
