import os
from pathlib import Path
import subprocess
import sys
import unittest

class ConfiguredNetworkTests(unittest.TestCase):
    def test_history_verification_uses_the_explicit_network(self):
        root=Path(__file__).resolve().parents[1]
        code="""
import os
from funded_batch_fixture import fixture,state,algod,indexer,END
from funded_reward_batch import prepare_batch

def configured(path):
    result=algod(path)
    if 'block' in result:
        result['block']['gen']=os.environ['ALGORAND_GENESIS_ID']
        result['block']['gh']=os.environ['ALGORAND_GENESIS_HASH']
    return result
result=prepare_batch(state(),fixture()['bundle'],configured,indexer,200,END)
assert result['payload']['issued_atomic']=='500'
assert result['evidence']['holding_history']['network']=='local-fixture-v1'
try:
    prepare_batch(state(),fixture()['bundle'],algod,indexer,200,END)
except ValueError:
    pass
else:
    raise AssertionError('Another network was accepted')
"""
        env={**os.environ,'ALGORAND_GENESIS_ID':'local-fixture-v1',
            'ALGORAND_GENESIS_HASH':'AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE=',
            'PYTHONPATH':str(root)+os.pathsep+str(root/'tests'),'PYTHONDONTWRITEBYTECODE':'1'}
        subprocess.run([sys.executable,'-B','-c',code],env=env,check=True,capture_output=True,text=True)
