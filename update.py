#!/usr/bin/env python3
"""
update.py — Investment game updater (A/B/C fixed shares + S&P500 benchmark + daily history)

Key points:
- A/B/C: fixed shares (initialised once from weights), revalued each run.
- Benchmark: SPY.US fixed shares (initialised once) representing £1m at inception, revalued each run.
- history.json: one row per UK date, ALWAYS includes A/B/C/S&P so the chart always has 4 lines.
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

# NEW
PORTFOLIO_C_PATH = os.path.join(ROOT, "portfolio_c.json")
BENCHMARKS_PATH = os.path.join(ROOT, "benchmarks.json")

STOOQ_QUOTE = "https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv"

# Challenge baseline
BASELINE_UK_DATE = "2026-01-01"
BASELINE_GBP = 1_000_000.0

CACHE_TTL_SECONDS = 55 * 60  # 55 minutes
INCEPTION_ALLOC_KEY = "inception_allocations_gbp"

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
def http_get(url: str, timeout: int = 20) -> str:
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


# -------------------------
# Parsing weights
# -------------------------
def _is_number(x) -> bool:
    try:
        float(x)
        return True
    except Exception:
        return False


def extract_weights(pdef: dict) -> dict:
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
    c = (from_ccy or "GBP").upper()
    if c == "GBP":
        return 1.0

    fx_cache = state_cache.setdefault("fx_cache", {})
    now = utc_now_iso()

    def get_cached(key: str):
        v = fx_cache.get(key)
        if isinstance(v, dict) and "rate" in v and "updated_at" in v:
            if is_fresh(v.get("updated_at"), CACHE_TTL_SECONDS):
                try:
                    return float(v["rate"])
                except Exception:
                    return None
        return None

    def set_cached(key: str, rate: float):
        fx_cache[key] = {"rate": float(rate), "updated_at": now}

    if c == "USD":
        cached = get_cached("USDGBP")
        if cached is not None:
            return cached
        q = stooq_quote("gbpusd")
        gbp_usd = float(q["close"])  # USD per GBP
        if gbp_usd <= 0:
            raise ValueError("Invalid GBPUSD rate from Stooq")
        usd_gbp = 1.0 / gbp_usd
        set_cached("USDGBP", usd_gbp)
        return usd_gbp

    raise ValueError(f"Unsupported currency for FX: {c}")


def gbp_to_ccy_rate(to_ccy: str, state_cache: dict) -> float:
    c = (to_ccy or "GBP").upper()
    if c == "GBP":
        return 1.0

    fx_cache = state_cache.setdefault("fx_cache", {})
    now = utc_now_iso()

    def get_cached(key: str):
        v = fx_cache.get(key)
        if isinstance(v, dict) and "rate" in v and "updated_at" in v:
            if is_fresh(v.get("updated_at"), CACHE_TTL_SECONDS):
                try:
                    return float(v["rate"])
                except Exception:
                    return None
        return None

    def set_cached(key: str, rate: float):
        fx_cache[key] = {"rate": float(rate), "updated_at": now}

    if c == "USD":
        cached = get_cached("GBPUSD")
        if cached is not None:
            return cached
        q = stooq_quote("gbpusd")
        gbp_usd = float(q["close"])  # USD per GBP
        if gbp_usd <= 0:
            raise ValueError("Invalid GBPUSD rate from Stooq")
        set_cached("GBPUSD", gbp_usd)
        return gbp_usd

    raise ValueError(f"Unsupported currency for FX: {c}")


# -------------------------
# Pricing (with TTL)
# -------------------------
def price_for_holding(ticker: str, state_cache: dict) -> float:
    px_cache = state_cache.setdefault("price_cache", {})
    now = utc_now_iso()

    cached = px_cache.get(ticker)
    if isinstance(cached, dict) and "price" in cached and "updated_at" in cached:
        if is_fresh(cached.get("updated_at"), CACHE_TTL_SECONDS):
            try:
                return float(cached["price"])
            except Exception:
                pass

    q = stooq_quote(ticker)
    close = float(q["close"])
    px_cache[ticker] = {
        "price": close,
        "quote_date": q.get("date"),
        "quote_time": q.get("time"),
        "updated_at": now,
        "source": "stooq",
    }
    return close


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
# Shares initialisation (ONE TIME)
# -------------------------
def init_state_shares_from_weights(portfolios_root: dict, state: dict):
    start_gbp = portfolios_root.get("start_gbp")
    if start_gbp is None:
        return
    start_gbp = float(start_gbp)

    for pkey, skey in PORT_STATE_KEY.items():
        # Portfolio C weights are in portfolio_c.json (not portfolios.json)
        if pkey == "portfolio_C":
            pdef = load_json(PORTFOLIO_C_PATH, {})
            port_start = float(pdef.get("start_gbp", start_gbp))
        else:
            pdef = portfolios_root.get(pkey)
            port_start = start_gbp

        if not isinstance(pdef, dict):
            continue

        weights = extract_weights(pdef)
        if not weights:
            continue

        existing = state.get(skey, {}).get("shares")
        if isinstance(existing, dict) and len(existing) > 0:
            continue  # already initialised

        shares = {}
        for ticker, weight in weights.items():
            if weight <= 0:
                continue

            ticker = str(ticker).strip()
            ccy = infer_currency_from_ticker(ticker)
            allocation_gbp = port_start * weight

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
    store = state.setdefault(INCEPTION_ALLOC_KEY, {})
    if not isinstance(store, dict):
        state[INCEPTION_ALLOC_KEY] = {}
        store = state[INCEPTION_ALLOC_KEY]

    # A/B inception from portfolios.json, C inception from portfolio_c.json
    for pkey in ("portfolio_A", "portfolio_B"):
        if isinstance(store.get(pkey), dict) and store[pkey]:
            continue
        pdef = portfolios_root.get(pkey)
        if not isinstance(pdef, dict):
            continue
        start_gbp = float(portfolios_root.get("start_gbp", BASELINE_GBP))
        weights = extract_weights(pdef)
        store[pkey] = {str(t).strip(): round(start_gbp * float(w), 8) for t, w in weights.items() if float(w) > 0}

    if not (isinstance(store.get("portfolio_C"), dict) and store["portfolio_C"]):
        pdef = load_json(PORTFOLIO_C_PATH, {})
        if isinstance(pdef, dict):
            start_gbp = float(pdef.get("start_gbp", BASELINE_GBP))
            weights = extract_weights(pdef)
            store["portfolio_C"] = {str(t).strip(): round(start_gbp * float(w), 8) for t, w in weights.items() if float(w) > 0}


def get_inception_alloc_for(portfolio_key: str, ticker: str, state: dict) -> float:
    store = state.get(INCEPTION_ALLOC_KEY, {})
    if not isinstance(store, dict):
        return 0.0
    p = store.get(portfolio_key, {})
    if not isinstance(p, dict):
        return 0.0
    for k, v in p.items():
        if str(k).strip().upper() == str(ticker).strip().upper():
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
# Valuation (for A/B/C)
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
            fx = fx_to_gbp_rate(ccy, state)
            value_gbp = qty * fx
            price = 1.0
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
                "fx_to_gbp": round((fx_to_gbp_rate(ccy, state) if ccy != "GBP" else 1.0), 8),
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
# Benchmark (S&P500 via SPY.US)
# -------------------------
def ensure_sp500_benchmark_position(state: dict):
    b = load_json(BENCHMARKS_PATH, {})
    sp = b.get("sp500") if isinstance(b, dict) else None
    if not isinstance(sp, dict):
        return None

    ticker = sp.get("ticker")
    if not ticker:
        return None

    bench = state.setdefault("benchmarks", {})
    sp_state = bench.setdefault("sp500", {})
    if sp_state.get("ticker") != ticker:
        sp_state.clear()
        sp_state["ticker"] = ticker

    if "qty" in sp_state and sp_state.get("qty") is not None:
        return sp_state

    # Initialise "buy £1m of SPY" once at inception (using current price/fx on first run)
    ccy = infer_currency_from_ticker(ticker)  # SPY.US -> USD
    price0 = price_for_holding(ticker, state)
    gbp_to_usd0 = gbp_to_ccy_rate(ccy, state)
    alloc_usd = BASELINE_GBP * gbp_to_usd0
    qty = alloc_usd / price0 if price0 else 0.0

    sp_state["ticker"] = ticker
    sp_state["qty"] = float(qty)
    sp_state["inception_gbp"] = float(BASELINE_GBP)
    sp_state["inception_note"] = "Notional £1m into SPY at inception"

    return sp_state


def sp500_value_gbp(state: dict) -> float:
    sp_state = ensure_sp500_benchmark_position(state)
    if not isinstance(sp_state, dict):
        return None
    ticker = sp_state.get("ticker")
    qty = sp_state.get("qty")
    if not ticker or qty is None:
        return None
    qty = float(qty)

    ccy = infer_currency_from_ticker(ticker)
    price = price_for_holding(ticker, state)
    fx = fx_to_gbp_rate(ccy, state)
    return qty * price * fx


# -------------------------
# History
# -------------------------
def ensure_baseline_row(history: list):
    for r in history:
        if isinstance(r, dict) and r.get("date") == BASELINE_UK_DATE:
            # ensure all keys exist
            r.setdefault("portfolio_A", BASELINE_GBP)
            r.setdefault("portfolio_B", BASELINE_GBP)
            r.setdefault("portfolio_C", BASELINE_GBP)
            r.setdefault("benchmark_sp500", BASELINE_GBP)
            return

    history.append(
        {
            "date": BASELINE_UK_DATE,
            "portfolio_A": BASELINE_GBP,
            "portfolio_B": BASELINE_GBP,
            "portfolio_C": BASELINE_GBP,
            "benchmark_sp500": BASELINE_GBP,
        }
    )


def upsert_daily_history(latest: dict, state: dict):
    today = uk_today_iso()
    history = load_json(HISTORY_PATH, [])
    if not isinstance(history, list):
        history = []

    ensure_baseline_row(history)

    a = latest.get("portfolio_A", {}).get("total_value_gbp")
    b = latest.get("portfolio_B", {}).get("total_value_gbp")
    c = latest.get("portfolio_C", {}).get("total_value_gbp")
    sp = sp500_value_gbp(state)

    # Only write today if all series are present (keeps chart consistent)
    if a is None or b is None or c is None or sp is None:
        save_json(HISTORY_PATH, sorted(history, key=lambda r: r.get("date", "")))
        return

    row = {
        "date": today,
        "portfolio_A": round(float(a), 2),
        "portfolio_B": round(float(b), 2),
        "portfolio_C": round(float(c), 2),
        "benchmark_sp500": round(float(sp), 2),
    }

    for r in history:
        if isinstance(r, dict) and r.get("date") == today:
            r.update(row)
            save_json(HISTORY_PATH, sorted(history, key=lambda r: r.get("date", "")))
            return

    history.append(row)
    save_json(HISTORY_PATH, sorted(history, key=lambda r: r.get("date", "")))


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

    # Init shares + allocations once (A/B from portfolios.json, C from portfolio_c.json)
    init_state_shares_from_weights(portfolios_root, state)
    ensure_inception_allocations(portfolios_root, state)

    latest = {
        "as_of_utc": utc_now_iso(),
        "as_of_uk_date": uk_today_iso(),
    }

    # Value A/B from portfolios.json names
    for pkey in ("portfolio_A", "portfolio_B"):
        pdef = portfolios_root.get(pkey, {})
        pname = pdef.get("name") if isinstance(pdef, dict) else None
        if not pname:
            pname = "Portfolio A" if pkey == "portfolio_A" else "Portfolio B"
        latest[pkey] = value_from_state_shares(pkey, pname, state, prev_latest)

    # Value C from portfolio_c.json name
    cdef = load_json(PORTFOLIO_C_PATH, {})
    cname = cdef.get("name") if isinstance(cdef, dict) else None
    if not cname:
        cname = "Portfolio C"
    latest["portfolio_C"] = value_from_state_shares("portfolio_C", cname, state, prev_latest)

    # Per-portfolio change vs previous snapshot (A/B/C)
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

    # Include benchmark in latest.json too (for display/debug)
    sp_val = sp500_value_gbp(state)
    latest["benchmarks"] = {
        "sp500": {
            "name": load_json(BENCHMARKS_PATH, {}).get("sp500", {}).get("name", "S&P 500"),
            "ticker": load_json(BENCHMARKS_PATH, {}).get("sp500", {}).get("ticker", "SPY.US"),
            "total_value_gbp": round(float(sp_val), 2) if sp_val is not None else None,
        }
    }

    state["last_run_utc"] = latest["as_of_utc"]

    save_json(LATEST_PATH, latest)
    save_json(STATE_PATH, state)

    # history row with A/B/C/SP500
    upsert_daily_history(latest, state)


if __name__ == "__main__":
    main()
