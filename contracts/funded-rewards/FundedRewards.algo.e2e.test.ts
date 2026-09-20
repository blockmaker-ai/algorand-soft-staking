import { Config } from '@algorandfoundation/algokit-utils'
import { algorandFixture } from '@algorandfoundation/algokit-utils/testing'
import { AlgoAmount } from '@algorandfoundation/algokit-utils/types/amount'
import algosdk from 'algosdk'
import { beforeEach, describe, expect, test } from 'vitest'
import { APP_SPEC, FundedRewardsFactory } from './client/FundedRewardsClient'
import { StakingPoolFactory } from '../legacy/client/StakingPoolClient'
import { FundedChain, sha256 } from './funded-chain'

// Explicit loopback-only clients: these integration tests cannot use mainnet.
const local = { server: 'http://127.0.0.1', token: 'a'.repeat(64) }
const fixture = algorandFixture({
  algodConfig: { ...local, port: 4001 },
  indexerConfig: { ...local, port: 8980 },
  kmdConfig: { ...local, port: 4002 },
})
Config.configure({ debug: false, populateAppCallResources: true })
beforeEach(fixture.newScope, 30_000)

async function setup(openingBudget = 0n, funding = 100n) {
  const { algorand, testAccount: owner, generateAccount } = fixture.context
  const params = await algorand.client.algod.getTransactionParams().do()
  if (params.genesisID === 'mainnet-v1.0' || params.genesisID === 'testnet-v1.0') throw new Error('LocalNet only')
  const publisher = await generateAccount({ initialFunds: AlgoAmount.Algos(2), suppressLog: true })
  const buyback = await generateAccount({ initialFunds: AlgoAmount.Algos(2), suppressLog: true })
  const alice = await generateAccount({ initialFunds: AlgoAmount.Algos(2), suppressLog: true })
  const bob = await generateAccount({ initialFunds: AlgoAmount.Algos(2), suppressLog: true })
  const { assetId } = await algorand.send.assetCreate({ sender: owner, total: 1_000_000n, decimals: 6 })
  for (const user of [buyback, alice, bob]) await algorand.send.assetOptIn({ sender: user, assetId })
  await algorand.send.assetTransfer({ sender: owner, receiver: buyback, assetId, amount: 100_000n })
  const factory = algorand.client.getTypedAppFactory(FundedRewardsFactory, { defaultSender: owner })
  const { appClient: client } = await factory.send.create.create({
    args: { rewardToken: assetId, poolId: 42n, publisher: publisher.toString(), buyback: buyback.toString(), openingBudget },
  })
  await algorand.send.payment({ sender: owner, receiver: client.appAddress, amount: AlgoAmount.Algos(2) })
  await client.send.optInAsset({ args: {}, staticFee: AlgoAmount.MicroAlgos(2000) })
  const allocate = (user: typeof alice, previous: bigint, cumulative: bigint) => client.send.allocateRewards({
    sender: publisher, args: { user: user.toString(), previous, cumulative },
  })
  const deposit = async (amount: bigint) => {
    const axfer = await algorand.createTransaction.assetTransfer({ sender: buyback, receiver: client.appAddress, assetId, amount })
    return client.send.fundBuyback({ sender: buyback, args: { axfer } })
  }
  const claim = (user: typeof alice) => client.send.claimRewards({
    sender: user, args: {}, staticFee: AlgoAmount.MicroAlgos(2000),
  })
  if (openingBudget === 0n) await client.send.activate({ args: {} })
  if (funding) await deposit(funding)
  return { algorand, owner, publisher, buyback, alice, bob, assetId, client, allocate, deposit, claim }
}

