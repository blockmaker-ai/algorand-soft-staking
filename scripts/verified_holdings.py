"""Read-only asset history reconstruction for funded reward eligibility.

Providers are supplied by the caller. This module never signs, publishes or
updates stakes. Indexer pages must cover a fixed round range completely; nested
transactions, clawbacks, closes and asset creation/destruction are included.
The caller seals the input evidence and checks any preceding holding checkpoint
before using the resulting time segments in a reward allocation batch.
"""
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from fractions import Fraction
import re
from urllib.parse import urlencode

from algosdk.encoding import is_valid_address
from network import GENESIS_ID, GENESIS_HASH
from reward_ledger import digest, integer, timestamp


def address(value):
    if not isinstance(value, str) or not is_valid_address(value):
        raise ValueError('Invalid holding address')
    return value


def exact_weight(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool) or not re.fullmatch(r'(0|[1-9][0-9]{0,19})(\.[0-9]{1,19})?', str(value)):
        raise ValueError('Invalid exact stake amount')
    return Fraction(Decimal(str(value)))


def weight_text(value):
    # Eligibility is a minimum of decimal stake amounts and base-unit holdings;
    # its denominator therefore contains only powers of two and five.
    with localcontext() as context:
        context.prec = 80
        return format(Decimal(value.numerator) / Decimal(value.denominator), 'f')


def account_snapshot(account):
    wallet = address(account['address'])
    round_number = integer(account['round'])
    assets = account.get('assets', [])
    if not round_number or not isinstance(assets, list) or integer(account['total-assets-opted-in']) != len(assets):
        raise ValueError('Incomplete account holding snapshot')
    holdings = {}
    for row in assets:
        asset = integer(row['asset-id'])
        if not asset or asset in holdings:
            raise ValueError('Duplicate or invalid account asset')
        holdings[asset] = integer(row['amount'])
    return {'wallet': wallet, 'round': round_number, 'holdings': holdings, 'source_digest': digest(account)}


def read_transactions(read, wallet, start_round, end_round, max_pages=1000):
    """Read every page, even when a short page supplies a continuation token."""
    address(wallet)
    start_round, end_round = integer(start_round), integer(end_round)
    if not start_round or end_round < start_round:
        raise ValueError('Invalid holding history range')
    if start_round == end_round:
        return []
    records, seen_tokens, token = {}, set(), None
    for _ in range(max_pages):
        query = {'min-round': start_round + 1, 'max-round': end_round, 'limit': 1000}
        if token:
            query['next'] = token
        # No transaction-type/asset/role filter: these can obscure parent calls
        # containing an inner transfer or an asset close/clawback participant.
        page = read(f'/v2/accounts/{wallet}/transactions?{urlencode(query)}')
        if integer(page['current-round']) < end_round or not isinstance(page.get('transactions'), list):
            raise ValueError('Indexer has not covered the account snapshot round')
        added = 0
        for txn in page['transactions']:
            tx_id = txn.get('id')
            if not isinstance(tx_id, str) or not tx_id or not start_round < integer(txn['confirmed-round']) <= end_round:
                raise ValueError('Transaction outside the requested holding history')
            if tx_id in records:
                if digest(txn) != digest(records[tx_id]):
                    raise ValueError('Conflicting transaction history')
            else:
                records[tx_id] = txn
                added += 1
        token = page.get('next-token')
        if not token:
            return list(records.values())
        if not isinstance(token, str) or token in seen_tokens or not added:
            raise ValueError('Holding history pagination did not advance')
        seen_tokens.add(token)
    raise ValueError('Holding history exceeded the page limit; no allocation is safe')


def merge_histories(histories):
    """The same parent call can appear in several wallets' account searches."""
    records = {}
    for history in histories:
        for txn in history:
            key = txn['id']
            if key in records and digest(records[key]) != digest(txn):
                raise ValueError('Wallet searches disagree about a transaction')
            records[key] = txn
    return list(records.values())


