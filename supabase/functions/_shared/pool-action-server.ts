import algosdk from 'npm:algosdk@2.7.0';
import {type PoolChallenge, type PoolIntent} from './pool-action-format.ts';
import {PoolActionError, verifyPoolProof} from './pool-action-proof.ts';

import {provider,networkIdentity} from './server-config.ts';
const ORIGINS = new Set((Deno.env.get('FRONTEND_ORIGINS') || '').split(',').map(value=>value.trim()).filter(Boolean));

export function actionOrigin(req: Request): string {
  const origin = req.headers.get('origin') || '';
  if (!origin || !ORIGINS.has(origin)) throw new PoolActionError('This site is not authorized for pool actions', 403);
  return origin;
}

export function actionHeaders(req: Request): Record<string, string> {
  const origin = req.headers.get('origin') || '';
  return {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': ORIGINS.has(origin) ? origin : '', 'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type', 'Access-Control-Allow-Methods': 'POST, OPTIONS', 'Vary': 'Origin'};
}

export async function readAlgod(path: string, allowMissing = false): Promise<any> {
  const endpoints=[provider('ALGOD')];
  const expected=networkIdentity();
  const network=await fetch(endpoints[0].url+'/v2/transactions/params',{headers:endpoints[0].headers,signal:AbortSignal.timeout(10000)});
  if(!network.ok)throw new PoolActionError('Chain identity could not be checked',503);
  const params=await network.json();
  if(params['genesis-id']!==expected.id || params['genesis-hash']!==expected.hash)throw new PoolActionError('Unexpected chain network',503);
  for (const endpoint of endpoints) {
    try {
      const response = await fetch(endpoint.url + path, {headers: endpoint.headers, signal: AbortSignal.timeout(10000)});
      if (response.ok) return await response.json();
      if (response.status === 404 && allowMissing) { await response.body?.cancel(); return null; }
      await response.body?.cancel();
    } catch { /* Try the other provider; never assume an unavailable account is unrekeyed. */ }
  }
  throw new PoolActionError('Wallet authority could not be checked. Please try again shortly.', 503);
}

export async function authorizePoolAction(req: Request, db: any, intent: PoolIntent, challengeId: unknown, signedProof: unknown): Promise<{challenge: PoolChallenge; account: any}> {
  const origin = actionOrigin(req);
  if (typeof challengeId !== 'string' || !/^[0-9a-f-]{36}$/i.test(challengeId) || typeof signedProof !== 'string') throw new PoolActionError('Please refresh the page and authorize this action with your wallet');
  if (!algosdk.isValidAddress(intent.wallet_address)) throw new PoolActionError('Invalid wallet address', 400);
  const {data: challenge, error} = await db.from('pool_action_challenges').select('*').eq('id', challengeId).maybeSingle();
  if (error) throw new PoolActionError('Wallet authorization service is unavailable', 503);
  if (!challenge) throw new PoolActionError('Wallet authorization was not found. Please try again.');
  const account = await readAlgod(`/v2/accounts/${intent.wallet_address}${intent.action === 'stake' ? '' : '?exclude=all'}`);
  if (account.address !== intent.wallet_address) throw new PoolActionError('Unexpected wallet authority response', 503);
  await verifyPoolProof(challenge, intent, signedProof, account['auth-addr'] || intent.wallet_address, origin);
  return {challenge, account};
}

export function actionFailure(error: unknown, headers: Record<string, string>): Response {
  const status = error instanceof PoolActionError ? error.status : error instanceof SyntaxError ? 400 : 500;
  return new Response(JSON.stringify({error: error instanceof PoolActionError ? error.message : 'Pool action could not be completed'}), {status, headers});
}

export async function actionBody(req: Request): Promise<any> {
  const text = await req.text();
  if (text.length > 24000) throw new PoolActionError('Action payload is too large', 400);
  const body = JSON.parse(text);
  if (!body || typeof body !== 'object' || Array.isArray(body)) throw new PoolActionError('Invalid action payload', 400);
  return body;
}
