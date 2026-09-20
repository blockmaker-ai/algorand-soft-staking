"""Prepare, archive, seal and resume funded rewards. No wallet keys live here.

The coordinator prepares from read-only inputs, then briefly acquires a database
lease to commit a compare-and-set proposal. Only the separate, explicitly enabled Edge
publisher can sign. A pending batch always resumes its archived calculation.
"""
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from funded_reward_batch import asset_policy, event_prefix, make_checkpoint, prepare_batch
from funded_reward_period import iso, open_period
from funded_reward_cap import reward_cap
from reward_ledger import digest, integer, timestamp
from verified_holdings import verified_window

from network import GENESIS_ID, GENESIS_HASH as GENESIS
from uuid import UUID

def valid_pool(value):
    try:
        return str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False

MAX_EVIDENCE_BYTES = 64 * 1024 * 1024


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def block_header(read, round_number):
    round_number = integer(round_number)
    block = read(f'/v2/blocks/{round_number}?format=json&header-only=true')['block']
    if (integer(block['rnd']) != round_number or block.get('gen') != GENESIS_ID or block.get('gh') != GENESIS):
        raise ValueError('The accounting boundary is not a verified configured-network block')
    return datetime.fromtimestamp(integer(block['ts']), timezone.utc)


def round_at_or_before(read, at, lower, upper):
    """Find the last block at/before a UTC cursor, including between-block ends."""
    at, lower, upper = timestamp(at), integer(lower), integer(upper)
    cache = {}
    def header(r):
        if r not in cache:
            cache[r] = block_header(read, r)
        return cache[r]
    if lower > upper or header(lower) > at or header(upper + 1) <= at:
        raise ValueError('The available chain window does not bracket the accounting time')
    while lower < upper:
        middle = (lower + upper + 1) // 2
        if header(middle) <= at:
            lower = middle
        else:
            upper = middle - 1
    if not header(lower) <= at < header(lower + 1):
        raise ValueError('The accounting time is not covered by adjacent blocks')
    return lower


def checked_pool(state, bundle, pool_id, until):
    matches = [p for p in bundle['pools'] if p['pool_id'] == pool_id]
    if len(matches) != 1 or timestamp(bundle['exported_at']) < timestamp(until):
        raise ValueError('The stake export does not cover this pool and boundary')
    pool, config = matches[0], state['config']
    reward_cap(pool)
    policy = asset_policy(pool)
    if integer(pool['app_id']) != integer(config['app_id']) or digest(policy) != config['asset_policy_digest']:
        raise ValueError('The reviewed vault or staking policy has changed')
    if sum(integer(v) for v in state['credits'].values()) != integer(state['confirmed_allocated']):
        raise ValueError('The confirmed credit ledger does not reconcile')
    if any(e['pool_id'] != pool_id for e in pool['stake_events']):
        raise ValueError('The stake export includes a different pool')
    return pool, policy


