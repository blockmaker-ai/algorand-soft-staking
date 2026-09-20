-- Wallet signatures are verified by the Edge Function. Only service_role can
-- issue/consume these challenges; nonce consumption and mutation commit together.
CREATE TABLE public.pool_action_challenges (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  action text NOT NULL CHECK (action IN ('stake','unstake')),
  pool_id uuid NOT NULL REFERENCES public.pools(id),
  wallet_address text NOT NULL CHECK (length(wallet_address)=58),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'),
  payload_hash text NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
  origin text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  expires_at timestamptz NOT NULL DEFAULT (clock_timestamp() + interval '5 minutes'),
  consumed_at timestamptz,
  result jsonb
);
CREATE INDEX pool_action_wallet_created ON public.pool_action_challenges(wallet_address,created_at);
CREATE INDEX pool_action_wallet_consumed ON public.pool_action_challenges(wallet_address,consumed_at) WHERE consumed_at IS NOT NULL;
ALTER TABLE public.pool_action_challenges ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.pool_action_challenges FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT,INSERT,UPDATE ON public.pool_action_challenges TO service_role;

CREATE TABLE public.stake_events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  pool_id uuid NOT NULL REFERENCES public.pools(id),
  stake_id uuid NOT NULL,
  wallet_address text NOT NULL,
  event_type text NOT NULL CHECK (event_type IN ('baseline','stake','increase','decrease','close','reactivate','delete')),
  amount_before numeric NOT NULL CHECK (amount_before>=0),
  amount_after numeric NOT NULL CHECK (amount_after>=0),
  active_before boolean NOT NULL,
  active_after boolean NOT NULL,
  occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  action_id uuid REFERENCES public.pool_action_challenges(id),
  observed_round bigint,
  stake_started_at timestamp without time zone,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX stake_events_pool_cursor ON public.stake_events(pool_id,occurred_at,id);
CREATE UNIQUE INDEX stake_events_baseline_once ON public.stake_events(stake_id) WHERE event_type='baseline';
ALTER TABLE public.stake_events ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.stake_events FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT ON public.stake_events TO service_role;

-- Baseline and trigger installation must see the same state. No stake is changed.
LOCK TABLE public.user_stakes IN SHARE ROW EXCLUSIVE MODE;
WITH baseline_clock AS MATERIALIZED (SELECT clock_timestamp() AS at)
INSERT INTO public.stake_events(pool_id,stake_id,wallet_address,event_type,amount_before,amount_after,active_before,active_after,occurred_at,stake_started_at,metadata)
SELECT s.pool_id,s.id,s.wallet_address,'baseline',0,s.amount_staked::numeric,false,true,baseline_clock.at,s.staked_at,
  jsonb_build_object('source','existing_active_stake','history_before_baseline','not_reconstructed')
FROM public.user_stakes s JOIN public.pools p ON p.id=s.pool_id
CROSS JOIN baseline_clock
WHERE s.is_active AND p.hidden IS NOT TRUE;

CREATE FUNCTION public.record_stake_event() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE
  before_amount numeric:=0; after_amount numeric:=0;
  was_active boolean:=false; now_active boolean:=false;
  event_kind text; row_data public.user_stakes;
BEGIN
  IF TG_OP<>'INSERT' THEN
    was_active:=coalesce(OLD.is_active,false);
    before_amount:=CASE WHEN was_active THEN coalesce(nullif(OLD.amount_staked,''),'0')::numeric ELSE 0 END;
  END IF;
  IF TG_OP<>'DELETE' THEN
    now_active:=coalesce(NEW.is_active,false);
    after_amount:=CASE WHEN now_active THEN coalesce(nullif(NEW.amount_staked,''),'0')::numeric ELSE 0 END;
    row_data:=NEW;
  ELSE row_data:=OLD;
  END IF;
  IF TG_OP='UPDATE' AND was_active=now_active AND before_amount=after_amount THEN RETURN NEW; END IF;
  event_kind:=CASE WHEN TG_OP='DELETE' THEN 'delete' WHEN TG_OP='INSERT' THEN 'stake'
    WHEN NOT now_active THEN 'close' WHEN NOT was_active THEN 'reactivate'
    WHEN after_amount>before_amount THEN 'increase' ELSE 'decrease' END;
  INSERT INTO public.stake_events(pool_id,stake_id,wallet_address,event_type,amount_before,amount_after,active_before,active_after,action_id,observed_round,stake_started_at,metadata)
  VALUES(row_data.pool_id,row_data.id,row_data.wallet_address,event_kind,before_amount,after_amount,was_active,now_active,
    nullif(current_setting('staking.action_id',true),'')::uuid,
    nullif(current_setting('staking.observed_round',true),'')::bigint,row_data.staked_at,
    jsonb_build_object('invalidation_reason',row_data.invalidation_reason,'invalidated_at',row_data.invalidated_at));
  IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public.record_stake_event() FROM PUBLIC,anon,authenticated;
