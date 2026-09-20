import algosdk from 'algosdk';
import {challengeNote, MAINNET_GENESIS_HASH, MAINNET_GENESIS_ID, type PoolChallenge} from '../supabase/functions/_shared/pool-action-format.ts';
/** An expired, zero-fee self-payment used only as an ownership proof. Never broadcast. */
export function poolActionProof(challenge:PoolChallenge){
 return algosdk.makePaymentTxnWithSuggestedParamsFromObject({sender:challenge.wallet_address,receiver:challenge.wallet_address,amount:0n,
  note:challengeNote(challenge),suggestedParams:{flatFee:true,fee:0n,minFee:0n,firstValid:1n,lastValid:1n,
   genesisID:MAINNET_GENESIS_ID,genesisHash:Uint8Array.from(atob(MAINNET_GENESIS_HASH),c=>c.charCodeAt(0))}});
}
