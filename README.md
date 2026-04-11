# Algorand Soft Staking Infrastructure

Open source multi-pool soft staking infrastructure with Merkle proof reward distribution on Algorand.

Live implementation: [dao.polaris.city](https://dao.polaris.city)

---

## What This Is

A complete staking platform stack that lets any Algorand project run staking pools for their community. Supports single token staking, NFT staking, and LP token staking with daily, weekly, or monthly reward distribution.

**Key innovation:** Soft staking + off-chain Merkle proof rewards.

- Tokens **never leave the staker's wallet** — no locking, no custodianship
- Stakes are recorded in Supabase (off-chain), with on-chain balance verification at claim time
- Rewards are distributed via Merkle proofs published on-chain each epoch
- Scales to any number of stakers without per-user on-chain storage costs

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     STAKING FLOW                        │
│                                                         │
│  User wallet ──stake──► Supabase (user_stakes table)    │
│  (tokens stay in wallet)                                │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│                     EPOCH FLOW (periodic)               │
│                                                         │
│  generate-epoch.py                                      │
│    1. Fetch active stakes from Supabase                 │
│    2. Calculate cumulative rewards per staker           │
│    3. Build Merkle tree of (address → cumulative)       │
│    4. Store proofs + root in merkle_epoch_claims table  │
│                                                         │
│  Pool creator (or automated publisher)                  │
│    5. Call set_root(epoch_id, merkle_root) on contract  │
│    6. Root stored in on-chain box storage               │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│                     CLAIM FLOW                          │
│                                                         │
│  get-merkle-proof edge function                         │
│    1. Verify wallet still holds staked tokens           │
│    2. Fetch proof + epoch from Supabase                 │
│    3. Verify on-chain root matches expected root        │
│    4. Return proof to frontend                          │
│                                                         │
│  Smart contract (claim)                                 │
│    5. Verify Merkle proof against stored root           │
│    6. Pay delta = cumulative - last_claimed             │
│    7. Update user box with new epoch + cumulative       │
└─────────────────────────────────────────────────────────┘
```

### Cumulative Reward Model

Rewards are cumulative across epochs, not per-epoch. The contract stores the user's last claimed cumulative amount in a box. Each claim pays the **delta** between the new cumulative and the last claimed amount. This means:

- Users can skip epochs without losing rewards
- A single claim catches up all missed epochs
- The Merkle tree only needs one leaf per user (their latest cumulative total)

### Pool ID in Merkle Leaves

**Critical:** Each Merkle leaf is `SHA256(user_address || app_id || pool_id || epoch_id || amount)`. The `pool_id` is a uint64 stored in the contract's global state — **not** the UUID from your database. Always fetch it with `get_contract_pool_id(app_id)`. Using the wrong pool_id causes root mismatches and claim failures.

---

## Repository Structure

```
contracts/
  pyteal/
    main.py              # Contract router + compilation entry point
    state.py             # Global state keys and constants
    helpers.py           # Merkle verification, leaf hashing, utilities
    pool_setup.py        # initialize(), opt_in_asset(), fund_pool()
    user_operations.py   # opt_in_user(), claim_rewards(), delete_box()
    admin_operations.py  # set_epoch_root(), pause, deprecate, fee management
    budget_helper.py     # Minimal contract to extend opcode budget for claims
    merkle_utils.py      # Python Merkle tree implementation (matches contract)
  teal/
    proxy_approval.teal  # Legacy proxy contract (pre-PyTeal, for reference only)
    proxy_clear.teal     # Legacy proxy contract clear state
    # Note: active contract compiles to merkle_approval_v1.3.3_FINAL.teal (run main.py)

supabase/
  schema/
    001_core_schema.sql  # All tables: pools, user_stakes, merkle_epoch_claims, etc.
  functions/
    manage-stake/        # Deno edge function: stake / unstake with balance verification
    get-merkle-proof/    # Deno edge function: fetch proof + verify on-chain publication

scripts/
  generate-epoch.py      # Calculate rewards + build Merkle trees for all active pools

.env.example             # Environment variable template
```

---

## Smart Contract

### Deployment

```python
# Compile
cd contracts/pyteal
pip install pyteal
python main.py
# Outputs: contracts/teal/merkle_approval_v1.3.3_FINAL.teal

# Deploy (using algokit or any deployment tool)
# Pass these args to initialize():
# [0] sponsor_address    (32 bytes)
# [1] reward_token_id    (uint64 ASA ID)
# [2] pool_id            (uint64 - your unique pool identifier)
# [3] distribution_type  (0=daily, 1=weekly)
# [4] funding_model      (0=one-time, 1=rolling)
# [5] pool_start_date    (unix timestamp)
# [6] pool_end_date      (unix timestamp)
# [7] platform_fee_address (32 bytes)
# [8] publisher_address  (32 bytes, optional - for automated publishing)
```

### Global State Keys

| Key | Type | Description |
|-----|------|-------------|
| `admin` | bytes | Platform/admin address |
| `sponsor` | bytes | Pool creator address |
| `reward_token` | uint64 | Reward ASA ID |
| `pool_id` | uint64 | Unique pool identifier (used in Merkle leaves) |
| `dist_type` | uint64 | 0=daily, 1=weekly |
| `funding` | uint64 | 0=one-time, 1=rolling |
| `start_date` | uint64 | Pool start (unix timestamp) |
| `end_date` | uint64 | Pool end (unix timestamp, informational) |
| `epoch_id` | uint64 | Last published epoch |
| `deposited` | uint64 | Total rewards deposited |
| `paused` | uint64 | 0=active, 1=paused |
| `deprecated` | uint64 | 0=active, 1=deprecated (irreversible) |
| `publisher` | bytes | Address allowed to publish epoch roots |

### Box Storage

| Box name | Value | Description |
|----------|-------|-------------|
| `Itob(epoch_id)` | 32 bytes | Merkle root for that epoch |
| `SHA256(user_address \|\| pool_id)` | 16 bytes | User's last epoch (8) + last cumulative (8) |

### Claim Transaction Group

**First claim (no user box yet):**
```
[0] Payment: user → platform_fee_address (0.8 ALGO)
[1] Payment: user → contract (0.0217 ALGO box MBR)
[2] AppCall: claim(epoch_id, cumulative_amount, proof...)
```

**Subsequent claims:**
```
[0] Payment: user → platform_fee_address (0.8 ALGO)
[1] AppCall: claim(epoch_id, cumulative_amount, proof...)
```

**With budget helper** (for large proofs):
```
[0] Payment: user → platform_fee_address (0.8 ALGO)
[1] AppCall: budget_helper (NoOp) — adds 700 opcodes
[2] Payment: user → contract (0.0217 ALGO, first claim only)
[3] AppCall: claim(epoch_id, cumulative_amount, proof...)
```

---

## Edge Functions

Both edge functions run on Supabase Edge (Deno). Deploy with:

```bash
supabase functions deploy manage-stake
supabase functions deploy get-merkle-proof
```

Set these secrets in your Supabase project:

```bash
supabase secrets set SUPABASE_URL=...
supabase secrets set SUPABASE_SERVICE_ROLE_KEY=...
supabase secrets set NODELY_API_KEY=...    # optional
supabase secrets set FRONTEND_URL=https://your-domain.com
```

### manage-stake

Handles stake and unstake requests. Validates on-chain balance before writing to the database. Cross-pool balance check prevents double-staking the same tokens across multiple pools.

**Request:**
```json
{
  "action": "stake" | "unstake",
  "pool_id": "uuid",
  "wallet_address": "ALGO_ADDRESS",
  "amount": 100
}
```

### get-merkle-proof

Returns the Merkle proof for a user's latest epoch. Verifies:
1. Wallet still holds staked tokens (at claim time)
2. Epoch root is published on-chain and matches expected root

**Query params:** `?address=ALGO_ADDRESS&pool_id=UUID`
**Optional:** `&display_only=true` (skips balance check, for UI display)

---

## Epoch Generation

```bash
# Install dependencies
pip install supabase python-dotenv algosdk

# Copy environment
cp .env.example .env
# Fill in SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY

# Generate epochs for all active pools
python scripts/generate-epoch.py

# Generate for a specific pool
python scripts/generate-epoch.py --pool-id YOUR_POOL_UUID

# Dry run (calculate but don't save)
python scripts/generate-epoch.py --dry-run
```

The script:
- Skips pools that were generated recently (20h minimum for daily, 7 days for weekly)
- Carries forward previous cumulatives for stakers who were active in prior epochs
- Fetches `pool_id` from the contract global state (critical — never use a UUID hash)
- Applies safeguards against reward inflation (cap at 2× daily rate)
- Saves proofs and merkle roots to `merkle_epoch_claims` table

After generating, the pool creator (or automated publisher) calls `set_root` on the contract to publish the epoch. Only after publishing will rewards show as claimable.

---

## Key Implementation Notes

### Decimals — use `??` not `||`
Some tokens have 0 decimals (whole numbers only). Always use nullish coalescing:
```javascript
const decimals = pool.reward_token_decimals ?? 6  // ✅ correct
const decimals = pool.reward_token_decimals || 6  // ❌ wrong: 0 || 6 = 6
```

### Algorand asset pagination
Wallets with 1000+ assets require pagination. Always loop with `next-token`:
```javascript
let nextToken = null
do {
  const resp = await fetch(`${indexer}/v2/accounts/${address}/assets?limit=1000${nextToken ? `&next=${nextToken}` : ''}`)
  const data = await resp.json()
  // process data.assets...
  nextToken = data['next-token'] || null
} while (nextToken)
```

### Pool ID must come from contract
```python
# ✅ correct
pool_id = get_contract_pool_id(app_id)

# ❌ wrong — UUID hash won't match contract's uint64 pool_id
pool_id = int(hashlib.sha256(uuid.encode()).hexdigest()[:16], 16)
```

---

## Tests

The test suite covers contract behaviour end-to-end against a live Algorand node (testnet or localnet via AlgoKit).

### What is tested

| Suite | File | Covers |
|-------|------|--------|
| Admin operations | `tests/test_admin_operations.py` | `set_root`, `pause`/`unpause`, `deprecate`, `emergency_withdraw`, fee address proposal + execution |
| User claims | `tests/test_user_claims.py` | First claim (box creation), delta payments, zero-delta rejection, invalid Merkle proof rejection, missing fee rejection, backwards epoch rejection |

Each test asserts on-chain state directly — box contents, token balances, and transaction acceptance/rejection — rather than mocking.

### Setup

```bash
cd tests
pip install -r requirements.txt
```

Create a `.env` file in the repo root (copy from `.env.example`):

```env
ALGOD_URL=https://testnet-api.algonode.cloud
ALGOD_TOKEN=
ADMIN_MNEMONIC=your twenty five word mnemonic here ...
REWARD_ASSET_ID=12345678
CONTRACT_APP_ID=12345678
POOL_ID=12345678
FEE_ADDRESS=YOURFEEADDRESSHERE
```

### Run

```bash
# Run all suites
python -m pytest tests/ -v

# Or run suites individually
python -m pytest tests/test_admin_operations.py -v
python -m pytest tests/test_user_claims.py -v
```

### Test utilities

| File | Purpose |
|------|---------|
| `tests/utils/merkle_tree.py` | Pure-Python Merkle tree matching the on-chain SHA256 implementation |
| `tests/utils/contract_helper.py` | Helpers to build claim/set_root transaction groups and read box storage |
| `tests/utils/account_manager.py` | Funded test account management |
| `tests/fixtures/test_data.py` | Epoch scenario generation and delta calculation helpers |

---

## License

MIT

---

Built by [Polaris](https://dao.polaris.city) for the Algorand ecosystem.
