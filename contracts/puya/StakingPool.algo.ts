import type { bytes, uint64 } from '@algorandfoundation/algorand-typescript'
import {
  abimethod,
  Account,
  assert,
  assertMatch,
  Asset,
  BigUint,
  Box,
  BoxMap,
  Bytes,
  clone,
  Contract,
  ensureBudget,
  Global,
  GlobalState,
  gtxn,
  itxn,
  log,
  op,
  OpUpFeeSource,
  Txn,
  Uint64,
  urange,
} from '@algorandfoundation/algorand-typescript'
import { extract, sha256 } from '@algorandfoundation/algorand-typescript/op'

/**
 * Polaris Staking Pool Contract (Algorand TypeScript / Puya)
 *
 * Merkle proof-based reward distribution for soft staking.
 * Users' tokens remain in their wallets — no custody required.
 * Rewards are verified via cumulative Merkle proofs and paid as deltas.
 *
 * Design:
 *  - Box storage eliminates user opt-in (BoxMap replaces LocalState)
 *  - Cumulative claims — skip epochs without losing rewards
 *  - Sorted-pair Merkle proofs (OpenZeppelin standard)
 *  - Leaf hash binds: address, app_id, pool_id, epoch_id, amount
 *  - Immutable epoch roots with monotonic ordering
 *  - ensureBudget replaces separate BudgetHelper contract
 *  - Typed ABI methods with auto-generated client
 */

// ==========================================================================
// TYPES
// ==========================================================================

type UserClaimState = {
  lastEpoch: uint64
  lastCumulative: uint64
}

// ==========================================================================
// CONSTANTS
// ==========================================================================

// Box MBR for user claim box: 2500 + 400 * (keyPrefix(1) + account(32) + value(16)) = 22,100 µA
const USER_BOX_MBR: uint64 = 22_100
const EPOCH_ROOT_SIZE: uint64 = 32
// Max Merkle proof depth — supports up to 2^12 = 4,096 stakers per pool
const MAX_PROOF_DEPTH: uint64 = 12

// ==========================================================================
// CONTRACT
// ==========================================================================

export class StakingPool extends Contract {
  // --- Global State ---
  admin = GlobalState<Account>({ key: 'admin' })
  sponsor = GlobalState<Account>({ key: 'sponsor' })
  rewardTokenId = GlobalState<uint64>({ key: 'reward_token' })
  poolId = GlobalState<uint64>({ key: 'pool_id' })
  fundingModel = GlobalState<uint64>({ key: 'funding' }) // 0 = one-time, 1 = rolling
  poolStartDate = GlobalState<uint64>({ key: 'start_date' })
  poolEndDate = GlobalState<uint64>({ key: 'end_date' })
  currentEpochId = GlobalState<uint64>({ key: 'epoch_id' })
  totalDeposited = GlobalState<uint64>({ key: 'deposited' })
  paused = GlobalState<uint64>({ key: 'paused' }) // 0 = active, 1 = paused
  deprecated = GlobalState<uint64>({ key: 'deprecated' }) // 0 = active, 1 = deprecated (one-way)
  publisher = GlobalState<Account>({ key: 'publisher' }) // Authorized epoch root publisher

  // --- Box Storage ---
  // Per-user claim tracking: last claimed epoch + cumulative amount
  userClaims = BoxMap<Account, UserClaimState>({ keyPrefix: 'u' })
  // Epoch roots stored as dynamic Box<bytes> keyed by itob(epochId)

  // ========================================================================
  // CONTRACT CREATION
  // ========================================================================

