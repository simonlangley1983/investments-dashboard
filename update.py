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

Change (Apr 2026):
- Added split detection and automatic quantity adjustment.
- If a ticker appears to have undergone a stock split, we:
    - multiply stored quantity by the inferred split ratio
    - preserve economic value
    - prevent fake crashes/gains caused by post-split price changes
- Split handling is persisted in state.json so it only applies once.

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
import math
import os
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.parse import quote

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

# Yahoo Finance fallback endpoint (no API key required).
# Used when Stooq returns anti-bot/browser-verification HTML instead of CSV.
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range}&interval={interval}&includePrePost=true"

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

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

# Split detection
KNOWN_SPLIT_RATIOS = [2, 3, 4, 5, 8, 10]
SPLIT_DETECTION_TOLERANCE = 0.18  # 18% either side of ideal ratio
MIN_SPLIT_TRIGGER_RATIO = 1.6     # ignore smaller moves


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
# HTTP / Stooq / Yahoo fallback
# -------------------------
def http_get(url: str, timeout: int = 25) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": BROWSER_USER_AGENT,
            "Accept": "text/csv,application/json,text/plain,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
            "Cache-Control": "no-cache",
        },
    )
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def looks_like_html_or_challenge(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    return (
        t.startswith("<!doctype html")
        or t.startswith("<html")
        or "<script" in t[:2000]
        or "requires javascript" in t
        or "__verify" in t
        or "verify your browser" in t
    )


def ensure_not_html_challenge(text: str, symbol: str, source: str):
    if looks_like_html_or_challenge(text):
        snippet = " ".join((text or "").split())[:240]
        raise ValueError(f"{source} returned HTML/browser verification for {symbol}: {snippet}")


def stooq_quote(symbol: str) -> dict:
    url = STOOQ_QUOTE.format(symbol=symbol)
    text = http_get(url)
    ensure_not_html_challenge(text, symbol, "Stooq quote")

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
    ensure_not_html_challenge(text, symbol, f"Stooq bars interval={interval}")

    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if len(lines) < 2:
        raise ValueError(f"No bars rows for {symbol} interval={interval}")

    last = lines[-1].split(",")
    if len(last) < 5:
        raise ValueError(f"Malformed bars row for {symbol} interval={interval}: {lines[-1]}")

    bar_dt = last[0].strip()
    close = float(last[4])

    return {
        "close": close,
        "bar_dt": bar_dt,
        "source": f"stooq_bars_{interval}",
    }


def yahoo_symbol_from_ticker(ticker: str) -> str:
    """
    Convert dashboard/Stooq-ish symbols to Yahoo Finance symbols.
    Examples:
      NVDA.US -> NVDA
      SPY.US  -> SPY
      ISF.UK  -> ISF.L
      ISF.LN  -> ISF.L
      gbpusd  -> GBPUSD=X
    """
    t = (ticker or "").strip().upper()
    if t in ("GBPUSD", "GBPUSD=X"):
        return "GBPUSD=X"
    if t in ("USDGBP", "USDGBP=X"):
        return "GBPUSD=X"
    if t.endswith(".US"):
        return t[:-3]
    if t.endswith(".UK"):
        return t[:-3] + ".L"
    if t.endswith(".LN"):
        return t[:-3] + ".L"
    if t.endswith(".LON"):
        return t[:-4] + ".L"
    return t


def yahoo_chart_latest_price(ticker: str) -> dict:
    """
    Latest usable price from Yahoo chart endpoint.
    This is intentionally dependency-free. It is used as a fallback when Stooq blocks GitHub Actions.
    """
    symbol = yahoo_symbol_from_ticker(ticker)
    errors = []

    for rng, interval in (("1d", "1m"), ("5d", "5m"), ("1mo", "1d")):
        url = YAHOO_CHART.format(symbol=quote(symbol, safe=""), range=rng, interval=interval)
        try:
            text = http_get(url)
            ensure_not_html_challenge(text, ticker, "Yahoo chart")
            payload = json.loads(text)
            result = (payload.get("chart", {}).get("result") or [None])[0]
            if not isinstance(result, dict):
                errors.append(f"{rng}/{interval}: no chart result")
                continue

            meta = result.get("meta") or {}
            price = meta.get("regularMarketPrice")
            if price is None:
                quote_block = ((result.get("indicators") or {}).get("quote") or [None])[0]
                closes = (quote_block or {}).get("close") or []
                usable = [float(x) for x in closes if x is not None and math.isfinite(float(x))]
                price = usable[-1] if usable else None

            if price is None:
                errors.append(f"{rng}/{interval}: no usable price")
                continue

            price = float(price)
            if not math.isfinite(price) or price <= 0:
                errors.append(f"{rng}/{interval}: invalid price {price}")
                continue

            return {
                "close": price,
                "symbol": symbol,
                "source": f"yahoo_chart_{rng}_{interval}",
                "fetched_at": utc_now_iso(),
            }
        except Exception as e:
            errors.append(f"{rng}/{interval}: {e}")

    raise ValueError(f"Yahoo chart failed for {ticker} ({symbol}): " + " | ".join(errors[-3:]))


def stale_cached_price(ticker: str, state_cache: dict) -> float | None:
    """
    Emergency fallback: return any previous cached price, even if expired.
    This prevents one temporary data-source block from killing the entire GitHub Action.
    """
    px_cache = state_cache.setdefault("price_cache", {})
    cached = px_cache.get(ticker)
    if isinstance(cached, dict) and "price" in cached:
        try:
            price = float(cached["price"])
            if math.isfinite(price) and price > 0:
                return price
        except Exception:
            return None
    return None

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

        gbp_usd = None
        meta = {}
        errors = []
        try:
            b = stooq_latest_close_from_bars("gbpusd", "5")
            gbp_usd = float(b["close"])
            meta = {"source": b.get("source"), "bar_dt": b.get("bar_dt")}
        except Exception as e:
            errors.append(f"stooq_bars: {e}")
            try:
                q = stooq_quote("gbpusd")
                gbp_usd = float(q["close"])
                meta = {"source": "stooq_quote", "quote_date": q.get("date"), "quote_time": q.get("time")}
            except Exception as e2:
                errors.append(f"stooq_quote: {e2}")
                try:
                    y = yahoo_chart_latest_price("GBPUSD=X")
                    gbp_usd = float(y["close"])
                    meta = {"source": y.get("source"), "yahoo_symbol": y.get("symbol")}
                except Exception as e3:
                    errors.append(f"yahoo_chart: {e3}")

        if gbp_usd is None or gbp_usd <= 0:
            stale = get_cached("GBPUSD")
            if stale is not None:
                gbp_usd = stale
                meta = {"source": "stale_fx_cache", "warnings": errors[-3:]}
            else:
                raise ValueError("Invalid GBPUSD rate; " + " | ".join(errors[-3:]))
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
        errors = []
        try:
            b = stooq_latest_close_from_bars("gbpusd", "5")
            gbp_usd = float(b["close"])
            meta = {"source": b.get("source"), "bar_dt": b.get("bar_dt")}
        except Exception as e:
            errors.append(f"stooq_bars: {e}")
            try:
                q = stooq_quote("gbpusd")
                gbp_usd = float(q["close"])
                meta = {"source": "stooq_quote", "quote_date": q.get("date"), "quote_time": q.get("time")}
            except Exception as e2:
                errors.append(f"stooq_quote: {e2}")
                try:
                    y = yahoo_chart_latest_price("GBPUSD=X")
                    gbp_usd = float(y["close"])
                    meta = {"source": y.get("source"), "yahoo_symbol": y.get("symbol")}
                except Exception as e3:
                    errors.append(f"yahoo_chart: {e3}")

        if gbp_usd is None or gbp_usd <= 0:
            stale = get_cached("GBPUSD")
            if stale is not None:
                gbp_usd = stale
                meta = {"source": "stale_fx_cache", "warnings": errors[-3:]}
            else:
                raise ValueError("Invalid GBPUSD rate; " + " | ".join(errors[-3:]))
        set_cached("GBPUSD", gbp_usd, meta)
        return gbp_usd

    raise ValueError(f"Unsupported currency for FX: {c}")


# -------------------------
# Pricing (with TTL) — prefer intraday bars
# -------------------------
def price_for_holding(ticker: str, state_cache: dict) -> float:
    """
    Returns a "current" price suitable for 'since last run'.
    Source order:
      1) Stooq 5-minute bars latest close
      2) Stooq quote close
      3) Yahoo Finance chart endpoint
      4) Expired/stale cached price as emergency fallback

    The stale-cache fallback is deliberate: a temporary provider block should not crash
    the whole dashboard run and stop latest/history JSON from being written.
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
    errors = []

    try:
        b = stooq_latest_close_from_bars(ticker, "5")
        price = float(b["close"])
        meta = {"source": b.get("source"), "bar_dt": b.get("bar_dt")}
    except Exception as e:
        errors.append(f"stooq_bars: {e}")
        try:
            q = stooq_quote(ticker)
            price = float(q["close"])
            meta = {"source": "stooq_quote", "quote_date": q.get("date"), "quote_time": q.get("time")}
        except Exception as e2:
            errors.append(f"stooq_quote: {e2}")
            try:
                y = yahoo_chart_latest_price(ticker)
                price = float(y["close"])
                meta = {"source": y.get("source"), "yahoo_symbol": y.get("symbol")}
            except Exception as e3:
                errors.append(f"yahoo_chart: {e3}")

    if price is None or not math.isfinite(float(price)) or float(price) <= 0:
        stale = stale_cached_price(ticker, state_cache)
        if stale is not None:
            price = stale
            meta = {"source": "stale_price_cache", "warnings": errors[-3:]}
        else:
            raise ValueError(f"No usable price for {ticker}: " + " | ".join(errors[-3:]))

    px_cache[ticker] = {
        "price": float(price),
        "updated_at": now,
        **meta,
    }
    return float(price)

def infer_split_ratio(prev_price: float, new_price: float):
    """
    Infer a likely stock split ratio from a large step down in price.
    Example:
      prev 806, new 100.8 -> ratio ~8
    """
    if prev_price is None or new_price is None:
        return None
    if prev_price <= 0 or new_price <= 0:
        return None

    raw_ratio = prev_price / new_price
    if raw_ratio < MIN_SPLIT_TRIGGER_RATIO:
        return None

    best = None
    best_diff = None

    for r in KNOWN_SPLIT_RATIOS:
        diff = abs(raw_ratio - r) / r
        if best_diff is None or diff < best_diff:
            best = r
            best_diff = diff

    if best is not None and best_diff is not None and best_diff <= SPLIT_DETECTION_TOLERANCE:
        return best
    return None


def maybe_apply_split_to_state(state: dict, portfolio_key: str, ticker: str, prev_latest: dict) -> int | None:
    """
    Detect likely split by comparing previous displayed price with newly fetched price.
    If detected and not already applied, multiply stored quantity and record it in state.
    Returns applied ratio if a split was applied, else None.
    """
    skey = PORT_STATE_KEY[portfolio_key]
    shares = state.get(skey, {}).get("shares", {})
    if not isinstance(shares, dict):
        return None

    ticker_upper = str(ticker).strip().upper()
    actual_key = None
    for k in shares.keys():
        if str(k).strip().upper() == ticker_upper:
            actual_key = k
            break
    if actual_key is None:
        return None

    prev_idx = build_prev_holdings_index(prev_latest, portfolio_key)
    prev = prev_idx.get(ticker_upper)
    if not isinstance(prev, dict):
        return None

    prev_price = prev.get("price")
    if prev_price is None:
        return None

    try:
        prev_price = float(prev_price)
    except Exception:
        return None

    try:
        new_price = price_for_holding(actual_key, state)
    except Exception:
        return None

    ratio = infer_split_ratio(prev_price, new_price)
    if ratio is None:
        return None

    applied_splits = state.setdefault("applied_splits", {})
    portfolio_splits = applied_splits.setdefault(portfolio_key, {})
    already = portfolio_splits.get(ticker_upper)
    if already == ratio:
        return None

    try:
        old_qty = float(shares[actual_key])
    except Exception:
        return None

    new_qty = old_qty * ratio
    shares[actual_key] = new_qty
    portfolio_splits[ticker_upper] = ratio

    px_cache = state.setdefault("price_cache", {})
    if actual_key in px_cache:
        px_cache.pop(actual_key, None)

    return ratio


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

    for ticker in list(shares.keys()):
        if str(ticker).strip().upper() == "CASH":
            continue
        maybe_apply_split_to_state(state, portfolio_key, ticker, prev_latest)

    shares = state.get(skey, {}).get("shares", {})

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

    ccy = infer_currency_from_ticker(ticker)
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

    init_state_shares_from_weights(portfolios_root, state)
    ensure_inception_allocations(portfolios_root, state)

    latest = {
        "as_of_utc": utc_now_iso(),
        "as_of_uk_date": uk_today_iso(),
    }

    for pkey in ("portfolio_A", "portfolio_B"):
        pdef = portfolios_root.get(pkey, {})
        pname = pdef.get("name") if isinstance(pdef, dict) else None
        if not pname:
            pname = "Portfolio A" if pkey == "portfolio_A" else "Portfolio B"
        latest[pkey] = value_from_state_shares(pkey, pname, state, prev_latest)

    cdef = load_json(PORTFOLIO_C_PATH, {})
    cname = cdef.get("name") if isinstance(cdef, dict) and cdef.get("name") else "Portfolio C"
    latest["portfolio_C"] = value_from_state_shares("portfolio_C", cname, state, prev_latest)

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

    latest["benchmarks"] = {"sp500": sp500_snapshot(state, prev_latest)}

    state["last_run_utc"] = latest["as_of_utc"]
    save_json(LATEST_PATH, latest)
    save_json(STATE_PATH, state)

    upsert_daily_history(latest)


if __name__ == "__main__":
    main()
