CREATE SCHEMA funded_private;
REVOKE ALL ON SCHEMA funded_private FROM PUBLIC,anon,authenticated,service_role;

CREATE FUNCTION funded_private.u64(v jsonb) RETURNS numeric
LANGUAGE plpgsql IMMUTABLE SET search_path='' AS $$
BEGIN
  -- Validate the original JSON string before casting; never round a number.
  IF jsonb_typeof(v) IS DISTINCT FROM 'string' OR (v#>>'{}') !~ '^(0|[1-9][0-9]{0,19})$'
     OR (v#>>'{}')::numeric>18446744073709551615 THEN
    RAISE EXCEPTION 'Expected an exact uint64 decimal string';
  END IF;
  RETURN (v#>>'{}')::numeric;
END $$;

CREATE FUNCTION funded_private.at(v jsonb) RETURNS timestamptz
LANGUAGE plpgsql IMMUTABLE SET search_path='' AS $$
BEGIN
  IF jsonb_typeof(v) IS DISTINCT FROM 'string'
     OR (v#>>'{}') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z$' THEN
    RAISE EXCEPTION 'Expected a UTC timestamp';
  END IF;
  RETURN (v#>>'{}')::timestamptz;
END $$;

CREATE FUNCTION funded_private.hash(v jsonb) RETURNS text
LANGUAGE sql IMMUTABLE STRICT SET search_path='' AS $$
  SELECT encode(sha256(convert_to(v::text,'UTF8')),'hex')
$$;

CREATE TABLE public.funded_reward_vaults (
  pool_id uuid PRIMARY KEY REFERENCES public.pools(id),
  app_id numeric NOT NULL UNIQUE CHECK(app_id>0 AND app_id=trunc(app_id) AND app_id<=18446744073709551615),
  config jsonb NOT NULL,
  opening_rewards jsonb NOT NULL,
  opening_snapshot jsonb NOT NULL,
  confirmed_allocated numeric NOT NULL CHECK(confirmed_allocated>=0 AND confirmed_allocated=trunc(confirmed_allocated) AND confirmed_allocated<=18446744073709551615),
  last_confirmed_round numeric NOT NULL CHECK(last_confirmed_round>0 AND last_confirmed_round=trunc(last_confirmed_round) AND last_confirmed_round<=18446744073709551615),
  lease_holder uuid,
  lease_until timestamptz,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK((lease_holder IS NULL)=(lease_until IS NULL))
);

CREATE TABLE public.funded_reward_credits (
  pool_id uuid NOT NULL REFERENCES public.funded_reward_vaults(pool_id),
  wallet_address text NOT NULL CHECK(wallet_address ~ '^[A-Z2-7]{58}$'),
  allocated numeric NOT NULL CHECK(allocated>0 AND allocated=trunc(allocated) AND allocated<=18446744073709551615),
  PRIMARY KEY(pool_id,wallet_address)
);

CREATE TABLE public.funded_reward_periods (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  pool_id uuid NOT NULL REFERENCES public.funded_reward_vaults(pool_id),
  start_at timestamptz NOT NULL,
  end_at timestamptz NOT NULL,
  cursor_at timestamptz NOT NULL,
  budget numeric NOT NULL,
  scheduled numeric NOT NULL DEFAULT 0,
  issued numeric NOT NULL DEFAULT 0,
  revision integer NOT NULL DEFAULT 0 CHECK(revision>=0),
  source_snapshot jsonb NOT NULL,
  policy_digest text NOT NULL CHECK(policy_digest ~ '^[0-9a-f]{64}$'),
  checkpoint jsonb NOT NULL,
  opening_checkpoint jsonb NOT NULL,
  closed_at timestamptz,
  UNIQUE(pool_id,start_at),
  CHECK(start_at<end_at AND start_at<=cursor_at AND cursor_at<=end_at),
  CHECK(0<=issued AND issued<=scheduled AND scheduled<=budget AND budget<=18446744073709551615),
  CHECK(budget=trunc(budget) AND scheduled=trunc(scheduled) AND issued=trunc(issued)),
  CHECK((closed_at IS NOT NULL)=(cursor_at=end_at))
);
CREATE UNIQUE INDEX funded_one_open_period ON public.funded_reward_periods(pool_id) WHERE closed_at IS NULL;

CREATE TABLE public.funded_reward_batches (
  id text PRIMARY KEY CHECK(id ~ '^[0-9a-f]{64}$'),
  pool_id uuid NOT NULL REFERENCES public.funded_reward_vaults(pool_id),
  period_id uuid NOT NULL REFERENCES public.funded_reward_periods(id),
  payload jsonb NOT NULL,
  issued numeric NOT NULL CHECK(issued>=0 AND issued=trunc(issued) AND issued<=18446744073709551615),
  next_scheduled numeric NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  confirmed_at timestamptz,
  final_snapshot jsonb,
  CHECK((confirmed_at IS NULL)=(final_snapshot IS NULL))
);
CREATE UNIQUE INDEX funded_one_pending_batch ON public.funded_reward_batches(pool_id) WHERE confirmed_at IS NULL;

CREATE TABLE public.funded_reward_batch_credits (
  batch_id text NOT NULL REFERENCES public.funded_reward_batches(id),
  wallet_address text NOT NULL CHECK(wallet_address ~ '^[A-Z2-7]{58}$'),
  previous numeric NOT NULL CHECK(previous>=0 AND previous=trunc(previous)),
  cumulative numeric NOT NULL CHECK(cumulative>previous AND cumulative=trunc(cumulative) AND cumulative<=18446744073709551615),
  receipt jsonb,
  PRIMARY KEY(batch_id,wallet_address)
);

-- Persist transaction identities BEFORE broadcast, including retries that only
-- become safe once earlier validity windows expire. No private keys are stored.
CREATE TABLE public.funded_reward_attempts (
  tx_id text PRIMARY KEY CHECK(tx_id ~ '^[A-Z2-7]{52}$'),
  batch_id text NOT NULL,
  wallet_address text NOT NULL,
  attempt jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY(batch_id,wallet_address) REFERENCES public.funded_reward_batch_credits(batch_id,wallet_address)
);

CREATE FUNCTION funded_private.snapshot(s jsonb, expected_app numeric, expected_network jsonb) RETURNS void
LANGUAGE plpgsql SET search_path='' AS $$
DECLARE d numeric; a numeric; p numeric; b numeric;
BEGIN
  IF funded_private.u64(s->'app_id')<>expected_app OR funded_private.u64(s->'round')=0
     OR s->>'genesis_id' IS DISTINCT FROM expected_network->>'genesis_id'
     OR s->>'genesis_hash' IS DISTINCT FROM expected_network->>'genesis_hash'
     OR funded_private.u64(s->'active')<>1 OR funded_private.u64(s->'paused')<>0 THEN
    RAISE EXCEPTION 'Active configured-network vault snapshot required';
  END IF;
  PERFORM funded_private.at(s->'at');
  d:=funded_private.u64(s->'deposited'); a:=funded_private.u64(s->'allocated');
  p:=funded_private.u64(s->'paid'); b:=funded_private.u64(s->'balance');
  IF p>a OR a>d OR b<a-p THEN RAISE EXCEPTION 'On-chain reserve could not be verified'; END IF;
END $$;

CREATE FUNCTION funded_private.checkpoint(c jsonb, expected_at timestamptz, policy text) RETURNS void
LANGUAGE plpgsql SET search_path='' AS $$
DECLARE w record; asset record;
BEGIN
  IF funded_private.u64(c->'round')=0 OR funded_private.at(c->'at')<>expected_at
     OR funded_private.at(c->'block_at')>expected_at OR funded_private.at(c->'next_block_at')<=expected_at
     OR c->>'policy_digest' IS DISTINCT FROM policy
     OR coalesce(c->>'evidence_digest','') !~ '^[0-9a-f]{64}$'
     OR coalesce(c->>'stake_export_digest','') !~ '^[0-9a-f]{64}$'
     OR coalesce(c->>'stake_event_digest','') !~ '^[0-9a-f]{64}$'
     OR jsonb_typeof(c->'holdings') IS DISTINCT FROM 'object' THEN
    RAISE EXCEPTION 'Invalid verified holding checkpoint';
  END IF;
  PERFORM funded_private.u64(c->'stake_event_count');
  FOR w IN SELECT * FROM jsonb_each(c->'holdings') LOOP
    IF w.key !~ '^[A-Z2-7]{58}$' OR jsonb_typeof(w.value)<>'object' THEN RAISE EXCEPTION 'Invalid checkpoint wallet'; END IF;
    FOR asset IN SELECT * FROM jsonb_each(w.value) LOOP
      IF funded_private.u64(to_jsonb(asset.key))=0 THEN RAISE EXCEPTION 'Invalid checkpoint asset'; END IF;
      PERFORM funded_private.u64(asset.value);
    END LOOP;
  END LOOP;
END $$;

CREATE FUNCTION funded_private.lock_vault(p_pool uuid,p_holder uuid) RETURNS public.funded_reward_vaults
LANGUAGE plpgsql SET search_path='' AS $$
DECLARE v public.funded_reward_vaults;
BEGIN
  SELECT * INTO v FROM public.funded_reward_vaults WHERE pool_id=p_pool FOR UPDATE;
  IF NOT FOUND OR p_holder IS NULL OR v.lease_holder IS DISTINCT FROM p_holder
     OR v.lease_until<=clock_timestamp() THEN RAISE EXCEPTION 'A current publisher lease is required'; END IF;
  RETURN v;
END $$;

CREATE FUNCTION funded_private.staking_assets(p_pool uuid) RETURNS text[]
LANGUAGE plpgsql STABLE SET search_path='' AS $$
DECLARE p public.pools; result text[];
BEGIN
 SELECT * INTO p FROM public.pools WHERE id=p_pool;
 IF p.pool_type='nft staking' THEN
   SELECT ARRAY(SELECT value::text FROM unnest(c.indexed_asset_ids) value) INTO result
     FROM public.nft_collections c WHERE c.id=p.nft_collection_id AND c.is_indexed;
 ELSE result:=ARRAY[CASE WHEN p.pool_type='lp staking' THEN p.lp_token_id ELSE p.staking_token_id END]; END IF;
 IF result IS NULL OR cardinality(result)=0 OR EXISTS(SELECT 1 FROM unnest(result) a WHERE a IS NULL OR a !~ '^[1-9][0-9]{0,19}$')
   OR cardinality(result)<>(SELECT count(DISTINCT a) FROM unnest(result) a) THEN
   RAISE EXCEPTION 'A complete distinct staking asset policy is required';
 END IF;
 RETURN result;
END $$;

CREATE FUNCTION public.register_funded_reward_vault(p_pool uuid,p_config jsonb,p_opening jsonb,p_snapshot jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE v public.funded_reward_vaults; item record; total numeric:=0; app numeric;
BEGIN
  IF p_pool IS NULL OR NOT EXISTS(SELECT 1 FROM public.pools WHERE id=p_pool AND hidden IS NOT TRUE) THEN RAISE EXCEPTION 'Pool is unavailable'; END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('funded-asset-registration',0));
  PERFORM pg_advisory_xact_lock(hashtextextended('funded-register:'||p_pool::text,0));
  SELECT * INTO v FROM public.funded_reward_vaults WHERE pool_id=p_pool FOR UPDATE;
  IF FOUND THEN
    IF v.config IS DISTINCT FROM p_config OR v.opening_rewards IS DISTINCT FROM p_opening
       OR v.opening_snapshot IS DISTINCT FROM p_snapshot THEN RAISE EXCEPTION 'Vault registration is immutable'; END IF;
    RETURN jsonb_build_object('pool_id',p_pool,'app_id',v.app_id::text);
  END IF;
  PERFORM funded_private.staking_assets(p_pool);
  IF EXISTS(SELECT 1 FROM public.funded_reward_vaults other JOIN public.pools p ON p.id=other.pool_id
    WHERE other.pool_id<>p_pool AND NOT p.hidden
      AND funded_private.staking_assets(p_pool) && funded_private.staking_assets(other.pool_id)) THEN
    RAISE EXCEPTION 'Staking assets overlap another active funded pool';
  END IF;
  app:=funded_private.u64(p_config->'app_id');
  IF app=0
     OR funded_private.u64(p_config->'reward_asset_id')=0
     OR funded_private.u64(p_config->'chain_pool_id')=0
     OR coalesce(p_config->>'admin','') !~ '^[A-Z2-7]{58}$'
     OR coalesce(p_config->>'buyback','') !~ '^[A-Z2-7]{58}$'
     OR coalesce(p_config->>'genesis_id','')=''
     OR coalesce(p_config->>'genesis_hash','') !~ '^[A-Za-z0-9+/]{43}=$'
     OR coalesce(p_config->>'publisher','') !~ '^[A-Z2-7]{58}$'
     OR coalesce(p_config->>'approval_sha256','') !~ '^[0-9a-f]{64}$'
     OR coalesce(p_config->>'asset_policy_digest','') !~ '^[0-9a-f]{64}$'
     OR jsonb_typeof(p_opening) IS DISTINCT FROM 'object' THEN RAISE EXCEPTION 'Invalid vault configuration'; END IF;
  IF NOT EXISTS(SELECT 1 FROM public.pools WHERE id=p_pool AND contract_app_id=app
     AND creator_address=p_config->>'admin' AND reward_token_id=p_config->>'reward_asset_id') THEN
    RAISE EXCEPTION 'Pool metadata does not match the verified vault';
  END IF;
  PERFORM funded_private.snapshot(p_snapshot,app,p_config);
  FOR item IN SELECT * FROM jsonb_each(p_opening) LOOP
    IF item.key !~ '^[A-Z2-7]{58}$' OR funded_private.u64(item.value)=0 THEN RAISE EXCEPTION 'Invalid opening credit'; END IF;
    total:=total+funded_private.u64(item.value);
  END LOOP;
  IF total<>funded_private.u64(p_config->'opening_budget') OR total<>funded_private.u64(p_snapshot->'allocated') THEN
    RAISE EXCEPTION 'Opening credits do not match the verified allocation';
  END IF;
  INSERT INTO public.funded_reward_vaults(pool_id,app_id,config,opening_rewards,opening_snapshot,confirmed_allocated,last_confirmed_round)
  VALUES(p_pool,app,p_config,p_opening,p_snapshot,total,funded_private.u64(p_snapshot->'round'));
  INSERT INTO public.funded_reward_credits(pool_id,wallet_address,allocated)
  SELECT p_pool,key,funded_private.u64(value) FROM jsonb_each(p_opening);
  RETURN jsonb_build_object('pool_id',p_pool,'app_id',app::text);
END $$;

CREATE FUNCTION public.acquire_funded_reward_lease(p_pool uuid,p_holder uuid,p_seconds integer DEFAULT 180) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE v public.funded_reward_vaults; deadline timestamptz;
BEGIN
  IF p_holder IS NULL OR p_seconds IS NULL OR p_seconds NOT BETWEEN 30 AND 300 THEN RAISE EXCEPTION 'Invalid publisher lease'; END IF;
  SELECT * INTO v FROM public.funded_reward_vaults WHERE pool_id=p_pool FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Vault is not registered'; END IF;
  IF v.lease_holder IS NOT NULL AND v.lease_holder<>p_holder AND v.lease_until>clock_timestamp() THEN RAISE EXCEPTION 'Another publisher holds this pool'; END IF;
  deadline:=clock_timestamp()+make_interval(secs=>p_seconds);
  UPDATE public.funded_reward_vaults SET lease_holder=p_holder,lease_until=deadline WHERE pool_id=p_pool;
  RETURN jsonb_build_object('holder',p_holder,'until',deadline);
END $$;

CREATE FUNCTION public.release_funded_reward_lease(p_pool uuid,p_holder uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
BEGIN
  PERFORM funded_private.lock_vault(p_pool,p_holder);
  UPDATE public.funded_reward_vaults SET lease_holder=NULL,lease_until=NULL WHERE pool_id=p_pool;
END $$;

CREATE FUNCTION funded_private.period(p public.funded_reward_periods) RETURNS jsonb
LANGUAGE sql STABLE SET search_path='' AS $$
  SELECT jsonb_build_object('version','funded-monthly-v1','id',p.id,'pool_id',p.pool_id,
    'app_id',v.app_id::text,'start',to_char(p.start_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
    'end',to_char(p.end_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
    'cursor',to_char(p.cursor_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
    'budget_atomic',p.budget::text,'scheduled_atomic',p.scheduled::text,'issued_atomic',p.issued::text,
    'source_round',p.source_snapshot->>'round','source_digest',funded_private.hash(p.source_snapshot),
    'revision',p.revision::text,'policy_digest',p.policy_digest,
    'checkpoint',p.checkpoint,'checkpoint_digest',funded_private.hash(p.checkpoint),'closed',p.closed_at IS NOT NULL)
  FROM public.funded_reward_vaults v WHERE v.pool_id=p.pool_id
$$;

CREATE FUNCTION public.open_funded_reward_period(p_pool uuid,p_holder uuid,p_snapshot jsonb,p_policy text,p_checkpoint jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE v public.funded_reward_vaults; p public.funded_reward_periods;
  starts timestamptz; ends timestamptz; budget numeric;
BEGIN
  v:=funded_private.lock_vault(p_pool,p_holder);
  SELECT * INTO p FROM public.funded_reward_periods WHERE pool_id=p_pool AND closed_at IS NULL;
  IF FOUND THEN
    IF p.source_snapshot IS NOT DISTINCT FROM p_snapshot AND p.policy_digest IS NOT DISTINCT FROM p_policy
       AND p.opening_checkpoint IS NOT DISTINCT FROM p_checkpoint THEN RETURN funded_private.period(p); END IF;
    RAISE EXCEPTION 'Finish the existing period before opening another';
  END IF;
  PERFORM funded_private.snapshot(p_snapshot,v.app_id,v.config);
  starts:=funded_private.at(p_snapshot->'at');
  IF starts>clock_timestamp() OR starts<funded_private.at(v.opening_snapshot->'at')
     OR funded_private.u64(p_snapshot->'round')<v.last_confirmed_round
     OR funded_private.u64(p_snapshot->'allocated')<>v.confirmed_allocated THEN
    RAISE EXCEPTION 'Snapshot is stale or allocations disagree with the ledger';
  END IF;
  IF p_policy IS NULL OR p_policy !~ '^[0-9a-f]{64}$' OR p_policy IS DISTINCT FROM v.config->>'asset_policy_digest' THEN RAISE EXCEPTION 'Invalid asset policy digest'; END IF;
  PERFORM funded_private.checkpoint(p_checkpoint,starts,p_policy);
  IF funded_private.u64(p_checkpoint->'round')<>funded_private.u64(p_snapshot->'round') THEN RAISE EXCEPTION 'Checkpoint round differs'; END IF;
  SELECT * INTO p FROM public.funded_reward_periods WHERE pool_id=p_pool ORDER BY start_at DESC LIMIT 1;
  IF FOUND AND (starts<p.end_at OR funded_private.u64(p_checkpoint->'round')<funded_private.u64(p.checkpoint->'round')
    OR p_checkpoint->>'previous_checkpoint_digest' IS DISTINCT FROM funded_private.hash(p.checkpoint)) THEN
    RAISE EXCEPTION 'Previous period or holding checkpoint is not covered';
  END IF;
  ends:=(date_trunc('month',starts AT TIME ZONE 'UTC')+interval '1 month') AT TIME ZONE 'UTC';
  budget:=least(funded_private.u64(p_snapshot->'deposited')-v.confirmed_allocated,
    funded_private.u64(p_snapshot->'balance')-(v.confirmed_allocated-funded_private.u64(p_snapshot->'paid')));
  INSERT INTO public.funded_reward_periods(pool_id,start_at,end_at,cursor_at,budget,source_snapshot,policy_digest,checkpoint,opening_checkpoint)
  VALUES(p_pool,starts,ends,starts,budget,p_snapshot,p_policy,p_checkpoint,p_checkpoint) RETURNING * INTO p;
  RETURN funded_private.period(p);
END $$;

CREATE FUNCTION public.seal_funded_reward_batch(p_pool uuid,p_holder uuid,p_payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE v public.funded_reward_vaults; p public.funded_reward_periods; b public.funded_reward_batches;
  batch_hash text; until_at timestamptz; next_scheduled numeric; emission numeric;
  item record; total numeric:=0; delta numeric; previous numeric; credits jsonb;
BEGIN
  v:=funded_private.lock_vault(p_pool,p_holder);
  IF jsonb_typeof(p_payload) IS DISTINCT FROM 'object' THEN RAISE EXCEPTION 'Invalid batch'; END IF;
  batch_hash:=funded_private.hash(p_payload);
  SELECT * INTO b FROM public.funded_reward_batches WHERE id=batch_hash;
  IF FOUND THEN
    IF b.pool_id<>p_pool OR b.payload<>p_payload THEN RAISE EXCEPTION 'Batch identity differs'; END IF;
  ELSE
    IF EXISTS(SELECT 1 FROM public.funded_reward_batches WHERE pool_id=p_pool AND confirmed_at IS NULL) THEN
      RAISE EXCEPTION 'Resume the sealed batch before calculating another';
    END IF;
    SELECT * INTO p FROM public.funded_reward_periods WHERE id=(p_payload->>'period_id')::uuid AND pool_id=p_pool AND closed_at IS NULL;
    IF NOT FOUND THEN RAISE EXCEPTION 'Open period not found'; END IF;
    until_at:=funded_private.at(p_payload->'end');
    IF funded_private.u64(p_payload->'revision')<>p.revision
       OR funded_private.at(p_payload->'start')<>p.cursor_at OR until_at<=p.cursor_at OR until_at>p.end_at OR until_at>clock_timestamp()
       OR p_payload->>'checkpoint_before_digest' IS DISTINCT FROM funded_private.hash(p.checkpoint)
       OR coalesce(p_payload->>'eligibility_digest','') !~ '^[0-9a-f]{64}$'
       OR coalesce(p_payload->>'calculation_digest','') !~ '^[0-9a-f]{64}$'
       OR coalesce(p_payload->>'evidence_digest','') !~ '^[0-9a-f]{64}$'
       OR jsonb_typeof(p_payload->'new_rewards') IS DISTINCT FROM 'object' THEN
      RAISE EXCEPTION 'Batch cursor, revision or evidence differs';
    END IF;
    PERFORM funded_private.checkpoint(p_payload->'checkpoint_after',until_at,p.policy_digest);
    IF funded_private.u64(p_payload->'checkpoint_after'->'round')<funded_private.u64(p.checkpoint->'round') THEN RAISE EXCEPTION 'Holding rounds cannot go backwards'; END IF;
    -- A later checkpoint must retain the coverage of every previously observed
    -- wallet, including exits. New wallet histories are verified by the reader.
    IF EXISTS(SELECT 1 FROM jsonb_object_keys(p.checkpoint->'holdings') AS w(address)
      WHERE NOT (p_payload->'checkpoint_after'->'holdings' ? w.address)) THEN RAISE EXCEPTION 'Holding checkpoint lost wallet coverage'; END IF;
    next_scheduled:=div(p.budget*(extract(epoch FROM until_at-p.start_at)*1000000),extract(epoch FROM p.end_at-p.start_at)*1000000);
    emission:=next_scheduled-p.scheduled;
    FOR item IN SELECT * FROM jsonb_each(p_payload->'new_rewards') LOOP
      delta:=funded_private.u64(item.value);
      IF item.key !~ '^[A-Z2-7]{58}$' OR delta=0 THEN RAISE EXCEPTION 'Invalid batch credit'; END IF;
      total:=total+delta;
    END LOOP;
    IF funded_private.u64(p_payload->'scheduled_atomic')<>emission OR total<>funded_private.u64(p_payload->'issued_atomic')
       OR total>emission OR p.issued+total>next_scheduled OR v.confirmed_allocated+total>18446744073709551615 THEN
      RAISE EXCEPTION 'Batch exceeds its funded period or does not conserve rewards';
    END IF;
    INSERT INTO public.funded_reward_batches(id,pool_id,period_id,payload,issued,next_scheduled)
    VALUES(batch_hash,p_pool,p.id,p_payload,total,next_scheduled) RETURNING * INTO b;
    FOR item IN SELECT * FROM jsonb_each(p_payload->'new_rewards') LOOP
      SELECT allocated INTO previous FROM public.funded_reward_credits WHERE pool_id=p_pool AND wallet_address=item.key;
      previous:=coalesce(previous,0); delta:=funded_private.u64(item.value);
      INSERT INTO public.funded_reward_batch_credits(batch_id,wallet_address,previous,cumulative)
      VALUES(batch_hash,item.key,previous,previous+delta);
    END LOOP;
  END IF;
  SELECT coalesce(jsonb_agg(jsonb_build_object('wallet',c.wallet_address,'previous',c.previous::text,'cumulative',c.cumulative::text,'receipt',c.receipt) ORDER BY c.wallet_address),'[]'::jsonb)
  INTO credits FROM public.funded_reward_batch_credits c WHERE c.batch_id=b.id;
  RETURN jsonb_build_object('id',b.id,'payload',b.payload,'confirmed',b.confirmed_at IS NOT NULL,'credits',credits);
END $$;

CREATE FUNCTION public.record_funded_reward_attempt(p_pool uuid,p_holder uuid,p_batch text,p_wallet text,p_attempt jsonb,p_observed_round text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE b public.funded_reward_batches; c public.funded_reward_batch_credits;
  a public.funded_reward_attempts; first_round numeric; last_round numeric; observed numeric;
BEGIN
  PERFORM funded_private.lock_vault(p_pool,p_holder);
  SELECT * INTO b FROM public.funded_reward_batches WHERE id=p_batch AND pool_id=p_pool;
  IF NOT FOUND THEN RAISE EXCEPTION 'Sealed batch not found'; END IF;
  SELECT * INTO c FROM public.funded_reward_batch_credits WHERE batch_id=p_batch AND wallet_address=p_wallet;
  IF NOT FOUND THEN RAISE EXCEPTION 'Sealed credit not found'; END IF;
  IF coalesce(p_attempt->>'tx_id','') !~ '^[A-Z2-7]{52}$'
    OR coalesce(p_attempt->>'unsigned_transaction','') !~ '^[A-Za-z0-9+/]+={0,2}$'
    OR length(p_attempt->>'unsigned_transaction')>32768 THEN RAISE EXCEPTION 'Invalid transaction identity'; END IF;
  first_round:=funded_private.u64(p_attempt->'first_valid'); last_round:=funded_private.u64(p_attempt->'last_valid');
  observed:=funded_private.u64(to_jsonb(p_observed_round));
  IF first_round<funded_private.u64(b.payload->'checkpoint_after'->'round') OR last_round<first_round
     OR last_round-first_round>1000 OR observed<first_round OR observed>last_round THEN RAISE EXCEPTION 'Invalid transaction validity window'; END IF;
  SELECT * INTO a FROM public.funded_reward_attempts WHERE tx_id=p_attempt->>'tx_id';
  IF FOUND THEN
    IF a.batch_id<>p_batch OR a.wallet_address<>p_wallet OR a.attempt<>p_attempt THEN RAISE EXCEPTION 'Transaction identity is immutable'; END IF;
    RETURN;
  END IF;
  IF b.confirmed_at IS NOT NULL OR c.receipt IS NOT NULL THEN RAISE EXCEPTION 'Credit already confirmed'; END IF;
  IF EXISTS(SELECT 1 FROM public.funded_reward_attempts WHERE batch_id=p_batch AND wallet_address=p_wallet
    AND funded_private.u64(attempt->'last_valid')>=observed) THEN
    RAISE EXCEPTION 'Resolve the existing transaction before preparing a retry';
  END IF;
  INSERT INTO public.funded_reward_attempts(tx_id,batch_id,wallet_address,attempt) VALUES(p_attempt->>'tx_id',p_batch,p_wallet,p_attempt);
END $$;

CREATE FUNCTION public.confirm_funded_reward_credit(p_pool uuid,p_holder uuid,p_batch text,p_wallet text,p_receipt jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE v public.funded_reward_vaults; c public.funded_reward_batch_credits; a public.funded_reward_attempts;
  current_amount numeric; confirmation_round numeric; box_round numeric;
BEGIN
  v:=funded_private.lock_vault(p_pool,p_holder);
  IF NOT EXISTS(SELECT 1 FROM public.funded_reward_batches WHERE id=p_batch AND pool_id=p_pool) THEN RAISE EXCEPTION 'Sealed batch not found'; END IF;
  SELECT * INTO c FROM public.funded_reward_batch_credits WHERE batch_id=p_batch AND wallet_address=p_wallet;
  IF NOT FOUND THEN RAISE EXCEPTION 'Sealed credit not found'; END IF;
  IF c.receipt IS NOT NULL THEN
    IF c.receipt IS DISTINCT FROM p_receipt THEN RAISE EXCEPTION 'Confirmation is immutable'; END IF;
    RETURN;
  END IF;
  SELECT * INTO a FROM public.funded_reward_attempts WHERE tx_id=p_receipt->>'tx_id' AND batch_id=p_batch AND wallet_address=p_wallet;
  IF NOT FOUND THEN RAISE EXCEPTION 'Transaction was not recorded before submission'; END IF;
  confirmation_round:=funded_private.u64(p_receipt->'confirmed_round'); box_round:=funded_private.u64(p_receipt->'box_round');
  IF funded_private.u64(p_receipt->'app_id')<>v.app_id OR p_receipt->>'wallet' IS DISTINCT FROM p_wallet
    OR funded_private.u64(p_receipt->'allocated')<>c.cumulative OR funded_private.u64(p_receipt->'paid')>c.cumulative
    OR confirmation_round<funded_private.u64(a.attempt->'first_valid') OR confirmation_round>funded_private.u64(a.attempt->'last_valid')
    OR box_round<confirmation_round OR coalesce(p_receipt->>'transaction_digest','') !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'Confirmed transaction or reward box differs from sealed credit';
  END IF;
  SELECT allocated INTO current_amount FROM public.funded_reward_credits WHERE pool_id=p_pool AND wallet_address=p_wallet;
  IF coalesce(current_amount,0)<>c.previous THEN RAISE EXCEPTION 'Confirmed wallet credits disagree with the ledger'; END IF;
  INSERT INTO public.funded_reward_credits(pool_id,wallet_address,allocated) VALUES(p_pool,p_wallet,c.cumulative)
  ON CONFLICT(pool_id,wallet_address) DO UPDATE SET allocated=EXCLUDED.allocated;
  UPDATE public.funded_reward_batch_credits SET receipt=p_receipt WHERE batch_id=p_batch AND wallet_address=p_wallet;
  UPDATE public.funded_reward_vaults SET confirmed_allocated=confirmed_allocated+c.cumulative-c.previous,
    last_confirmed_round=greatest(last_confirmed_round,box_round) WHERE pool_id=p_pool;
END $$;

CREATE FUNCTION public.finish_funded_reward_batch(p_pool uuid,p_holder uuid,p_batch text,p_snapshot jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE v public.funded_reward_vaults; p public.funded_reward_periods; b public.funded_reward_batches;
  until_at timestamptz; end_round numeric;
BEGIN
  v:=funded_private.lock_vault(p_pool,p_holder);
  SELECT * INTO b FROM public.funded_reward_batches WHERE id=p_batch AND pool_id=p_pool;
  IF NOT FOUND THEN RAISE EXCEPTION 'Sealed batch not found'; END IF;
  SELECT * INTO p FROM public.funded_reward_periods WHERE id=b.period_id;
  IF b.confirmed_at IS NOT NULL THEN RETURN funded_private.period(p); END IF;
  IF EXISTS(SELECT 1 FROM public.funded_reward_batch_credits WHERE batch_id=p_batch AND receipt IS NULL) THEN
    RAISE EXCEPTION 'Not all sealed credits have confirmed';
  END IF;
  PERFORM funded_private.snapshot(p_snapshot,v.app_id,v.config);
  until_at:=funded_private.at(b.payload->'end'); end_round:=funded_private.u64(b.payload->'checkpoint_after'->'round');
  IF funded_private.u64(p_snapshot->'round')<greatest(v.last_confirmed_round,end_round)
     OR funded_private.at(p_snapshot->'at')<until_at OR funded_private.u64(p_snapshot->'allocated')<>v.confirmed_allocated
     OR (SELECT coalesce(sum(allocated),0) FROM public.funded_reward_credits WHERE pool_id=p_pool)<>v.confirmed_allocated
     OR p.revision<>funded_private.u64(b.payload->'revision') OR p.cursor_at<>funded_private.at(b.payload->'start') THEN
    RAISE EXCEPTION 'Final chain snapshot or accounting cursor disagrees';
  END IF;
  UPDATE public.funded_reward_batches SET confirmed_at=clock_timestamp(),final_snapshot=p_snapshot WHERE id=p_batch;
  UPDATE public.funded_reward_periods SET cursor_at=until_at,scheduled=b.next_scheduled,issued=issued+b.issued,
    checkpoint=b.payload->'checkpoint_after',revision=revision+1,
    closed_at=CASE WHEN until_at=end_at THEN clock_timestamp() ELSE NULL END WHERE id=p.id RETURNING * INTO p;
  UPDATE public.funded_reward_vaults SET last_confirmed_round=funded_private.u64(p_snapshot->'round') WHERE pool_id=p_pool;
  RETURN funded_private.period(p);
END $$;

-- Exact decimal strings are returned even above JavaScript's integer limit.
-- Reads are service-only; public claim/status endpoints verify chain state.
CREATE FUNCTION public.get_funded_reward_state(p_pool uuid) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path='' AS $$
DECLARE v public.funded_reward_vaults; p public.funded_reward_periods; b public.funded_reward_batches;
  credits jsonb; pending_credits jsonb;
BEGIN
  SELECT * INTO v FROM public.funded_reward_vaults WHERE pool_id=p_pool;
  IF NOT FOUND THEN RAISE EXCEPTION 'Vault is not registered'; END IF;
  SELECT * INTO p FROM public.funded_reward_periods WHERE pool_id=p_pool ORDER BY start_at DESC LIMIT 1;
  SELECT * INTO b FROM public.funded_reward_batches WHERE pool_id=p_pool AND confirmed_at IS NULL;
  SELECT coalesce(jsonb_object_agg(wallet_address,allocated::text),'{}'::jsonb) INTO credits
    FROM public.funded_reward_credits WHERE pool_id=p_pool;
  SELECT coalesce(jsonb_agg(jsonb_build_object('wallet',c.wallet_address,'previous',c.previous::text,'cumulative',c.cumulative::text,
    'receipt',c.receipt,'attempts',(SELECT coalesce(jsonb_agg(a.attempt ORDER BY a.created_at,a.tx_id),'[]'::jsonb)
      FROM public.funded_reward_attempts a WHERE a.batch_id=c.batch_id AND a.wallet_address=c.wallet_address)) ORDER BY c.wallet_address),'[]'::jsonb)
    INTO pending_credits FROM public.funded_reward_batch_credits c WHERE c.batch_id=b.id;
  RETURN jsonb_build_object('config',v.config,'confirmed_allocated',v.confirmed_allocated::text,
    'last_confirmed_round',v.last_confirmed_round::text,'credits',credits,
    'period',CASE WHEN p.id IS NULL THEN NULL ELSE funded_private.period(p) END,
    'pending',CASE WHEN b.id IS NULL THEN NULL ELSE jsonb_build_object('id',b.id,'payload',b.payload,'credits',pending_credits) END);
END $$;

CREATE FUNCTION public.get_funded_stake_export(p_pool uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' SET timezone='UTC' SET lock_timeout='5s' AS $$
DECLARE exported_at timestamptz; result jsonb;
BEGIN
  IF NOT EXISTS(SELECT 1 FROM public.funded_reward_vaults WHERE pool_id=p_pool) THEN RAISE EXCEPTION 'Vault is not registered'; END IF;
  -- Wait for in-flight stake mutations and their journal entries to commit,
  -- then choose the timestamp. No reward allocation is performed by this RPC.
  LOCK TABLE public.user_stakes IN SHARE MODE;
  exported_at:=clock_timestamp();
  SELECT jsonb_build_object('mode','funded_stake_export','exported_at',exported_at,'pools',jsonb_build_array(
    jsonb_build_object('pool_id',p.id,'name',p.pool_name,'app_id',p.contract_app_id::text,
      'nft_pool',p.pool_type='nft staking','nft_indexed',c.is_indexed,
      'asset_ids',CASE WHEN p.pool_type='nft staking' THEN to_jsonb(c.indexed_asset_ids)
        WHEN p.pool_type='lp staking' THEN jsonb_build_array(p.lp_token_id)
        ELSE jsonb_build_array(p.staking_token_id) END,
      'decimals',CASE WHEN p.pool_type='nft staking' THEN 0 ELSE p.staking_token_decimals END,
      'stake_events',coalesce((SELECT jsonb_agg(to_jsonb(e)||jsonb_build_object('id',e.id::text,
        'amount_before',e.amount_before::text,'amount_after',e.amount_after::text) ORDER BY e.occurred_at,e.id)
        FROM public.stake_events e WHERE e.pool_id=p.id AND e.occurred_at<=exported_at),'[]'::jsonb))))
    INTO result FROM public.pools p LEFT JOIN public.nft_collections c ON c.id=p.nft_collection_id
    WHERE p.id=p_pool AND p.hidden IS NOT TRUE;
  IF result IS NULL THEN RAISE EXCEPTION 'Pool is unavailable'; END IF;
  RETURN result;
END $$;

DO $$
DECLARE object_name text;
BEGIN
  FOREACH object_name IN ARRAY ARRAY['funded_reward_vaults','funded_reward_credits','funded_reward_periods',
    'funded_reward_batches','funded_reward_batch_credits','funded_reward_attempts'] LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY',object_name);
    EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC,anon,authenticated,service_role',object_name);
    EXECUTE format('GRANT SELECT ON public.%I TO service_role',object_name);
  END LOOP;
  FOR object_name IN SELECT p.oid::regprocedure::text FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
    WHERE n.nspname='public' AND p.proname IN ('register_funded_reward_vault','acquire_funded_reward_lease','release_funded_reward_lease',
      'open_funded_reward_period','seal_funded_reward_batch','record_funded_reward_attempt','confirm_funded_reward_credit',
      'finish_funded_reward_batch','get_funded_reward_state','get_funded_stake_export') LOOP
    EXECUTE 'REVOKE ALL ON FUNCTION '||object_name||' FROM PUBLIC,anon,authenticated,service_role';
    EXECUTE 'GRANT EXECUTE ON FUNCTION '||object_name||' TO service_role';
  END LOOP;
END $$;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA funded_private FROM PUBLIC,anon,authenticated,service_role;