  @abimethod({ onCreate: 'require' })
  public create(
    sponsor: Account,
    rewardTokenId: uint64,
    poolId: uint64,
    fundingModel: uint64,
    startDate: uint64,
    endDate: uint64,
    publisher: Account,
  ): void {
    assert(sponsor !== Global.zeroAddress, 'Invalid sponsor')
    assert(publisher !== Global.zeroAddress, 'Invalid publisher')
    assert(rewardTokenId > 0, 'Invalid reward token')
    assert(poolId > 0, 'Invalid pool ID')
    assert(fundingModel === 0 || fundingModel === 1, 'Invalid funding model')

    this.admin.value = Txn.sender
    this.sponsor.value = sponsor
    this.rewardTokenId.value = rewardTokenId
    this.poolId.value = poolId
    this.fundingModel.value = fundingModel
    this.poolStartDate.value = startDate
    this.poolEndDate.value = endDate
    this.currentEpochId.value = Uint64(0)
    this.totalDeposited.value = Uint64(0)
    this.paused.value = Uint64(0)
    this.deprecated.value = Uint64(0)
    this.publisher.value = publisher
  }

  // ========================================================================
  // POOL SETUP
  // ========================================================================

  /**
   * Contract opts into the reward token ASA.
   * Must be called after creation and before funding.
   * Admin only.
   */
  public optInAsset(): void {
    assert(Txn.sender === this.admin.value, 'Admin only')

    itxn
      .assetTransfer({
        xferAsset: Asset(this.rewardTokenId.value),
        assetReceiver: Global.currentApplicationAddress,
        assetAmount: 0,
        fee: 0,
      })
      .submit()
  }

  /**
   * Deposit reward tokens into the pool.
   * One-time pools can only be funded once; rolling pools allow multiple deposits.
   *
   * Expects a grouped AssetTransfer from the sponsor preceding this call.
   */
  public fundPool(axfer: gtxn.AssetTransferTxn): void {
    assert(Txn.sender === this.admin.value, 'Admin only')

    assertMatch(axfer, {
      xferAsset: Asset(this.rewardTokenId.value),
      assetReceiver: Global.currentApplicationAddress,
      sender: this.sponsor.value,
    })
    assert(axfer.assetAmount > 0, 'Amount must be positive')

    // One-time pools: enforce single funding
    if (this.fundingModel.value === Uint64(0)) {
      assert(this.totalDeposited.value === Uint64(0), 'Already funded')
    }

    this.totalDeposited.value = this.totalDeposited.value + axfer.assetAmount
  }

  // ========================================================================
  // USER OPERATIONS
  // ========================================================================

