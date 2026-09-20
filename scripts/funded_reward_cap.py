"""Timestamped per-wallet reward caps; existing stakes and credits are preserved."""
from decimal import Decimal

from funded_reward_period import iso, positive_weight
from reward_ledger import timestamp

def reward_cap(pool):
    amount, effective = pool.get('reward_stake_cap'), pool.get('reward_stake_cap_from')
    if amount is None and effective is None:
        return None
    if not isinstance(amount, str) or not isinstance(effective, str):
        raise ValueError('A reward cap needs an exact positive amount and effective timestamp')
    limit = positive_weight(amount)
    if limit <= 0 or limit > 2**64 - 1:
        raise ValueError('Invalid reward cap')
    return {'amount': format(Decimal(amount), 'f'), 'from': iso(timestamp(effective))}


def capped_segments(segments, cap):
    if cap is None:
        return segments
    effective, limit = timestamp(cap['from']), Decimal(cap['amount'])
    result = []
    for segment in segments:
        start, end = timestamp(segment['start']), timestamp(segment['end'])
        boundaries = [start, effective, end] if start < effective < end else [start, end]
        for left, right in zip(boundaries, boundaries[1:]):
            weights = segment['weights']
            if left >= effective:
                # positive_weight rejects floats, non-finite and negative values.
                for value in weights.values():
                    positive_weight(value)
                weights = {wallet: format(min(Decimal(value), limit), 'f') for wallet, value in weights.items()}
            result.append({'start': iso(left), 'end': iso(right), 'weights': dict(weights)})
    return result
