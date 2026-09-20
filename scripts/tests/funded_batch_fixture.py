"""Deterministic chain fixture shared by Python and PostgreSQL integration tests."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from algosdk.encoding import encode_address
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from funded_reward_batch import asset_policy, event_prefix, prepare_batch
from reward_ledger import digest

A, B = encode_address(bytes([1]) * 32), encode_address(bytes([2]) * 32)
POOL, ASSET = '00000000-0000-4000-8000-000000000001', 1234
START, MIDDLE, END = '2026-01-01T00:00:00Z', '2026-01-08T18:00:00Z', '2026-01-16T12:00:00Z'
GENESIS = 'wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8='


def fixture():
    events = [{'id': i, 'pool_id': POOL, 'stake_id': f'stake-{i}', 'wallet_address': wallet,
        'occurred_at': '2025-12-31T23:59:00Z', 'amount_before': '0', 'amount_after': '1',
        'active_before': False, 'active_after': True} for i, wallet in enumerate([A, B], 1)]
    pool = {'pool_id': POOL, 'app_id': '77', 'name': 'fixture', 'nft_pool': True, 'nft_indexed': True,
        'asset_ids': [ASSET], 'decimals': 0, 'stake_events': events}
    policy = digest(asset_policy(pool))
    config = {'app_id': '77', 'reward_asset_id': str(ASSET), 'chain_pool_id': '42', 'opening_budget': '100',
        'admin': A,
        'buyback': B,
        'publisher': A, 'approval_sha256': 'a' * 64, 'manifest_sha256': 'a' * 64, 'asset_policy_digest': policy}
    snapshot = {'app_id': '77', 'round': '100', 'at': START, 'genesis_id': 'mainnet-v1.0', 'genesis_hash': GENESIS,
        'deposited': '1100', 'allocated': '100', 'paid': '0', 'balance': '1100', 'active': '1', 'paused': '0'}
    checkpoint = {'round': '100', 'at': START, 'block_at': START, 'next_block_at': '2026-01-01T00:00:03Z',
        'policy_digest': policy, 'evidence_digest': 'a' * 64, 'stake_export_digest': 'b' * 64,
        'stake_event_count': '2', 'stake_event_digest': digest(event_prefix(events, START)),
        'holdings': {A: {str(ASSET): '1'}, B: {}}}
    return {'config': config, 'snapshot': snapshot, 'opening': {A: '60', B: '40'}, 'checkpoint': checkpoint,
        'bundle': {'mode': 'eligibility_preview', 'exported_at': END, 'pools': [pool]}}


def block_time(round_number):
    if round_number in (100, 101): base, offset = START, round_number - 100
    elif round_number in (200, 201): base, offset = END, round_number - 200
    else: raise ValueError('Unexpected fixture block')
    return int(datetime.fromisoformat(base.replace('Z', '+00:00')).timestamp()) + 3 * offset


def algod(path):
    if '/blocks/' in path:
        r = int(path.split('/blocks/')[1].split('?')[0])
        return {'block': {'rnd': r, 'ts': block_time(r), 'gen': 'mainnet-v1.0', 'gh': GENESIS}}
    wallet = path.rsplit('/', 1)[1]
    return {'address': wallet, 'round': 400, 'total-assets-opted-in': 1,
        'assets': [{'asset-id': ASSET, 'amount': int(wallet == B)}]}


def indexer(path):
    if path == '/health': return {'round': 400}
    txn = {'id': 'FIXTURE-TRANSFER', 'tx-type': 'axfer', 'sender': A, 'confirmed-round': 150,
        'round-time': int(datetime.fromisoformat(MIDDLE.replace('Z', '+00:00')).timestamp()),
        'asset-transfer-transaction': {'asset-id': ASSET, 'receiver': B, 'amount': 1}}
    return {'current-round': 400, 'transactions': [txn]}


def state():
    f = fixture()
    return {'config': f['config'], 'credits': f['opening'], 'confirmed_allocated': '100', 'pending': None,
        'period': {'version': 'funded-monthly-v1', 'id': '00000000-0000-4000-8000-000000000010',
            'pool_id': POOL, 'app_id': '77', 'start': START, 'end': '2026-02-01T00:00:00Z', 'cursor': START,
            'budget_atomic': '1000', 'scheduled_atomic': '0', 'issued_atomic': '0', 'revision': '0',
            'closed': False, 'policy_digest': f['config']['asset_policy_digest'],
            'checkpoint': f['checkpoint'], 'checkpoint_digest': 'd' * 64}}


if __name__ == '__main__':
    if sys.argv[1:] == ['--fixture']:
        print(json.dumps(fixture()))
    else:
        print(json.dumps(prepare_batch(json.load(sys.stdin), fixture()['bundle'], algod, indexer, 200, END)))
