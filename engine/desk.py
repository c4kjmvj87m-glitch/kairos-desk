#!/usr/bin/env python3
"""KAIROS desk-run.

Haalt koersen, kalender, nieuws en sentiment op, berekent het factormodel, laat een
AI-model het kwalitatieve oordeel en de Nederlandse tekst schrijven en schrijft
data/desk.json en data/runs.json. Draait in GitHub Actions; alleen de standaardbibliotheek.

AI: de eerste sleutel die als GitHub-secret bestaat, in deze volgorde:
  ANTHROPIC_API_KEY  Claude Haiku 5.5 (betalen per gebruik, enkele centen per maand)
  GEMINI_API_KEY     Google Gemini (gratis binnen de limieten)
  GROQ_API_KEY       Groq (gratis binnen de limieten)
Lukt geen enkele, dan draait de desk op de rekenregels alleen.
"""
from __future__ import annotations

import csv
import gzip
import html
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("KAIROS_DATA_DIR") or os.path.join(ROOT, "data")
UTC = timezone.utc
BXL = ZoneInfo("Europe/Brussels")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
NOW = datetime.now(UTC)
MISSING: list[str] = []
NOTES: list[str] = []
ENGINE = {"ai": "geen (alleen rekenregels)"}
REAL_SRC = [None]

SESSION_NL = {"london": "ochtend, voor Londen", "ny": "middag, voor New York", "close": "avond, na de slot van Wall Street"}
DOW = ["ma", "di", "wo", "do", "vr", "za", "zo"]


# ---------------------------------------------------------------- helpers
def log(*a):
    print(*a, flush=True)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def clamp(v, a, b):
    return max(a, min(b, v))


def jsround(x: float) -> int:
    """Rondt af zoals JavaScript Math.round, zodat engine en app dezelfde score tonen."""
    return int(math.floor(x + 0.5))


def nl(x, dp=2):
    if x is None:
        return "—"
    s = f"{abs(x):,.{dp}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("-" if x < 0 else "") + s


def sgn(x, dp=2, unit=""):
    if x is None:
        return "—"
    r = round(x, dp)
    return ("+" if r > 0 else "−" if r < 0 else "±") + nl(abs(r), dp) + unit


def fetch(url, headers=None, body=None, timeout=25, tries=2):
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        h.update(headers)
    err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers=h, method="POST" if body is not None else "GET")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                detail = ""
            err = f"HTTP {e.code} {detail}".strip()
            if e.code in (400, 401, 403, 404, 413, 422):
                break
        except Exception as e:  # timeouts, DNS, reset
            err = f"{type(e).__name__}: {e}"
        if i < tries - 1:
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{urllib.parse.urlsplit(url).netloc}: {err}")


API_UA = "kairos-desk/2.0 (github-actions; python-urllib)"