  /**
   * Claim rewards using a Merkle proof.
   *
   * CUMULATIVE MODEL: Each epoch's Merkle tree stores total-to-date amounts.
   * The contract pays delta = (newCumulative - lastCumulative).
   * Users can skip epochs without losing rewards — a single claim catches up.
   *
   * Transaction group structure:
   *   First claim:  [ Payment(boxMBR -> contract), AppCall(claimRewards) ]
   *   Subsequent:   [ AppCall(claimRewards) ]
   *
   * @param epochId - Epoch to claim from (must be published)
   * @param cumulativeAmount - Total rewards to date (from Merkle tree leaf)
   * @param proof - Concatenated 32-byte Merkle proof elements
   */
  public claimRewards(epochId: uint64, cumulativeAmount: uint64, proof: bytes): void {
    // Ensure sufficient opcode budget for Merkle proof verification
    ensureBudget(1400, OpUpFeeSource.GroupCredit)

    // --- Input validation ---
    assert(cumulativeAmount > 0, 'Amount must be positive')
    assert(this.paused.value === Uint64(0), 'Pool is paused')
    assert(this.deprecated.value === Uint64(0), 'Pool is deprecated')
    assert(Global.latestTimestamp >= this.poolStartDate.value, 'Pool not started')
    assert(epochId > 0, 'Invalid epoch')
    assert(epochId <= this.currentEpochId.value, 'Epoch not published')

    const proofCount: uint64 = proof.length / Uint64(32)
    assert(proofCount <= MAX_PROOF_DEPTH, 'Proof too deep')

    // --- Fetch epoch Merkle root from box ---
    const epochRootBox = Box<bytes>({ key: op.itob(epochId) })
    const [epochRoot, rootExists] = epochRootBox.maybe()
    assert(rootExists, 'Epoch root not found')
    assert(epochRoot.length === EPOCH_ROOT_SIZE, 'Invalid root')

    // --- Determine user claim state ---
    let lastEpochClaimed: uint64 = Uint64(0)
    let lastCumulativeClaimed: uint64 = Uint64(0)

    if (this.userClaims(Txn.sender).exists) {
      // Subsequent claim — read existing state
      const claimState = clone(this.userClaims(Txn.sender).value)
      lastEpochClaimed = claimState.lastEpoch
      lastCumulativeClaimed = claimState.lastCumulative
    } else {
      // First claim — validate box MBR payment in preceding transaction
      const mbrPayment = gtxn.PaymentTxn(Txn.groupIndex - Uint64(1))
      assert(mbrPayment.sender === Txn.sender, 'MBR sender mismatch')
      assert(mbrPayment.receiver === Global.currentApplicationAddress, 'MBR receiver mismatch')
      assert(mbrPayment.amount >= USER_BOX_MBR, 'Insufficient MBR')
    }

    // --- Validate claim progression ---
    assert(epochId > lastEpochClaimed, 'Must claim newer epoch')
    assert(cumulativeAmount >= lastCumulativeClaimed, 'Cumulative cannot decrease')

    const deltaAmount: uint64 = cumulativeAmount - lastCumulativeClaimed
    assert(deltaAmount > 0, 'Nothing to claim')

    // --- Verify Merkle proof ---
    const leafHash: bytes = this.computeLeafHash(Txn.sender.bytes, epochId, cumulativeAmount)
    this.verifyMerkleProof(leafHash, proof, proofCount, epochRoot)

    // --- Verify sufficient token balance ---
    const rewardAsset = Asset(this.rewardTokenId.value)
    const [asaBalance, hasBalance] = op.AssetHolding.assetBalance(Global.currentApplicationAddress, rewardAsset)
    assert(hasBalance, 'Token not opted in')
    assert(asaBalance >= deltaAmount, 'Insufficient funds')

    // --- Update user claim state ---
    const newState: UserClaimState = {
      lastEpoch: epochId,
      lastCumulative: cumulativeAmount,
    }
    this.userClaims(Txn.sender).value = clone(newState)

    // --- Transfer reward tokens ---
    itxn
      .assetTransfer({
        xferAsset: rewardAsset,
        assetReceiver: Txn.sender,
        assetAmount: deltaAmount,
        fee: 0,
      })
      .submit()
  }

  /**
   * Delete user's claim box and recover MBR.
   * Returns (boxMBR - minTxnFee) to the caller.
   */
  public deleteBox(): void {
    assert(this.userClaims(Txn.sender).exists, 'No box to delete')

    this.userClaims(Txn.sender).delete()

    itxn
      .payment({
        receiver: Txn.sender,
        amount: USER_BOX_MBR - Global.minTxnFee,
        fee: 0,
      })
      .submit()

    log(Bytes('BOX_DELETE'))
  }

  // ========================================================================
  // ADMIN OPERATIONS
  // ========================================================================

  /**
   * Publish a new Merkle root for an epoch.
   * Callable by admin or delegated publisher.
   * Roots are immutable — cannot republish the same epoch.
   * Epoch IDs must be strictly monotonically increasing.
   */
  public setEpochRoot(epochId: uint64, root: bytes): void {
    assert(
      Txn.sender === this.admin.value || Txn.sender === this.publisher.value,
      'Not authorized',
    )
    assert(root.length === EPOCH_ROOT_SIZE, 'Invalid root size')
    assert(epochId > 0, 'Invalid epoch')
    assert(this.deprecated.value === Uint64(0), 'Pool deprecated')

    // Epoch must be monotonically increasing
    const currentEpoch: uint64 = this.currentEpochId.value
    assert(currentEpoch === Uint64(0) || epochId > currentEpoch, 'Epoch not increasing')

    // Create epoch root box (fails if exists = immutable roots)
    const epochBox = Box<bytes>({ key: op.itob(epochId) })
    assert(epochBox.create({ size: EPOCH_ROOT_SIZE }), 'Root already published')
    epochBox.replace(0, root)

    this.currentEpochId.value = epochId

    log(Bytes('SET_ROOT').concat(op.itob(epochId)).concat(root))
  }

