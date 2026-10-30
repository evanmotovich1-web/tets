#!/usr/bin/env python3
"""Full US Stock & ETF Market Universe catalog, sector presets, and live on-demand symbol lookup."""
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / 'desk/runtime/.state/tets'

SYMBOL_RE = re.compile(r'^[A-Z][A-Z0-9]{0,4}(?:[.-][A-Z])?$')

MARKET_SECTORS = [
    {
        'id': 'all_leaders',
        'label': '🔥 Market Leaders (Top 15)',
        'symbols': ['SPY', 'QQQ', 'NVDA', 'AAPL', 'MSFT', 'TSLA', 'META', 'AMZN', 'GOOGL', 'AMD', 'PLTR', 'NFLX', 'AVGO', 'COIN', 'JPM'],
    },
    {
        'id': 'indices_etfs',
        'label': '📊 Indices & Macro ETFs',
        'symbols': ['SPY', 'QQQ', 'IWM', 'DIA', 'SMH', 'SOXX', 'XLK', 'XLF', 'XLE', 'XLV', 'ARKK', 'TLT', 'GLD', 'SLV', 'IBIT'],
    },
    {
        'id': 'ai_semis',
        'label': '🧠 AI & Semiconductors',
        'symbols': ['NVDA', 'AMD', 'AVGO', 'TSM', 'MU', 'ARM', 'SMCI', 'ASML', 'INTC', 'QCOM', 'MRVL', 'AMAT', 'LRCX', 'KLAC', 'SNPS'],
    },
    {
        'id': 'megacap_tech',
        'label': '💻 Mega-Cap & Cloud Tech',
        'symbols': ['AAPL', 'MSFT', 'META', 'AMZN', 'GOOGL', 'NFLX', 'ORCL', 'CRM', 'NOW', 'ADBE', 'PANW', 'CRWD', 'SNOW', 'DDOG', 'SHOP'],
    },
    {
        'id': 'momentum_fintech',
        'label': '🚀 High-Beta, Crypto & Fintech',
        'symbols': ['TSLA', 'PLTR', 'COIN', 'MSTR', 'HOOD', 'SOFI', 'SQ', 'PYPL', 'UBER', 'AFRM', 'RBLX', 'DKNG', 'RDDT', 'IONQ', 'RKLB'],
    },
    {
        'id': 'financials_energy',
        'label': '🏦 Wall St, Energy & Industrials',
        'symbols': ['JPM', 'GS', 'MS', 'BAC', 'BLK', 'V', 'MA', 'XOM', 'CVX', 'OXY', 'CAT', 'GE', 'BA', 'LMT', 'RTX'],
    },
    {
        'id': 'healthcare_consumer',
        'label': '💊 Healthcare, Biotech & Consumer',
        'symbols': ['LLY', 'UNH', 'NVO', 'ABBV', 'MRK', 'AMGN', 'ISRG', 'COST', 'WMT', 'HD', 'MCD', 'SBUX', 'NKE', 'DIS', 'CMG'],
    },
]

