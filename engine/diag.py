"""Diagnose 2: Dukascopy-details, Stocktwits-meter, Fed-agenda en NY Fed. Alleen lezen."""
import json, lzma, re, struct, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"


def get(url, raw=False, timeout=30):
    t = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=timeout) as r:
            b = r.read()
            return r.status, (b if raw else b.decode("utf-8", "replace")), time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, b"" if raw else "", time.time() - t
    except Exception as e:
        return None, f"{type(e).__name__}: {e}", time.time() - t


def candles(b, scale):
    if not b:
        return []
    d = lzma.decompress(b)
    return [(x[0], x[1] / scale, x[2] / scale, x[3] / scale, x[4] / scale) for x in (struct.unpack(">5if", d[i:i + 24]) for i in range(0, len(d), 24))]


now = datetime.now(timezone.utc)
print("NU", now.isoformat(), flush=True)
B = "https://datafeed.dukascopy.com/datafeed"
for inst, sc in (("XAUUSD", 1000), ("USA500IDXUSD", 1000), ("USATECHIDXUSD", 1000), ("DOLLARIDXUSD", 1000)):
    for y in (now.year, now.year - 1):
        st, b, dt = get(f"{B}/{inst}/{y}/BID_candles_day_1.bi5", raw=True)
        c = candles(b, sc) if st == 200 else []
        print(f"[{st}] {inst} dag {y} ({dt:.1f}s) n={len(c)} laatste={c[-1] if c else '-'}", flush=True)
    d = now - timedelta(days=3)
    st, b, dt = get(f"{B}/{inst}/{d.year}/{d.month - 1:02d}/{d.day:02d}/BID_candles_min_1.bi5", raw=True)
    c = candles(b, sc) if st == 200 else []
    print(f"[{st}] {inst} min {d:%d-%m} ({dt:.1f}s) n={len(c)} eerste={c[0] if c else '-'}", flush=True)
# parallel: 12 days of minute files for gold
days = [now - timedelta(days=i) for i in range(1, 13)]
t0 = time.time()
with ThreadPoolExecutor(8) as ex:
    res = list(ex.map(lambda d: get(f"{B}/XAUUSD/{d.year}/{d.month - 1:02d}/{d.day:02d}/BID_candles_min_1.bi5", raw=True), days))
print(f"parallel 12 min-bestanden: {time.time() - t0:.1f}s statussen={[r[0] for r in res]} groottes={[len(r[1]) for r in res]}", flush=True)
# today's ticks per hour (completed hours)
h0 = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
st, b, dt = get(f"{B}/XAUUSD/{h0.year}/{h0.month - 1:02d}/{h0.day:02d}/{h0.hour:02d}h_ticks.bi5", raw=True)
n = len(lzma.decompress(b)) // 20 if st == 200 and b else 0
print(f"[{st}] XAUUSD ticks {h0:%d-%m %H}h ({dt:.1f}s) ticks={n}", flush=True)
yday = now - timedelta(days=1)
st, b, dt = get(f"{B}/XAUUSD/{yday.year}/{yday.month - 1:02d}/{yday.day:02d}/BID_candles_min_1.bi5", raw=True)
print(f"[{st}] XAUUSD min gisteren {yday:%d-%m} ({dt:.1f}s) bytes={len(b) if isinstance(b, bytes) else b}", flush=True)
# Stocktwits page: look for the sentiment meter
st, page, dt = get("https://stocktwits.com/symbol/GLD")
for pat in (r'"sentimentChange"[^{}]{0,200}', r'"sentiment"\s*:\s*\{[^{}]{0,300}\}', r'sentimentScore[^,]{0,80}', r'"valueNormalized"[^,]{0,40}', r'"messageVolume"[^{}]{0,200}'):
    m = re.findall(pat, page)[:3]
    print(f"stocktwits {pat[:24]}: {m}"[:500], flush=True)
st, b, dt = get("https://api.stocktwits.com/api/2/symbols/GLD/sentiment.json")
print(f"[{st}] stocktwits sentiment.json {str(b)[:200]}", flush=True)
# Fed calendar and NY Fed EFFR
st, page, dt = get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm")
yrs = re.findall(r"(\d{4}) FOMC Meetings", page)
print(f"[{st}] fed agenda ({dt:.1f}s) jaren={yrs[:4]} stukje={re.sub(r'\s+', ' ', page[page.find('2026 FOMC Meetings'):page.find('2026 FOMC Meetings') + 900])[:600] if '2026 FOMC Meetings' in page else '-'}", flush=True)
st, b, dt = get("https://markets.newyorkfed.org/api/rates/unsecured/effr/last/1.json")
print(f"[{st}] NY Fed EFFR {b[:300] if isinstance(b, str) else b}", flush=True)
