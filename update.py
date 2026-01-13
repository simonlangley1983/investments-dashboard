#!/usr/bin/env python3
"""
update.py — Investment game updater (fixed shares + daily deltas + totals since start + TTL caching)

Key requirements:
- Hold FIXED quantities (shares) and revalue over time.
- Record daily totals in history.json.
- Provide per-holding daily change vs previous snapshot.
- Provide per-holding totals since start (per ticker).

Change (Jan 2026):
- "Since last run" requires prices that move intraday. Stooq quote endpoint returns CLOSE and often
  stays flat intraday, causing 0.000% everywhere. We now:
    - Prefer Stooq 5-minute bars (latest bar close) for intraday movement
    - Fall back to quote close if intraday is unavailable
    - Use shorter TTLs suitable for intraday prices/FX

Portfolios:
- A/B definitions + weights live in portfolios.json (used ONLY to initialise shares once)
- C definition + weights live in portfolio_c.json (used ONLY to initialise shares once)
- S&P 500 benchmark tracked via SPY.US fixed shares (initialised once)

Outputs:
- latest.json  : snapshot (A/B/C totals + holdings breakdown + per-holding deltas vs prev
                + per-holding totals since start + benchmark snapshot)
- state.json   : caches + A/B/C shares + benchmark shares + inception allocations
- history.json : one row per UK date, updated each run for "today"
                (portfolio_A, portfolio_B, portfolio_C, benchmark_sp500) — writes what it has (no gating)
"""

import csv
import json
import os
from datetime import datetime, timezone
from urllib.request import Request, urlopen

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    ZoneInfo = None


ROOT = os.path.dirname(os.path.abspath(__file__))

STATE_PATH = os.path.join(ROOT, "state.json")
LATEST_PATH = os.path.join(ROOT, "latest.json")
HISTORY_PATH = os.path.join(ROOT, "history.json")
PORTFOLIOS_PATH = os.path.join(ROOT, "portfolios.json")

# Separate definition files
PORTFOLIO_C_PATH = os.path.join(ROOT, "portfolio_c.json")
BENCHMARKS_PATH = os.path.join(ROOT, "benchmarks.json")

# Stooq endpoints
STOOQ_QUOTE = "https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv"
STOOQ_BARS = "https://stooq.com/q/d/l/?s={symbol}&i={interval}"  # interval: 5 (5-min), h (hourly), d (daily)

# TTLs (intraday friendly)
PRICE_CACHE_TTL_SECONDS = 4 * 60     # 4 minutes
FX_CACHE_TTL_SECONDS = 10 * 60       # 10 minutes

INCEPTION_ALLOC_KEY = "inception_allocations_gbp"

BASELINE_GBP = 1_000_000.0
BASELINE_UK_DATE = "2026-01-01"

PORT_STATE_KEY = {
    "portfolio_A": "A",
    "portfolio_B": "B",
    "portfolio_C": "C",
}


# -------------------------
# Time helpers
# -------------------------
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def uk_today_iso() -> str:
    if ZoneInfo is not None:
        return datetime.now(ZoneInfo("Europe/London")).date().isoformat()
    return datetime.now(timezone.utc).date().isoformat()


def parse_iso_z(s: str):
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def is_fresh(updated_at_iso: str, ttl_seconds: int) -> bool:
    dt = parse_iso_z(updated_at_iso)
    if dt is None:
        return False
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age = (now - dt).total_seconds()
    return age >= 0 and age <= ttl_seconds


def clamp_neg_zero(x: float, eps: float = 1e-12) -> float:
    try:
        xf = float(x)
    except Exception:
        return 0.0
    if abs(xf) < eps:
        return 0.0
    return xf


# -------------------------
# JSON helpers
# -------------------------
def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=False)
    os.replace(tmp, path)


