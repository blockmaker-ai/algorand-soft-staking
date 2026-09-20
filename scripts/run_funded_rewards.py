#!/usr/bin/env python3
"""Funded reward scheduler. Preview by default; publishing also requires an env flag.

This process has read access to chain history and service access to the private
ledger/archive. The Edge publisher retains signing keys and independently gates
every send. Output contains evidence and summaries, never credentials.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from funded_reward_runner import EvidenceArchive, FundedRunner, MAX_EVIDENCE_BYTES, valid_pool, canonical_bytes

BUCKET = 'funded-reward-evidence'


class ServiceHTTP:
    def __init__(self, origin, headers):
        self.origin, self.headers = origin.rstrip('/'), headers

    def request(self, method, path, body=None, headers=None, binary=False, attempts=1):
        for attempt in range(attempts):
            request = Request(self.origin + path, data=body, method=method, headers={**self.headers, **(headers or {})})
            try:
                with urlopen(request, timeout=70 if '/functions/' in path else 30) as response:
                    raw = response.read(MAX_EVIDENCE_BYTES + 1)
                    if len(raw) > MAX_EVIDENCE_BYTES:
                        raise RuntimeError('Response exceeds the evidence size limit')
                return raw if binary else (json.loads(raw) if raw else None)
            except HTTPError as error:
                # Keep headers, bodies and credentials out of CI exceptions.
                retryable = error.code in (429, 500, 502, 503, 504) or (method == 'GET' and '/blocks/' in path and error.code == 404)
                if not retryable or attempt + 1 == attempts:
                    raise RuntimeError(f'{method} {path.split("?")[0]} failed (HTTP {error.code})') from None
            except (TimeoutError, URLError):
                if attempt + 1 == attempts:
                    raise RuntimeError(f'{method} {path.split("?")[0]} could not be confirmed') from None
            time.sleep(min(2 ** attempt, 4))

    def rpc(self, name, args):
        # No automatic POST retry: an ambiguous commit is recovered from state
        # on the next run, never by recalculating or inventing a new operation.
        return self.request('POST', f'/rest/v1/rpc/{name}', canonical_bytes(args), {'Content-Type': 'application/json'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--pool-id', action='append', required=True, help='Registered pool UUID; repeat for multiple pools')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    if args.publish and os.environ.get('FUNDED_REWARDS_PUBLICATION_ENABLED') != 'true':
        parser.error('Publishing is disabled; explicitly enable the scheduler and Edge publisher')
    if any(not valid_pool(pool) for pool in args.pool_id):
        parser.error('Every pool ID must be a canonical UUID')
    url, service = (os.environ.get(name) for name in ('SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY'))
    if not url or not service:
        parser.error('SUPABASE_URL and the service-role credential are required')
    from urllib.parse import urlparse
    for name in ('SUPABASE_URL','ALGOD_URL','INDEXER_URL'):
        value=os.environ.get(name, '')
        parsed=urlparse(value)
        if parsed.scheme!='https' and not (parsed.scheme=='http' and parsed.hostname in ('localhost','127.0.0.1')):
            parser.error(f'{name} must use HTTPS or loopback')
    db = ServiceHTTP(url, {'Authorization': f'Bearer {service}', 'apikey': service, 'Cache-Control': 'no-cache'})
    def chain_http(kind):
        token=os.environ.get(f'{kind}_TOKEN', '')
        header=os.environ.get(f'{kind}_TOKEN_HEADER') or ('X-Algo-API-Token' if kind=='ALGOD' else 'X-Indexer-API-Token')
        return ServiceHTTP(os.environ[f'{kind}_URL'], {**({header:token} if token else {}), 'Cache-Control':'no-cache'})
    algod_http, indexer_http = chain_http('ALGOD'), chain_http('INDEXER')
    def write_object(key, data):
        try:
            db.request('POST', f'/storage/v1/object/{BUCKET}/{key}', data,
                {'Content-Type': 'application/gzip', 'x-upsert': 'false'})
        except RuntimeError:
            # Upload may have succeeded before a response was lost, or an
            # identical previous upload may exist. The read-back MUST verify it.
            db.request('GET', f'/storage/v1/object/authenticated/{BUCKET}/{key}', binary=True, attempts=3)
            # EvidenceArchive.load compares the uncompressed canonical hash;
            # gzip headers may differ between Python versions or platforms.
    def read_object(key):
        return db.request('GET', f'/storage/v1/object/authenticated/{BUCKET}/{key}', binary=True, attempts=3)
    archive = EvidenceArchive(write_object, read_object, args.output_dir)
    runner = FundedRunner(db.rpc,
        lambda pool, preview: db.request('POST', '/functions/v1/funded-publish', canonical_bytes({'pool_id': pool, 'preview': preview}),
            {'Content-Type': 'application/json'}),
        lambda path: algod_http.request('GET', path, attempts=4),
        lambda path: indexer_http.request('GET', path, attempts=4), archive)
    results, failed = [], False
    for pool_id in dict.fromkeys(args.pool_id):
        try:
            result = runner.run_pool(pool_id, publish=args.publish)
        except Exception as error:
            failed = True
            # These modules raise sanitised errors, never SDK signer objects.
            result = {'pool_id': pool_id, 'status': 'failed', 'error': str(error), 'publish': args.publish}
        print(json.dumps(result), flush=True)
        results.append(result)
    args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    (args.output_dir / 'summary.json').write_text(json.dumps(results, indent=2) + '\n')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