def prepare_opening(pool_id, state, bundle, snapshot, algod, indexer):
    """Start at a real verified snapshot. Any elapsed gap earns no new rewards."""
    previous = state.get('period')
    if state.get('pending') or (previous and previous['closed'] is not True):
        raise ValueError('Finish the existing batch and period first')
    config, at, end_round = state['config'], timestamp(snapshot['at']), integer(snapshot['round'])
    if (snapshot['app_id'] != config['app_id'] or snapshot['genesis_id'] != GENESIS_ID
            or snapshot['genesis_hash'] != GENESIS or snapshot['active'] != '1' or snapshot['paused'] != '0'
            or integer(snapshot['allocated']) != integer(state['confirmed_allocated'])
            or end_round < integer(state['last_confirmed_round'])):
        raise ValueError('The opening snapshot differs from the active ledger')
    proposal = open_period(pool_id, config['app_id'], snapshot['at'],
        {**snapshot, 'active': 1, 'paused': 0})
    if block_header(algod, end_round) != at:
        raise ValueError('The snapshot timestamp differs from its confirmed block')
    pool, policy = checked_pool(state, bundle, pool_id, snapshot['at'])
    policy_hash, events = digest(policy), pool['stake_events']
    checkpoint = previous['checkpoint'] if previous else None
    known = set(checkpoint['holdings']) if checkpoint else set()
    if checkpoint:
        prefix = event_prefix(events, checkpoint['at'])
        if (at < timestamp(previous['end']) or timestamp(checkpoint['at']) != timestamp(previous['end'])
                or checkpoint['policy_digest'] != policy_hash
                or len(prefix) != integer(checkpoint['stake_event_count'])
                or digest(prefix) != checkpoint['stake_event_digest']
                or any(e['wallet_address'] not in known for e in prefix)):
            raise ValueError('The previous period or its stake journal is not covered')
    wallets = known | {e['wallet_address'] for e in event_prefix(events, snapshot['at'])}
    assets = {integer(a) for a in policy['asset_decimals']}
    advances = checkpoint and timestamp(checkpoint['at']) < at
    start_round = integer(checkpoint['round']) if advances else end_round - 1
    start = checkpoint['at'] if advances else iso(block_header(algod, start_round))
    window = verified_window(algod, indexer, wallets, assets, start_round, end_round,
        accounting_start=start, accounting_end=snapshot['at'])
    if checkpoint:
        observed = window['initial_holdings'] if advances else window['end_holdings']
        for wallet in known:
            if any(integer(a) not in assets for a in checkpoint['holdings'][wallet]):
                raise ValueError('The previous checkpoint includes an unapproved asset')
            expected = {a: integer(checkpoint['holdings'][wallet].get(str(a), '0')) for a in assets}
            if observed[wallet] != expected:
                raise ValueError('Opening holdings differ from the preceding sealed checkpoint')
    after = make_checkpoint(window, events, policy_hash, digest(bundle))
    if previous:
        after['previous_checkpoint_digest'] = previous['checkpoint_digest']
    evidence = {'version': 'funded-opening-evidence-v1', 'pool_id': pool_id, 'snapshot': snapshot,
        'policy': policy, 'stake_export': bundle, 'previous_period': previous,
        'holding_history': window['evidence'], 'checkpoint': after, 'proposal': proposal}
    # This hashes the entire opening evidence without a circular self-reference.
    return {'snapshot': snapshot, 'policy_digest': policy_hash, 'checkpoint': {
        **after, 'opening_evidence_digest': digest(evidence)}, 'evidence': evidence}


def verify_batch_evidence(pool_id, pending, evidence):
    payload, calculation = pending['payload'], evidence['calculation']
    if (evidence['version'] != 'funded-batch-evidence-v1' or digest(evidence) != payload['evidence_digest']
            or evidence['period']['pool_id'] != pool_id or evidence['period']['id'] != payload['period_id']
            or evidence['period']['revision'] != payload['revision']
            or calculation['pool_id'] != pool_id or calculation['batch_digest'] != payload['calculation_digest']
            or calculation['period_digest'] != digest(evidence['period'])
            or calculation['batch_digest'] != digest({k: v for k, v in calculation.items() if k != 'batch_digest'})
            or calculation['eligibility_digest'] != digest(evidence['segments'])
            or evidence['checkpoint_after'] != payload['checkpoint_after']):
        raise ValueError('The sealed batch differs from its archived calculation')
    for key in ('start', 'end'):
        if timestamp(calculation[key]) != timestamp(payload[key]):
            raise ValueError('The sealed accounting boundary differs from the archive')
    for key in ('new_rewards', 'issued_atomic', 'scheduled_atomic', 'eligibility_digest'):
        if calculation[key] != payload[key]:
            raise ValueError('The sealed batch amounts or boundary differ from the archive')
    expected = {c['wallet']: integer(c['cumulative']) - integer(c['previous']) for c in pending['credits']}
    if expected != {w: integer(a) for w, a in payload['new_rewards'].items()}:
        raise ValueError('The sealed credits do not match the archived awards')
    for credit in pending['credits']:
        if integer(credit['previous']) != integer(evidence['prior_credits'].get(credit['wallet'], '0')):
            raise ValueError('The sealed prior credit differs from the archived ledger')


