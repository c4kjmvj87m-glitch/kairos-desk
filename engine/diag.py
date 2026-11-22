"""Diagnose: welke gratis bronnen werken vanaf GitHub? Alleen lezen, schrijft niets."""
import json, lzma, struct, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"


def get(url, raw=False, headers=None, timeout=25):
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    t = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
            b = r.read()
            return r.status, (b if raw else b.decode("utf-8", "replace")), time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, (e.read()[:300] if raw else e.read().decode("utf-8", "replace")[:300]), time.time() - t
    except Exception as e:
        return None, f"{type(e).__name__}: {e}", time.time() - t


def show(name, url, fn=None, **kw):
    st, body, dt = get(url, **kw)
    info = ""
    if st == 200 and fn:
        try:
            info = fn(body)
        except Exception as e:
            info = f"parse fout: {type(e).__name__}: {e}"
    elif st != 200:
        info = (body[:160] if isinstance(body, str) else repr(body[:80]))
    print(f"[{st}] {name} ({dt:.1f}s) {info}"[:900], flush=True)


def yh(body):
    j = json.loads(body)
    r = j["chart"]["result"][0]
    m = r["meta"]
    ts = r.get("timestamp") or []
    q = r["indicators"]["quote"][0]
    last = [(datetime.fromtimestamp(t, timezone.utc).strftime("%a %d %H:%M"), c) for t, c in zip(ts[-3:], q["close"][-3:])]
    return f"px={m.get('regularMarketPrice')} prev={m.get('chartPreviousClose')} t={datetime.fromtimestamp(m.get('regularMarketTime', 0), timezone.utc):%a %H:%M} n={len(ts)} last={last}"


def st_stream(body):
    j = json.loads(body)
    msgs = j.get("messages") or []
    bull = sum(1 for m in msgs if ((m.get("entities") or {}).get("sentiment") or {}).get("basic") == "Bullish")
    bear = sum(1 for m in msgs if ((m.get("entities") or {}).get("sentiment") or {}).get("basic") == "Bearish")
    sym = j.get("symbol") or {}
    return f"n={len(msgs)} bull={bull} bear={bear} keys={list(sym.keys())[:12]} first={msgs[0]['created_at'] if msgs else '-'}"


def duka(body):
    data = lzma.decompress(body)
    n = len(data) // 24
    recs = [struct.unpack(">5if", data[i * 24:(i + 1) * 24]) for i in (0, n - 1)] if n else []
    return f"{n} candles; first={recs[0] if recs else '-'} last={recs[-1] if recs else '-'}"


now = datetime.now(timezone.utc)
print("NU", now.isoformat())
for s, rng, iv in (("GC=F", "1mo", "1h"), ("ES=F", "5d", "5m"), ("NQ=F", "5d", "5m"), ("^GSPC", "5d", "5m"), ("^NDX", "5d", "5m"),
                   ("DX-Y.NYB", "1mo", "1h"), ("HG=F", "5d", "1d"), ("EURUSD=X", "5d", "1d"), ("NZDUSD=X", "5d", "1d"), ("^TNX", "5d", "1d"),
                   ("2YY=F", "5d", "1d"), ("ZQZ26.CBT", "5d", "1d"), ("ZQF27.CBT", "5d", "1d")):
    show(f"yahoo {s} {iv}", f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(s)}?range={rng}&interval={iv}&includePrePost=true", yh)
show("stocktwits GLD stream", "https://api.stocktwits.com/api/2/streams/symbol/GLD.json", st_stream)
show("stocktwits SPY stream", "https://api.stocktwits.com/api/2/streams/symbol/SPY.json", st_stream)
show("stocktwits GLD page", "https://stocktwits.com/symbol/GLD", lambda b: f"{len(b)} tekens, sentiment-woorden: {[w for w in ('sentimentScore', 'Bullish', 'Bearish', 'sentiment') if w in b]}")
y = now.year
show("treasury nominaal", f"https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{y}/all?type=daily_treasury_yield_curve&field_tdr_date_value={y}&page&_format=csv",
     lambda b: b.splitlines()[0][:200] + " | " + b.splitlines()[1][:120])
show("ff volgende week", "https://nfs.faireconomy.media/ff_calendar_nextweek.json", lambda b: f"{len(json.loads(b))} events")
show("ff deze week", "https://nfs.faireconomy.media/ff_calendar_thisweek.json", lambda b: json.dumps(json.loads(b)[0])[:300])
d = now - timedelta(days=3)
show("dukascopy XAUUSD min", f"https://datafeed.dukascopy.com/datafeed/XAUUSD/{d.year}/{d.month - 1:02d}/{d.day:02d}/BID_candles_min_1.bi5", duka, raw=True)
show("dukascopy XAUUSD hour (maand ervoor)", f"https://datafeed.dukascopy.com/datafeed/XAUUSD/{d.year}/{d.month - 2:02d}/BID_candles_hour_1.bi5", duka, raw=True)
show("swissquote XAU", "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAU/USD", lambda b: b[:200])
show("gold-api XAU", "https://api.gold-api.com/price/XAU", lambda b: b[:200])
show("cme fedwatch", "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html", lambda b: f"{len(b)} tekens")
show("investing fed rate monitor", "https://www.investing.com/central-banks/fed-rate-monitor", lambda b: f"{len(b)} tekens")
