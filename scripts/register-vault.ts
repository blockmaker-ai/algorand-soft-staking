import {readFileSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {FundedChain,type VaultConfig} from '../contracts/funded-rewards/funded-chain.ts';
import {operatorChain,operatorDatabase} from './operator-config.ts';
const file=process.argv[2];
if(!file)throw new Error('Usage: node --env-file=.env --import tsx scripts/register-vault.ts deployment.json [--apply]');
const config=JSON.parse(readFileSync(file,'utf8')) as VaultConfig & {pool_id:string;genesis_id:string;genesis_hash:string};
if(!/^[0-9a-f-]{36}$/i.test(config.pool_id)||config.opening_budget!=='0')throw new Error('This command registers a fresh vault with zero opening credits');
if(config.genesis_id!==process.env.ALGORAND_GENESIS_ID||config.genesis_hash!==process.env.ALGORAND_GENESIS_HASH)throw new Error('Configured network differs from the deployment');
const algorand=operatorChain(),db=operatorDatabase();
const chain=new FundedChain(algorand,config,{id:config.genesis_id,hash:config.genesis_hash});
const snapshot=await chain.snapshot();
if(snapshot.active!=='1'||snapshot.paused!=='0'||snapshot.allocated!=='0')throw new Error('Register a fresh active vault before allocating rewards');
const rows=await db(`/rest/v1/pools?id=eq.${encodeURIComponent(config.pool_id)}&select=*`);
if(rows.length!==1)throw new Error('Pool metadata is missing');
const p=rows[0];
if(String(p.contract_app_id)!==config.app_id||String(p.reward_token_id)!==config.reward_asset_id||p.creator_address!==config.admin)throw new Error('Pool metadata differs from the deployed vault');
const nft=p.pool_type==='nft staking';
let assets:string[];
if(nft){
 const [collection]=await db(`/rest/v1/nft_collections?id=eq.${encodeURIComponent(p.nft_collection_id)}&select=is_indexed,indexed_asset_ids`);
 if(!collection?.is_indexed||!Array.isArray(collection.indexed_asset_ids)||collection.indexed_asset_ids.some((id:unknown)=>!Number.isSafeInteger(id)))throw new Error('A precisely indexed NFT collection is required');
 assets=collection.indexed_asset_ids.map(String);
}else assets=[String(p.pool_type==='lp staking'?p.lp_token_id:p.staking_token_id)];
for(const id of assets){
 const asset=await algorand.client.algod.getAssetByID(BigInt(id)).do();
 if(asset.params.decimals!==(nft?0:p.staking_token_decimals)||(nft&&asset.params.total!==1n))throw new Error('Staking asset metadata differs');
}
const reward=await algorand.client.algod.getAssetByID(BigInt(config.reward_asset_id)).do();
if(reward.params.decimals!==p.reward_token_decimals)throw new Error('Reward decimals differ from the chain');
const policy={pool_id:p.id,nft_pool:nft,nft_indexed:nft,asset_ids:assets,decimals:nft?0:p.staking_token_decimals,
 reward_stake_cap:p.reward_stake_cap===null?null:String(p.reward_stake_cap),reward_stake_cap_from:p.reward_stake_cap_from};
const result=spawnSync(process.env.PYTHON || 'python3',['-B','scripts/policy_digest.py'],{input:JSON.stringify(policy),encoding:'utf8'});
if(result.status!==0)throw new Error('Staking policy could not be verified');
const {digest}=JSON.parse(result.stdout),registration={p_pool:p.id,p_config:{...config,asset_policy_digest:digest},p_opening:{},p_snapshot:snapshot};
if(process.argv.includes('--apply')){
 await db('/rest/v1/rpc/register_funded_reward_vault','POST',registration);
 await db(`/rest/v1/pools?id=eq.${encodeURIComponent(p.id)}`,'PATCH',{status:'active',funding_confirmed:true});
 console.log(JSON.stringify({pool_id:p.id,app_id:config.app_id,registered:true}));
}else console.log(JSON.stringify({preview:true,registration},null,2));
