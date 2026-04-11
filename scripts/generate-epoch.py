#!/usr/bin/env python3
"""
Generate Epoch - Mainnet Reward Distribution Script

This script:
1. Queries all active pools and their stakers
2. Calculates rewards based on pool settings (daily/weekly distribution)
3. Generates Merkle trees with cumulative rewards
4. Stores claims in merkle_epoch_claims table
5. Optionally publishes epoch roots to smart contracts

Usage:
  python scripts/generate-epoch.py                    # Generate for all pools
  python scripts/generate-epoch.py --pool-id UUID    # Generate for specific pool
  python scripts/generate-epoch.py --dry-run         # Calculate but don't save
  python scripts/generate-epoch.py --publish         # Also publish roots on-chain
"""

import os
import sys
import json
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib

# Add parent directory to path for merkle_utils import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'contracts', 'pyteal'))

try:
    from merkle_utils import MerkleTree
except ImportError:
    print("❌ Could not import merkle_utils. Make sure contracts/pyteal/merkle_utils.py exists")
    sys.exit(1)

# Load .env from project root
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
except ImportError:
    pass  # dotenv optional — fall back to shell environment

# Supabase connection
from supabase import create_client, Client

SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_SERVICE_ROLE_KEY') or os.getenv('SERVICE_ROLE')

if not SUPABASE_URL:
    print("❌ SUPABASE_URL environment variable is required")
    print("   Copy .env.example to .env and fill in your Supabase project URL")
    sys.exit(1)

if not SUPABASE_KEY:
    print("❌ SUPABASE_SERVICE_ROLE_KEY (or SERVICE_ROLE) environment variable is required")
    print("   This must be the service role key — the anon key does not have write access")
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Algorand node for fetching contract state
ALGOD_URL = os.getenv('ALGOD_URL', 'https://mainnet-api.algonode.cloud')


def get_contract_pool_id(app_id):
    """Fetch the pool_id from the contract's global state on Algorand.

    CRITICAL: The pool_id stored in the contract MUST be used when building
    merkle trees, otherwise the leaf hashes won't match and claims will fail.
    """
    import urllib.request
    import base64

    try:
        url = f"{ALGOD_URL}/v2/applications/{app_id}"
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read().decode())

        global_state = data.get('params', {}).get('global-state', [])

        for item in global_state:
            key = base64.b64decode(item.get('key', '')).decode('utf-8', errors='replace')
            if key == 'pool_id':
                pool_id = item.get('value', {}).get('uint', 0)
                print(f"   📦 Contract pool_id: {pool_id}")
                return pool_id

        print(f"   ⚠️ No pool_id found in contract {app_id} global state")
        return None

    except Exception as e:
        print(f"   ❌ Error fetching contract pool_id: {e}")
        return None


def get_contract_reward_balance(app_id, reward_token_id):
    """Fetch the contract's reward token balance from Algorand.

    For monthly rolling pools, the rewards are deposited directly to the contract
    rather than stored in the database. This function checks the actual on-chain balance.
    """
    import urllib.request

    try:
        # Calculate application address
        # app_address = algosdk.logic.get_application_address(app_id)
        # Using raw calculation to avoid algosdk import issues
        import hashlib
        app_id_bytes = app_id.to_bytes(8, 'big')
        prefix = b'appID'
        addr_bytes = hashlib.new('sha512_256', prefix + app_id_bytes).digest()

        # Convert to Algorand address format
        import base64
        # We need the raw 32 bytes for the API call
        # Use base32 encoding for the address
        ALGORAND_CHECKSUM_BYTE_LENGTH = 4
        checksum = hashlib.new('sha512_256', addr_bytes).digest()[-ALGORAND_CHECKSUM_BYTE_LENGTH:]
        addr_with_checksum = addr_bytes + checksum
        app_address = base64.b32encode(addr_with_checksum).decode('utf-8').rstrip('=')

        url = f"{ALGOD_URL}/v2/accounts/{app_address}"
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read().decode())

        assets = data.get('assets', [])
        for asset in assets:
            if asset.get('asset-id') == int(reward_token_id):
                balance = asset.get('amount', 0)
                print(f"   💰 Contract reward balance: {balance:,} (token {reward_token_id})")
                return balance

        print(f"   ⚠️ Reward token {reward_token_id} not found in contract")
        return 0

    except Exception as e:
        print(f"   ❌ Error fetching contract reward balance: {e}")
        return 0