# -------------------------
# HTTP / Stooq
# -------------------------
def http_get(url: str, timeout: int = 25) -> str:
    req = Request(url, headers={"User-Agent": "invest-game-bot"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def stooq_quote(symbol: str) -> dict:
    url = STOOQ_QUOTE.format(symbol=symbol)
    text = http_get(url)

    reader = csv.DictReader(text.splitlines())
    rows = list(reader)
    if not rows:
        raise ValueError(f"No quote rows returned for {symbol}")

    row = rows[0]
    if row.get("Close") in (None, "", "N/D"):
        raise ValueError(f"No Close for {symbol}: {row}")

    def to_float(x: str) -> float:
        if x in (None, "", "N/D"):
            return float("nan")
        return float(x)

    return {
        "symbol": symbol,
        "date": row.get("Date"),
        "time": row.get("Time"),
        "open": to_float(row.get("Open")),
        "high": to_float(row.get("High")),
        "low": to_float(row.get("Low")),
        "close": to_float(row.get("Close")),
        "volume": to_float(row.get("Volume")),
        "source": "stooq",
        "fetched_at": utc_now_iso(),
    }


def stooq_latest_close_from_bars(symbol: str, interval: str) -> dict:
    """
    Fetch latest bar close from Stooq bars endpoint.
    interval examples:
      - "5"  : 5-minute
      - "h"  : hourly
      - "d"  : daily
    Returns dict with: close, bar_time (string), bar_date (string), source
    """
    url = STOOQ_BARS.format(symbol=symbol.lower(), interval=interval)
    text = http_get(url)

    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if len(lines) < 2:
        raise ValueError(f"No bars rows for {symbol} interval={interval}")

    header = lines[0].split(",")
    last = lines[-1].split(",")
    if len(last) < 5:
        raise ValueError(f"Malformed bars row for {symbol} interval={interval}: {lines[-1]}")

    # Bars format typically: Date,Open,High,Low,Close,Volume
    # For intraday (5/h), Date may include time component (depends on Stooq)
    # We'll store it as-is.
    bar_dt = last[0].strip()
    close = float(last[4])

    return {
        "close": close,
        "bar_dt": bar_dt,
        "source": f"stooq_bars_{interval}",
    }


def infer_currency_from_ticker(ticker: str) -> str:
    t = (ticker or "").strip().upper()
    if t.endswith(".US"):
        return "USD"
    if t.endswith(".UK"):
        return "GBP"
    if t == "CASH":
        return "GBP"
    return "GBP"


# -------------------------
# Portfolio weights parsing
# -------------------------
def _is_number(x) -> bool:
    try:
        float(x)
        return True
    except Exception:
        return False


def extract_weights(pdef: dict) -> dict:
    """
    Supports:
      {"name": "...", "weights": {...}}
      OR direct mapping numeric keys -> weights
    """
    if not isinstance(pdef, dict):
        return {}
    w = pdef.get("weights")
    if isinstance(w, dict) and w:
        return {str(k).strip(): float(v) for k, v in w.items() if _is_number(v)}
    out = {}
    for k, v in pdef.items():
        if str(k) in ("name", "key", "holdings", "start_gbp"):
            continue
        if _is_number(v):
            out[str(k).strip()] = float(v)
    return out


# -------------------------
# FX conversion (with TTL)
# -------------------------
def fx_to_gbp_rate(from_ccy: str, state_cache: dict) -> float:
    """
    Returns GBP per unit of from_ccy (e.g. USD -> GBP).
    Uses intraday bars for gbpusd where possible; falls back to quote close.
    """
    c = (from_ccy or "GBP").upper()
    if c == "GBP":
        return 1.0

    fx_cache = state_cache.setdefault("fx_cache", {})
    now = utc_now_iso()

    def get_cached(key: str):
        v = fx_cache.get(key)
        if isinstance(v, dict) and "rate" in v and "updated_at" in v:
            if is_fresh(v.get("updated_at"), FX_CACHE_TTL_SECONDS):
                try:
                    return float(v["rate"])
                except Exception:
                    return None
        return None

    def set_cached(key: str, rate: float, meta: dict | None = None):
        fx_cache[key] = {"rate": float(rate), "updated_at": now}
        if meta:
            fx_cache[key].update(meta)

    if c == "USD":
        cached = get_cached("USDGBP")
        if cached is not None:
            return cached

        # Prefer intraday bars for gbpusd
        gbp_usd = None
        meta = {}
        try:
            b = stooq_latest_close_from_bars("gbpusd", "5")
            gbp_usd = float(b["close"])  # USD per GBP
            meta = {"source": b.get("source"), "bar_dt": b.get("bar_dt")}
        except Exception:
            q = stooq_quote("gbpusd")
            gbp_usd = float(q["close"])
            meta = {"source": "stooq_quote", "quote_date": q.get("date"), "quote_time": q.get("time")}

        if gbp_usd <= 0:
            raise ValueError("Invalid GBPUSD rate from Stooq")
        usd_gbp = 1.0 / gbp_usd
        set_cached("USDGBP", usd_gbp, meta)
        return usd_gbp

    raise ValueError(f"Unsupported currency for FX: {c}")


def gbp_to_ccy_rate(to_ccy: str, state_cache: dict) -> float:
    """
    Returns units of to_ccy per GBP (e.g. GBP -> USD).
    Uses intraday bars for gbpusd where possible; falls back to quote close.
    """
    c = (to_ccy or "GBP").upper()
    if c == "GBP":
        return 1.0

    fx_cache = state_cache.setdefault("fx_cache", {})
    now = utc_now_iso()

    def get_cached(key: str):
        v = fx_cache.get(key)
        if isinstance(v, dict) and "rate" in v and "updated_at" in v:
            if is_fresh(v.get("updated_at"), FX_CACHE_TTL_SECONDS):
                try:
                    return float(v["rate"])
                except Exception:
                    return None
        return None

    def set_cached(key: str, rate: float, meta: dict | None = None):
        fx_cache[key] = {"rate": float(rate), "updated_at": now}
        if meta:
            fx_cache[key].update(meta)

    if c == "USD":
        cached = get_cached("GBPUSD")
        if cached is not None:
            return cached

        gbp_usd = None
        meta = {}
        try:
            b = stooq_latest_close_from_bars("gbpusd", "5")
            gbp_usd = float(b["close"])  # USD per GBP
            meta = {"source": b.get("source"), "bar_dt": b.get("bar_dt")}
        except Exception:
            q = stooq_quote("gbpusd")
            gbp_usd = float(q["close"])
            meta = {"source": "stooq_quote", "quote_date": q.get("date"), "quote_time": q.get("time")}

        if gbp_usd <= 0:
            raise ValueError("Invalid GBPUSD rate from Stooq")
        set_cached("GBPUSD", gbp_usd, meta)
        return gbp_usd

    raise ValueError(f"Unsupported currency for FX: {c}")


# -------------------------
# Pricing (with TTL) — prefer intraday bars
# -------------------------
def price_for_holding(ticker: str, state_cache: dict) -> float:
    """
    Returns a "current" price suitable for 'since last run'.
    - Prefer Stooq 5-minute bars latest close
    - Fall back to Stooq quote close
    Cached for PRICE_CACHE_TTL_SECONDS.
    """
    px_cache = state_cache.setdefault("price_cache", {})
    now = utc_now_iso()

    cached = px_cache.get(ticker)
    if isinstance(cached, dict) and "price" in cached and "updated_at" in cached:
        if is_fresh(cached.get("updated_at"), PRICE_CACHE_TTL_SECONDS):
            try:
                return float(cached["price"])
            except Exception:
                pass

    price = None
    meta = {}
    # Try intraday first
    try:
        b = stooq_latest_close_from_bars(ticker, "5")
        price = float(b["close"])
        meta = {"source": b.get("source"), "bar_dt": b.get("bar_dt")}
    except Exception:
        q = stooq_quote(ticker)
        price = float(q["close"])
        meta = {"source": "stooq_quote", "quote_date": q.get("date"), "quote_time": q.get("time")}

    px_cache[ticker] = {
        "price": float(price),
        "updated_at": now,
        **meta,
    }
    return float(price)


# -------------------------
# Shares initialisation (ONE TIME)
# -------------------------
def init_state_shares_from_weights(portfolios_root: dict, state: dict):
    """
    Initialise shares ONCE if missing:
      - A/B from portfolios.json weights using portfolios_root["start_gbp"]
      - C from portfolio_c.json weights using portfolio_c["start_gbp"] (default £1m)
    """
    start_gbp_ab = float(portfolios_root.get("start_gbp", BASELINE_GBP))

    # A/B
    for pkey in ("portfolio_A", "portfolio_B"):
        pdef = portfolios_root.get(pkey)
        if not isinstance(pdef, dict):
            continue

        weights = extract_weights(pdef)
        if not weights:
            continue

        skey = PORT_STATE_KEY[pkey]
        existing = state.get(skey, {}).get("shares")
        if isinstance(existing, dict) and len(existing) > 0:
            continue

        shares = {}
        for ticker, weight in weights.items():
            if weight <= 0:
                continue

            ticker = str(ticker).strip()
            allocation_gbp = start_gbp_ab * float(weight)
            ccy = infer_currency_from_ticker(ticker)

            if ticker.upper() == "CASH":
                shares["CASH"] = shares.get("CASH", 0.0) + allocation_gbp
                continue

            price = price_for_holding(ticker, state)
            gbp_to_ccy = gbp_to_ccy_rate(ccy, state) if ccy != "GBP" else 1.0
            allocation_ccy = allocation_gbp * gbp_to_ccy
            qty = allocation_ccy / price if price else 0.0
            shares[ticker] = qty

        state.setdefault(skey, {})["shares"] = shares

    # C
    cdef = load_json(PORTFOLIO_C_PATH, {})
    if isinstance(cdef, dict):
        c_start = float(cdef.get("start_gbp", BASELINE_GBP))
        weights = extract_weights(cdef)
        if weights:
            skey = PORT_STATE_KEY["portfolio_C"]
            existing = state.get(skey, {}).get("shares")
            if not (isinstance(existing, dict) and len(existing) > 0):
                shares = {}
                for ticker, weight in weights.items():
                    if float(weight) <= 0:
                        continue
                    ticker = str(ticker).strip()
                    allocation_gbp = c_start * float(weight)
                    ccy = infer_currency_from_ticker(ticker)

                    if ticker.upper() == "CASH":
                        shares["CASH"] = shares.get("CASH", 0.0) + allocation_gbp
                        continue

                    price = price_for_holding(ticker, state)
                    gbp_to_ccy = gbp_to_ccy_rate(ccy, state) if ccy != "GBP" else 1.0
                    allocation_ccy = allocation_gbp * gbp_to_ccy
                    qty = allocation_ccy / price if price else 0.0
                    shares[ticker] = qty

                state.setdefault(skey, {})["shares"] = shares


# -------------------------
# Inception allocations (ONE TIME)
# -------------------------
def ensure_inception_allocations(portfolios_root: dict, state: dict):
    """
    Persist per-ticker inception allocations in GBP for A/B/C (once).
    This enables per-ticker totals since start without needing inception prices/FX.
    """
    store = state.setdefault(INCEPTION_ALLOC_KEY, {})
    if not isinstance(store, dict):
        state[INCEPTION_ALLOC_KEY] = {}
        store = state[INCEPTION_ALLOC_KEY]

    start_gbp_ab = float(portfolios_root.get("start_gbp", BASELINE_GBP))

    # A/B
    for pkey in ("portfolio_A", "portfolio_B"):
        existing = store.get(pkey)
        if isinstance(existing, dict) and existing:
            continue

        pdef = portfolios_root.get(pkey)
        if not isinstance(pdef, dict):
            continue

        weights = extract_weights(pdef)
        if not weights:
            continue

        allocs = {}
        for ticker, weight in weights.items():
            if float(weight) <= 0:
                continue
            allocs[str(ticker).strip()] = round(start_gbp_ab * float(weight), 8)

        store[pkey] = allocs

    # C
    existing = store.get("portfolio_C")
    if not (isinstance(existing, dict) and existing):
        cdef = load_json(PORTFOLIO_C_PATH, {})
        if isinstance(cdef, dict):
            c_start = float(cdef.get("start_gbp", BASELINE_GBP))
            weights = extract_weights(cdef)
            if weights:
                store["portfolio_C"] = {
                    str(t).strip(): round(c_start * float(w), 8)
                    for t, w in weights.items()
                    if float(w) > 0
                }


def get_inception_alloc_for(portfolio_key: str, ticker: str, state: dict) -> float:
    store = state.get(INCEPTION_ALLOC_KEY, {})
    if not isinstance(store, dict):
        return 0.0
    p = store.get(portfolio_key, {})
    if not isinstance(p, dict):
        return 0.0

    t = str(ticker).strip().upper()
    for k, v in p.items():
        if str(k).strip().upper() == t:
            try:
                return float(v)
            except Exception:
                return 0.0
    return 0.0


# -------------------------
# Previous snapshot lookup
# -------------------------
def build_prev_holdings_index(prev_latest: dict, portfolio_key: str) -> dict:
    idx = {}
    p = prev_latest.get(portfolio_key)
    if not isinstance(p, dict):
        return idx
    hs = p.get("holdings")
    if not isinstance(hs, list):
        return idx
    for h in hs:
        if isinstance(h, dict) and h.get("ticker"):
            idx[str(h["ticker"]).strip().upper()] = h
    return idx


def direction_from_delta(d: float) -> str:
    d = clamp_neg_zero(d)
    if d > 0:
        return "up"
    if d < 0:
        return "down"
    return "flat"


# -------------------------
# Valuation (A/B/C)
# -------------------------
def value_from_state_shares(portfolio_key: str, portfolio_name: str, state: dict, prev_latest: dict) -> dict:
    skey = PORT_STATE_KEY[portfolio_key]
    shares = state.get(skey, {}).get("shares", {})
    if not isinstance(shares, dict):
        shares = {}

    prev_idx = build_prev_holdings_index(prev_latest, portfolio_key)

    holdings = []
    total_gbp = 0.0
    inception_total_gbp = 0.0

    for ticker, qty in shares.items():
        try:
            qty = float(qty)
        except Exception:
            continue
        if qty == 0.0:
            continue

        ticker = str(ticker).strip()
        tkey = ticker.upper()
        ccy = infer_currency_from_ticker(ticker)

        inception_alloc_gbp = get_inception_alloc_for(portfolio_key, ticker, state)
        inception_total_gbp += inception_alloc_gbp

        if tkey == "CASH":
            price = 1.0
            fx = 1.0
            value_gbp = qty
        else:
            price = price_for_holding(ticker, state)
            fx = fx_to_gbp_rate(ccy, state) if ccy != "GBP" else 1.0
            value_gbp = (qty * price) * fx

        prev = prev_idx.get(tkey)
        prev_price = float(prev.get("price")) if isinstance(prev, dict) and prev.get("price") is not None else None
        prev_value = float(prev.get("value_gbp")) if isinstance(prev, dict) and prev.get("value_gbp") is not None else None

        change_price = (price - prev_price) if prev_price is not None else 0.0
        change_value = (value_gbp - prev_value) if prev_value is not None else 0.0
        change_pct = (change_value / prev_value * 100.0) if (prev_value is not None and prev_value != 0) else 0.0

        change_price = clamp_neg_zero(round(change_price, 6))
        change_value = clamp_neg_zero(round(change_value, 2))
        change_pct = clamp_neg_zero(round(change_pct, 4))

        total_change_gbp = (value_gbp - inception_alloc_gbp) if inception_alloc_gbp else 0.0
        total_change_pct = (total_change_gbp / inception_alloc_gbp * 100.0) if inception_alloc_gbp else 0.0
        total_change_gbp = clamp_neg_zero(round(total_change_gbp, 2))
        total_change_pct = clamp_neg_zero(round(total_change_pct, 4))

        holdings.append(
            {
                "ticker": ticker,
                "qty": round(qty, 8),
                "currency": ccy,
                "price": round(price, 6),
                "fx_to_gbp": round(fx, 8),
                "value_gbp": round(value_gbp, 2),
                "type": "cash" if tkey == "CASH" else "asset",
                "change_price_vs_prev": change_price,
                "change_value_gbp_vs_prev": change_value,
                "change_pct_vs_prev": change_pct,
                "price_direction": direction_from_delta(change_price),
                "inception_value_gbp": round(inception_alloc_gbp, 2),
                "total_change_gbp_since_start": total_change_gbp,
                "total_change_pct_since_start": total_change_pct,
                "total_direction": direction_from_delta(total_change_gbp),
            }
        )

        total_gbp += value_gbp

    port_total_change_gbp = (total_gbp - inception_total_gbp) if inception_total_gbp else 0.0
    port_total_change_pct = (port_total_change_gbp / inception_total_gbp * 100.0) if inception_total_gbp else 0.0
    port_total_change_gbp = clamp_neg_zero(round(port_total_change_gbp, 2))
    port_total_change_pct = clamp_neg_zero(round(port_total_change_pct, 4))

    return {
        "key": portfolio_key,
        "name": portfolio_name,
        "holdings": holdings,
        "total_value_gbp": round(total_gbp, 2),
        "inception_value_gbp": round(inception_total_gbp, 2),
        "total_change_gbp_since_start": port_total_change_gbp,
        "total_change_pct_since_start": port_total_change_pct,
        "total_direction": direction_from_delta(port_total_change_gbp),
    }


# -------------------------
# Benchmark: S&P500 (SPY.US fixed shares)
# -------------------------
def ensure_sp500_position(state: dict) -> dict:
    """
    Initialise benchmark shares once (state["benchmarks"]["sp500"]["qty"]).
    """
    b = load_json(BENCHMARKS_PATH, {})
    sp = b.get("sp500") if isinstance(b, dict) else None
    if not isinstance(sp, dict):
        sp = {"name": "S&P 500", "ticker": "SPY.US"}

    ticker = sp.get("ticker", "SPY.US")

    bench = state.setdefault("benchmarks", {})
    s = bench.setdefault("sp500", {})

    if s.get("ticker") != ticker:
        s.clear()
        s["ticker"] = ticker

    if s.get("qty") is not None:
        return s

    # initialise using current price/fx so inception is £1m
    ccy = infer_currency_from_ticker(ticker)  # USD
    price0 = price_for_holding(ticker, state)
    gbp_to_usd0 = gbp_to_ccy_rate(ccy, state)
    alloc_usd = BASELINE_GBP * gbp_to_usd0
    qty = alloc_usd / price0 if price0 else 0.0

    s["ticker"] = ticker
    s["qty"] = float(qty)
    s["inception_value_gbp"] = float(BASELINE_GBP)
    s["name"] = sp.get("name", "S&P 500")
    return s


def sp500_snapshot(state: dict, prev_latest: dict) -> dict:
    s = ensure_sp500_position(state)
    ticker = s.get("ticker", "SPY.US")
    name = s.get("name", "S&P 500")

    qty = s.get("qty")
    if qty is None:
        return {"name": name, "ticker": ticker, "total_value_gbp": None}

    qty = float(qty)
    ccy = infer_currency_from_ticker(ticker)
    price = price_for_holding(ticker, state)
    fx = fx_to_gbp_rate(ccy, state)
    value_gbp = qty * price * fx

    inception = float(s.get("inception_value_gbp", BASELINE_GBP))
    total_change_gbp = clamp_neg_zero(round(value_gbp - inception, 2))
    total_change_pct = clamp_neg_zero(round((total_change_gbp / inception * 100.0) if inception else 0.0, 4))

    # since last run vs prev snapshot
    prev_val = None
    try:
        prev_val = prev_latest.get("benchmarks", {}).get("sp500", {}).get("total_value_gbp")
        prev_val = float(prev_val) if prev_val is not None else None
    except Exception:
        prev_val = None

    change_vs_prev = clamp_neg_zero(round((value_gbp - prev_val) if prev_val is not None else 0.0, 2))
    change_pct_vs_prev = clamp_neg_zero(
        round((change_vs_prev / prev_val * 100.0) if (prev_val not in (None, 0)) else 0.0, 4)
    )

    return {
        "name": name,
        "ticker": ticker,
        "qty": round(qty, 8),
        "currency": ccy,
        "price": round(price, 6),
        "fx_to_gbp": round(fx, 8),
        "total_value_gbp": round(value_gbp, 2),
        "inception_value_gbp": round(inception, 2),
        "total_change_gbp_since_start": total_change_gbp,
        "total_change_pct_since_start": total_change_pct,
        "change_value_gbp_vs_prev": change_vs_prev,
        "change_pct_vs_prev": change_pct_vs_prev,
        "direction": direction_from_delta(total_change_gbp),
    }


# -------------------------
# History (daily totals)
# -------------------------
def upsert_daily_history(latest: dict):
    """
    Writes what it has (no gating), so chart can still draw series when available.
    Ensures baseline row exists.
    """
    today = uk_today_iso()
    history = load_json(HISTORY_PATH, [])
    if not isinstance(history, list):
        history = []

    # baseline row
    if not any(isinstance(r, dict) and r.get("date") == BASELINE_UK_DATE for r in history):
        history.append(
            {
                "date": BASELINE_UK_DATE,
                "portfolio_A": BASELINE_GBP,
                "portfolio_B": BASELINE_GBP,
                "portfolio_C": BASELINE_GBP,
                "benchmark_sp500": BASELINE_GBP,
            }
        )

    row = {"date": today}

    for key in ("portfolio_A", "portfolio_B", "portfolio_C"):
        pdata = latest.get(key)
        if isinstance(pdata, dict) and pdata.get("total_value_gbp") is not None:
            row[key] = round(float(pdata["total_value_gbp"]), 2)

    sp = latest.get("benchmarks", {}).get("sp500", {})
    if isinstance(sp, dict) and sp.get("total_value_gbp") is not None:
        row["benchmark_sp500"] = round(float(sp["total_value_gbp"]), 2)

    # upsert
    for r in history:
        if isinstance(r, dict) and r.get("date") == today:
            r.update(row)
            save_json(HISTORY_PATH, sorted(history, key=lambda x: x.get("date", "")))
            return

    history.append(row)
    save_json(HISTORY_PATH, sorted(history, key=lambda x: x.get("date", "")))


# -------------------------
# Main
# -------------------------
def main():
    portfolios_root = load_json(PORTFOLIOS_PATH, {})
    if not isinstance(portfolios_root, dict) or not portfolios_root:
        raise SystemExit("portfolios.json missing or empty")

    state = load_json(STATE_PATH, {})
    if not isinstance(state, dict):
        state = {}

    prev_latest = load_json(LATEST_PATH, {}) if os.path.exists(LATEST_PATH) else {}
    if not isinstance(prev_latest, dict):
        prev_latest = {}

    # Initialise shares + inception allocations (only if missing)
    init_state_shares_from_weights(portfolios_root, state)
    ensure_inception_allocations(portfolios_root, state)

    # Build latest
    latest = {
        "as_of_utc": utc_now_iso(),
        "as_of_uk_date": uk_today_iso(),
    }

    # Portfolio A/B names from portfolios.json
    for pkey in ("portfolio_A", "portfolio_B"):
        pdef = portfolios_root.get(pkey, {})
        pname = pdef.get("name") if isinstance(pdef, dict) else None
        if not pname:
            pname = "Portfolio A" if pkey == "portfolio_A" else "Portfolio B"
        latest[pkey] = value_from_state_shares(pkey, pname, state, prev_latest)

    # Portfolio C name from portfolio_c.json
    cdef = load_json(PORTFOLIO_C_PATH, {})
    cname = cdef.get("name") if isinstance(cdef, dict) and cdef.get("name") else "Portfolio C"
    latest["portfolio_C"] = value_from_state_shares("portfolio_C", cname, state, prev_latest)

    # Portfolio-level since last run change vs prev total
    for pkey in ("portfolio_A", "portfolio_B", "portfolio_C"):
        pdata = latest.get(pkey)
        if not isinstance(pdata, dict) or "total_value_gbp" not in pdata:
            continue
        prev_total = prev_latest.get(pkey, {}).get("total_value_gbp")
        if prev_total is not None:
            try:
                delta = float(pdata["total_value_gbp"]) - float(prev_total)
                pdata["change_gbp_vs_prev"] = clamp_neg_zero(round(delta, 2))
            except Exception:
                pdata["change_gbp_vs_prev"] = 0.0
        else:
            pdata["change_gbp_vs_prev"] = 0.0

    # Benchmark snapshot
    latest["benchmarks"] = {"sp500": sp500_snapshot(state, prev_latest)}

    # Persist
    state["last_run_utc"] = latest["as_of_utc"]
    save_json(LATEST_PATH, latest)
    save_json(STATE_PATH, state)

    # History
    upsert_daily_history(latest)


if __name__ == "__main__":
    main()
