import algosdk from 'algosdk';
import {walletTransactionSignatures} from './wallet-transaction-signatures.js';

const {matchSignedTransactions} = walletTransactionSignatures(algosdk);

// Invoke from a click handler after preparing the review. Nothing is awaited
// before opening the wallet, and cancelled/late signatures cannot reach submission.
export function requestPreparedWalletSignatures({transactions, receivedAt, signTransactions, signal,
  timeoutMs = 120000, now = Date.now()}) {
  return new Promise((resolve, reject) => {
    let settled = false, timer;
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener('abort', cancel);
      if (error) reject(error); else resolve(value);
    };
    const cancel = () => finish(new Error('Wallet request cancelled. A late approval will not be submitted.'));
    try {
      if (signal?.aborted) {cancel(); return;}
      if (!Number.isFinite(receivedAt) || now < receivedAt || now - receivedAt > 60000) throw new Error('This check is out of date. Refresh before approving.');
      signal?.addEventListener('abort', cancel, {once: true});
      timer = setTimeout(() => finish(new Error('the wallet did not return an approval within two minutes. Reconnect your wallet and try again.')), timeoutMs);
      const pending = signTransactions(transactions.map(txn => algosdk.encodeUnsignedTransaction(txn)));
      Promise.resolve(pending).then(signed => {
        if (settled) return;
        try {finish(null, matchSignedTransactions(transactions, signed));} catch (error) {finish(error);}
      }, error => finish(error));
    } catch (error) {finish(error);}
  });
}
