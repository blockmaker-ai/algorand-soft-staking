import test from 'node:test';
import assert from 'node:assert/strict';
import algosdk from 'algosdk';
import {claimTransactions,verifyClaimQuote,base64} from '../../sdk/claims.ts';
import {monthlyRewards,annualRewardRate} from '../../sdk/display.ts';
import {requestPreparedWalletSignatures} from '../../sdk/preparedWalletSigning.js';
const account=algosdk.generateAccount();
const expected={pool_id:'00000000-0000-4000-8000-000000000001',app_id:'42',reward_asset_id:'1234',genesis_id:'fixture-v1',genesis_hash:Buffer.alloc(32,1).toString('base64')};
function quote(needs_opt_in=false){
 const q={...expected,wallet:account.addr.toString(),first_valid:'100',last_valid:'220',claimable_atomic:'1000000',needs_opt_in};
 const txns=claimTransactions(q);
 return {...q,unsigned_transactions:txns.map(t=>base64(algosdk.encodeUnsignedTransaction(t))),claim_tx_id:txns.at(-1).txID()};
}
test('claims allow only the exact reviewed group, including optional opt-in',()=>{
 for(const opt of [false,true]){
  const q=quote(opt),txns=verifyClaimQuote(q,expected,account.addr.toString());assert.equal(txns.length,opt?2:1);
  for(const change of [tx=>{tx.fee=99000n},tx=>{tx.rekeyTo=algosdk.generateAccount().addr},tx=>{tx.applicationCall.appIndex=99n},tx=>{tx.note=new Uint8Array([1])}]){
   const bad=structuredClone(q),tx=algosdk.decodeUnsignedTransaction(Buffer.from(bad.unsigned_transactions.at(-1),'base64'));change(tx);
   bad.unsigned_transactions[bad.unsigned_transactions.length-1]=base64(algosdk.encodeUnsignedTransaction(tx));bad.claim_tx_id=tx.txID();
   assert.throws(()=>verifyClaimQuote(bad,expected,account.addr.toString()));
  }
  assert.throws(()=>verifyClaimQuote({...q,genesis_id:'other'},expected,account.addr.toString()));
 }
});
test('wallet request starts during the click and a late cancelled response cannot submit',async()=>{
 const transactions=verifyClaimQuote(quote(),expected,account.addr.toString());let started=false,resolve;
 const controller=new AbortController();
 const result=requestPreparedWalletSignatures({transactions,receivedAt:Date.now(),signal:controller.signal,
  signTransactions:()=>{started=true;return new Promise(r=>{resolve=r;});}});
 assert(started);controller.abort();await assert.rejects(result,/cancelled/);
 resolve(transactions.map(t=>t.signTxn(account.sk)));
 await assert.rejects(requestPreparedWalletSignatures({transactions,receivedAt:Date.now()-61000,signTransactions:()=>{throw new Error('Must not open');}}),/out of date/);
});
test('paying now remains the monthly total after allocation and claims; APR uses actual duration',()=>{
 const period={start:'2020-01-01T00:00:00Z',end:'2020-02-01T00:00:00Z',budget_atomic:'1000',scheduled_atomic:'500',closed:false};
 const before={deposited:'1100',allocated:'600',paid:'0',balance:'1100',available:'500'};
 const after={...before,paid:'600',balance:'500'};
 for(const state of [before,after])assert.deepEqual(monthlyRewards(state,period,Date.parse('2020-01-20T00:00:00Z')),{payingNowAtomic:'1000',nextMonthAtomic:'0'});
 assert(Math.abs(annualRewardRate('100',0,period.start,period.end,1000)-365/31*10)<1e-9);
 assert.equal(annualRewardRate('100',0,period.start,period.end,0),null);
});
