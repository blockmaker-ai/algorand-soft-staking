import type { uint64 } from '@algorandfoundation/algorand-typescript'
import {
  abimethod, Account, Application, assert, assertMatch, Asset, BoxMap, Bytes, clone,
  Contract, Global, GlobalState, gtxn, itxn, log, op, Txn, Uint64,
} from '@algorandfoundation/algorand-typescript'

type RewardAccount = { allocated: uint64; paid: uint64 }
type RewardSummary = {
  balance: uint64; deposited: uint64; allocated: uint64; paid: uint64;
  reserved: uint64; available: uint64; active: uint64; paused: uint64;
}

/**
 * A funded soft-staking reward vault. Stakes remain off-chain/in users' wallets.
 * Every allocation reserves actual, previously unallocated deposits on-chain.
 * There are no Merkle roots, unbacked pending claims, or balance-derived budgets.
 * No method can lower allocations, delete claim state, withdraw rewards, update
 * the program or delete the application. Only users can withdraw their rewards.
 */
export class FundedRewards extends Contract {
  admin = GlobalState<Account>({ key: 'admin' })
  publisher = GlobalState<Account>({ key: 'publisher' })
  buyback = GlobalState<Account>({ key: 'buyback' })
  rewardToken = GlobalState<uint64>({ key: 'reward_token' })
  poolId = GlobalState<uint64>({ key: 'pool_id' })
  deposited = GlobalState<uint64>({ key: 'deposited' })
  allocated = GlobalState<uint64>({ key: 'allocated' })
  paid = GlobalState<uint64>({ key: 'paid' })
  openingBudget = GlobalState<uint64>({ key: 'opening_budget' })
  openingFunded = GlobalState<uint64>({ key: 'opening_funded' })
  active = GlobalState<uint64>({ key: 'active' })
  paused = GlobalState<uint64>({ key: 'paused' })
  rewards = BoxMap<Account, RewardAccount>({ keyPrefix: 'r' })

  @abimethod({ onCreate: 'require' })
  public create(
    rewardToken: uint64, poolId: uint64, publisher: Account,
    buyback: Account, openingBudget: uint64,
  ): void {
    assert(rewardToken > 0 && poolId > 0, 'Invalid pool or asset')
    assert(publisher !== Global.zeroAddress && buyback !== Global.zeroAddress, 'Invalid operator')
    this.admin.value = Txn.sender
    this.publisher.value = publisher
    this.buyback.value = buyback
    this.rewardToken.value = rewardToken
    this.poolId.value = poolId
    this.deposited.value = Uint64(0)
    this.allocated.value = Uint64(0)
    this.paid.value = Uint64(0)
    this.openingBudget.value = openingBudget
    this.openingFunded.value = Uint64(0)
    this.active.value = Uint64(0)
    this.paused.value = Uint64(0)
  }

  public optInAsset(): void {
    assert(Txn.sender === this.admin.value, 'Admin only')
    itxn.assetTransfer({ xferAsset: Asset(this.rewardToken.value),
      assetReceiver: Global.currentApplicationAddress, assetAmount: 0, fee: 0 }).submit()
  }

  /** The transfer must immediately precede this call; it cannot be counted twice. */
  private receive(axfer: gtxn.AssetTransferTxn, sender: Account): void {
    assert(Txn.groupIndex > 0 && axfer.groupIndex + 1 === Txn.groupIndex, 'Deposit must immediately precede call')
    assertMatch(axfer, {
      sender, assetReceiver: Global.currentApplicationAddress,
      xferAsset: Asset(this.rewardToken.value), assetSender: Global.zeroAddress,
      assetCloseTo: Global.zeroAddress, rekeyTo: Global.zeroAddress,
    })
    assert(axfer.assetAmount > 0, 'Empty deposit')
    this.deposited.value = this.deposited.value + axfer.assetAmount
    // A physical balance check also catches unsupported/externally impaired assets.
    assert(this.balance() >= this.deposited.value - this.paid.value, 'Deposit not backed')
    log(Bytes('DEPOSIT').concat(op.itob(axfer.assetAmount)))
  }

  public fundBuyback(axfer: gtxn.AssetTransferTxn): void {
    assert(Txn.sender === this.buyback.value, 'Buyback only')
    this.receive(axfer, this.buyback.value)
  }

  /**
   * First call of the owner's atomic legacy migration group. If another deposit
   * arrived after the reset snapshot, the whole retirement/withdrawal/deposit
   * group must fail instead of leaving unassigned rewards in the sponsor wallet.
   * This method itself changes no state and cannot retire or withdraw anything.
   */
  @abimethod({ readonly: true })
  public assertMigrationSource(sourceApp: uint64): void {
    assert(Txn.sender === this.admin.value, 'Admin only')
    assert(this.active.value === 0 && this.openingFunded.value === 0, 'Opening already closed')
    assert(sourceApp > 0 && sourceApp !== Global.currentApplicationId.id, 'Invalid migration source')
    const source = Application(sourceApp)
    const [admin, hasAdmin] = op.AppGlobal.getExBytes(source, Bytes('admin'))
    const [sponsor, hasSponsor] = op.AppGlobal.getExBytes(source, Bytes('sponsor'))
    const [token, hasToken] = op.AppGlobal.getExUint64(source, Bytes('reward_token'))
    const [poolId, hasPool] = op.AppGlobal.getExUint64(source, Bytes('pool_id'))
    const [paused, hasPause] = op.AppGlobal.getExUint64(source, Bytes('paused'))
    const [deprecated, hasDeprecated] = op.AppGlobal.getExUint64(source, Bytes('deprecated'))
    assert(hasAdmin && hasSponsor && hasToken && hasPool && hasPause && hasDeprecated, 'Incomplete legacy state')
    assert(admin === this.admin.value.bytes && sponsor === this.admin.value.bytes, 'Legacy owner or sponsor differs')
    assert(token === this.rewardToken.value && poolId === this.poolId.value, 'Legacy pool or token differs')
    assert(paused === 1 && deprecated === 0, 'Legacy pool must be paused and not retired')
    const [balance, exists] = op.AssetHolding.assetBalance(source.address, Asset(this.rewardToken.value))
    assert(exists && balance === this.openingBudget.value, 'Legacy balance changed; recalculate the opening')
  }

