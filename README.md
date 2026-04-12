# Algorand Soft Staking Infrastructure

Open source multi-pool soft staking infrastructure with Merkle proof reward distribution on Algorand.

**Smart contract: Algorand TypeScript (Puya)** — compiled to AVM bytecode via the [Puya compiler](https://github.com/algorandfoundation/puya-ts).

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
│    5. Call setEpochRoot(epoch_id, root) on contract     │
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
│  Smart contract (claimRewards)                          │
│    5. Verify Merkle proof against stored root           │
│    6. Pay delta = cumulative - last_claimed             │
│    7. Update user BoxMap with new epoch + cumulative    │
└─────────────────────────────────────────────────────────┘
```

### Cumulative Reward Model

Rewards are cumulative across epochs, not per-epoch. The contract stores the user's last claimed cumulative amount in a BoxMap. Each claim pays the **delta** between the new cumulative and the last claimed amount. This means:

- Users can skip epochs without losing rewards
- A single claim catches up all missed epochs
- The Merkle tree only needs one leaf per user (their latest cumulative total)

### Pool ID in Merkle Leaves

**Critical:** Each Merkle leaf is `SHA256(user_address || app_id || pool_id || epoch_id || amount)`. The `pool_id` is a uint64 stored in the contract's global state — **not** the UUID from your database. Always fetch it with `get_contract_pool_id(app_id)`. Using the wrong pool_id causes root mismatches and claim failures.

---

## Repository Structure

```
contracts/
  puya/
    StakingPool.algo.ts    # Contract source (Algorand TypeScript)
    artifacts/
      StakingPool.approval.teal   # Compiled approval program (AVM v11)
      StakingPool.clear.teal      # Compiled clear state program
      StakingPool.arc56.json      # ARC-56 application spec (methods, state, boxes)
      StakingPool.arc32.json      # ARC-32 application spec

supabase/
  schema/
    001_core_schema.sql    # All tables: pools, user_stakes, merkle_epoch_claims, etc.
  functions/
    manage-stake/          # Deno edge function: stake / unstake with balance verification
    get-merkle-proof/      # Deno edge function: fetch proof + verify on-chain publication
    save-monthly-snapshots/  # Deno edge function: monthly APY snapshots for rolling pools

scripts/
  generate-epoch.py        # Calculate rewards + build Merkle trees for all active pools
  utils/
    merkle_utils.py        # Python Merkle tree implementation (matches contract)

extras/
  algorand-matrix/         # Bonus: live blockchain visualiser (matrix rain effect)
    AlgorandMatrix.jsx     # Drop-in React component
    README.md              # Integration guide

.env.example               # Environment variable template
```

---

## Smart Contract

Written in [Algorand TypeScript](https://github.com/algorandfoundation/puya-ts) and compiled to TEAL bytecode via the Puya compiler. Targets AVM v11.

### Key improvements over PyTeal version

| Feature | PyTeal (old) | Puya (current) |
|---------|-------------|----------------|
| User registration | Required app opt-in (unnecessary MBR) | No opt-in needed — BoxMap storage |
| Opcode budget | Separate BudgetHelper contract | Built-in `ensureBudget` (OpUp pattern) |
| ABI | Manual routing | Typed ABI methods with ARC-56 spec |
| Claim fees | ~0.82 ALGO (first claim) | ~0.03 ALGO (first claim) |
| Platform fee | 0.8 ALGO per claim (on-chain) | Removed from contract (handled off-chain) |

### Build from Source

```bash
# Prerequisites: Node.js 18+, AlgoKit CLI
cd contracts/puya

# Install dependencies
npm install

# Compile contract (outputs to artifacts/)
algokit project run build
```

Or use the pre-compiled TEAL in `contracts/puya/artifacts/`.

### Deployment

```typescript
// Using the ARC-56 spec + AlgoKit Utils
import { StakingPoolClient } from './artifacts/StakingPoolClient'

const client = new StakingPoolClient({...})

// Deploy with create() — all parameters set at creation
await client.create.create({
  sponsor: sponsorAddress,      // Pool creator
  rewardTokenId: assetId,       // Reward ASA ID
  poolId: uniquePoolId,         // uint64 pool identifier
  fundingModel: 0,              // 0=one-time, 1=rolling
  startDate: unixTimestamp,
  endDate: unixTimestamp,
  publisher: publisherAddress,  // Automated epoch publisher
})

// Then: optInAsset → fundPool → setEpochRoot (per epoch)
```

### ABI Methods

| Method | Access | Description |
|--------|--------|-------------|
| `create(...)` | onCreate | Deploy and initialize pool |
| `optInAsset()` | Admin | Contract opts into reward ASA |
| `fundPool(axfer)` | Admin | Deposit reward tokens (grouped with AssetTransfer) |
| `claimRewards(epochId, amount, proof)` | Anyone | Claim with Merkle proof |
| `deleteBox()` | Anyone | Delete own claim box, recover MBR |
| `setEpochRoot(epochId, root)` | Admin/Publisher | Publish epoch Merkle root |
| `togglePause(state)` | Admin | Emergency pause/unpause |
| `toggleDeprecated(state)` | Admin | One-way deprecation |
| `emergencyWithdraw()` | Admin | Withdraw all rewards (deprecated pools only) |
| `updateEndDate(date)` | Admin | Update informational end date |
| `updatePublisher(addr)` | Admin | Change publisher address |

### Global State

| Key | Type | Description |
|-----|------|-------------|
| `admin` | Account | Platform/admin address |
| `sponsor` | Account | Pool creator address |
| `reward_token` | uint64 | Reward ASA ID |
| `pool_id` | uint64 | Unique pool identifier (used in Merkle leaves) |
| `funding` | uint64 | 0=one-time, 1=rolling |
| `start_date` | uint64 | Pool start (unix timestamp) |
| `end_date` | uint64 | Pool end (unix timestamp, informational) |
| `epoch_id` | uint64 | Last published epoch |
| `deposited` | uint64 | Total rewards deposited |
| `paused` | uint64 | 0=active, 1=paused |
| `deprecated` | uint64 | 0=active, 1=deprecated (irreversible) |
| `publisher` | Account | Address allowed to publish epoch roots |

### Box Storage

| Box key | Value | Description |
|---------|-------|-------------|
| `"u" + sender` (BoxMap) | `{lastEpoch: uint64, lastCumulative: uint64}` | User claim state |
| `itob(epoch_id)` (dynamic Box) | 32 bytes | Merkle root for that epoch |

### Claim Transaction Group

**First claim (no user box yet):**
```
[0] Payment: user → contract (0.0221 ALGO box MBR)
[1] AppCall: claimRewards(epochId, cumulativeAmount, proof)
    - fee: 0.005 ALGO (covers ensureBudget inner txns)
```

**Subsequent claims:**
```
[0] AppCall: claimRewards(epochId, cumulativeAmount, proof)
    - fee: 0.005 ALGO
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
- Applies safeguards against reward inflation (cap at 2x daily rate)
- Saves proofs and merkle roots to `merkle_epoch_claims` table

After generating, the pool creator (or automated publisher) calls `setEpochRoot` on the contract to publish the epoch. Only after publishing will rewards show as claimable.

---

## Key Implementation Notes

### Decimals — use `??` not `||`
Some tokens have 0 decimals (whole numbers only). Always use nullish coalescing:
```javascript
const decimals = pool.reward_token_decimals ?? 6  // correct
const decimals = pool.reward_token_decimals || 6  // wrong: 0 || 6 = 6
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
# correct
pool_id = get_contract_pool_id(app_id)

# wrong — UUID hash won't match contract's uint64 pool_id
pool_id = int(hashlib.sha256(uuid.encode()).hexdigest()[:16], 16)
```

---

## Security Model

### On-chain protections
- Merkle proof verification — only amounts authorized by the backend can be claimed
- Cumulative model with delta payment — prevents double-claiming any epoch
- Epoch monotonicity — roots are immutable, epoch IDs strictly increase
- Leaf hash binds address + app_id + pool_id + epoch_id + amount — prevents all replay vectors
- ASA balance check — contract verifies it holds enough tokens before transfer

### Off-chain protections
- Balance verification at stake time (manage-stake) and claim time (get-merkle-proof)
- Cross-pool balance deduplication — same tokens can't be staked in multiple pools
- Rate limiting (10 operations per 60s per wallet)
- Stake invalidation on balance shortfall (tokens moved out of wallet)
- Input validation (address format, UUID format, numeric bounds)

### Soft staking tradeoff
Tokens remain in the user's wallet. The system checks balances at epoch generation (snapshot) and at claim time, but does not track transaction history between these checkpoints. This is a deliberate design choice — full transaction monitoring would require expensive indexer queries and add latency, while the economic incentive to game soft staking (moving tokens between wallets between snapshots) is bounded by the per-epoch reward rate.

---

## Bonus: AlgorandMatrix

The `extras/algorand-matrix/` folder contains a drop-in React component that renders live Algorand transactions as a matrix-style falling character rain. It connects to public Algorand nodes (no API key needed) and color-codes transactions by type.

You can optionally highlight transactions from holders of specific tokens or NFT collections — just pass asset IDs or creator addresses as props.

See [`extras/algorand-matrix/README.md`](extras/algorand-matrix/README.md) for the full integration guide.

---

## License

MIT

---

Built by [Polaris](https://dao.polaris.city) for the Algorand ecosystem.
