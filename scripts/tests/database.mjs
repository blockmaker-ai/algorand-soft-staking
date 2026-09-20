import {readFile,readdir} from 'node:fs/promises';
import assert from 'node:assert/strict';
import test from 'node:test';
import algosdk from 'algosdk';
import {PGlite} from '@electric-sql/pglite';
const pool='00000000-0000-4000-8000-000000000001',holder='00000000-0000-4000-8000-000000000002';
const alice=algosdk.encodeAddress(new Uint8Array(32).fill(1)),bob=algosdk.encodeAddress(new Uint8Array(32).fill(2));
const hash='a'.repeat(64),at='2020-01-01T00:00:00Z',half='2020-01-16T12:00:00Z';
const network={genesis_id:'fixture-v1',genesis_hash:Buffer.alloc(32,1).toString('base64')};
const config={app_id:'77',reward_asset_id:'1234',chain_pool_id:'42',opening_budget:'100',admin:alice,publisher:alice,buyback:bob,approval_sha256:hash,asset_policy_digest:hash,...network};
const snapshot=(extra={})=>({app_id:'77',round:'100',at,...network,balance:'1100',deposited:'1100',allocated:'100',paid:'0',active:'1',paused:'0',...extra});
const checkpoint=(extra={})=>({round:'100',at,block_at:extra.at||at,next_block_at:new Date(Date.parse(extra.at||at)+3000).toISOString(),policy_digest:hash,evidence_digest:hash,stake_export_digest:hash,stake_event_digest:hash,stake_event_count:'0',holdings:{[alice]:{'1234':'20'},[bob]:{}},...extra});
test('fresh schema, authenticated stakes and funded publication use real PostgreSQL',async t=>{
 const db=new PGlite();
 const query=async(sql,args=[])=>(await db.query(sql,args)).rows;
 const rpc=async(name,args=[])=>(await query(`SELECT public.${name}(${args.map((_,i)=>'$'+(i+1)).join(',')}) AS result`,args))[0].result;
 try{
  await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
   CREATE SCHEMA storage; CREATE TABLE storage.buckets(id text PRIMARY KEY,name text,public boolean,file_size_limit bigint);`);
  const dir=new URL('../../supabase/migrations/',import.meta.url);
  for(const name of (await readdir(dir)).filter(n=>n.endsWith('.sql')).sort())await db.exec('BEGIN;'+await readFile(new URL(name,dir),'utf8')+'COMMIT;');
  await query(`INSERT INTO pools(id,pool_name,pool_type,status,funding_confirmed,creator_address,contract_app_id,staking_token_id,staking_token_decimals,reward_token_id,reward_token_decimals)
    VALUES($1,'Example','single token staking','active',true,$2,77,'1234',6,'1234',6)`,[pool,alice]);
  await t.test('anonymous and authenticated users cannot write metadata, stakes or reward state',async()=>{
   for(const role of ['anon','authenticated']){
    await db.exec(`SET ROLE ${role}`);
    await query('SELECT * FROM pools');
    for(const table of ['pools','user_stakes','stake_events','funded_reward_vaults'])await assert.rejects(()=>query(`DELETE FROM ${table}`),/permission denied/);
    await assert.rejects(()=>rpc('get_funded_reward_state',[pool]),/permission denied/);
    await assert.rejects(()=>rpc('issue_pool_action_challenge',['stake',pool,alice,{amount:'1'},hash,'https://staking.example']),/permission denied/);
    await db.exec('RESET ROLE');
   }
   assert.equal((await query('SELECT public FROM storage.buckets'))[0].public,false);
  });
  await t.test('signed action nonce, stake mutation and history commit once; stale requests fail',async()=>{
   await db.exec('SET ROLE service_role');
   const issue=amount=>rpc('issue_pool_action_challenge',['stake',pool,alice,{amount},hash,'https://staking.example']);
   const c=await issue('10');
   const result=await rpc('apply_stake_action',[c.id,hash,'0','100','200']);
   assert.equal(result.newStakeAmount,'10');assert.deepEqual(await rpc('apply_stake_action',[c.id,hash,'0','100','200']),result);
   assert.equal((await query('SELECT count(*)::int AS n FROM stake_events'))[0].n,1);
   const stale=await issue('1');await assert.rejects(()=>rpc('apply_stake_action',[stale.id,hash,'0','100','201']),/changed/);
   await assert.rejects(()=>query("UPDATE user_stakes SET amount_staked='999'"),/permission denied/);
   await assert.rejects(()=>query('DELETE FROM stake_events'),/permission denied/);
   await db.exec('RESET ROLE');
  });
  await t.test('stake caps keep existing records and allow withdrawals',async()=>{
   await query("UPDATE pools SET max_stake='5' WHERE id=$1",[pool]);
   await db.exec('SET ROLE service_role');
   const c=await rpc('issue_pool_action_challenge',['stake',pool,alice,{amount:'1'},hash,'https://staking.example']);
   await assert.rejects(()=>rpc('apply_stake_action',[c.id,hash,'10','100','202']),/maximum/);
   const exit=await rpc('issue_pool_action_challenge',['unstake',pool,alice,{amount:'10'},hash,'https://staking.example']);
   const result=await rpc('apply_stake_action',[exit.id,hash,'10',null,null]);assert.equal(result.newStakeAmount,'0');
   await db.exec('RESET ROLE');
  });
  await db.exec('SET ROLE service_role');
  const register=(cfg=config,s=snapshot())=>rpc('register_funded_reward_vault',[pool,cfg,{[alice]:'60',[bob]:'40'},s]);
  await t.test('vault registration binds metadata, exact credits and network',async()=>{
   await assert.rejects(()=>register({...config,app_id:'78'}),/metadata/);
   await assert.rejects(()=>register(config,snapshot({genesis_id:'another-network'})),/network/);
   await register();await register();
   await assert.rejects(()=>register({...config,opening_budget:'99'}),/immutable/);
   await assert.rejects(()=>query('DELETE FROM funded_reward_vaults'),/permission denied/);
  });
  await t.test('overlapping staking assets cannot be registered in another funded pool',async()=>{
   const other='00000000-0000-4000-8000-000000000003';
   await query(`INSERT INTO pools(id,pool_name,pool_type,creator_address,contract_app_id,staking_token_id,staking_token_decimals,reward_token_id,reward_token_decimals)
     VALUES($1,'Overlap','single token staking',$2,78,'1234',6,'1234',6)`,[other,alice]);
   await assert.rejects(()=>rpc('register_funded_reward_vault',[other,{...config,app_id:'78'},{[alice]:'100'},snapshot({app_id:'78'})]),/overlap/);
  });
  await rpc('acquire_funded_reward_lease',[pool,holder,180]);
  let period,batch;
  await t.test('unpaid rewards stay reserved; leases and period openings are idempotent',async()=>{
   await assert.rejects(()=>rpc('acquire_funded_reward_lease',[pool,pool,180]),/Another publisher/);
   period=await rpc('open_funded_reward_period',[pool,holder,snapshot(),hash,checkpoint()]);
   assert.equal(period.budget_atomic,'1000');
   assert.deepEqual(await rpc('open_funded_reward_period',[pool,holder,snapshot(),hash,checkpoint()]),period);
  });
  const payload={period_id:period.id,revision:'0',start:at,end:half,checkpoint_before_digest:period.checkpoint_digest,
   checkpoint_after:checkpoint({at:half,round:'200'}),eligibility_digest:hash,calculation_digest:hash,evidence_digest:hash,
   scheduled_atomic:'500',issued_atomic:'500',new_rewards:{[alice]:'300',[bob]:'200'}};
  await t.test('an over-budget batch is rejected and a sealed batch is immutable',async()=>{
   await assert.rejects(()=>rpc('seal_funded_reward_batch',[pool,holder,{...payload,issued_atomic:'501',new_rewards:{[alice]:'301',[bob]:'200'}}]),/exceeds/);
   batch=await rpc('seal_funded_reward_batch',[pool,holder,payload]);
   assert.equal((await rpc('seal_funded_reward_batch',[pool,holder,payload])).id,batch.id);
   await assert.rejects(()=>rpc('seal_funded_reward_batch',[pool,holder,{...payload,new_rewards:{[alice]:'200',[bob]:'300'}}]),/Resume/);
  });
  const receipt=(wallet,cumulative,tx)=>({tx_id:tx,app_id:'77',wallet,allocated:cumulative,paid:'0',confirmed_round:'210',box_round:'211',transaction_digest:hash});
  await t.test('receipts require recorded attempts and all credits must confirm before the cursor advances',async()=>{
   await assert.rejects(()=>rpc('confirm_funded_reward_credit',[pool,holder,batch.id,alice,receipt(alice,'360','C'.repeat(52))]),/not recorded/);
   await assert.rejects(()=>rpc('finish_funded_reward_batch',[pool,holder,batch.id,snapshot({at:half,round:'220',allocated:'600'})]),/Not all/);
   for(const [wallet,cumulative,letter] of [[alice,'360','C'],[bob,'240','D']]){
    const tx=letter.repeat(52),attempt={tx_id:tx,unsigned_transaction:'YWJj',first_valid:'201',last_valid:'300'};
    await rpc('record_funded_reward_attempt',[pool,holder,batch.id,wallet,attempt,'202']);
    await rpc('record_funded_reward_attempt',[pool,holder,batch.id,wallet,attempt,'202']);
    await rpc('confirm_funded_reward_credit',[pool,holder,batch.id,wallet,receipt(wallet,cumulative,tx)]);
   }
   const result=await rpc('finish_funded_reward_batch',[pool,holder,batch.id,snapshot({at:half,round:'220',allocated:'600'})]);
   assert.equal(result.issued_atomic,'500');assert.equal(result.budget_atomic,'1000');
   assert.equal((await rpc('get_funded_reward_state',[pool])).pending,null);
  });
  await t.test('public display has the total budget and no wallet history or private evidence',async()=>{
   const [display]=await rpc('get_funded_pool_display',[null]);
   assert.equal(display.period.budget_atomic,'1000');assert.equal(display.period.issued_atomic,'500');
   const text=JSON.stringify(display);for(const key of ['holdings','checkpoint','stake_events','unsigned_transaction'])assert(!text.includes(key));
  });
 }finally{await db.close();}
});
