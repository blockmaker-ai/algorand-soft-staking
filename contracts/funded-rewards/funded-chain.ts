import { createHash } from 'node:crypto'
import { Buffer } from 'node:buffer'
import { AlgorandClient } from '@algorandfoundation/algokit-utils'
import { AlgoAmount } from '@algorandfoundation/algokit-utils/types/amount'
import algosdk from 'algosdk'
import { APP_SPEC, FundedRewardsClient } from './client/FundedRewardsClient.ts'

export const MAINNET = { id: 'mainnet-v1.0', hash: 'wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=' }
export type VaultConfig = {
  app_id: string; reward_asset_id: string; chain_pool_id: string; opening_budget: string
  admin: string; publisher: string; buyback: string; approval_sha256: string
}
export type SealedCredit = { wallet: string; previous: string; cumulative: string }
export type RecordedAttempt = { tx_id: string; unsigned_transaction: string; first_valid: string; last_valid: string }
export const sha256 = (value: Uint8Array) => createHash('sha256').update(value).digest('hex')
export const ensureAlgokitRuntime = () => {
  // AlgoKit 9.2.0's state and box decoders use the Node Buffer global. Supabase
  // Edge does not supply it, even when our own modules import node:buffer.
  const runtime = globalThis as typeof globalThis & { Buffer?: typeof Buffer }
  runtime.Buffer ??= Buffer
}
const equal = (a: Uint8Array | undefined, b: Uint8Array) => a !== undefined && Buffer.from(a).equals(Buffer.from(b))
const base64 = (value: Uint8Array) => Buffer.from(value).toString('base64')
const uint = (value: string) => {
  if (typeof value !== 'string' || !/^(0|[1-9][0-9]{0,19})$/.test(value) || BigInt(value) > 18446744073709551615n) {
    throw new Error('An exact uint64 decimal string is required')
  }
  return BigInt(value)
}
const boxName = (wallet: string) => new Uint8Array([114, ...algosdk.decodeAddress(wallet).publicKey])
const note = (batch: string) => {
  if (!/^[0-9a-f]{64}$/.test(batch)) throw new Error('A sealed database batch identity is required')
  return new TextEncoder().encode(`soft-staking-funded:${batch}`)
}

/** Read/prepare/verify only. No signing key, broadcast or database write is hidden
 * in this adapter. Its caller must persist an attempt before signing/submitting.
 */
export class FundedChain {
  readonly client: FundedRewardsClient
  readonly algorand: AlgorandClient
  readonly config: VaultConfig
  readonly network: typeof MAINNET
  constructor(algorand: AlgorandClient, config: VaultConfig, network: {id: string; hash: string}) {
    ensureAlgokitRuntime()
    // Unsigned preparation must also work for wallets whose signer is held by
    // Pera, not this server. Registered signers still take precedence in tests.
    algorand.account.setDefaultSigner(algosdk.makeEmptyTransactionSigner())
    for (const field of ['admin', 'publisher', 'buyback'] as const) {
      if (!algosdk.isValidAddress(config[field])) throw new Error('Invalid vault operator address')
    }
    for (const field of ['app_id', 'reward_asset_id', 'chain_pool_id', 'opening_budget'] as const) uint(config[field])
    if (uint(config.app_id) === 0n || uint(config.reward_asset_id) === 0n) throw new Error('Missing vault identity')
    this.algorand = algorand
    this.config = { ...config }
    this.network = { ...network }
    this.client = algorand.client.getTypedAppClientById(FundedRewardsClient, {
      appId: uint(config.app_id), defaultSender: config.publisher, defaultSigner: algosdk.makeEmptyTransactionSigner(),
    })
  }

  private checkNetwork(id: string | undefined, hash: Uint8Array | undefined) {
    if (id !== this.network.id || !equal(hash, Buffer.from(this.network.hash, 'base64'))) throw new Error('Unexpected chain network')
  }