class _ApiRedirect(urllib.request.HTTPRedirectHandler):
    """Een API-aanroep die wordt doorgestuurd is bijna altijd een fout (bv. naar een loginpagina): maak dat zichtbaar."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if req.get_method() != "GET":
            raise urllib.error.HTTPError(req.full_url, code, f"doorgestuurd naar {newurl}", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_API = urllib.request.build_opener(_ApiRedirect)


def api_json(url, headers=None, payload=None, timeout=180, tries=2):
    """JSON-API zonder browser-UA. Bij een fout staat in de melding wat er wel terugkwam."""
    h = {"User-Agent": API_UA, "Accept": "application/json"}
    if payload is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    body = None if payload is None else json.dumps(payload).encode()
    err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers=h, method="POST" if body is not None else "GET")
            with _API.open(req, timeout=timeout) as r:
                raw = r.read()
                if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                txt = raw.decode("utf-8", "replace")
                try:
                    return json.loads(txt)
                except ValueError:
                    final = r.geturl()
                    err = (f"HTTP {r.status} {r.headers.get('Content-Type', '?')}" + (f" via {final}" if final != url else "")
                           + f", {len(raw)} bytes, geen JSON: {txt[:160]!r}")
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                detail = ""
            err = f"HTTP {e.code} {e.reason} {detail}".strip()
            if e.code == 429 and i < tries - 1:
                wait = _retry_after(e.headers, detail)
                log(f"[i] {urllib.parse.urlsplit(url).netloc}: limiet bereikt, {wait:.0f}s wachten")
                time.sleep(wait)
                continue
            if e.code in (400, 401, 403, 404, 413, 422, 429) or 300 <= e.code < 400:
                break
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        if i < tries - 1:
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"{urllib.parse.urlsplit(url).netloc}: {err}")


def _retry_after(headers, detail=""):
    """Hoe lang wachten na een 429: Retry-After, Groq-resetheader of Gemini retryDelay; tussen 3 en 70 seconden."""
    cand = [(headers or {}).get("retry-after"), (headers or {}).get("x-ratelimit-reset-tokens"),
            (headers or {}).get("x-ratelimit-reset-requests")]
    m = re.search(r'"retryDelay"\s*:\s*"([\d.]+)s"', detail or "")
    if m:
        cand.append(m.group(1))
    for c in cand:
        if not c:
            continue
        mm = re.fullmatch(r"\s*(?:(\d+)m)?\s*([\d.]+)?s?\s*", str(c))
        if mm and (mm.group(1) or mm.group(2)):
            sec = int(mm.group(1) or 0) * 60 + float(mm.group(2) or 0)
            return max(3.0, min(70.0, sec + 1))
    return 20.0


# ---------------------------------------------------------------- prices
def _series(bars, px, px_at, src, sym, tz, max_jump=0.25):
    uniq = {}
    for b in bars:
        uniq[b["d"]] = b
    bars = [uniq[k] for k in sorted(uniq)]
    if not bars:
        raise RuntimeError(f"{sym}: geen koersdata")
    if px is None or px <= 0 or abs(px / bars[-1]["c"] - 1) > max_jump:
        px, px_at = bars[-1]["c"], None  # ongeloofwaardige quote: val terug op de laatste dagkoers
    day = px_at.astimezone(tz).date() if px_at else bars[-1]["d"]
    prior = [b for b in bars if b["d"] < day]
    today = next((b for b in bars if b["d"] == day), None)
    if prior and abs(px / prior[-1]["c"] - 1) > max_jump:
        raise RuntimeError(f"{sym}: onwaarschijnlijke koerssprong, bron overgeslagen")
    return {
        "sym": sym, "src": src, "px": float(px), "at": px_at,
        "prev": prior[-1]["c"] if prior else None,
        "hi": max(today["h"], px) if today else None,
        "lo": min(today["l"], px) if today else None,
        "bars": prior, "day": day,
    }


def yahoo(sym, max_jump=0.25):
    q = urllib.parse.quote(sym, safe="")
    j, last = None, None
    for host in ("query1", "query2"):
        try:
            j = json.loads(fetch(f"https://{host}.finance.yahoo.com/v8/finance/chart/{q}?range=1y&interval=1d&includePrePost=false", tries=1))
            break
        except Exception as e:
            last = e
    if j is None:
        raise RuntimeError(f"Yahoo {sym}: {last}")
    res = (j.get("chart") or {}).get("result") or []
    if not res:
        raise RuntimeError(f"Yahoo {sym}: leeg antwoord")
    r = res[0]
    m = r.get("meta") or {}
    tz = ZoneInfo(m.get("exchangeTimezoneName") or "America/New_York")
    q0 = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    bars = []
    for t, o, hi, lo, c in zip(r.get("timestamp") or [], q0.get("open") or [], q0.get("high") or [], q0.get("low") or [], q0.get("close") or []):
        if c is None or hi is None or lo is None:
            continue
        bars.append({"d": datetime.fromtimestamp(t, tz).date(), "o": o if o is not None else c, "h": hi, "l": lo, "c": c})
    pt = m.get("regularMarketTime")
    return _series(bars, m.get("regularMarketPrice"), datetime.fromtimestamp(pt, UTC) if pt else None, "Yahoo Finance", sym, tz, max_jump)


def yahoo_at(sym, ts):
    """Koers van sym (5-minutenbalk) op of vlak voor tijdstip ts, voor een eerlijke vergelijking met een spotprijs."""
    q = urllib.parse.quote(sym, safe="")
    j = json.loads(fetch(f"https://query1.finance.yahoo.com/v8/finance/chart/{q}?range=5d&interval=5m&includePrePost=true", tries=1))
    r = ((j.get("chart") or {}).get("result") or [{}])[0]
    closes = (((r.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    best = None
    for t, c in zip(r.get("timestamp") or [], closes):
        if c is not None and t <= ts.timestamp():
            best = (t, c)
    if not best:
        raise RuntimeError("geen 5-minutenkoers voor dat tijdstip")
    return best[1], datetime.fromtimestamp(best[0], UTC)


def stooq(sym, max_jump=0.25):
    d1 = (NOW - timedelta(days=430)).strftime("%Y%m%d")
    d2 = (NOW + timedelta(days=2)).strftime("%Y%m%d")
    txt = fetch(f"https://stooq.com/q/d/l/?s={urllib.parse.quote(sym)}&d1={d1}&d2={d2}&i=d", timeout=12, tries=1)
    bars = []
    for r in csv.DictReader(io.StringIO(txt)):
        try:
            bars.append({"d": date.fromisoformat(r["Date"]), "o": float(r["Open"]), "h": float(r["High"]), "l": float(r["Low"]), "c": float(r["Close"])})
        except Exception:
            continue
    if len(bars) < 30:
        raise RuntimeError(f"Stooq {sym}: te weinig data ({txt[:80]!r})")
    tz = ZoneInfo("Europe/Warsaw")
    px = at = None
    try:
        q = next(csv.DictReader(io.StringIO(fetch(f"https://stooq.com/q/l/?s={urllib.parse.quote(sym)}&f=sd2t2ohlc&h&e=csv", tries=1))))
        px = float(q["Close"])
        at = datetime.fromisoformat(f"{q['Date']}T{q['Time']}").replace(tzinfo=tz)
    except Exception:
        pass
    return _series(bars, px, at, "Stooq", sym, tz, max_jump)


def _scale(s, f):
    s["px"] *= f
    for k in ("prev", "hi", "lo"):
        if s.get(k) is not None:
            s[k] *= f
    for b in s["bars"]:
        for k in ("o", "h", "l", "c"):
            b[k] *= f


INSTR = {
    "XAUUSD": {"name": "Goud", "dp": 2, "spot": "XAU", "src": [("yahoo", "GC=F", "goud-future"), ("stooq", "xauusd", "spot")]},
    "US500": {"name": "S&P 500", "dp": 1, "src": [("yahoo", "^GSPC", "index"), ("stooq", "^spx", "index")]},
    "US100": {"name": "Nasdaq 100", "dp": 1, "src": [("yahoo", "^NDX", "index"), ("stooq", "^ndx", "index")]},
    "ES": {"name": "S&P-future", "dp": 1, "src": [("yahoo", "ES=F", "")]},
    "NQ": {"name": "Nasdaq-future", "dp": 1, "src": [("yahoo", "NQ=F", "")]},
    "DXY": {"name": "Dollar-index", "dp": 2, "src": [("yahoo", "DX-Y.NYB", ""), ("stooq", "dx.f", "")]},
    "US10Y": {"name": "US 10j rente", "dp": 3, "src": [("yahoo", "^TNX", ""), ("stooq", "10usy.b", "")]},
    "VIX": {"name": "Volatiliteit", "dp": 2, "jump": 1.5, "src": [("yahoo", "^VIX", "")]},
    "WTI": {"name": "Olie (WTI)", "dp": 2, "src": [("yahoo", "CL=F", ""), ("stooq", "cl.f", "")]},
    "BRENT": {"name": "Olie (Brent)", "dp": 2, "src": [("yahoo", "BZ=F", ""), ("stooq", "cb.f", "")]},
    "XAGUSD": {"name": "Zilver", "dp": 2, "spot": "XAG", "src": [("yahoo", "SI=F", ""), ("stooq", "xagusd", "")]},
    "US30": {"name": "Dow Jones", "dp": 0, "src": [("yahoo", "^DJI", ""), ("stooq", "^dji", "")]},
    "BTCUSD": {"name": "Bitcoin", "dp": 0, "jump": 0.5, "src": [("yahoo", "BTC-USD", ""), ("stooq", "btcusd", "")]},
}
PRIMARY = ["XAUUSD", "US500", "US100"]


def load_series():
    out = {}
    for k, spec in INSTR.items():
        errs = []
        for kind, sym, note in spec["src"]:
            try:
                mj = spec.get("jump", 0.25)
                s = stooq(sym, mj) if kind == "stooq" else yahoo(sym, mj)
                if k == "US10Y" and s["px"] > 20:  # sommige bronnen geven de rente x10
                    _scale(s, 0.1)
                s["note"] = note
                out[k] = s
                break
            except Exception as e:
                errs.append(str(e)[:160])
        if k in out and spec.get("spot") and out[k]["sym"].endswith("=F"):
            to_spot(out[k], spec["spot"])
        if k in out:
            log(f"[ok] {k} {out[k]['px']:.4f} via {out[k]['src']} {out[k]['sym']}" + (f"  (eerder mislukt: {' | '.join(errs)})" if errs else ""))
        else:
            MISSING.append(f"koers {k}")
            log(f"[!] {k}: " + " | ".join(errs))
    return out


def spot_quote(metal):
    """Live spotprijs (midden van bied en laat) in USD voor XAU of XAG."""
    errs = []
    try:
        j = api_json(f"https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/{metal}/USD",
                     headers={"User-Agent": UA}, timeout=20, tries=2)
        best = None
        for feed in j if isinstance(j, list) else []:
            for pr in feed.get("spreadProfilePrices") or []:
                b, a = pr.get("bid"), pr.get("ask")
                if isinstance(b, (int, float)) and isinstance(a, (int, float)) and 0 < b <= a:
                    if best is None or a - b < best[1] - best[0]:
                        best = (b, a, feed.get("ts"))
        if best:
            at = datetime.fromtimestamp(best[2] / 1000, UTC) if isinstance(best[2], (int, float)) else NOW
            return (best[0] + best[1]) / 2, at, "Swissquote"
        errs.append("Swissquote: geen prijs in antwoord")
    except Exception as e:
        errs.append(f"Swissquote: {e}"[:160])
    try:
        j = api_json(f"https://api.gold-api.com/price/{metal}", headers={"User-Agent": UA}, timeout=20, tries=2)
        at = NOW
        try:
            at = datetime.fromisoformat(str(j.get("updatedAt")).replace("Z", "+00:00"))
        except Exception:
            pass
        return float(j["price"]), at, "gold-api.com"
    except Exception as e:
        errs.append(f"gold-api.com: {e}"[:160])
    raise RuntimeError(" | ".join(errs))


def to_spot(s, metal):
    """Zet een future-reeks om naar spot: koershistorie van de future, verschoven met het actuele verschil (basis)."""
    try:
        px, at, src = spot_quote(metal)
    except Exception as e:
        log(f"[!] spot {metal}: {e}")
        return False
    if not 0.96 < px / s["px"] < 1.04:
        log(f"[!] spot {metal} {px:.2f} wijkt te veel af van de future {s['px']:.2f}; de future blijft staan")
        return False
    basis, how = s["px"] - px, f"laatste future-koers ({s['at']:%a %H:%M} UTC)" if s.get("at") else "laatste future-koers"
    try:
        fpx, ft = yahoo_at(s["sym"], at)
        if abs((at - ft).total_seconds()) <= 30 * 60:
            basis, how = fpx - px, f"future om {ft:%a %H:%M} UTC, spot om {at:%a %H:%M} UTC"
    except Exception as e:
        log(f"[i] {metal}: geen 5-minutenkoers van de future ({str(e)[:100]})")
    if not -0.006 < basis / px < 0.03:
        log(f"[!] {metal}: verschil future-spot {basis:+.2f} is ongewoon ({how})")
    for b in s["bars"]:
        for k in ("o", "h", "l", "c"):
            if isinstance(b.get(k), (int, float)):
                b[k] -= basis
    for k in ("prev", "hi", "lo"):
        if s.get(k) is not None:
            s[k] -= basis
    s["px"] = px
    if s.get("hi") is not None:
        s["hi"], s["lo"] = max(s["hi"], px), min(s["lo"], px)
    s["at"] = at
    s["src"] = f"{src} spot"
    s["sym"] = f"{metal}/USD"
    s["note"] = "niveaus via de COMEX-future"
    s["basis"] = round(basis, 2)
    log(f"[ok] {metal} spot {px:.2f} via {src}; verschil met de future {basis:+.2f} ({how})")
    return True


def chg(s):
    return None if not s or not s.get("prev") else s["px"] - s["prev"]


def chg_pct(s):
    return None if not s or not s.get("prev") else (s["px"] / s["prev"] - 1) * 100


def ret_n(s, n):
    if not s:
        return None
    closes = [b["c"] for b in s["bars"]]
    if len(closes) < n or not closes[-n]:
        return None
    return (s["px"] / closes[-n] - 1) * 100


def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def treasury_real10():
    """Reële 10-jaarsrente (TIPS) van het Amerikaanse ministerie van Financiën."""
    vals = []
    for yr in (NOW.year, NOW.year - 1):
        txt = fetch("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
                    f"{yr}/all?type=daily_treasury_real_yield_curve&field_tdr_date_value={yr}&page&_format=csv", timeout=30)
        for row in csv.DictReader(io.StringIO(txt.lstrip("\ufeff"))):
            try:
                m, d, y = (row.get("Date") or "").strip().split("/")
                vals.append((f"{int(y):04d}-{int(m):02d}-{int(d):02d}", float((row.get("10 YR") or "").strip())))
            except ValueError:
                pass
        if len(vals) >= 10:
            break
    vals.sort()
    if len(vals) < 6:
        raise RuntimeError(f"te weinig rentedata ({len(vals)} dagen)")
    return vals[-30:]


def fred(series_id):
    txt = fetch(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}", timeout=20, tries=1)
    vals = []
    for row in list(csv.reader(io.StringIO(txt)))[1:]:
        if len(row) > 1 and row[1] not in (".", ""):
            try:
                vals.append((row[0], float(row[1])))
            except ValueError:
                pass
    return vals[-30:]


# ---------------------------------------------------------------- calendar
FF_FEEDS = ["https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            "https://nfs.faireconomy.media/ff_calendar_nextweek.json"]
CCY = {"USD": "US", "EUR": "EU", "GBP": "UK", "JPY": "JP", "CNY": "CN", "CAD": "CA", "AUD": "AU", "NZD": "NZ", "CHF": "CH"}
NL_EV = [
    (r"^Core CPI m/m$", "Kern-CPI m/m"), (r"^Core CPI y/y$", "Kern-CPI j/j"), (r"^CPI m/m$", "CPI m/m"), (r"^CPI y/y$", "CPI j/j"),
    (r"^Core PPI m/m$", "Kern-PPI m/m"), (r"^PPI m/m$", "PPI m/m"), (r"^PPI y/y$", "PPI j/j"),
    (r"^Core PCE Price Index m/m$", "Kern-PCE m/m"), (r"^Core PCE Price Index y/y$", "Kern-PCE j/j"), (r"^PCE Price Index m/m$", "PCE m/m"),
    (r"^Non-Farm Employment Change$", "Banenrapport (NFP)"), (r"^ADP Non-Farm Employment Change$", "ADP-banen"),
    (r"^Unemployment Rate$", "Werkloosheid"), (r"^Unemployment Claims$", "Werkloosheidsaanvragen"),
    (r"^Average Hourly Earnings m/m$", "Uurlonen m/m"),
    (r"^Core Retail Sales m/m$", "Kern-detailhandel m/m"), (r"^Retail Sales m/m$", "Detailhandel m/m"),
    (r"^Federal Funds Rate$", "Fed-rentebesluit"), (r"^FOMC Statement$", "FOMC-verklaring"),
    (r"^FOMC Press Conference$", "Fed-persconferentie"), (r"^FOMC Meeting Minutes$", "FOMC-notulen"),
    (r"^Fed Chair (.+) Speaks$", r"Fed-voorzitter \1 spreekt"), (r"^FOMC Member (.+) Speaks$", r"Fed-lid \1 spreekt"),
    (r"^ISM Manufacturing PMI$", "ISM industrie"), (r"^ISM Services PMI$", "ISM diensten"),
    (r"^Prelim UoM Consumer Sentiment$", "Consumentenvertrouwen UoM (voorlopig)"), (r"^Revised UoM Consumer Sentiment$", "Consumentenvertrouwen UoM"),
    (r"^Prelim UoM Inflation Expectations$", "Inflatieverwachting UoM (voorlopig)"),
    (r"^Advance GDP q/q$", "BBP k/k (eerste schatting)"), (r"^Prelim GDP q/q$", "BBP k/k (voorlopig)"), (r"^Final GDP q/q$", "BBP k/k (definitief)"),
    (r"^GDP q/q$", "BBP k/k"), (r"^JOLTS Job Openings$", "JOLTS-vacatures"), (r"^Bank Holiday$", "Feestdag"),
    (r"^Main Refinancing Rate$", "ECB-rentebesluit"), (r"^Official Bank Rate$", "BoE-rentebesluit"), (r"^BOJ Policy Rate$", "BoJ-rentebesluit"),
    (r"^Durable Goods Orders m/m$", "Duurzame goederen m/m"), (r"^Core Durable Goods Orders m/m$", "Kern duurzame goederen m/m"),
    (r"^Flash Manufacturing PMI$", "Flash industrie-PMI"), (r"^Flash Services PMI$", "Flash diensten-PMI"),
    (r"^CB Consumer Confidence$", "Consumentenvertrouwen (CB)"), (r"^Pending Home Sales m/m$", "Lopende woningverkopen m/m"),
    (r"^New Home Sales$", "Nieuwe woningverkopen"), (r"^Existing Home Sales$", "Bestaande woningverkopen"),
    (r"^Empire State Manufacturing Index$", "Empire State industrie"), (r"^Philly Fed Manufacturing Index$", "Philly Fed industrie"),
    (r"^Personal Spending m/m$", "Consumentenbestedingen m/m"), (r"^Personal Income m/m$", "Persoonlijk inkomen m/m"),
    (r"^Trade Balance$", "Handelsbalans"), (r"^Building Permits$", "Bouwvergunningen"), (r"^Housing Starts$", "Woningbouw"),
]
TIER1 = re.compile(r"\b(CPI|Non-Farm Employment Change|Federal Funds Rate|FOMC Statement|FOMC Press Conference|PCE Price Index|Fed Chair)\b", re.I)
TIER2 = re.compile(r"\b(PPI|Retail Sales|ISM|GDP|JOLTS|Unemployment Claims|FOMC Meeting Minutes|Unemployment Rate|Average Hourly Earnings|UoM)\b", re.I)


def nl_event(t):
    for pat, rep in NL_EV:
        if re.match(pat, t):
            return re.sub(pat, rep, t)
    return t


def nl_val(v):
    return re.sub(r"(\d)\.(\d)", r"\1,\2", v) if v else None


def when_nl(t: datetime):
    loc = t.astimezone(BXL)
    d = (loc.date() - datetime.now(BXL).date()).days
    day = "vandaag" if d == 0 else "morgen" if d == 1 else DOW[loc.weekday()]
    return f"{day} {loc:%H:%M}"


def calendar():
    raw = []
    for u in FF_FEEDS:
        try:
            raw += json.loads(fetch(u))
        except Exception as e:
            if "thisweek" in u:
                MISSING.append("kalender")
                log("[!] kalender", u, e)
            else:
                log("[i] kalender volgende week nog niet gepubliceerd")
    start = datetime.now(BXL).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=5)
    seen, ev = set(), []
    for e in raw:
        try:
            t = datetime.fromisoformat(e["date"])
        except Exception:
            continue
        if not (start <= t.astimezone(BXL) < end):
            continue
        c = e.get("country", "")
        impact = e.get("impact", "")
        imp = {"High": 3, "Medium": 2, "Low": 1, "Holiday": 1}.get(impact, 1)
        if c == "USD":
            if imp < 2 and impact != "Holiday":
                continue
        elif imp < 3 or c not in ("EUR", "GBP", "JPY", "CNY"):
            continue
        title = e.get("title", "")
        key = (title, c, iso(t))
        if key in seen:
            continue
        seen.add(key)
        ev.append({"t": iso(t), "cty": CCY.get(c, c[:2]), "ev": nl_event(title), "en": title, "imp": imp,
                   "prev": nl_val(e.get("previous")) or None, "cons": nl_val(e.get("forecast")) or None, "act": None,
                   "hits": PRIMARY[:] if c == "USD" else ["XAUUSD"]})
    ev.sort(key=lambda x: (x["t"], -x["imp"]))
    if len(ev) > 18:
        keep = sorted(ev, key=lambda x: (-(x["imp"] + (1 if x["cty"] == "US" else 0)), x["t"]))[:18]
        ev = sorted(keep, key=lambda x: (x["t"], -x["imp"]))
    for i, x in enumerate(ev):
        x["id"] = f"e{i}"
    log(f"[ok] kalender: {len(ev)} events")
    return ev


def event_penalty(cal):
    best, why = 0, ""
    for x in cal:
        if x["cty"] != "US":
            continue
        t = datetime.fromisoformat(x["t"].replace("Z", "+00:00"))
        hrs = (t - NOW).total_seconds() / 3600
        if hrs < 0:
            continue
        t1 = bool(TIER1.search(x["en"])) and not x["en"].startswith("ADP")
        t2 = bool(TIER2.search(x["en"]))
        pen = 20 if (t1 and hrs <= 24) else 10 if ((t1 and hrs <= 72) or (t2 and hrs <= 24)) else 0
        if pen > best:
            best, why = pen, f"{x['ev']} {when_nl(t)}"
    return best, why


# ---------------------------------------------------------------- news & sentiment
GN = "https://news.google.com/rss/search?q={}&hl=en-US&gl=US&ceid=US:en"
NEWS_Q = ["gold price when:1d", "stock market today when:1d", "Federal Reserve rate when:2d",
          "Nasdaq S&P 500 futures when:1d", "FedWatch probability when:4d", "XAUUSD when:1d"]


def rss_items(xml_text, default_src=""):
    root = ET.fromstring(xml_text)
    out = []
    for it in root.iter("item"):
        title = html.unescape((it.findtext("title") or "").strip())
        link = (it.findtext("link") or "").strip()
        el = it.find("source")
        src = (el.text or "").strip() if el is not None and el.text else default_src
        pub = it.findtext("pubDate")
        try:
            t = parsedate_to_datetime(pub).astimezone(UTC) if pub else None
        except Exception:
            t = None
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        if title:
            out.append({"t": t, "h": title, "src": src, "url": link})
    return out


def news():
    items = []
    for q in NEWS_Q:
        try:
            items += rss_items(fetch(GN.format(urllib.parse.quote(q))))
        except Exception as e:
            log("[!] nieuws", q, e)
    cutoff = NOW - timedelta(hours=40)
    seen, out = set(), []
    for it in sorted(items, key=lambda x: x["t"] or NOW - timedelta(days=9), reverse=True):
        if it["t"] and it["t"] < cutoff:
            continue
        k = re.sub(r"[^a-z0-9 ]", "", it["h"].lower())[:70]
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    if not out:
        MISSING.append("nieuws")
    log(f"[ok] nieuws: {len(out)} koppen")
    return out[:28]


def fear_greed():
    try:
        j = json.loads(fetch("https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
                             headers={"Referer": "https://edition.cnn.com/", "Origin": "https://edition.cnn.com", "Accept": "application/json"}))
        fg = j["fear_and_greed"]
        lab = {"extreme fear": "Extreme angst", "fear": "Angst", "neutral": "Neutraal", "greed": "Hebzucht",
               "extreme greed": "Extreme hebzucht"}.get(str(fg.get("rating", "")).lower(), str(fg.get("rating", "")))
        prev = fg.get("previous_close")
        out = {"v": int(round(fg["score"])), "label": lab, "prev": int(round(prev)) if isinstance(prev, (int, float)) else None, "src": "CNN Fear & Greed"}
        log(f"[ok] fear&greed {out['v']}")
        return out
    except Exception as e:
        MISSING.append("Fear & Greed")
        log("[!] fear&greed", e)
        return None


# ---------------------------------------------------------------- factor model
MODEL = {
    "XAUUSD": [("fed", "Fed-pad", 3, "ai"), ("yields", "Reële rente", 2, "code"), ("usd", "Dollar", 2, "code"),
               ("risk", "Safe-haven", 2, "ai"), ("flows", "Flows", 1, "ai"), ("tech", "Structuur", 2, "code"), ("sent", "Sentiment", 1, "ai")],
    "US500": [("fed", "Fed-pad", 3, "ai"), ("yields", "Rente", 2, "code"), ("earn", "Winsten & AI", 2, "ai"),
              ("macro", "Macro-data", 2, "ai"), ("risk", "Risico & olie", 2, "code"), ("tech", "Structuur", 2, "code"), ("sent", "Sentiment", 1, "code")],
    "US100": [("fed", "Fed-pad", 3, "ai"), ("yields", "Rente", 3, "code"), ("earn", "AI & winsten", 3, "ai"),
              ("macro", "Macro-data", 1, "ai"), ("risk", "Risico & olie", 2, "code"), ("tech", "Structuur", 2, "code"), ("sent", "Sentiment", 1, "code")],
}
AI_GUIDE = {
    "fed": "Fed-pad: hawkish herprijzing (meer of eerder renteverhoging) is bearish voor goud en indices, dovish is bullish",
    "risk": "Safe-haven: geopolitieke spanning of aandelenstress is bullish voor goud, ontspanning bearish",
    "flows": "Flows: aankopen door centrale banken en ETF-instroom zijn bullish voor goud",
    "sent": "Sentiment: positionering en toon rond goud, contrair bij extremen",
    "earn": "Winsten en AI-nieuws: sterke cijfers en AI-optimisme bullish, AI-capex-zorgen en tegenvallers bearish",
    "macro": "Macro-data: sterke groei met beheerste inflatie bullish, hete inflatie of zwakke groei bearish",
}


def f_tech(s, dp):
    closes = [b["c"] for b in s["bars"]] + [s["px"]]
    p = s["px"]
    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    m5 = ret_n(s, 5)
    pts = n = 0
    for v in (s20, s50, s200):
        if v:
            pts += 1 if p > v else -1
            n += 1
    if s20 and s50:
        pts += 1 if s20 > s50 else -1
        n += 1
    if m5 is not None:
        pts += 1 if m5 > 0 else -1
        n += 1
    if not n:
        return None
    parts = []
    if s20:
        parts.append(f"{'boven' if p > s20 else 'onder'} het 20-daags gemiddelde ({nl(s20, dp)})")
    if s50:
        parts.append(f"{'boven' if p > s50 else 'onder'} het 50-daags ({nl(s50, dp)})")
    if s200:
        parts.append(f"{'boven' if p > s200 else 'onder'} het 200-daags ({nl(s200, dp)})")
    txt = "Koers " + ", ".join(parts) + (f"; 5-daags momentum {sgn(m5, 1, '%')}" if m5 is not None else "") + "."
    return clamp(jsround(2 * pts / n), -2, 2), txt, s["src"]


def f_rates(y10, real, gold):
    if gold and real and len(real) >= 6:
        lvl = real[-1][1]
        ch = (real[-1][1] - real[-6][1]) * 100
        what = f"Reële 10-jaarsrente (TIPS) {nl(lvl, 2)}%, {sgn(ch, 0)} bp in 5 dagen"
        src = f"{REAL_SRC[0] or 'FRED DFII10'}, {real[-1][0]}"
    elif y10 and len(y10["bars"]) >= 5:
        lvl = y10["px"]
        ch = (lvl - y10["bars"][-5]["c"]) * 100
        dch = (lvl - y10["prev"]) * 100 if y10.get("prev") is not None else None
        what = f"US 10-jaarsrente {nl(lvl, 2)}%, {sgn(ch, 0)} bp in 5 dagen" + (f" ({sgn(dch, 0)} bp vandaag)" if dch is not None else "")
        src = y10["src"]
    else:
        return None
    a = abs(ch)
    mag = 0 if a < 5 else 1 if a < 15 else 2
    mkt = "goud" if gold else "aandelen"
    concl = (f"rente daalt, dat steunt {mkt}" if ch < 0 else f"rente stijgt, dat drukt {mkt}") if mag else "rente nauwelijks veranderd, geen edge"
    return (-mag if ch > 0 else mag), f"{what}: {concl}.", src


def f_usd(dxy):
    w = ret_n(dxy, 5)
    if w is None:
        return None
    t = chg_pct(dxy)
    a = abs(w)
    mag = 0 if a < 0.3 else 1 if a < 1.0 else 2
    concl = ("dollar sterker, dat drukt goud" if w > 0 else "dollar zwakker, dat steunt goud") if mag else "dollar vrijwel vlak, geen edge"
    txt = f"DXY {nl(dxy['px'], 2)}, {sgn(w, 2, '%')} in 5 dagen" + (f" ({sgn(t, 2, '%')} vandaag)" if t is not None else "") + f": {concl}."
    return (-mag if w > 0 else mag), txt, dxy["src"]


def f_risk_idx(vix, wti):
    pts, parts = 0, []
    if vix:
        v, v5 = vix["px"], ret_n(vix, 5)
        pts += -2 if v >= 30 else -1 if v >= 24 else 1 if v <= 15 else 0
        if v5 is not None:
            pts += 1 if v5 <= -10 else -1 if v5 >= 10 else 0
        mood = "hoge angst" if v >= 30 else "verhoogd" if v >= 24 else "rustig" if v <= 15 else "normaal"
        parts.append(f"VIX {nl(v, 1)}, {mood}" + (f" ({sgn(v5, 0, '%')} in 5 dagen)" if v5 is not None else ""))
    if wti:
        w5 = ret_n(wti, 5)
        if w5 is not None:
            pts += 1 if w5 <= -5 else -1 if w5 >= 5 else 0
            parts.append(f"olie {sgn(w5, 1, '%')} in 5 dagen")
    if not parts:
        return None
    d = clamp(pts, -2, 2)
    concl = "dat drukt aandelen" if d < 0 else "dat steunt aandelen" if d > 0 else "geen duidelijke edge"
    return d, ", ".join(parts) + f": {concl}.", "Yahoo Finance"


def f_sent_idx(fg):
    if not fg:
        return None
    v = fg["v"]
    d = 2 if v < 15 else 1 if v < 25 else -2 if v > 85 else -1 if v > 75 else 0
    tail = "contrair bullish" if d > 0 else "contrair bearish" if d < 0 else "geen extreem, dus geen edge"
    return d, f"CNN Fear & Greed {v} ({fg['label'].lower()}): {tail}.", "CNN Fear & Greed"


def evaluate(factors, ep):
    sw = sum(f["w"] for f in factors)
    sdw = sum(f["d"] * f["w"] for f in factors)
    sab = sum(abs(f["d"]) * f["w"] for f in factors)
    score = jsround(100 * sdw / (2 * sw)) if sw else 0
    agree = abs(sdw) / sab if sab else 0
    cov = sum(1 for f in factors if f["fresh"]) / len(factors) if factors else 0
    conf = int(clamp(jsround(20 + 60 * agree * cov + 15 * min(1, abs(score) / 50) - ep), 10, 90))
    return score, conf, agree, cov


def label(score):
    return "Bullish" if score >= 40 else "Licht bullish" if score >= 15 else "Neutraal" if score > -15 else "Licht bearish" if score > -40 else "Bearish"


def levels(s, dp):
    bars, p = s["bars"], s["px"]
    cand = []
    if bars:
        cand += [(bars[-1]["h"], "vorige dag high"), (bars[-1]["l"], "vorige dag low")]
        wk = s["day"].isocalendar()[:2]
        prevw = [b for b in bars if b["d"].isocalendar()[:2] < wk]
        if prevw:
            last = prevw[-1]["d"].isocalendar()[:2]
            wb = [b for b in prevw if b["d"].isocalendar()[:2] == last]
            cand += [(max(b["h"] for b in wb), "vorige week high"), (min(b["l"] for b in wb), "vorige week low")]
        l20 = bars[-20:]
        cand += [(max(b["h"] for b in l20), "hoogste punt 20 dagen"), (min(b["l"] for b in l20), "laagste punt 20 dagen")]
    if s.get("hi") and s.get("lo"):
        cand += [(s["hi"], "high van de laatste sessie"), (s["lo"], "low van de laatste sessie")]
    closes = [b["c"] for b in bars] + [p]
    for n in (50, 200):
        v = sma(closes, n)
        if v:
            cand.append((v, f"{n}-daags gemiddelde", "pivot"))
    step = 100 if p < 10000 else 500
    below = math.floor(p / step) * step
    cand += [(below + step, "rond getal", "pivot"), (below, "rond getal", "pivot"),
             (below + 2 * step, "rond getal", "pivot"), (below - step, "rond getal", "pivot")]
    out = []
    for c in cand:
        v, note = c[0], c[1]
        if abs(v / p - 1) > 0.08:
            continue
        if any(abs(v / o["p"] - 1) < 0.0015 for o in out) or abs(v / p - 1) < 0.0005:
            continue
        kind = c[2] if len(c) > 2 else ("supply" if v >= p else "demand")
        out.append({"p": round(v, dp), "kind": kind, "note": note, "src": s["src"]})
    out.sort(key=lambda o: abs(o["p"] / p - 1))
    return sorted(out[:6], key=lambda o: -o["p"])


# ---------------------------------------------------------------- AI
def parse_json(text):
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1)
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("geen JSON in antwoord: " + text[:160])
    return json.loads(t[a:b + 1])


def claude_call(system, user, key, max_tokens):
    payload = {"model": os.environ.get("KAIROS_CLAUDE_MODEL") or "claude-haiku-5-5", "max_tokens": max(8000, max_tokens * 3),
               "system": system, "messages": [{"role": "user", "content": user}]}
    r = api_json("https://api.anthropic.com/v1/messages", headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                 payload=payload, tries=3)
    txt = "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")
    if not txt.strip():
        raise ValueError(f"leeg antwoord (stop_reason {r.get('stop_reason')})")
    return parse_json(txt)


GEM = "https://generativelanguage.googleapis.com/v1beta"
GEM_PREF = [r"gemini-flash-latest", r"gemini-\d+(?:\.\d+)?-flash", r"gemini-flash-lite-latest", r"gemini-\d+(?:\.\d+)?-flash-lite"]
GROQ = "https://api.groq.com/openai/v1"
GROQ_PREF = ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "llama-3.3-70b-versatile", "openai/gpt-oss-20b"]
_MODELS = {}


def _version(name):
    return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", name)] or [0.0]


def gemini_models(key):
    if "gemini" in _MODELS:
        return _MODELS["gemini"]
    want = os.environ.get("KAIROS_GEMINI_MODEL", "").strip()
    names = []
    try:
        j = api_json(f"{GEM}/models?pageSize=200", headers={"x-goog-api-key": key}, timeout=30)
        names = [m["name"].split("/", 1)[-1] for m in j.get("models", [])
                 if "generateContent" in (m.get("supportedGenerationMethods") or [])]
    except Exception as e:
        log("[!] Gemini-modellijst", e)
    order = [want] if want else []
    for pat in GEM_PREF:
        hits = sorted((n for n in names if re.fullmatch(pat, n)), key=_version, reverse=True)
        order += hits[:1]
    if not names:
        order += ["gemini-flash-latest", "gemini-flash-lite-latest"]
    out = []
    for m in order:
        if m and m not in out:
            out.append(m)
    _MODELS["gemini"] = out[:3]
    return _MODELS["gemini"]


def gemini_call(system, user, key, model, max_tokens):
    payload = {"systemInstruction": {"parts": [{"text": system}]},
               "contents": [{"role": "user", "parts": [{"text": user}]}],
               "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3,
                                    "maxOutputTokens": max(8192, max_tokens * 4)}}
    r = api_json(f"{GEM}/models/{model}:generateContent", headers={"x-goog-api-key": key}, payload=payload, tries=2)
    c = (r.get("candidates") or [{}])[0]
    txt = "".join(pt.get("text", "") for pt in (c.get("content") or {}).get("parts", []) if not pt.get("thought"))
    if not txt.strip():
        raise ValueError(f"leeg antwoord ({c.get('finishReason') or r.get('promptFeedback')})")
    return parse_json(txt)


def groq_models(key):
    if "groq" in _MODELS:
        return _MODELS["groq"]
    want = os.environ.get("KAIROS_GROQ_MODEL", "").strip()
    avail = None
    try:
        j = api_json(f"{GROQ}/models", headers={"Authorization": f"Bearer {key}"}, timeout=30)
        avail = {m.get("id") for m in j.get("data", []) if m.get("active", True)}
    except Exception as e:
        log("[!] Groq-modellijst", e)
    order = []
    for m in ([want] if want else []) + GROQ_PREF:
        if m and m not in order and (avail is None or m in avail):
            order.append(m)
    if avail and len(order) < 2:  # nieuwe modelnamen: neem herkenbare grote tekstmodellen
        extra = sorted((m for m in avail if re.search(r"gpt-oss|qwen|llama|kimi|deepseek", m)
                        and not re.search(r"guard|whisper|tts|vision|8b|20b", m)), key=_version, reverse=True)
        order += [m for m in extra if m not in order]
    _MODELS["groq"] = order[:2]
    return _MODELS["groq"]


def groq_call(system, user, key, model, max_tokens):
    # gratis Groq: ongeveer 8.000 tokens per minuut, dus vraag niet meer dan er past
    est = (len(system) + len(user)) / 3.0
    payload = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
               "temperature": 0.3, "max_completion_tokens": int(max(1500, min(max_tokens + 1000, 7600 - est))),
               "response_format": {"type": "json_object"}}
    if "gpt-oss" in model:
        payload["reasoning_effort"] = "medium"
    elif "qwen" in model:
        payload["reasoning_format"] = "hidden"
    try:
        r = api_json(f"{GROQ}/chat/completions", headers={"Authorization": f"Bearer {key}"}, payload=payload, tries=3)
    except RuntimeError as e:
        if "HTTP 400" not in str(e):
            raise
        # sommige modellen weigeren JSON-modus of de denk-instellingen: probeer het kaal, parse_json vist de JSON er zelf uit
        log(f"[i] Groq {model}: opnieuw zonder extra opties ({str(e)[:120]})")
        for k in ("response_format", "reasoning_effort", "reasoning_format"):
            payload.pop(k, None)
        r = api_json(f"{GROQ}/chat/completions", headers={"Authorization": f"Bearer {key}"}, payload=payload, tries=3)
    ch, u = r["choices"][0], r.get("usage") or {}
    log(f"[i] Groq {model}: {u.get('prompt_tokens')} tokens in, {u.get('completion_tokens')} uit, einde: {ch.get('finish_reason')}")
    if ch.get("finish_reason") == "length" and payload.get("reasoning_effort") == "medium":
        payload["reasoning_effort"] = "low"  # te lang nagedacht: nog een keer, korter
        r = api_json(f"{GROQ}/chat/completions", headers={"Authorization": f"Bearer {key}"}, payload=payload, tries=3)
        ch = r["choices"][0]
    return parse_json(ch["message"].get("content") or "")


def ai_json(system, user, max_tokens=3500):
    errors = []
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        try:
            out = claude_call(system, user, key, max_tokens)
            ENGINE["ai"] = "Claude Haiku 5.5 (API)"
            return out
        except Exception as e:
            errors.append(f"Claude: {e}")
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        for model in gemini_models(key):
            try:
                out = gemini_call(system, user, key, model, max_tokens)
                ENGINE["ai"] = f"Google Gemini ({model})"
                return out
            except Exception as e:
                errors.append(f"Gemini {model}: {e}")
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if key:
        for model in groq_models(key):
            try:
                out = groq_call(system, user, key, model, max_tokens)
                ENGINE["ai"] = f"Groq ({model})"
                return out
            except Exception as e:
                errors.append(f"Groq {model}: {e}")
    raise RuntimeError("; ".join(errors)[:600] or "geen AI-sleutel ingesteld (zie README)")


SYSTEM = ("Je bent de macro-analist van KAIROS, een bias-desk voor een supply-and-demand daytrader in Brussel die XAUUSD (goud), "
          "US500 en US100 handelt, vooral in de New York-sessie. Werk alleen met de gegeven data en koppen. Verzin nooit cijfers, "
          "niveaus of feiten. Schrijf alle tekst in helder Nederlands (goud, niet gold), kort en zonder markdown. Antwoord met één JSON-object en niets anders.")


# ---------------------------------------------------------------- run
def session_of(now_bxl):
    h = now_bxl.hour
    return "london" if 4 <= h < 12 else "ny" if 12 <= h < 20 else "close"


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def data_pack(S, cal, heads, fg, code_f, session):
    nowb = datetime.now(BXL)
    L = [f"Nu: {DOW[nowb.weekday()]} {nowb:%d-%m-%Y %H:%M} Brussel. Deze run: {SESSION_NL[session]}.", "", "KOERSEN (laatste | vandaag | 5 dagen | 20 dagen):"]
    for k in ["XAUUSD", "US500", "US100", "ES", "NQ", "DXY", "US10Y", "VIX", "WTI", "BRENT", "XAGUSD", "US30", "BTCUSD"]:
        s = S.get(k)
        if s:
            dp = INSTR[k]["dp"]
            L.append(f"- {k} ({INSTR[k]['name']}): {nl(s['px'], dp)} | {sgn(chg_pct(s), 2, '%')} | {sgn(ret_n(s, 5), 1, '%')} | {sgn(ret_n(s, 20), 1, '%')}")
    if fg:
        L.append(f"- CNN Fear & Greed: {fg['v']} ({fg['label']})" + (f", vorige {fg['prev']}" if fg.get("prev") is not None else ""))
    L += ["", "VASTE FACTOREN (berekend uit de data, niet aanpassen):"]
    for a in PRIMARY:
        for key, lab, w, kind in MODEL[a]:
            if kind == "code" and code_f[a].get(key):
                d, txt, _ = code_f[a][key]
                L.append(f"- {a} {lab}: {d:+d}. {txt}")
    L += ["", "KALENDER (Brusselse tijd):"]
    for x in cal[:14]:
        t = datetime.fromisoformat(x["t"].replace("Z", "+00:00"))
        L.append(f"- [{x['id']}] {when_nl(t)} {x['cty']} {x['en']} (impact {x['imp']}/3, vorige {x['prev'] or '-'}, verwacht {x['cons'] or '-'})")
    L += ["", "NIEUWSKOPPEN (nieuwste eerst):"]
    for i, n in enumerate(heads):
        L.append(f"[{i}] {n['t'].strftime('%d-%m %H:%MZ') if n['t'] else '?'} | {n['src'] or '?'} | {n['h']}")
    return "\n".join(L)


def step1_prompt(pack):
    lines = [pack, "", "OPDRACHT 1: beoordeel de kwalitatieve factoren met een richting d van -2 tot +2 (+ is bullish voor die markt).",
             "Vul ELKE factor hieronder in. Gebruik ±2 alleen bij sterk en duidelijk bewijs. Is het nieuws gemengd of neutraal, geef dan d 0 met fresh true.",
             "Zet fresh false alleen als geen enkele kop iets over die factor zegt. Uitspraken van Fed-leden, rentekansen (FedWatch) en renteverwachtingen tellen als bewijs voor het Fed-pad.",
             "In text staat het bewijs in eigen woorden (welke kop of welk cijfer), in src de bron en datum van die kop.",
             "Factoren:"]
    for a in PRIMARY:
        ks = [k for k, _, _, kind in MODEL[a] if kind == "ai"]
        lines.append(f"- {a}: " + "; ".join(AI_GUIDE[k] for k in ks))
    ex = {"d": 0, "fresh": True, "text": "bewijs in max 200 tekens", "src": "bron, DD-MM"}
    shape = {"factors": {a: {k: ex for k, _, _, kind in MODEL[a] if kind == "ai"} for a in PRIMARY},
             "fed": {"meeting": "JJJJ-MM-DD", "hike": 0, "hold": 0, "cut": 0, "src": "bron"},
             "leans": {k: 0 for k in ("DXY", "US10Y", "VIX", "WTI", "BRENT", "XAGUSD", "US30", "BTCUSD")},
             "news": [{"i": 0, "tags": [{"id": "XAUUSD", "d": 1}], "why": "max 110 tekens"}]}
    lines += [
        "Geef ook: de marktverwachting voor de volgende Fed-vergadering in procenten, alleen als een kop die noemt (anders \"fed\": null);",
        "een richting (lean, -2..2 of null) voor DXY, US10Y, VIX, WTI, BRENT, XAGUSD, US30 en BTCUSD;",
        "en kies de 6 belangrijkste koppen met tags per markt (d +1 bullish, -1 bearish) en één zin 'why' in het Nederlands.",
        "JSON-vorm (vul de echte waarden in): " + json.dumps(shape, ensure_ascii=False),
    ]
    return "\n".join(lines)


def step2_prompt(pack, assets, session):
    L = [pack, "", "DEFINITIEVE DESK (berekend; neem labels, richtingen en getallen exact over):"]
    for a in assets:
        dp = INSTR[a["id"]]["dp"]
        L.append(f"- {a['id']}: {label(a['score'])}, zekerheid {a['conf']}%, event-risico {a['ep']} ({a['epWhy'] or 'geen'}), "
                 f"koers {nl(a['px'], dp) if a['px'] is not None else '-'}, vandaag {sgn(a['chgPct'], 2, '%')}.")
        for f in a["factors"]:
            way = "steunt" if f["d"] > 0 else "drukt" if f["d"] < 0 else "neutraal"
            L.append(f"    · {f['label']} ({way}, {f['d']:+d}): {f['text'] if f['fresh'] else 'geen verse data'}")
        for o in a["levels"]:
            pos = "boven" if a["px"] and o["p"] > a["px"] else "onder"
            dist = abs(o["p"] / a["px"] - 1) * 100 if a["px"] else 0
            L.append(f"    · level {o['kind']} {nl(o['p'], dp)}: {nl(dist, 1)}% {pos} de koers ({o['note']})")
    nxt = "de volgende sessie (morgen Londen en New York)" if session == "close" else "de komende sessie"
    L += ["", f"OPDRACHT 2: schrijf de desk-tekst voor {nxt}.",
          "Regels: volg de labels hierboven en spreek ze nooit tegen. Noem rente, dollar, VIX en momentum alleen met de richting uit de factorregels "
          "(staat er 'rente daalt', schrijf dan nooit 'stijgende rente'). Zet geen scores in de tekst. Verzin geen niveaus: gebruik alleen de levels hierboven.",
          "Supply & demand-logica: een short-idee hoort bij een supply-zone BOVEN de koers, na een sweep van de highs boven die zone en een break naar beneden. "
          "Een long-idee hoort bij een demand-zone ONDER de koers, na een sweep van de lows eronder en een break omhoog.",
          "Velden per markt:",
          "- call: begint met het label van die markt hierboven (zoals Licht bearish), niet met de naam van de markt, dan de zone die ertoe doet, in de vorm "
          "'<label> zolang <level> (supply) houdt; pas daarboven nemen kopers het over.' of '<label> zolang <level> (demand) houdt; pas daaronder nemen verkopers het over.' "
          "Gebruik alleen getallen uit de levels van die markt. Geen order met entry en target.",
          "- thesis: waarom, met de twee of drie zwaarste factoren en hun richting zoals in de factorregels.",
          "- flip: wat de bias laat omslaan (een level of een event).",
          "- risk: het grootste risico voor deze bias (een cijfer, event of level). Geen advies over positiegrootte of stop-loss.",
          "- playbook: het S&D-plan met de levels hierboven: welke zone voorrang heeft, wachten op sweep en market-structure break, "
          "killzones Londen 08:00-11:00 en New York 13:00-16:00 Brusselse tijd, en wanneer je aan de kant blijft. Geen garanties.",
          "- moveNote: alleen als de beweging van vandaag minstens 0,25% tegen een bias van ±15 of meer ingaat, anders null.",
          "- headline: één zin over wat de markt nu drijft, in gewoon Nederlands, zonder labels zoals bullish of bearish.",
          "- regime: label is het marktregime in twee tot vier woorden (bijvoorbeeld Risk-off, Risk-on, Afwachten op data, Rente-gedreven), tone -1, 0 of 1.",
          'JSON-vorm: {"headline":"max 140 tekens","summary":["3 zinnen van max 130 tekens"],"regime":{"label":"max 40 tekens","tone":-1,"text":"max 240 tekens"},'
          '"assets":{"XAUUSD":{"call":"max 120","thesis":"max 320","flip":"max 160","risk":"max 140","playbook":"max 260","moveNote":null},'
          '"US500":{"call":"","thesis":"","flip":"","risk":"","playbook":"","moveNote":null},"US100":{"call":"","thesis":"","flip":"","risk":"","playbook":"","moveNote":null}},'
          '"chatter":"max 200 tekens: waar traders en media nu op letten","calNotes":[{"id":"e0","note":"max 80 tekens"}]}']
    return "\n".join(L)


LABEL_RE = re.compile(r"^\s*(licht\s+bullish|licht\s+bearish|bullish|bearish|neutraal)\b[\s:,;.\-–]*", re.I)
LEAD_RE = re.compile(r"^\s*(xauusd|xau/usd|goud|us500|us100|s&p\s*500|nasdaq(\s*100)?)\b[\s:,;.\-–]*", re.I)
SOFT_START = ("zolang", "onder", "boven", "tot", "zodra", "pas", "met", "bij", "zonder")


def fix_texts(a):
    """Vangnet voor de AI-tekst: de call begint met het berekende label (niet met de marktnaam of een ander
    label), en een moveNote mag alleen bestaan als de koers van vandaag echt tegen een duidelijke bias ingaat."""
    lab = label(a["score"])
    call = a.get("call")
    if isinstance(call, str) and call.strip():
        rest = call.strip()
        for _ in range(2):  # marktnaam en/of label vooraan weghalen, in elke volgorde
            rest = LEAD_RE.sub("", rest, count=1)
            rest = LABEL_RE.sub("", rest, count=1)
        first = rest.split(" ", 1)[0].lower()
        a["call"] = f"{lab} {first}{rest[len(first):]}" if first in SOFT_START else f"{lab}: {rest}" if rest else lab
    cp = a.get("chgPct")
    against = cp is not None and abs(a["score"]) >= 15 and abs(cp) >= 0.25 and (cp > 0) != (a["score"] > 0)
    if not against:
        a.pop("moveNote", None)


def fallback_text(assets, cal):
    nxt = next((x for x in cal if x["imp"] >= 3 and x["cty"] == "US" and x["t"] > iso(NOW)), None)
    for a in assets:
        lab = label(a["score"])
        pos = [f["label"].lower() for f in a["factors"] if f["d"] > 0]
        neg = [f["label"].lower() for f in a["factors"] if f["d"] < 0]
        a["call"] = f"{lab} volgens de rekenregels; wacht op bevestiging in je eigen zone."
        a["thesis"] = ("Steun: " + (", ".join(pos) or "geen") + ". Druk: " + (", ".join(neg) or "geen") + ".")
        a["flip"] = "Een duidelijke breuk van de dichtstbijzijnde zone tegen de bias in."
        a["risk"] = a["epWhy"] or "Geen grote Amerikaanse data binnen 24 uur."
        a["playbook"] = "Werk alleen vanuit je eigen zones met bevestiging (sweep en MSB) in de killzone; blijf aan de kant rond grote data."
    head = ", ".join(f"{a['name']} {label(a['score']).lower()}" for a in assets)
    return {"headline": head + (f". Let op: {nxt['ev']} {when_nl(datetime.fromisoformat(nxt['t'].replace('Z', '+00:00')))}" if nxt else ""),
            "summary": [], "regime": None, "chatter": None}


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    now_bxl = datetime.now(BXL)
    session = session_of(now_bxl)
    run_id = now_bxl.strftime("%Y%m%d-%H%M")
    prev = load_json(os.path.join(DATA_DIR, "desk.json"), {})
    log(f"KAIROS desk-run {run_id} ({session})")

    S = load_series()
    if sum(1 for k in PRIMARY if k in S) < 2:
        log("[x] Te weinig koersen voor de hoofdmarkten; de vorige desk blijft staan.")
        sys.exit(1)
    real, REAL_SRC[0] = None, None
    for name, getter in (("FRED DFII10", lambda: fred("DFII10")), ("US Treasury", treasury_real10)):
        try:
            real = getter()
            if real and len(real) >= 6:
                REAL_SRC[0] = name
                log(f"[ok] reële rente {real[-1]} via {name}")
                break
            real = None
        except Exception as e:
            log(f"[!] reële rente via {name}: {e}")
    cal = calendar()
    heads = news()
    fg = fear_greed()
    ep, ep_why = event_penalty(cal)

    code_f = {
        "XAUUSD": {"yields": f_rates(S.get("US10Y"), real, True), "usd": f_usd(S["DXY"]) if "DXY" in S else None,
                   "tech": f_tech(S["XAUUSD"], 2) if "XAUUSD" in S else None},
        "US500": {"yields": f_rates(S.get("US10Y"), None, False), "risk": f_risk_idx(S.get("VIX"), S.get("WTI")),
                  "tech": f_tech(S["US500"], 1) if "US500" in S else None, "sent": f_sent_idx(fg)},
        "US100": {"yields": f_rates(S.get("US10Y"), None, False), "risk": f_risk_idx(S.get("VIX"), S.get("WTI")),
                  "tech": f_tech(S["US100"], 1) if "US100" in S else None, "sent": f_sent_idx(fg)},
    }
    pack = data_pack(S, cal, heads, fg, code_f, session)

    ai1 = None
    p1 = step1_prompt(pack)
    log(f"[i] prompt 1: {len(p1)} tekens")
    try:
        ai1 = ai_json(SYSTEM, p1, 3000)
        log(f"[ok] AI stap 1 via {ENGINE['ai']}")
    except Exception as e:
        NOTES.append("AI-analyse niet beschikbaar, alleen rekenregels")
        log("[!] AI stap 1", e)

    prev_f = {a.get("id"): {f.get("k"): f for f in a.get("factors", [])} for a in prev.get("assets", [])}
    assets = []
    for aid in PRIMARY:
        s = S.get(aid)
        factors = []
        for key, lab, w, kind in MODEL[aid]:
            d, fresh, txt, src = 0, False, "Geen verse data in deze run.", ""
            old = prev_f.get(aid, {}).get(key)
            if old and isinstance(old.get("d"), (int, float)):
                d, txt, src = int(old["d"]), old.get("text") or txt, old.get("src") or ""
            if kind == "code" and code_f[aid].get(key):
                d, txt, src = code_f[aid][key]
                fresh = True
            elif kind == "ai" and ai1:
                v = ((ai1.get("factors") or {}).get(aid) or {}).get(key) or {}
                if isinstance(v.get("d"), (int, float)):
                    d = int(clamp(jsround(v["d"]), -2, 2))
                    fresh = v.get("fresh") is not False
                    txt = str(v.get("text") or txt)[:240]
                    src = str(v.get("src") or "nieuwskoppen")[:80]
            factors.append({"k": key, "label": lab, "w": w, "d": d, "fresh": fresh, "text": txt, "src": src})
        score, conf, agree, cov = evaluate(factors, ep)
        dp = INSTR[aid]["dp"]
        note = f" ({s['note']})" if s and s.get("note") else ""
        assets.append({
            "id": aid, "name": INSTR[aid]["name"],
            "px": round(s["px"], dp) if s else None,
            "chg": round(chg(s), dp) if s and chg(s) is not None else None,
            "chgPct": round(chg_pct(s), 2) if s and chg_pct(s) is not None else None,
            "hi": round(s["hi"], dp) if s and s.get("hi") else None, "lo": round(s["lo"], dp) if s and s.get("lo") else None,
            "pxSrc": (s["src"] + note) if s else None, "pxAt": iso(s["at"]) if s and s.get("at") else iso(NOW),
            "factors": factors, "ep": ep, "epWhy": ep_why, "score": score, "conf": conf,
            "levels": levels(s, dp) if s else [],
        })
        log(f"    {aid}: {label(score)} {score:+d}, zekerheid {conf}%")

    texts = None
    if ai1:
        try:
            picked = [x.get("i") for x in (ai1.get("news") or []) if isinstance(x, dict) and isinstance(x.get("i"), int)]
            heads2 = [heads[i] for i in picked if 0 <= i < len(heads)][:10] or heads[:10]
            p2 = step2_prompt(data_pack(S, cal, heads2, fg, code_f, session), assets, session)
            log(f"[i] prompt 2: {len(p2)} tekens")
            texts = ai_json(SYSTEM, p2, 3500)
            log("[ok] AI stap 2")
        except Exception as e:
            NOTES.append("AI-tekst niet beschikbaar, standaardtekst gebruikt")
            log("[!] AI stap 2", e)
    fb = fallback_text(assets, cal)
    if texts:
        for a in assets:
            t = (texts.get("assets") or {}).get(a["id"]) or {}
            for k, lim in (("call", 140), ("thesis", 360), ("flip", 200), ("risk", 180), ("playbook", 320), ("moveNote", 180)):
                v = t.get(k)
                if isinstance(v, str) and v.strip():
                    a[k] = v.strip()[:lim]
            fix_texts(a)
    headline = (texts or {}).get("headline") or fb["headline"]
    summary = [str(x)[:160] for x in ((texts or {}).get("summary") or [])][:3]
    regime = (texts or {}).get("regime") if isinstance((texts or {}).get("regime"), dict) else None

    notes = {str(n.get("id")): str(n.get("note"))[:100] for n in ((texts or {}).get("calNotes") or []) if isinstance(n, dict) and n.get("note")}
    calendar_out = []
    for x in cal:
        item = {k: x[k] for k in ("t", "cty", "ev", "imp", "prev", "cons", "act", "hits")}
        if notes.get(x["id"]):
            item["note"] = notes[x["id"]]
        calendar_out.append(item)

    leans = (ai1 or {}).get("leans") or {}
    context = []
    for k in ["ES", "NQ", "DXY", "US10Y", "VIX", "WTI", "BRENT", "XAGUSD", "US30", "BTCUSD"]:
        s = S.get(k)
        if not s:
            continue
        if k in ("ES", "NQ"):
            base = next((a for a in assets if a["id"] == ("US500" if k == "ES" else "US100")), None)
            lean = int(clamp(jsround(base["score"] / 25), -2, 2)) if base else None
        else:
            v = leans.get(k)
            lean = int(clamp(jsround(v), -2, 2)) if isinstance(v, (int, float)) else None
        context.append({"id": k, "name": INSTR[k]["name"], "px": round(s["px"], INSTR[k]["dp"]),
                        "chgPct": round(chg_pct(s), 2) if chg_pct(s) is not None else None, "lean": lean})

    picked = []
    for n in ((ai1 or {}).get("news") or [])[:8]:
        try:
            h = heads[int(n.get("i"))]
        except Exception:
            continue
        tags = [{"id": str(t.get("id")), "d": int(clamp(jsround(t.get("d", 0)), -1, 1))} for t in (n.get("tags") or []) if isinstance(t, dict) and t.get("id") in PRIMARY]
        picked.append({"t": iso(h["t"]) if h["t"] else iso(NOW), "h": h["h"], "src": h["src"], "url": h["url"], "tags": tags, "why": str(n.get("why") or "")[:140]})
    if not picked:
        picked = [{"t": iso(h["t"]) if h["t"] else iso(NOW), "h": h["h"], "src": h["src"], "url": h["url"], "tags": [], "why": ""} for h in heads[:6]]
    picked.sort(key=lambda x: x["t"], reverse=True)

    fed = (ai1 or {}).get("fed")
    if not (isinstance(fed, dict) and any(isinstance(fed.get(k), (int, float)) for k in ("hike", "hold", "cut"))):
        fed = None

    desk = {
        "v": 2, "runId": run_id, "asof": iso(NOW), "session": session,
        "engine": {"ai": ENGINE["ai"], "host": "GitHub Actions"},
        "headline": str(headline)[:180], "summary": summary, "regime": regime,
        "assets": assets, "context": context, "calendar": calendar_out, "news": picked, "fed": fed,
        "sentiment": {"fg": fg, "chatter": (texts or {}).get("chatter"), "chatterSrc": "nieuwskoppen" if (texts or {}).get("chatter") else None},
        "sources": [
            {"n": "Yahoo Finance (koersen)", "u": "https://finance.yahoo.com"}, {"n": "Swissquote (goud en zilver spot)", "u": "https://www.swissquote.com"},
            {"n": "ForexFactory (kalender)", "u": "https://www.forexfactory.com/calendar"}, {"n": "Google News (koppen)", "u": "https://news.google.com"},
            {"n": "CNN Fear & Greed", "u": "https://edition.cnn.com/markets/fear-and-greed"}, {"n": "FRED / US Treasury (reële rente)", "u": "https://fred.stlouisfed.org/series/DFII10"},
        ] + [{"n": f"{n['src'] or 'Nieuws'}: {n['h'][:60]}", "u": n["url"]} for n in picked[:6] if n.get("url")],
        "health": {"missing": MISSING, "note": "; ".join(NOTES)[:240]},
    }
    with open(os.path.join(DATA_DIR, "desk.json"), "w", encoding="utf-8") as fh:
        json.dump(desk, fh, ensure_ascii=False, separators=(",", ":"))

    runs = load_json(os.path.join(DATA_DIR, "runs.json"), [])
    if not isinstance(runs, list):
        runs = []
    runs = [r for r in runs if r.get("runId") != run_id]
    runs.append({"runId": run_id, "asof": iso(NOW), "session": session, "calls": {
        a["id"]: {"px": a["px"], "d": [f["d"] for f in a["factors"]], "w": [f["w"] for f in a["factors"]],
                  "f": [1 if f["fresh"] else 0 for f in a["factors"]], "ep": a["ep"], "score": a["score"], "conf": a["conf"]}
        for a in assets}})
    runs = runs[-600:]
    with open(os.path.join(DATA_DIR, "runs.json"), "w", encoding="utf-8") as fh:
        json.dump(runs, fh, ensure_ascii=False, separators=(",", ":"))
    log(f"Klaar. AI: {ENGINE['ai']}. Ontbrak: {', '.join(MISSING) or 'niets'}.")


if __name__ == "__main__":
    main()
