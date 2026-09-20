import unittest
from copy import deepcopy
from funded_batch_fixture import A, B
from funded_reward_cap import capped_segments, reward_cap
from funded_reward_period import allocate_window

class RewardCapTests(unittest.TestCase):
    def test_timestamped_caps_preserve_stakes_and_conserve_budget(self):
        cap=reward_cap({'reward_stake_cap':'30','reward_stake_cap_from':'2020-01-16T12:00:00Z'})
        segments=[{'start':'2020-01-01T00:00:00Z','end':'2020-02-01T00:00:00Z','weights':{A:'100',B:'30'}}]
        before=deepcopy(segments)
        result=capped_segments(segments,cap)
        self.assertEqual(result[0]['weights'][A],'100')
        self.assertEqual(result[1]['weights'],{A:'30',B:'30'})
        self.assertEqual(segments,before)
        period={'version':'funded-monthly-v1','pool_id':'pool','app_id':'1','start':segments[0]['start'],
            'end':segments[0]['end'],'cursor':segments[0]['start'],'budget_atomic':'1000','scheduled_atomic':'0','issued_atomic':'0'}
        allocation=allocate_window(period,period['end'],result)
        self.assertEqual(sum(map(int,allocation['new_rewards'].values())),1000)
    def test_absent_caps_and_invalid_partial_configuration(self):
        self.assertIsNone(reward_cap({}))
        for data in [{'reward_stake_cap':'1'},{'reward_stake_cap':'0','reward_stake_cap_from':'2020-01-01T00:00:00Z'},
            {'reward_stake_cap':1,'reward_stake_cap_from':'2020-01-01T00:00:00Z'},
            {'reward_stake_cap':'NaN','reward_stake_cap_from':'2020-01-01T00:00:00Z'}]:
            with self.assertRaises((ValueError,TypeError)):
                reward_cap(data)
