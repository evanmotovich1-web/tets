"""Research-only, hindsight-labeled Maverick daily history import.

No broker or Maverick backtester is reachable. Unknown availability is fetch
 time, never session close. Canonical rows retain that honest timestamp even
when a separate, explicitly unverified research replay uses historical bars.
"""
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

from .contracts import (Rejected, canonical, dec, digest, identifier, integer, loads, obj, require, rounded,
                        scaled, sealed, sha, sha_bytes, stamp, verify)
from .engine import validate_v2_rows

PROVIDER = 'maverick_yfinance'
CLASSIFICATION = 'historical_unverified'
TOOL = 'market_data_get_price_history'
STATE = Path(__file__).resolve().parents[1] / 'runtime/.state'
MAX_BYTES = 8 * 1024 * 1024
META = ['provider_version', 'source_revision', 'source_files_sha256', 'source_dirty', 'tool', 'request', 'retrieved_at',
        'yfinance_version', 'calendar_version', 'calendar_library', 'calendar_name', 'timezone', 'timezone_source',
        'adjustment_policy', 'adjustment_as_of', 'cache_policy', 'raw_response_sha256', 'raw_format', 'price_rounding', 'license_reference']


def request(ticker, start_date, end_date):
    require(type(ticker) is str and re.fullmatch(r'[A-Z][A-Z0-9]{0,4}(?:[.-][A-Z])?', ticker), 'symbol_out_of_scope')
    try:
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    except (ValueError, TypeError):
        raise Rejected('invalid_window') from None
    require(start.isoformat() == start_date and end.isoformat() == end_date and start < end and (end - start).days <= 1500, 'invalid_window')
    return {'ticker': ticker, 'start_date': start_date, 'end_date': end_date}


