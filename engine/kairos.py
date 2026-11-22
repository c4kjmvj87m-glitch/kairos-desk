#!/usr/bin/env python3
"""KAIROS desk-run (v3) voor GitHub.

Schrijft data/desk.json en data/runs.json in exact dezelfde vorm als de desk-run van Claude,
zodat de pagina (een kopie van de Claude-desk) er precies hetzelfde uitziet en alles kan tonen.

Gegevens (gratis):
  goud        spotprijs van Swissquote; uur- en kwartierkaarsen van het actieve COMEX-contract,
              omgerekend naar spot met het verschil op hetzelfde moment
  US500/100   cash-index van Yahoo; buiten de beurstijden de E-mini-future, omgerekend met het
              verschil bij het laatste slot (zoals een CFD bij je broker)
  dollar      ICE-dollarindex, rente via Yahoo en het Amerikaanse ministerie van Financiën
  Fed         kansen berekend uit fed-funds-futures, rente van de NY Fed, vergaderdata van de Fed
  agenda      ForexFactory; nieuws via Google News; stemming CNN Fear & Greed en Stocktwits
AI: de eerste sleutel die werkt (ANTHROPIC_API_KEY, GEMINI_API_KEY, GROQ_API_KEY). Alleen met Claude
zoekt de desk ook op internet naar X-berichten, de FedWatch-kans en uitkomsten van cijfers.
"""
import json
import math
import os
import re
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import desk as L  # noqa: E402  (bouwstenen: ophalen, agenda, nieuws, factormodel, AI-aanroepen)

UTC, BXL, NY = L.UTC, L.BXL, ZoneInfo("America/New_York")
NOW = L.NOW
log, iso, clamp, jsround, nl, sgn = L.log, L.iso, L.clamp, L.jsround, L.nl, L.sgn
MISSING, NOTES, ENGINE = L.MISSING, L.NOTES, L.ENGINE
MON = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]
DAG = ["ma", "di", "wo", "do", "vr", "za", "zo"]
CALC = "berekend uit koersdata"
PRIMARY = ["XAUUSD", "US500", "US100"]
NAMES = {"XAUUSD": "Goud", "US500": "S&P 500", "US100": "Nasdaq 100"}
DP = {"XAUUSD": 2, "US500": 2, "US100": 2, "DXY": 3}
SEARCHES = int(os.environ.get("KAIROS_SEARCHES") or 3)
DATA_DIR = L.DATA_DIR


def r(x, dp=2):
    return None if x is None else round(float(x), dp)


def pct(a, b):
    return None if a is None or not b else (a / b - 1) * 100


def day_label(t):
    t = t.astimezone(BXL)
    return f"{t.day} {MON[t.month - 1]}"


def safe(fn, what, default=None):
    try:
        return fn()
    except Exception as e:
        log(f"[!] {what}: {str(e)[:220]}")
        return default


# ---------------------------------------------------------------- Yahoo
def ychart(sym, rng, iv, prepost=True):
    q = urllib.parse.quote(sym, safe="")
    err = None
    for host in ("query1", "query2"):
        url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/{q}?range={rng}&interval={iv}"
               f"&includePrePost={'true' if prepost else 'false'}")
        try:
            j = json.loads(L.fetch(url, tries=1))
            res = ((j.get("chart") or {}).get("result") or [None])[0]
            if not res:
                raise RuntimeError("leeg antwoord")
            q0 = ((res.get("indicators") or {}).get("quote") or [{}])[0]
            vols = q0.get("volume") or []
            bars = [{"t": t, "o": o, "h": h, "l": lo, "c": c, "v": (vols[i] if i < len(vols) and vols[i] else 0)}
                    for i, (t, o, h, lo, c) in enumerate(zip(res.get("timestamp") or [], q0.get("open") or [], q0.get("high") or [],
                                                             q0.get("low") or [], q0.get("close") or []))
                    if None not in (o, h, lo, c) and c > 0]
            return {"meta": res.get("meta") or {}, "bars": bars}
        except Exception as e:
            err = e
    raise RuntimeError(f"Yahoo {sym} {iv}: {err}")


def yfetch_many(reqs):
    """Meerdere Yahoo-aanvragen tegelijk; geeft per sleutel het resultaat of None."""
    out = {}
    with ThreadPoolExecutor(5) as ex:
        futs = {k: ex.submit(ychart, *args) for k, args in reqs.items()}
        for k, f in futs.items():
            try:
                out[k] = f.result()
            except Exception as e:
                out[k] = None
                log(f"[!] {k}: {str(e)[:160]}")
    return out


MCODE = "FGHJKMNQUVXZ"


def gold_contracts(now=NOW):
    """De twee eerstvolgende goudcontracten (feb/apr/jun/aug/okt/dec) waarvan de leveringsmaand nog niet begonnen is."""
    out = []
    for k in range(14):
        y, m = now.year + (now.month - 1 + k) // 12, (now.month - 1 + k) % 12 + 1
        if m % 2 == 0 and datetime(y, m, 1, tzinfo=UTC) > now:
            out.append(f"GC{MCODE[m - 1]}{y % 100:02d}.CMX")
        if len(out) == 2:
            break
    return out


def gold_contract(now=NOW):
    """Het actiefste contract: het meeste volume in de laatste 24 uurkaarsen (zo is het verschil met spot het stabielst)."""
    cands = gold_contracts(now)
    best, vol = None, -1
    for c in cands:
        try:
            v = sum(b["v"] for b in ychart(c, "5d", "1h")["bars"][-24:])
        except Exception:
            v = -1
        if v > vol:
            best, vol = c, v
    log(f"[i] goudcontract {best} (volume 24 uur {vol}; kandidaten {', '.join(cands)})")
    return best or "GC=F"


def index_contract(root, exch, now=NOW):
    """Kwartaalcontract (mrt/jun/sep/dec); rolt 8 dagen voor de derde vrijdag."""
    for k in range(15):
        y, m = now.year + (now.month - 1 + k) // 12, (now.month - 1 + k) % 12 + 1
        if m not in (3, 6, 9, 12):
            continue
        third_fri = 15 + (4 - date(y, m, 15).weekday()) % 7
        if now < datetime(y, m, third_fri, tzinfo=UTC) - timedelta(days=8):
            return f"{root}{MCODE[m - 1]}{y % 100:02d}.{exch}"
    return f"{root}=F"


def ny_day(ts):
    """Handelsdag zoals bij CFD's: van 17:00 tot 17:00 New York."""
    return (datetime.fromtimestamp(ts, NY) + timedelta(hours=7)).date()


def day_candles(bars):
    days = {}
    for b in bars:
        d = ny_day(b["t"])
        if d.weekday() == 5:  # na het slot op vrijdag hoort bij vrijdag
            d -= timedelta(days=1)
        elif d.weekday() == 6:  # de opening op zondagavond hoort bij maandag
            d += timedelta(days=1)
        x = days.get(d)
        if x is None:
            days[d] = {"d": d, "t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]}
        else:
            x["h"], x["l"], x["c"] = max(x["h"], b["h"]), min(x["l"], b["l"]), b["c"]
    return [days[k] for k in sorted(days)]


def group(bars, hours):
    out, cur, key = [], None, None
    for b in bars:
        k = b["t"] // (hours * 3600)
        if k != key:
            if cur:
                out.append(cur)
            cur, key = dict(b), k
        else:
            cur["h"], cur["l"], cur["c"] = max(cur["h"], b["h"]), min(cur["l"], b["l"]), b["c"]
    if cur:
        out.append(cur)
    return out


def shift(bars, basis):
    return [{**b, "o": b["o"] - basis, "h": b["h"] - basis, "l": b["l"] - basis, "c": b["c"] - basis} for b in bars]


def last_at_or_before(bars, ts):
    best = None
    for b in bars:
        if b["t"] <= ts:
            best = b
    return best


def daily_closes(y, before_day, tzname=None):
    """Dagslotkoersen van een Yahoo-dagreeks voor de handelsdag before_day (niet meegerekend)."""
    tz = ZoneInfo(tzname or (y["meta"].get("exchangeTimezoneName") or "America/New_York"))
    out = []
    for b in y["bars"]:
        d = datetime.fromtimestamp(b["t"], tz).date()
        if d < before_day:
            out.append({"d": d, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]})
    return out


# ---------------------------------------------------------------- markten
def gold_series():
    c = gold_contract()
    Y = yfetch_many({"h1": (c, "3mo", "1h"), "m15": (c, "10d", "15m"), "m5": (c, "5d", "5m"),
                     "gld": ("GLD", "1y", "1d", False), "gcf": ("GC=F", "5d", "5m")})
    if not Y["h1"] or not Y["h1"]["bars"]:
        raise RuntimeError(f"geen uurkaarsen voor {c}")
    fut_last = Y["h1"]["meta"].get("regularMarketPrice") or Y["h1"]["bars"][-1]["c"]
    spot = safe(lambda: L.spot_quote("XAU"), "spot goud")
    if spot:
        px, at, src = spot
        f = last_at_or_before((Y["m5"] or {}).get("bars") or [], at.timestamp() + 60)
        if f and abs(at.timestamp() - f["t"]) <= 20 * 60:
            basis, how = f["c"] - px, "op hetzelfde moment"
        else:
            basis, how = fut_last - px, "laatste koersen"
        if not -0.01 < basis / px < 0.04:
            log(f"[!] goud: verschil future-spot {basis:+.2f} is ongewoon ({how})")
        pxsrc = f"{src} spot"
        log(f"[ok] goud spot {px:.2f} via {src}; contract {c}, verschil {basis:+.2f} ({how})")
    else:
        m = Y["gcf"]["meta"] if Y["gcf"] else Y["h1"]["meta"]
        px = m.get("regularMarketPrice") or fut_last
        at = datetime.fromtimestamp(m.get("regularMarketTime") or NOW.timestamp(), UTC)
        basis, pxsrc = fut_last - px, "Yahoo (goud-future, geen spot gevonden)"
        MISSING.append("spotprijs goud (future gebruikt)")
    h1 = shift(Y["h1"]["bars"], basis)
    m15 = shift((Y["m15"] or {}).get("bars") or [], basis)
    days = day_candles(h1)
    today = days[-1]
    prev = days[-2]["c"] if len(days) >= 2 else None
    # lange reeks voor de 200-daagse: GLD omgerekend naar spot (weinig verloop over een jaar)
    bars = []
    if Y["gld"] and Y["gld"]["bars"]:
        gl = Y["gld"]["bars"]
        k = px / (Y["gld"]["meta"].get("regularMarketPrice") or gl[-1]["c"])
        cut = today["d"]
        for b in gl:
            d = datetime.fromtimestamp(b["t"], NY).date()
            if d < cut:
                bars.append({"d": d, "o": b["o"] * k, "h": b["h"] * k, "l": b["l"] * k, "c": b["c"] * k})
    if len(bars) < 30:
        bars = [{"d": x["d"], "o": x["o"], "h": x["h"], "l": x["l"], "c": x["c"]} for x in days[:-1]]
    if days[:-1]:  # de laatste dagen exact uit de uurkaarsen
        exact = {x["d"]: x for x in days[:-1]}
        bars = [exact.get(b["d"], b) for b in bars]
        for d in sorted(exact):
            if not bars or d > bars[-1]["d"]:
                bars.append(exact[d])
    return {"id": "XAUUSD", "px": px, "at": at, "prev": prev, "hi": max(today["h"], px), "lo": min(today["l"], px),
            "days": days, "h1": h1, "m15": m15, "bars": bars, "src": pxsrc, "pxSrc": pxsrc, "dp": 2,
            "note": f"zones uit het {c[:5]}-contract, omgerekend naar spot", "step": 50, "basis": basis}


