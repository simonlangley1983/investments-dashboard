#!/usr/bin/env python3
"""
update.py — Investment game updater

- A/B: existing portfolios from portfolios.json (fixed shares)
- C: from portfolio_c.json (fixed shares, trackable proxy tickers)
- S&P500: benchmark via SPY.US (fixed shares)
- history.json: ALWAYS writes A/B/C/SP500 for today (so chart always has 4 lines)
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

PORTFOLIO_C_PATH = os.path.join(ROOT, "portfolio_c.json")
BENCHMARKS_PATH = os.path.join(ROOT, "benchmarks.json")

STOOQ_QUOTE = "https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv"

CACHE_TTL_SECONDS = 55 * 60  # 55 minutes
INCEPTION_ALLOC_KEY = "inception_allocations_gbp"

BASELINE_UK_DATE = "2026-01-01"
BASELINE_GBP = 1_000_000.0

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
        "close": to_float(row.get("Close")),
        "source": "stooq",
        "fetched_at": utc_now_iso(),
    }


# -------------------------
# Weights parsing
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
# FX + Pricing cache
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
        "updated_at": now,
        "source": "stooq",
        "quote_date": q.get("date"),
        "quote_time": q.get("time"),
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


def fx_to_gbp_rate(from_ccy: str, state_cache: dict) -> float:
    c = (from_ccy or "GBP").upper()
    if c == "GBP":
        return 1.0

    fx_cache = state_cache.setdefault("fx_cache", {})
    now = utc_now_iso()

    def get_cached(key: str):
        v = fx_cache.get(key)
        if isinstance(v, dict) and "rate" in v and "updated_at" in v and is_fresh(v["updated_at"], CACHE_TTL_SECONDS):
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
            raise ValueError("Invalid GBPUSD")
        usd_gbp = 1.0 / gbp_usd
        set_cached("USDGBP", usd_gbp)
        return usd_gbp

    raise ValueError(f"Unsupported FX currency: {c}")


def gbp_to_ccy_rate(to_ccy: str, state_cache: dict) -> float:
    c = (to_ccy or "GBP").upper()
    if c == "GBP":
        return 1.0

    fx_cache = state_cache.setdefault("fx_cache", {})
    now = utc_now_iso()

    def get_cached(key: str):
        v = fx_cache.get(key)
        if isinstance(v, dict) and "rate" in v and "updated_at" in v and is_fresh(v["updated_at"], CACHE_TTL_SECONDS):
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
            raise ValueError("Invalid GBPUSD")
        set_cached("GBPUSD", gbp_usd)
        return gbp_usd

    raise ValueError(f"Unsupported FX currency: {c}")


# -------------------------
# Initialise shares ONCE
# -------------------------
def init_portfolio_shares_once(portfolio_key: str, weights: dict, start_gbp: float, state: dict):
    skey = PORT_STATE_KEY[portfolio_key]
    existing = state.get(skey, {}).get("shares")
    if isinstance(existing, dict) and existing:
        return

    shares = {}
    for ticker, weight in weights.items():
        if float(weight) <= 0:
            continue

        ticker = str(ticker).strip()
        alloc_gbp = float(start_gbp) * float(weight)

        if ticker.upper() == "CASH":
            shares["CASH"] = shares.get("CASH", 0.0) + alloc_gbp
            continue

        ccy = infer_currency_from_ticker(ticker)
        price = price_for_holding(ticker, state)
        gbp_to_ccy = gbp_to_ccy_rate(ccy, state) if ccy != "GBP" else 1.0
        alloc_ccy = alloc_gbp * gbp_to_ccy
        qty = alloc_ccy / price if price else 0.0
        shares[ticker] = qty

    state.setdefault(skey, {})["shares"] = shares


# -------------------------
# Value portfolio from shares
# -------------------------
def value_from_state_shares(portfolio_key: str, portfolio_name: str, state: dict) -> dict:
    skey = PORT_STATE_KEY[portfolio_key]
    shares = state.get(skey, {}).get("shares", {})
    if not isinstance(shares, dict):
        shares = {}

    holdings = []
    total_gbp = 0.0

    for ticker, qty in shares.items():
        qty = float(qty)
        if qty == 0:
            continue

        ticker = str(ticker).strip()
        ccy = infer_currency_from_ticker(ticker)

        if ticker.upper() == "CASH":
            price = 1.0
            fx = 1.0
            value_gbp = qty
        else:
            price = price_for_holding(ticker, state)
            fx = fx_to_gbp_rate(ccy, state) if ccy != "GBP" else 1.0
            value_gbp = qty * price * fx

        holdings.append({
            "ticker": ticker,
            "qty": round(qty, 8),
            "currency": ccy,
            "price": round(float(price), 6),
            "fx_to_gbp": round(float(fx), 8),
            "value_gbp": round(float(value_gbp), 2),
            "type": "asset"
        })

        total_gbp += float(value_gbp)

    return {
        "key": portfolio_key,
        "name": portfolio_name,
        "holdings": holdings,
        "total_value_gbp": round(total_gbp, 2)
    }


# -------------------------
# Benchmark SP500 (SPY)
# -------------------------
def init_benchmark_sp500_once(state: dict):
    b = load_json(BENCHMARKS_PATH, {})
    sp = b.get("sp500") if isinstance(b, dict) else None
    if not isinstance(sp, dict):
        return None
    ticker = sp.get("ticker")
    if not ticker:
        return None

    bench = state.setdefault("benchmarks", {})
    sps = bench.setdefault("sp500", {})
    if sps.get("ticker") != ticker:
        sps.clear()
        sps["ticker"] = ticker

    if "qty" in sps and sps["qty"] is not None:
        return sps

    # buy £1m of SPY at first run (close enough for baseline)
    ccy = infer_currency_from_ticker(ticker)  # USD
    price0 = price_for_holding(ticker, state)
    gbp_to_usd0 = gbp_to_ccy_rate(ccy, state)
    alloc_usd = BASELINE_GBP * gbp_to_usd0
    qty = alloc_usd / price0 if price0 else 0.0

    sps["ticker"] = ticker
    sps["qty"] = float(qty)
    return sps


def sp500_value_gbp(state: dict):
    sps = init_benchmark_sp500_once(state)
    if not isinstance(sps, dict):
        return None
    ticker = sps.get("ticker")
    qty = sps.get("qty")
    if not ticker or qty is None:
        return None

    ccy = infer_currency_from_ticker(ticker)
    price = price_for_holding(ticker, state)
    fx = fx_to_gbp_rate(ccy, state)
    return float(qty) * float(price) * float(fx)


# -------------------------
# History write
# -------------------------
def load_history():
    h = load_json(HISTORY_PATH, [])
    return h if isinstance(h, list) else []


def save_history(history):
    history.sort(key=lambda r: r.get("date", ""))
    save_json(HISTORY_PATH, history)


def ensure_baseline_row(history):
    for r in history:
        if isinstance(r, dict) and r.get("date") == BASELINE_UK_DATE:
            r.setdefault("portfolio_A", BASELINE_GBP)
            r.setdefault("portfolio_B", BASELINE_GBP)
            r.setdefault("portfolio_C", BASELINE_GBP)
            r.setdefault("benchmark_sp500", BASELINE_GBP)
            return
    history.append({
        "date": BASELINE_UK_DATE,
        "portfolio_A": BASELINE_GBP,
        "portfolio_B": BASELINE_GBP,
        "portfolio_C": BASELINE_GBP,
        "benchmark_sp500": BASELINE_GBP
    })


def upsert_today(history, today_row):
    d = today_row["date"]
    for r in history:
        if isinstance(r, dict) and r.get("date") == d:
            r.update(today_row)
            return
    history.append(today_row)


def main():
    portfolios_root = load_json(PORTFOLIOS_PATH, {})
    if not isinstance(portfolios_root, dict) or not portfolios_root:
        raise SystemExit("portfolios.json missing or empty")

    state = load_json(STATE_PATH, {})
    if not isinstance(state, dict):
        state = {}

    # Load defs
    start_gbp = float(portfolios_root.get("start_gbp", BASELINE_GBP))

    pA = portfolios_root.get("portfolio_A", {})
    pB = portfolios_root.get("portfolio_B", {})
    pC = load_json(PORTFOLIO_C_PATH, {})

    wA = extract_weights(pA)
    wB = extract_weights(pB)
    wC = extract_weights(pC)

    # Init shares once
    init_portfolio_shares_once("portfolio_A", wA, start_gbp, state)
    init_portfolio_shares_once("portfolio_B", wB, start_gbp, state)
    init_portfolio_shares_once("portfolio_C", wC, float(pC.get("start_gbp", BASELINE_GBP)), state)

    # Value
    latest = {
        "as_of_utc": utc_now_iso(),
        "as_of_uk_date": uk_today_iso(),
        "portfolio_A": value_from_state_shares("portfolio_A", pA.get("name", "Portfolio A"), state),
        "portfolio_B": value_from_state_shares("portfolio_B", pB.get("name", "Portfolio B"), state),
        "portfolio_C": value_from_state_shares("portfolio_C", pC.get("name", "Portfolio C"), state),
    }

    sp_val = sp500_value_gbp(state)
    latest["benchmarks"] = {
        "sp500": {
            "name": load_json(BENCHMARKS_PATH, {}).get("sp500", {}).get("name", "S&P 500"),
            "ticker": load_json(BENCHMARKS_PATH, {}).get("sp500", {}).get("ticker", "SPY.US"),
            "total_value_gbp": round(float(sp_val), 2) if sp_val is not None else None
        }
    }

    save_json(LATEST_PATH, latest)
    state["last_run_utc"] = latest["as_of_utc"]
    save_json(STATE_PATH, state)

    # History today (all 4 series)
    history = load_history()
    ensure_baseline_row(history)

    a = latest["portfolio_A"]["total_value_gbp"]
    b = latest["portfolio_B"]["total_value_gbp"]
    c = latest["portfolio_C"]["total_value_gbp"]
    sp = latest["benchmarks"]["sp500"]["total_value_gbp"]

    if sp is None:
        # don't write partial rows
        save_history(history)
        return

    today_row = {
        "date": latest["as_of_uk_date"],
        "portfolio_A": round(float(a), 2),
        "portfolio_B": round(float(b), 2),
        "portfolio_C": round(float(c), 2),
        "benchmark_sp500": round(float(sp), 2)
    }
    upsert_today(history, today_row)
    save_history(history)


if __name__ == "__main__":
    main()