  async verifyIdentity() {
    const algod = this.algorand.client.algod
    const [params, app] = await Promise.all([algod.getTransactionParams().do(), algod.getApplicationByID(uint(this.config.app_id)).do()])
    this.checkNetwork(params.genesisID, params.genesisHash)
    const expected = Buffer.from(APP_SPEC.byteCode!.approval!, 'base64')
    const clear = Buffer.from(APP_SPEC.byteCode!.clear!, 'base64')
    if (app.id !== uint(this.config.app_id) || app.params.creator.toString() !== this.config.admin
      || !equal(app.params.approvalProgram, expected) || !equal(app.params.clearStateProgram, clear)
      || sha256(expected) !== this.config.approval_sha256) throw new Error('Vault bytecode, creator or release identity differs')
    const globals = await this.client.state.global.getAll()
    if (globals.admin !== this.config.admin || globals.publisher !== this.config.publisher || globals.buyback !== this.config.buyback
      || globals.poolId !== uint(this.config.chain_pool_id) || globals.rewardToken !== uint(this.config.reward_asset_id)
      || globals.openingBudget !== uint(this.config.opening_budget)) throw new Error('Vault configuration differs from the reviewed mapping')
    return params
  }

  async display(wallet?: string): Promise<{snapshot: Record<string, string>; reward: {allocated: string; paid: string} | null}> {
    await this.verifyIdentity()
    if (wallet && !algosdk.isValidAddress(wallet)) throw new Error('Invalid reward wallet')
    const group = this.client.newGroup().getSummary({ args: {}, assetReferences: [uint(this.config.reward_asset_id)] })
    if (wallet) group.getReward({args: {user: wallet}, boxReferences: [{appId: uint(this.config.app_id), name: boxName(wallet)}]})
    const result = await group.simulate({ skipSignatures: true })
    const summary = result.returns[0], round = result.simulateResponse.lastRound
    if (!summary || round <= 0n) throw new Error('Missing consistent vault snapshot')
    const block = (await this.algorand.client.algod.block(round).headerOnly(true).do()).block.header
    this.checkNetwork(block.genesisID, block.genesisHash)
    // Date conversion is only for the timestamp; every token amount remains bigint.
    if (block.round !== round || block.timestamp < 0n || block.timestamp > 8640000000000n) throw new Error('Invalid snapshot block')
    if (summary.paid > summary.allocated || summary.allocated > summary.deposited
      || summary.balance < summary.allocated - summary.paid) throw new Error('Unbacked vault reserve')
    const reward = wallet ? result.returns[1] as {allocated: bigint; paid: bigint} | undefined : undefined
    if (wallet && (!reward || reward.paid > reward.allocated || reward.allocated > summary.allocated || reward.paid > summary.paid)) {
      throw new Error('Invalid reward account snapshot')
    }
    return {snapshot: { app_id: this.config.app_id, round: round.toString(),
      at: new Date(Number(block.timestamp) * 1000).toISOString(), genesis_id: this.network.id, genesis_hash: this.network.hash,
      ...Object.fromEntries(Object.entries(summary).map(([key, value]) => [key, value.toString()])) },
      reward: reward ? {allocated: reward.allocated.toString(), paid: reward.paid.toString()} : null}
  }

  async snapshot(): Promise<Record<string, string>> {
    return (await this.display()).snapshot
  }

  async prepareClaim(wallet: string, first: bigint, last: bigint) {
    if (!algosdk.isValidAddress(wallet) || first <= 0n || last <= first || last - first > 300n) throw new Error('Invalid claim request')
    const result = await this.client.createTransaction.claimRewards({sender: wallet, args: {},
      staticFee: AlgoAmount.MicroAlgos(2000), firstValidRound: first, lastValidRound: last,
      assetReferences: [uint(this.config.reward_asset_id)], boxReferences: [{appId: uint(this.config.app_id), name: boxName(wallet)}],
      note: new TextEncoder().encode('Soft staking: claim funded rewards'),
    })
    if (result.transactions.length !== 1) throw new Error('Unexpected claim composition')
    result.transactions[0].group = undefined
    return result.transactions[0]
  }

  async prepareAllocation(batch: string, credit: SealedCredit): Promise<RecordedAttempt> {
    const params = await this.verifyIdentity()
    if (!algosdk.isValidAddress(credit.wallet) || uint(credit.cumulative) <= uint(credit.previous)) throw new Error('Invalid sealed credit')
    const { transactions } = await this.client.createTransaction.allocateRewards({
      args: { user: credit.wallet, previous: uint(credit.previous), cumulative: uint(credit.cumulative) },
      staticFee: AlgoAmount.MicroAlgos(1000), firstValidRound: params.firstValid, lastValidRound: params.firstValid + 120n,
      assetReferences: [uint(this.config.reward_asset_id)], boxReferences: [{ appId: uint(this.config.app_id), name: boxName(credit.wallet) }],
      note: note(batch),
    })
    if (transactions.length !== 1) throw new Error('Allocation must contain exactly one application call')
    const txn = transactions[0]
    // Each persisted allocation is independent; a partial batch can resume any
    // individual credit without reconstructing or re-signing a different group.
    txn.group = undefined
    const attempt = { tx_id: txn.txID(), unsigned_transaction: base64(algosdk.encodeUnsignedTransaction(txn)),
      first_valid: txn.firstValid.toString(), last_valid: txn.lastValid.toString() }
    this.validateAttempt(batch, credit, attempt)
    return attempt
  }

