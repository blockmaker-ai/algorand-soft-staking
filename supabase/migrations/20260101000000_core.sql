-- Fresh Supabase database. Configuration writes belong to the trusted operator.
CREATE TABLE public.nft_collections (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), name text NOT NULL,
 is_indexed boolean NOT NULL DEFAULT false, indexed_asset_ids bigint[] NOT NULL DEFAULT '{}'
);
CREATE TABLE public.pools (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), pool_name text NOT NULL,
 pool_type text NOT NULL CHECK(pool_type IN ('single token staking','lp staking','nft staking')),
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','active','ended')),
 hidden boolean NOT NULL DEFAULT false, funding_confirmed boolean NOT NULL DEFAULT false,
 creator_address text NOT NULL CHECK(creator_address ~ '^[A-Z2-7]{58}$'),
 contract_app_id numeric CHECK(contract_app_id>0 AND contract_app_id=trunc(contract_app_id) AND contract_app_id<=18446744073709551615),
 staking_token_id text, lp_token_id text, staking_token_decimals integer CHECK(staking_token_decimals BETWEEN 0 AND 19),
 nft_collection_id uuid REFERENCES public.nft_collections(id),
 reward_token_id text NOT NULL CHECK(reward_token_id ~ '^[1-9][0-9]{0,19}$'),
 reward_token_symbol text NOT NULL DEFAULT 'TOKEN', reward_token_decimals integer NOT NULL CHECK(reward_token_decimals BETWEEN 0 AND 19),
 min_stake text NOT NULL DEFAULT '0' CHECK(min_stake ~ '^(0|[1-9][0-9]*)(\.[0-9]{1,19})?$'),
 max_stake text NOT NULL DEFAULT '0' CHECK(max_stake ~ '^(0|[1-9][0-9]*)(\.[0-9]{1,19})?$'),
 start_date timestamptz, end_date timestamptz,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE public.user_stakes (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), pool_id uuid NOT NULL REFERENCES public.pools(id),
 wallet_address text NOT NULL CHECK(wallet_address ~ '^[A-Z2-7]{58}$'),
 amount_staked text NOT NULL CHECK(amount_staked ~ '^(0|[1-9][0-9]*)(\.[0-9]{1,19})?$'),
 is_active boolean NOT NULL DEFAULT true, staked_at timestamp without time zone NOT NULL DEFAULT (clock_timestamp() AT TIME ZONE 'UTC'),
 unstaked_at timestamp without time zone, updated_at timestamp without time zone NOT NULL DEFAULT (clock_timestamp() AT TIME ZONE 'UTC'),
 invalidated_at timestamptz, invalidation_reason text, stake_tx_id text
);
CREATE UNIQUE INDEX user_stakes_active ON public.user_stakes(pool_id,wallet_address) WHERE is_active;
CREATE INDEX user_stakes_wallet ON public.user_stakes(wallet_address,is_active);
ALTER TABLE public.pools ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.nft_collections ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_stakes ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.pools,public.nft_collections,public.user_stakes FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT ON public.pools,public.nft_collections TO anon,authenticated;
CREATE POLICY visible_pools ON public.pools FOR SELECT TO anon,authenticated USING(NOT hidden);
CREATE POLICY collection_metadata ON public.nft_collections FOR SELECT TO anon,authenticated USING(true);
GRANT SELECT,INSERT,UPDATE ON public.pools,public.nft_collections TO service_role;
GRANT SELECT ON public.user_stakes TO service_role;
