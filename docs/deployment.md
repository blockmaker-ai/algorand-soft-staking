# Deployment and operations

## 1. Build and test

Run the quick-start checks in the README. `npm run build` compiles both contracts with the pinned Puya toolchain and generates their typed clients. Generated clients are committed; CI compares them with a fresh build. Deploy `FundedRewards` for new pools.

`scripts/deploy-localnet.ts` is an executable, loopback-only deployment example. A real deployment uses the same typed factory calls with an explicitly configured node and wallet signer. The creation arguments are:

```typescript
await factory.send.create.create({ args: {
  rewardToken: rewardAssetId,
  poolId: uniqueNumericPoolId,
  publisher: publisherAddress,
  buyback: fundingOperatorAddress,
  openingBudget: 0n,
}})
```

Use `openingBudget: 0` for a fresh vault. Fund its ALGO minimum balance, call `optInAsset`, and call `activate`. Register reward funding with one atomic group containing an ASA transfer from the funding operator immediately followed by `fundBuyback`. Its name does not require a buyback tool: a treasury or another funding service can act as that operator. Direct transfers are not automatically recognised.

The vault needs the account/ASA minimum balances plus 22,100 microALGO for each new reward recipient box. The publisher also needs ALGO for its application-call fees. Existing credits do not expire and their boxes cannot be deleted.

## 2. Create the database

Use a **fresh Supabase project** for the schema supplied here. Link your own project, inspect the migrations, then apply them:

```bash
supabase link --project-ref YOUR_PROJECT_REF
supabase db push
```

The migrations create pool metadata, a signed stake journal, a service-only funded ledger and the private `funded-reward-evidence` bucket. They contain no deployment data. Public roles can read visible pool and collection metadata; they cannot change stakes or reward accounting.

Create your project’s pool rows through an operator connection. Each row needs its own UUID and:

- `pool_name`, `pool_type` (`single token staking`, `lp staking` or `nft staking`), and `creator_address`.
- The deployed `contract_app_id` and exact `reward_token_id`, `reward_token_decimals` and display symbol.
- A staking ASA in `staking_token_id`, or an LP ASA in `lp_token_id`, with its actual decimals.
- For NFTs, an indexed `nft_collections` row with the verified ASA IDs and `is_indexed=true`; the pool references that collection.
- Optional decimal-text `min_stake` and `max_stake` (`0` means no limit), plus any `reward_stake_cap` and `reward_stake_cap_from`.

Keep the pool pending until registration completes. Registration rejects overlapping staking asset sets between visible funded pools; a wallet must not earn from the same asset through overlapping pool definitions. An ASA used as an LP token is treated as a fungible holding; reserve valuation is a frontend concern.

## 3. Configure your deployment

Copy `.env.example` to `.env` and fill it locally. Supply exact node URLs, node authentication headers if required, the chosen network’s genesis ID/hash, your Supabase service-role credential, and exact frontend origins. Obtain network identity from that node’s `/v2/transactions/params` endpoint; the clients verify it rather than trusting the URL alone.

The `deployment.example.json` template describes the **public** vault identity. Save real configuration in an ignored `deployment.json`. `approval_sha256` is the SHA-256 of the compiled approval bytecode, not its TEAL source. `FundedChain` also compares the actual deployed approval/clear programs, creator and global settings against the generated release.

The local demo prints this configuration. For another network, record it from your own deployment. No credentials belong in this JSON.

Register a fresh, active vault before allocating any rewards:

```bash
# .venv is active so the policy digest uses the installed Python SDK.
node --env-file=.env --import tsx scripts/register-vault.ts deployment.json
node --env-file=.env --import tsx scripts/register-vault.ts deployment.json --apply
```

The first command previews. The second records the verified opening snapshot and policy digest, then marks the pool active. It requires an empty credit ledger and zero opening budget. It does not deploy, fund, sign for or change a contract.

The staking asset policy is pinned in the vault registration. The scheduler rejects changes to its assets, decimals or reward cap. Maximum *new stake* settings can be adjusted independently; lowering one leaves larger existing registrations in place and still permits withdrawals.

