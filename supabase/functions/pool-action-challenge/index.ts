import {createClient} from 'npm:@supabase/supabase-js@2';
import algosdk from 'npm:algosdk@2.7.0';
import {intentHash, poolIntent, type PoolAction} from '../_shared/pool-action-format.ts';
import {actionBody, actionFailure, actionHeaders, actionOrigin} from '../_shared/pool-action-server.ts';
import {PoolActionError} from '../_shared/pool-action-proof.ts';

Deno.serve(async req => {
  const headers = actionHeaders(req);
  if (req.method === 'OPTIONS') return new Response('ok', {headers});
  if (req.method !== 'POST') return new Response(JSON.stringify({error: 'POST required'}), {status: 405, headers});
  try {
    const origin = actionOrigin(req);
    const body = await actionBody(req);
    let intent;
    try { intent = poolIntent(body.action as PoolAction, body.pool_id, body.wallet_address, body.payload); }
    catch (error) { throw new PoolActionError((error as Error).message, 400); }
    if (!algosdk.isValidAddress(intent.wallet_address)) throw new PoolActionError('Invalid wallet address', 400);
    const db = createClient(Deno.env.get('SUPABASE_URL')!, Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!);
    const {data, error} = await db.rpc('issue_pool_action_challenge', {p_action: intent.action, p_pool_id: intent.pool_id, p_wallet: intent.wallet_address, p_payload: intent.payload, p_payload_hash: await intentHash(intent), p_origin: origin});
    if (error) throw new PoolActionError(error.message, error.message.includes('Too many') ? 429 : 400);
    return new Response(JSON.stringify({challenge: data}), {headers});
  } catch (error) { return actionFailure(error, headers); }
});