  validateAttempt(batch: string, credit: SealedCredit, attempt: RecordedAttempt) {
    if (!/^[A-Za-z0-9+/]+={0,2}$/.test(attempt.unsigned_transaction) || attempt.unsigned_transaction.length > 32768) throw new Error('Invalid stored transaction')
    const raw = Buffer.from(attempt.unsigned_transaction, 'base64')
    const txn = algosdk.decodeUnsignedTransaction(raw), call = txn.applicationCall
    const method = algosdk.ABIMethod.fromSignature('allocateRewards(address,uint64,uint64)uint64')
    const expectedArgs = [method.getSelector(), algosdk.decodeAddress(credit.wallet).publicKey,
      algosdk.encodeUint64(uint(credit.previous)), algosdk.encodeUint64(uint(credit.cumulative))]
    this.checkNetwork(txn.genesisID, txn.genesisHash)
    if (uint(credit.cumulative) <= uint(credit.previous) || !equal(raw, algosdk.encodeUnsignedTransaction(txn))
      || txn.txID() !== attempt.tx_id || txn.sender.toString() !== this.config.publisher || txn.type !== algosdk.TransactionType.appl
      || txn.group || txn.rekeyTo || txn.lease?.length || txn.fee !== 1000n || !equal(txn.note, note(batch))
      || txn.firstValid !== uint(attempt.first_valid) || txn.lastValid !== uint(attempt.last_valid)
      || txn.firstValid <= 0n || txn.lastValid < txn.firstValid || txn.lastValid - txn.firstValid > 120n
      || !call || call.appIndex !== uint(this.config.app_id) || call.onComplete !== algosdk.OnApplicationComplete.NoOpOC
      || call.appArgs.length !== 4 || call.appArgs.some((arg, index) => !equal(arg, expectedArgs[index]))
      || call.approvalProgram.length || call.clearProgram.length || call.numGlobalInts || call.numGlobalByteSlices
      || call.numLocalInts || call.numLocalByteSlices || call.extraPages || call.rejectVersion || call.access.length
      || call.accounts.length || call.foreignApps.length || call.foreignAssets.length !== 1 || call.foreignAssets[0] !== uint(this.config.reward_asset_id)
      || call.boxes.length !== 1 || call.boxes[0].appIndex !== 0n || !equal(call.boxes[0].name, boxName(credit.wallet))) {
      throw new Error('Stored transaction differs from the sealed allocation')
    }
    return txn
  }

  async confirmedCredit(batch: string, credit: SealedCredit, attempt: RecordedAttempt, confirmation: algosdk.modelsv2.PendingTransactionResponse) {
    const txn = this.validateAttempt(batch, credit, attempt)
    const round = confirmation.confirmedRound
    if (!round || round < txn.firstValid || round > txn.lastValid || confirmation.poolError
      || !equal(algosdk.encodeUnsignedTransaction(confirmation.txn.txn), algosdk.encodeUnsignedTransaction(txn))) {
      throw new Error('The recorded transaction has not been confirmed exactly')
    }
    await this.verifyIdentity()
    const result = await this.client.newGroup().getReward({ args: { user: credit.wallet },
      boxReferences: [{ appId: uint(this.config.app_id), name: boxName(credit.wallet) }] }).simulate({ skipSignatures: true })
    const reward = result.returns[0], boxRound = result.simulateResponse.lastRound
    if (!reward || boxRound < round || reward.allocated !== uint(credit.cumulative) || reward.paid > reward.allocated) {
      throw new Error('The confirmed reward box differs from the sealed credit')
    }
    return { tx_id: attempt.tx_id, app_id: this.config.app_id, wallet: credit.wallet, confirmed_round: round.toString(),
      box_round: boxRound.toString(), allocated: reward.allocated.toString(), paid: reward.paid.toString(),
      transaction_digest: sha256(algosdk.encodeUnsignedTransaction(txn)) }
  }
}
