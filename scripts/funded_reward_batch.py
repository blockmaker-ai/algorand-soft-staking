"""Connect verified chain/stake history to the durable funded-reward ledger.

All functions are read-only. A proposal is not a credit: the operator must save
its evidence, seal it through the service-only RPC, persist transaction identities
before broadcast, then independently verify confirmations. A pending batch is
always resumed as stored instead of recalculated from new balances or stakes.
"""
from datetime import datetime, timezone
import re

from funded_reward_period import allocate_window, iso
from funded_reward_cap import capped_segments, reward_cap
from reward_ledger import digest, integer, timestamp
from verified_holdings import address, eligibility_segments, verified_window


def asset_policy(pool):
    if type(pool['nft_pool']) is not bool or (pool['nft_pool'] and pool.get('nft_indexed') is not True):
        raise ValueError('The staking asset policy is not verified')
    assets = [integer(asset) for asset in pool['asset_ids']]
    decimals = integer(pool['decimals'])
    if not assets or 0 in assets or len(set(assets)) != len(assets) or decimals > 19:
        raise ValueError('Invalid staking asset policy')
    if (pool['nft_pool'] and decimals != 0) or (not pool['nft_pool'] and len(assets) != 1):
        raise ValueError('Staking policy type or decimals differ')
    return {'version': 'funded-asset-policy-v1', 'pool_id': pool['pool_id'], 'nft_pool': pool['nft_pool'],
        'asset_decimals': {str(asset): str(decimals) for asset in sorted(assets)},
        'reward_cap': reward_cap(pool)}


def event_prefix(events, until):
    return sorted((event for event in events if timestamp(event['occurred_at']) <= timestamp(until)),
        key=lambda event: (timestamp(event['occurred_at']), integer(event['id'])))


def make_checkpoint(window, events, policy_digest, export_digest):
    """Seal sparse holdings plus the entire journal prefix, including exits."""
    evidence = window['evidence']
    if len(evidence['following_block_times']) != 2:
        raise ValueError('Accounting checkpoints require adjacent block verification')
    prefix = event_prefix(events, window['end'])
    return {'round': str(window['end_round']), 'at': iso(timestamp(window['end'])),
        'block_at': iso(datetime.fromtimestamp(evidence['block_times'][1], timezone.utc)),
        'next_block_at': iso(datetime.fromtimestamp(evidence['following_block_times'][1], timezone.utc)),
        'policy_digest': policy_digest, 'evidence_digest': window['evidence_digest'],
        'stake_export_digest': export_digest, 'stake_event_count': str(len(prefix)), 'stake_event_digest': digest(prefix),
        'holdings': {wallet: {str(asset): str(integer(amount)) for asset, amount in values.items() if integer(amount)}
            for wallet, values in sorted(window['end_holdings'].items())}}


def prepare_batch(state, bundle, algod_read, indexer_read, end_round, until, destroyed_supply=None):
    """Return either a stored batch to resume or a fully evidenced new proposal.

    The source export must lock stake writers and cover the chosen endpoint.
    New participants are allowed only when their first journal event follows
    the previous cursor; their earlier holdings are reconstructed, never guessed.
    Previously observed wallets remain in the checkpoint after leaving a pool.
    """
    if state.get('pending') is not None:
        return {'action': 'resume', 'batch': state['pending']}
    period = state.get('period')
    if not period or period['closed'] is True:
        return {'action': 'open_period_required'}
    if period['closed'] is not False:
        raise ValueError('Invalid period status')
    config = state['config']
    pools = [pool for pool in bundle['pools'] if pool['pool_id'] == period['pool_id']]
    if len(pools) != 1:
        raise ValueError('The stake export does not identify exactly one matching pool')
    pool = pools[0]
    if integer(pool['app_id']) != integer(config['app_id']) or integer(period['app_id']) != integer(config['app_id']):
        raise ValueError('Vault mapping differs from the stake export or period')
    policy = asset_policy(pool)
    policy_digest = digest(policy)
    if policy_digest != period['policy_digest'] or policy_digest != config['asset_policy_digest']:
        raise ValueError('The pinned staking asset policy has changed')
    if sum(integer(value) for value in state['credits'].values()) != integer(state['confirmed_allocated']):
        raise ValueError('Confirmed wallet credits disagree with the global ledger')
    for wallet in state['credits']:
        address(wallet)
    until_at, cursor = timestamp(until), timestamp(period['cursor'])
    if not cursor < until_at <= timestamp(period['end']) or timestamp(bundle['exported_at']) < until_at:
        raise ValueError('The verified stake export does not cover a contiguous period window')
    checkpoint = period['checkpoint']
    if timestamp(checkpoint['at']) != cursor or checkpoint['policy_digest'] != policy_digest:
        raise ValueError('The preceding checkpoint does not match this period')
    if not re.fullmatch('[0-9a-f]{64}', period['checkpoint_digest']):
        raise ValueError('Missing database checkpoint identity')
    events = pool['stake_events'] or []
    if any(event['pool_id'] != period['pool_id'] for event in events):
        raise ValueError('Stake events contain another pool')
    prefix = event_prefix(events, period['cursor'])
    if len(prefix) != integer(checkpoint['stake_event_count']) or digest(prefix) != checkpoint['stake_event_digest']:
        raise ValueError('The preceding stake journal has changed or is incomplete')
    known_wallets = set(checkpoint['holdings'])
    if any(event['wallet_address'] not in known_wallets for event in prefix):
        raise ValueError('A previously registered wallet is missing its checkpoint')
    wallets = known_wallets | {event['wallet_address'] for event in event_prefix(events, until)}
    assets = {integer(asset) for asset in policy['asset_decimals']}
    window = verified_window(algod_read, indexer_read, wallets, assets,
        integer(checkpoint['round']), integer(end_round), destroyed_supply=destroyed_supply,
        accounting_start=period['cursor'], accounting_end=until)
    for wallet in known_wallets:
        previous = checkpoint['holdings'][wallet]
        if any(integer(asset) not in assets for asset in previous):
            raise ValueError('The checkpoint contains an unapproved staking asset')
        if window['initial_holdings'][wallet] != {asset: integer(previous.get(str(asset), '0')) for asset in assets}:
            raise ValueError('Holding history differs from the preceding sealed checkpoint')
    segments = eligibility_segments(events, window['initial_holdings'], window['changes'],
        policy['asset_decimals'], policy['nft_pool'], integer(checkpoint['round']), period['cursor'], until)
    cap = reward_cap(pool)
    segments = capped_segments(segments, cap)
    calculation = allocate_window(period, until, segments)
    after = make_checkpoint(window, events, policy_digest, digest(bundle))
    evidence = {'version': 'funded-batch-evidence-v1', 'policy': policy, 'stake_export': bundle,
        'period': period, 'prior_credits': state['credits'], 'checkpoint_after': after,
        'holding_history': window['evidence'], 'reward_cap': cap, 'segments': segments, 'calculation': calculation}
    payload = {'period_id': period['id'], 'revision': period['revision'], 'start': period['cursor'],
        'end': iso(until_at), 'checkpoint_before_digest': period['checkpoint_digest'], 'checkpoint_after': after,
        'eligibility_digest': calculation['eligibility_digest'], 'calculation_digest': calculation['batch_digest'],
        'evidence_digest': digest(evidence), 'scheduled_atomic': calculation['scheduled_atomic'],
        'issued_atomic': calculation['issued_atomic'], 'new_rewards': calculation['new_rewards']}
    return {'action': 'seal', 'payload': payload, 'evidence': evidence}
