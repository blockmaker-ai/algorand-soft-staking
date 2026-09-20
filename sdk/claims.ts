import algosdk from 'algosdk';
export type ClaimQuote={pool_id:string;wallet:string;app_id:string;reward_asset_id:string;genesis_id:string;genesis_hash:string;
 first_valid:string;last_valid:string;claimable_atomic:string;needs_opt_in:boolean;unsigned_transactions:string[];claim_tx_id:string};
export type ClaimIdentity={pool_id:string;app_id:string;reward_asset_id:string;genesis_id:string;genesis_hash:string};
const bytes=(text:string)=>Uint8Array.from(atob(text),c=>c.charCodeAt(0));
export const base64=(value:Uint8Array)=>btoa(String.fromCharCode(...value));
const uint=(value:string)=>{
 if(typeof value!=='string'||!/^(0|[1-9][0-9]{0,19})$/.test(value)||BigInt(value)>18446744073709551615n)throw new Error('Invalid exact claim amount');
 return BigInt(value);
};
export function claimTransactions(q:Omit<ClaimQuote,'unsigned_transactions'|'claim_tx_id'>){
 if(!algosdk.isValidAddress(q.wallet)||typeof q.needs_opt_in!=='boolean'||uint(q.app_id)===0n||uint(q.reward_asset_id)===0n
   ||uint(q.claimable_atomic)===0n||uint(q.first_valid)===0n||uint(q.last_valid)<=uint(q.first_valid)
   ||uint(q.last_valid)-uint(q.first_valid)>300n||bytes(q.genesis_hash).length!==32)throw new Error('Invalid claim quote');
 const suggestedParams={flatFee:true,fee:1000n,minFee:1000n,firstValid:uint(q.first_valid),lastValid:uint(q.last_valid),genesisID:q.genesis_id,genesisHash:bytes(q.genesis_hash)};
 const txns:algosdk.Transaction[]=[];
 if(q.needs_opt_in)txns.push(algosdk.makeAssetTransferTxnWithSuggestedParamsFromObject({sender:q.wallet,receiver:q.wallet,assetIndex:uint(q.reward_asset_id),amount:0n,suggestedParams}));
 txns.push(algosdk.makeApplicationNoOpTxnFromObject({sender:q.wallet,appIndex:uint(q.app_id),
  appArgs:[algosdk.ABIMethod.fromSignature('claimRewards()uint64').getSelector()],foreignAssets:[uint(q.reward_asset_id)],
  boxes:[{appIndex:uint(q.app_id),name:new Uint8Array([114,...algosdk.decodeAddress(q.wallet).publicKey])}],
  note:new TextEncoder().encode('Soft staking: claim funded rewards'),suggestedParams:{...suggestedParams,fee:2000n}}));
 if(txns.length>1)algosdk.assignGroupID(txns);
 return txns;
}
/** Pin the deployment identity in your application; do not trust arbitrary quote fields. */
export function verifyClaimQuote(q:ClaimQuote,expected:ClaimIdentity,wallet:string){
 for(const key of ['pool_id','app_id','reward_asset_id','genesis_id','genesis_hash'] as const){
  if(q[key]!==expected[key])throw new Error('Claim belongs to another deployment');
 }
 if(q.wallet!==wallet)throw new Error('Claim belongs to another wallet');
 const transactions=claimTransactions(q);
 if(!Array.isArray(q.unsigned_transactions)||q.unsigned_transactions.length!==transactions.length
  ||transactions.some((tx,index)=>base64(algosdk.encodeUnsignedTransaction(tx))!==q.unsigned_transactions[index])
  ||transactions.at(-1)!.txID()!==q.claim_tx_id)throw new Error('Claim transaction group was changed');
 return transactions;
}