def asset_changes(transactions, eligible_assets, destroyed_supply=None):
    """Return net holding changes at each block's timestamp, using exact units.

    All operations in a block have the same reward timestamp, so net them before
    computing weights. Supply for a destroyed asset must come from its verified
    immutable asset parameters (Indexer include-all), never a guessed amount.
    """
    eligible = {integer(asset) for asset in eligible_assets}
    supplies = {integer(asset): integer(total) for asset, total in (destroyed_supply or {}).items()}
    by_round = {}
    seen = set()

    def visit(txn, round_number, at):
        if integer(txn.get('confirmed-round', round_number)) != round_number or integer(txn.get('round-time', at)) != at:
            raise ValueError('Inner transaction disagrees with its block')
        event = by_round.setdefault(round_number, {'round': round_number, 'time': at, 'deltas': defaultdict(int)})
        if event['time'] != at:
            raise ValueError('Conflicting block timestamps')
        transfer = txn.get('asset-transfer-transaction')
        if txn.get('tx-type') == 'axfer' and not isinstance(transfer, dict):
            raise ValueError('Missing asset transfer details')
        if transfer and integer(transfer['asset-id']) in eligible:
            asset = integer(transfer['asset-id'])
            sender = address(transfer.get('sender') or txn['sender'])
            receiver = address(transfer['receiver'])
            amount, closed = integer(transfer['amount']), integer(transfer.get('close-amount', 0))
            event['deltas'][(sender, asset)] -= amount
            event['deltas'][(receiver, asset)] += amount
            if transfer.get('close-to'):
                close_to = address(transfer['close-to'])
                event['deltas'][(sender, asset)] -= closed
                event['deltas'][(close_to, asset)] += closed
            elif closed:
                raise ValueError('Asset close has no destination')
        config = txn.get('asset-config-transaction')
        if txn.get('tx-type') == 'acfg' and not isinstance(config, dict):
            raise ValueError('Missing asset configuration details')
        created = integer(txn.get('created-asset-index', 0))
        if config is not None:
            configured = integer(config.get('asset-id', 0))
            if created in eligible:
                if configured or not config.get('params'):
                    raise ValueError('Invalid asset creation history')
                event['deltas'][(address(txn['sender']), created)] += integer(config['params']['total'])
            elif configured in eligible and not config.get('params'):
                if configured not in supplies:
                    raise ValueError('Destroyed asset supply must be verified')
                event['deltas'][(address(txn['sender']), configured)] -= supplies[configured]
        children = txn.get('inner-txns', [])
        if not isinstance(children, list):
            raise ValueError('Incomplete inner transaction history')
        for child in children:
            visit(child, round_number, at)

    for txn in transactions:
        if txn['id'] in seen:
            raise ValueError('Duplicate parent transaction; merge histories first')
        seen.add(txn['id'])
        round_number, at = integer(txn['confirmed-round']), integer(txn['round-time'])
        if not round_number or not at:
            raise ValueError('Missing block identity')
        visit(txn, round_number, at)
    events = sorted(by_round.values(), key=lambda event: event['round'])
    if any(left['time'] > right['time'] for left, right in zip(events, events[1:])):
        raise ValueError('Block timestamps went backwards')
    return [{**event, 'deltas': dict(event['deltas'])} for event in events if any(event['deltas'].values())]


def rewind_holdings(snapshot, changes, start_round, eligible_assets, checkpoint=None):
    """Reconstruct one account at a common starting round from its live balance."""
    start_round = integer(start_round)
    if not start_round or snapshot['round'] < start_round:
        raise ValueError('Account snapshot precedes the holding cursor')
    eligible = {integer(asset) for asset in eligible_assets}
    balance = {asset: snapshot['holdings'].get(asset, 0) for asset in eligible}
    wallet = snapshot['wallet']
    for event in reversed(changes):
        if start_round < event['round'] <= snapshot['round']:
            for (owner, asset), delta in event['deltas'].items():
                if owner == wallet and asset in eligible:
                    balance[asset] -= delta
                    if not 0 <= balance[asset] <= 2**64 - 1:
                        raise ValueError('Holding history does not reconcile with the account')
    if checkpoint is not None:
        expected = {asset: integer(checkpoint.get(str(asset), checkpoint.get(asset, 0))) for asset in eligible}
        if balance != expected:
            raise ValueError('Holding history differs from the preceding sealed checkpoint')
    return balance


