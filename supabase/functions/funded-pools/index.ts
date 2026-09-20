import {createClient} from 'npm:@supabase/supabase-js@2';
import {AlgorandClient,Config} from '@algorandfoundation/algokit-utils';
import algosdk from 'algosdk';
import {FundedChain,type VaultConfig} from '../../../contracts/funded-rewards/funded-chain.ts';
import {chainClients,networkIdentity} from '../_shared/server-config.ts';
import {actionHeaders} from '../_shared/pool-action-server.ts';
import {claimTransactions,base64} from '../../../sdk/claims.ts';
import {monthlyRewards} from '../../../sdk/display.ts';
Config.configure({debug:false,populateAppCallResources:false});
Deno.serve(async req=>{
 const headers={...actionHeaders(req),'Access-Control-Allow-Methods':'GET, OPTIONS','Cache-Control':'no-store'};
 const json=(body:unknown,status=200)=>new Response(JSON.stringify(body),{status,headers});
 if(req.method==='OPTIONS')return new Response('ok',{headers});
 if(req.method!=='GET')return json({error:'GET required'},405);
 try{
  const url=new URL(req.url),pool=url.searchParams.get('pool_id'),wallet=url.searchParams.get('wallet')||undefined;
  if(!pool||!/^[0-9a-f-]{36}$/i.test(pool)||(wallet&&!algosdk.isValidAddress(wallet)))return json({error:'Invalid pool or wallet'},400);
  const db=createClient(Deno.env.get('SUPABASE_URL')!,Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!,{auth:{persistSession:false,autoRefreshToken:false}});
  const {data,error}=await db.rpc('get_funded_pool_display',{p_wallet:null}).abortSignal(AbortSignal.timeout(12000));
  if(error)throw new Error('Unavailable');
  const metadata=data.find((row:{pool_id:string})=>row.pool_id===pool);
  if(!metadata)return json({error:'Pool unavailable'},404);
  const {algod,indexer}=chainClients(),network=networkIdentity();
  const chain=new FundedChain(AlgorandClient.fromClients({algod,indexer}),metadata.config as VaultConfig,network);
  const view=await chain.display(wallet),s=view.snapshot;
  if(url.searchParams.get('prepare')==='true'){
   if(!wallet)return json({error:'Wallet required'},400);
   const amount=BigInt(view.reward!.allocated)-BigInt(view.reward!.paid);
   if(s.active!=='1'||s.paused!=='0'||amount<=0n)return json({error:'No claim is available'},409);
   const [params,account]=await Promise.all([algod.getTransactionParams().do(),algod.accountInformation(wallet).do()]);
   if(params.genesisID!==network.id||!params.genesisHash||base64(params.genesisHash)!==network.hash)throw new Error('Network differs');
   const asset=account.assets?.find(item=>item.assetId===BigInt(metadata.config.reward_asset_id));
   if(asset?.isFrozen)return json({error:'Reward asset is frozen'},409);
   const quote={pool_id:pool,wallet,app_id:metadata.config.app_id,reward_asset_id:metadata.config.reward_asset_id,
    genesis_id:network.id,genesis_hash:network.hash,first_valid:params.firstValid.toString(),last_valid:(params.firstValid+120n).toString(),
    claimable_atomic:amount.toString(),needs_opt_in:!asset};
   const transactions=claimTransactions(quote);
   return json({...quote,unsigned_transactions:transactions.map(tx=>base64(algosdk.encodeUnsignedTransaction(tx))),claim_tx_id:transactions.at(-1)!.txID()});
  }
  return json({pool_id:pool,name:metadata.name,reward_decimals:metadata.reward_decimals,reward_symbol:metadata.reward_symbol,
   snapshot:s,reward:view.reward?{...view.reward,claimable:(BigInt(view.reward.allocated)-BigInt(view.reward.paid)).toString()}:null,
   period:metadata.period,display:monthlyRewards(s as Parameters<typeof monthlyRewards>[0],metadata.period)});
 }catch{return json({error:'Pool state could not be verified. Please retry.'},503);}
});
