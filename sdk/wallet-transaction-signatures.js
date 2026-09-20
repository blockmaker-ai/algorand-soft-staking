// The browser and relays use the same exact-transaction check. Relays also
// verify the signature against the account's current on-chain authority.
export function walletTransactionSignatures(algosdk) {
  function matchSignedTransactions(expected, signed) {
    if (!Array.isArray(signed) || signed.length !== expected.length || expected.length < 1 || expected.length > 16) {
      throw new Error('The wallet did not sign the complete transaction group');
    }
    signed.forEach((bytes, index) => {
      if (!(bytes instanceof Uint8Array)) throw new Error('Missing wallet signature');
      const decoded = algosdk.decodeSignedTransaction(bytes);
      if ((!!decoded.sig === !!decoded.msig) || decoded.lsig) throw new Error('A wallet signature is required');
      const actual = algosdk.encodeUnsignedTransaction(decoded.txn);
      const wanted = algosdk.encodeUnsignedTransaction(expected[index]);
      if (actual.length !== wanted.length || actual.some((byte, i) => byte !== wanted[i])) {
        throw new Error('The signed transaction differs from the reviewed transaction');
      }
    });
    return signed;
  }

  async function verifyWalletSignatures(expected, signed, authority) {
    matchSignedTransactions(expected, signed);
    const publicKey = algosdk.decodeAddress(authority).publicKey;
    for (const bytes of signed) {
      const decoded = algosdk.decodeSignedTransaction(bytes);
      if ((decoded.sgnr?.toString() || decoded.txn.sender.toString()) !== authority) {
        throw new Error('Wallet signing authority changed. Reconnect and prepare again.');
      }
      const message = new Uint8Array(decoded.txn.bytesToSign());
      let valid = false;
      if (decoded.msig) valid = algosdk.verifyMultisig(message, decoded.msig, publicKey);
      else if (decoded.sig?.length === 64) {
        const key = await crypto.subtle.importKey('raw', new Uint8Array(publicKey), 'Ed25519', false, ['verify']);
        valid = await crypto.subtle.verify('Ed25519', key, new Uint8Array(decoded.sig), message);
      }
      if (!valid) throw new Error('Wallet signature could not be verified');
    }
    return signed;
  }
  return {matchSignedTransactions, verifyWalletSignatures};
}