async function setupMigration(sponsorOverride = false, poolId = 42n) {
  const context = await setup(100n, 0n)
  const { algorand, owner, publisher, bob, assetId, client } = context
  const factory = algorand.client.getTypedAppFactory(StakingPoolFactory, { defaultSender: owner })
  const { appClient: legacy } = await factory.send.create.create({ args: {
    sponsor: (sponsorOverride ? bob : owner).toString(), rewardTokenId: assetId, poolId,
    fundingModel: 1n, startDate: 0n, endDate: 0n, publisher: publisher.toString(),
  } })
  await algorand.send.payment({ sender: owner, receiver: legacy.appAddress, amount: AlgoAmount.Algos(2) })
  await legacy.send.optInAsset({ args: {}, staticFee: AlgoAmount.MicroAlgos(2000) })
  await algorand.send.assetTransfer({ sender: owner, receiver: legacy.appAddress, assetId, amount: 100n })
  await legacy.send.togglePause({ args: { state: 1n } })
  const move = async (amount = 100n) => {
    const axfer = await algorand.createTransaction.assetTransfer({ sender: owner, receiver: client.appAddress, assetId, amount })
    return algorand.newGroup()
      .addAppCallMethodCall(await client.params.assertMigrationSource({ args: { sourceApp: legacy.appId } }))
      .addAppCallMethodCall(await legacy.params.toggleDeprecated({ args: { state: 1n } }))
      .addAppCallMethodCall(await legacy.params.emergencyWithdraw({ args: {}, staticFee: AlgoAmount.MicroAlgos(2000) }))
      .addAppCallMethodCall(await client.params.fundOpening({ args: { axfer } }))
      .send({ populateAppCallResources: true })
  }
  const balance = async () => {
    const result = await algorand.client.algod.accountAssetInformation(legacy.appAddress, assetId).do()
    if (!result.assetHolding) throw new Error('Legacy asset holding is missing')
    return result.assetHolding.amount
  }
  return { ...context, legacy, move, balance }
}

async function chainAdapter(context: Awaited<ReturnType<typeof setup>>) {
  const { algorand, owner, publisher, buyback, client, assetId } = context
  const params = await algorand.client.algod.getTransactionParams().do()
  if (!params.genesisID || !params.genesisHash) throw new Error('Missing LocalNet identity')
  return new FundedChain(algorand, { app_id: client.appId.toString(), reward_asset_id: assetId.toString(),
    chain_pool_id: '42', opening_budget: '0', admin: owner.toString(), publisher: publisher.toString(), buyback: buyback.toString(),
    approval_sha256: sha256(Buffer.from(APP_SPEC.byteCode!.approval!, 'base64')) },
    { id: params.genesisID, hash: Buffer.from(params.genesisHash).toString('base64') })
}

