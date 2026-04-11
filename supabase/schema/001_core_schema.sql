-- ============================================================================
-- Algorand Soft Staking Infrastructure - Core Database Schema
-- ============================================================================
-- Deploy this to a Supabase project. Tables use Row Level Security (RLS).
-- Run migrations in order (001, 002, ...).
-- ============================================================================

-- ── pools ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pools (
  id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  pool_name               TEXT NOT NULL,
  pool_type               TEXT NOT NULL CHECK (pool_type IN ('single token staking', 'nft staking', 'lp staking')),
  status                  TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'active', 'ended', 'cancelled')),

  -- Staking token
  staking_token_id        TEXT,           -- Algorand ASA ID (null for NFT pools)
  staking_token_symbol    TEXT,
  staking_token_decimals  INTEGER DEFAULT 6,
  lp_token_id             TEXT,           -- For LP staking pools
  lp_token_pair_name      TEXT,
  nft_collection_id       UUID,           -- FK to nft_collections (for NFT pools)

  -- Reward token
  reward_token_id         TEXT,
  reward_token_symbol     TEXT,
  reward_token_decimals   INTEGER DEFAULT 6,

  -- Pool configuration
  funding_model           TEXT DEFAULT 'one-time pool' CHECK (funding_model IN ('one-time pool', 'monthly rolling pool')),
  reward_distribution     TEXT DEFAULT 'daily' CHECK (reward_distribution IN ('daily', 'weekly', 'monthly')),
  publishing_mode         TEXT NOT NULL DEFAULT 'self' CHECK (publishing_mode IN ('self', 'automated')),
  min_stake               NUMERIC,
  max_stake               NUMERIC,
  total_rewards           TEXT,           -- Human-readable reward amount
  lock_period             TEXT,

  -- Dates
  start_date              TIMESTAMPTZ,
  end_date                TIMESTAMPTZ,

  -- Smart contract
  contract_app_id         BIGINT,         -- Deployed Algorand app ID

  -- Metadata
  pool_description        TEXT,
  pool_image              TEXT,
  pool_image_hash         TEXT,
  creator_address         TEXT,           -- Wallet address of pool creator
  whitelist_mode          BOOLEAN DEFAULT FALSE,
  whitelist_addresses     TEXT,

  created_at              TIMESTAMPTZ DEFAULT NOW(),
  updated_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pools_status ON pools(status);
CREATE INDEX IF NOT EXISTS idx_pools_creator ON pools(creator_address);

ALTER TABLE pools ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Public read pools" ON pools FOR SELECT USING (true);
CREATE POLICY "Authenticated insert pools" ON pools FOR INSERT WITH CHECK (true);
CREATE POLICY "Authenticated update pools" ON pools FOR UPDATE USING (true);


-- ── user_stakes ───────────────────────────────────────────────────────────────
-- Soft staking: tokens stay in wallet, stakes are recorded here only.
CREATE TABLE IF NOT EXISTS user_stakes (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  pool_id             UUID NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
  wallet_address      TEXT NOT NULL,      -- Algorand address (58 chars)
  amount_staked       TEXT NOT NULL,      -- Stored as string to avoid float precision issues
  is_active           BOOLEAN NOT NULL DEFAULT TRUE,

  -- Timestamps
  staked_at           TIMESTAMPTZ DEFAULT NOW(),
  unstaked_at         TIMESTAMPTZ,
  updated_at          TIMESTAMPTZ DEFAULT NOW(),

  -- Invalidation (set when balance check fails at claim time)
  invalidated_at      TIMESTAMPTZ,
  invalidation_reason TEXT,

  -- Optional blockchain reference
  stake_tx_id         TEXT
);

CREATE INDEX IF NOT EXISTS idx_user_stakes_pool_wallet ON user_stakes(pool_id, wallet_address);
CREATE INDEX IF NOT EXISTS idx_user_stakes_wallet_active ON user_stakes(wallet_address, is_active);
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_stakes_unique_active
  ON user_stakes(pool_id, wallet_address)
  WHERE is_active = TRUE;

ALTER TABLE user_stakes ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Public read stakes" ON user_stakes FOR SELECT USING (true);
CREATE POLICY "Authenticated insert stakes" ON user_stakes FOR INSERT WITH CHECK (true);
CREATE POLICY "Authenticated update stakes" ON user_stakes FOR UPDATE USING (true);


