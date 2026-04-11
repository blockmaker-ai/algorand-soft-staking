// Supabase Edge Function: save-monthly-snapshots
// Runs at the end of each month to capture pool state for APY tracking
// Schedule via Supabase Dashboard > Database > Extensions > pg_cron
// Example: SELECT cron.schedule('monthly-snapshots', '0 0 1 * *', $$SELECT net.http_post(...)$$);

import { createClient } from 'https://esm.sh/@supabase/supabase-js@2';

// ✅ SECURITY: Only allow cron jobs or admin to call this function
const CRON_SECRET = Deno.env.get('CRON_SECRET');

const corsHeaders = {
  'Access-Control-Allow-Origin': 'https://supabase.com', // Only allow from Supabase cron
  'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type',
};

interface Pool {
  id: string;
  pool_name: string;
  contract_address: string;
  reward_token_id: number;
  funding_model: string;
}

interface Stake {
  amount_staked: string;
}

Deno.serve(async (req) => {
  // Handle CORS preflight
  if (req.method === 'OPTIONS') {
    return new Response('ok', { headers: corsHeaders });
  }

  try {
    // ✅ SECURITY: Verify cron secret or service role authorization
    const authHeader = req.headers.get('authorization');
    const cronSecretHeader = req.headers.get('x-cron-secret');

    const isAuthorized =
      (CRON_SECRET && cronSecretHeader === CRON_SECRET) ||
      (authHeader && authHeader.includes('service_role'));

    if (!isAuthorized) {
      console.error('❌ Unauthorized access attempt to save-monthly-snapshots');
      return new Response(
        JSON.stringify({ error: 'Unauthorized' }),
        { status: 401, headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
      );
    }
    // Initialize Supabase client
    const supabaseUrl = Deno.env.get('SUPABASE_URL')!;
    const supabaseServiceKey = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!;
    const supabase = createClient(supabaseUrl, supabaseServiceKey);

    // Algod configuration — set ALGOD_URL and optionally NODELY_API_KEY in env
    const algodAddress = Deno.env.get('ALGOD_URL') || 'https://mainnet-api.algonode.cloud';
    const nodelyApiKey = Deno.env.get('NODELY_API_KEY');
    const fetchOptions = nodelyApiKey
      ? { headers: { 'X-Algo-API-Token': nodelyApiKey } }
      : {};

    console.log('📸 Starting monthly snapshot save...');

    // Fetch all active monthly rolling pools
    const { data: pools, error: poolsError } = await supabase
      .from('pools')
      .select('id, pool_name, contract_address, reward_token_id, funding_model')
      .eq('funding_model', 'monthly rolling pool')
      .eq('funding_confirmed', true)
      .eq('hidden', false);

    if (poolsError) {
      throw new Error(`Failed to fetch pools: ${poolsError.message}`);
    }

    const results = [];
    const now = new Date();
    // Use UTC components explicitly — avoids DST/timezone shifts from toISOString()
    const snapshotMonth = `${now.getUTCFullYear()}-${String(now.getUTCMonth() + 1).padStart(2, '0')}-01`;

    for (const pool of pools as Pool[]) {
      try {
        // Get total staked for this pool
        const { data: stakes, error: stakesError } = await supabase
          .from('user_stakes')
          .select('amount_staked')
          .eq('pool_id', pool.id)
          .eq('is_active', true);

        if (stakesError) {
          console.error(`Error fetching stakes for pool ${pool.id}:`, stakesError);
          continue;
        }

        const totalStaked = (stakes as Stake[] || []).reduce((sum, s) =>
          sum + (parseFloat(s.amount_staked) || 0), 0);
        const stakerCount = (stakes || []).length;

        // Fetch token balance from Algorand (via Nodely.io)
        let totalRewards = 0;
        if (pool.contract_address && pool.reward_token_id) {
          try {
            const accountUrl = `${algodAddress}/v2/accounts/${pool.contract_address}`;
            const response = await fetch(accountUrl, fetchOptions);
            const accountInfo = await response.json();

            const assets = accountInfo.assets || [];
            const asset = assets.find((a: any) => {
              const id = Number(a['asset-id'] ?? a.assetId ?? 0);
              return id === pool.reward_token_id;
            });

            if (asset) {
              // Fetch asset info for decimals
              const assetUrl = `${algodAddress}/v2/assets/${pool.reward_token_id}`;
              const assetResponse = await fetch(assetUrl, fetchOptions);
              const assetInfo = await assetResponse.json();
              const decimals = assetInfo.params?.decimals ?? 6;

              const rawBalance = Number(asset.amount || 0);
              totalRewards = rawBalance / Math.pow(10, decimals);
            }
          } catch (err) {
            console.error(`Error fetching balance for pool ${pool.id}:`, err);
          }
        }

        // Calculate APY
        let calculatedApy = null;
        if (totalStaked > 0 && totalRewards > 0) {
          const monthlyRate = totalRewards / totalStaked;
          const annualRate = monthlyRate * 12 * 100;
          calculatedApy = Math.min(annualRate, 10000);
        }

        // Save snapshot
        const { data: snapshot, error: insertError } = await supabase
          .from('monthly_pool_snapshots')
          .upsert({
            pool_id: pool.id,
            snapshot_month: snapshotMonth,
            total_rewards: totalRewards,
            total_staked: totalStaked,
            staker_count: stakerCount,
            calculated_apy: calculatedApy
          }, {
            onConflict: 'pool_id,snapshot_month'
          })
          .select()
          .single();

        if (insertError) {
          console.error(`Error saving snapshot for pool ${pool.id}:`, insertError);
          results.push({ poolId: pool.id, poolName: pool.pool_name, error: insertError.message });
        } else {
          console.log(`✅ Snapshot saved for ${pool.pool_name}`);
          results.push({ poolId: pool.id, poolName: pool.pool_name, snapshot });
        }
      } catch (err) {
        console.error(`Error processing pool ${pool.id}:`, err);
        results.push({ poolId: pool.id, poolName: pool.pool_name, error: String(err) });
      }
    }

    console.log(`📸 Completed: ${results.length} snapshots processed`);

    return new Response(
      JSON.stringify({
        success: true,
        snapshotMonth,
        poolsProcessed: results.length,
        results
      }),
      {
        headers: { ...corsHeaders, 'Content-Type': 'application/json' },
        status: 200
      }
    );

  } catch (error) {
    console.error('❌ Error in save-monthly-snapshots:', error);

    return new Response(
      JSON.stringify({
        success: false,
        error: String(error)
      }),
      {
        headers: { ...corsHeaders, 'Content-Type': 'application/json' },
        status: 500
      }
    );
  }
});
