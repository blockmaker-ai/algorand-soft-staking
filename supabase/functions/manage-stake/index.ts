// Supabase Edge Function: manage-stake
// Server-side staking with on-chain balance validation
// Prevents fake stakes by verifying wallet holdings before writing to DB

import { createClient } from 'npm:@supabase/supabase-js@2';

// Algorand Indexer endpoints - Primary: Nodely, Fallback: Algonode
const NODELY_API_KEY = Deno.env.get('NODELY_API_KEY') || ''  // Set NODELY_API_KEY env var;
const NODELY_INDEXER = 'https://mainnet-idx.4160.nodely.io';
const ALGONODE_INDEXER = 'https://mainnet-idx.algonode.cloud';

let currentProvider = 'nodely';
let INDEXER_URL = NODELY_INDEXER;

const getFetchOptions = () => {
  if (currentProvider === 'nodely') {
    return { headers: { 'X-Algo-API-Token': NODELY_API_KEY } };
  }
  return {};
};

async function fetchWithFallback(url: string): Promise<Response> {
  try {
    const response = await fetch(url, getFetchOptions());
    if (!response.ok && response.status >= 500) {
      throw new Error(`Server error: ${response.status}`);
    }
    return response;
  } catch (error) {
    if (currentProvider === 'nodely') {
      console.warn('Nodely failed, switching to Algonode fallback');
      currentProvider = 'algonode';
      INDEXER_URL = ALGONODE_INDEXER;
      const fallbackUrl = url.replace(NODELY_INDEXER, ALGONODE_INDEXER);
      return fetch(fallbackUrl);
    }
    throw error;
  }
}

// CORS setup — match get-merkle-proof pattern
const ALLOWED_ORIGINS = [
  'http://localhost:3000',
  'http://localhost:5173',
  'http://127.0.0.1:3000',
  'http://127.0.0.1:5173',
  'https://your-domain.com',
  Deno.env.get('FRONTEND_URL') || ''
].filter(Boolean);

const getCorsHeaders = (origin: string | null) => {
  const allowedOrigin = origin && ALLOWED_ORIGINS.some(allowed =>
    origin === allowed || origin.endsWith('.vercel.app') || origin.endsWith('.netlify.app')
  ) ? origin : ALLOWED_ORIGINS[0];

  return {
    'Access-Control-Allow-Origin': allowedOrigin,
    'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type',
    'Access-Control-Allow-Credentials': 'true',
  };
};

// ── On-chain balance helpers (reused from verify-stakes) ──────────────

async function getTokenBalance(walletAddress: string, assetId: string): Promise<number> {
  if (assetId === '0' || assetId === 'ALGO') {
    const response = await fetchWithFallback(`${INDEXER_URL}/v2/accounts/${walletAddress}`);
    if (!response.ok) {
      if (response.status === 404) return 0;
      throw new Error(`Account lookup failed: ${response.status}`);
    }
    const data = await response.json();
    return (data.account?.amount || 0) / 1_000_000;
  }

  const response = await fetchWithFallback(
    `${INDEXER_URL}/v2/accounts/${walletAddress}/assets?asset-id=${assetId}`
  );
  if (!response.ok) {
    if (response.status === 404) return 0;
    throw new Error(`Asset lookup failed: ${response.status}`);
  }
  const data = await response.json();
  const assets = data.assets || [];
  if (assets.length === 0) return 0;
  const asset = assets.find((a: any) => String(a['asset-id']) === assetId);
  return asset ? Number(asset.amount || 0) : 0;
}

async function getTokenDecimals(assetId: string): Promise<number> {
  if (assetId === '0' || assetId === 'ALGO') return 6;
  const response = await fetchWithFallback(`${INDEXER_URL}/v2/assets/${assetId}`);
  if (!response.ok) return 6;
  const data = await response.json();
  return data.asset?.params?.decimals ?? 6;
}

