# Contract Tests

End-to-end tests for the Algorand soft staking smart contract. Tests run against a live Algorand node and assert on-chain state directly — box contents, token balances, and transaction acceptance/rejection.

## Structure

```
tests/
├── test_admin_operations.py   # set_root, pause, deprecate, emergency_withdraw, fee management
├── test_user_claims.py        # first claim, delta payments, proof/fee/epoch rejection cases
├── fixtures/
│   └── test_data.py           # epoch scenario generation, delta calculation helpers
└── utils/
    ├── merkle_tree.py         # SHA256 Merkle tree matching on-chain implementation
    ├── contract_helper.py     # transaction group builders, box storage readers
    └── account_manager.py     # funded test account management
```

## Setup

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` in the repo root and fill in:

```env
ALGOD_URL=https://testnet-api.algonode.cloud
ALGOD_TOKEN=
ADMIN_MNEMONIC=your twenty five word mnemonic ...
REWARD_ASSET_ID=12345678
CONTRACT_APP_ID=12345678
POOL_ID=12345678
FEE_ADDRESS=YOURFEEADDRESSHERE
```

You need a deployed contract instance and a funded admin account on testnet (or localnet via `algokit localnet start`).

## Run

```bash
# All tests (from repo root)
python -m pytest tests/ -v

# Individual suites (from repo root)
python -m pytest tests/test_admin_operations.py -v
python -m pytest tests/test_user_claims.py -v
```

## Test coverage

### Admin operations (`test_admin_operations.py`)
- ✅ Publish epoch root (`set_root`) — asserts box created with correct root
- ✅ Duplicate root rejected — same epoch cannot be published twice
- ✅ Non-admin rejected — only admin address can publish
- ✅ Pause contract — claims rejected while paused
- ✅ Unpause contract — claims accepted after unpause
- ✅ Deprecate pool — marks contract as deprecated (one-way)
- ✅ Emergency withdraw — sends all reward tokens to sponsor after deprecation
- ✅ Fee address proposal + execution — two-step fee address update

### User claims (`test_user_claims.py`)
- ✅ First claim — box created, epoch and cumulative stored correctly
- ✅ Subsequent claim — delta paid (cumulative_new − cumulative_prev), box updated
- ✅ Zero-delta rejected — cannot claim same epoch/cumulative twice
- ✅ Invalid Merkle proof rejected — wrong proof for address fails on-chain verification
- ✅ Missing fee payment rejected — claim without fee transaction fails
- ✅ Missing box payment on first claim — contract rejects if MBR not covered
- ✅ Backwards epoch rejected — cannot claim epoch older than last claimed
