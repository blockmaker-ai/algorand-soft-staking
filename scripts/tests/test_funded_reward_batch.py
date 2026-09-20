import unittest
from copy import deepcopy
from funded_batch_fixture import A, B, ASSET, START, END, fixture, state, algod, indexer
from funded_reward_batch import prepare_batch


class FundedBatchTests(unittest.TestCase):
    def test_verified_nft_transfer_is_split_by_holding_time_before_sealing(self):
        result = prepare_batch(state(), fixture()['bundle'], algod, indexer, 200, END)
        self.assertEqual(result['action'], 'seal')
        self.assertEqual(result['payload']['issued_atomic'], '500')
        self.assertEqual(result['payload']['new_rewards'], {A: '250', B: '250'})
        checkpoint = result['payload']['checkpoint_after']
        self.assertEqual(checkpoint['holdings'], {A: {}, B: {str(ASSET): '1'}})
        self.assertEqual(checkpoint['stake_event_count'], '2')
        self.assertEqual(len(result['evidence']['segments']), 2)
        self.assertEqual(len(result['evidence']['holding_history']['transactions']), 1)

    def test_pending_batch_is_resumed_without_recalculation_or_provider_reads(self):
        current = state(); current['pending'] = {'id': 'sealed', 'credits': [{'wallet': A, 'cumulative': '310'}]}
        def forbidden(*args): raise AssertionError('A sealed batch must not be recalculated')
        result = prepare_batch(current, {}, forbidden, forbidden, 0, 'invalid')
        self.assertEqual(result, {'action': 'resume', 'batch': current['pending']})
        current['pending'] = None; current['period']['closed'] = True
        self.assertEqual(prepare_batch(current, {}, forbidden, forbidden, 0, 'invalid'), {'action': 'open_period_required'})

    def test_lagged_export_policy_changes_and_wrong_app_mapping_fail(self):
        for change in ('export', 'policy', 'mapping', 'ledger'):
            current, bundle = state(), fixture()['bundle']
            if change == 'export': bundle['exported_at'] = START
            elif change == 'policy': bundle['pools'][0]['asset_ids'] = [ASSET + 1]
            elif change == 'mapping': bundle['pools'][0]['app_id'] = '78'
            else: current['confirmed_allocated'] = '101'
            with self.assertRaises(ValueError): prepare_batch(current, bundle, algod, indexer, 200, END)

    def test_missing_changed_or_forgotten_historical_events_fail(self):
        for change in ('removed', 'changed', 'missing_wallet'):
            current, bundle = state(), fixture()['bundle']
            if change == 'removed': bundle['pools'][0]['stake_events'].pop()
            elif change == 'changed': bundle['pools'][0]['stake_events'][0]['amount_after'] = '2'
            else: del current['period']['checkpoint']['holdings'][A]
            with self.assertRaisesRegex(ValueError, 'journal|missing its checkpoint'):
                prepare_batch(current, bundle, algod, indexer, 200, END)

    def test_known_holdings_must_reconcile_with_the_previous_sealed_window(self):
        current = state(); current['period']['checkpoint']['holdings'][A][str(ASSET)] = '2'
        with self.assertRaisesRegex(ValueError, 'preceding sealed checkpoint'):
            prepare_batch(current, fixture()['bundle'], algod, indexer, 200, END)

    def test_a_new_wallet_uses_verified_history_and_only_participates_after_joining(self):
        current, bundle = state(), fixture()['bundle']
        # Bob first joins halfway through the window. He had no baseline, but
        # his real earlier holding is reconstructed rather than assumed zero.
        events = bundle['pools'][0]['stake_events']; bob = events.pop()
        from funded_reward_batch import event_prefix
        from reward_ledger import digest
        current['period']['checkpoint']['stake_event_count'] = '1'
        current['period']['checkpoint']['stake_event_digest'] = digest(event_prefix(events, START))
        del current['period']['checkpoint']['holdings'][B]
        bob['occurred_at'] = '2026-01-08T18:00:00Z'; events.append(bob)
        result = prepare_batch(current, bundle, algod, indexer, 200, END)
        self.assertEqual(result['payload']['new_rewards'], {A: '250', B: '250'})
        self.assertIn(B, result['payload']['checkpoint_after']['holdings'])


if __name__ == '__main__': unittest.main()
