import algosdk from 'npm:algosdk@2.7.0';
import {challengeNote, intentHash, MAINNET_GENESIS_HASH, MAINNET_GENESIS_ID, type PoolChallenge, type PoolIntent} from './pool-action-format.ts';

export class PoolActionError extends Error {
  constructor(message: string, public status = 401) { super(message); }
}

export function proofTransaction(challenge: PoolChallenge) {
  // Expired round one and zero fee make this an off-chain proof. Never submit it.
  return algosdk.makePaymentTxnWithSuggestedParamsFromObject({
    from: challenge.wallet_address, to: challenge.wallet_address, amount: 0,
    note: challengeNote(challenge),
    suggestedParams: {fee: 0, flatFee: true, firstRound: 1, lastRound: 1, genesisID: MAINNET_GENESIS_ID, genesisHash: MAINNET_GENESIS_HASH},
  });
}

function equalBytes(a: Uint8Array, b: Uint8Array) { return a.length === b.length && a.every((byte, i) => byte === b[i]); }

export async function verifyPoolProof(challenge: PoolChallenge, intent: PoolIntent, signedProof: string, authorizedAddress: string, origin: string, now = Date.now()) {
  if (challenge.origin !== origin || !Number.isFinite(Date.parse(challenge.expires_at)) || Date.parse(challenge.expires_at) <= now) throw new PoolActionError('Wallet authorization expired or belongs to another site. Please try again.');
  if (challenge.payload_hash !== await intentHash(intent)) throw new PoolActionError('Wallet authorization does not match this action');
  const storedIntent: PoolIntent = {action: challenge.action, pool_id: challenge.pool_id, wallet_address: challenge.wallet_address, payload: challenge.payload};
  if (challenge.payload_hash !== await intentHash(storedIntent)) throw new PoolActionError('Invalid stored authorization');
  if (!algosdk.isValidAddress(authorizedAddress) || typeof signedProof !== 'string' || signedProof.length > 16384) throw new PoolActionError('Invalid wallet signature');
  try {
    const signed = algosdk.decodeSignedTransaction(Uint8Array.from(atob(signedProof), c => c.charCodeAt(0)));
    const expected = proofTransaction(challenge);
    if (!equalBytes(algosdk.encodeUnsignedTransaction(signed.txn), algosdk.encodeUnsignedTransaction(expected))) throw new Error('Transaction changed');
    if (signed.lsig || (!!signed.sig === !!signed.msig)) throw new Error('Unsupported authorization');
    const signer = signed.sgnr ? algosdk.encodeAddress(signed.sgnr) : challenge.wallet_address;
    if (signer !== authorizedAddress || (signed.sgnr && signer === challenge.wallet_address)) throw new Error('Signing authority changed');
    const message = new Uint8Array(expected.bytesToSign());
    const publicKey = algosdk.decodeAddress(authorizedAddress).publicKey;
    let valid = false;
    if (signed.msig) {
      valid = algosdk.verifyMultisig(message, signed.msig, publicKey);
    } else if (signed.sig?.length === 64) {
      const key = await crypto.subtle.importKey('raw', new Uint8Array(publicKey), 'Ed25519', false, ['verify']);
      valid = await crypto.subtle.verify('Ed25519', key, new Uint8Array(signed.sig), message);
    }
    if (!valid) throw new Error('Invalid signature');
  } catch {
    throw new PoolActionError('Wallet signature could not be verified. Please sign this action again.');
  }
}
