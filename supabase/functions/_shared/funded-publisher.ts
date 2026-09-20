// A bounded publication pass. Injected adapters keep signing/provider/database
// behavior testable without importing an Edge server or using production keys.
type Credit = {wallet: string; previous: string; cumulative: string; receipt: unknown; attempts: Attempt[]};
type Attempt = {tx_id: string; unsigned_transaction: string; first_valid: string; last_valid: string};
type Pending = {id: string; payload: Record<string, unknown>; credits: Credit[]};
type State = {pending: Pending | null; config: Record<string, unknown>; confirmed_allocated: string};
type Dependencies = {
  rpc: (name: string, args: Record<string, unknown>) => Promise<unknown>;
  snapshot: () => Promise<Record<string, string>>;
  verifyEvidence: (pending: Pending) => Promise<void>;
  prepare: (batch: string, credit: Credit) => Promise<Attempt>;
  // Return a verified receipt only when the exact stored transaction and its
  // current reward box are confirmed. A missing/pending transaction returns null.
  receipt: (batch: string, credit: Credit, attempt: Attempt) => Promise<unknown | null>;
  // This is called only AFTER record_funded_reward_attempt commits.
  submit: (batch: string, credit: Credit, attempt: Attempt) => Promise<void>;
  round: () => Promise<bigint>;
  now?: () => number;
};
const exact = (value: string) => {
  if (typeof value !== 'string' || !/^(0|[1-9][0-9]{0,19})$/.test(value) || BigInt(value)>18446744073709551615n) throw new Error('Invalid ledger amount');
  return BigInt(value);
};

/** One lease, one sealed batch, at most sixteen credits per invocation. A retry
 * never changes a recipient or amount; an expired attempt is replaced only by
 * the same compare-and-set credit, which is harmless if it already succeeded.
 */
export async function publishFundedPass(pool: string, holder: string, deps: Dependencies, opening = false) {
  const now=deps.now || Date.now, deadline=now()+50_000;
  await deps.rpc('acquire_funded_reward_lease',{p_pool:pool,p_holder:holder,p_seconds:180});
  try {
    const state=await deps.rpc('get_funded_reward_state',{p_pool:pool}) as State;
    if (!state.pending) return {status:'idle',confirmed:0,remaining:0};
    const pending=state.pending;
    await deps.verifyEvidence(pending);
    const unresolved=pending.credits.filter(credit=>credit.receipt===null);
    const before=await deps.snapshot();
    if (before.active!==(opening?'0':'1') || before.paused!=='0') throw new Error('The funded vault activation or pause state differs');
    const known=exact(state.confirmed_allocated);
    const maximum=unresolved.reduce((sum,credit)=>sum+exact(credit.cumulative)-exact(credit.previous),known);
    if (exact(before.allocated)<known || exact(before.allocated)>maximum) throw new Error('On-chain allocations disagree with the sealed ledger');
    let confirmed=0;
    const problems: string[]=[];
    // Four concurrent credits share this invocation's lease. Every mutation is
    // also serialized by the pool row lock in PostgreSQL.
    const selected=unresolved.slice(0,16);
    for (let offset=0;offset<selected.length && now()<deadline;offset+=4) {
      const results=await Promise.allSettled(selected.slice(offset,offset+4).map(async credit=>{
        let attempt: Attempt | undefined;
        for (const previous of credit.attempts) {
          const receipt=await deps.receipt(pending.id,credit,previous);
          if (receipt!==null) {
            await deps.rpc('confirm_funded_reward_credit',{p_pool:pool,p_holder:holder,p_batch:pending.id,p_wallet:credit.wallet,p_receipt:receipt});
            confirmed++;return;
          }
        }
        if (now()>=deadline) return;
        const round=await deps.round();
        attempt=credit.attempts.find(value=>exact(value.first_valid)<=round && exact(value.last_valid)>=round);
        if (!attempt) {
          if (credit.attempts.some(value=>exact(value.last_valid)>=round)) throw new Error('A previous transaction validity window is unresolved');
          attempt=await deps.prepare(pending.id,credit);
          await deps.rpc('record_funded_reward_attempt',{p_pool:pool,p_holder:holder,p_batch:pending.id,p_wallet:credit.wallet,
            p_attempt:attempt,p_observed_round:(await deps.round()).toString()});
        }
        // The same encoded transaction may be submitted again after a timeout.
        // A submit error is ambiguous: check confirmation, then preserve it.
        let submitFailed=false;
        try {await deps.submit(pending.id,credit,attempt);} catch {submitFailed=true;}
        const receipt=await deps.receipt(pending.id,credit,attempt);
        if (receipt!==null) {
          await deps.rpc('confirm_funded_reward_credit',{p_pool:pool,p_holder:holder,p_batch:pending.id,p_wallet:credit.wallet,p_receipt:receipt});
          confirmed++;
        } else if (submitFailed) {
          problems.push('Submission outcome is unknown; the stored transaction will be checked again.');
        }
      }));
      for (const result of results) if (result.status==='rejected') {
        // Do not serialize provider errors or signer objects, which may include
        // key material. Detailed diagnosis uses the stored public transaction.
        problems.push('A credit could not be verified or recorded; its sealed state has been preserved.');
      }
    }
    const after=await deps.rpc('get_funded_reward_state',{p_pool:pool}) as State;
    if (!after.pending || after.pending.id!==pending.id) throw new Error('The pending batch changed during publication');
    const remaining=after.pending.credits.filter(credit=>credit.receipt===null).length;
    if (remaining===0) {
      await deps.rpc('finish_funded_reward_batch',{p_pool:pool,p_holder:holder,p_batch:pending.id,p_snapshot:await deps.snapshot()});
      return {status:'confirmed',batch_id:pending.id,confirmed,remaining:0};
    }
    return {status:'pending',batch_id:pending.id,confirmed,remaining,problems};
  } finally {
    // A failed release cannot erase publication evidence; the bounded lease
    // expires automatically and any recorded in-flight transaction stays bound.
    try {await deps.rpc('release_funded_reward_lease',{p_pool:pool,p_holder:holder});} catch { /* expires */ }
  }
}