def verified_window(algod_read, indexer_read, wallets, eligible_assets, start_round, end_round, checkpoints=None, destroyed_supply=None, wait=None,
                    accounting_start=None, accounting_end=None):
    """Fetch a bounded common-round holding window without publishing rewards.

    Account reads may come from different rounds. Each account's transaction
    search must catch up to its own snapshot, and all searches are merged before
    rewinding so one transfer cannot be counted separately for its two wallets.
    Both cursor block timestamps are checked against Algod. Optional accounting
    times can fall between blocks (for example UTC midnight). In that case each
    cursor must lie at/after its block and strictly before the following block.
    """
    from concurrent.futures import ThreadPoolExecutor
    from time import sleep
    wait = wait or sleep
    start_round, end_round = integer(start_round), integer(end_round)
    between_blocks = accounting_start is not None and accounting_end is not None
    if (accounting_start is None) != (accounting_end is None):
        raise ValueError('Both accounting cursor times are required')
    if not start_round or end_round < start_round or (end_round == start_round and not between_blocks):
        raise ValueError('Invalid verified holding window')
    wallets = sorted(set(address(wallet) for wallet in wallets))
    eligible = {integer(asset) for asset in eligible_assets}
    if not eligible:
        raise ValueError('Missing staking asset policy')
    bounds, following_bounds, headers = [], [], {}
    def header(round_number):
        if round_number not in headers:
            block = algod_read(f'/v2/blocks/{round_number}?format=json&header-only=true')['block']
            if (integer(block['rnd']) != round_number or block.get('gen') != GENESIS_ID
                    or block.get('gh') != GENESIS_HASH):
                raise ValueError('Unexpected holding checkpoint block or network')
            headers[round_number] = block
        return headers[round_number]
    for round_number in (start_round, end_round):
        bounds.append(integer(header(round_number)['ts']))
        if between_blocks:
            following_bounds.append(integer(header(round_number + 1)['ts']))
    accounting_times = [timestamp(value) for value in (accounting_start, accounting_end)] if between_blocks else [
        datetime.fromtimestamp(value, timezone.utc) for value in bounds]
    if accounting_times[0] >= accounting_times[1]:
        raise ValueError('Holding checkpoint times did not advance')
    if between_blocks and any(not datetime.fromtimestamp(left, timezone.utc) <= at < datetime.fromtimestamp(right, timezone.utc)
                              for left, at, right in zip(bounds, accounting_times, following_bounds)):
        raise ValueError('Accounting cursor is not covered by its adjacent blocks')
    def read_wallet(wallet):
        raw = algod_read(f'/v2/accounts/{wallet}')
        snapshot = account_snapshot(raw)
        if snapshot['wallet'] != wallet or snapshot['round'] < end_round:
            raise ValueError('Account snapshot does not cover the requested window')
        return raw, snapshot
    with ThreadPoolExecutor(max_workers=4) as executor:
        inputs = list(executor.map(read_wallet, wallets))
    # Algod can be a few blocks ahead. Keep these fixed account snapshots while
    # waiting briefly for Indexer; repeatedly taking new snapshots would chase
    # the moving tip and could prevent a busy pool from ever publishing.
    newest_snapshot = max((row[1]['round'] for row in inputs), default=end_round)
    for attempt in range(21):
        if integer(indexer_read('/health')['round']) >= newest_snapshot:
            break
        if attempt == 20:
            raise ValueError('Indexer has not caught up; keep this batch unallocated')
        wait(0.5)
    with ThreadPoolExecutor(max_workers=4) as executor:
        histories = list(executor.map(lambda row: read_transactions(indexer_read, row[1]['wallet'], start_round, row[1]['round']), inputs))
    transactions = merge_histories(histories)
    changes = asset_changes(transactions, eligible, destroyed_supply)
    if any((event['round'] <= end_round and not bounds[0] <= event['time'] <= bounds[1]) or
           (event['round'] > end_round and event['time'] < bounds[1]) for event in changes):
        raise ValueError('Transaction times disagree with the cursor blocks')
    initial = {}
    for _, snapshot in inputs:
        wallet = snapshot['wallet']
        if checkpoints is not None and wallet not in checkpoints:
            raise ValueError('A wallet is missing its preceding holding checkpoint')
        initial[wallet] = rewind_holdings(snapshot, changes, start_round, eligible,
            checkpoints[wallet] if checkpoints is not None else None)
    until = {wallet: values.copy() for wallet, values in initial.items()}
    window_changes = [event for event in changes if event['round'] <= end_round]
    for event in window_changes:
        for (wallet, asset), delta in event['deltas'].items():
            if wallet in until and asset in eligible:
                until[wallet][asset] += delta
                if not 0 <= until[wallet][asset] <= 2**64 - 1:
                    raise ValueError('Invalid end-of-window holding checkpoint')
    evidence = {'version': 'verified-holdings-v1', 'network': GENESIS_ID,
        'start_round': start_round, 'end_round': end_round, 'block_times': bounds,
        'accounting_times': [at.isoformat() for at in accounting_times],
        'following_block_times': following_bounds, 'blocks': [headers[key] for key in sorted(headers)],
        'eligible_assets': sorted(eligible), 'accounts': [row[0] for row in inputs],
        'transactions': sorted(transactions, key=lambda txn: (txn['confirmed-round'], txn['id']))}
    return {'start': accounting_times[0].isoformat(), 'end': accounting_times[1].isoformat(),
        'start_round': start_round, 'end_round': end_round, 'initial_holdings': initial,
        'end_holdings': until, 'changes': window_changes,
        'evidence_digest': digest(evidence), 'evidence': evidence}


