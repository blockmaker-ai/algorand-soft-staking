import { serve } from 'https://deno.land/std@0.168.0/http/server.ts'
import { createClient } from 'https://esm.sh/@supabase/supabase-js@2'

// Algorand Indexer endpoints for balance verification at claim time
const INDEXER_URLS = [
  'https://mainnet-idx.4160.nodely.io',
  'https://mainnet-idx.algonode.cloud'
]

async function getTokenBalance(address: string, tokenId: string): Promise<number | null> {
  for (const baseUrl of INDEXER_URLS) {
    try {
      const headers: Record<string, string> = { 'Content-Type': 'application/json' }
      if (baseUrl.includes('nodely')) {
        headers['X-Algo-API-Token'] = Deno.env.get('NODELY_API_KEY') || ''  // Set NODELY_API_KEY env var
      }
      const resp = await fetch(`${baseUrl}/v2/accounts/${address}/assets?asset-id=${tokenId}`, { headers })
      if (!resp.ok) continue
      const data = await resp.json()
      if (data.assets && data.assets.length > 0) {
        // Find the specific asset by ID — don't assume it's always [0]
        const asset = data.assets.find((a: any) => String(a['asset-id']) === String(tokenId))
        return asset ? (asset.amount || 0) : 0
      }
      return 0
    } catch {
      continue
    }
  }
  return null
}

async function getNFTCount(address: string, indexedAssetIds: number[]): Promise<number> {
  // Count how many of the indexed NFT assets the wallet holds
  const indexedSet = new Set(indexedAssetIds)
  let count = 0
  let nextToken: string | null = null

  for (const baseUrl of INDEXER_URLS) {
    try {
      count = 0
      nextToken = null
      do {
        const pageParam = nextToken ? `&next=${nextToken}` : ''
        const headers: Record<string, string> = { 'Content-Type': 'application/json' }
        if (baseUrl.includes('nodely')) {
          headers['X-Algo-API-Token'] = Deno.env.get('NODELY_API_KEY') || ''  // Set NODELY_API_KEY env var
        }
        const resp = await fetch(`${baseUrl}/v2/accounts/${address}/assets?limit=100${pageParam}`, { headers })
        if (!resp.ok) break
        const data = await resp.json()
        for (const asset of (data.assets || [])) {
          if (asset.amount > 0 && indexedSet.has(asset['asset-id'])) {
            count++
          }
        }
        nextToken = data['next-token'] || null
      } while (nextToken)
      return count
    } catch {
      continue
    }
  }
  return count
}

// ✅ SECURITY: Restrict CORS to allowed origins
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