describe('FundedRewards on a real isolated AVM', () => {
  test('publisher prepares an exact typed-client transaction and verifies its real receipt and reward box', async () => {
    const context = await setup(), chain = await chainAdapter(context)
    const snapshot = await chain.snapshot()
    expect(snapshot.balance).toBe('100')
    expect(snapshot.allocated).toBe('0')
    expect(BigInt(snapshot.round)).toBeGreaterThan(0n)
    const batch = 'a'.repeat(64), credit = { wallet: context.alice.toString(), previous: '0', cumulative: '70' }
    const attempt = await chain.prepareAllocation(batch, credit)
    const txn = chain.validateAttempt(batch, credit, attempt)
    // In production the service-only attempt RPC must commit before this step.
    await context.algorand.client.algod.sendRawTransaction(txn.signTxn(context.publisher.sk)).do()
    const confirmation = await algosdk.waitForConfirmation(context.algorand.client.algod, attempt.tx_id, 4)
    const receipt = await chain.confirmedCredit(batch, credit, attempt, confirmation)
    expect(receipt.allocated).toBe('70')
    expect(receipt.paid).toBe('0')
    expect(receipt.tx_id).toBe(attempt.tx_id)
    await context.claim(context.alice)
    const afterClaim = await chain.confirmedCredit(batch, credit, attempt, confirmation)
    expect(afterClaim.allocated).toBe('70')
    expect(afterClaim.paid).toBe('70')
    expect((await chain.snapshot()).balance).toBe('30')
  }, 60_000)

  test('publisher rejects altered stored transactions and mismatched release, operators or network', async () => {
    const context = await setup(), chain = await chainAdapter(context)
    const batch = 'b'.repeat(64), credit = { wallet: context.alice.toString(), previous: '0', cumulative: '25' }
    const attempt = await chain.prepareAllocation(batch, credit)
    for (const change of [
      (txn: algosdk.Transaction) => { txn.fee = 2000n },
      (txn: algosdk.Transaction) => { txn.group = new Uint8Array(32).fill(1) },
      (txn: algosdk.Transaction) => { txn.note[0] ^= 1 },
      (txn: algosdk.Transaction) => { txn.applicationCall!.appArgs[3][7] ^= 1 },
    ]) {
      const txn = algosdk.decodeUnsignedTransaction(Buffer.from(attempt.unsigned_transaction, 'base64'))
      change(txn)
      const changed = { ...attempt, tx_id: txn.txID(), unsigned_transaction: Buffer.from(algosdk.encodeUnsignedTransaction(txn)).toString('base64') }
      expect(() => chain.validateAttempt(batch, credit, changed)).toThrow()
    }
    expect(() => chain.validateAttempt('c'.repeat(64), credit, attempt)).toThrow()
    expect(() => chain.validateAttempt(batch, { ...credit, wallet: context.bob.toString() }, attempt)).toThrow()
    await expect(new FundedChain(context.algorand, chain.config, {...chain.network,id:'different-network'}).verifyIdentity()).rejects.toThrow('network')
    await expect(new FundedChain(context.algorand, { ...chain.config, approval_sha256: '0'.repeat(64) }, chain.network).verifyIdentity()).rejects.toThrow('release')
    await expect(new FundedChain(context.algorand, { ...chain.config, publisher: context.bob.toString() }, chain.network).verifyIdentity()).rejects.toThrow('configuration')
  }, 60_000)

  test('moves the reviewed legacy balance atomically while replacement claims remain closed', async () => {
    const { client, legacy, move, balance, alice } = await setupMigration()
    const result = await move()
    expect(result.txIds.length).toBe(5)
    expect(await balance()).toBe(0n)
    const old = await legacy.state.global.getAll()
    expect(old.deprecated).toBe(1n)
    expect(old.paused).toBe(1n)
    const next = await client.getSummary()
    expect(next.balance).toBe(100n)
    expect(next.deposited).toBe(100n)
    expect(next.allocated).toBe(0n)
    expect(next.active).toBe(0n)
    await expect(client.send.claimRewards({ sender: alice, args: {}, staticFee: AlgoAmount.MicroAlgos(2000) })).rejects.toThrow()
    await expect(move()).rejects.toThrow()
  }, 60_000)

  test('a changed source balance or failed final deposit cannot partially retire or withdraw the legacy pool', async () => {
    const { algorand, owner, assetId, client, legacy, move, balance } = await setupMigration()
    // Failure at the final call must roll back the earlier retirement/withdrawal.
    await expect(move(99n)).rejects.toThrow()
    expect(await balance()).toBe(100n)
    expect((await legacy.state.global.getAll()).deprecated).toBe(0n)
    expect((await client.getSummary()).deposited).toBe(0n)
    // A buyback/donation landing after the reviewed snapshot fails the first call.
    await algorand.send.assetTransfer({ sender: owner, receiver: legacy.appAddress, assetId, amount: 1n })
    await expect(move()).rejects.toThrow()
    expect(await balance()).toBe(101n)
    expect((await legacy.state.global.getAll()).deprecated).toBe(0n)
    expect((await client.getSummary()).deposited).toBe(0n)
  }, 60_000)

  test('migration rejects a different legacy sponsor', async () => {
    const wrongSponsor = await setupMigration(true)
    await expect(wrongSponsor.move()).rejects.toThrow()
    expect(await wrongSponsor.balance()).toBe(100n)
  }, 60_000)

  test('migration rejects a different legacy pool identity', async () => {
    const wrongPool = await setupMigration(false, 43n)
    await expect(wrongPool.move()).rejects.toThrow()
    expect(await wrongPool.balance()).toBe(100n)
  }, 60_000)

  test('migration requires the owner and legacy pause', async () => {
    const { client, legacy, alice, move, balance } = await setupMigration()
    await expect(client.send.assertMigrationSource({ sender: alice, args: { sourceApp: legacy.appId } })).rejects.toThrow()
    await legacy.send.togglePause({ args: { state: 0n } })
    await expect(move()).rejects.toThrow()
    expect(await balance()).toBe(100n)
    expect((await legacy.state.global.getAll()).deprecated).toBe(0n)
  }, 60_000)

  test('reserves credits once, pays once, and never reuses unpaid rewards as new budget', async () => {
    const { client, alice, bob, allocate, claim, deposit } = await setup()
    await allocate(alice, 0n, 60n)
    await allocate(bob, 0n, 40n)
    expect((await client.getSummary()).reserved).toBe(100n)
    expect((await client.getSummary()).available).toBe(0n)
    await expect(allocate(bob, 40n, 41n)).rejects.toThrow()
    await claim(alice)
    const paid = await client.getSummary()
    expect(paid.balance).toBe(40n)
    expect(paid.reserved).toBe(40n)
    expect(paid.available).toBe(0n)
    await expect(claim(alice)).rejects.toThrow()
    await expect(allocate(alice, 60n, 61n)).rejects.toThrow()
    await deposit(7n)
    await allocate(alice, 60n, 67n)
    await claim(alice)
    await claim(bob)
    const finished = await client.getSummary()
    expect(finished.balance).toBe(0n)
    expect(finished.deposited).toBe(107n)
    expect(finished.allocated).toBe(107n)
    expect(finished.paid).toBe(107n)
    expect(finished.reserved).toBe(0n)
  }, 60_000)

  test('retries are idempotent and stale writers cannot overwrite an allocation', async () => {
    const { client, alice, allocate } = await setup()
    await allocate(alice, 0n, 30n)
    const repeat = await allocate(alice, 0n, 30n)
    expect(repeat.return).toBe(0n)
    await expect(allocate(alice, 0n, 40n)).rejects.toThrow()
    await expect(allocate(alice, 30n, 20n)).rejects.toThrow()
    expect((await client.getSummary()).allocated).toBe(30n)
  }, 60_000)

  test('claim display reads the summary and wallet box together, including a wallet with no box', async () => {
    const context = await setup()
    const chain = await chainAdapter(context)
    const empty = await chain.display(context.bob.toString())
    expect(empty.reward).toEqual({ allocated: '0', paid: '0' })
    await context.allocate(context.alice, 0n, 31n)
    const ready = await chain.display(context.alice.toString())
    expect(ready.reward).toEqual({ allocated: '31', paid: '0' })
    expect(ready.snapshot.reserved).toBe('31')
    await context.claim(context.alice)
    const paid = await chain.display(context.alice.toString())
    expect(paid.reward).toEqual({ allocated: '31', paid: '31' })
    expect(paid.snapshot.balance).toBe('69')
    expect(paid.snapshot.reserved).toBe('0')
  }, 60_000)

  test('an over-budget atomic batch leaves every wallet unchanged', async () => {
    const { client, publisher, alice, bob } = await setup()
    await expect(client.newGroup()
      .allocateRewards({ sender: publisher, args: { user: alice.toString(), previous: 0n, cumulative: 80n } })
      .allocateRewards({ sender: publisher, args: { user: bob.toString(), previous: 0n, cumulative: 30n } })
      .send()).rejects.toThrow()
    expect((await client.getSummary()).allocated).toBe(0n)
    expect((await client.getReward({ args: { user: alice.toString() } })).allocated).toBe(0n)
  }, 60_000)

  test('competing allocations cannot both reserve the same tokens', async () => {
    const { client, alice, bob, allocate } = await setup()
    const results = await Promise.allSettled([allocate(alice, 0n, 80n), allocate(bob, 0n, 80n)])
    expect(results.filter(result => result.status === 'fulfilled')).toHaveLength(1)
    expect((await client.getSummary()).reserved).toBe(80n)
    expect((await client.getSummary()).available).toBe(20n)
  }, 60_000)

  test('unrecognised transfers do not become reward budget', async () => {
    const { algorand, owner, client, assetId, alice, allocate } = await setup(0n, 0n)
    await algorand.send.assetTransfer({ sender: owner, receiver: client.appAddress, assetId, amount: 100n })
    expect((await client.getSummary()).balance).toBe(100n)
    expect((await client.getSummary()).available).toBe(0n)
    await expect(allocate(alice, 0n, 1n)).rejects.toThrow()
  }, 60_000)

  test('only the buyback wallet can register regular deposits', async () => {
    const { algorand, owner, client, assetId } = await setup(0n, 0n)
    const axfer = await algorand.createTransaction.assetTransfer({ sender: owner, receiver: client.appAddress, assetId, amount: 20n })
    await expect(client.send.fundBuyback({ args: { axfer } })).rejects.toThrow()
    expect((await client.getSummary()).balance).toBe(0n)
    expect((await client.getSummary()).deposited).toBe(0n)
  }, 60_000)

  test('one transfer cannot be registered twice within a group', async () => {
    const { algorand, client, buyback, assetId } = await setup(0n, 0n)
    const axfer = await algorand.createTransaction.assetTransfer({ sender: buyback, receiver: client.appAddress, assetId, amount: 20n })
    const suggestedParams = await algorand.client.algod.getTransactionParams().do()
    const method = algosdk.ABIMethod.fromSignature('fundBuyback(axfer)void')
    const call = () => algosdk.makeApplicationNoOpTxnFromObject({
      sender: buyback.addr, appIndex: client.appId, appArgs: [method.getSelector()],
      foreignAssets: [assetId], suggestedParams,
    })
    await expect(algorand.newGroup().addTransaction(axfer, buyback.signer)
      .addTransaction(call(), buyback.signer).addTransaction(call(), buyback.signer).send()).rejects.toThrow()
    expect((await client.getSummary()).balance).toBe(0n)
    expect((await client.getSummary()).deposited).toBe(0n)
  }, 60_000)

  test('opening funds are exact and one-time, with claims closed until completely assigned', async () => {
    const { algorand, owner, client, assetId, alice, bob, allocate, claim } = await setup(60n, 0n)
    const opening = async (amount: bigint) => client.send.fundOpening({ args: {
      axfer: await algorand.createTransaction.assetTransfer({ sender: owner, receiver: client.appAddress, assetId, amount }),
    } })
    await expect(opening(59n)).rejects.toThrow()
    await opening(60n)
    await expect(opening(60n)).rejects.toThrow()
    await allocate(alice, 0n, 20n)
    await expect(client.send.activate({ args: {} })).rejects.toThrow()
    await expect(claim(alice)).rejects.toThrow()
    await expect(allocate(bob, 0n, 41n)).rejects.toThrow()
    await allocate(bob, 0n, 40n)
    await client.send.activate({ args: {} })
    await claim(alice)
    await claim(bob)
    expect((await client.getSummary()).balance).toBe(0n)
  }, 60_000)

  test('unauthorised allocation, pause and operator replacement are rejected', async () => {
    const { client, alice } = await setup()
    await expect(client.send.allocateRewards({ sender: alice, args: { user: alice.toString(), previous: 0n, cumulative: 100n } })).rejects.toThrow()
    await expect(client.send.togglePause({ sender: alice, args: { state: 1n } })).rejects.toThrow()
    await expect(client.send.updatePublisher({ sender: alice, args: { publisher: alice.toString() } })).rejects.toThrow()
    expect((await client.getSummary()).allocated).toBe(0n)
  }, 60_000)

  test('a pause preserves reserves and claim state, then resumes the same funded claim', async () => {
    const { client, alice, allocate, claim } = await setup()
    await allocate(alice, 0n, 25n)
    await client.send.togglePause({ args: { state: 1n } })
    await expect(claim(alice)).rejects.toThrow()
    await expect(allocate(alice, 25n, 30n)).rejects.toThrow()
    expect((await client.getSummary()).reserved).toBe(25n)
    await client.send.togglePause({ args: { state: 0n } })
    await claim(alice)
    expect((await client.getSummary()).paid).toBe(25n)
  }, 60_000)

  test('the admin cannot delete the application to bypass reward state', async () => {
    const { algorand, owner, client, alice, allocate } = await setup()
    await allocate(alice, 0n, 25n)
    await expect(algorand.send.appDelete({ sender: owner, appId: client.appId })).rejects.toThrow()
    expect((await client.getSummary()).reserved).toBe(25n)
  }, 60_000)
})