def get_active_pools(pool_id=None):
    """Get all active funded pools, or a specific pool"""
    query = supabase.table('pools').select('*').eq('funding_confirmed', True).eq('hidden', False)

    if pool_id:
        query = query.eq('id', pool_id)

    response = query.execute()
    return response.data or []


def get_pool_stakes(pool_id):
    """Get all active stakes for a pool"""
    response = supabase.table('user_stakes').select('*').eq('pool_id', pool_id).eq('is_active', True).execute()
    return response.data or []


def get_latest_epoch(pool_id):
    """Get the latest epoch ID for a pool from database"""
    response = supabase.table('merkle_epoch_claims').select('epoch_id').eq('pool_id', pool_id).order('epoch_id', desc=True).limit(1).execute()

    if response.data and len(response.data) > 0:
        return response.data[0]['epoch_id']
    return 0


def get_on_chain_epoch(app_id):
    """Get the current epoch ID from the contract's global state.

    CRITICAL: Used to ensure we don't generate an epoch that can't be published.
    If epoch N is already published on-chain, the next epoch must be N+1 or higher.
    """
    import urllib.request

    try:
        url = f"{ALGOD_URL}/v2/applications/{app_id}"
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read().decode())

        global_state = data.get('params', {}).get('global-state', [])

        for item in global_state:
            import base64
            key = base64.b64decode(item.get('key', '')).decode('utf-8', errors='replace')
            if key == 'epoch_id':
                return item.get('value', {}).get('uint', 0)

        return 0  # No epoch published yet

    except Exception as e:
        print(f"   ⚠️ Could not fetch on-chain epoch: {e}")
        return 0


def get_last_epoch_time(pool_id):
    """Get the timestamp of the last epoch generation for a pool"""
    response = supabase.table('merkle_epoch_claims').select('created_at').eq('pool_id', pool_id).order('created_at', desc=True).limit(1).execute()

    if response.data and len(response.data) > 0:
        created_at = response.data[0]['created_at']
        if created_at:
            # Parse the timestamp
            created_at = created_at.replace('Z', '+00:00')
            if '+' not in created_at and 'T' in created_at:
                created_at = created_at + '+00:00'
            try:
                dt = datetime.fromisoformat(created_at)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except:
                pass
    return None


def should_generate_epoch(pool, now=None):
    """
    Check if we should generate an epoch for this pool based on its distribution schedule.

    Returns: (should_generate: bool, reason: str)
    """
    if now is None:
        now = datetime.now(timezone.utc)

    pool_id = pool['id']
    pool_name = pool['pool_name']
    distribution = pool.get('reward_distribution', 'daily')

    # Parse pool start date
    start_date_str = pool.get('start_date')
    if start_date_str:
        start_date_str = start_date_str.replace('Z', '+00:00')
        if '+' not in start_date_str and 'T' in start_date_str:
            start_date_str = start_date_str + '+00:00'
        try:
            start_date = datetime.fromisoformat(start_date_str)
            if start_date.tzinfo is None:
                start_date = start_date.replace(tzinfo=timezone.utc)
        except:
            start_date = None
    else:
        start_date = None

    # Parse pool end date
    end_date_str = pool.get('end_date')
    end_date = None
    if end_date_str:
        end_date_str = end_date_str.replace('Z', '+00:00')
        if '+' not in end_date_str and 'T' in end_date_str:
            end_date_str = end_date_str + '+00:00'
        try:
            end_date = datetime.fromisoformat(end_date_str)
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)
        except:
            pass

    last_epoch_time = get_last_epoch_time(pool_id)

    # For one-time pools with an end_date: once the pool has ended AND the final
    # epoch has already been generated (last_epoch_time >= end_date), stop.
    # This prevents the cron from generating endless zero-reward epochs post-end.
    # Monthly rolling pools have no fixed end_date so this check is skipped for them.
    funding_model = pool.get('funding_model', 'one-time pool')
    if end_date and now > end_date and funding_model != 'monthly rolling pool':
        if last_epoch_time is not None and last_epoch_time >= end_date:
            return False, f"Pool ended on {end_date.date()} — final epoch already generated on {last_epoch_time.date()}"
        # If last_epoch_time < end_date (or no epochs yet), fall through to generate the final epoch

    if last_epoch_time is None:
        # No epochs yet - check if pool has been active long enough
        # CRITICAL: Don't generate epochs for brand new pools!
        if start_date:
            time_since_start = now - start_date
            hours_since_start = time_since_start.total_seconds() / 3600

            if distribution == 'weekly':
                # Weekly pools need at least 7 days before first epoch
                if hours_since_start < 168:  # 7 days = 168 hours
                    days = hours_since_start / 24
                    return False, f"Weekly pool - only {days:.1f} days since start (need 7 for first epoch)"
            else:
                # Daily pools need at least 20 hours before first epoch
                if hours_since_start < 20:
                    return False, f"Daily pool - only {hours_since_start:.1f} hours since start (need 20 for first epoch)"

        return True, "First epoch for this pool"

    time_since_last = now - last_epoch_time
    hours_since_last = time_since_last.total_seconds() / 3600
    days_since_last = hours_since_last / 24

    if distribution == 'weekly':
        # For weekly pools, only generate if 7+ days since last epoch
        if days_since_last < 6.9:  # Allow slight margin for timing
            return False, f"Weekly pool - only {days_since_last:.1f} days since last epoch (need 7)"
        return True, f"Weekly pool - {days_since_last:.1f} days since last epoch"
    else:
        # For daily pools, generate if 20+ hours since last epoch
        if hours_since_last < 20:  # Allow for some timing variance
            return False, f"Daily pool - only {hours_since_last:.1f} hours since last epoch (need 20+)"
        return True, f"Daily pool - {hours_since_last:.1f} hours since last epoch"


