-- Read-only rate metadata from the same sealed holdings and stake journal used
-- by reward accounting. No credits, budgets, stakes or publication RPCs change.
CREATE FUNCTION funded_private.display_eligible_stake(p_pool uuid,p_checkpoint jsonb) RETURNS text
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path='' AS $$
DECLARE p public.pools; checked_at timestamptz; result numeric; expected_policy text;
BEGIN
  SELECT * INTO p FROM public.pools WHERE id=p_pool;
  SELECT config->>'asset_policy_digest' INTO expected_policy FROM public.funded_reward_vaults WHERE pool_id=p_pool;
  checked_at:=(p_checkpoint->>'at')::timestamptz;
  IF checked_at IS NULL OR jsonb_typeof(p_checkpoint->'holdings') IS DISTINCT FROM 'object'
    OR p_checkpoint->>'policy_digest' IS DISTINCT FROM expected_policy OR expected_policy IS NULL
    OR (SELECT count(*) FROM public.stake_events WHERE pool_id=p_pool AND occurred_at<=checked_at)
      IS DISTINCT FROM (p_checkpoint->>'stake_event_count')::bigint THEN RETURN NULL; END IF;

  WITH latest AS (
    SELECT DISTINCT ON (stake_id) wallet_address,amount_after,active_after
      FROM public.stake_events WHERE pool_id=p_pool AND occurred_at<=checked_at
      ORDER BY stake_id,occurred_at DESC,id DESC
  ), registered AS (
    SELECT wallet_address,sum(CASE WHEN active_after THEN amount_after ELSE 0 END) amount
      FROM latest GROUP BY wallet_address
  ), weights AS (
    SELECT r.wallet_address,r.amount,p_checkpoint->'holdings'->r.wallet_address holdings,
      CASE WHEN p.pool_type='nft staking' THEN
        (SELECT count(*)::numeric FROM jsonb_each_text(p_checkpoint->'holdings'->r.wallet_address) h WHERE h.value::numeric>0)
      ELSE coalesce((p_checkpoint->'holdings'->r.wallet_address->>
        CASE WHEN p.pool_type='lp staking' THEN p.lp_token_id::text ELSE p.staking_token_id::text END)::numeric,0)
        / power(10::numeric,p.staking_token_decimals) END held
    FROM registered r
  )
  SELECT CASE WHEN count(*) FILTER(WHERE holdings IS NULL OR amount<0 OR held<0)>0 THEN NULL
    ELSE coalesce(sum(least(amount,held,
      CASE WHEN p.reward_stake_cap IS NOT NULL AND checked_at>=p.reward_stake_cap_from
        THEN p.reward_stake_cap ELSE amount END)),0) END INTO result FROM weights;
  RETURN result::text;
EXCEPTION WHEN data_exception THEN
  -- Missing rate metadata must not disable a user's otherwise valid claim.
  RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION funded_private.display_eligible_stake(uuid,jsonb) FROM PUBLIC,anon,authenticated,service_role;

CREATE OR REPLACE FUNCTION public.get_funded_pool_display(p_wallet text DEFAULT NULL) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path='' AS $$
DECLARE result jsonb;
BEGIN
  IF p_wallet IS NOT NULL AND p_wallet !~ '^[A-Z2-7]{58}$' THEN RAISE EXCEPTION 'Invalid wallet'; END IF;
  SELECT coalesce(jsonb_agg(jsonb_build_object('pool_id',p.id,'name',p.pool_name,'config',v.config,
    'reward_decimals',p.reward_token_decimals,'reward_symbol',p.reward_token_symbol,
    'period',CASE WHEN r.id IS NULL THEN NULL ELSE jsonb_build_object('id',r.id,
      'start',to_char(r.start_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
      'end',to_char(r.end_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
      'cursor',to_char(r.cursor_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
      'budget_atomic',r.budget::text,'scheduled_atomic',r.scheduled::text,'issued_atomic',r.issued::text,
      'eligible_stake',funded_private.display_eligible_stake(p.id,r.checkpoint),
      'eligible_stake_at',to_char(r.cursor_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
      'closed',r.closed_at IS NOT NULL) END) ORDER BY p.id),'[]'::jsonb) INTO result
    FROM public.pools p JOIN public.funded_reward_vaults v ON v.pool_id=p.id
    LEFT JOIN LATERAL(SELECT * FROM public.funded_reward_periods WHERE pool_id=p.id ORDER BY start_at DESC LIMIT 1) r ON true
    WHERE p.hidden IS NOT TRUE AND p.contract_app_id::numeric=v.app_id;
  RETURN result;
END $$;
REVOKE ALL ON FUNCTION public.get_funded_pool_display(text) FROM PUBLIC,anon,authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.get_funded_pool_display(text) TO service_role;
