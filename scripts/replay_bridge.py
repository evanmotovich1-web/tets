"""Read-only connection to the earlier Algo Lab historical replay, when installed."""
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
MAX_BYTES=16*1024*1024


def location():
    # Fixed local evidence locations only; no URL/path can be supplied by HTTP.
    for candidate in [ROOT/'desk/runtime/.state/tets/replay', ROOT.parent/'algo/desk/runtime/.state/workstation']:
        if candidate.is_dir() and not candidate.is_symlink():
            report=candidate/'backtest-report.json';html=candidate/'backtest-report.html'
            if report.exists() and html.exists():return report,html
    return None


def read():
    paths=location()
    if not paths:return None
    report,html=paths
    for path in paths:
        if path.is_symlink() or not path.is_file() or path.stat().st_size>MAX_BYTES:return None
    try:
        raw=report.read_bytes();data=json.loads(raw)
        if data.get('kind')!='algo_backtest_report' or data.get('schema_version')!=1 or \
           data.get('live_orders')is not False or data.get('promoting')is not False:return None
        if not all(data.get('independent_replay',{}).get(k)is True for k in ['orders','fills','equity','metrics']):return None
        # Refuse corrupted report bytes; the original report uses RFC 8785.
        import rfc8785
        expected=data['report_sha256']
        if hashlib.sha256(rfc8785.dumps({k:v for k,v in data.items()if k!='report_sha256'})).hexdigest()!=expected:return None
        page=html.read_bytes()
        # The embedded report must be the same frozen evidence as the JSON.
        marker=b'<script id="report-data" type="application/json">'
        if marker in page:
            embedded=page.split(marker,1)[1].split(b'</script>',1)[0]
        else:
            # Original Algo Lab template embeds a const REPORT assignment.
            marker=b'const REPORT='
            if marker not in page:return None
            embedded=page.split(marker,1)[1].split(b';\n',1)[0]
        if json.loads(embedded)!=data:return None
        return dict(data=data,html=page)
    except (ValueError,KeyError,OSError,TypeError):return None


def status():
    result=read()
    if not result:return dict(state='unavailable',mode='historical_replay',message='No verified Algo Lab replay is saved on this Mac.')
    data=result['data']
    return dict(state='available',mode='historical_replay',symbol=data['symbol'],window=data['window'],
                denominator=data['denominator'],created_at=data['created_at'],live_orders=False,promoting=False,
                message='The earlier Algo Lab replay is connected. Playback is historical; its fills are simulated.')
