"""Pure accounting for funded monthly payouts; no database writes or signing.

A period reserves only recognised deposits not already allocated on-chain.
It begins at the verified snapshot, never retroactively at a month boundary.
The caller must finish the previous period/batch before opening another and
provide complete, verified holding/stake history as contiguous eligibility
segments. Proposed amounts become claimable only after on-chain confirmation.
"""
from collections import defaultdict
from decimal import Decimal
from fractions import Fraction
from algosdk.encoding import is_valid_address
from reward_ledger import digest, integer, micros, month_bounds, timestamp

VERSION = "funded-monthly-v1"


def iso(at):
    return at.isoformat().replace("+00:00", "Z")


def open_period(pool_id, app_id, at, snapshot):
    start = timestamp(at)
    _, end = month_bounds(start)
    balance, deposited, allocated, paid = (integer(snapshot[key]) for key in ("balance", "deposited", "allocated", "paid"))
    if not paid <= allocated <= deposited or balance < allocated - paid:
        raise ValueError("The on-chain reserve could not be verified")
    if snapshot["active"] != 1 or snapshot["paused"] != 0:
        raise ValueError("The funded vault is not active")
    if integer(snapshot["round"]) == 0 or integer(app_id) == 0:
        raise ValueError("Missing chain identity or round")
    available = min(deposited - allocated, balance - (allocated - paid))
    return {"version": VERSION, "pool_id": pool_id, "app_id": str(app_id),
            "start": iso(start), "end": iso(end), "cursor": iso(start),
            "budget_atomic": str(available), "scheduled_atomic": "0", "issued_atomic": "0",
            "source_round": str(snapshot["round"]), "source_digest": digest(snapshot)}


def positive_weight(value):
    if isinstance(value, (float, bool)):
        raise ValueError("Eligibility weights must use exact decimal values")
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("Invalid eligibility weight")
    return Fraction(amount)


def allocate_window(period, until, segments):
    """Allocate one contiguous window, capped by its immutable funded budget.

    Segment weights must already be capped by verified wallet holdings. The
    share within each segment uses that segment's total stake, so top-ups and
    exits only affect the time after the event. Empty time is left unallocated.
    Integer rounding conserves every base unit; ties use wallet address order.
    """
    if period["version"] != VERSION:
        raise ValueError("Unexpected accounting version")
    start, end, cursor, until = (timestamp(value) for value in (period["start"], period["end"], period["cursor"], until))
    if not start <= cursor < until <= end:
        raise ValueError("The calculation has a gap, overlaps or exceeds the period")
    budget = integer(period["budget_atomic"])
    previous_scheduled, previous_issued = integer(period["scheduled_atomic"]), integer(period["issued_atomic"])
    duration = micros(end - start)
    expected_previous = budget * micros(cursor - start) // duration
    if previous_scheduled != expected_previous or previous_issued > previous_scheduled:
        raise ValueError("The period cursor and accounting totals disagree")
    scheduled_total = budget * micros(until - start) // duration
    emission = scheduled_total - previous_scheduled
    shares = defaultdict(Fraction)
    active_micros, position = 0, cursor
    for segment in segments:
        left, right = timestamp(segment["start"]), timestamp(segment["end"])
        if left != position or not left < right <= until:
            raise ValueError("Eligibility history is incomplete or overlaps")
        weights = {}
        for address, value in segment["weights"].items():
            if not is_valid_address(address):
                raise ValueError("Invalid reward recipient")
            amount = positive_weight(value)
            if amount:
                weights[address] = amount
        total, elapsed = sum(weights.values(), Fraction()), micros(right - left)
        if total:
            active_micros += elapsed
            for address, amount in weights.items():
                shares[address] += elapsed * amount / total
        position = right
    if position != until:
        raise ValueError("Eligibility history does not cover the complete window")
    award = emission * active_micros // micros(until - cursor)
    exact = {address: award * share / active_micros for address, share in shares.items()} if active_micros else {}
    issued = {address: value.numerator // value.denominator for address, value in exact.items()}
    remainder = award - sum(issued.values())
    for address in sorted(exact, key=lambda address: (-(exact[address] - issued[address]), address))[:remainder]:
        issued[address] += 1
    if sum(issued.values()) != award or not previous_issued + award <= scheduled_total <= budget:
        raise ValueError("Funded reward conservation failed")
    following = {**period, "cursor": iso(until), "scheduled_atomic": str(scheduled_total),
                 "issued_atomic": str(previous_issued + award)}
    result = {"version": VERSION, "pool_id": period["pool_id"], "app_id": period["app_id"],
              "start": iso(cursor), "end": iso(until), "period_digest": digest(period),
              "eligibility_digest": digest(segments), "scheduled_atomic": str(emission),
              "new_rewards": {address: str(value) for address, value in sorted(issued.items()) if value},
              "issued_atomic": str(award), "unallocated_atomic": str(emission - award), "next_period": following}
    return {**result, "batch_digest": digest(result)}
