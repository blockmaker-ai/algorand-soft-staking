import {publishFundedPass} from '../funded-publisher.ts';
const assert=(value: unknown,message='assertion failed')=>{if(!value)throw new Error(message);};
const rejects=async(fn:()=>Promise<unknown>,pattern:RegExp)=>{
  let failure:unknown;try{await fn();}catch(error){failure=error;}
  assert(failure instanceof Error && pattern.test(failure.message),'expected rejection');
};

function fixture(count=2) {
  const credits=Array.from({length:count},(_,i)=>({wallet:`wallet-${i}`,previous:'0',cumulative:'10',receipt:null as unknown,
    attempts:[] as {tx_id:string;unsigned_transaction:string;first_valid:string;last_valid:string}[]}));
  const pending={id:'a'.repeat(64),payload:{},credits};
  const state={config:{},confirmed_allocated:'0',pending:pending as typeof pending | null};
  const accepted=new Map<string,unknown>(), actual=new Map<string,bigint>();
  const calls:string[]=[],faults={record:false,confirm:false,submitAfterCommit:false,submitBeforeCommit:false,finish:false,lease:false,evidence:false};
  let round=100n,sequence=0;
  const deps={
    async verifyEvidence(){calls.push('verify-evidence');if(faults.evidence)throw new Error('Evidence archive is unavailable');},
    async rpc(name:string,args:Record<string,unknown>):Promise<unknown> {
      calls.push(name);
      if(name==='acquire_funded_reward_lease' && faults.lease)throw new Error('lease busy');
      if(name==='get_funded_reward_state')return structuredClone(state);
      const credit=credits.find(item=>item.wallet===args.p_wallet);
      if(name==='record_funded_reward_attempt'){
        if(faults.record)throw new Error('database unavailable');
        credit!.attempts.push(structuredClone(args.p_attempt) as typeof credits[number]['attempts'][number]);
        calls.push(`persist:${(args.p_attempt as {tx_id:string}).tx_id}`);
      }
      if(name==='confirm_funded_reward_credit'){
        if(faults.confirm)throw new Error('database unavailable');
        assert(credit!.receipt===null,'duplicate confirmation');
        credit!.receipt=args.p_receipt;state.confirmed_allocated=(BigInt(state.confirmed_allocated)+10n).toString();
      }
      if(name==='finish_funded_reward_batch'){
        if(faults.finish)throw new Error('final snapshot unavailable');
        assert(credits.every(credit=>credit.receipt!==null));
        assert([...actual.values()].reduce((sum,n)=>sum+n,0n)===BigInt(state.confirmed_allocated));
        state.pending=null;
      }
      return null;
    },
    async snapshot(){return {allocated:[...actual.values()].reduce((sum,n)=>sum+n,0n).toString(),
      active:'1',paused:'0',round:round.toString(),deposited:'1000',paid:'0',balance:'1000'};},
    async prepare(_batch:string,credit:typeof credits[number]){
      calls.push('prepare');sequence++;
      return {tx_id:`attempt-${sequence}-${credit.wallet}`,unsigned_transaction:'encoded',first_valid:round.toString(),last_valid:(round+120n).toString()};
    },
    async receipt(_batch:string,_credit:typeof credits[number],attempt:typeof credits[number]['attempts'][number]){
      calls.push('receipt');return accepted.get(attempt.tx_id) || null;
    },
    async submit(_batch:string,credit:typeof credits[number],attempt:typeof credits[number]['attempts'][number]){
      assert(credits.find(value=>value.wallet===credit.wallet)!.attempts.some(value=>value.tx_id===attempt.tx_id),'broadcast before persistence');
      calls.push(`submit:${attempt.tx_id}`);
      if(faults.submitBeforeCommit)throw new Error('connection failed');
      const current=actual.get(credit.wallet) || 0n;
      assert(current===BigInt(credit.previous) || current===BigInt(credit.cumulative));
      actual.set(credit.wallet,BigInt(credit.cumulative));
      accepted.set(attempt.tx_id,{tx_id:attempt.tx_id,allocated:credit.cumulative});
      if(faults.submitAfterCommit)throw new Error('connection lost after commit');
    },
    async round(){return round;},
  };
  return {deps,state,credits,accepted,actual,calls,faults,setRound:(value:bigint)=>{round=value;}};
}

Deno.test('funded publisher persists each exact attempt before submitting and finishes the reconciled batch',async()=>{
  const f=fixture();const result=await publishFundedPass('pool','holder',f.deps);
  assert(result.status==='confirmed' && result.confirmed===2 && result.remaining===0);
  assert(f.state.confirmed_allocated==='20' && f.state.pending===null);
  for(const call of f.calls.filter(call=>call.startsWith('submit:'))){
    assert(f.calls.indexOf(call)>f.calls.indexOf(call.replace('submit:','persist:')));
  }
  assert(f.calls.at(-1)==='release_funded_reward_lease');
});

