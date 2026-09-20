"""One explicitly configured Algorand network for each operator process."""
import os
import base64

GENESIS_ID = os.environ.get('ALGORAND_GENESIS_ID', 'mainnet-v1.0')
GENESIS_HASH = os.environ.get('ALGORAND_GENESIS_HASH', 'wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=')
if not GENESIS_ID or len(base64.b64decode(GENESIS_HASH, validate=True)) != 32:
    raise ValueError('Configure an exact Algorand genesis ID and hash')
