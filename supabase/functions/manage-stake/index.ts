// Wallet ownership, holdings and the database mutation are independently checked.
// Existing stakes and contracts are retained; only future actions need a challenge.
import {createClient} from 'npm:@supabase/supabase-js@2';
import {poolIntent} from '../_shared/pool-action-format.ts';
import {actionBody, actionFailure, actionHeaders, authorizePoolAction} from '../_shared/pool-action-server.ts';
import {PoolActionError} from '../_shared/pool-action-proof.ts';

export function tokenHoldings(raw: unknown, decimals: number): string {
  if (!Number.isInteger(decimals) || decimals < 0 || decimals > 19 || typeof raw !== 'number' || !Number.isSafeInteger(raw) || raw < 0) throw new PoolActionError('Wallet holdings could not be read precisely', 503);
  const amount = BigInt(raw), scale = 10n ** BigInt(decimals);
  if (decimals === 0) return String(amount);
  return `${amount / scale}.${String(amount % scale).padStart(decimals, '0')}`;
}

Deno.serve(async req => {
  const headers = actionHeaders(req);
  if (req.method === 'OPTIONS') return new Response('ok', {headers});
  if (req.method !== 'POST') return new Response(JSON.stringify({error: 'POST required'}), {status: 405, headers});
  try {
    const body = await actionBody(req);
    if (!['stake', 'unstake'].includes(body.action)) throw new PoolActionError('Invalid stake action', 400);
    let intent;
    try { intent = poolIntent(body.action, body.pool_id, body.wallet_address, {amount: body.amount}); }
    catch (error) { throw new PoolActionError(String((error as Error).message), 400); }
    const db = createClient(Deno.env.get('SUPABASE_URL')!, Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!);
    const {challenge, account} = await authorizePoolAction(req, db, intent, body.challenge_id, body.signed_proof);
    if (challenge.consumed_at && challenge.result) return new Response(JSON.stringify(challenge.result), {headers});
    const {data: pool, error: poolError} = await db.from('pools')
      .select('id,pool_type,hidden,status,funding_confirmed,end_date,staking_token_id,lp_token_id,staking_token_decimals,nft_collection_id')
      .eq('id', intent.pool_id).single();
    if (poolError || !pool) throw new PoolActionError('Pool not found', 404);
    if (intent.action === 'stake' && pool.hidden) throw new PoolActionError('Pool is not accepting stakes', 400);
    const {data: stake, error: stakeError} = await db.from('user_stakes').select('amount_staked')
      .eq('pool_id', intent.pool_id).eq('wallet_address', intent.wallet_address).eq('is_active', true).maybeSingle();
    if (stakeError) throw new PoolActionError('Your current stake could not be read. Please try again.', 503);
    let holdings: string | null = null;
    if (intent.action === 'stake') {
      if (!Array.isArray(account.assets) || !Number.isSafeInteger(account.round) || account.round <= 0) throw new PoolActionError('Wallet holdings are unavailable', 503);
      if (pool.pool_type === 'nft staking') {
        const {data: collection, error} = await db.from('nft_collections').select('is_indexed,indexed_asset_ids')
          .eq('id', pool.nft_collection_id).single();
        if (error || !collection?.is_indexed || !Array.isArray(collection.indexed_asset_ids) || !collection.indexed_asset_ids.length) throw new PoolActionError('This NFT collection needs to be indexed before staking', 503);
        const assetIds = new Set(collection.indexed_asset_ids.map(String));
        holdings = String(account.assets.filter((asset: any) => asset.amount > 0 && assetIds.has(String(asset['asset-id']))).length);
      } else {
        const token = pool.pool_type === 'lp staking' ? pool.lp_token_id : pool.staking_token_id;
        if (!token || pool.staking_token_decimals === null) throw new PoolActionError('Staking token configuration is incomplete', 503);
        const asset = account.assets.find((item: any) => String(item['asset-id']) === String(token));
        holdings = tokenHoldings(asset?.amount ?? 0, pool.staking_token_decimals);
      }
    }
    const {data: result, error} = await db.rpc('apply_stake_action', {
      p_challenge_id: challenge.id, p_payload_hash: challenge.payload_hash,
      p_expected_amount: stake?.amount_staked ?? '0', p_wallet_holdings: holdings,
      p_observed_round: intent.action === 'stake' ? account.round : null,
    });
    if (error) throw new PoolActionError(error.message, error.message.includes('changed while') ? 409 : 400);
    return new Response(JSON.stringify(result), {headers});
  } catch (error) { return actionFailure(error, headers); }
});