INDEX = {"US500": ("^GSPC", "ES", "CME"), "US100": ("^NDX", "NQ", "CME"), "US30": ("^DJI", "YM", "CBT")}


def cash_open(now=NOW):
    t = now.astimezone(NY)
    return t.weekday() < 5 and (t.hour, t.minute) >= (9, 30) and t.hour < 16


def index_series(key, full=True):
    cash, root, exch = INDEX[key]
    c = index_contract(root, exch)
    reqs = {"cd": (cash, "1y", "1d", False), "c5": (cash, "5d", "5m", False), "f5": (c, "5d", "5m")}
    if full:
        reqs.update({"h1": (c, "3mo", "1h"), "m15": (c, "10d", "15m")})
    Y = yfetch_many(reqs)
    if not Y["cd"] or not Y["cd"]["bars"]:
        raise RuntimeError(f"geen dagkoersen voor {cash}")
    cm = Y["cd"]["meta"]
    c5 = (Y["c5"] or {}).get("bars") or []
    f5 = (Y["f5"] or {}).get("bars") or []
    basis = None
    if c5 and f5:
        last_cash = c5[-1]
        fb = last_at_or_before(f5, last_cash["t"] - 300) or last_at_or_before(f5, last_cash["t"])
        if fb and abs(fb["t"] - (last_cash["t"] - 300)) <= 900:
            basis = fb["c"] - last_cash["c"]
    live_cash = cash_open() and c5 and (NOW.timestamp() - c5[-1]["t"]) < 40 * 60
    if live_cash or basis is None or not f5:
        px = cm.get("regularMarketPrice") or Y["cd"]["bars"][-1]["c"]
        at = datetime.fromtimestamp(cm.get("regularMarketTime") or NOW.timestamp(), UTC)
        src = "Yahoo Finance (index)"
        cur_day = ny_day(at.timestamp()) if not live_cash else datetime.now(NY).date()
    else:
        px = f5[-1]["c"] - basis
        at = datetime.fromtimestamp(f5[-1]["t"] + 300, UTC)
        src = f"Yahoo Finance ({c[:2]}-future, omgerekend naar de index)"
        cur_day = ny_day(f5[-1]["t"])
    prior = daily_closes(Y["cd"], cur_day, "America/New_York")
    prev = prior[-1]["c"] if prior else None
    out = {"id": key, "px": px, "at": at, "prev": prev, "bars": prior, "src": src, "pxSrc": src, "dp": 2,
           "step": 100 if key != "US30" else 250, "basis": basis}
    if full:
        b = basis if basis is not None else 0.0
        h1 = shift((Y["h1"] or {}).get("bars") or [], b)
        m15 = shift((Y["m15"] or {}).get("bars") or [], b)
        days = day_candles(h1) if h1 else []
        if days and days[-1]["d"] == cur_day:
            out["hi"], out["lo"] = max(days[-1]["h"], px), min(days[-1]["l"], px)
        out.update({"days": days, "h1": h1, "m15": m15, "note": f"zones uit de {root}-future, omgerekend naar de index"})
    log(f"[ok] {key} {px:.2f} ({src}); vorige slot {prev}; verschil future-index {basis}")
    return out


def dxy_series():
    Y = yfetch_many({"d": ("DX-Y.NYB", "1y", "1d", False), "h1": ("DX-Y.NYB", "1mo", "1h")})
    h1 = (Y["h1"] or {}).get("bars") or []
    m = (Y["h1"] or Y["d"])["meta"]
    px = m.get("regularMarketPrice") or h1[-1]["c"]
    at = datetime.fromtimestamp(m.get("regularMarketTime") or NOW.timestamp(), UTC)
    days = day_candles(h1) if h1 else []
    prev = days[-2]["c"] if len(days) >= 2 else None
    cur = days[-1]["d"] if days else datetime.now(NY).date()
    bars = daily_closes(Y["d"], cur) if Y["d"] else []
    log(f"[ok] DXY {px:.3f}")
    return {"id": "DXY", "px": px, "at": at, "prev": prev, "days": days, "h1": h1, "m15": [], "bars": bars,
            "src": "Yahoo Finance (ICE)", "dp": 3, "step": 0.5}


def treasury_curve():
    """Nominale rente (2 en 10 jaar) per dag van het Amerikaanse ministerie van Financiën."""
    rows = []
    for yr in (NOW.year, NOW.year - 1):
        txt = L.fetch("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
                      f"{yr}/all?type=daily_treasury_yield_curve&field_tdr_date_value={yr}&page&_format=csv", timeout=30)
        for row in L.csv.DictReader(L.io.StringIO(txt.lstrip("﻿"))):
            try:
                mo, d, y = (row.get("Date") or "").split("/")
                rows.append((f"{int(y):04d}-{int(mo):02d}-{int(d):02d}", float(row["2 Yr"]), float(row["10 Yr"])))
            except (ValueError, KeyError):
                pass
        if len(rows) >= 10:
            break
    rows.sort()
    if len(rows) < 2:
        raise RuntimeError("te weinig rentedata")
    return rows


# ---------------------------------------------------------------- agenda
IMP2_US = re.compile(r"\b(PMI|ISM|Consumer Confidence|Durable Goods|Retail Sales|PPI|GDP|JOLTS|Unemployment Claims|"
                     r"FOMC Meeting Minutes|Unemployment Rate|Average Hourly Earnings|UoM|Fed Chair|Powell|Speaks)\b", re.I)
NONUS = {"EUR", "GBP", "JPY", "CNY"}


def calendar3():
    raw = []
    for u in L.FF_FEEDS:
        try:
            raw += json.loads(L.fetch(u))
        except Exception as e:
            if "thisweek" in u:
                MISSING.append("agenda (ForexFactory)")
                log("[!] agenda", e)
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
        c, impact, title = e.get("country", ""), e.get("impact", ""), e.get("title", "")
        if c == "USD":
            if impact not in ("High", "Medium", "Holiday"):
                continue
            t1 = bool(L.TIER1.search(title)) and not title.startswith("ADP")
            imp = 3 if t1 else 2 if (impact == "High" or IMP2_US.search(title)) else 1
            cty = "US"
        elif c in NONUS:
            if impact not in ("High", "Medium"):
                continue
            imp = 2 if impact == "High" else 1
            cty = L.CCY.get(c, c[:2])
            for word, cc in (("German", "DE"), ("French", "FR"), ("Italian", "IT"), ("Spanish", "ES")):
                if title.startswith(word):
                    cty = cc
        else:
            continue
        key = (title, c, t.isoformat())
        if key in seen:
            continue
        seen.add(key)
        ev.append({"t": iso(t), "cty": cty, "ev": L.nl_event(title), "en": title, "imp": imp,
                   "prev": L.nl_val(e.get("previous")) or None, "cons": L.nl_val(e.get("forecast")) or None, "act": None,
                   "hits": PRIMARY[:] if cty == "US" else ["XAUUSD"]})
    ev.sort(key=lambda x: (x["t"], -x["imp"]))
    if len(ev) > 16:
        keep = sorted(ev, key=lambda x: (-(x["imp"] * 2 + (1 if x["cty"] == "US" else 0)), x["t"]))[:16]
        ev = sorted(keep, key=lambda x: (x["t"], -x["imp"]))
    for i, x in enumerate(ev):
        x["id"] = f"e{i}"
    log(f"[ok] agenda: {len(ev)} events")
    return ev


# ---------------------------------------------------------------- Fed
def fed_block():
    page = L.fetch("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", timeout=30)
    meets = []
    for y in (NOW.year, NOW.year + 1):
        i = page.find(f"{y} FOMC Meetings")
        if i < 0:
            continue
        seg = page[i:i + 30000]
        j = seg.find("FOMC Meetings", 40)
        seg = seg[:j] if j > 0 else seg
        for mon, days in re.findall(r'fomc-meeting__month[^>]*><strong>([A-Za-z/]+)</strong></div>\s*'
                                    r'<div class="fomc-meeting__date[^>]*>([^<]+)</div>', seg):
            mname = mon.split("/")[-1][:3].lower()
            mi = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"].index(mname) + 1
            dd = re.findall(r"\d+", days)
            if dd:
                meets.append(date(y, mi, int(dd[-1])))
    today = datetime.now(NY).date()
    nxt = [d for d in sorted(meets) if d >= today]
    if not nxt:
        raise RuntimeError("geen volgende Fed-vergadering gevonden")
    meet = nxt[0]
    e = json.loads(L.fetch("https://markets.newyorkfed.org/api/rates/unsecured/effr/last/1.json"))["refRates"][0]
    effr, lo, hi = float(e["percentRate"]), float(e["targetRateFrom"]), float(e["targetRateTo"])

    def zq(y, m):
        q = ychart(f"ZQ{MCODE[m - 1]}{y % 100:02d}.CBT", "5d", "1d", prepost=False)
        return 100 - (q["meta"].get("regularMarketPrice") or q["bars"][-1]["c"])

    est = []
    n_days = (date(meet.year + meet.month // 12, meet.month % 12 + 1, 1) - date(meet.year, meet.month, 1)).days
    d_before = meet.day  # nieuwe rente geldt vanaf de dag na het besluit
    if n_days - d_before >= 7:
        avg = zq(meet.year, meet.month)
        est.append((avg - d_before / n_days * effr) / ((n_days - d_before) / n_days))
    ny_, nm_ = meet.year + meet.month // 12, meet.month % 12 + 1
    later = [d for d in nxt[1:] if (d.year, d.month) == (ny_, nm_)]
    if not later or later[0].day >= 24:
        est.append(zq(ny_, nm_))
    if not est:
        raise RuntimeError("geen fed-funds-future voor deze vergadering")
    post = sum(est) / len(est)
    move = post - effr
    p = clamp(abs(move) / 0.25, 0, 1)
    hike = jsround(100 * p) if move > 0 else 0
    cut = jsround(100 * p) if move < 0 else 0
    out = {"meeting": meet.isoformat(), "hike": hike, "hold": 100 - hike - cut, "cut": cut,
           "rate": f"{nl(lo, 2)}-{nl(hi, 2)}%", "src": "berekend uit fed-funds-futures (CME, via Yahoo) en de NY Fed",
           "asof": day_label(NOW), "effr": effr, "post": round(post, 3)}
    log(f"[ok] Fed {meet}: verhoging {hike}%, gelijk {out['hold']}%, verlaging {cut}% (EFFR {effr}, verwacht {post:.3f})")
    return out


# ---------------------------------------------------------------- stemming
def stocktwits():
    out = []
    for sym, fr in (("GLD", "XAUUSD"), ("SPY", "US500"), ("QQQ", "US100")):
        try:
            j = json.loads(L.fetch(f"https://api.stocktwits.com/api/2/symbols/{sym}/sentiment.json"))
            row = (j.get("data") or [])[0]
            v = jsround(float(row["bullish"]))
            lab = ("Extreem bearish" if v < 20 else "Bearish" if v < 45 else "Neutraal" if v <= 55
                   else "Bullish" if v <= 80 else "Extreem bullish")
            d = datetime.fromisoformat(row["timestamp"]) if row.get("timestamp") else NOW
            out.append({"id": sym, "for": fr, "v": v, "label": lab, "src": "Stocktwits (aandeel bullish berichten)",
                        "at": f"{d.day} {MON[d.month - 1]}"})
        except Exception as e:
            log(f"[!] stocktwits {sym}: {str(e)[:120]}")
    return out


# ---------------------------------------------------------------- techniek: zones, niveaus, trend
def atr(bars, n=14):
    if len(bars) < 2:
        return None
    tr = [max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])) for p, b in zip(bars, bars[1:])]
    tr = tr[-n:]
    return sum(tr) / len(tr) if tr else None


