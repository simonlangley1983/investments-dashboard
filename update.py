#!/usr/bin/env python3
"""
update.py — Investment game updater (RUNNING TOTAL + per-holding deltas + per-holding totals since start + TTL caching)

Key requirement:
- Hold FIXED quantities (shares) and revalue over time.
- Record daily totals in history.json.

Quantities source of truth:
- Portfolio A shares at state["A"]["shares"]
- Portfolio B shares at state["B"]["shares"]

Weights in portfolios.json are ONLY used to initialise shares ONCE
(if state shares don't already exist).

Adds:
- TTL cache for prices/FX so deltas update when new data arrives, without hammering Stooq.
- Per-holding daily indicator fields in latest.json:
  - change_price_vs_prev
  - change_value_gbp_vs_prev
  - change_pct_vs_prev
  - price_direction: "up" | "down" | "flat"

Fixes:
- Per-holding "Total % change" since start is now *per ticker* (not identical).
  We persist each ticker's inception GBP allocation ONCE in state.json, based on portfolios.json weights + start_gbp.
  This gives a clean baseline without needing to store inception prices/FX.

Outputs:
- latest.json  : snapshot (portfolio totals + holdings breakdown + per-holding changes vs previous snapshot
                + per-holding total changes since start)
- state.json   : caches + A/B shares + last_run timestamp + inception allocations
- history.json : one row per UK date, updated each run for "today"

ADDED (2026-01):
- Portfolio C total logging into history.json (from portfolio_c.json)
- S&P 500 benchmark logging into history.json (from benchmarks.json)
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

# NEW:
PORTFOLIO_C_PATH = os.path.join(ROOT, "portfolio_c.json")
BENCHMARKS_PATH = os.path.join(ROOT, "benchmarks.json")

STOOQ_QUOTE = "https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv"

PORT_STATE_KEY = {
    "portfolio_A": "A",
    "portfolio_B": "B",
}

CACHE_TTL_SECONDS = 55 * 60  # 55 minutes
INCEPTION_ALLOC_KEY = "inception_allocations_gbp"


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
      {"name": "...", "weights": {...}}  (your current format)
      OR direct mapping numeric keys -> weights
    """
    if not isinstance(pdef, dict):
        return {}
    w = pdef.get("weights")
    if isinstance(w, dict) and w:
        return {str(k).strip(): float(v) for k, v in w.items() if _is_number(v)}
    out = {}
    for k, v in pdef.items():
        if str(k) in ("name", "key", "holdings"):
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
        pdef = portfolios_root.get(pkey)
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
            allocation_gbp = start_gbp * weight

            if ticker.upper() == "CASH":
                shares["CASH"] = shares.get("CASH", 0.0) + allocation_gbp
                continue

            price = price_for_holding(ticker, state)
            gbp_to_ccy = gbp_to_ccy_rate(ccy, state)
            allocation_ccy = allocation_gbp * gbp_to_ccy
            qty = allocation_ccy / price if price else 0.0
            shares[ticker] = qty

        state.setdefault(skey, {})["shares"] = shares


# -------------------------
# Inception allocations (ONE TIME)
# -------------------------
def ensure_inception_allocations(portfolios_root: dict, state: dict):
    start_gbp = portfolios_root.get("start_gbp")
    if start_gbp is None:
        return
    try:
        start_gbp = float(start_gbp)
    except Exception:
        return

    store = state.setdefault(INCEPTION_ALLOC_KEY, {})
    if not isinstance(store, dict):
        state[INCEPTION_ALLOC_KEY] = {}
        store = state[INCEPTION_ALLOC_KEY]

    for pkey in ("portfolio_A", "portfolio_B"):
        existing = store.get(pkey)
        if isinstance(existing, dict) and existing:
            continue  # don't overwrite

        pdef = portfolios_root.get(pkey)
        if not isinstance(pdef, dict):
            continue

        weights = extract_weights(pdef)
        if not weights:
            continue

        allocs = {}
        for ticker, weight in weights.items():
            if weight <= 0:
                continue
            allocs[str(ticker).strip()] = round(start_gbp * float(weight), 8)

        store[pkey] = allocs


