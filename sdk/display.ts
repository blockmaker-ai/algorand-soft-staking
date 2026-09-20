type Snapshot={balance:string;deposited:string;allocated:string;paid:string;available:string};
type Period={start:string;end:string;budget_atomic:string;scheduled_atomic:string;closed:boolean};
/** Monthly totals include rewards already issued/paid; claims never reduce the displayed budget. */
export function monthlyRewards(s:Snapshot,period:Period|null,now=Date.now()){
 const [balance,deposited,allocated,paid,available]=[s.balance,s.deposited,s.allocated,s.paid,s.available].map(BigInt);
 if(paid>allocated||allocated>deposited||balance<allocated-paid||available<0n
  ||available!==((deposited-allocated<balance-(allocated-paid))?deposited-allocated:balance-(allocated-paid)))throw new Error('Unbacked reward balance');
 const committed=period&&!period.closed?BigInt(period.budget_atomic)-BigInt(period.scheduled_atomic):0n;
 if(committed<0n)throw new Error('Invalid period');
 return {payingNowAtomic:period&&!period.closed&&Date.parse(period.start)<=now&&now<Date.parse(period.end)?period.budget_atomic:'0',
  nextMonthAtomic:(available>committed?available-committed:0n).toString()};
}
/** Display estimate only. Prices must use the same quote currency and eligible stake must reflect reward caps. */
export function annualRewardRate(budgetAtomic:string,rewardDecimals:number,start:string,end:string,eligibleStake:number,rewardPrice=1,stakePrice=1){
 const duration=Date.parse(end)-Date.parse(start);
 if(!Number.isInteger(rewardDecimals)||rewardDecimals<0||rewardDecimals>19||!Number.isFinite(duration)||duration<=0
  ||![eligibleStake,rewardPrice,stakePrice].every(n=>Number.isFinite(n)&&n>0))return null;
 const rate=Number(BigInt(budgetAtomic))/10**rewardDecimals*rewardPrice/(eligibleStake*stakePrice)*365*86400000/duration*100;
 return Number.isFinite(rate)&&rate>=0?rate:null;
}