serve(async (req) => {
  const origin = req.headers.get('origin');
  const corsHeaders = getCorsHeaders(origin);

  if (req.method === 'OPTIONS') {
    return new Response('ok', { headers: corsHeaders })
  }

  try {
    const url = new URL(req.url)
    const address = url.searchParams.get('address')
    const pool_id = url.searchParams.get('pool_id')
    // When display_only=true the caller just wants to show pending/claimable amounts.
    // Skip the live balance check so a transient indexer blip cannot permanently
    // invalidate a stake just because the user loaded the Pools page.
    const displayOnly = url.searchParams.get('display_only') === 'true'

    if (!address || !pool_id) {
      return new Response(
        JSON.stringify({ error: 'Missing address or pool_id parameter' }),
        { status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
      )
    }

    // ✅ SECURITY: Validate Algorand address format
    if (address.length !== 58 || !/^[A-Z2-7]+$/.test(address)) {
      return new Response(
        JSON.stringify({ error: 'Invalid Algorand address format' }),
        { status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
      )
    }

    // ✅ SECURITY: Validate UUID format for pool_id
    const uuidRegex = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
    if (!uuidRegex.test(pool_id)) {
      return new Response(
        JSON.stringify({ error: 'Invalid pool_id format' }),
        { status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
      )
    }

    // Create Supabase client
    const supabaseUrl = Deno.env.get('SUPABASE_URL')!
    const supabaseKey = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!
    const supabase = createClient(supabaseUrl, supabaseKey)

    // Query the LATEST epoch for this user and pool
    const { data: claims, error } = await supabase
      .from('merkle_epoch_claims')
      .select('*')
      .eq('pool_id', pool_id)
      .eq('user_address', address)
      .order('epoch_id', { ascending: false })
      .limit(1)
      .single()

    if (error || !claims) {
      return new Response(
        JSON.stringify({ error: 'No rewards available' }),
        { status: 404, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
      )
    }

    // Get pool info for app_id and balance verification
    const { data: pool, error: poolError } = await supabase
      .from('pools')
      .select('contract_app_id, contract_version, pool_type, staking_token_id, lp_token_id, nft_collection_id, staking_token_decimals, end_date, status')
      .eq('id', pool_id)
      .single()

    if (poolError || !pool) {
      return new Response(
        JSON.stringify({ error: 'Pool not found' }),
        { status: 404, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
      )
    }

    // ✅ SECURITY: Verify wallet still holds the required tokens before returning proof
    // This prevents users from unstaking (moving tokens out) and then claiming rewards.
    // Skip when display_only=true — just showing pending amounts, not issuing a real claim.
    // Skip for ended pools — rewards were already earned and locked in the merkle tree;
    // stake status at claim time is irrelevant since the pool is no longer generating epochs.
    const poolEnded = pool.status === 'ended' || (pool.end_date && new Date(pool.end_date) < new Date())
    if (!displayOnly && !poolEnded) {
      const { data: activeStake } = await supabase
        .from('user_stakes')
        .select('amount_staked')
        .eq('pool_id', pool_id)
        .eq('wallet_address', address)
        .eq('is_active', true)
        .single()

      // If no active stake, check if there's an invalidated one — block proof if so
      if (!activeStake) {
        const { data: invalidatedStake } = await supabase
          .from('user_stakes')
          .select('id, invalidated_at')
          .eq('pool_id', pool_id)
          .eq('wallet_address', address)
          .eq('is_active', false)
          .not('invalidated_at', 'is', null)
          .limit(1)
          .single()

        if (invalidatedStake) {
          return new Response(
            JSON.stringify({
              error: 'Stake invalidated',
              message: 'Your stake was removed because your wallet no longer holds the required tokens. Any pending rewards have been forfeited.'
            }),
            { status: 403, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
          )
        }
      }

      if (activeStake && parseFloat(activeStake.amount_staked) > 0) {
        const stakedAmount = parseFloat(activeStake.amount_staked)
        let walletBalance = 0
        let balanceCheckPassed = true
        const isNFTPool = pool.pool_type === 'nft staking'
        const isLPPool = pool.pool_type === 'lp staking'

        if (isNFTPool) {
          // For NFT pools: check wallet still holds enough NFTs from the collection
          if (pool.nft_collection_id) {
            const { data: collection } = await supabase
              .from('nft_collections')
              .select('indexed_asset_ids')
              .eq('id', pool.nft_collection_id)
              .single()

            if (collection?.indexed_asset_ids) {
              walletBalance = await getNFTCount(address, collection.indexed_asset_ids)
            }
          }
          balanceCheckPassed = walletBalance >= stakedAmount
        } else {
          // For token/LP pools: check on-chain token balance
          const tokenId = isLPPool ? pool.lp_token_id : pool.staking_token_id
          if (tokenId) {
            const decimals = pool.staking_token_decimals ?? 6
            const rawBalance = await getTokenBalance(address, String(tokenId))
            if (rawBalance !== null) {
              walletBalance = rawBalance / Math.pow(10, decimals)
            } else {
              // If we can't fetch balance, allow the claim (don't block on indexer errors)
              console.warn(`Could not fetch balance for ${address} - allowing claim`)
              walletBalance = stakedAmount
            }
          }
          const PRECISION_TOLERANCE = 0.01
          balanceCheckPassed = walletBalance + PRECISION_TOLERANCE >= stakedAmount
        }

        if (!balanceCheckPassed) {
          console.warn(`⚠️ Claim blocked: ${address} staked ${stakedAmount} but wallet only has ${walletBalance} (pool: ${pool_id})`)

          // Invalidate the stake since they no longer hold enough tokens
          await supabase
            .from('user_stakes')
            .update({
              is_active: false,
              amount_staked: 0,
              invalidation_reason: `Balance check at claim time: wallet=${walletBalance}, staked=${stakedAmount}`,
              invalidated_at: new Date().toISOString()
            })
            .eq('pool_id', pool_id)
            .eq('wallet_address', address)
            .eq('is_active', true)

          return new Response(
            JSON.stringify({
              error: 'Insufficient balance',
              message: `Your wallet no longer holds enough tokens for this pool. You have ${walletBalance.toLocaleString()} but staked ${stakedAmount.toLocaleString()}. Your stake has been removed.`
            }),
            { status: 403, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
          )
        }
      }
    } // end !displayOnly balance check

    // Fetch the pool_id from the contract's global state
    // CRITICAL: Must match the pool_id stored in the smart contract
    // DO NOT use fallback values - wrong pool_id causes merkle root mismatches!
    let pool_id_uint: number | null = null

    try {
      const algodUrl = `https://mainnet-api.algonode.cloud/v2/applications/${pool.contract_app_id}`
      const algodResponse = await fetch(algodUrl)

      if (algodResponse.ok) {
        const appData = await algodResponse.json()
        const globalState = appData?.params?.['global-state'] || []

        for (const item of globalState) {
          const keyBytes = Uint8Array.from(atob(item.key), c => c.charCodeAt(0))
          const key = new TextDecoder().decode(keyBytes)

          if (key === 'pool_id') {
            pool_id_uint = item.value?.uint || null
            break
          }
        }
      }
    } catch (e) {
      console.error('Error fetching contract pool_id:', e)
    }

    // CRITICAL: Fail if we couldn't get pool_id from contract
    // Using wrong pool_id causes merkle root mismatches and claim failures
    if (pool_id_uint === null) {
      console.error('CRITICAL: Could not fetch pool_id from contract - cannot generate valid proof')
      return new Response(
        JSON.stringify({ error: 'Could not fetch pool_id from contract. Please try again.' }),
        { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
      )
    }

    // Check if epoch is published on-chain and root matches
    // Box name for epoch = encodeUint64(epoch_id)
    let is_published = false
    let on_chain_root: string | null = null
    const expected_root = claims.merkle_root || null

    try {
      // Encode epoch_id as 8-byte big-endian uint64
      const epochId = claims.epoch_id
      const epochBytes = new Uint8Array(8)
      let tempEpoch = BigInt(epochId)
      for (let i = 7; i >= 0; i--) {
        epochBytes[i] = Number(tempEpoch & BigInt(0xff))
        tempEpoch = tempEpoch >> BigInt(8)
      }
      const boxNameB64 = btoa(String.fromCharCode(...epochBytes))

      const boxUrl = `https://mainnet-api.algonode.cloud/v2/applications/${pool.contract_app_id}/box?name=b64:${boxNameB64}`
      const boxResponse = await fetch(boxUrl)

      if (boxResponse.ok) {
        const boxData = await boxResponse.json()
        // Box value contains the merkle root (32 bytes as base64)
        if (boxData.value) {
          // Decode base64 to bytes, then convert to hex
          const rootBytes = Uint8Array.from(atob(boxData.value), c => c.charCodeAt(0))
          on_chain_root = Array.from(rootBytes).map(b => b.toString(16).padStart(2, '0')).join('')

          // If we have an expected root, verify it matches
          if (expected_root) {
            is_published = on_chain_root.toLowerCase() === expected_root.toLowerCase()
            if (!is_published) {
              console.warn(`Root mismatch for epoch ${epochId}: on-chain=${on_chain_root}, expected=${expected_root}`)
            }
          } else {
            // No expected root stored in database - this means the epoch was generated
            // before we started storing roots. Mark as NOT published to be safe.
            // To fix: regenerate epochs with generate-epoch.py to store the merkle_root
            console.warn(`No expected root for epoch ${epochId} - marking as pending (needs regeneration)`)
            is_published = false
          }
        }
      } else if (boxResponse.status === 404) {
        // Box not found = epoch not published
        is_published = false
      }
    } catch (e) {
      console.error('Error checking epoch publication:', e)
      // On error, assume not published to be safe
      is_published = false
    }

    // Return the proof data with publication status
    return new Response(
      JSON.stringify({
        epoch_id: claims.epoch_id,
        cumulative: claims.cumulative_amount,
        proof: claims.proof || [],
        app_id: pool.contract_app_id,
        contract_version: pool.contract_version || 'puya',
        pool_id_int: pool_id_uint,
        is_published: is_published,  // ✅ NEW: Whether epoch is verified on-chain
        merkle_root: expected_root,  // ✅ NEW: Expected root for debugging
        message: is_published
          ? `✅ VERIFIED - Epoch ${claims.epoch_id} published on-chain`
          : `⏳ PENDING - Epoch ${claims.epoch_id} not yet published`
      }),
      { status: 200, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
    )

  } catch (error) {
    console.error('Error:', error)
    return new Response(
      JSON.stringify({ error: error.message }),
      { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
    )
  }
})