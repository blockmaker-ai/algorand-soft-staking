# Wallet integration

Use your own public pool configuration and Supabase function URL. Keep all service credentials and publisher keys on the server. The helpers use `algosdk` 3.5.2; stake-proof encoding is also tested against the server’s pinned 2.7.0 verifier.

## Register or reduce a stake

1. POST to `pool-action-challenge` with `{action, pool_id, wallet_address, payload: {amount: "10"}}`, using the exact configured frontend origin. `action` is `stake` or `unstake`; amount is a decimal string in whole-token units, or a whole NFT count.
2. Verify the returned challenge matches that wallet, pool, amount, action and origin. Display the action to the user.
3. Use `poolActionProof(challenge)` from `sdk/pool-actions.ts` and ask the wallet to sign it.
4. POST to `manage-stake` with `{action, pool_id, wallet_address, amount: "10", challenge_id, signed_proof}`. `signed_proof` is the base64 signed transaction.

The proof is an expired round-one, zero-fee self-payment. **Never broadcast it.** Its fixed genesis fields are an off-chain message format; the server checks account authority and holdings on the configured network. The note binds the origin, wallet, action, amount, pool, payload hash, challenge and expiry. The server supports ordinary, rekeyed and threshold multisignature accounts; logic-signature proofs are not supported.

A nonce and its stake mutation commit together. An identical retry returns the saved result. A changed stake, expired challenge or insufficient holdings fails without consuming a new entitlement. Returned stake amounts are strings, preserving precision.

## Read rewards

GET `funded-pools?pool_id=UUID&wallet=ADDRESS` returns verified vault balances, the requested wallet’s cumulative `allocated`, `paid` and `claimable`, current-period metadata and display totals. Omit `wallet` for a pool-only response.

Read token values as `BigInt`; divide only when formatting. Do not use the vault’s raw balance as a new reward budget. Use `display.payingNowAtomic` for the current month’s full funded total and `display.nextMonthAtomic` for uncommitted funding.

`period.eligible_stake` and `eligible_stake_at` are aggregate verified earning weights. They account for holdings and reward caps. For APR, use a current stake observation and current prices in a common currency; suppress the estimate when either is unavailable or stale. `annualRewardRate` annualises the actual funded period without assuming compounding or future funding.

## Prepare and sign a claim

GET `funded-pools?pool_id=UUID&wallet=ADDRESS&prepare=true` returns an unsigned quote. Pin your pool’s app ID, reward asset and network identity in your application, then verify it:

```typescript
import {verifyClaimQuote} from './sdk/claims'
import {requestPreparedWalletSignatures} from './sdk/preparedWalletSigning'

const transactions = verifyClaimQuote(quote, trustedDeployment, activeAddress)
const receivedAt = Date.now()
// Show the amount, network fee and any required reward-ASA opt-in.
// Later, directly inside the user's claim click handler:
const signed = await requestPreparedWalletSignatures({
  transactions, receivedAt, signTransactions, signal: abortController.signal,
})
```

Prepare before the click so the wallet request starts synchronously from that click. `signTransactions` accepts unsigned byte arrays and returns signed byte arrays; adapt your wallet connector to that interface. Cancellation, expired reviews, changed signed transactions and late wallet responses are rejected.

The verified group contains only an optional zero-amount reward-ASA opt-in and `claimRewards()`. It cannot include a payment, close-out, rekey or another application. The wallet needs its transaction fees and any ASA opt-in minimum balance. There is no platform fee.

Before broadcasting the signed bytes, persist the claim transaction ID and last valid round locally. Submit that same group to your configured Algod node. Show success only after confirming that exact transaction; a dropped HTTP response is not proof of failure. On refresh, check the stored transaction first. Do not start a different group while it might still confirm. The contract’s cumulative credit accounting prevents the same earned amount from being paid twice.

Claims pay already earned credits. They do not reduce the month’s displayed total, and they do not require the staked asset to remain in the wallet after the earning window.