class EvidenceArchive:
    """Immutable private Storage objects, verified by a read-back before sealing."""
    def __init__(self, write_object, read_object, output_dir):
        self.write_object, self.read_object, self.output_dir = write_object, read_object, Path(output_dir)

    @staticmethod
    def key(pool_id, identity):
        import re
        if not valid_pool(pool_id) or not re.fullmatch('[0-9a-f]{64}', identity):
            raise ValueError('Invalid evidence object identity')
        return f'{pool_id}/{identity}.json.gz'

    def save(self, pool_id, evidence, publish):
        raw = canonical_bytes(evidence)
        if len(raw) > MAX_EVIDENCE_BYTES:
            raise ValueError('Evidence exceeds the reviewed archive size limit')
        identity = hashlib.sha256(raw).hexdigest()
        key = self.key(pool_id, identity)
        compressed = gzip.compress(raw, mtime=0)
        path = self.output_dir / key
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as handle:
                handle.write(compressed)
        except FileExistsError:
            # Local retries may reuse an evidence directory, but cannot replace
            # a different object at a content-addressed path.
            with gzip.open(path, 'rb') as handle:
                existing = handle.read(MAX_EVIDENCE_BYTES + 1)
            if existing != raw:
                raise ValueError('The local evidence file differs from its checksum')
        if publish:
            self.write_object(key, compressed)  # create-only, never overwrite
            if self.load(pool_id, identity) != evidence:
                raise ValueError('Archived evidence did not survive a verified read-back')
        return identity

    def load(self, pool_id, identity):
        import io
        compressed = self.read_object(self.key(pool_id, identity))
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as handle:
            raw = handle.read(MAX_EVIDENCE_BYTES + 1)
        if len(raw) > MAX_EVIDENCE_BYTES or hashlib.sha256(raw).hexdigest() != identity:
            raise ValueError('The archived evidence is missing or its checksum differs')
        return json.loads(raw)