def build_snapshot(capture):
    obj(capture, ['raw_response_json', 'calendar', 'metadata'])
    meta = obj(capture['metadata'], META)
    obj(meta['request'], ['ticker', 'start_date', 'end_date'])
    query = request(**meta['request'])
    require(meta['tool'] == TOOL and meta['cache_policy'] == 'fresh_isolated_empty_database' and meta['raw_format'] == 'mcp_text_json', 'invalid_provenance')
    require(meta['calendar_library'] == 'pandas_market_calendars' and meta['calendar_name'] == 'NYSE' and
            meta['timezone'] == 'America/New_York' and meta['timezone_source'] == 'stripped_naive_session_date', 'calendar_mismatch')
    require(meta['adjustment_policy'] == 'yfinance_auto_adjust_split_dividend' and meta['adjustment_as_of'] == meta['retrieved_at'] and
            meta['price_rounding'] == 'half_even_6_decimal_places', 'price_basis_mismatch')
    require(type(meta['source_revision']) is str and re.fullmatch(r'[a-f0-9]{40}', meta['source_revision']) and type(meta['source_dirty']) is bool, 'invalid_provenance')
    require(type(meta['source_files_sha256']) is dict and meta['source_files_sha256'] and len(meta['source_files_sha256']) <= 50, 'invalid_provenance')
    for value in meta['source_files_sha256'].values(): digest(value)
    for key in ['provider_version', 'yfinance_version', 'calendar_version', 'license_reference']:
        require(type(meta[key]) is str and 1 <= len(meta[key]) <= 2000, 'invalid_provenance')
    fetched = stamp(meta['retrieved_at'])
    require(fetched <= datetime.now(timezone.utc), 'future_data')
    raw = capture['raw_response_json']
    require(type(raw) is str and len(raw.encode('utf-8')) <= MAX_BYTES, 'file_too_large')
    require(sha_bytes(raw.encode('utf-8')) == digest(meta['raw_response_sha256']), 'hash_mismatch')
    payload = loads(raw)
    obj(payload, ['columns', 'index', 'data', 'ticker', 'status', 'record_count'], ['available_at'])
    require(payload['status'] == 'success' and payload['ticker'] == query['ticker'], 'provider_symbol_mismatch')
    require(payload['columns'] == ['Open', 'High', 'Low', 'Close', 'Volume'], 'unsupported_provider_response')
    require(type(payload['data']) is list and type(payload['index']) is list and len(payload['data']) == len(payload['index']), 'provider_count_mismatch')
    require(integer(payload['record_count'], 2, 10000) == len(payload['data']), 'provider_count_mismatch')
    explicit = payload.get('available_at')
    require(explicit is None or type(explicit) is list and len(explicit) == len(payload['data']), 'unsupported_provider_response')
    sessions = capture['calendar'];require(type(sessions) is list and 1 <= len(sessions) <= 1500, 'calendar_mismatch')
    calendar = {}
    for entry in sessions:
        obj(entry, ['date', 'bar_open_at', 'bar_close_at'])
        require(entry['date'] not in calendar and query['start_date'] <= entry['date'] <= query['end_date'], 'calendar_mismatch')
        opened, closed = stamp(entry['bar_open_at']), stamp(entry['bar_close_at'])
        from zoneinfo import ZoneInfo
        local = opened.astimezone(ZoneInfo('America/New_York'))
        require(local.date().isoformat() == entry['date'] and local.weekday() < 5 and local.strftime('%H:%M:%S') == '09:30:00' and opened < closed, 'calendar_mismatch')
        calendar[entry['date']] = entry
    rows = [];excluded = {'early_close': 0, 'missing_session': 0, 'invalid_ohlc': 0};previous = None;seen = set()
    for i, (label, values) in enumerate(zip(payload['index'], payload['data'])):
        require(type(label) is str and re.fullmatch(r'\d{4}-\d\d-\d\dT00:00:00(?:\.000)?', label), 'invalid_session_date')
        day = label[:10]
        require(query['start_date'] <= day <= query['end_date'] and (previous is None or day > previous), 'duplicate_or_unordered_bars')
        previous = day;seen.add(day)
        entry = calendar.get(day)
        if entry is None: excluded['missing_session'] += 1;continue
        opened, closed = stamp(entry['bar_open_at']), stamp(entry['bar_close_at'])
        require(closed <= fetched, 'future_data')
        available = meta['retrieved_at'] if explicit is None else explicit[i]
        require(closed <= stamp(available) <= fetched, 'future_data')
        if (closed - opened).total_seconds() != 23400:excluded['early_close'] += 1;continue
        try:
            require(type(values) is list and len(values) == 5, 'invalid_ohlc')
            numbers = [Decimal(str(v)) for v in values]
            require(all(v.is_finite() and Decimal('0') <= v <= Decimal('1000000000') for v in numbers), 'invalid_ohlc')
            op, hi, lo, cl, volume = numbers
            require(lo > 0 and lo <= min(op, cl) <= max(op, cl) <= hi and volume >= 0, 'invalid_ohlc')
            prices = [scaled(v, 6) for v in numbers[:4]]
            require(all(Decimal(v) > 0 for v in prices), 'invalid_ohlc')
        except (InvalidOperation, ValueError, Rejected):excluded['invalid_ohlc'] += 1;continue
        rows.append({'symbol':query['ticker'], 'bar_open_at':entry['bar_open_at'], 'bar_close_at':entry['bar_close_at'], 'available_at':available,
                     **dict(zip(['open','high','low','close'], prices)), 'volume':scaled(volume, 0)})
    require(len(rows) >= 2, 'invalid_provider_rows')
    validate_v2_rows(rows, '1d', observed_at=meta['retrieved_at'])
    body = {'schema_version':1, 'kind':'bar_snapshot', 'provider':PROVIDER, 'classification':CLASSIFICATION, 'symbol':query['ticker'], 'timeframe':'1d',
            'received_at':meta['retrieved_at'], 'rows':rows, 'availability_basis':'observed_at_fetch' if explicit is None else 'source',
            'point_in_time_verified':False, 'historical_eligible':False, 'counts':{'raw_rows':len(payload['data']), 'returned_rows':len(rows), 'excluded':excluded},
            'raw_response_sha256':meta['raw_response_sha256'], 'data_sha256':sha_bytes(b''.join(canonical(r)+b'\n' for r in rows)), 'capture':capture}
    return sealed(body, 'snapshot_sha256')