STOCK_DIRECTORY = {
    'SPY': {'name': 'SPDR S&P 500 ETF Trust', 'sector': 'Indices & ETFs'},
    'QQQ': {'name': 'Invesco QQQ Trust (Nasdaq-100)', 'sector': 'Indices & ETFs'},
    'IWM': {'name': 'iShares Russell 2000 ETF', 'sector': 'Indices & ETFs'},
    'DIA': {'name': 'SPDR Dow Jones Industrial Average ETF', 'sector': 'Indices & ETFs'},
    'SMH': {'name': 'VanEck Semiconductor ETF', 'sector': 'Indices & ETFs'},
    'SOXX': {'name': 'iShares Semiconductor ETF', 'sector': 'Indices & ETFs'},
    'XLK': {'name': 'Technology Select Sector SPDR', 'sector': 'Indices & ETFs'},
    'XLF': {'name': 'Financial Select Sector SPDR', 'sector': 'Indices & ETFs'},
    'XLE': {'name': 'Energy Select Sector SPDR', 'sector': 'Indices & ETFs'},
    'XLV': {'name': 'Health Care Select Sector SPDR', 'sector': 'Indices & ETFs'},
    'ARKK': {'name': 'ARK Innovation ETF', 'sector': 'Indices & ETFs'},
    'TLT': {'name': 'iShares 20+ Year Treasury Bond ETF', 'sector': 'Indices & ETFs'},
    'GLD': {'name': 'SPDR Gold Trust', 'sector': 'Indices & ETFs'},
    'SLV': {'name': 'iShares Silver Trust', 'sector': 'Indices & ETFs'},
    'IBIT': {'name': 'iShares Bitcoin Trust ETF', 'sector': 'Indices & ETFs'},
    'NVDA': {'name': 'NVIDIA Corporation', 'sector': 'AI & Semiconductors'},
    'AMD': {'name': 'Advanced Micro Devices, Inc.', 'sector': 'AI & Semiconductors'},
    'AVGO': {'name': 'Broadcom Inc.', 'sector': 'AI & Semiconductors'},
    'TSM': {'name': 'Taiwan Semiconductor Manufacturing', 'sector': 'AI & Semiconductors'},
    'MU': {'name': 'Micron Technology, Inc.', 'sector': 'AI & Semiconductors'},
    'ARM': {'name': 'Arm Holdings plc', 'sector': 'AI & Semiconductors'},
    'SMCI': {'name': 'Super Micro Computer, Inc.', 'sector': 'AI & Semiconductors'},
    'ASML': {'name': 'ASML Holding N.V.', 'sector': 'AI & Semiconductors'},
    'INTC': {'name': 'Intel Corporation', 'sector': 'AI & Semiconductors'},
    'QCOM': {'name': 'QUALCOMM Incorporated', 'sector': 'AI & Semiconductors'},
    'MRVL': {'name': 'Marvell Technology, Inc.', 'sector': 'AI & Semiconductors'},
    'AMAT': {'name': 'Applied Materials, Inc.', 'sector': 'AI & Semiconductors'},
    'LRCX': {'name': 'Lam Research Corporation', 'sector': 'AI & Semiconductors'},
    'KLAC': {'name': 'KLA Corporation', 'sector': 'AI & Semiconductors'},
    'SNPS': {'name': 'Synopsys, Inc.', 'sector': 'AI & Semiconductors'},
    'AAPL': {'name': 'Apple Inc.', 'sector': 'Mega-Cap Tech'},
    'MSFT': {'name': 'Microsoft Corporation', 'sector': 'Mega-Cap Tech'},
    'META': {'name': 'Meta Platforms, Inc.', 'sector': 'Mega-Cap Tech'},
    'AMZN': {'name': 'Amazon.com, Inc.', 'sector': 'Mega-Cap Tech'},
    'GOOGL': {'name': 'Alphabet Inc.', 'sector': 'Mega-Cap Tech'},
    'NFLX': {'name': 'Netflix, Inc.', 'sector': 'Mega-Cap Tech'},
    'ORCL': {'name': 'Oracle Corporation', 'sector': 'Mega-Cap Tech'},
    'CRM': {'name': 'Salesforce, Inc.', 'sector': 'Mega-Cap Tech'},
    'NOW': {'name': 'ServiceNow, Inc.', 'sector': 'Mega-Cap Tech'},
    'ADBE': {'name': 'Adobe Inc.', 'sector': 'Mega-Cap Tech'},
    'PANW': {'name': 'Palo Alto Networks, Inc.', 'sector': 'Mega-Cap Tech'},
    'CRWD': {'name': 'CrowdStrike Holdings, Inc.', 'sector': 'Mega-Cap Tech'},
    'SNOW': {'name': 'Snowflake Inc.', 'sector': 'Mega-Cap Tech'},
    'DDOG': {'name': 'Datadog, Inc.', 'sector': 'Mega-Cap Tech'},
    'SHOP': {'name': 'Shopify Inc.', 'sector': 'Mega-Cap Tech'},
    'TSLA': {'name': 'Tesla, Inc.', 'sector': 'High-Beta & Momentum'},
    'PLTR': {'name': 'Palantir Technologies Inc.', 'sector': 'High-Beta & Momentum'},
    'COIN': {'name': 'Coinbase Global, Inc.', 'sector': 'Crypto & Fintech'},
    'MSTR': {'name': 'MicroStrategy Incorporated', 'sector': 'Crypto & Fintech'},
    'HOOD': {'name': 'Robinhood Markets, Inc.', 'sector': 'Crypto & Fintech'},
    'SOFI': {'name': 'SoFi Technologies, Inc.', 'sector': 'Crypto & Fintech'},
    'SQ': {'name': 'Block, Inc.', 'sector': 'Crypto & Fintech'},
    'PYPL': {'name': 'PayPal Holdings, Inc.', 'sector': 'Crypto & Fintech'},
    'UBER': {'name': 'Uber Technologies, Inc.', 'sector': 'High-Beta & Momentum'},
    'AFRM': {'name': 'Affirm Holdings, Inc.', 'sector': 'Crypto & Fintech'},
    'RBLX': {'name': 'Roblox Corporation', 'sector': 'High-Beta & Momentum'},
    'DKNG': {'name': 'DraftKings Inc.', 'sector': 'High-Beta & Momentum'},
    'RDDT': {'name': 'Reddit, Inc.', 'sector': 'High-Beta & Momentum'},
    'IONQ': {'name': 'IonQ, Inc.', 'sector': 'High-Beta & Momentum'},
    'RKLB': {'name': 'Rocket Lab USA, Inc.', 'sector': 'High-Beta & Momentum'},
    'JPM': {'name': 'JPMorgan Chase & Co.', 'sector': 'Financials'},
    'GS': {'name': 'The Goldman Sachs Group, Inc.', 'sector': 'Financials'},
    'MS': {'name': 'Morgan Stanley', 'sector': 'Financials'},
    'BAC': {'name': 'Bank of America Corporation', 'sector': 'Financials'},
    'BLK': {'name': 'BlackRock, Inc.', 'sector': 'Financials'},
    'V': {'name': 'Visa Inc.', 'sector': 'Financials'},
    'MA': {'name': 'Mastercard Incorporated', 'sector': 'Financials'},
    'XOM': {'name': 'Exxon Mobil Corporation', 'sector': 'Energy'},
    'CVX': {'name': 'Chevron Corporation', 'sector': 'Energy'},
    'OXY': {'name': 'Occidental Petroleum Corporation', 'sector': 'Energy'},
    'CAT': {'name': 'Caterpillar Inc.', 'sector': 'Industrials'},
    'GE': {'name': 'GE Aerospace', 'sector': 'Industrials'},
    'BA': {'name': 'The Boeing Company', 'sector': 'Industrials'},
    'LMT': {'name': 'Lockheed Martin Corporation', 'sector': 'Industrials'},
    'RTX': {'name': 'RTX Corporation', 'sector': 'Industrials'},
    'LLY': {'name': 'Eli Lilly and Company', 'sector': 'Healthcare'},
    'UNH': {'name': 'UnitedHealth Group Incorporated', 'sector': 'Healthcare'},
    'NVO': {'name': 'Novo Nordisk A/S', 'sector': 'Healthcare'},
    'ABBV': {'name': 'AbbVie Inc.', 'sector': 'Healthcare'},
    'MRK': {'name': 'Merck & Co., Inc.', 'sector': 'Healthcare'},
    'AMGN': {'name': 'Amgen Inc.', 'sector': 'Healthcare'},
    'ISRG': {'name': 'Intuitive Surgical, Inc.', 'sector': 'Healthcare'},
    'COST': {'name': 'Costco Wholesale Corporation', 'sector': 'Consumer'},
    'WMT': {'name': 'Walmart Inc.', 'sector': 'Consumer'},
    'HD': {'name': 'The Home Depot, Inc.', 'sector': 'Consumer'},
    'MCD': {'name': "McDonald's Corporation", 'sector': 'Consumer'},
    'SBUX': {'name': 'Starbucks Corporation', 'sector': 'Consumer'},
    'NKE': {'name': 'NIKE, Inc.', 'sector': 'Consumer'},
    'DIS': {'name': 'The Walt Disney Company', 'sector': 'Consumer'},
    'CMG': {'name': 'Chipotle Mexican Grill, Inc.', 'sector': 'Consumer'},
}