def find_zones(bars, tf, max_back):
    """Supply en demand zoals de trader ze tekent: een basiskaars met kleine body, gevolgd door een sterke
    vertrekbeweging (minstens 2x ATR binnen 3 kaarsen). Alleen verse zones: de koers kwam er daarna niet meer in."""
    if len(bars) < 20:
        return []
    a = atr(bars[:-1], 20)
    if not a:
        return []
    zones = []
    start = max(1, len(bars) - max_back)
    for i in range(start, len(bars) - 3):
        b = bars[i]
        rng = b["h"] - b["l"]
        if rng <= 0 or abs(b["c"] - b["o"]) > 0.6 * rng:
            continue
        nxt = bars[i + 1:i + 4]
        up = max(x["h"] for x in nxt) - b["h"]
        dn = b["l"] - min(x["l"] for x in nxt)
        later = bars[i + 4:]
        if up >= 2 * a and up > dn:
            lo, hi = b["l"], max(b["o"], b["c"])
            if hi - lo < 0.15 * a:
                hi = lo + 0.15 * a
            if all(x["l"] > hi for x in later):
                zones.append({"kind": "demand", "lo": lo, "hi": hi, "t": b["t"], "tf": tf, "force": up / a})
        elif dn >= 2 * a and dn > up:
            lo, hi = min(b["o"], b["c"]), b["h"]
            if hi - lo < 0.15 * a:
                lo = hi - 0.15 * a
            if all(x["h"] < lo for x in later):
                zones.append({"kind": "supply", "lo": lo, "hi": hi, "t": b["t"], "tf": tf, "force": dn / a})
    return zones


def when_short(ts):
    t = datetime.fromtimestamp(ts, BXL)
    return f"{DAG[t.weekday()]} {t.day} {MON[t.month - 1]} {t:%H:%M}"


def rnd(x, step):
    return round(x / step) * step


def build_levels(s):
    """2-5 niveaus: de dichtstbijzijnde verse zones boven en onder de koers, aangevuld met
    vorige-dag high/low, het slot van gisteren, de week-high/low en een rond getal."""
    px, dp = s["px"], s["dp"]
    unit = 1.0 if s["id"] != "DXY" else 0.01
    cands = []
    for tf, key, back in (("M15", "m15", 3 * 96), ("H1", "h1", 10 * 24)):
        for z in find_zones(s.get(key) or [], tf, back):
            lo, hi = rnd(z["lo"], unit), rnd(z["hi"], unit)
            if z["kind"] == "supply" and lo <= px:
                continue
            if z["kind"] == "demand" and hi >= px:
                continue
            word = "daling" if z["kind"] == "supply" else "stijging"
            cands.append({"lo": lo, "hi": hi, "kind": z["kind"], "pri": 0 if tf == "M15" else 1,
                          "note": f"verse {tf}-zone: sterke {word} {when_short(z['t'])}", "src": CALC})
    days = s.get("days") or []
    if len(days) >= 2:
        y = days[-2]
        dn = DAG[y["d"].weekday()]
        cands += [{"p": rnd(y["h"], unit), "note": f"high van {dn}", "pri": 2},
                  {"p": rnd(y["l"], unit), "note": f"low van {dn}", "pri": 2},
                  {"p": rnd(y["c"], unit), "note": f"slot van {dn}", "pri": 3}]
    if len(days) >= 7:
        wk = [x for x in days[:-1] if x["d"].isocalendar()[1] == days[-2]["d"].isocalendar()[1]]
        if wk and len(wk) >= 2 and days[-1]["d"].isocalendar()[1] != days[-2]["d"].isocalendar()[1]:
            cands += [{"p": rnd(max(x["h"] for x in wk), unit), "note": "high van vorige week", "pri": 2},
                      {"p": rnd(min(x["l"] for x in wk), unit), "note": "low van vorige week", "pri": 2}]
    step = s.get("step") or 50
    base = math.floor(px / step) * step
    cands += [{"p": base + step, "note": "rond getal", "pri": 4}, {"p": base, "note": "rond getal", "pri": 4}]
    out = []
    for cnd in sorted(cands, key=lambda x: (x["pri"], abs(((x.get("lo", x.get("p")) + x.get("hi", x.get("p"))) / 2) - px))):
        mid = (cnd.get("lo", cnd.get("p")) + cnd.get("hi", cnd.get("p"))) / 2
        if abs(mid / px - 1) > (0.05 if s["id"] != "DXY" else 0.02) or abs(mid / px - 1) < 0.0008:
            continue
        if "kind" not in cnd:
            cnd["kind"] = "supply" if mid > px else "demand"
            if cnd["note"] == "rond getal":
                cnd["kind"] = "pivot"
        lo_c, hi_c = cnd.get("lo", cnd.get("p")), cnd.get("hi", cnd.get("p"))
        if any(not (hi_c < o.get("lo", o.get("p")) - 0.0015 * px or lo_c > o.get("hi", o.get("p")) + 0.0015 * px) for o in out):
            continue
        out.append(cnd)
    above = [x for x in out if (x.get("lo", x.get("p")) > px)]
    below = [x for x in out if (x.get("hi", x.get("p")) < px)]
    above.sort(key=lambda x: x.get("lo", x.get("p")))
    below.sort(key=lambda x: -x.get("hi", x.get("p")))
    pick = above[:2] + below[:2]
    rest = [x for x in out if x not in pick]
    pick += rest[:max(0, 4 - len(pick))]
    pick.sort(key=lambda x: -(x.get("hi", x.get("p"))))
    res = []
    for x in pick[:5]:
        item = {"kind": x["kind"], "note": x["note"][:50], "src": x.get("src") or CALC}
        if "lo" in x and x["hi"] - x["lo"] >= unit:
            item.update({"lo": r(x["lo"], dp), "hi": r(x["hi"], dp)})
        else:
            item["p"] = r(x.get("p", x.get("lo")), dp)
        res.append(item)
    return res


def trend_of(candles, n1=20, n2=50):
    cl = [c["c"] for c in candles]
    if len(cl) < max(n2, 12):
        return None
    s1, s2 = L.sma(cl, n1), L.sma(cl, n2)
    s1_old = L.sma(cl[:-5], n1)
    hh = max(c["h"] for c in candles[-10:]) > max(c["h"] for c in candles[-20:-10])
    ll = min(c["l"] for c in candles[-10:]) < min(c["l"] for c in candles[-20:-10])
    last = cl[-1]
    up = last > s1 > s2 and s1 > s1_old and not ll
    down = last < s1 < s2 and s1 < s1_old and not hh
    d = 1 if up else -1 if down else 0
    facts = (f"slot {last:.2f}, gemiddelde {n1} {s1:.2f}, gemiddelde {n2} {s2:.2f}, "
             f"{'hogere toppen' if hh else 'geen hogere toppen'}, {'lagere bodems' if ll else 'geen lagere bodems'}")
    return {"d": d, "facts": facts}


def htf_facts(s):
    out = {}
    days = [x for x in (s.get("bars") or [])]
    if len(days) >= 50:
        out["d1"] = trend_of([{"c": x["c"], "h": x["h"], "l": x["l"]} for x in days])
    h4 = group(s.get("h1") or [], 4)
    if len(h4) >= 60:
        out["h4"] = trend_of(h4[:-1] if len(h4) > 60 else h4)
    return out


# ---------------------------------------------------------------- AI
LANG = """LANGUAGE: VERY SIMPLE DUTCH ("JIP-EN-JANNEKE-TAAL"). Every reader-facing text is Dutch that a beginner understands at first reading. Plain text, no markdown.
- Short sentences (about 15 words at most), everyday words, active voice. Rather two short sentences than one long one.
- No jargon without explanation. These trader words are fine: bullish, bearish, long, short, supply, demand, zone, fresh, sweep, liquidity grab, engulfing, MSB, killzone, stop, doel, DXY, Fed, CPI, H1, H4, M5, M15. Replace other jargon with plain words: hawkish = "de Fed wil de rente hoog houden of verhogen"; dovish = "de Fed wil de rente verlagen"; yields = "rente"; basispunten = write it as a percentage ("0,25 procentpunt"); risk-on = "beleggers durven risico aan"; risk-off = "beleggers zijn bang"; safe haven = "veilige haven"; consensus = "verwacht"; priced in = "de markt rekent er al op". No abbreviations such as bp, m/m or j/j in running text: write "per maand" and "per jaar" (event names may keep them).
- Always give cause, effect and meaning: "X gebeurt. Daardoor Y. Dus goud Z."
- The gold link g: {"d":-1, 0 or 1,"t":"text"}. d says what the item does to gold right now: 1 = pushes gold up, -1 = pushes gold down, 0 = no clear push or not known yet. t explains it in one or two short sentences that end with gold. For a number that still has to come out, t gives both sides.
- Killzones: name them (Londen-killzone, NY-killzone) without clock times. All other times in text are Brussels time.
- Numbers in text use Dutch notation (4.125 and 0,3%). Never put the numeric score inside a text.
- Use only the numbers, levels and facts in the data. Never invent a price, level, number, quote, post or source."""