// Fast indexed NFT counting (O(1) lookups against pre-indexed set)
async function countNFTsFromIndexedSet(
  walletAddress: string,
  indexedAssetIds: number[]
): Promise<number> {
  const indexedSet = new Set(indexedAssetIds);
  let count = 0;
  let nextToken: string | null = null;

  do {
    const url = new URL(`${INDEXER_URL}/v2/accounts/${walletAddress}/assets`);
    if (nextToken) url.searchParams.set('next', nextToken);
    url.searchParams.set('limit', '1000');

    const response = await fetchWithFallback(url.toString());
    if (!response.ok) {
      if (response.status === 404) return 0;
      throw new Error(`Wallet assets lookup failed: ${response.status}`);
    }

    const data = await response.json();
    const assets = data.assets || [];
    nextToken = data['next-token'] || null;

    for (const asset of assets) {
      if (asset.amount > 0 && indexedSet.has(asset['asset-id'])) {
        count++;
      }
    }
  } while (nextToken);

  return count;
}

// Slower NFT count using creator's created assets (fallback for non-indexed)
async function countNFTsFromCreator(
  walletAddress: string,
  creatorAddress: string
): Promise<number> {
  const createdAssetsResponse = await fetchWithFallback(
    `${INDEXER_URL}/v2/accounts/${creatorAddress}/created-assets?limit=1000`
  );
  if (!createdAssetsResponse.ok) {
    throw new Error(`Created assets lookup failed: ${createdAssetsResponse.status}`);
  }

  const createdData = await createdAssetsResponse.json();
  const createdAssets = createdData.assets || [];

  const walletResponse = await fetchWithFallback(
    `${INDEXER_URL}/v2/accounts/${walletAddress}/assets?limit=1000`
  );
  if (!walletResponse.ok) {
    if (walletResponse.status === 404) return 0;
    throw new Error(`Wallet assets lookup failed: ${walletResponse.status}`);
  }

  const walletData = await walletResponse.json();
  const walletAssets = walletData.assets || [];

  const createdAssetIds = new Set(createdAssets.map((a: any) => a.index));
  let count = 0;
  for (const asset of walletAssets) {
    if (asset.amount > 0 && createdAssetIds.has(asset['asset-id'])) {
      count++;
    }
  }
  return count;
}

// ── Main handler ──────────────────────────────────────────────────────