def get_inception_alloc_for(portfolio_key: str, ticker: str, state: dict) -> float:
    store = state.get(INCEPTION_ALLOC_KEY, {})
    if not isinstance(store, dict):
        return 0.0
    p = store.get(portfolio_key, {})
    if not isinstance(p, dict):
        return 0.0

    if ticker in p:
        try:
            return float(p[ticker])
        except Exception:
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
# Valuation
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

            prev = prev_idx.get("CASH")
            prev_value = float(prev.get("value_gbp")) if isinstance(prev, dict) and prev.get("value_gbp") is not None else None

            change_value = (value_gbp - prev_value) if prev_value is not None else 0.0
            change_pct = (change_value / prev_value * 100.0) if (prev_value is not None and prev_value != 0) else 0.0

            change_value = clamp_neg_zero(round(change_value, 2))
            change_pct = clamp_neg_zero(round(change_pct, 4))

            total_change_gbp = (value_gbp - inception_alloc_gbp) if inception_alloc_gbp else 0.0
            total_change_pct = (total_change_gbp / inception_alloc_gbp * 100.0) if inception_alloc_gbp else 0.0
            total_change_gbp = clamp_neg_zero(round(total_change_gbp, 2))
            total_change_pct = clamp_neg_zero(round(total_change_pct, 4))

            holdings.append(
                {
                    "ticker": "CASH",
                    "qty": round(qty, 8),
                    "currency": ccy,
                    "price": 1.0,
                    "fx_to_gbp": round(fx, 8),
                    "value_gbp": round(value_gbp, 2),
                    "type": "cash",
                    "change_price_vs_prev": 0.0,
                    "change_value_gbp_vs_prev": change_value,
                    "change_pct_vs_prev": change_pct,
                    "price_direction": "flat",
                    "inception_value_gbp": round(inception_alloc_gbp, 2),
                    "total_change_gbp_since_start": total_change_gbp,
                    "total_change_pct_since_start": total_change_pct,
                    "total_direction": direction_from_delta(total_change_gbp),
                }
            )
            total_gbp += value_gbp
            continue

        price = price_for_holding(ticker, state)
        fx = fx_to_gbp_rate(ccy, state)
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
                "type": "asset",
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
# Benchmark helpers (NEW)
# -------------------------
def load_portfolio_c_total_gbp() -> float:
    c = load_json(PORTFOLIO_C_PATH, None)
    if not isinstance(c, dict):
        return None
    v = c.get("total_value_gbp")
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def load_sp500_total_gbp(state: dict) -> float:
    b = load_json(BENCHMARKS_PATH, None)
    if not isinstance(b, dict):
        return None
    sp = b.get("sp500")
    if not isinstance(sp, dict):
        return None
    ticker = sp.get("ticker")
    if not ticker:
        return None

    # Assume USD unless .UK etc; ^SPX is effectively USD
    ccy = infer_currency_from_ticker(ticker)
    if ticker.startswith("^"):
        ccy = "USD"

    price = price_for_holding(ticker, state)
    fx = fx_to_gbp_rate(ccy, state)

    # baseline is "£1m invested in benchmark at start" concept:
    # BUT for history plotting we just want a GBP series comparable to A/B/C.
    # simplest: store a notional £1m -> shares at baseline, BUT we don't have that here yet.
    #
    # For now, we store: benchmark value = £1m * (price_now / price_at_start).
    # We need start price persisted once in state.
    bench_state = state.setdefault("benchmarks", {})
    sp_state = bench_state.setdefault("sp500", {})

    start_price = sp_state.get("start_price")
    start_fx = sp_state.get("start_fx_to_gbp")

    if start_price is None or start_fx is None:
        # initialise baseline on first run
        sp_state["start_price"] = float(price)
        sp_state["start_fx_to_gbp"] = float(fx)
        sp_state["start_value_gbp"] = 1000000.0
        return 1000000.0

    try:
        start_price = float(start_price)
        start_fx = float(start_fx)
        start_value = float(sp_state.get("start_value_gbp", 1000000.0))
    except Exception:
        sp_state["start_price"] = float(price)
        sp_state["start_fx_to_gbp"] = float(fx)
        sp_state["start_value_gbp"] = 1000000.0
        return 1000000.0

    # Benchmark return in local terms, then adjust FX relative to start
    # This approximates: GBP value evolves with both index move + FX move
    ratio = (price * fx) / (start_price * start_fx) if (start_price * start_fx) else 1.0
    return float(start_value) * float(ratio)


# -------------------------
# History (daily totals)
# -------------------------
def upsert_daily_history(latest: dict, state: dict):
    today = uk_today_iso()
    history = load_json(HISTORY_PATH, [])
    if not isinstance(history, list):
        history = []

    today_row = {"date": today}

    # Existing: A/B totals from latest.json
    for key, pdata in latest.items():
        if isinstance(pdata, dict) and "total_value_gbp" in pdata:
            today_row[key] = round(float(pdata["total_value_gbp"]), 2)

    # NEW: Portfolio C total (from portfolio_c.json)
    c_total = load_portfolio_c_total_gbp()
    if c_total is not None:
        today_row["portfolio_C"] = round(float(c_total), 2)

    # NEW: S&P500 notional £1m benchmark
    sp_total = load_sp500_total_gbp(state)
    if sp_total is not None:
        today_row["benchmark_sp500"] = round(float(sp_total), 2)

    if len(today_row) <= 1:
        return

    for r in history:
        if isinstance(r, dict) and r.get("date") == today:
            for k, v in today_row.items():
                r[k] = v
            save_json(HISTORY_PATH, history)
            return

    history.append(today_row)
    save_json(HISTORY_PATH, history)


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

    for pkey, _ in PORT_STATE_KEY.items():
        pdef = portfolios_root.get(pkey, {})
        pname = pdef.get("name") if isinstance(pdef, dict) else None
        if not pname:
            pname = "Portfolio A" if pkey == "portfolio_A" else "Portfolio B"
        latest[pkey] = value_from_state_shares(pkey, pname, state, prev_latest)

    for pkey, pdata in latest.items():
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

    state["last_run_utc"] = latest["as_of_utc"]

    save_json(LATEST_PATH, latest)
    save_json(STATE_PATH, state)

    # UPDATED signature (passes state so benchmark baseline can be persisted)
    upsert_daily_history(latest, state)


if __name__ == "__main__":
    main()
