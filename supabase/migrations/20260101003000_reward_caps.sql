-- Preserve existing stakes/credits. A timestamped reward-weight cap is
-- configured separately after the scheduler has passed its preview.
ALTER TABLE public.pools ADD COLUMN reward_stake_cap numeric;
ALTER TABLE public.pools ADD COLUMN reward_stake_cap_from timestamptz;
ALTER TABLE public.pools ADD CONSTRAINT pool_reward_stake_cap_valid CHECK (
  (reward_stake_cap IS NULL AND reward_stake_cap_from IS NULL) OR
  (reward_stake_cap IS NOT NULL AND reward_stake_cap_from IS NOT NULL
    AND reward_stake_cap>0
    AND reward_stake_cap<=18446744073709551615));

-- Closing an empty placeholder preserves its original end, full row and the
-- verified funding observation. Positive-budget periods cannot be restarted.
CREATE TABLE public.funded_reward_period_restarts (
  period_id uuid PRIMARY KEY REFERENCES public.funded_reward_periods(id),
  previous_period jsonb NOT NULL,
  funding_snapshot jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE public.funded_reward_period_restarts ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.funded_reward_period_restarts FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT ON public.funded_reward_period_restarts TO service_role;

CREATE FUNCTION public.close_empty_funded_reward_period(
  p_pool uuid,p_holder uuid,p_period uuid,p_revision text,p_snapshot jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE v public.funded_reward_vaults; p public.funded_reward_periods;
  previous jsonb; available numeric;
BEGIN
  v:=funded_private.lock_vault(p_pool,p_holder);
  SELECT * INTO p FROM public.funded_reward_periods WHERE id=p_period AND pool_id=p_pool FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Empty period not found'; END IF;
  SELECT previous_period INTO previous FROM public.funded_reward_period_restarts WHERE period_id=p.id;
  IF FOUND THEN
    IF previous->>'revision' IS DISTINCT FROM p_revision THEN RAISE EXCEPTION 'Empty period revision differs'; END IF;
    RETURN funded_private.period(p);
  END IF;
  IF p.closed_at IS NOT NULL OR p.budget<>0 OR p.scheduled<>0 OR p.issued<>0
     OR p.cursor_at<=p.start_at OR p.cursor_at>=p.end_at
     OR p.revision<>funded_private.u64(to_jsonb(p_revision)) THEN
    RAISE EXCEPTION 'Only an unchanged empty period with verified elapsed history can restart';
  END IF;
  IF EXISTS(SELECT 1 FROM public.funded_reward_batches WHERE pool_id=p_pool AND confirmed_at IS NULL) THEN
    RAISE EXCEPTION 'Finish the pending reward batch first';
  END IF;
  PERFORM funded_private.snapshot(p_snapshot,v.app_id,v.config);
  IF funded_private.u64(p_snapshot->'round')<v.last_confirmed_round
     OR funded_private.at(p_snapshot->'at')<p.cursor_at
     OR funded_private.at(p_snapshot->'at')>=p.end_at
     OR funded_private.at(p_snapshot->'at')>clock_timestamp()
     OR funded_private.u64(p_snapshot->'allocated')<>v.confirmed_allocated
     OR funded_private.u64(p_snapshot->'deposited')<=funded_private.u64(p.source_snapshot->'deposited') THEN
    RAISE EXCEPTION 'Fresh registered funding and a current reconciled snapshot are required';
  END IF;
  available:=least(funded_private.u64(p_snapshot->'deposited')-v.confirmed_allocated,
    funded_private.u64(p_snapshot->'balance')-(v.confirmed_allocated-funded_private.u64(p_snapshot->'paid')));
  IF available<=0 THEN RAISE EXCEPTION 'No unallocated registered funding is available'; END IF;
  INSERT INTO public.funded_reward_period_restarts(period_id,previous_period,funding_snapshot)
    VALUES(p.id,to_jsonb(p),p_snapshot);
  UPDATE public.funded_reward_periods SET end_at=cursor_at,closed_at=clock_timestamp(),revision=revision+1
    WHERE id=p.id RETURNING * INTO p;
  RETURN funded_private.period(p);
END $$;
REVOKE ALL ON FUNCTION public.close_empty_funded_reward_period(uuid,uuid,uuid,text,jsonb)
  FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.close_empty_funded_reward_period(uuid,uuid,uuid,text,jsonb) TO service_role;

CREATE OR REPLACE FUNCTION public.get_funded_stake_export(p_pool uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET timezone='UTC' SET lock_timeout='5s' AS $$
DECLARE exported_at timestamptz; result jsonb;
BEGIN
  IF NOT EXISTS(SELECT 1 FROM public.funded_reward_vaults WHERE pool_id=p_pool) THEN RAISE EXCEPTION 'Vault is not registered'; END IF;
  LOCK TABLE public.user_stakes IN SHARE MODE;
  exported_at:=clock_timestamp();
  SELECT jsonb_build_object('mode','funded_stake_export','exported_at',exported_at,'pools',jsonb_build_array(
    jsonb_build_object('pool_id',p.id,'name',p.pool_name,'app_id',p.contract_app_id::text,
      'nft_pool',p.pool_type='nft staking','nft_indexed',c.is_indexed,
      'asset_ids',CASE WHEN p.pool_type='nft staking' THEN to_jsonb(c.indexed_asset_ids)
        WHEN p.pool_type='lp staking' THEN jsonb_build_array(p.lp_token_id)
        ELSE jsonb_build_array(p.staking_token_id) END,
      'decimals',CASE WHEN p.pool_type='nft staking' THEN 0 ELSE p.staking_token_decimals END,
      'reward_stake_cap',p.reward_stake_cap::text,
      'reward_stake_cap_from',to_char(p.reward_stake_cap_from AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
      'stake_events',coalesce((SELECT jsonb_agg(to_jsonb(e)||jsonb_build_object('id',e.id::text,
        'amount_before',e.amount_before::text,'amount_after',e.amount_after::text) ORDER BY e.occurred_at,e.id)
        FROM public.stake_events e WHERE e.pool_id=p.id AND e.occurred_at<=exported_at),'[]'::jsonb))))
    INTO result FROM public.pools p LEFT JOIN public.nft_collections c ON c.id=p.nft_collection_id
    WHERE p.id=p_pool AND p.hidden IS NOT TRUE;
  IF result IS NULL THEN RAISE EXCEPTION 'Pool is unavailable'; END IF;
  RETURN result;
END $$;
REVOKE ALL ON FUNCTION public.get_funded_stake_export(uuid) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.get_funded_stake_export(uuid) TO service_role;