def eligibility_segments(events, initial_holdings, changes, asset_decimals, nft_pool, start_round, start, end):
    """Cap registered stakes by actual holdings throughout a complete window.

    initial_holdings must be verified at start_round and changes must be the
    merged complete histories through the window end. Initial state is not a
    wallet's current balance pasted onto an earlier time period.
    """
    start_at, end_at = timestamp(start), timestamp(end)
    if start_at >= end_at:
        raise ValueError('Invalid eligibility window')
    decimals = {integer(asset): integer(scale) for asset, scale in asset_decimals.items()}
    if not decimals or any(scale > 19 for scale in decimals.values()) or (not nft_pool and len(decimals) != 1):
        raise ValueError('Invalid staking asset policy')
    holdings = {address(wallet): {asset: integer(values.get(asset, values.get(str(asset), 0))) for asset in decimals}
                for wallet, values in initial_holdings.items()}
    registered, stakes, seen_ids = defaultdict(Fraction), {}, set()
    timeline = defaultdict(list)
    for event in events:
        at = timestamp(event['occurred_at'])
        if at <= end_at:
            timeline[at].append(('stake', event))
    for change in changes:
        if change['round'] <= integer(start_round):
            continue
        at = datetime.fromtimestamp(change['time'], timezone.utc)
        if at <= end_at:
            timeline[at].append(('holding', change))

    def apply(items):
        for kind, event in sorted(items, key=lambda item: (item[0], integer(item[1]['id']) if item[0] == 'stake' else item[1]['round'])):
            if kind == 'holding':
                for (wallet, asset), delta in event['deltas'].items():
                    if wallet in holdings and asset in decimals:
                        holdings[wallet][asset] += delta
                        if not 0 <= holdings[wallet][asset] <= 2**64 - 1:
                            raise ValueError('Invalid reconstructed holding balance')
                continue
            key, wallet = event['stake_id'], address(event['wallet_address'])
            event_id = integer(event['id'])
            if event_id in seen_ids or type(event.get('active_before')) is not bool or type(event.get('active_after')) is not bool:
                raise ValueError('Invalid or duplicate stake event')
            seen_ids.add(event_id)
            before, after = exact_weight(event['amount_before']), exact_weight(event['amount_after'])
            previous = stakes.get(key, (wallet, Fraction(), False))
            if previous != (wallet, before, event['active_before']) or (not event['active_after'] and after):
                raise ValueError('Stake history has a gap or conflicting before/after values')
            if wallet not in holdings:
                raise ValueError('A registered wallet is missing its verified holdings')
            stakes[key] = (wallet, after, event['active_after'])
            registered[wallet] += after - before

    def weights():
        if nft_pool:
            # A curated NFT can contribute to at most one participating wallet.
            for asset in decimals:
                if sum(values[asset] > 0 for values in holdings.values()) > 1:
                    raise ValueError('The same NFT appears in multiple wallets')
        result = {}
        for wallet, amount in registered.items():
            if nft_pool:
                held = Fraction(sum(value > 0 for value in holdings[wallet].values()))
            else:
                asset, scale = next(iter(decimals.items()))
                held = Fraction(holdings[wallet][asset], 10**scale)
            eligible = min(amount, held)
            if eligible:
                result[wallet] = weight_text(eligible)
        return result

    for at in sorted(time for time in timeline if time <= start_at):
        apply(timeline[at])
    position, result = start_at, []
    for until in sorted({end_at} | {time for time in timeline if start_at < time < end_at}):
        result.append({'start': position.isoformat(), 'end': until.isoformat(), 'weights': weights()})
        apply(timeline.get(until, []))
        position = until
    return result