SYSTEM = ("You write the KAIROS macro desk for a supply-and-demand day trader in Brussels. Gold (XAUUSD) is the main market; "
          "US500 and US100 are traded next to it, mostly in the New York session. The trader's method: first the higher-timeframe "
          "context (H1 and H4), then a fresh 15-minute demand or supply zone of the day itself with a strong departure and in line "
          "with the higher timeframe, then the entry on the 5-minute chart (a liquidity grab, an engulfing candle or a "
          "market-structure break, MSB), with the target on the next zone on the other side. The trader is still learning and "
          "wants to master gold day trading, so the desk is gold-first. Answer with one JSON object and nothing else.\n\n" + LANG)


def claude_research(user, max_uses):
    """Zoekt op internet met Claude (web search). Alleen met ANTHROPIC_API_KEY."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key or max_uses <= 0:
        return None
    model = os.environ.get("KAIROS_CLAUDE_MODEL") or "claude-haiku-5-5"
    last = None
    for tool in ({"type": "web_search_20250305", "name": "web_search", "max_uses": max_uses},
                 {"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses, "allowed_callers": ["direct"]}):
        payload = {"model": model, "max_tokens": 12000, "tools": [tool],
                   "system": "You are a careful research assistant. Search, then answer with one JSON object and nothing else.",
                   "messages": [{"role": "user", "content": user}]}
        try:
            r = L.api_json("https://api.anthropic.com/v1/messages",
                           headers={"x-api-key": key, "anthropic-version": "2023-06-01"}, payload=payload, tries=2, timeout=240)
        except Exception as e:
            last = e
            if "HTTP 400" in str(e):
                continue
            raise
        u = r.get("usage") or {}
        n = ((u.get("server_tool_use") or {}).get("web_search_requests")) or 0
        tin, tout = u.get("input_tokens") or 0, u.get("output_tokens") or 0
        cost = n * 0.01 + tin * 0.10 / 1e6 + tout * 0.50 / 1e6
        log(f"[i] Claude zoekt: {n} zoekopdrachten, {tin} tokens in, {tout} uit (ongeveer ${cost:.4f})")
        COST[0] += cost
        txt = "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")
        return L.parse_json(txt)
    raise RuntimeError(f"web search niet beschikbaar: {last}")


COST = [0.0]


def ai(user, max_tokens):
    out = L.ai_json(SYSTEM, user, max_tokens)
    return out


# ---------------------------------------------------------------- hulpjes voor de tekst
def nlnum(x, dp=2):
    return nl(x, dp) if x is not None else "-"


def lvl_txt(lv, dp):
    if "lo" in lv:
        return f"{lv['kind']} {nlnum(lv['lo'], dp)}-{nlnum(lv['hi'], dp)} ({lv['note']})"
    return f"{lv['kind']} {nlnum(lv['p'], dp)} ({lv['note']})"


def g_ok(g, default_t=""):
    if not isinstance(g, dict):
        return {"d": 0, "t": default_t} if default_t else None
    d = g.get("d")
    d = int(clamp(jsround(d), -1, 1)) if isinstance(d, (int, float)) else 0
    t = str(g.get("t") or default_t)[:160]
    return {"d": d, "t": t} if t else None


def cut(x, n):
    """Tekst inkorten tot n tekens, bij voorkeur op het einde van een zin, anders op een woordgrens."""
    if not isinstance(x, str) or not x.strip():
        return None
    t = re.sub(r"\s+", " ", x).strip()
    if len(t) <= n:
        return t
    part = t[:n]
    ends = [m.end() for m in re.finditer(r"[.!?](?=\s|$)", part)]
    if ends and ends[-1] >= n * 0.5:
        return part[:ends[-1]].strip()
    sp = part.rfind(" ")
    return (part[:sp] if sp > n * 0.5 else part[:n - 1]).rstrip(" ,;:-") + "…"


LABEL_RE = re.compile(r"^\s*(licht\s+bullish|licht\s+bearish|bullish|bearish|neutraal)\b[\s:,;.\-–]*", re.I)
LEAD_RE = re.compile(r"^\s*(xauusd|xau/usd|goud|us500|us100|s&p\s*500|nasdaq(\s*100)?)\b[\s:,;.\-–]*", re.I)


def fix_call(call, lab):
    rest = str(call or "").strip()
    for _ in range(2):
        rest = LEAD_RE.sub("", rest, count=1)
        rest = LABEL_RE.sub("", rest, count=1)
    rest = rest[:1].upper() + rest[1:] if rest else ""
    return f"{lab}. {rest}" if rest else f"{lab}."


def valid_plans(plans, a, levels):
    """Een plan is alleen geldig met een zone uit de levels, aan de goede kant van de koers, en doelen aan de overkant."""
    px = a["px"]
    edges = []
    for lv in levels:
        lo, hi = lv.get("lo", lv.get("p")), lv.get("hi", lv.get("p"))
        edges.append((lo, hi))
    out = []
    for p in plans if isinstance(plans, list) else []:
        try:
            side = 1 if int(p.get("side")) > 0 else -1
            z = p.get("zone") or {}
            lo, hi = float(z.get("lo")), float(z.get("hi"))
        except (TypeError, ValueError):
            continue
        if lo > hi:
            lo, hi = hi, lo
        if not any(abs(lo - e[0]) <= 0.002 * px and abs(hi - e[1]) <= 0.002 * px for e in edges):
            continue
        if side < 0 and lo <= px * 0.9995 and hi <= px:
            continue
        if side > 0 and hi >= px * 1.0005 and lo >= px:
            continue
        tps = []
        for t in p.get("tp") or []:
            try:
                t = float(t)
            except (TypeError, ValueError):
                continue
            if (side < 0 and t < lo) or (side > 0 and t > hi):
                tps.append(r(t, a["dp"]))
        t_txt, x_txt = cut(p.get("t"), 240), cut(p.get("x"), 110)
        if not tps or not t_txt:
            continue
        out.append({"side": side, "zone": {"lo": r(lo, a["dp"]), "hi": r(hi, a["dp"])}, "t": t_txt, "tp": tps[:2],
                    "x": x_txt or "Uit als de zone op H1 duidelijk breekt."})
    return out[:2]


def code_plans(a, levels):
    """Reserveplannen als de AI geen geldig plan gaf: de dichtstbijzijnde zone boven (short) en onder (long)."""
    px, dp = a["px"], a["dp"]
    above = sorted([lv for lv in levels if lv.get("lo", lv.get("p")) > px], key=lambda lv: lv.get("lo", lv.get("p")))
    below = sorted([lv for lv in levels if lv.get("hi", lv.get("p")) < px], key=lambda lv: -lv.get("hi", lv.get("p")))

    def z(lv):
        return {"lo": lv.get("lo", lv.get("p")), "hi": lv.get("hi", lv.get("p"))}
    plans = []
    if above and below:
        short = {"side": -1, "zone": z(above[0]), "tp": [below[0].get("hi", below[0].get("p"))] + ([below[1].get("hi", below[1].get("p"))] if len(below) > 1 else []),
                 "t": f"Wacht tot de koers terugloopt naar {nl(z(above[0])['lo'], dp)}-{nl(z(above[0])['hi'], dp)}. Zoek daar een verse M15 supply-zone. Op M5 wil je een sweep boven een oude top en dan een engulfing of een MSB omlaag. Stop boven de sweep.",
                 "x": f"Uit als de koers op H1 boven {nl(z(above[0])['hi'], dp)} sluit en daar blijft."}
        long = {"side": 1, "zone": z(below[0]), "tp": [above[0].get("lo", above[0].get("p"))] + ([above[1].get("lo", above[1].get("p"))] if len(above) > 1 else []),
                "t": f"Wacht op een sweep onder {nl(z(below[0])['lo'], dp)}: de koers prikt eronder en komt terug. Dan op M5 een engulfing of een MSB omhoog. Stop onder de sweep.",
                "x": f"Uit als de koers op H1 onder {nl(z(below[0])['lo'], dp)} sluit en daar blijft."}
        plans = [short, long] if a["score"] <= 0 else [long, short]
        if a["score"] != 0 and abs(a["score"]) >= 15:
            plans[1]["t"] = "Tegen de trend, dus klein of overslaan. " + plans[1]["t"]
    return plans


# ---------------------------------------------------------------- run
def session_of(now_bxl):
    if now_bxl.weekday() >= 5:
        return "close"
    h = now_bxl.hour
    return "london" if 4 <= h < 12 else "ny" if 12 <= h < 20 else "close"


def main():
    nowb = datetime.now(BXL)
    session = session_of(nowb)
    run_id = nowb.strftime("%Y%m%d-%H%M")
    log(f"KAIROS desk-run v3 {run_id} ({session})")
    desk_path, runs_path = os.path.join(DATA_DIR, "desk.json"), os.path.join(DATA_DIR, "runs.json")
    prev = L.load_json(desk_path, {})
    runs = L.load_json(runs_path, [])

    # ---- gegevens ophalen (tegelijk waar het kan)
    with ThreadPoolExecutor(3) as ex:
        f_gold = ex.submit(lambda: safe(gold_series, "goud"))
        f_spx = ex.submit(lambda: safe(lambda: index_series("US500"), "US500"))
        f_ndx = ex.submit(lambda: safe(lambda: index_series("US100"), "US100"))
        f_dxy = ex.submit(lambda: safe(dxy_series, "DXY"))
        f_us30 = ex.submit(lambda: safe(lambda: index_series("US30", full=False), "US30"))
        f_ctx = ex.submit(lambda: yfetch_ctx())
    S = {"XAUUSD": f_gold.result(), "US500": f_spx.result(), "US100": f_ndx.result()}
    dxy, us30, ctxq = f_dxy.result(), f_us30.result(), f_ctx.result()
    if sum(1 for k in PRIMARY if S[k]) < 2:
        log("[!] te weinig koersen; de vorige desk blijft staan")
        sys.exit(1)
    curve = safe(treasury_curve, "rentecurve")
    real = None
    for name, getter in (("US Treasury", L.treasury_real10), ("FRED DFII10", lambda: L.fred("DFII10"))):
        real = safe(getter, f"reële rente via {name}")
        if real and len(real) >= 6:
            L.REAL_SRC[0] = name
            break
    cal = safe(calendar3, "agenda", [])
    heads = safe(L.news, "nieuws", [])
    fg = L.fear_greed()
    meters = safe(stocktwits, "stocktwits", [])
    fed = safe(fed_block, "Fed")

    ep, ep_why = L.event_penalty(cal)

    # ---- context-koersen
    y10 = ctxq.get("US10Y")
    context_raw = []
    for cid, nm in (("US10Y", "US 10j rente"), ("US02Y", "US 2j rente"), ("VIX", "Volatiliteit"), ("WTI", "Olie (WTI)"),
                    ("BRENT", "Olie (Brent)"), ("XAGUSD", "Zilver"), ("COPPER", "Koper"), ("US30", "Dow Jones"), ("BTCUSD", "Bitcoin")):
        q = None
        if cid == "US02Y" and curve:
            q = {"px": curve[-1][1], "prev": curve[-2][1], "chgPct": pct(curve[-1][1], curve[-2][1]), "dp": 2, "at": curve[-1][0]}
        elif cid == "US30" and us30:
            q = {"px": us30["px"], "chgPct": pct(us30["px"], us30["prev"]), "dp": 2}
        elif cid == "XAGUSD":
            q = ctxq.get("XAGUSD")
            sp = safe(lambda: L.spot_quote("XAG"), "spot zilver")
            if q and sp:
                q = {**q, "px": sp[0]}
        else:
            q = ctxq.get(cid)
        if q and q.get("px") is not None:
            context_raw.append({"id": cid, "name": nm, "px": r(q["px"], 3 if cid in ("US10Y", "US02Y", "BRENT") else 2),
                                "chgPct": r(q.get("chgPct"), 2), "q": q})
    fx = []
    for pid in ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF"):
        q = ctxq.get(pid)
        if q:
            fx.append({"id": pid, "px": r(q["px"], 3 if pid == "USDJPY" else 5), "chgPct": r(q.get("chgPct"), 2)})

    # ---- factormodel (code-deel)
    vix, wti = ctxq.get("VIX"), ctxq.get("WTI")
    y10s = None
    if y10:
        y10s = {"px": y10["px"], "prev": y10["prev"], "bars": y10["bars"], "src": "Yahoo Finance"}
    code_f = {a: {} for a in PRIMARY}
    for aid in PRIMARY:
        s = S[aid]
        if not s:
            continue
        t = L.f_tech(s, 2)
        if t:
            code_f[aid]["tech"] = t
        rr = L.f_rates(y10s, real, aid == "XAUUSD")
        if rr:
            code_f[aid]["yields"] = rr
        if aid == "XAUUSD":
            u = L.f_usd(dxy) if dxy and len(dxy.get("bars") or []) >= 5 else None
            if u:
                code_f[aid]["usd"] = u
        else:
            rk = L.f_risk_idx(vix, wti)
            if rk:
                code_f[aid]["risk"] = rk
            st = L.f_sent_idx(fg)
            if st:
                code_f[aid]["sent"] = st

    # ---- niveaus en trend
    LV, HT = {}, {}
    for aid in PRIMARY:
        if S[aid]:
            LV[aid] = safe(lambda: build_levels(S[aid]), f"niveaus {aid}", [])
            HT[aid] = safe(lambda: htf_facts(S[aid]), f"trend {aid}", {})
    dxy_levels = safe(lambda: build_levels(dxy), "niveaus DXY", []) if dxy else []

    # ---- 1. zoeken op internet (alleen met Claude)
    released = [x for x in cal if x["cty"] == "US" and x["imp"] >= 2 and x["t"] <= iso(NOW)]
    research = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        ask = [f"Today is {NOW:%A %d %B %Y}, {NOW:%H:%M} UTC.",
               "1. Find 3 to 8 posts on X (Twitter) or Truth Social from the last 48 hours (72 hours in the weekend) that matter for gold, "
               "the Fed or the US stock market. Accounts that matter: The Kobeissi Letter, Nick Timiraos (WSJ Fed reporter), Walter Bloomberg "
               "(@DeItaone), First Squawk, FinancialJuice, Peter Schiff, World Gold Council, Mohamed El-Erian, Fed officials, and the US "
               "president on Truth Social. Search with the account or topic, the word X or tweet, and today's date in words "
               "(for example: Kobeissi Letter X post gold November 23 2026; site:x.com DeItaone Fed). Keep only the post itself "
               "(x.com, twitter.com or truthsocial.com) or a news page that quotes the post with its date. Keep only posts whose date you "
               "can see. Never invent a post, handle, number of views or link. who is only the name of the account.",
               ]
        if not fed:
            ask.append("2. The current CME FedWatch probabilities for the next FOMC meeting (hike, hold, cut in %), "
                       "with the meeting date, the publisher and the date of the reading.")
        if released:
            ask.append("3. The actual outcomes of these US releases from today, if they are out: " +
                       "; ".join(f"{x['id']} {x['en']} at {x['t']}" for x in released[:5]) + ".")
        ask.append('Answer with JSON only: {"posts":[{"t":"UTC time YYYY-MM-DDTHH:MM:SSZ (12:00:00Z when you only know the date)",'
                   '"day":true when only the date is known,"who":"name","handle":"@name or empty","net":"X|Truth Social",'
                   '"kind":"nieuws|data|mening|politiek","trust":3 official or wire, 2 journalist or analyst, 1 opinion account,'
                   '"reach":"views when shown, else empty","url":"link to the post or to the page that quotes it",'
                   '"text":"what the post says, max 300 chars, in English"}],'
                   '"fedwatch":{"hike":0,"hold":0,"cut":0,"src":"publisher","asof":"date"} or null,'
                   '"actuals":[{"id":"e3","act":"value as shown, e.g. 52,4 or 3,1%"}],"missing":"what you could not find"}')
        research = safe(lambda: claude_research("\n".join(ask), SEARCHES), "zoeken op internet")
    posts = []
    if isinstance(research, dict):
        cutoff = NOW - timedelta(hours=80)
        for p in research.get("posts") or []:
            try:
                t = datetime.fromisoformat(str(p.get("t")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if t < cutoff or t > NOW + timedelta(hours=1) or not p.get("text") or not p.get("who"):
                continue
            url = str(p.get("url") or "")
            if url and not url.startswith("http"):
                url = ""
            who = re.sub(r"\s*[\(\[].*$", "", str(p["who"])).strip()
            if not who:
                continue
            posts.append({"t": iso(t), "day": bool(p.get("day")), "who": who[:40], "handle": str(p.get("handle") or "")[:30],
                          "net": p.get("net") if p.get("net") in ("X", "Truth Social", "Stocktwits") else "X",
                          "kind": p.get("kind") if p.get("kind") in ("nieuws", "data", "mening", "politiek") else "mening",
                          "trust": int(clamp(p.get("trust") or 1, 1, 3)), "reach": str(p.get("reach") or "")[:30], "url": url,
                          "text": str(p["text"])[:300]})
        posts.sort(key=lambda x: x["t"], reverse=True)
        posts = posts[:8]
        fw = research.get("fedwatch")
        if not fed and isinstance(fw, dict) and fw.get("meeting") and all(isinstance(fw.get(k), (int, float)) for k in ("hike", "hold", "cut")):
            tot = fw["hike"] + fw["hold"] + fw["cut"]
            if 95 <= tot <= 105:
                fed = {"meeting": str(fw["meeting"])[:10], "hike": jsround(fw["hike"]), "hold": jsround(fw["hold"]), "cut": jsround(fw["cut"]),
                       "rate": "", "src": f"CME FedWatch via {str(fw.get('src') or 'nieuws')[:30]}", "asof": str(fw.get("asof") or day_label(NOW))[:12]}
        acts = {str(a.get("id")): str(a.get("act"))[:16] for a in (research.get("actuals") or []) if isinstance(a, dict) and a.get("act")}
        for x in cal:
            if x["id"] in acts and x["t"] <= iso(NOW):
                x["act"] = acts[x["id"]]
        if len(posts) < 3:
            MISSING.append(f"Minder dan 3 berichten van X gevonden ({len(posts)})")
    elif os.environ.get("ANTHROPIC_API_KEY"):
        MISSING.append("Zoeken op internet lukte niet: geen posts van X en geen FedWatch-kans uit het nieuws")
    else:
        MISSING.append("Geen Claude-sleutel: geen posts van X en geen FedWatch-kans uit het nieuws")

    # ---- gegevenspakket voor de AI
    prev_assets = {a.get("id"): a for a in prev.get("assets", [])} if prev.get("v") == 3 else {}
    P = [f"NOW: {DAG[nowb.weekday()]} {nowb:%d-%m-%Y %H:%M} Brussels. Session of this run: {session} "
         f"({'evening wrap; calls are for the next session' if session == 'close' else 'before London' if session == 'london' else 'before/at the New York open'}).",
         "", "PRICES (last | today % | 5 days % | 20 days %):"]
    for aid in PRIMARY:
        s = S[aid]
        if s:
            P.append(f"- {aid} ({NAMES[aid]}): {nl(s['px'], 2)} | {sgn(pct(s['px'], s['prev']), 2, '%')} | {sgn(L.ret_n(s, 5), 1, '%')} | "
                     f"{sgn(L.ret_n(s, 20), 1, '%')} | today high {nlnum(s.get('hi'))} low {nlnum(s.get('lo'))} | source {s['src']}")
    if dxy:
        P.append(f"- DXY: {nl(dxy['px'], 3)} | {sgn(pct(dxy['px'], dxy['prev']), 2, '%')} | {sgn(L.ret_n(dxy, 5), 2, '%')} | {sgn(L.ret_n(dxy, 20), 2, '%')}")
    for c in context_raw:
        if c["id"] in ("US10Y", "US02Y") and c["q"].get("prev") is not None:
            pp = c["px"] - c["q"]["prev"]
            P.append(f"- {c['id']} ({c['name']}): {nl(c['px'], 3)}% | {sgn(pp, 2)} procentpunt vandaag (write yield changes in procentpunt, never in %)")
        else:
            P.append(f"- {c['id']} ({c['name']}): {nl(c['px'], 2)} | {sgn(c['chgPct'], 2, '%')}")
    for f_ in fx:
        P.append(f"- {f_['id']}: {f_['px']} | {sgn(f_['chgPct'], 2, '%')}")
    if real:
        P.append(f"- Real 10-year yield (TIPS): {nl(real[-1][1], 2)}% on {real[-1][0]}, 5 days ago {nl(real[-6][1], 2)}%")
    if curve:
        P.append(f"- Treasury yields {curve[-1][0]}: 2-year {nl(curve[-1][1], 2)}%, 10-year {nl(curve[-1][2], 2)}% (day before: {nl(curve[-2][1], 2)}% and {nl(curve[-2][2], 2)}%)")
    if fed:
        P.append(f"- Fed: next meeting {fed['meeting']}, policy rate {fed['rate']}, market odds hike {fed['hike']}% / hold {fed['hold']}% / cut {fed['cut']}% ({fed['src']}, {fed['asof']})")
    if fg:
        P.append(f"- CNN Fear & Greed: {fg['v']} ({fg['label']}), previous {fg.get('prev')}")
    for m in meters:
        P.append(f"- Stocktwits {m['id']} (for {m['for']}): {m['v']}% of tagged posts bullish ({m['label']}, {m['at']})")
    P += ["", "COMPUTED FACTORS (fixed by code; explain them, do not change d):"]
    for aid in PRIMARY:
        for k, v in code_f[aid].items():
            P.append(f"- {aid} {k}: d={v[0]:+d}. {v[1]}")
    P += ["", "LEVELS (computed from price data; use only these numbers for zones, plans and targets):"]
    for aid in PRIMARY:
        if S[aid]:
            P.append(f"- {aid} (price {nl(S[aid]['px'], 2)}): " + "; ".join(lvl_txt(lv, 2) for lv in LV.get(aid, [])))
    if dxy:
        P.append(f"- DXY (price {nl(dxy['px'], 3)}): " + "; ".join(lvl_txt(lv, 3) for lv in dxy_levels))
    P += ["", "HIGHER TIMEFRAMES (computed; d 1 up, -1 down, 0 sideways):"]
    for aid in PRIMARY:
        for tf in ("d1", "h4"):
            h = HT.get(aid, {}).get(tf)
            if h:
                P.append(f"- {aid} {tf}: d={h['d']:+d} ({h['facts']})")
    P += ["", "CALENDAR (Brussels time; imp 3 tier-1, 2 tier-2, 1 minor):"]
    for x in cal:
        tb = datetime.fromisoformat(x["t"].replace("Z", "+00:00")).astimezone(BXL)
        P.append(f"[{x['id']}] {DAG[tb.weekday()]} {tb:%d-%m %H:%M} {x['cty']} {x['en']} (imp {x['imp']}, previous {x['prev'] or '-'}, "
                 f"expected {x['cons'] or '-'}, actual {x.get('act') or '-'})")
    P += ["", "HEADLINES (newest first):"]
    for i, n in enumerate(heads):
        P.append(f"[{i}] {n['t'].strftime('%d-%m %H:%MZ') if n['t'] else '?'} | {n['src'] or '?'} | {n['h']}")
    if posts:
        P += ["", "POSTS FROM X AND TRUTH SOCIAL (found by web search):"]
        for i, p in enumerate(posts):
            P.append(f"[p{i}] {p['t']} {p['who']} {p['handle']} ({p['kind']}, trust {p['trust']}): {p['text']}")
    pack = "\n".join(P)

    # ---- 2. oordeel: AI-factoren, richting van de dollar en de rest, nieuwskeuze
    ai_keys = {aid: [k for k, _, _, kind in L.MODEL[aid] if kind == "ai"] for aid in PRIMARY}
    shape = {"factors": {aid: {k: {"d": 0, "fresh": True, "evidence": "max 200 chars, English is fine", "src": "publisher, DD mon"}
                               for k in ai_keys[aid]} for aid in PRIMARY},
             "dxyLean": 0, "leans": {k: 0 for k in ("US10Y", "US02Y", "VIX", "WTI", "BRENT", "XAGUSD", "COPPER", "US30", "BTCUSD")},
             "news": [{"i": 0, "tags": [{"id": "XAUUSD", "d": 1}]}]}
    p1 = "\n".join([pack, "", "TASK 1: judge the qualitative factors with a direction d in {-2,-1,0,1,2} (+ = bullish for that market).",
                    "Guidance: " + "; ".join(L.AI_GUIDE[k] for k in ("fed", "risk", "flows", "sent", "earn", "macro")) + ".",
                    "Posts count as evidence for the factor they are about, never as a factor of their own; an opinion never outweighs a hard number. "
                    "Use +/-2 only for strong, clear evidence. fresh=false only when nothing in the data says anything about that factor.",
                    "dxyLean: your view on the dollar index for the coming session (-2..2, + = dollar up). leans: same for the other markets.",
                    "news: choose the 5 to 8 most market-moving headlines (by index), tag each with the markets it moves (d +1, -1 or 0).",
                    "JSON shape: " + json.dumps(shape)])
    log(f"[i] opdracht 1: {len(p1)} tekens")
    j1 = safe(lambda: ai(p1, 3500), "AI oordeel")
    if j1:
        log(f"[ok] AI oordeel via {ENGINE['ai']}")
    else:
        NOTES.append("AI-analyse niet beschikbaar, alleen rekenregels")

    prev_f = {a.get("id"): {f.get("k"): f for f in a.get("factors", [])} for a in prev.get("assets", [])}
    assets = []
    for aid in PRIMARY:
        s = S[aid]
        factors = []
        for key, lab, w, kind in L.MODEL[aid]:
            d, fresh, txt, src = 0, False, "Geen verse gegevens in deze run.", ""
            old = prev_f.get(aid, {}).get(key)
            if old and isinstance(old.get("d"), (int, float)):
                d, txt, src = int(old["d"]), old.get("text") or txt, old.get("src") or ""
            ev = ""
            if kind == "code" and code_f[aid].get(key):
                d, ev, src = code_f[aid][key]
                fresh = True
            elif kind == "ai" and j1:
                v = ((j1.get("factors") or {}).get(aid) or {}).get(key) or {}
                if isinstance(v.get("d"), (int, float)):
                    d = int(clamp(jsround(v["d"]), -2, 2))
                    fresh = v.get("fresh") is not False
                    ev = str(v.get("evidence") or "")[:240]
                    src = str(v.get("src") or "nieuwskoppen")[:80]
            factors.append({"k": key, "label": lab, "w": w, "d": d, "fresh": fresh, "text": txt, "src": src, "_ev": ev})
        score, conf, _, _ = L.evaluate(factors, ep)
        a = {"id": aid, "name": NAMES[aid], "px": r(s["px"], 2) if s else None,
             "chg": r(s["px"] - s["prev"], 2) if s and s.get("prev") else None,
             "chgPct": r(pct(s["px"], s["prev"]), 2) if s and s.get("prev") else None,
             "pxSrc": (s["pxSrc"] if s else None), "pxAt": iso(s["at"]) if s else iso(NOW),
             "factors": factors, "ep": ep, "epWhy": ep_why or "Geen groot nieuws binnen 3 dagen", "score": score, "conf": conf,
             "levels": LV.get(aid, []), "dp": 2}
        assets.append(a)
        log(f"    {aid}: {L.label(score)} {score:+d}, zekerheid {conf}%")

    # ---- 3. de desk-tekst
    lines = ["", "FINAL DESK (computed; copy labels exactly, never contradict them):"]
    for a in assets:
        cp = a["chgPct"]
        need = cp is not None and abs(a["score"]) >= 15 and abs(cp) >= 0.25 and (cp > 0) != (a["score"] > 0)
        lines.append(f"- {a['id']}: label {L.label(a['score'])}, confidence {a['conf']}%, event penalty {a['ep']} ({a['epWhy']}), "
                     f"price {nlnum(a['px'])}, today {sgn(a['chgPct'], 2, '%')}. moveNote: "
                     f"{'REQUIRED: today the price moves against the desk' if need else 'null'}.")
        for f_ in a["factors"]:
            way = "pushes up" if f_["d"] > 0 else "pushes down" if f_["d"] < 0 else "neutral"
            lines.append(f"    factor {f_['k']} ({f_['label']}, weight {f_['w']}): d={f_['d']:+d} {way}; "
                         f"{'evidence: ' + f_['_ev'] if f_['_ev'] else 'no fresh evidence this run' if not f_['fresh'] else ''} [src {f_['src']}]")
        for lv in a["levels"]:
            pos = "above" if (lv.get("lo", lv.get("p")) > (a["px"] or 0)) else "below"
            lines.append(f"    level {lvl_txt(lv, 2)}: {pos} the price")
    prev_learn = ((prev.get("learn") or {}).get("h")) if prev.get("v") == 3 else None
    prev_mood = ((prev.get("brief") or {}).get("mood") or {}).get("to") if prev.get("v") == 3 else None
    lines += ["", f"Previous desk: run {prev.get('runId')}, lesson '{prev_learn}', mood {prev_mood}, headline '{prev.get('headline')}'.",
              "", "TASK 2: write the desk texts in very simple Dutch. Rules:",
              "- The call, thesis, plans, playbook and brief must match the computed label. Start each call with the label.",
              "- factors: one text per factor (max 220 chars): what happened and what it does to this market, using the evidence or the computed facts.",
              "- plans: 1 or 2 per market in the trader's method. side -1 = short from a supply zone above the price, 1 = long from a demand zone below. "
              "zone = lo/hi of one of that market's levels (for a single level use the same number twice). t = the steps in simple words "
              "(where the price must go first, a fresh M15 zone there, what to see on M5: liquidity grab or sweep, engulfing or MSB, stop behind the sweep). "
              "tp = 1 or 2 target prices from the levels on the other side. x = when the plan is off. Plan 1 follows the label (short for bearish, long for bullish); "
              "plan 2 is the other side, smaller or skip. With Neutraal give one long and one short plan. Never promise a result.",
              "- htf: d1 and h4 texts (max 90 chars) from the computed higher-timeframe facts; todo (max 150 chars) = what the trader checks on his own H1 and H4 chart.",
              "- brief: what changed since the previous run, what the market reacts to, how that fits the desk, what decides the coming session. "
              "moodTo: 2 greedy, 1 optimistic, 0 neutral, -1 careful, -2 scared. watch: 2 or 3 concrete things with levels, Brussels times and events.",
              "- moveNote (max 140): only where the line above says REQUIRED: what it means that the price moves against the desk today. Otherwise null.",
              "- risk (max 140): the event risk for the coming session: the calendar items that can move this market, with Brussels day and time. Not a stop or an invalidation level.",
              "- flip (max 160): what would flip or weaken the bias (a level and/or an event).",
              "- Keep these trader words in English exactly as written: liquidity grab, sweep, engulfing, MSB, supply, demand, zone, fresh, killzone, long, short, stop.",
              "- g on US500 and US100: what the state of that index means for gold. regime.g, dxy.g, context g, fed.g, sentiment.g: what it means for gold.",
              "JSON shape: " + json.dumps({
                  "headline": "max 140 chars, gold first", "summary": ["exactly 3 bullets, max 130 chars"],
                  "brief": {"title": "max 80", "moodTo": -1, "text": "max 560, 4-6 short sentences", "watch": ["2-3 bullets, max 110"]},
                  "regime": {"label": "max 40", "tone": -1, "text": "max 240", "g": {"d": 0, "t": "max 130"}},
                  "dxy": {"text": "max 220", "g": {"d": 0, "t": "max 160"}, "watch": "max 170"},
                  "assets": {aid: {"call": "max 120", "thesis": "max 320",
                                   "factors": {k: "max 220" for k, _, _, _ in L.MODEL[aid]},
                                   "htf": {"d1": "max 90", "h4": "max 90", "todo": "max 150"},
                                   "plans": [{"side": -1, "zone": {"lo": 0, "hi": 0}, "t": "max 240", "tp": [0], "x": "max 110"}],
                                   "flip": "max 160", "risk": "max 140", "playbook": "max 260", "moveNote": None,
                                   **({"g": {"d": 0, "t": "max 130"}} if aid != "XAUUSD" else {})} for aid in PRIMARY},
                  "context": {c["id"]: {"note": "max 90", "g": {"d": 0, "t": "max 110"}} for c in context_raw},
                  "fed": {"g": {"d": 0, "t": "max 130"}},
                  "sentiment": {"chatter": "max 260", "g": {"d": 0, "t": "max 130"}}}, ensure_ascii=False)]
    p2 = pack + "\n" + "\n".join(lines)
    log(f"[i] opdracht 2: {len(p2)} tekens")
    j2 = safe(lambda: ai(p2, 7000), "AI tekst") if j1 else None
    if j2:
        log("[ok] AI tekst")

    # ---- 4. agenda, nieuws, social, les
    picked = []
    for x in (j1 or {}).get("news") or []:
        if isinstance(x, dict) and isinstance(x.get("i"), int) and 0 <= x["i"] < len(heads) and x["i"] not in [p["i"] for p in picked]:
            picked.append({"i": x["i"], "tags": [t for t in (x.get("tags") or []) if isinstance(t, dict) and t.get("id") in PRIMARY]})
    if not picked:
        picked = [{"i": i, "tags": []} for i in range(min(6, len(heads)))]
    picked = picked[:8]
    lines3 = ["", "TASK 3: write in very simple Dutch:",
              "- calendar: for every event id: ev = the event name in Dutch (short, may keep CPI, PMI, m/m), per = the period like 'nov' or null, "
              "note (max 90) = what this number or event is, g = what it means for gold (both sides when it still has to come out; once actual is filled in, what the outcome did).",
              "- news: for each chosen headline index: why (max 120) = what happened in simple Dutch, g = what it means for gold.",
              "- social: read (max 280) = what people on X talk about most and whether that agrees with the desk; mood per market (-2..2, the crowd, not the desk); "
              "themes 2-5 {t max 28, heat 1-3, d for gold}; posts: for each post index: sum (max 200, your own simple Dutch words, no copying), tags, g; g for the whole talk. "
              "With no posts: read about the Stocktwits meters and the Fear & Greed index, posts empty.",
              f"- learn: one short lesson tied to today (not the same as '{prev_learn}'): h (max 60), t (max 420), quiz {{q (max 160), a: exactly 3 answers (max 70), ok: index of the right one, why (max 200)}}.",
              "- words: 2-4 words from today's texts a beginner may not know (not trader words from the list), each with a simple explanation (max 110).",
              "Chosen headlines: " + ", ".join(str(p["i"]) for p in picked),
              "JSON shape: " + json.dumps({
                  "calendar": {x["id"]: {"ev": "", "per": None, "note": "", "g": {"d": 0, "t": ""}} for x in cal},
                  "news": {str(p["i"]): {"why": "", "g": {"d": 0, "t": ""}} for p in picked},
                  "social": {"read": "", "mood": {"XAUUSD": 0, "US500": 0, "US100": 0}, "themes": [{"t": "", "heat": 2, "d": 0}],
                             "posts": {f"p{i}": {"sum": "", "tags": [{"id": "XAUUSD", "d": 0}], "g": {"d": 0, "t": ""}} for i in range(len(posts))},
                             "g": {"d": 0, "t": ""}},
                  "learn": {"h": "", "t": "", "quiz": {"q": "", "a": ["", "", ""], "ok": 0, "why": ""}},
                  "words": [{"w": "", "t": ""}]}, ensure_ascii=False)]
    p3 = pack + "\n" + "\n".join(lines3)
    log(f"[i] opdracht 3: {len(p3)} tekens")
    j3 = safe(lambda: ai(p3, 6000), "AI agenda en les") if j1 else None
    if j3:
        log("[ok] AI agenda, nieuws, social en les")

    desk = assemble(run_id, session, S, assets, dxy, dxy_levels, context_raw, fx, cal, heads, picked, posts, meters, fg, fed,
                    HT, j1 or {}, j2 or {}, j3 or {}, prev)
    validate(desk)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(desk_path, "w", encoding="utf-8") as fh:
        json.dump(desk, fh, ensure_ascii=False, separators=(",", ":"))
    calls = {a["id"]: {"px": a["px"], "d": [f["d"] for f in a["factors"]], "w": [f["w"] for f in a["factors"]],
                       "f": [1 if f["fresh"] else 0 for f in a["factors"]], "ep": a["ep"], "score": a["score"], "conf": a["conf"]}
             for a in desk["assets"]}
    runs = [x for x in runs if x.get("runId") != run_id]
    runs.append({"runId": run_id, "asof": desk["asof"], "session": session,
                 "dxy": {"px": desk["dxy"]["px"], "lean": desk["dxy"]["lean"]} if desk.get("dxy") else None,
                 "calls": calls, "src": "github"})
    runs.sort(key=lambda x: x.get("asof") or "")
    with open(runs_path, "w", encoding="utf-8") as fh:
        json.dump(runs[-600:], fh, ensure_ascii=False, separators=(",", ":"))
    size = len(json.dumps(desk, ensure_ascii=False))
    log(f"Klaar. AI: {ENGINE['ai']}. Kosten zoeken ongeveer ${COST[0]:.4f}. Desk {size} tekens. Ontbrak: {', '.join(MISSING) or 'niets'}.")


def yfetch_ctx():
    reqs = {"US10Y": ("^TNX", "1mo", "1d", False), "VIX": ("^VIX", "1mo", "1d", False), "WTI": ("CL=F", "1mo", "1d", False),
            "BRENT": ("BZ=F", "1mo", "1d", False), "XAGUSD": ("SI=F", "1mo", "1d", False), "COPPER": ("HG=F", "1mo", "1d", False),
            "BTCUSD": ("BTC-USD", "1mo", "1d", False), "EURUSD": ("EURUSD=X", "1mo", "1d", False), "GBPUSD": ("GBPUSD=X", "1mo", "1d", False),
            "USDJPY": ("USDJPY=X", "1mo", "1d", False), "AUDUSD": ("AUDUSD=X", "1mo", "1d", False), "USDCAD": ("USDCAD=X", "1mo", "1d", False),
            "USDCHF": ("USDCHF=X", "1mo", "1d", False)}
    Y = yfetch_many(reqs)
    out = {}
    for k, y in Y.items():
        if not y or not y["bars"]:
            continue
        m = y["meta"]
        px = m.get("regularMarketPrice") or y["bars"][-1]["c"]
        t = m.get("regularMarketTime") or y["bars"][-1]["t"]
        tz = ZoneInfo(m.get("exchangeTimezoneName") or "America/New_York")
        dn = datetime.fromtimestamp(t, tz).date()
        prior = [b for b in y["bars"] if datetime.fromtimestamp(b["t"], tz).date() < dn]
        prev = prior[-1]["c"] if prior else None
        out[k] = {"px": px, "prev": prev, "chgPct": pct(px, prev), "bars": [{"c": b["c"], "h": b["h"], "l": b["l"]} for b in prior]}
    return out


def assemble(run_id, session, S, assets, dxy, dxy_levels, context_raw, fx, cal, heads, picked, posts, meters, fg, fed, HT, j1, j2, j3, prev):
    T = j2.get("assets") or {}
    out_assets = []
    for a in assets:
        aid, t = a["id"], T.get(a["id"]) or {}
        lab = L.label(a["score"])
        ftx = t.get("factors") or {}
        factors = []
        for f_ in a["factors"]:
            txt = cut(ftx.get(f_["k"]), 220) if f_["fresh"] else None
            if not txt:
                txt = f_["text"] if not f_["fresh"] else (f_["_ev"] or f_["text"])
            factors.append({"k": f_["k"], "label": f_["label"], "w": f_["w"], "d": f_["d"], "fresh": f_["fresh"],
                            "text": str(txt)[:220], "src": f_["src"] or ("Yahoo Finance" if f_["fresh"] else "")})
        plans = valid_plans(t.get("plans"), a, a["levels"]) or code_plans(a, a["levels"])
        if plans and lab != "Neutraal" and plans[0]["side"] != (1 if a["score"] > 0 else -1):
            plans = list(reversed(plans)) if len(plans) == 2 else plans
        ht = HT.get(aid) or {}
        htx = t.get("htf") or {}

        def tf(k):
            h = ht.get(k)
            if not h:
                return None
            txt = cut(htx.get(k), 90) or ("Omhoog." if h["d"] > 0 else "Omlaag." if h["d"] < 0 else "Zijwaarts of onduidelijk.")
            return {"d": h["d"], "t": txt}
        htf = {"d1": tf("d1"), "h4": tf("h4"),
               "todo": cut(htx.get("todo"), 150) or "Kijk op H1 en H4 welke kant de toppen en bodems op gaan. Zoek alleen M15-zones in die richting.",
               "src": CALC}
        call = fix_call(t.get("call") or ("Wacht op een duidelijke zone" if lab == "Neutraal" else "Werk alleen met zones in de richting van de desk"), lab)
        item = {"id": aid, "name": NAMES[aid], "px": a["px"], "chg": a["chg"], "chgPct": a["chgPct"], "pxSrc": a["pxSrc"], "pxAt": a["pxAt"],
                "call": call[:120], "thesis": cut(t.get("thesis"), 320) or default_thesis(a),
                "factors": factors, "ep": a["ep"], "epWhy": a["epWhy"][:40], "score": a["score"], "conf": a["conf"],
                "htf": htf, "levels": a["levels"], "plans": plans,
                "flip": cut(t.get("flip"), 160) or "Breekt de koers door de dichtstbijzijnde zone aan de andere kant, dan wordt de verwachting zwakker.",
                "risk": cut(t.get("risk"), 140) or (a["epWhy"] if a["ep"] else "Geen groot cijfer in de komende sessie."),
                "playbook": cut(t.get("playbook"), 260) or "Wacht in de Londen- of NY-killzone op een liquidity grab bij een zone en daarna een MSB. Geen duidelijke zone? Sta aan de kant."}
        cp = a["chgPct"]
        if cut(t.get("moveNote"), 140) and cp is not None and abs(a["score"]) >= 15 and abs(cp) >= 0.25 and (cp > 0) != (a["score"] > 0):
            item["moveNote"] = cut(t.get("moveNote"), 140)
        if aid != "XAUUSD":
            g = g_ok(t.get("g"))
            if g:
                item["g"] = g
        out_assets.append(item)

    dt = j2.get("dxy") or {}
    dxy_out = None
    if dxy:
        lean = (j1 or {}).get("dxyLean")
        dxy_out = {"px": r(dxy["px"], 3), "chg": r(dxy["px"] - dxy["prev"], 3) if dxy.get("prev") else None,
                   "chgPct": r(pct(dxy["px"], dxy["prev"]), 2), "pxSrc": dxy["src"], "pxAt": iso(dxy["at"]),
                   "lean": int(clamp(jsround(lean), -2, 2)) if isinstance(lean, (int, float)) else 0,
                   "text": cut(dt.get("text"), 220) or f"De dollar-index staat op {nl(dxy['px'], 2)}.",
                   "g": g_ok(dt.get("g"), "Een sterkere dollar drukt goud. Een zwakkere dollar helpt goud."),
                   "levels": dxy_levels[:4],
                   "watch": cut(dt.get("watch"), 170) or "Let op de dichtstbijzijnde niveaus: breekt de dollar erboven, dan krijgt goud meer druk."}
    leans = (j1 or {}).get("leans") or {}
    ctx_t = j2.get("context") or {}
    context = []
    for c in context_raw:
        ct = ctx_t.get(c["id"]) or {}
        lv = leans.get(c["id"])
        context.append({"id": c["id"], "name": c["name"], "px": c["px"], "chgPct": c["chgPct"],
                        "lean": int(clamp(jsround(lv), -2, 2)) if isinstance(lv, (int, float)) else None,
                        "note": cut(ct.get("note"), 90) or f"{nl(c['px'], 2)}, {sgn(c['chgPct'], 2, '%')} vandaag.",
                        "g": g_ok(ct.get("g"), "Geen duidelijke duw voor goud.")})
    C3 = j3.get("calendar") or {}
    calendar = []
    for x in cal[:16]:
        ct = C3.get(x["id"]) or {}
        calendar.append({"t": x["t"], "cty": x["cty"], "ev": cut(ct.get("ev"), 60) or x["ev"], "per": cut(ct.get("per"), 12),
                         "imp": x["imp"], "prev": x["prev"], "cons": x["cons"], "act": x.get("act"),
                         "note": cut(ct.get("note"), 90) or "", "g": g_ok(ct.get("g"), "Nog niet bekend. Pas na het cijfer zie je wat het met goud doet."),
                         "hits": x["hits"]})
    N3 = j3.get("news") or {}
    news = []
    for p in picked:
        n = heads[p["i"]]
        nt = N3.get(str(p["i"])) or {}
        tags = [{"id": t["id"], "d": int(clamp(jsround(t.get("d") or 0), -1, 1))} for t in p["tags"]]
        g = g_ok(nt.get("g"))
        gd = next((t["d"] for t in tags if t["id"] == "XAUUSD"), None)
        if g and gd is not None:
            g["d"] = gd
        news.append({"t": iso(n["t"] or NOW), "h": n["h"][:200], "src": n["src"] or "Google News", "url": n["url"],
                     "tags": tags, "why": cut(nt.get("why"), 120) or n["h"][:120], "g": g or {"d": gd or 0, "t": "Geen duidelijke duw voor goud."}})
    news.sort(key=lambda x: x["t"], reverse=True)
    SO = j3.get("social") or {}
    social_posts = []
    PT = SO.get("posts") or {}
    for i, p in enumerate(posts):
        pt = PT.get(f"p{i}") or {}
        sm = cut(pt.get("sum"), 200)
        if not sm:
            continue
        item = {"t": p["t"], "who": p["who"], "net": p["net"], "kind": p["kind"], "trust": p["trust"], "url": p["url"], "sum": sm,
                "tags": [{"id": t["id"], "d": int(clamp(jsround(t.get("d") or 0), -1, 1))} for t in (pt.get("tags") or [])
                         if isinstance(t, dict) and t.get("id") in PRIMARY],
                "g": g_ok(pt.get("g"), "Een bericht, geen feit. Voor goud geen directe duw.")}
        if p["day"]:
            item["day"] = True
        if p["handle"]:
            item["handle"] = p["handle"]
        if p["reach"]:
            item["reach"] = p["reach"]
        social_posts.append(item)
    social = None
    if social_posts or meters or SO.get("read"):
        mood = {k: int(clamp(jsround(v), -2, 2)) for k, v in (SO.get("mood") or {}).items() if k in PRIMARY and isinstance(v, (int, float))}
        social = {"read": cut(SO.get("read"), 280) or "Deze run vond geen berichten van X. Hieronder de stemming op Stocktwits.",
                  "mood": mood or {k: 0 for k in PRIMARY},
                  "themes": [{"t": str(x.get("t"))[:28], "heat": int(clamp(x.get("heat") or 1, 1, 3)), "d": int(clamp(jsround(x.get("d") or 0), -1, 1))}
                             for x in (SO.get("themes") or []) if isinstance(x, dict) and x.get("t")][:5],
                  "posts": social_posts, "meters": meters,
                  "g": g_ok(SO.get("g"), "De stemming online geeft goud nu geen duidelijke duw.")}
    lr = j3.get("learn") or {}
    qz = lr.get("quiz") or {}
    learn = None
    if cut(lr.get("h"), 60) and cut(lr.get("t"), 420):
        learn = {"h": cut(lr.get("h"), 60), "t": cut(lr.get("t"), 420)}
        ans = [str(x)[:70] for x in (qz.get("a") or []) if isinstance(x, str) and x.strip()]
        if cut(qz.get("q"), 160) and len(ans) == 3 and isinstance(qz.get("ok"), int) and 0 <= qz["ok"] <= 2:
            learn["quiz"] = {"q": cut(qz.get("q"), 160), "a": ans, "ok": qz["ok"], "why": cut(qz.get("why"), 200) or ""}
    elif prev.get("learn"):
        learn = prev.get("learn")
    words = [{"w": str(w.get("w"))[:30], "t": str(w.get("t"))[:110]} for w in (j3.get("words") or []) if isinstance(w, dict) and w.get("w") and w.get("t")][:4]
    br = j2.get("brief") or {}
    mood_to = br.get("moodTo")
    mood_to = int(clamp(jsround(mood_to), -2, 2)) if isinstance(mood_to, (int, float)) else 0
    prev_mood = ((prev.get("brief") or {}).get("mood") or {}).get("to") if prev.get("v") == 3 else None
    lead = next((a for a in out_assets if a["id"] == "XAUUSD"), out_assets[0])
    headline = cut(j2.get("headline"), 140) or f"Goud: {L.label(lead['score']).lower()}. {lead['call']}"[:140]
    summary = [str(x)[:130] for x in (j2.get("summary") or []) if isinstance(x, str) and x.strip()][:3]
    while len(summary) < 3:
        a = out_assets[len(summary) % len(out_assets)]
        summary.append(f"{a['name']}: {L.label(a['score']).lower()}, {sgn(a['chgPct'], 2, '%')} vandaag.")
    rg = j2.get("regime") or {}
    tone = rg.get("tone")
    desk = {
        "v": 3, "runId": run_id, "asof": iso(NOW), "session": session, "headline": headline, "summary": summary,
        "brief": {"title": cut(br.get("title"), 80) or headline[:80],
                  "mood": {"from": prev_mood if isinstance(prev_mood, int) else mood_to, "to": mood_to},
                  "text": cut(br.get("text"), 560) or " ".join(summary),
                  "watch": [str(x)[:110] for x in (br.get("watch") or []) if isinstance(x, str) and x.strip()][:3] or summary[:2]},
        "regime": {"label": cut(rg.get("label"), 40) or "Gemengd", "tone": int(clamp(jsround(tone), -2, 2)) if isinstance(tone, (int, float)) else 0,
                   "text": cut(rg.get("text"), 240) or summary[0], "g": g_ok(rg.get("g"), "Geen duidelijke duw voor goud.")},
        "dxy": dxy_out, "assets": out_assets, "context": context, "fx": fx, "calendar": calendar, "news": news,
        "fed": None, "sentiment": None, "learn": learn, "words": words,
        "sources": [{"n": "Yahoo Finance (koersen)", "u": "https://finance.yahoo.com"},
                    {"n": "Swissquote (goud spot)", "u": "https://www.swissquote.com"},
                    {"n": "ForexFactory (agenda)", "u": "https://www.forexfactory.com/calendar"},
                    {"n": "Federal Reserve (vergaderdata)", "u": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"},
                    {"n": "NY Fed (rente)", "u": "https://www.newyorkfed.org/markets/reference-rates/effr"},
                    {"n": "US Treasury (rente)", "u": "https://home.treasury.gov/resource-center/data-chart-center/interest-rates"},
                    {"n": "CNN Fear & Greed", "u": "https://edition.cnn.com/markets/fear-and-greed"},
                    {"n": "Stocktwits", "u": "https://stocktwits.com"},
                    {"n": "Google News", "u": "https://news.google.com"}],
        "health": {"missing": MISSING[:12], "note": "; ".join(NOTES)[:160]},
        "engine": {"ai": ENGINE["ai"], "host": "GitHub Actions", "v": 3},
    }
    if social:
        desk["social"] = social
    if fed:
        fg_ = (j2.get("fed") or {}).get("g")
        desk["fed"] = {k: fed[k] for k in ("meeting", "hike", "hold", "cut", "rate", "src", "asof")}
        desk["fed"]["g"] = g_ok(fg_, "Hoe groter de kans op een verhoging, hoe meer druk op goud.")
    st = j2.get("sentiment") or {}
    if fg or social:
        desk["sentiment"] = {"fg": fg, "chatter": cut(st.get("chatter"), 260) or (social or {}).get("read") or "",
                             "chatterSrc": "X; Stocktwits" if social_posts else "Stocktwits; CNN",
                             "g": g_ok(st.get("g"), "De stemming geeft goud nu geen duidelijke duw.")}
        if not fg:
            desk["sentiment"].pop("fg")
    for n in news:
        if n["url"]:
            desk["sources"].append({"n": f"{n['src']}: {n['h'][:40]}", "u": n["url"]})
    for p in social_posts:
        if p["url"]:
            desk["sources"].append({"n": f"{p['who']} ({p['net']})", "u": p["url"]})
    desk["sources"] = desk["sources"][:24]
    if desk["fed"] is None:
        desk.pop("fed")
    if desk["sentiment"] is None:
        desk.pop("sentiment")
    if not desk["learn"]:
        desk.pop("learn")
    return desk


def default_thesis(a):
    up = [f["label"].lower() for f in a["factors"] if f["d"] > 0]
    dn = [f["label"].lower() for f in a["factors"] if f["d"] < 0]
    parts = []
    if dn:
        parts.append("Omlaag duwen: " + ", ".join(dn) + ".")
    if up:
        parts.append("Omhoog duwen: " + ", ".join(up) + ".")
    return (" ".join(parts) or "De krachten houden elkaar in evenwicht.")[:320]


def validate(desk):
    """Laatste controle: elke markt heeft 7 factoren, plannen met zones uit de levels, en de desk past in de grens."""
    for a in desk["assets"]:
        assert len(a["factors"]) == 7, a["id"]
        assert a["call"].startswith(L.label(a["score"])), a["call"]
        for p in a["plans"]:
            assert p["side"] in (-1, 1) and p["tp"], p
    n = len(json.dumps(desk, ensure_ascii=False))
    if n > 60000:
        desk["sources"] = desk["sources"][:12]
        for x in desk["news"]:
            x["h"] = x["h"][:120]


if __name__ == "__main__":
    main()