-- ── merkle_epoch_claims ───────────────────────────────────────────────────────
-- One row per user per epoch. Cumulative amounts (not per-epoch deltas).
-- is_published=true means the merkle root is verified on-chain and claimable.
CREATE TABLE IF NOT EXISTS merkle_epoch_claims (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  pool_id             UUID NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
  epoch_id            INTEGER NOT NULL,
  user_address        TEXT NOT NULL,
  cumulative_amount   NUMERIC NOT NULL,   -- Total earned to date (in raw token units)
  proof               JSONB,              -- Array of 32-byte hex proof elements
  merkle_root         TEXT,               -- Expected root for verification
  is_published        BOOLEAN DEFAULT FALSE,
  created_at          TIMESTAMPTZ DEFAULT NOW(),
  updated_at          TIMESTAMPTZ DEFAULT NOW(),

  UNIQUE(pool_id, epoch_id, user_address)
);

CREATE INDEX IF NOT EXISTS idx_epoch_claims_pool_user ON merkle_epoch_claims(pool_id, user_address);
CREATE INDEX IF NOT EXISTS idx_epoch_claims_published ON merkle_epoch_claims(pool_id, is_published);

ALTER TABLE merkle_epoch_claims ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Public read claims" ON merkle_epoch_claims FOR SELECT USING (true);
CREATE POLICY "Service role write claims" ON merkle_epoch_claims FOR INSERT WITH CHECK (true);
CREATE POLICY "Service role update claims" ON merkle_epoch_claims FOR UPDATE USING (true);


-- ── nft_collections ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS nft_collections (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name                TEXT NOT NULL,
  creator_address     TEXT,               -- Algorand creator address (for lookup)
  indexed_asset_ids   BIGINT[],           -- Pre-indexed list of all ASA IDs in collection
  total_supply        INTEGER,
  floor_price_algo    NUMERIC,            -- Optional floor price hint (informational)
  created_at          TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE nft_collections ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Public read nft_collections" ON nft_collections FOR SELECT USING (true);


-- ── verified_tokens ──────────────────────────────────────────────────────────
-- Curated list of tokens that can be used as staking/reward tokens.
CREATE TABLE IF NOT EXISTS verified_tokens (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  asset_id    BIGINT NOT NULL UNIQUE,
  symbol      TEXT NOT NULL,
  name        TEXT NOT NULL,
  decimals    INTEGER DEFAULT 6,  -- IMPORTANT: use ?? not || when reading (0 is valid)
  logo_url    TEXT,
  is_verified BOOLEAN DEFAULT TRUE,
  added_by    TEXT,               -- Admin wallet address
  created_at  TIMESTAMPTZ DEFAULT NOW(),
  updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_verified_tokens_asset_id ON verified_tokens(asset_id);
CREATE INDEX IF NOT EXISTS idx_verified_tokens_symbol ON verified_tokens(symbol);

ALTER TABLE verified_tokens ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Public read verified_tokens" ON verified_tokens FOR SELECT USING (true);
CREATE POLICY "Authenticated insert verified_tokens" ON verified_tokens FOR INSERT WITH CHECK (true);
CREATE POLICY "Authenticated update verified_tokens" ON verified_tokens FOR UPDATE USING (true);
CREATE POLICY "Authenticated delete verified_tokens" ON verified_tokens FOR DELETE USING (true);

-- Seed with common Algorand tokens
INSERT INTO verified_tokens (asset_id, symbol, name, decimals, is_verified) VALUES
  (0,          'ALGO',  'Algorand',  6, true),
  (31566704,   'USDC',  'USD Coin',  6, true),
  (312769,     'USDT',  'Tether',    6, true),
  (470842789,  'DEFLY', 'Defly',     6, true)
ON CONFLICT (asset_id) DO NOTHING;


-- ── monthly_pool_snapshots ────────────────────────────────────────────────────
-- For rolling monthly pools: snapshot of reward deposits per month.
CREATE TABLE IF NOT EXISTS monthly_pool_snapshots (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  pool_id         UUID NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
  snapshot_month  DATE NOT NULL,          -- First day of the month (YYYY-MM-01)
  total_rewards   NUMERIC NOT NULL,
  created_at      TIMESTAMPTZ DEFAULT NOW(),

  UNIQUE(pool_id, snapshot_month)
);

ALTER TABLE monthly_pool_snapshots ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Public read snapshots" ON monthly_pool_snapshots FOR SELECT USING (true);
CREATE POLICY "Service role write snapshots" ON monthly_pool_snapshots FOR INSERT WITH CHECK (true);