## 4. Deploy the API

Configure server secrets with Supabase’s secret store. Supabase provides `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` to Edge Functions; do not try to publish those as frontend variables. Set your node/network settings, `FRONTEND_ORIGINS`, `PUBLISHER_MNEMONIC` and `FUNDED_REWARDS_PUBLICATION_ENABLED=false` separately.

The publisher mnemonic belongs only in the Edge secret store. It must derive the configured publisher address. It is not needed by the Python scheduler or a frontend. This reference endpoint supports one publisher account across its configured pools.

```bash
supabase functions deploy pool-action-challenge
supabase functions deploy manage-stake
supabase functions deploy funded-pools
supabase functions deploy funded-publish
```

`supabase/config.toml` disables gateway JWT validation for these functions deliberately: stake writes use wallet proofs, status and unsigned claims are public reads, and `funded-publish` independently requires the exact service credential. An anonymous/publishable Supabase key does not authorise publication.

Local Docker-hosted functions need node URLs reachable from their container, not the host’s loopback address. For a hosted deployment, use HTTPS providers with complete Algod/Indexer history on the same network.

## 5. Preview, then enable rewards

```bash
# Read-only remote preview; writes local evidence to the ignored output directory.
python -m dotenv -f .env run -- python -B scripts/run_funded_rewards.py \
  --pool-id YOUR_POOL_UUID --output-dir ./evidence
```

Repeat `--pool-id` for multiple pools. The preview verifies chain identity, registered deposits, stake events, holding history, checkpoints and exact reward conservation. Review its proposed start/budget or allocation before enabling publication.

Set `FUNDED_REWARDS_PUBLICATION_ENABLED=true` in the Edge secret store **and** the scheduler environment, then add `--publish` to the same command. An initial run opens the funded period at the verified snapshot; a subsequent run publishes accrued rewards. Rewards never backdate to before that snapshot.

The publisher records each unsigned transaction and validity window before signing. After an ambiguous response, rerun the same command: it checks the persisted attempt and resumes the sealed batch. Do not delete pending rows, alter cumulative amounts or create a fresh allocation to bypass a timeout. Private Storage evidence must remain available for retries.

The scheduler uses a calendar-month budget. New funding during a positive-budget period belongs to the next period. A zero-budget placeholder can close only after its elapsed zero-reward history is verified, allowing fresh funding to start now.

## GitHub Actions

`verify.yml` runs contract, PostgreSQL, wallet, API and accounting checks without deployment secrets. `rewards.yml` is a reference operator workflow. It has no configured project, pools or credentials in this repository.

For your own deployment, configure `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `ALGOD_TOKEN` and `INDEXER_TOKEN` as repository/environment secrets as needed. Configure node URLs, token-header names, `ALGORAND_GENESIS_ID`, `ALGORAND_GENESIS_HASH`, `FUNDED_POOL_IDS` (a JSON array of UUIDs), and `FUNDED_REWARDS_PUBLICATION_ENABLED` as variables. Run a manual preview first. The daily schedule is gated by the publication variable; the Edge Function retains its independent gate.

A public repository’s workflow logs are public. The workflow prints aggregate run status only and deliberately does not upload wallet-history evidence as an Actions artifact. Durable evidence remains in private Supabase Storage.

## Existing deployments

The schema files are a fresh-install definition, not a universal migration for existing databases. Back up and compare an existing schema before applying a compatible migration. Preserve stake registrations, stake-event history, confirmed credits and transaction receipts.

The funded contract is immutable. An existing Merkle contract cannot acquire these enforcement rules through a repository pull. An owner-authorised transition uses a new vault and a reviewed, funded opening allocation; `assertMigrationSource` and the LocalNet migration tests show the atomic source-check/retirement/deposit mechanism for the compatible legacy contract. Existing tokens stay in users’ wallets. Historical entitlements and available reserves require reconciliation; no automatic haircut, treasury transfer or production migration is supplied here.
