"""Foreground MCP capture helper; invoked with the host's Maverick environment.

Only market_data_get_price_history is called. An empty private database/cache
prevents mixing adjusted vintages without clearing the installed server's cache.
"""
import asyncio
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys

TOOL = 'market_data_get_price_history'


async def capture(message):
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport
    import pandas_market_calendars as calendars
    from yfinance.scrapers.history import PriceHistory

    source = Path(message['source_root']).resolve()
    isolated = Path(message['isolated_state']).resolve()
    assert isolated.is_dir() and not list(isolated.iterdir()), 'cache_not_empty'
    assert inspect.signature(PriceHistory.history).parameters['auto_adjust'].default is True
    request = message['request']
    schedule = calendars.get_calendar('NYSE').schedule(start_date=request['start_date'], end_date=request['end_date'])
    sessions = [{'date': str(day.date()), 'bar_open_at': opened.isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
                 'bar_close_at': closed.isoformat(timespec='milliseconds').replace('+00:00', 'Z')}
                for day, opened, closed in zip(schedule.index, schedule.market_open, schedule.market_close)]
    tracked = ['maverick/market_data/tools.py', 'maverick/market_data/service.py', 'maverick/market_data/fetchers.py',
               'maverick/market_data/data.py', 'maverick/platform/config.py', 'uv.lock']
    hashes = {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in tracked}
    revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=source, capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    dirty = bool(subprocess.run(['git', 'status', '--porcelain'], cwd=source, capture_output=True, text=True, timeout=5, check=True).stdout)
    env = {'PATH': str(source / '.venv/bin') + ':/usr/bin:/bin',
           'DATABASE_URL': 'sqlite:///' + str(isolated / 'market.sqlite'), 'CACHE_SQLITE_PATH': str(isolated / 'cache.sqlite'),
           'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1'}
    transport = StdioTransport(str(source / '.venv/bin/maverick-mcp'), ['--transport', 'stdio'], cwd=str(source),
                               env=env, keep_alive=False, log_file=isolated / 'server.stderr.log')
    async with Client(transport, timeout=35) as client:
        result = await client.call_tool(TOOL, request)
        assert not result.is_error and len(result.content) == 1 and result.content[0].type == 'text', 'invalid_tool_response'
        raw = result.content[0].text
        parsed = json.loads(raw)
        assert parsed.get('status') == 'success', 'history_request_failed'
        if result.structured_content is not None:
            assert result.structured_content == parsed, 'tool_payload_disagreement'
        provider_version = client.server_info.version
    retrieved = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    return {'raw_response_json': raw, 'calendar': sessions, 'metadata': {
        'provider_version': provider_version, 'source_revision': revision, 'source_files_sha256': hashes, 'source_dirty': dirty,
        'tool': TOOL, 'request': request, 'retrieved_at': retrieved,
        'yfinance_version': importlib.metadata.version('yfinance'), 'calendar_version': importlib.metadata.version('pandas-market-calendars'),
        'calendar_library': 'pandas_market_calendars', 'calendar_name': 'NYSE', 'timezone': 'America/New_York',
        'timezone_source': 'stripped_naive_session_date', 'adjustment_policy': 'yfinance_auto_adjust_split_dividend',
        'adjustment_as_of': retrieved, 'cache_policy': 'fresh_isolated_empty_database',
        'raw_response_sha256': hashlib.sha256(raw.encode('utf-8')).hexdigest(), 'raw_format': 'mcp_text_json',
        'price_rounding': 'half_even_6_decimal_places',
        'license_reference': 'Yahoo Finance via yfinance; personal research only; redistribution not granted by this importer'}}


if __name__ == '__main__':
    try:
        data = sys.stdin.buffer.read(65537)
        assert len(data) <= 65536
        value = asyncio.run(asyncio.wait_for(capture(json.loads(data)), timeout=48))
        sys.stdout.write(json.dumps({'ok': True, 'capture': value}, allow_nan=False) + '\n')
    except Exception:
        # Upstream messages and environments are never copied into a receipt.
        sys.stdout.write(json.dumps({'ok': False, 'error': 'maverick_capture_failed'}) + '\n')
        sys.exit(1)