  /** One exact, reviewed migration deposit from the owner, before activation. */
  public fundOpening(axfer: gtxn.AssetTransferTxn): void {
    assert(Txn.sender === this.admin.value, 'Admin only')
    assert(this.active.value === 0 && this.openingFunded.value === 0, 'Opening already closed')
    assert(axfer.assetAmount === this.openingBudget.value, 'Opening amount differs')
    this.receive(axfer, this.admin.value)
    this.openingFunded.value = Uint64(1)
  }

  /**
   * Compare-and-set cumulative credits make a retry harmless and reject a stale
   * competing writer. The physical and recognised-deposit caps are both enforced.
   * Box MBR is supplied by the operator, so users do not need to restake or opt in.
   */
  public allocateRewards(user: Account, previous: uint64, cumulative: uint64): uint64 {
    assert(Txn.sender === this.publisher.value || Txn.sender === this.admin.value, 'Publisher only')
    assert(this.paused.value === 0, 'Paused')
    assert(user !== Global.zeroAddress && cumulative > 0, 'Invalid allocation')
    let state: RewardAccount = { allocated: Uint64(0), paid: Uint64(0) }
    if (this.rewards(user).exists) state = clone(this.rewards(user).value)
    if (state.allocated === cumulative) return Uint64(0)
    assert(state.allocated === previous, 'Stale allocation')
    assert(cumulative > state.allocated, 'Allocation cannot decrease')
    const delta: uint64 = cumulative - state.allocated
    const newAllocated: uint64 = this.allocated.value + delta
    assert(newAllocated <= this.deposited.value, 'Unfunded allocation')
    assert(this.balance() >= newAllocated - this.paid.value, 'Insufficient reserve')
    if (this.active.value === 0) {
      assert(this.openingFunded.value === 1, 'Opening not funded')
      assert(newAllocated <= this.openingBudget.value, 'Opening allocation exceeded')
    }
    state.allocated = cumulative
    this.rewards(user).value = clone(state)
    this.allocated.value = newAllocated
    log(Bytes('ALLOCATE').concat(user.bytes).concat(op.itob(cumulative)))
    return delta
  }

  /** Claims open only after the entire approved opening budget has been assigned. */
  public activate(): void {
    assert(Txn.sender === this.admin.value, 'Admin only')
    assert(this.active.value === 0, 'Already active')
    assert(this.openingBudget.value === 0 || this.openingFunded.value === 1, 'Opening not funded')
    assert(this.allocated.value === this.openingBudget.value, 'Opening allocation incomplete')
    assert(this.balance() >= this.allocated.value - this.paid.value, 'Insufficient reserve')
    this.active.value = Uint64(1)
  }

  public claimRewards(): uint64 {
    assert(this.active.value === 1 && this.paused.value === 0, 'Claims unavailable')
    assert(this.rewards(Txn.sender).exists, 'No rewards')
    const state = clone(this.rewards(Txn.sender).value)
    const amount: uint64 = state.allocated - state.paid
    assert(amount > 0, 'Nothing to claim')
    assert(this.balance() >= this.allocated.value - this.paid.value, 'Insufficient reserve')
    state.paid = state.allocated
    this.rewards(Txn.sender).value = clone(state)
    this.paid.value = this.paid.value + amount
    itxn.assetTransfer({ xferAsset: Asset(this.rewardToken.value),
      assetReceiver: Txn.sender, assetAmount: amount, fee: 0 }).submit()
    log(Bytes('CLAIM').concat(Txn.sender.bytes).concat(op.itob(amount)))
    return amount
  }

  public togglePause(state: uint64): void {
    assert(Txn.sender === this.admin.value, 'Admin only')
    assert(state === 0 || state === 1, 'Invalid pause state')
    this.paused.value = state
  }

  public updatePublisher(publisher: Account): void {
    assert(Txn.sender === this.admin.value, 'Admin only')
    assert(publisher !== Global.zeroAddress, 'Invalid publisher')
    this.publisher.value = publisher
  }

  private balance(): uint64 {
    const [balance, exists] = op.AssetHolding.assetBalance(Global.currentApplicationAddress, Asset(this.rewardToken.value))
    assert(exists, 'Reward asset not opted in')
    return balance
  }

  @abimethod({ readonly: true })
  public getReward(user: Account): RewardAccount {
    if (this.rewards(user).exists) return clone(this.rewards(user).value)
    return { allocated: Uint64(0), paid: Uint64(0) }
  }

  @abimethod({ readonly: true })
  public getSummary(): RewardSummary {
    const balance = this.balance()
    const reserved: uint64 = this.allocated.value - this.paid.value
    assert(balance >= reserved, 'Reserve impaired')
    const recognised: uint64 = this.deposited.value - this.allocated.value
    const physical: uint64 = balance - reserved
    return { balance, deposited: this.deposited.value, allocated: this.allocated.value,
      paid: this.paid.value, reserved, available: recognised < physical ? recognised : physical,
      active: this.active.value, paused: this.paused.value }
  }
}