  /** Emergency pause / unpause. Admin only. */
  public togglePause(state: uint64): void {
    assert(Txn.sender === this.admin.value, 'Admin only')
    assert(state === Uint64(0) || state === Uint64(1), 'Invalid state')
    this.paused.value = state
  }

  /**
   * Deprecate pool (one-way). Admin only.
   * Once deprecated: no claims, no new roots, emergency withdrawal available.
   */
  public toggleDeprecated(state: uint64): void {
    assert(Txn.sender === this.admin.value, 'Admin only')
    assert(state === Uint64(0) || state === Uint64(1), 'Invalid state')
    // One-way: once deprecated, cannot reactivate
    if (this.deprecated.value === Uint64(1)) {
      assert(state === Uint64(1), 'Cannot reactivate')
    }
    this.deprecated.value = state
  }

  /**
   * Emergency withdraw all remaining rewards to sponsor.
   * Pool must be deprecated first.
   */
  public emergencyWithdraw(): void {
    assert(Txn.sender === this.admin.value, 'Admin only')
    assert(this.deprecated.value === Uint64(1), 'Must deprecate first')

    const rewardAsset = Asset(this.rewardTokenId.value)
    const [balance, hasBalance] = op.AssetHolding.assetBalance(Global.currentApplicationAddress, rewardAsset)
    assert(hasBalance, 'Token not opted in')

    if (balance > 0) {
      itxn
        .assetTransfer({
          xferAsset: rewardAsset,
          assetReceiver: this.sponsor.value,
          assetAmount: balance,
          fee: 0,
        })
        .submit()

      log(Bytes('EMERGENCY_WITHDRAW').concat(op.itob(balance)))
    }
  }

  /** Update pool end date (informational only). Admin only. */
  public updateEndDate(newEndDate: uint64): void {
    assert(Txn.sender === this.admin.value, 'Admin only')
    this.poolEndDate.value = newEndDate
  }

  /** Update the publisher address for automated epoch publishing. Admin only. */
  public updatePublisher(newPublisher: Account): void {
    assert(Txn.sender === this.admin.value, 'Admin only')
    assert(newPublisher !== Global.zeroAddress, 'Invalid publisher')
    this.publisher.value = newPublisher
  }

  // ========================================================================
  // PRIVATE HELPERS
  // ========================================================================

  /**
   * Compute Merkle leaf hash.
   * leaf = SHA256(address || app_id || pool_id || epoch_id || amount)
   *
   * Binds each claim to a specific user, app, pool, epoch, and amount
   * preventing cross-app, cross-pool, cross-epoch, and amount manipulation.
   */
  private computeLeafHash(userAddress: bytes, epochId: uint64, amount: uint64): bytes {
    return sha256(
      userAddress
        .concat(op.itob(Global.currentApplicationId.id))
        .concat(op.itob(this.poolId.value))
        .concat(op.itob(epochId))
        .concat(op.itob(amount)),
    )
  }

  /**
   * Hash a pair of Merkle nodes in sorted order.
   * Always hash(min, max) for deterministic verification.
   * Matches OpenZeppelin sorted-pair standard.
   */
  private hashPair(a: bytes, b: bytes): bytes {
    if (BigUint(a) < BigUint(b)) {
      return sha256(a.concat(b))
    } else {
      return sha256(b.concat(a))
    }
  }

  /**
   * Verify a Merkle proof against an expected root.
   * Iteratively hashes the leaf with each proof element using sorted pairs.
   */
  private verifyMerkleProof(leaf: bytes, proof: bytes, proofCount: uint64, root: bytes): void {
    let currentHash: bytes = leaf
    for (const i of urange(proofCount)) {
      currentHash = this.hashPair(currentHash, extract(proof, i * Uint64(32), Uint64(32)))
    }
    assert(currentHash === root, 'Invalid Merkle proof')
  }
}