Deno.test('approved opening publication requires an inactive vault and preserves the same save-before-send rules',async()=>{
  const f=fixture(),read=f.deps.snapshot;
  f.deps.snapshot=async()=>({...await read(),active:'0'});
  await rejects(()=>publishFundedPass('pool','holder',f.deps),/activation/);
  assert(!f.calls.some(call=>call.startsWith('submit:')));
  const result=await publishFundedPass('pool','holder',f.deps,true);
  assert(result.status==='confirmed' && f.state.confirmed_allocated==='20');
  for(const call of f.calls.filter(call=>call.startsWith('submit:')))assert(f.calls.indexOf(call)>f.calls.indexOf(call.replace('submit:','persist:')));
  const active=fixture();await rejects(()=>publishFundedPass('pool','holder',active.deps,true),/activation/);
  assert(!active.calls.some(call=>call.startsWith('submit:')));
});

Deno.test('missing archived evidence prevents every preparation and broadcast',async()=>{
  const f=fixture();f.faults.evidence=true;
  await rejects(()=>publishFundedPass('pool','holder',f.deps),/Evidence archive/);
  assert(!f.calls.includes('prepare') && !f.calls.some(value=>value.startsWith('submit:')));
  assert(f.calls.at(-1)==='release_funded_reward_lease');
});

Deno.test('a failed attempt save cannot lead to a broadcast or changed reward',async()=>{
  const f=fixture(1);f.faults.record=true;
  const result=await publishFundedPass('pool','holder',f.deps);
  assert(result.status==='pending' && result.remaining===1);
  assert(f.actual.size===0 && f.credits[0].attempts.length===0);
  assert(!f.calls.some(call=>call.startsWith('submit:')));
});

Deno.test('accepted transactions are recovered after confirmation recording fails, without submitting again',async()=>{
  const f=fixture(1);f.faults.confirm=true;
  const first=await publishFundedPass('pool','holder',f.deps);
  assert(first.status==='pending' && f.actual.get('wallet-0')===10n && f.state.confirmed_allocated==='0');
  f.faults.confirm=false;
  const second=await publishFundedPass('pool','holder',f.deps);
  assert(second.status==='confirmed' && f.state.confirmed_allocated==='10');
  assert(f.calls.filter(call=>call.startsWith('submit:')).length===1);
  assert(f.credits[0].attempts.length===1);
});

Deno.test('a lost submission response is resolved from its exact confirmed transaction',async()=>{
  const f=fixture(1);f.faults.submitAfterCommit=true;
  const result=await publishFundedPass('pool','holder',f.deps);
  assert(result.status==='confirmed' && f.actual.get('wallet-0')===10n);
});

Deno.test('an unconfirmed unexpired transaction is reused after a network failure',async()=>{
  const f=fixture(1);f.faults.submitBeforeCommit=true;
  const first=await publishFundedPass('pool','holder',f.deps);
  assert(first.status==='pending' && f.credits[0].attempts.length===1 && f.actual.size===0);
  f.faults.submitBeforeCommit=false;
  const second=await publishFundedPass('pool','holder',f.deps);
  assert(second.status==='confirmed');
  assert(f.calls.filter(call=>call==='prepare').length===1 && f.credits[0].attempts.length===1);
});

Deno.test('an expired transaction with a forgotten receipt can be verified by an identical harmless credit retry',async()=>{
  const f=fixture(1);
  f.credits[0].attempts.push({tx_id:'old',unsigned_transaction:'encoded',first_valid:'1',last_valid:'20'});
  f.actual.set('wallet-0',10n); // Accepted previously, but the provider no longer has its receipt.
  const result=await publishFundedPass('pool','holder',f.deps);
  assert(result.status==='confirmed' && f.actual.get('wallet-0')===10n);
  assert(f.state.confirmed_allocated==='10' && f.credits[0].attempts.length===2);
});

Deno.test('unexpected on-chain allocations stop a pass before any publication',async()=>{
  const f=fixture(1);f.actual.set('unexpected',11n);
  await rejects(()=>publishFundedPass('pool','holder',f.deps),/disagree/);
  assert(!f.calls.includes('prepare'));assert(f.calls.at(-1)==='release_funded_reward_lease');
});

Deno.test('a failed final database commit resumes finalization without repeating confirmed credits',async()=>{
  const f=fixture(1);f.faults.finish=true;
  await rejects(()=>publishFundedPass('pool','holder',f.deps),/snapshot unavailable/);
  assert(f.state.pending!==null && f.state.confirmed_allocated==='10');
  f.faults.finish=false;
  const result=await publishFundedPass('pool','holder',f.deps);
  assert(result.status==='confirmed' && result.confirmed===0);
  assert(f.calls.filter(call=>call.startsWith('submit:')).length===1);
});

Deno.test('publication is bounded to sixteen credits and preserves the rest for another pass',async()=>{
  const f=fixture(20);
  const first=await publishFundedPass('pool','holder',f.deps);
  assert(first.status==='pending' && first.confirmed===16 && first.remaining===4);
  const second=await publishFundedPass('pool','holder',f.deps);
  assert(second.status==='confirmed' && second.confirmed===4);
  assert(f.actual.size===20 && f.state.confirmed_allocated==='200');
});

Deno.test('empty windows finalize without transactions and a competing lease prevents every action',async()=>{
  const empty=fixture(0);const result=await publishFundedPass('pool','holder',empty.deps);
  assert(result.status==='confirmed' && !empty.calls.includes('prepare'));
  const busy=fixture();busy.faults.lease=true;
  await rejects(()=>publishFundedPass('pool','holder',busy.deps),/lease busy/);
  assert(busy.calls.length===1);
});
