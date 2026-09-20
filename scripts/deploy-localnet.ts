import {AlgorandClient,Config} from '@algorandfoundation/algokit-utils';
import {AlgoAmount} from '@algorandfoundation/algokit-utils/types/amount';
import {APP_SPEC,FundedRewardsFactory} from '../contracts/funded-rewards/client/FundedRewardsClient.ts';
import {sha256} from '../contracts/funded-rewards/funded-chain.ts';
// Explicit loopback services: this example cannot deploy to TestNet or MainNet.
const config={server:'http://127.0.0.1',token:'a'.repeat(64)};
const algorand=AlgorandClient.fromConfig({algodConfig:{...config,port:4001},indexerConfig:{...config,port:8980},kmdConfig:{...config,port:4002}});
Config.configure({debug:false,populateAppCallResources:true});
const rounds=await algorand.client.algod.getTransactionParams().do();
if(rounds.genesisID==='mainnet-v1.0'||rounds.genesisID==='testnet-v1.0')throw new Error('LocalNet only');
const owner=await algorand.account.localNetDispenser();
const {assetId}=await algorand.send.assetCreate({sender:owner,total:1000000000000n,decimals:6,assetName:'Example rewards',unitName:'TOKEN'});
const factory=algorand.client.getTypedAppFactory(FundedRewardsFactory,{defaultSender:owner});
const {appClient:client}=await factory.send.create.create({args:{rewardToken:assetId,poolId:1n,publisher:owner.toString(),buyback:owner.toString(),openingBudget:0n}});
await algorand.send.payment({sender:owner,receiver:client.appAddress,amount:AlgoAmount.Algos(2)});
await client.send.optInAsset({args:{},staticFee:AlgoAmount.MicroAlgos(2000)});
await client.send.activate({args:{}});
const axfer=await algorand.createTransaction.assetTransfer({sender:owner,receiver:client.appAddress,assetId,amount:100000000n});
await client.send.fundBuyback({args:{axfer}});
console.log(JSON.stringify({pool_id:crypto.randomUUID(),app_id:client.appId.toString(),reward_asset_id:assetId.toString(),chain_pool_id:'1',opening_budget:'0',
 admin:owner.toString(),publisher:owner.toString(),buyback:owner.toString(),approval_sha256:sha256(Buffer.from(APP_SPEC.byteCode!.approval!,'base64')),
 genesis_id:rounds.genesisID,genesis_hash:Buffer.from(rounds.genesisHash!).toString('base64')},null,2));
