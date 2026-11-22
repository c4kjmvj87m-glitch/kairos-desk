"""Diagnose 3: losse futures-contracten op Yahoo, Swissquote-profielen, Stocktwits. Alleen lezen."""
import json, re, time, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timezone

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"


def get(url, timeout=25):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:200]
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


print("NU", datetime.now(timezone.utc).isoformat(), flush=True)
for s, rng, iv in (("GCG27.CMX", "1mo", "1h"), ("GCZ26.CMX", "5d", "1h"), ("GCJ27.CMX", "5d", "1h"), ("GCG27.CMX", "5d", "15m"),
                   ("ESZ26.CME", "5d", "15m"), ("NQZ26.CME", "5d", "15m"), ("ESH27.CME", "5d", "1h"), ("SIH27.CMX", "5d", "1h"),
                   ("^GSPC", "1mo", "1h"), ("^GSPC", "60d", "15m"), ("GC=F", "1d", "5m")):
    st, b = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(s)}?range={rng}&interval={iv}&includePrePost=true")
    if st != 200:
        print(f"[{st}] {s} {iv}: {b[:150]}", flush=True); continue
    r = json.loads(b)["chart"]["result"][0]; m = r["meta"]; ts = r.get("timestamp") or []; q = r["indicators"]["quote"][0]
    vol = sum(v or 0 for v in (q.get("volume") or [])[-50:])
    first = datetime.fromtimestamp(ts[0], timezone.utc).strftime("%d-%m %H:%M") if ts else "-"
    print(f"[200] {s} {iv} {rng}: px={m.get('regularMarketPrice')} t={datetime.fromtimestamp(m.get('regularMarketTime', 0), timezone.utc):%a %H:%M} n={len(ts)} eerste={first} vol50={vol} name={m.get('shortName') or m.get('longName')}", flush=True)
st, b = get("https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAU/USD")
try:
    for feed in json.loads(b):
        print("swissquote", feed.get("topo"), feed.get("ts"), [(p.get("spreadProfile"), p.get("bid"), p.get("ask")) for p in feed.get("spreadProfilePrices", [])], flush=True)
except Exception as e:
    print("swissquote fout", e, b[:200])
for sym in ("GLD", "SPY", "QQQ"):
    st, b = get(f"https://api.stocktwits.com/api/2/symbols/{sym}/sentiment.json")
    print(f"[{st}] stocktwits {sym}: {b[:160]}", flush=True)
st, page = get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm")
for y in ("2026", "2027"):
    i = page.find(f"{y} FOMC Meetings")
    seg = page[i:i + 20000] if i >= 0 else ""
    j = seg.find("FOMC Meetings", 30)
    seg = seg[:j] if j > 0 else seg
    rows = re.findall(r'fomc-meeting__month[^>]*><strong>([A-Za-z/]+)</strong></div>\s*<div class="fomc-meeting__date[^>]*>([^<]+)</div>', seg)
    print(f"fed {y}: {rows}", flush=True)