def catalog():
    cfg_path = STATE / 'config.json'
    active_watchlist = ['SPY', 'QQQ', 'NVDA', 'AAPL', 'MSFT']
    try:
        if cfg_path.is_file() and not cfg_path.is_symlink():
            raw = json.loads(cfg_path.read_text())
            if isinstance(raw, dict) and isinstance(raw.get('watchlist'), list) and raw['watchlist']:
                active_watchlist = [str(s).upper() for s in raw['watchlist'] if SYMBOL_RE.match(str(s).upper())]
    except Exception:
        pass
    return {
        'sectors': MARKET_SECTORS,
        'directory': [
            {'symbol': sym, 'name': meta['name'], 'sector': meta['sector']}
            for sym, meta in STOCK_DIRECTORY.items()
        ],
        'total_catalog_symbols': len(STOCK_DIRECTORY),
        'supports_any_us_symbol': True,
        'active_watchlist': active_watchlist,
    }


def lookup_symbol(payload):
    """Fetch live intraday candles, latest price quote, company info, and strategy signal for ANY US stock/ETF."""
    if not isinstance(payload, dict):
        raise ValueError('Expected symbol lookup payload')
    symbol = str(payload.get('symbol') or '').strip().upper()
    if not SYMBOL_RE.match(symbol):
        raise ValueError('Enter a valid US stock or ETF ticker (e.g. NVDA, TSLA, SPY, PLTR)')
    timeframe = str(payload.get('timeframe') or '1m').strip()
    if timeframe not in ('1m', '5m', '15m', '1h'):
        timeframe = '1m'

    import company_data
    import news
    import paper
    import shadow
    from market_data import YahooSource, iso

    now = datetime.now(timezone.utc)
    source = YahooSource()
    bars = []
    bar_error = None
    try:
        bars = source.bars(symbol, timeframe, now)
    except Exception as exc:
        bar_error = type(exc).__name__

    comp_info = {}
    try:
        comp_info = company_data.collect(symbol)
    except Exception:
        comp_info = {
            'symbol': symbol,
            'name': STOCK_DIRECTORY.get(symbol, {}).get('name', symbol),
            'sector': STOCK_DIRECTORY.get(symbol, {}).get('sector', 'US Equity / ETF'),
            'state': 'cached_directory',
            'observed_at': iso(now),
        }
    if not comp_info.get('name') and symbol in STOCK_DIRECTORY:
        comp_info['name'] = STOCK_DIRECTORY[symbol]['name']
    if not comp_info.get('sector') and symbol in STOCK_DIRECTORY:
        comp_info['sector'] = STOCK_DIRECTORY[symbol]['sector']

    latest_bar = bars[-1] if bars else None
    latest_price = latest_bar['close'] if latest_bar else comp_info.get('previous_close')
    quote_obj = (
        {
            'symbol': symbol,
            'price': str(latest_price),
            'quote_at': latest_bar['bar_close_at'] if latest_bar else iso(now),
            'received_at': iso(now),
        }
        if latest_price is not None
        else None
    )

    decisions = []
    if bars:
        try:
            spec = shadow.load_spec(symbol=symbol)
            sys.path.insert(0, str(ROOT / 'desk'))
            from backtest.engine import family_signal
            start_idx = max(0, len(bars) - 60)
            for idx in range(start_idx, len(bars)):
                side = family_signal(spec, bars, idx, False, None, None)
                if side in ('buy', 'sell') or idx == len(bars) - 1:
                    decisions.append({
                        'symbol': symbol,
                        'decision_at': bars[idx]['bar_close_at'],
                        'bar_close_at': bars[idx]['bar_close_at'],
                        'side': side or 'hold',
                        'reason': f"{spec['family']} on completed {timeframe} bar; "
                                  + ('entry rule met' if side == 'buy' else 'exit rule met' if side == 'sell' else 'monitoring price structure'),
                    })
        except Exception:
            pass

    company_news = None
    try:
        company_news = news.collect(symbol, paper.credentials())
    except Exception:
        company_news = None

    # Cache in market.json so renderMarket in status.html has company metadata immediately
    try:
        mkt_path = STATE / 'market.json'
        mkt = json.loads(mkt_path.read_text()) if mkt_path.is_file() else {'observed_at': iso(now), 'companies': []}
        existing = [c for c in (mkt.get('companies') or []) if isinstance(c, dict) and c.get('symbol') != symbol]
        existing.append(comp_info)
        mkt['companies'] = existing[-60:]
        mkt['observed_at'] = iso(now)
        shadow.save(mkt_path, mkt)
    except Exception:
        pass

    return {
        'symbol': symbol,
        'name': comp_info.get('name') or STOCK_DIRECTORY.get(symbol, {}).get('name') or symbol,
        'sector': comp_info.get('sector') or STOCK_DIRECTORY.get(symbol, {}).get('sector') or 'US Market',
        'timeframe': timeframe,
        'state': 'shadow_watching' if bars else 'waiting_for_bars',
        'bar_status': 'available' if bars else 'unavailable',
        'feed_status': 'fresh_indicative' if quote_obj else 'waiting',
        'last_error': bar_error,
        'quote': quote_obj,
        'last_bar_at': latest_bar['bar_close_at'] if latest_bar else None,
        'last_checked_at': iso(now),
        'chart_bars': [
            {k: r[k] for k in ('bar_close_at', 'open', 'high', 'low', 'close', 'volume')}
            for r in bars[-120:]
        ],
        'quote_history': [
            {'at': r['bar_close_at'], 'price': r['close']}
            for r in bars[-120:]
        ],
        'decisions': decisions[-25:],
        'fills': [],
        'virtual_equity': '2000.00',
        'virtual_realized_pnl': '0.00',
        'company_info': comp_info,
        'company_news': company_news,
        'live_orders': False,
    }