def get_user_last_cumulative(pool_id, user_address):
    """Get user's last cumulative amount from previous epoch"""
    response = supabase.table('merkle_epoch_claims').select('cumulative_amount').eq('pool_id', pool_id).eq('user_address', user_address).order('epoch_id', desc=True).limit(1).execute()

    if response.data and len(response.data) > 0:
        return int(response.data[0]['cumulative_amount'])
    return 0


def calculate_pool_rewards(pool, stakes, now=None):
    """
    Calculate rewards for all stakers in a pool.

    IMPORTANT: Calculates rewards SINCE THE LAST EPOCH, not since staking started.
    This ensures incremental reward accrual without double-counting.

    Returns list of {address, cumulative, new_rewards, stake_info}
    """
    if now is None:
        now = datetime.now(timezone.utc)

    pool_name = pool['pool_name']
    pool_id = pool['id']
    pool_type = pool['pool_type']
    distribution = pool.get('reward_distribution', 'daily')
    lock_period = pool.get('lock_period', 'flexible (no lock)')
    funding_model = pool.get('funding_model', 'one-time pool')
    total_rewards = float(pool.get('total_rewards') or 0)

    # Get the last epoch time to calculate incremental rewards
    last_epoch_time = get_last_epoch_time(pool_id)
    # Parse dates and ensure they're timezone-aware
    def parse_date(date_str):
        if not date_str:
            return None
        # Handle various date formats
        date_str = date_str.replace('Z', '+00:00')
        if '+' not in date_str and 'T' in date_str:
            date_str = date_str + '+00:00'
        try:
            dt = datetime.fromisoformat(date_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except:
            return None

    start_date = parse_date(pool.get('start_date'))
    end_date = parse_date(pool.get('end_date'))
    # Use proper None check to correctly handle 0 decimals (0 or 6 = 6, which is wrong)
    reward_decimals_raw = pool.get('reward_token_decimals')
    reward_decimals = int(reward_decimals_raw) if reward_decimals_raw is not None else 6

    print(f"\n{'='*60}")
    print(f"📦 Pool: {pool_name}")
    print(f"   Type: {pool_type}")
    print(f"   Distribution: {distribution}")
    print(f"   Lock Period: {lock_period}")
    print(f"   Total Rewards: {total_rewards:,.0f}")
    print(f"   Start: {start_date}")
    print(f"   End: {end_date}")
    print(f"   Active Stakers: {len(stakes)}")

    if not stakes:
        print("   ⚠️ No active stakes")
        return []

    if not start_date:
        print("   ⚠️ No start date set")
        return []

    # Pool hasn't started yet
    if now < start_date:
        print(f"   ⚠️ Pool hasn't started yet (starts {start_date})")
        return []

    # Calculate pool duration and daily rate
    if funding_model == 'monthly rolling pool':
        # For rolling pools, calculate rewards for the current month
        # Use the start of current month (or pool start) to end of current month (or now)
        from calendar import monthrange

        # Get current month boundaries
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        _, last_day = monthrange(now.year, now.month)
        month_end = now.replace(day=last_day, hour=23, minute=59, second=59, microsecond=999999)

        # Effective period is from month start (or pool start) to now (or month end)
        effective_period_start = max(month_start, start_date)
        effective_period_end = min(now, month_end)

        # For monthly rolling pools, rewards deposited THIS month are for NEXT month
        # The epoch generation happens at month-end to distribute the previous month's rewards
        #
        # Flow:
        # 1. February: Creator deposits rewards to contract
        # 2. March 1st: Epoch generates for February stakers using February's deposit
        # 3. Users claim their February rewards in March
        #
        # Use monthly_pool_snapshots to lock in the month's rewards.
        # The snapshot is taken on the 1st of each month, capturing the balance
        # that was deposited the previous month. This ensures that deposits made
        # during the current month don't inflate the current month's rewards —
        # they'll be captured in NEXT month's snapshot instead.
        days_in_month = last_day
        snapshot_month = now.strftime('%Y-%m-01')
        pool_id_for_snapshot = pool.get('id')

        monthly_rewards = 0.0
        snapshot_result = supabase.table('monthly_pool_snapshots').select('total_rewards').eq('pool_id', pool_id_for_snapshot).eq('snapshot_month', snapshot_month).maybe_single().execute()
        snapshot_data = snapshot_result.data if snapshot_result else None
        if snapshot_data and float(snapshot_data.get('total_rewards') or 0) > 0:
            monthly_rewards = float(snapshot_data['total_rewards'])
            total_rewards = monthly_rewards
            print(f"   ✅ Using {now.strftime('%B')} snapshot: {monthly_rewards:,.2f} tokens")
        else:
            # No snapshot for this month — this pool was created mid-month (after the 1st).
            # Any funds in the contract are THIS month's deposit, which belongs to NEXT month.
            # Rule: funds deposited in month X are distributed in month X+1.
            # The April 1st snapshot will capture these funds and they'll be distributed in April.
            # Do not generate an epoch this month.
            print(f"   ℹ️  No {now.strftime('%B')} snapshot found — pool was created mid-month.")
            print(f"   ⏭️  Funds in contract are {now.strftime('%B')} deposits (for next month). Skipping epoch.")
            return []

        # Spread rewards over the effective days (pool start → month end), not the full calendar month.
        # If the pool started mid-month, the daily rate is higher so the full snapshot amount
        # is distributed by month end rather than rolling surplus into next month.
        effective_days = (month_end - effective_period_start).total_seconds() / 86400
        effective_days = max(effective_days, 1)
        daily_pool_rewards = monthly_rewards / effective_days

        print(f"   Monthly Rolling Pool:")
        print(f"      Month: {now.strftime('%B %Y')}")
        print(f"      Days in month: {days_in_month}")
        print(f"      Effective days (pool start → month end): {effective_days:.1f}")
        print(f"      Monthly rewards: {monthly_rewards:,.0f}")
        print(f"      Daily rate: {daily_pool_rewards:,.2f}")

        # Set end_date and reward window for calculations below.
        # reward_window_start ensures per-user periods are clamped to this month —
        # stakers who joined in February don't get credited for pre-March days.
        end_date = effective_period_end
        reward_window_start = effective_period_start
    else:
        # One-time pool logic
        if not end_date:
            print("   ⚠️ One-time pool without end date")
            return []

        # Calculate total pool duration in days
        pool_duration_days = (end_date - start_date).total_seconds() / 86400
        if pool_duration_days <= 0:
            print("   ⚠️ Invalid pool duration")
            return []

        # Daily reward rate for entire pool
        daily_pool_rewards = total_rewards / pool_duration_days
        reward_window_start = start_date

    # Calculate total staked amount
    total_staked = sum(float(s.get('amount_staked') or 0) for s in stakes)
    if total_staked <= 0:
        print("   ⚠️ No tokens/NFTs staked")
        return []

    print(f"   Total Staked: {total_staked:,.0f}")
    print(f"   Daily Pool Rewards: {daily_pool_rewards:,.2f}")
    if last_epoch_time:
        print(f"   Last Epoch: {last_epoch_time.isoformat()}")
    else:
        print(f"   Last Epoch: None (first epoch)")

    # Check lock period
    has_lock = '1 month' in lock_period.lower() or 'month' in lock_period.lower()
    lock_days = 30 if has_lock else 0

    claims = []

    for stake in stakes:
        user_address = stake['wallet_address']
        amount_staked = float(stake.get('amount_staked') or 0)
        staked_at = parse_date(stake.get('staked_at')) or start_date

        if amount_staked <= 0:
            continue

        # Calculate user's share of rewards
        user_share = amount_staked / total_staked

        # INCREMENTAL REWARDS: Calculate from last epoch (or reward window start if first epoch).
        # reward_window_start is March 1 for monthly rolling pools — ensures stakers who
        # joined in a prior month aren't credited for days before the current reward period.
        if last_epoch_time:
            reward_period_start = max(staked_at, reward_window_start, last_epoch_time)
        else:
            reward_period_start = max(staked_at, reward_window_start)

        reward_period_end = min(now, end_date)

        if reward_period_end <= reward_period_start:
            # No new rewards for this period (e.g., staked after last epoch)
            # But still include them with 0 new rewards if they have previous cumulative
            last_cumulative = get_user_last_cumulative(pool_id, user_address)
            if last_cumulative > 0:
                claims.append({
                    'address': user_address,
                    'cumulative': last_cumulative,
                    'new_rewards': 0,
                    'last_cumulative': last_cumulative,
                    'amount_staked': amount_staked,
                    'days_in_period': 0,
                    'days_for_rewards': 0,
                    'user_share': user_share,
                    'can_claim': True,
                    'lock_expires': None
                })
            continue

        days_in_period = (reward_period_end - reward_period_start).total_seconds() / 86400

        # For weekly distribution, only count complete weeks WITHIN THIS PERIOD.
        # Add a small tolerance (0.02 days ≈ 29 minutes) to handle cron timing drift:
        # when the cron fires at exactly the 7-day mark, the DB-stored last_epoch_time
        # slightly lags behind the trigger time, making days_in_period land at ~6.99
        # instead of 7.00, which would cause int(6.99 / 7) = 0 and zero out all rewards.
        if distribution == 'weekly':
            complete_weeks = int(days_in_period / 7 + 0.02)
            days_for_rewards = complete_weeks * 7
        else:
            days_for_rewards = days_in_period

        # Calculate new rewards for THIS PERIOD ONLY (incremental)
        new_rewards = daily_pool_rewards * user_share * days_for_rewards

        # Get previous cumulative
        last_cumulative = get_user_last_cumulative(pool_id, user_address)

        # SAFEGUARD: Validate rewards aren't absurdly high
        # Max new rewards per user per epoch should be <= 2x their daily rate
        max_expected_daily = daily_pool_rewards * user_share * 2  # 2 days worth max
        if new_rewards > max_expected_daily and days_for_rewards < 2:
            print(f"   ⚠️ SAFEGUARD TRIGGERED for {user_address[:12]}...")
            print(f"      Calculated: {new_rewards:,.2f} tokens for {days_for_rewards:.2f} days")
            print(f"      Max expected: {max_expected_daily:,.2f} (2 days worth)")
            print(f"      This suggests a bug! Capping to expected amount.")
            new_rewards = daily_pool_rewards * user_share * days_for_rewards  # Recalculate strictly

        # New cumulative = previous + new rewards (proper incremental addition)
        # Convert to integer (smallest unit based on decimals)
        new_rewards_int = int(new_rewards * (10 ** reward_decimals))
        cumulative = last_cumulative + new_rewards_int

        # SAFEGUARD: Log warning if cumulative seems too high relative to pool total
        max_possible = int(total_rewards * user_share * (10 ** reward_decimals))
        if cumulative > max_possible:
            print(f"   ⚠️ WARNING: {user_address[:12]}... cumulative ({cumulative:,}) exceeds max possible ({max_possible:,})")
            print(f"      This indicates historical over-distribution. Capping to max.")
            cumulative = max_possible

        # Check lock period - if locked, rewards accrue but can't claim
        can_claim = True
        lock_expires = None
        if has_lock:
            lock_expires = staked_at + timedelta(days=lock_days)
            if now < lock_expires:
                can_claim = False

        claims.append({
            'address': user_address,
            'cumulative': cumulative,
            'new_rewards': new_rewards_int,
            'last_cumulative': last_cumulative,
            'amount_staked': amount_staked,
            'days_in_period': days_in_period,
            'days_for_rewards': days_for_rewards,
            'user_share': user_share,
            'can_claim': can_claim,
            'lock_expires': lock_expires
        })

    # Sort by address for consistent ordering
    claims.sort(key=lambda x: x['address'])

    # VALIDATION SUMMARY
    total_new_rewards = sum(c['new_rewards'] for c in claims)
    total_cumulative = sum(c['cumulative'] for c in claims)
    total_rewards_raw = int(total_rewards * (10 ** reward_decimals))

    # Calculate expected based on time elapsed
    if last_epoch_time:
        time_elapsed = now - last_epoch_time
        days_elapsed = time_elapsed.total_seconds() / 86400
    else:
        time_elapsed = now - start_date
        days_elapsed = time_elapsed.total_seconds() / 86400

    expected_new = int(daily_pool_rewards * days_elapsed * (10 ** reward_decimals))

    print(f"\n   📊 EPOCH VALIDATION:")
    print(f"      Time period: {days_elapsed:.2f} days")
    print(f"      New rewards this epoch: {total_new_rewards:,} ({total_new_rewards / (10 ** reward_decimals):,.2f} tokens)")
    print(f"      Expected new rewards: {expected_new:,} ({expected_new / (10 ** reward_decimals):,.2f} tokens)")

    # Check if rewards are within reasonable bounds (0.5x to 1.5x expected)
    if expected_new > 0:
        ratio = total_new_rewards / expected_new
        if ratio > 1.5:
            print(f"      ⚠️ WARNING: Rewards are {ratio:.1f}x expected! May indicate a bug.")
        elif ratio < 0.5:
            print(f"      ⚠️ WARNING: Rewards are only {ratio:.1f}x expected. Some stakers may have joined late.")
        else:
            print(f"      ✅ Ratio: {ratio:.2f}x expected (within normal range)")

    print(f"      Total cumulative: {total_cumulative:,} ({total_cumulative / (10 ** reward_decimals):,.2f} tokens)")
    print(f"      Pool total rewards: {total_rewards_raw:,} ({total_rewards:,.0f} tokens)")
    print(f"      Distribution progress: {(total_cumulative / total_rewards_raw * 100) if total_rewards_raw > 0 else 0:.1f}%")

    print(f"\n   Calculated rewards for {len(claims)} users:")
    for c in claims[:5]:  # Show first 5
        lock_status = "" if c['can_claim'] else f" (locked until {c['lock_expires']})"
        print(f"      {c['address'][:12]}...: +{c['new_rewards']:,} → {c['cumulative']:,}{lock_status}")
    if len(claims) > 5:
        print(f"      ... and {len(claims) - 5} more")

    return claims


def generate_merkle_tree(pool, claims, epoch_id):
    """Generate Merkle tree for claims"""
    if not claims:
        return None, None

    app_id = pool.get('contract_app_id')
    if not app_id:
        print(f"   ⚠️ No contract_app_id for pool {pool['pool_name']}")
        return None, None

    # Format claims for MerkleTree
    tree_claims = [
        {'address': c['address'], 'cumulative': c['cumulative']}
        for c in claims
        if c['cumulative'] > 0
    ]

    if not tree_claims:
        print("   ⚠️ No claims with cumulative > 0")
        return None, None

    # Generate tree
    # CRITICAL: pool_id MUST match what's stored in the contract's global state
    # Fetch pool_id from the contract on Algorand
    pool_id_int = get_contract_pool_id(app_id)
    if pool_id_int is None:
        print(f"   ⚠️ Could not fetch pool_id from contract {app_id}")
        return None, None

    tree = MerkleTree(
        claims=tree_claims,
        app_id=int(app_id),
        pool_id=pool_id_int,
        epoch_id=epoch_id
    )

    root_hex = tree.get_root_hex()

    print(f"\n   🌳 Merkle Tree Generated:")
    print(f"      Epoch: {epoch_id}")
    print(f"      Claims: {len(tree_claims)}")
    print(f"      Root: {root_hex}")

    return tree, root_hex


def save_epoch_claims(pool, claims, tree, epoch_id, root_hex, dry_run=False):
    """Save claims to merkle_epoch_claims table"""
    if not claims or not tree:
        return False

    pool_id = pool['id']
    records = []

    for claim in claims:
        if claim['cumulative'] <= 0:
            continue

        # Get proof for this user
        try:
            proof = tree.get_proof_hex(claim['address'])
        except Exception as e:
            print(f"   ⚠️ Could not get proof for {claim['address']}: {e}")
            continue

        records.append({
            'pool_id': pool_id,
            'user_address': claim['address'],
            'epoch_id': epoch_id,
            'cumulative_amount': claim['cumulative'],
            'proof': proof,
            'merkle_root': root_hex,  # Store expected root for verification
            'created_at': datetime.now(timezone.utc).isoformat()
        })

    if dry_run:
        print(f"\n   🔍 DRY RUN: Would save {len(records)} claims")
        return True

    if not records:
        return False

    # Upsert claims (update if exists for same pool_id, user_address, epoch_id)
    try:
        response = supabase.table('merkle_epoch_claims').upsert(
            records,
            on_conflict='pool_id,user_address,epoch_id'
        ).execute()

        print(f"\n   ✅ Saved {len(records)} claims to database")
        return True
    except Exception as e:
        print(f"\n   ❌ Error saving claims: {e}")
        return False


def publish_epoch_root(pool, epoch_id, root_hex, dry_run=False):
    """Publish epoch root to smart contract"""
    # This would require the admin's private key to sign transactions
    # For now, we'll just print what would be published

    app_id = pool.get('contract_app_id')
    if not app_id:
        return False

    print(f"\n   📤 Epoch root ready for publication:")
    print(f"      App ID: {app_id}")
    print(f"      Epoch: {epoch_id}")
    print(f"      Root: {root_hex}")

    if dry_run:
        print("      (DRY RUN - not publishing)")
        return True

    # TODO: Implement actual on-chain publication
    # This requires:
    # 1. Admin mnemonic/private key
    # 2. Algorand client connection
    # 3. Transaction signing and submission
    print("      ⚠️ On-chain publication not implemented yet")
    print("      Run scripts/deployment/testnet/6_publish_epoch.py manually")

    return False


def delete_epochs_from(pool_id, from_epoch, dry_run=False):
    """Delete all epoch claims >= from_epoch for a pool.

    Requires service role key — anon key does not have DELETE permission.
    Used to reset bad/corrupt epochs so they can be regenerated correctly.
    """
    print(f"\n🗑️  Deleting epochs >= {from_epoch} for pool {pool_id}...")

    if dry_run:
        # Count what would be deleted
        resp = supabase.table('merkle_epoch_claims') \
            .select('epoch_id', count='exact') \
            .eq('pool_id', pool_id) \
            .gte('epoch_id', from_epoch) \
            .execute()
        count = resp.count if hasattr(resp, 'count') and resp.count is not None else len(resp.data or [])
        print(f"   🔍 DRY RUN: Would delete {count} rows (epochs >= {from_epoch})")
        return True

    try:
        resp = supabase.table('merkle_epoch_claims') \
            .delete() \
            .eq('pool_id', pool_id) \
            .gte('epoch_id', from_epoch) \
            .execute()
        print(f"   ✅ Deleted epochs >= {from_epoch}")
        return True
    except Exception as e:
        print(f"   ❌ Failed to delete epochs: {e}")
        print("      Note: This requires SUPABASE_SERVICE_ROLE_KEY — anon key cannot delete.")
        return False


def main():
    parser = argparse.ArgumentParser(description='Generate reward epochs for staking pools')
    parser.add_argument('--pool-id', help='Specific pool ID to process')
    parser.add_argument('--dry-run', action='store_true', help='Calculate but do not save')
    parser.add_argument('--publish', action='store_true', help='Also publish roots on-chain')
    parser.add_argument('--force', action='store_true', help='Force generation even if schedule says skip')
    parser.add_argument('--delete-from-epoch', type=int, metavar='N',
                        help='Delete all epoch claims >= N for the given --pool-id before regenerating. '
                             'Requires --pool-id and SUPABASE_SERVICE_ROLE_KEY.')
    args = parser.parse_args()

    print("=" * 70)
    print("EPOCH GENERATION - Mainnet Reward Distribution")
    print("=" * 70)
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    mode_parts = []
    if args.dry_run:
        mode_parts.append('DRY RUN')
    else:
        mode_parts.append('LIVE')
    if args.force:
        mode_parts.append('FORCE')
    if args.delete_from_epoch:
        mode_parts.append(f'RESET(epoch>={args.delete_from_epoch})')
    print(f"Mode: {' + '.join(mode_parts)}")

    # Handle epoch deletion before regeneration
    if args.delete_from_epoch is not None:
        if not args.pool_id:
            print("❌ --delete-from-epoch requires --pool-id")
            sys.exit(1)
        print(f"\n⚠️  EPOCH RESET: will delete epochs >= {args.delete_from_epoch} for pool {args.pool_id}")
        ok = delete_epochs_from(args.pool_id, args.delete_from_epoch, dry_run=args.dry_run)
        if not ok and not args.dry_run:
            print("❌ Epoch deletion failed — aborting to avoid inconsistent state.")
            sys.exit(1)
        # Force generation since we just deleted epochs
        args.force = True

    # Get pools
    pools = get_active_pools(args.pool_id)
    print(f"\nFound {len(pools)} active pool(s)")

    if not pools:
        print("No pools to process")
        return

    results = []

    for pool in pools:
        pool_id = pool['id']
        pool_name = pool['pool_name']
        distribution = pool.get('reward_distribution', 'daily')

        # Auto-sync: fix is_published BEFORE the schedule check so it runs even on skipped pools.
        # Heals the case where the browser DB update failed after a successful on-chain publish.
        _app_id = pool.get('contract_app_id')
        _db_epoch = get_latest_epoch(pool_id)
        _on_chain_epoch = get_on_chain_epoch(_app_id) if _app_id else 0
        if _on_chain_epoch >= _db_epoch and _db_epoch > 0:
            _sync = supabase.table('merkle_epoch_claims') \
                .update({'is_published': True}) \
                .eq('pool_id', pool_id) \
                .eq('epoch_id', _db_epoch) \
                .eq('is_published', False) \
                .execute()
            if _sync.data:
                print(f"   🔄 Auto-synced {pool_name} epoch {_db_epoch} → is_published=True ({len(_sync.data)} claims)")

        # Check if we should generate an epoch based on schedule
        should_generate, schedule_reason = should_generate_epoch(pool)
        print(f"\n📅 {pool_name} ({distribution}): {schedule_reason}")

        if not should_generate and not args.force:
            results.append({
                'pool': pool_name,
                'status': 'skipped',
                'reason': f"Schedule: {schedule_reason}"
            })
            continue

        if not should_generate and args.force:
            print(f"   ⚠️ Force flag set - generating anyway")

        # Get stakes
        stakes = get_pool_stakes(pool_id)

        # Calculate rewards
        from datetime import timedelta
        claims = calculate_pool_rewards(pool, stakes)

        if not claims:
            results.append({
                'pool': pool_name,
                'status': 'skipped',
                'reason': 'No claims to process'
            })
            continue

        # Get next epoch ID
        # CRITICAL: Must be higher than BOTH database epoch AND on-chain epoch
        # This handles cases where epochs were published out of order or without db records
        db_epoch = get_latest_epoch(pool_id)
        app_id = pool.get('contract_app_id')
        on_chain_epoch = get_on_chain_epoch(app_id) if app_id else 0

        if on_chain_epoch > db_epoch:
            print(f"   ⚠️ On-chain epoch ({on_chain_epoch}) > database epoch ({db_epoch})")
            print(f"      Skipping to epoch {on_chain_epoch + 1} to maintain sequentiality")

        next_epoch = max(db_epoch, on_chain_epoch) + 1

        # Generate Merkle tree
        tree, root_hex = generate_merkle_tree(pool, claims, next_epoch)

        if not tree:
            results.append({
                'pool': pool_name,
                'status': 'failed',
                'reason': 'Could not generate Merkle tree'
            })
            continue

        # Save claims to database (include merkle_root for verification)
        saved = save_epoch_claims(pool, claims, tree, next_epoch, root_hex, args.dry_run)

        if not saved and not args.dry_run:
            results.append({
                'pool': pool_name,
                'status': 'failed',
                'reason': 'Could not save claims'
            })
            continue

        # Optionally publish root on-chain
        if args.publish:
            publish_epoch_root(pool, next_epoch, root_hex, args.dry_run)

        results.append({
            'pool': pool_name,
            'status': 'success',
            'epoch': next_epoch,
            'claims': len(claims),
            'root': root_hex
        })

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    for r in results:
        if r['status'] == 'success':
            print(f"✅ {r['pool']}: Epoch {r['epoch']} with {r['claims']} claims")
        elif r['status'] == 'skipped':
            print(f"⏭️  {r['pool']}: {r['reason']}")
        else:
            print(f"❌ {r['pool']}: {r['reason']}")

    print("\n" + "=" * 70)
    if args.dry_run:
        print("DRY RUN COMPLETE - No changes made")
    else:
        print("EPOCH GENERATION COMPLETE")
    print("=" * 70)


if __name__ == "__main__":
    # Import timedelta here for use in calculate_pool_rewards
    from datetime import timedelta
    main()
