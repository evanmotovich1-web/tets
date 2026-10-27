"""Timestamped Yahoo headlines and optional Alpaca news; no article bodies or trading inputs."""
from datetime import datetime, timezone
import json
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


def now():
    return datetime.now(timezone.utc).isoformat()


def article(source, title, url, published_at):
    if not isinstance(title, str) or not title.strip() or not isinstance(url, str): return None
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password: return None
    return dict(source=source, title=title[:500], url=url, published_at=published_at, received_at=now())


RSS_SOURCES=(('yahoo_finance_rss','https://finance.yahoo.com/rss/headline?s={symbol}'),
             ('bbc_business_rss','https://feeds.bbci.co.uk/news/business/rss.xml'))


def rss_feed(source,url,opener=urlopen):
    try:
        with opener(Request(url,headers={'User-Agent':'tets/0.1 (personal algo research)'}),timeout=12)as response:
            raw=response.read(262145)
            if len(raw)>262144:raise ValueError('rss_response_too_large')
        items=[];seen=set()
        for row in ET.fromstring(raw).findall('.//item')[:20]:
            published=row.findtext('pubDate')
            try:published=parsedate_to_datetime(published).astimezone(timezone.utc).isoformat()
            except (TypeError,ValueError):published=None
            item=article(source,row.findtext('title'),row.findtext('link'),published)
            if item and item['url']not in seen:items.append(item);seen.add(item['url'])
            if len(items)>=10:break
        return dict(source=source,state='connected'if items else'empty',items=items,authentication='not_required')
    except Exception as error:
        return dict(source=source,state='unavailable',error_type=type(error).__name__,items=[])


def collect(symbol, creds=None, ticker=None, opener=urlopen,rss_sources=RSS_SOURCES):
    reports = []
    try:
        if ticker is None:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
        items = []
        seen = set()
        for row in ticker.get_news(count=10)[:10]:
            c = row.get('content', row)
            provider = c.get('provider') or {}
            raw_url = c.get('canonicalUrl') or c.get('clickThroughUrl') or {}
            url = raw_url.get('url') if isinstance(raw_url, dict) else raw_url
            item = article(provider.get('displayName', 'Yahoo Finance'), c.get('title'), url or row.get('link'),
                           c.get('pubDate') or row.get('providerPublishTime'))
            if item and item['url'] not in seen: items.append(item); seen.add(item['url'])
        reports.append(dict(source='yahoo_finance', state='connected' if items else 'empty', items=items,
                            authentication='not_required'))
    except Exception as error:
        reports.append(dict(source='yahoo_finance', state='unavailable', error_type=type(error).__name__, items=[]))
    if not creds:
        reports.append(dict(source='alpaca_news', state='credentials_required', items=[], authentication='Alpaca paper keys'))
    else:
        try:
            request = Request('https://data.alpaca.markets/v1beta1/news?' + urlencode(dict(symbols=symbol,limit=10,include_content='false')),
                headers={'APCA-API-KEY-ID':creds['api_key'], 'APCA-API-SECRET-KEY':creds['secret_key']})
            with opener(request, timeout=15) as response:
                raw = response.read(262145)
                if len(raw) > 262144: raise ValueError('news_response_too_large')
                payload = json.loads(raw)
            items = [item for r in payload.get('news', [])[:10]
                     if (item := article(r.get('source', 'Alpaca'),r.get('headline'),r.get('url'),r.get('created_at')))]
            reports.append(dict(source='alpaca_news',state='connected' if items else 'empty',items=items,
                                feed_note='Actual access and delay depend on your Alpaca entitlement'))
        except Exception as error:
            reports.append(dict(source='alpaca_news',state='unavailable',error_type=type(error).__name__,items=[]))
    for source,url in rss_sources:reports.append(rss_feed(source,url.format(symbol=symbol),opener))
    return dict(kind='tets_news',symbol=symbol,observed_at=now(),sources=reports,
                use='display_and_record_only; headlines do not trigger orders')