CREATE TRIGGER record_stake_history AFTER INSERT OR UPDATE OR DELETE ON public.user_stakes
FOR EACH ROW EXECUTE FUNCTION public.record_stake_event();

CREATE FUNCTION public.issue_pool_action_challenge(p_action text,p_pool_id uuid,p_wallet text,p_payload jsonb,p_payload_hash text,p_origin text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE p public.pools; c public.pool_action_challenges;
BEGIN
  IF p_action NOT IN ('stake','unstake') OR length(p_wallet)<>58 OR jsonb_typeof(p_payload)<>'object' THEN RAISE EXCEPTION 'Invalid action'; END IF;
  SELECT * INTO p FROM public.pools WHERE id=p_pool_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'Pool not found'; END IF;
  IF p_action='stake' AND (p.hidden IS TRUE OR p.status IS DISTINCT FROM 'active' OR p.funding_confirmed IS DISTINCT FROM true OR (p.end_date IS NOT NULL AND p.end_date<=(clock_timestamp() AT TIME ZONE 'UTC'))) THEN RAISE EXCEPTION 'Pool is not accepting stakes'; END IF;
  IF p_action IN ('stake','unstake') AND (coalesce(p_payload->>'amount','') !~ '^(0|[1-9][0-9]*)(\.[0-9]{1,19})?$' OR (p_payload->>'amount')::numeric<=0) THEN RAISE EXCEPTION 'Invalid stake amount'; END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('pool-challenge:'||p_wallet,0));
  IF (SELECT count(*) FROM public.pool_action_challenges WHERE wallet_address=p_wallet AND created_at>clock_timestamp()-interval '5 minutes')>=60 THEN RAISE EXCEPTION 'Too many authorization requests. Please wait a few minutes.'; END IF;
  INSERT INTO public.pool_action_challenges(action,pool_id,wallet_address,payload,payload_hash,origin)
  VALUES(p_action,p_pool_id,p_wallet,p_payload,p_payload_hash,p_origin) RETURNING * INTO c;
  RETURN to_jsonb(c)-'result'-'consumed_at';
END $$;

