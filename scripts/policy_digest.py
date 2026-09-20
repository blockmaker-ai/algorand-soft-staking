"""Read a generic staking policy on stdin and print its canonical identity."""
import json
import sys
from funded_reward_batch import asset_policy
from reward_ledger import digest

if __name__ == '__main__':
    policy = asset_policy(json.load(sys.stdin))
    print(json.dumps({'policy': policy, 'digest': digest(policy)}))
