// Shared by the browser and Edge Functions. No server secrets or SDK imports.
export const MAINNET_GENESIS_ID = 'mainnet-v1.0';
export const MAINNET_GENESIS_HASH = 'wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=';
export type PoolAction = 'stake' | 'unstake';
export type PoolIntent = {action: PoolAction; pool_id: string; wallet_address: string; payload: Record<string, unknown>};
export type PoolChallenge = PoolIntent & {id: string; origin: string; payload_hash: string; expires_at: string; consumed_at?: string | null; result?: unknown};

export function canonicalJSON(value: unknown): string {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return JSON.stringify(value);
  if (typeof value === 'number' && Number.isFinite(value)) return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJSON).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonicalJSON((value as Record<string, unknown>)[key])}`).join(',')}}`;
  }
  throw new Error('Invalid action payload');
}

export function normalizeAmount(value: unknown): string {
  const text = String(value);
  if (!/^(0|[1-9]\d*)(\.\d{1,19})?$/.test(text) || Number(text) <= 0 || Number(text) > Number.MAX_SAFE_INTEGER) throw new Error('Invalid stake amount');
  return text.includes('.') ? text.replace(/0+$/, '').replace(/\.$/, '') : text;
}

export function poolIntent(action: PoolAction, pool_id: string, wallet_address: string, payload: Record<string, unknown>): PoolIntent {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw new Error('Invalid action payload');
  if (!['stake', 'unstake'].includes(action)) throw new Error('Unsupported pool action');
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(pool_id)) throw new Error('Invalid pool ID');
  if (!/^[A-Z2-7]{58}$/.test(wallet_address)) throw new Error('Invalid wallet address');
  const normalized = {amount: normalizeAmount(payload.amount)};
  if (canonicalJSON(normalized).length > 8192) throw new Error('Action payload is too large');
  return {action, pool_id: pool_id.toLowerCase(), wallet_address, payload: normalized};
}

export async function intentHash(intent: PoolIntent): Promise<string> {
  const bytes = new TextEncoder().encode(canonicalJSON(intent));
  const hash = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, '0')).join('');
}

export function challengeNote(challenge: PoolChallenge): Uint8Array {
  return new TextEncoder().encode([
    'Soft staking: authorize one action. No transfer.',
    `Origin: ${challenge.origin}`, `Wallet: ${challenge.wallet_address}`,
    `Pool: ${challenge.pool_id}`, `Action: ${challenge.action}`,
    `Amount: ${challenge.payload.amount ?? 'pool settings'}`, `Payload: ${challenge.payload_hash}`,
    `Challenge: ${challenge.id}`, `Expires: ${new Date(challenge.expires_at).toISOString()}`,
  ].join('\n'));
}