Deno.serve(async (req) => {
  const origin = req.headers.get('origin');
  const corsHeaders = getCorsHeaders(origin);

  if (req.method === 'OPTIONS') {
    return new Response('ok', { headers: corsHeaders });
  }

  if (req.method !== 'POST') {
    return new Response(
      JSON.stringify({ error: 'Method not allowed' }),
      { status: 405, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
    );
  }

  try {
    const { action, pool_id, wallet_address, amount } = await req.json();

    // ── Input validation ────────────────────────────────────────────
    if (!action || !['stake', 'unstake'].includes(action)) {
      return new Response(
        JSON.stringify({ error: 'Invalid action. Must be "stake" or "unstake".' }),
        { status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
      );
    }

    const uuidRegex = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
    if (!pool_id || !uuidRegex.test(pool_id)) {
      return new Response(
        JSON.stringify({ error: 'Invalid pool_id format' }),
        { status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
      );
    }

    if (!wallet_address || wallet_address.length !== 58 || !/^[A-Z2-7]+$/.test(wallet_address)) {
      return new Response(
        JSON.stringify({ error: 'Invalid Algorand address format' }),
        { status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
      );
    }

    const numericAmount = Number(amount);
    if (!Number.isFinite(numericAmount) || numericAmount <= 0) {
      return new Response(
        JSON.stringify({ error: 'Invalid amount. Must be a positive finite number.' }),
        { status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
      );
    }
    if (numericAmount > Number.MAX_SAFE_INTEGER) {
      return new Response(
        JSON.stringify({ error: 'Amount exceeds maximum allowed value.' }),
        { status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
      );
    }

    // ── Supabase service-role client — created early for rate limit check ──
    const supabaseUrl = Deno.env.get('SUPABASE_URL')!;
    const supabaseServiceKey = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!;
    const supabase = createClient(supabaseUrl, supabaseServiceKey);

    // ── Server-side rate limiting (10 operations per 60s per wallet) ────
    const rateLimitWindow = new Date(Date.now() - 60_000).toISOString();
    const { data: recentOps } = await supabase
      .from('user_stakes')
      .select('id')
      .eq('wallet_address', wallet_address)
      .gte('updated_at', rateLimitWindow);

    if (recentOps && recentOps.length >= 10) {
      return new Response(
        JSON.stringify({ error: 'Too many stake operations. Please wait a moment and try again.' }),
        { status: 429, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
      );
    }

    // ── Fetch pool data ─────────────────────────────────────────────
    const { data: pool, error: poolError } = await supabase
      .from('pools')
      .select('id, pool_name, pool_type, status, end_date, staking_token_id, lp_token_id, nft_collection_id, min_stake, max_stake, staking_token_decimals, staking_token_symbol, lp_token_pair_name')
      .eq('id', pool_id)
      .single();

    if (poolError || !pool) {
      return new Response(
        JSON.stringify({ error: 'Pool not found' }),
        { status: 404, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
      );
    }

    const isNFTPool = pool.pool_type === 'nft staking';
    const isLPPool = pool.pool_type === 'lp staking';

    // ── NFT amounts must be whole numbers ───────────────────────────
    if (isNFTPool && !Number.isInteger(numericAmount)) {
      return new Response(
        JSON.stringify({ error: 'NFT stake amounts must be whole numbers.' }),
        { status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
      );
    }

    // ── Fetch user's current active stake ───────────────────────────
    const { data: existingStake, error: stakeError } = await supabase
      .from('user_stakes')
      .select('id, amount_staked, is_active')
      .eq('pool_id', pool_id)
      .eq('wallet_address', wallet_address)
      .eq('is_active', true)
      .maybeSingle();

    if (stakeError) {
      console.error('Error fetching existing stake:', stakeError);
      return new Response(
        JSON.stringify({ error: 'Failed to fetch existing stake' }),
        { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
      );
    }

    const currentStake = existingStake ? parseFloat(existingStake.amount_staked) || 0 : 0;

    // ════════════════════════════════════════════════════════════════
    // UNSTAKE FLOW
    // ════════════════════════════════════════════════════════════════
    if (action === 'unstake') {
      if (!existingStake) {
        return new Response(
          JSON.stringify({ error: 'No active stake found' }),
          { status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
        );
      }

      if (numericAmount > currentStake) {
        return new Response(
          JSON.stringify({ error: `Cannot unstake ${numericAmount}. You only have ${currentStake} staked.` }),
          { status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
        );
      }

      const newStakeAmount = currentStake - numericAmount;

      if (newStakeAmount <= 0) {
        // Full unstake — mark inactive
        const { error: updateError } = await supabase
          .from('user_stakes')
          .update({
            is_active: false,
            unstaked_at: new Date().toISOString(),
            amount_staked: '0',
            updated_at: new Date().toISOString()
          })
          .eq('id', existingStake.id);

        if (updateError) throw updateError;

        // Clear unpublished pending epochs — rewards are forfeited on full unstake
        // These show as false "pending" amounts for users who have already left the pool
        const { error: clearPendingError } = await supabase
          .from('merkle_epoch_claims')
          .delete()
          .eq('pool_id', pool_id)
          .eq('user_address', wallet_address)
          .eq('is_published', false);

        if (clearPendingError) {
          console.error('Warning: could not clear pending epochs on unstake:', clearPendingError);
          // Non-fatal — stake was already marked inactive
        }
      } else {
        // Partial unstake
        const { error: updateError } = await supabase
          .from('user_stakes')
          .update({
            amount_staked: newStakeAmount.toString(),
            updated_at: new Date().toISOString()
          })
          .eq('id', existingStake.id);

        if (updateError) throw updateError;
      }

      // Fetch updated pool totals
      const { totalStaked, stakerCount } = await getPoolTotals(supabase, pool_id);

      return new Response(
        JSON.stringify({
          success: true,
          newStakeAmount: newStakeAmount <= 0 ? 0 : newStakeAmount,
          totalPoolStaked: totalStaked,
          stakerCount
        }),
        { status: 200, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
      );
    }

    // ════════════════════════════════════════════════════════════════
    // STAKE FLOW — with on-chain balance verification
    // ════════════════════════════════════════════════════════════════

    // ── Pool must be active and not past its end date ────────────
    const poolHasEnded = pool.end_date
      ? new Date() > new Date(pool.end_date.endsWith('Z') ? pool.end_date : pool.end_date + 'Z')
      : false;

    if (pool.status !== 'active' || poolHasEnded) {
      return new Response(
        JSON.stringify({ error: poolHasEnded ? 'Pool has ended and is no longer accepting new stakes' : `Pool is not accepting new stakes (status: ${pool.status})` }),
        { status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
      );
    }

    if (isNFTPool) {
      // ── NFT balance check ───────────────────────────────────────
      if (!pool.nft_collection_id) {
        return new Response(
          JSON.stringify({ error: 'NFT collection not configured for this pool' }),
          { status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
        );
      }

      const { data: collection, error: collError } = await supabase
        .from('nft_collections')
        .select('id, name, creator_address, is_indexed, indexed_asset_ids')
        .eq('id', pool.nft_collection_id)
        .single();

      if (collError || !collection) {
        return new Response(
          JSON.stringify({ error: 'NFT collection not found' }),
          { status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
        );
      }

      // Count NFTs on-chain — use indexed fast path if available
      let nftCount: number;
      if (collection.is_indexed && collection.indexed_asset_ids?.length > 0) {
        console.log(`Using indexed lookup (${collection.indexed_asset_ids.length} assets) for ${collection.name}`);
        nftCount = await countNFTsFromIndexedSet(wallet_address, collection.indexed_asset_ids);
      } else {
        console.log(`Using creator lookup for ${collection.name}`);
        nftCount = await countNFTsFromCreator(wallet_address, collection.creator_address);
      }

      const availableToStake = nftCount - currentStake;
      console.log(`NFT check: owns=${nftCount}, staked=${currentStake}, available=${availableToStake}, requested=${numericAmount}`);

      if (availableToStake < numericAmount) {
        return new Response(
          JSON.stringify({
            error: `Insufficient NFTs. You have ${availableToStake} available to stake (${nftCount} owned, ${currentStake} already staked)`
          }),
          { status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
        );
      }

      // Min/max validation
      const minStake = Number(pool.min_stake) || 0;
      const maxStake = Number(pool.max_stake) || 0;
      if (minStake > 0 && numericAmount < minStake) {
        return new Response(
          JSON.stringify({ error: `Must stake at least ${minStake} NFTs` }),
          { status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
        );
      }
      if (maxStake > 0 && (currentStake + numericAmount) > maxStake) {
        return new Response(
          JSON.stringify({ error: `Cannot exceed ${maxStake} NFTs staked. You have ${currentStake} already staked.` }),
          { status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
        );
      }

    } else {
      // ── Token / LP balance check ────────────────────────────────
      const tokenId = isLPPool ? pool.lp_token_id : pool.staking_token_id;
      if (!tokenId) {
        return new Response(
          JSON.stringify({ error: 'No staking token configured for this pool' }),
          { status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
        );
      }

      // Use pool's stored decimals, falling back to indexer lookup
      // Critical: use ?? not || because 0 decimals is valid (e.g. DDAO)
      let decimals = pool.staking_token_decimals ?? null;
      if (decimals === null) {
        decimals = await getTokenDecimals(String(tokenId));
      }

      const rawBalance = await getTokenBalance(wallet_address, String(tokenId));
      const walletBalance = rawBalance / Math.pow(10, decimals);

      // Check how much of this token the wallet has committed to OTHER active pools
      // LP pools are excluded — their tokens are unique per pair so no overlap possible
      let committedElsewhere = 0;
      if (!isLPPool) {
        const { data: otherPools } = await supabase
          .from('pools')
          .select('id')
          .eq('staking_token_id', pool.staking_token_id)
          .neq('id', pool_id);

        if (otherPools && otherPools.length > 0) {
          const otherPoolIds = otherPools.map((p: any) => p.id);
          const { data: otherStakes } = await supabase
            .from('user_stakes')
            .select('amount_staked')
            .eq('wallet_address', wallet_address)
            .eq('is_active', true)
            .in('pool_id', otherPoolIds);

          committedElsewhere = (otherStakes || []).reduce(
            (sum: number, s: any) => sum + parseFloat(s.amount_staked || '0'), 0
          );
        }
      }

      const availableBalance = walletBalance - currentStake - committedElsewhere;

      console.log(`Token check: wallet=${walletBalance}, staked=${currentStake}, committedElsewhere=${committedElsewhere}, available=${availableBalance}, requested=${numericAmount}`);

      // Use small tolerance for floating point comparison (same as client)
      const PRECISION_TOLERANCE = 0.01;
      if (availableBalance + PRECISION_TOLERANCE < numericAmount) {
        const tokenSymbol = isLPPool ? (pool.lp_token_pair_name || 'LP') : (pool.staking_token_symbol || 'tokens');
        const committedMsg = committedElsewhere > 0
          ? ` (${committedElsewhere.toFixed(6)} committed to other pools)`
          : '';
        return new Response(
          JSON.stringify({
            error: `Insufficient balance. You have ${availableBalance.toFixed(6)} ${tokenSymbol} available (${walletBalance.toFixed(6)} in wallet, ${currentStake.toFixed(6)} already staked here${committedMsg})`
          }),
          { status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
        );
      }

      // Min/max validation
      const minStake = Number(pool.min_stake) || 0;
      const maxStake = Number(pool.max_stake) || 0;
      if (minStake > 0 && numericAmount < minStake) {
        const tokenSymbol = isLPPool ? (pool.lp_token_pair_name || 'LP') : (pool.staking_token_symbol || 'tokens');
        return new Response(
          JSON.stringify({ error: `Must stake at least ${minStake} ${tokenSymbol}` }),
          { status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
        );
      }
      if (maxStake > 0 && (currentStake + numericAmount) > maxStake) {
        const tokenSymbol = isLPPool ? (pool.lp_token_pair_name || 'LP') : (pool.staking_token_symbol || 'tokens');
        return new Response(
          JSON.stringify({ error: `Cannot exceed ${maxStake} ${tokenSymbol} staked. You have ${currentStake} already staked.` }),
          { status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
        );
      }
    }

    // ── Balance verified — write to DB ────────────────────────────
    if (existingStake) {
      const newAmount = currentStake + numericAmount;
      const { error: updateError } = await supabase
        .from('user_stakes')
        .update({
          amount_staked: newAmount.toString(),
          updated_at: new Date().toISOString()
        })
        .eq('id', existingStake.id);

      if (updateError) throw updateError;
    } else {
      const { error: insertError } = await supabase
        .from('user_stakes')
        .insert([{
          pool_id,
          wallet_address,
          amount_staked: numericAmount.toString(),
          staked_at: new Date().toISOString(),
          is_active: true,
          stake_tx_id: null
        }]);

      if (insertError) throw insertError;
    }

    // Fetch updated pool totals
    const { totalStaked, stakerCount } = await getPoolTotals(supabase, pool_id);

    return new Response(
      JSON.stringify({
        success: true,
        newStakeAmount: (currentStake + numericAmount),
        totalPoolStaked: totalStaked,
        stakerCount
      }),
      { status: 200, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
    );

  } catch (error) {
    console.error('manage-stake error:', error);
    return new Response(
      JSON.stringify({ error: String(error?.message || error) }),
      { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
    );
  }
});

// ── Helper: get pool totals after write ────────────────────────────────

async function getPoolTotals(supabase: any, poolId: string) {
  const { data: stakes } = await supabase
    .from('user_stakes')
    .select('amount_staked')
    .eq('pool_id', poolId)
    .eq('is_active', true);

  const totalStaked = (stakes || []).reduce(
    (sum: number, s: any) => sum + (parseFloat(s.amount_staked) || 0), 0
  );
  const stakerCount = (stakes || []).length;

  return { totalStaked, stakerCount };
}