def maverick_manifest(snapshot, partition, development_manifest=None):
    require(partition in ('development', 'validation'), 'dataset_access_denied')
    require(type(snapshot) is dict and 'capture' in snapshot, 'invalid_provider_snapshot')
    verify(snapshot, 'snapshot_sha256')
    expected = build_snapshot(snapshot['capture'])
    require(canonical(expected) == canonical(snapshot), 'provider_snapshot_mismatch')
    meta = snapshot['capture']['metadata']
    provenance = {**meta, 'calendar_sha256':sha(snapshot['capture']['calendar']), 'availability_basis':snapshot['availability_basis'],
                  'point_in_time_verified':False, 'historical_eligible':False, 'missing_expected_sessions':sorted(set(e['date'] for e in snapshot['capture']['calendar']) - set(d[:10] for d in loads(snapshot['capture']['raw_response_json'])['index']))}
    rows = snapshot['rows']
    if partition == 'validation':
        require(development_manifest is not None, 'development_reference_required')
        verify(development_manifest, 'manifest_sha256');validate_manifest(development_manifest)
        require(development_manifest['partition'] == 'development' and development_manifest['symbols'] == [snapshot['symbol']], 'strategy_dataset_mismatch')
        require(stamp(development_manifest['coverage']['last_bar_close']) < stamp(rows[0]['bar_open_at']), 'partition_overlap')
        require(date.fromisoformat(meta['request']['start_date']) > stamp(development_manifest['coverage']['last_bar_close']).date(), 'partition_overlap')
        provenance.update(development_manifest_sha256=development_manifest['manifest_sha256'],
                          development_last_bar_close=development_manifest['coverage']['last_bar_close'])
    else:require(development_manifest is None, 'unexpected_development_reference')
    dataset_id = 'maverick:' + ('validation:' if partition == 'validation' else '') + snapshot['snapshot_sha256']
    manifest = sealed({'schema_version':1,'kind':'dataset_manifest','dataset_id':dataset_id, 'classification':CLASSIFICATION,'partition':partition,
        'source':{'provider':PROVIDER,'source_reference':TOOL,'license_reference':meta['license_reference'],'retrieved_at':meta['retrieved_at'],'snapshot_sha256':snapshot['snapshot_sha256']},
        'format':'jsonl','row_schema':'ohlcv_pti_v1','timeframe':'1d','calendar_id':'nyse_regular','price_basis':'split_dividend_adjusted',
        'currency':'USD','price_scale':6,'cash_scale':6,'quantity_scale':0,'symbols':[snapshot['symbol']],
        'coverage':{'first_bar_open':rows[0]['bar_open_at'],'last_bar_close':rows[-1]['bar_close_at']},'row_count':len(rows),'data_sha256':snapshot['data_sha256'],
        'provenance':provenance}, 'manifest_sha256')
    return manifest, rows


def validate_manifest(manifest):
    require(manifest.get('classification') == CLASSIFICATION and manifest.get('partition') in ('development', 'validation') and
            manifest.get('price_basis') == 'split_dividend_adjusted' and manifest.get('timeframe') == '1d' and manifest.get('calendar_id') == 'nyse_regular', 'dataset_access_denied')
    p = manifest.get('provenance', {})
    require(p.get('point_in_time_verified') is False and p.get('historical_eligible') is False and
            p.get('adjustment_policy') == 'yfinance_auto_adjust_split_dividend' and p.get('timezone') == 'America/New_York' and
            p.get('cache_policy') == 'fresh_isolated_empty_database' and manifest.get('source',{}).get('provider') == PROVIDER and
            p.get('retrieved_at') == manifest['source']['retrieved_at'], 'invalid_provenance')
    require(stamp(manifest['source']['retrieved_at']) <= datetime.now(timezone.utc), 'future_data')
    if manifest['partition'] == 'validation':
        require('development_manifest_sha256' in p and 'development_last_bar_close' in p, 'development_reference_required')
        digest(p['development_manifest_sha256'])
        require(stamp(p['development_last_bar_close']) < stamp(manifest['coverage']['first_bar_open']), 'partition_overlap')
    return manifest['source']['retrieved_at']


