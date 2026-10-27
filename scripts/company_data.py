"""Public company snapshots. Share counts are snapshots, never tick-level facts."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


def positive(value):
    if isinstance(value, bool): return None
    try: result=Decimal(str(value))
    except (InvalidOperation,ValueError,TypeError): return None
    return str(result) if result.is_finite() and result>0 else None


def collect(symbol, ticker=None):
    observed=datetime.now(timezone.utc).isoformat()
    try:
        if ticker is None:
            import yfinance as yf
            ticker=yf.Ticker(symbol)
        info=ticker.get_info()
        if not isinstance(info,dict) or not info:raise ValueError('empty_company_snapshot')
        kind=str(info.get('quoteType','UNKNOWN')).upper()
        return dict(symbol=symbol,state='connected',source='Yahoo Finance / yfinance',observed_at=observed,
                    name=str(info.get('longName') or info.get('shortName') or symbol)[:180],
                    quote_type=kind,currency=str(info.get('currency')or'USD')[:8],
                    sector=str(info.get('sector')or'')[:100],
                    shares_outstanding=positive(info.get('sharesOutstanding')) if kind=='EQUITY' else None,
                    reported_market_cap=positive(info.get('marketCap')) if kind=='EQUITY' else None,
                    fund_assets=positive(info.get('totalAssets')) if kind in ('ETF','MUTUALFUND') else None,
                    previous_close=positive(info.get('previousClose')or info.get('regularMarketPreviousClose')),
                    day_volume=positive(info.get('regularMarketVolume')or info.get('volume')),
                    note='Company/share-count snapshot; filing date and provider delay are not verified. Estimated cap uses the separately timestamped last quote.')
    except Exception as error:
        return dict(symbol=symbol,state='unavailable',source='Yahoo Finance / yfinance',observed_at=observed,
                    error_type=type(error).__name__,name=symbol,quote_type='UNKNOWN',
                    shares_outstanding=None,reported_market_cap=None,fund_assets=None,previous_close=None,day_volume=None)