CREATE FUNCTION public.apply_stake_action(p_challenge_id uuid,p_payload_hash text,p_expected_amount numeric,p_wallet_holdings numeric DEFAULT NULL,p_observed_round bigint DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE c public.pool_action_challenges; p public.pools; s public.user_stakes;
  current_amount numeric:=0; new_amount numeric; amount numeric; other_commitments numeric:=0;
  minimum numeric; maximum numeric; total numeric; stakers bigint; answer jsonb;
BEGIN
  SELECT * INTO c FROM public.pool_action_challenges WHERE id=p_challenge_id FOR UPDATE;
  IF NOT FOUND OR c.payload_hash IS DISTINCT FROM p_payload_hash OR c.action NOT IN ('stake','unstake') THEN RAISE EXCEPTION 'Invalid wallet authorization'; END IF;
  IF c.consumed_at IS NOT NULL THEN RETURN c.result; END IF;
  IF c.expires_at<=clock_timestamp() THEN RAISE EXCEPTION 'Wallet authorization expired. Please try again.'; END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('pool-wallet:'||c.wallet_address,0));
  IF (SELECT count(*) FROM public.pool_action_challenges WHERE wallet_address=c.wallet_address AND consumed_at>clock_timestamp()-interval '1 minute')>=10 THEN RAISE EXCEPTION 'Too many pool actions. Please wait a minute.'; END IF;
  SELECT * INTO p FROM public.pools WHERE id=c.pool_id FOR UPDATE;
  SELECT * INTO s FROM public.user_stakes WHERE pool_id=c.pool_id AND wallet_address=c.wallet_address AND is_active FOR UPDATE;
  IF FOUND THEN current_amount:=s.amount_staked::numeric; END IF;
  IF current_amount IS DISTINCT FROM p_expected_amount THEN RAISE EXCEPTION 'Your stake changed while this action was being checked. Please refresh and try again.'; END IF;
  amount:=(c.payload->>'amount')::numeric;
  IF amount<=0 OR amount::text='NaN' THEN RAISE EXCEPTION 'Invalid stake amount'; END IF;
  IF p.pool_type='nft staking' AND trunc(amount)<>amount THEN RAISE EXCEPTION 'NFT amounts must be whole numbers'; END IF;
  IF p.pool_type<>'nft staking' AND amount<>trunc(amount,coalesce(p.staking_token_decimals,6)) THEN RAISE EXCEPTION 'Amount has too many decimal places'; END IF;
  IF c.action='stake' THEN
    IF p.hidden IS TRUE OR p.status IS DISTINCT FROM 'active' OR p.funding_confirmed IS DISTINCT FROM true OR (p.end_date IS NOT NULL AND p.end_date<=(clock_timestamp() AT TIME ZONE 'UTC')) THEN RAISE EXCEPTION 'Pool is not accepting stakes'; END IF;
    IF p_wallet_holdings IS NULL OR p_wallet_holdings::text !~ '^[0-9]+(\.[0-9]+)?$' OR p_observed_round IS NULL OR p_observed_round<=0 THEN RAISE EXCEPTION 'Wallet holdings could not be verified'; END IF;
    minimum:=coalesce(nullif(p.min_stake,''),'0')::numeric; maximum:=coalesce(nullif(p.max_stake,''),'0')::numeric;
    new_amount:=current_amount+amount;
    IF amount<minimum THEN RAISE EXCEPTION 'Amount is below the minimum stake'; END IF;
    IF maximum>0 AND new_amount>maximum THEN RAISE EXCEPTION 'Amount exceeds the maximum stake'; END IF;
    SELECT coalesce(sum(us.amount_staked::numeric),0) INTO other_commitments
    FROM public.user_stakes us JOIN public.pools op ON op.id=us.pool_id
    WHERE us.wallet_address=c.wallet_address AND us.is_active AND us.pool_id<>c.pool_id AND op.hidden IS NOT TRUE AND (
      (p.pool_type='nft staking' AND op.pool_type='nft staking' AND op.nft_collection_id=p.nft_collection_id)
      OR (p.pool_type<>'nft staking' AND op.pool_type<>'nft staking' AND
        (CASE WHEN op.pool_type='lp staking' THEN op.lp_token_id::text ELSE op.staking_token_id END)=
        (CASE WHEN p.pool_type='lp staking' THEN p.lp_token_id::text ELSE p.staking_token_id END)));
    IF new_amount+other_commitments>p_wallet_holdings THEN RAISE EXCEPTION 'Insufficient available wallet holdings'; END IF;
  ELSE
    IF s.id IS NULL OR amount>current_amount THEN RAISE EXCEPTION 'Amount exceeds your current stake'; END IF;
    new_amount:=current_amount-amount;
  END IF;
  IF c.expires_at<=clock_timestamp() THEN RAISE EXCEPTION 'Wallet authorization expired. Please try again.'; END IF;
  -- All validation completed before consuming the nonce or touching the stake.
  PERFORM set_config('staking.action_id',c.id::text,true);
  PERFORM set_config('staking.observed_round',coalesce(p_observed_round::text,''),true);
  IF s.id IS NULL THEN
    INSERT INTO public.user_stakes(pool_id,wallet_address,amount_staked,staked_at,is_active,stake_tx_id)
    VALUES(c.pool_id,c.wallet_address,new_amount::text,clock_timestamp() AT TIME ZONE 'UTC',true,NULL);
  ELSE
    UPDATE public.user_stakes SET amount_staked=new_amount::text,is_active=(new_amount>0),
      unstaked_at=CASE WHEN new_amount=0 THEN clock_timestamp() AT TIME ZONE 'UTC' ELSE unstaked_at END,
      updated_at=clock_timestamp() AT TIME ZONE 'UTC' WHERE id=s.id;
  END IF;
  SELECT coalesce(sum(amount_staked::numeric),0),count(*) INTO total,stakers FROM public.user_stakes WHERE pool_id=c.pool_id AND is_active;
  answer:=jsonb_build_object('success',true,'newStakeAmount',new_amount::text,'totalPoolStaked',total::text,'stakerCount',stakers::text);
  UPDATE public.pool_action_challenges SET consumed_at=clock_timestamp(),result=answer WHERE id=c.id;
  RETURN answer;
END $$;


REVOKE ALL ON FUNCTION public.issue_pool_action_challenge(text,uuid,text,jsonb,text,text), public.apply_stake_action(uuid,text,numeric,numeric,bigint) FROM PUBLIC,anon,authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.issue_pool_action_challenge(text,uuid,text,jsonb,text,text), public.apply_stake_action(uuid,text,numeric,numeric,bigint) TO service_role;
