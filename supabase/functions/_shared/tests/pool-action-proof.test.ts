import algosdk from 'npm:algosdk@2.7.0';
import {Buffer} from 'node:buffer';
import {intentHash,poolIntent,type PoolChallenge} from '../pool-action-format.ts';
import {proofTransaction,verifyPoolProof} from '../pool-action-proof.ts';
const A=algosdk.generateAccount(), B=algosdk.generateAccount(), C=algosdk.generateAccount();
const origin='https://staking.example';
const now=Date.parse('2026-09-19T12:00:00Z');
const pool='00000000-0000-4000-8000-000000000001';
const b64=(value:Uint8Array)=>btoa(String.fromCharCode(...value));
function assert(ok:unknown,message='assertion failed'){if(!ok)throw new Error(message);}
async function rejects(action:()=>Promise<unknown>){let rejected=false;try{await action();}catch{rejected=true;}assert(rejected,'Expected authorization to fail');}
async function fixture(wallet=A.addr){
 const intent=poolIntent('stake',pool,wallet,{amount:'10'});
 const challenge:PoolChallenge={...intent,id:'10000000-0000-4000-8000-000000000001',origin,payload_hash:await intentHash(intent),expires_at:new Date(now+300000).toISOString()};
 return {intent,challenge};
}
Deno.test('standard wallet signs exact action with zero fee and no live rounds',async()=>{
 const {intent,challenge}=await fixture();const tx=proofTransaction(challenge);
 assert((tx.fee??0)===0 && tx.firstRound===1 && tx.lastRound===1);
 await verifyPoolProof(challenge,intent,b64(tx.signTxn(A.sk)),A.addr,origin,now);
});
Deno.test('fabricated signatures are rejected',async()=>{
 const {intent,challenge}=await fixture();const tx=proofTransaction(challenge);
 const forged=algosdk.encodeObj({txn:tx.get_obj_for_encoding(),sig:new Uint8Array(64).fill(1)});
 await rejects(()=>verifyPoolProof(challenge,intent,b64(forged),A.addr,origin,now));
});
Deno.test('wrong wallet and wrong authority rejected',async()=>{
 const {intent,challenge}=await fixture();const proof=b64(proofTransaction(challenge).signTxn(B.sk));
 await rejects(()=>verifyPoolProof(challenge,intent,proof,A.addr,origin,now));
});
Deno.test('rekeyed account verifies against current signing authority',async()=>{
 const {intent,challenge}=await fixture();const tx=proofTransaction(challenge);
 await verifyPoolProof(challenge,intent,b64(tx.signTxn(B.sk)),B.addr,origin,now);
 await rejects(()=>verifyPoolProof(challenge,intent,b64(tx.signTxn(A.sk)),B.addr,origin,now));
});
for(const change of ['pool','amount','action','origin','expiry','note','fee','close','rekey','group']){
 Deno.test(`rejects changed ${change}`,async()=>{
  const {intent,challenge}=await fixture();let checkIntent=intent, checkOrigin=origin, checkTime=now;
  const tx=proofTransaction(challenge);
  if(change==='pool')checkIntent={...intent,pool_id:'00000000-0000-4000-8000-000000000002'};
  if(change==='amount')checkIntent={...intent,payload:{amount:'11'}};
  if(change==='action')checkIntent={...intent,action:'unstake'};
  if(change==='origin')checkOrigin='https://example.invalid';
  if(change==='expiry')checkTime=now+300000;
  if(change==='note')tx.note=new TextEncoder().encode('another action');
  if(change==='fee')tx.fee=1000;
  if(change==='close')tx.closeRemainderTo=algosdk.decodeAddress(B.addr);
  if(change==='rekey')tx.reKeyTo=algosdk.decodeAddress(B.addr);
  if(change==='group')tx.group=Buffer.alloc(32,1);
  await rejects(()=>verifyPoolProof(challenge,checkIntent,b64(tx.signTxn(A.sk)),A.addr,checkOrigin,checkTime));
 });
}
Deno.test('threshold multisignature verifies; incomplete signature rejected',async()=>{
 const metadata={version:1,threshold:2,addrs:[A.addr,B.addr,C.addr]};
 const wallet=algosdk.multisigAddress(metadata);const {intent,challenge}=await fixture(wallet);const tx=proofTransaction(challenge);
 const first=algosdk.signMultisigTransaction(tx,metadata,A.sk).blob;
 await rejects(()=>verifyPoolProof(challenge,intent,b64(first),wallet,origin,now));
 const complete=algosdk.appendSignMultisigTransaction(first,metadata,B.sk).blob;
 await verifyPoolProof(challenge,intent,b64(complete),wallet,origin,now);
});
Deno.test('rekeyed multisignature verifies against account auth address',async()=>{
 const metadata={version:1,threshold:2,addrs:[A.addr,B.addr,C.addr]};const authority=algosdk.multisigAddress(metadata);
 const {intent,challenge}=await fixture(A.addr);const first=algosdk.signMultisigTransaction(proofTransaction(challenge),metadata,A.sk).blob;
 const complete=algosdk.appendSignMultisigTransaction(first,metadata,B.sk).blob;
 await verifyPoolProof(challenge,intent,b64(complete),authority,origin,now);
});
Deno.test('stored action corruption is rejected',async()=>{
 const {intent,challenge}=await fixture();const proof=b64(proofTransaction(challenge).signTxn(A.sk));
 await rejects(()=>verifyPoolProof({...challenge,payload:{amount:'999'}},intent,proof,A.addr,origin,now));
});
Deno.test('empty and oversized proofs are rejected',async()=>{
 const {intent,challenge}=await fixture();
 await rejects(()=>verifyPoolProof(challenge,intent,'',A.addr,origin,now));
 await rejects(()=>verifyPoolProof(challenge,intent,'a'.repeat(17000),A.addr,origin,now));
});

Deno.test('browser SDK proof bytes match the server verifier',async()=>{
 const {poolActionProof}=await import('../../../../sdk/pool-actions.ts');
 const {intent,challenge}=await fixture();
 const actual=poolActionProof(challenge);
 assert(Buffer.from(actual.toByte()).equals(Buffer.from(algosdk.encodeUnsignedTransaction(proofTransaction(challenge)))));
 await verifyPoolProof(challenge,intent,b64(actual.signTxn(A.sk)),A.addr,origin,now);
});