class FundedRunner:
    def __init__(self, rpc, publisher, algod, indexer, archive, wait=time.sleep):
        self.rpc, self.publisher = rpc, publisher
        self.algod, self.indexer, self.archive, self.wait = algod, indexer, archive, wait

    def state(self, pool_id):
        return self.rpc('get_funded_reward_state', {'p_pool': pool_id})

    def commit(self, pool_id, name, args):
        holder = str(uuid4())
        self.rpc('acquire_funded_reward_lease', {'p_pool': pool_id, 'p_holder': holder, 'p_seconds': 180})
        try:
            return self.rpc(name, {'p_pool': pool_id, 'p_holder': holder, **args})
        finally:
            try:
                self.rpc('release_funded_reward_lease', {'p_pool': pool_id, 'p_holder': holder})
            except Exception:
                pass  # The bounded lease expires; stored evidence is unchanged.

    def finish_pending(self, pool_id, state, max_passes=50):
        pending = state['pending']
        evidence = self.archive.load(pool_id, pending['payload']['evidence_digest'])
        verify_batch_evidence(pool_id, pending, evidence)
        for attempt in range(max_passes):
            self.publisher(pool_id, False)
            state = self.state(pool_id)
            if state['pending'] is None:
                if (state['period']['id'] != pending['payload']['period_id']
                        or timestamp(state['period']['cursor']) != timestamp(pending['payload']['end'])):
                    raise ValueError('The completed batch did not advance its exact accounting cursor')
                return state
            if state['pending']['id'] != pending['id']:
                raise ValueError('The pending batch changed while publication was running')
            if attempt + 1 < max_passes:
                self.wait(4)
        raise RuntimeError('Publication is still pending; all sealed amounts and attempts are preserved')

    def available_funding(self, pool_id, state):
        snapshot = self.publisher(pool_id, True)['snapshot']
        config = state['config']
        if (snapshot['app_id'] != config['app_id'] or snapshot['genesis_id'] != GENESIS_ID
                or snapshot['genesis_hash'] != GENESIS or snapshot['active'] != '1' or snapshot['paused'] != '0'
                or integer(snapshot['allocated']) != integer(state['confirmed_allocated'])
                or integer(snapshot['round']) < integer(state['last_confirmed_round'])):
            raise ValueError('The funding snapshot differs from the active ledger')
        proposal = open_period(pool_id, config['app_id'], snapshot['at'],
            {**snapshot, 'active': 1, 'paused': 0})
        return snapshot, proposal['budget_atomic']

    def run_pool(self, pool_id, publish=False):
        if not valid_pool(pool_id):
            raise ValueError('Invalid pool UUID')
        state = self.state(pool_id)
        if state['pending'] is not None:
            if not publish:
                return {'pool_id': pool_id, 'action': 'resume', 'batch_id': state['pending']['id'], 'publish': False}
            state = self.finish_pending(pool_id, state)
        # At most one old window plus a new period opening per run. We do not
        # keep chasing a moving chain tip or allocate a backdated budget.
        for _ in range(2):
            period = state['period']
            if period is None or period['closed']:
                snapshot, available = self.available_funding(pool_id, state)
                if integer(available) == 0:
                    return {'pool_id': pool_id, 'action': 'await_funding', 'budget_atomic': '0', 'publish': publish}
                bundle = self.rpc('get_funded_stake_export', {'p_pool': pool_id})
                proposal = prepare_opening(pool_id, state, bundle, snapshot, self.algod, self.indexer)
                identity = self.archive.save(pool_id, proposal['evidence'], publish)
                if identity != proposal['checkpoint']['opening_evidence_digest']:
                    raise ValueError('Opening evidence checksum differs')
                if publish:
                    self.commit(pool_id, 'open_funded_reward_period', {'p_snapshot': snapshot,
                        'p_policy': proposal['policy_digest'], 'p_checkpoint': proposal['checkpoint']})
                return {'pool_id': pool_id, 'action': 'open_period', 'start': snapshot['at'],
                    'budget_atomic': proposal['evidence']['proposal']['budget_atomic'], 'publish': publish}
            restart_empty = integer(period['budget_atomic']) == 0
            if restart_empty:
                _, available = self.available_funding(pool_id, state)
                if integer(available) == 0:
                    return {'pool_id': pool_id, 'action': 'await_funding', 'budget_atomic': '0', 'publish': publish}
            tip = integer(self.algod('/v2/status')['last-round']) - 1
            latest = block_header(self.algod, tip)
            until = min(latest, timestamp(period['end']))
            if until <= timestamp(period['cursor']):
                return {'pool_id': pool_id, 'action': 'caught_up', 'cursor': period['cursor'], 'publish': publish}
            end_round = round_at_or_before(self.algod, iso(until), period['checkpoint']['round'], tip)
            bundle = self.rpc('get_funded_stake_export', {'p_pool': pool_id})
            proposal = prepare_batch(state, bundle, self.algod, self.indexer, end_round, iso(until))
            if proposal['action'] != 'seal':
                raise ValueError('The proposed accounting action changed')
            identity = self.archive.save(pool_id, proposal['evidence'], publish)
            if identity != proposal['payload']['evidence_digest']:
                raise ValueError('Batch evidence checksum differs')
            if not publish:
                return {'pool_id': pool_id, 'action': 'restart_empty_period' if restart_empty else 'seal', 'end': iso(until),
                    'issued_atomic': proposal['payload']['issued_atomic'],
                    **({'available_atomic': available} if restart_empty else {}), 'publish': False}
            self.commit(pool_id, 'seal_funded_reward_batch', {'p_payload': proposal['payload']})
            state = self.finish_pending(pool_id, self.state(pool_id))
            if not state['period']['closed']:
                if restart_empty:
                    # First finish and archive the zero-reward history. Closing
                    # only this empty period lets fresh funding start now;
                    # an existing funded monthly budget is never enlarged.
                    snapshot, available = self.available_funding(pool_id, state)
                    if integer(available) > 0:
                        self.commit(pool_id, 'close_empty_funded_reward_period', {
                            'p_period': state['period']['id'], 'p_revision': state['period']['revision'],
                            'p_snapshot': snapshot})
                        state = self.state(pool_id)
                        continue
                return {'pool_id': pool_id, 'action': 'confirmed', 'cursor': state['period']['cursor'], 'publish': True}
        raise RuntimeError('The bounded run finished without a stable period')