def state_path(root):
    path = Path(root).resolve()
    require(path.is_relative_to(STATE.resolve()), 'gitignored_state_required')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def publish(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.staging-', dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f: f.write(raw);f.flush();os.fsync(f.fileno())
        try:os.link(name,path)
        except FileExistsError:require(path.read_bytes() == raw, 'snapshot_conflict')
    finally:os.unlink(name)


def freeze_capture(capture, root, partition='development', *, development_registry=None, development_dataset_id=None):
    from .walkforward import CampaignRegistry, provider_manifest
    from .storage import load_dataset
    require(partition in ('development', 'validation'), 'dataset_access_denied')
    development_manifest = None
    if partition == 'validation':
        require(development_registry is not None and development_dataset_id is not None, 'development_reference_required')
        # Load the referenced bytes through the existing checksummed provider path.
        development_manifest, _ = load_dataset(development_registry, development_dataset_id, 2)
    else:require(development_registry is None and development_dataset_id is None, 'unexpected_development_reference')
    snapshot = build_snapshot(capture)
    manifest, rows = provider_manifest(snapshot, partition, development_manifest=development_manifest)
    root = state_path(root);folder = root/'snapshots'
    if partition == 'validation':folder = folder/'validation'
    folder = folder/snapshot['snapshot_sha256']
    for name,value in [('snapshot.json',snapshot),('manifest.json',manifest),('calendar.json',capture['calendar'])]:publish(folder/name,canonical(value)+b'\n')
    publish(folder/'raw.json',capture['raw_response_json'].encode('utf-8'))
    data = b''.join(canonical(r)+b'\n' for r in rows);publish(folder/'data.jsonl',data)
    require(sha_bytes((folder/'data.jsonl').read_bytes()) == manifest['data_sha256'],'hash_mismatch')
    registry = {'schema_version':1,'kind':'dataset_registry','datasets':{manifest['dataset_id']:{'classification':CLASSIFICATION,'partition':partition,
                'manifest':'manifest.json','data':'data.jsonl','manifest_sha256':manifest['manifest_sha256']}}}
    publish(folder/'registry.json',canonical(registry)+b'\n')
    store = CampaignRegistry(root/'campaigns.sqlite')
    try:
        if development_manifest is not None:store.import_registered_dataset(development_registry, development_dataset_id)
        require(store.import_provider(snapshot, partition, development_manifest_sha256=development_manifest['manifest_sha256'] if development_manifest else None) == manifest,'manifest_mismatch')
    finally:store.close()
    return {'manifest':manifest,'snapshot_path':str(folder/'snapshot.json'),'manifest_path':str(folder/'manifest.json'),'data_path':str(folder/'data.jsonl'),
            'registry_path':str(folder/'registry.json'),'campaign_registry_path':str(root/'campaigns.sqlite'),'qualification':'research_only','promoting':False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ticker',required=True);parser.add_argument('--start-date',required=True);parser.add_argument('--end-date',required=True)
    parser.add_argument('--state-root',default=str(STATE/'maverick'));parser.add_argument('--maverick-root',default=str(Path.home()/'code/maverick-mcp'))
    parser.add_argument('--partition',choices=['development','validation'],default='development')
    parser.add_argument('--development-registry');parser.add_argument('--development-dataset-id')
    args = parser.parse_args();query=request(args.ticker,args.start_date,args.end_date);root=state_path(args.state_root)
    if args.partition == 'validation':require(args.development_registry is not None and args.development_dataset_id is not None, 'development_reference_required')
    else:require(args.development_registry is None and args.development_dataset_id is None, 'unexpected_development_reference')
    if args.partition == 'validation':
        from .storage import load_dataset
        development, _ = load_dataset(args.development_registry,args.development_dataset_id,2)
        validate_manifest(development)
        require(development['partition'] == 'development' and development['symbols'] == [args.ticker], 'strategy_dataset_mismatch')
        # Reject an overlapping request before touching the network.
        require(date.fromisoformat(args.start_date) > stamp(development['coverage']['last_bar_close']).date(), 'partition_overlap')
    isolated=root/'transports'/uuid.uuid4().hex;isolated.mkdir(parents=True,mode=0o700)
    source=Path(args.maverick_root).resolve()
    command=[str(source/'.venv/bin/python'),str(Path(__file__).with_name('maverick_capture.py'))]
    p=subprocess.run(command,input=canonical({'source_root':str(source),'isolated_state':str(isolated),'request':query}),capture_output=True,timeout=55,
                     env={'PATH':'/usr/bin:/bin','PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1'})
    require(len(p.stdout)<=MAX_BYTES and len(p.stderr)<=MAX_BYTES,'capture_too_large')
    reply=loads(p.stdout);require(p.returncode==0 and reply.get('ok') is True,'maverick_capture_failed')
    import sys
    sys.stdout.buffer.write(canonical(freeze_capture(reply['capture'],root,args.partition,
        development_registry=args.development_registry,development_dataset_id=args.development_dataset_id))+b'\n')


if __name__=='__main__':
    try:main()
    except (Rejected,subprocess.TimeoutExpired) as error:
        import sys
        sys.stdout.buffer.write(canonical({'ok':False,'error':error.code if isinstance(error,Rejected) else 'maverick_capture_timeout'})+b'\n');sys.exit(1)
