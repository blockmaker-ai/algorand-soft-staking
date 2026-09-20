import {createClient} from 'npm:@supabase/supabase-js@2';
import {AlgorandClient, Config} from '@algorandfoundation/algokit-utils';
import algosdk from 'algosdk';
import {FundedChain, type VaultConfig} from '../../../contracts/funded-rewards/funded-chain.ts';
import {publishFundedPass} from '../_shared/funded-publisher.ts';
import {verifyArchivedEvidence} from '../_shared/funded-evidence.ts';
import {chainClients, networkIdentity} from '../_shared/server-config.ts';
import {fundedServiceAuthorized} from '../_shared/funded-service-auth.ts';

const json=(body:unknown,status=200)=>new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json','Cache-Control':'no-store'}});
Config.configure({debug:false,populateAppCallResources:false});

Deno.serve(async req=>{
  if(req.method!=='POST')return json({error:'POST required'},405);
  const service=Deno.env.get('SUPABASE_SERVICE_ROLE_KEY') || '';
  if(!service || !await fundedServiceAuthorized(req))return json({error:'Unauthorized'},401);
  let secret:Uint8Array | undefined;
  try {
    const raw=await req.text();
    if(raw.length>1024)return json({error:'Invalid publication request'},400);
    const body=JSON.parse(raw);
    if(!body || typeof body!=='object' || Array.isArray(body) || typeof body.pool_id!=='string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(body.pool_id)
      || Object.keys(body).some(key=>!['pool_id','preview'].includes(key))
      || (body.preview!==undefined && typeof body.preview!=='boolean'))return json({error:'Invalid publication request'},400);
    const preview=body.preview!==false;
    if(!preview && Deno.env.get('FUNDED_REWARDS_PUBLICATION_ENABLED')!=='true')return json({publication_enabled:false,error:'Funded publication is not enabled'},409);
    const db=createClient(Deno.env.get('SUPABASE_URL')!,service,{auth:{persistSession:false,autoRefreshToken:false}});
    const rpc=async(name:string,args:Record<string,unknown>)=>{
      const {data,error}=await db.rpc(name,args).abortSignal(AbortSignal.timeout(12_000));
      if(error)throw new Error(`Funded accounting operation failed: ${name}`);
      return data;
    };
    const initial=await rpc('get_funded_reward_state',{p_pool:body.pool_id});
    const {data:pool,error:poolError}=await db.from('pools').select('contract_app_id,hidden').eq('id',body.pool_id).single();
    if(poolError || !pool || pool.hidden===true || String(pool.contract_app_id)!==initial.config.app_id)throw new Error('The funded pool mapping is not active');
    const {algod,indexer}=chainClients();
    const algorand=AlgorandClient.fromClients({algod,indexer});
    const chain=new FundedChain(algorand,initial.config as VaultConfig,networkIdentity());
    if(preview)return json({mode:'preview',snapshot:await chain.snapshot(),pending_batch:initial.pending?.id || null,
      pending_credits:initial.pending?.credits.filter((credit:{receipt:unknown})=>credit.receipt===null).length || 0});
    if(!initial.pending)return json({status:'idle',confirmed:0,remaining:0});
    let signingKey:Promise<Uint8Array> | undefined;
    const result=await publishFundedPass(body.pool_id,crypto.randomUUID(),{
      rpc,snapshot:()=>chain.snapshot(),prepare:(batch,credit)=>chain.prepareAllocation(batch,credit),
      verifyEvidence:async pending=>{
        const digest=pending.payload.evidence_digest;
        if(typeof digest!=='string' || !/^[0-9a-f]{64}$/.test(digest))throw new Error('Missing sealed evidence identity');
        const response=await fetch(`${Deno.env.get('SUPABASE_URL')}/storage/v1/object/authenticated/funded-reward-evidence/${body.pool_id}/${digest}.json.gz`,{
          headers:{Authorization:`Bearer ${service}`,apikey:service},signal:AbortSignal.timeout(30_000),
        });
        await verifyArchivedEvidence(response,digest);
      },
      round:async()=>(await algod.status().do()).lastRound,
      receipt:async(batch,credit,attempt)=>{
        let confirmation;
        try {confirmation=await algod.pendingTransactionInformation(attempt.tx_id).do();}
        catch(error){
          if((error as {status?:number}).status===404 || (error as {response?:{status?:number}}).response?.status===404)return null;
          throw new Error('Transaction confirmation provider is unavailable');
        }
        if(!confirmation.confirmedRound)return null;
        return chain.confirmedCredit(batch,credit,attempt,confirmation);
      },
      submit:async(batch,credit,attempt)=>{
        const txn=chain.validateAttempt(batch,credit,attempt);
        // Load the signer only after the archive, reserve and saved attempt were checked.
        signingKey ||= Promise.resolve().then(()=>{
          const account=algosdk.mnemonicToSecretKey(Deno.env.get('PUBLISHER_MNEMONIC') || '');
          if(account.addr.toString()!==initial.config.publisher){account.sk.fill(0);throw new Error('Publisher does not match this vault');}
          secret=account.sk;return secret;
        });
        const result=await algod.sendRawTransaction(txn.signTxn(await signingKey)).do();
        if(result.txid!==attempt.tx_id)throw new Error('Submission returned an unexpected transaction identity');
      },
    });
    return json(result,result.status==='pending'?202:200);
  } catch(error) {
    // Never serialize the SDK error object or decrypted signer material.
    const message=error instanceof SyntaxError?'Invalid publication request':'Funded publication could not be confirmed; retry the stored batch';
    return json({error:message,retryable:!(error instanceof SyntaxError)},error instanceof SyntaxError?400:503);
  } finally {secret?.fill(0);}
});
