import unittest
from copy import deepcopy
from pathlib import Path
import sys
from algosdk import account
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from funded_reward_period import allocate_window, open_period

A, B = sorted([account.generate_account()[1], account.generate_account()[1]])


class FundedPeriodTests(unittest.TestCase):
    def snapshot(self, **changes):
        return {"balance": "100", "deposited": "100", "allocated": "0", "paid": "0", "active": 1, "paused": 0, "round": "1", **changes}

    def period(self, budget=100):
        return open_period("pool", "42", "2026-09-01T00:00:00Z", self.snapshot(balance=str(budget), deposited=str(budget)))

    def segments(self, start="2026-09-01T00:00:00Z", end="2026-10-01T00:00:00Z", weights=None):
        return [{"start": start, "end": end, "weights": {A: "1", B: "1"} if weights is None else weights}]

    def test_unpaid_rewards_and_unrecognised_transfers_cannot_be_budgeted_again(self):
        before = self.snapshot(balance="60", allocated="70", paid="40")
        after = self.snapshot(balance="30", allocated="70", paid="70")
        extra = self.snapshot(balance="500", allocated="70", paid="40")
        for snapshot in (before, after, extra):
            self.assertEqual(open_period("pool", "42", "2026-09-19T12:00:00Z", snapshot)["budget_atomic"], "30")

    def test_existing_allocations_reserve_every_remaining_token(self):
        snapshot = self.snapshot(balance="1234567890000", deposited="1234567890000", allocated="1234567890000")
        self.assertEqual(open_period("pool", "42", "2026-09-19T12:00:00Z", snapshot)["budget_atomic"], "0")

    def test_late_start_is_not_backdated(self):
        period = open_period("pool", "42", "2026-09-19T12:34:56Z", self.snapshot())
        self.assertEqual(period["start"], "2026-09-19T12:34:56Z")
        self.assertEqual(period["end"], "2026-10-01T00:00:00Z")

    def test_impaired_and_inactive_vaults_fail_closed(self):
        for snapshot in (self.snapshot(allocated="101"), self.snapshot(paid="1"),
                         self.snapshot(balance="39", allocated="40"), self.snapshot(paused=1), self.snapshot(active=0)):
            with self.assertRaises(ValueError):
                open_period("pool", "42", "2026-09-01T00:00:00Z", snapshot)

    def test_entire_funded_month_is_conserved(self):
        result = allocate_window(self.period(), "2026-10-01T00:00:00Z", self.segments())
        self.assertEqual(result["new_rewards"], {A: "50", B: "50"})
        self.assertEqual(result["next_period"]["issued_atomic"], "100")
        self.assertEqual(result["unallocated_atomic"], "0")

    def test_moving_the_same_asset_between_wallets_cannot_duplicate_its_weight(self):
        segments = self.segments(end="2026-09-16T00:00:00Z", weights={A: "1", B: "0"})
        segments += self.segments(start="2026-09-16T00:00:00Z", weights={A: "0", B: "1"})
        result = allocate_window(self.period(), "2026-10-01T00:00:00Z", segments)
        self.assertEqual(result["new_rewards"], {A: "50", B: "50"})

    def test_topup_only_changes_rewards_after_its_recorded_time(self):
        segments = self.segments(end="2026-09-16T00:00:00Z")
        segments += self.segments(start="2026-09-16T00:00:00Z", weights={A: "3", B: "1"})
        result = allocate_window(self.period(400), "2026-10-01T00:00:00Z", segments)
        self.assertEqual(result["new_rewards"], {A: "250", B: "150"})

    def test_empty_time_does_not_create_payable_rewards(self):
        segments = self.segments(end="2026-09-16T00:00:00Z", weights={})
        segments += self.segments(start="2026-09-16T00:00:00Z", weights={A: "1"})
        result = allocate_window(self.period(), "2026-10-01T00:00:00Z", segments)
        self.assertEqual(result["new_rewards"], {A: "50"})
        self.assertEqual(result["unallocated_atomic"], "50")

    def test_resumed_windows_and_retries_keep_the_same_budget(self):
        period = self.period(101)
        first = allocate_window(period, "2026-09-16T00:00:00Z", self.segments(end="2026-09-16T00:00:00Z"))
        self.assertEqual(first, allocate_window(period, "2026-09-16T00:00:00Z", self.segments(end="2026-09-16T00:00:00Z")))
        second = allocate_window(first["next_period"], "2026-10-01T00:00:00Z", self.segments(start="2026-09-16T00:00:00Z"))
        self.assertEqual(int(first["issued_atomic"]) + int(second["issued_atomic"]), 101)
        with self.assertRaises(ValueError):
            allocate_window(first["next_period"], "2026-09-16T00:00:00Z", [])

    def test_incomplete_or_overlapping_eligibility_cannot_be_published(self):
        for segments in ([], self.segments(start="2026-09-02T00:00:00Z"), self.segments() * 2):
            with self.assertRaises(ValueError):
                allocate_window(self.period(), "2026-10-01T00:00:00Z", segments)

    def test_large_integer_rounding_is_exact_and_invalid_weights_are_rejected(self):
        budget = 3141592653000000
        result = allocate_window(self.period(budget), "2026-10-01T00:00:00Z", self.segments(weights={A: "1", B: "2"}))
        self.assertEqual(sum(map(int, result["new_rewards"].values())), budget)
        for weights in ({A: -1}, {A: 0.1}, {"invalid": "1"}):
            with self.assertRaises(ValueError):
                allocate_window(self.period(), "2026-10-01T00:00:00Z", self.segments(weights=weights))
        corrupt = deepcopy(self.period()); corrupt["issued_atomic"] = "1"
        with self.assertRaises(ValueError):
            allocate_window(corrupt, "2026-10-01T00:00:00Z", self.segments())
