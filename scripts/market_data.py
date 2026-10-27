"""Public Yahoo data through pinned yfinance; no brokerage or credentials."""
from datetime import datetime, timedelta, timezone, time
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

NY = ZoneInfo('America/New_York')
INTERVALS = {'1m': 60, '5m': 300, '15m': 900, '1h': 3600}


def iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def date(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def regular(value):
    local = value.astimezone(NY)
    return local.weekday() < 5 and time(9, 30) <= local.time() < time(16)


def positive(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('invalid_price') from None
    if not result.is_finite() or result <= 0: raise ValueError('invalid_price')
    return result


def normalize_quote(message, symbol, received_at, max_age_seconds=10):
    if message.get('id') != symbol: raise ValueError('unexpected_symbol')
    if isinstance(message.get('time'), bool): raise ValueError('invalid_quote_timestamp')
    try: quoted = datetime.fromtimestamp(int(message['time']) / 1000, timezone.utc)
    except (KeyError, ValueError, OverflowError, OSError, TypeError):
        raise ValueError('invalid_quote_timestamp') from None
    age = (received_at - quoted).total_seconds()
    if age < -2: raise ValueError('future_quote')
    if age > max_age_seconds: raise ValueError('stale_quote')
    return dict(symbol=symbol, price=str(positive(message.get('price'))), provider='yahoo_yfinance',
                quote_at=iso(quoted), received_at=iso(received_at), available_at=iso(received_at),
                market_hours=message.get('market_hours'), price_kind='indicative_last_price',
                age_seconds=max(0, age))


class YahooSource:
    provider = 'yahoo_yfinance'
    def bars(self, symbol, timeframe, now):
        import yfinance as yf
        if timeframe not in INTERVALS: raise ValueError('unsupported_timeframe')
        frame = yf.Ticker(symbol).history(period='5d', interval=timeframe, auto_adjust=False,
                                         prepost=False, timeout=12)
        if frame.empty: raise ValueError('no_market_bars')
        received = datetime.now(timezone.utc)
        rows = []
        for stamp, row in frame.iterrows():
            opened = stamp.to_pydatetime().astimezone(timezone.utc)
            closed = min(opened + timedelta(seconds=INTERVALS[timeframe]),
                         opened.astimezone(NY).replace(hour=16, minute=0, second=0, microsecond=0).astimezone(timezone.utc))
            if not regular(opened) or closed > now - timedelta(seconds=2): continue
            values = {key: str(positive(row[column])) for key, column in
                      [('open', 'Open'), ('high', 'High'), ('low', 'Low'), ('close', 'Close')]}
            volume = Decimal(str(row['Volume']))
            if not volume.is_finite() or volume < 0: raise ValueError('invalid_volume')
            if Decimal(values['high']) < max(Decimal(values['open']), Decimal(values['close']), Decimal(values['low'])):
                raise ValueError('invalid_market_bar')
            if Decimal(values['low']) > min(Decimal(values['open']), Decimal(values['close'])):
                raise ValueError('invalid_market_bar')
            rows.append(dict(symbol=symbol, bar_open_at=iso(opened), bar_close_at=iso(closed),
                             available_at=iso(received), volume=str(volume), **values))
        if not rows: raise ValueError('no_completed_regular_session_bars')
        return rows[-600:]

    def websocket(self):
        import yfinance as yf
        return yf.AsyncWebSocket(verbose=False)
