#!/usr/bin/env python3
"""
update.py — Investment game updater (RUNNING TOTAL)

Key requirement:
- Hold FIXED quantities (shares) and revalue over time.
- Record daily totals in history.json.

This script uses state.json as the source of truth for quantities:
- Portfolio A shares live at state["A"]["shares"]
- Portfolio B shares live at state["B"]["shares"]

Weights in portfolios.json are ONLY used to initialise shares ONCE
(if state shares don't already exist).

Outputs:
- latest.json  : snapshot (portfolio totals + holdings breakdown)
- state.json   : caches + A/B shares + last_run timestamp
- history.json : one row per UK date, updated each run for "today"
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

STOOQ_QUOTE = "https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv"

# map portfolio keys -> state keys (matches your existing state.json)
PORT_STATE_KEY = {
    "portfolio_A": "A",
    "portfolio_B": "B",
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
# FX conversion
# -------------------------
def fx_to_gbp_rate(from_ccy: str, state_cache: dict) -> float:
    """
    Returns multiplier to convert 1 unit of from_ccy into GBP.
    """
    c = (from_ccy or "GBP").upper()
    if c == "GBP":
        return 1.0

    fx_cache = state_cache.setdefault("fx_cache", {})
    now = utc_now_iso()

    def get_cached(key: str):
        v = fx_cache.get(key)
        if isinstance(v, dict) and "rate" in v:
            try:
                return float(v["rate"])
            except Exception:
                return None
        return None

    def set_cached(key: str, rate: float):
        fx_cache[key] = {"rate": rate, "updated_at": now}

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
    """
    Returns multiplier to convert 1 GBP into target currency.
    """
    c = (to_ccy or "GBP").upper()
    if c == "GBP":
        return 1.0

    fx_cache = state_cache.setdefault("fx_cache", {})
    now = utc_now_iso()

    def get_cached(key: str):
        v = fx_cache.get(key)
        if isinstance(v, dict) and "rate" in v:
            try:
                return float(v["rate"])
            except Exception:
                return None
        return None

    def set_cached(key: str, rate: float):
        fx_cache[key] = {"rate": rate, "updated_at": now}

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
# Pricing
# -------------------------
def price_for_holding(ticker: str, state_cache: dict) -> float:
    px_cache = state_cache.setdefault("price_cache", {})
    now = utc_now_iso()

    cached = px_cache.get(ticker)
    if isinstance(cached, dict) and "price" in cached:
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
    """
    If state["A"]["shares"] / state["B"]["shares"] missing or empty,
    initialise them from portfolios.json weights using current prices and FX.
    """
    start_gbp = portfolios_root.get("start_gbp")
    if start_gbp is None:
        return

    start_gbp = float(start_gbp)

    for pkey, skey in PORT_STATE_KEY.items():
        pdef = portfolios_root.get(pkey)
        if not isinstance(pdef, dict):
            continue
        weights = pdef.get("weights")
        if not isinstance(weights, dict) or not weights:
            continue

        existing = state.get(skey, {}).get("shares")
        if isinstance(existing, dict) and len(existing) > 0:
            # already initialised — do nothing
            continue

        shares = {}
        for ticker, w in weights.items():
            try:
                weight = float(w)
            except Exception:
                continue
            if weight <= 0:
                continue

            ticker = str(ticker).strip()
            ccy = infer_currency_from_ticker(ticker)

            allocation_gbp = start_gbp * weight
            if ticker.upper() == "CASH":
                # store cash as GBP amount (optional)
                shares["CASH"] = shares.get("CASH", 0.0) + allocation_gbp
                continue

            price = price_for_holding(ticker, state)
            gbp_to_ccy = gbp_to_ccy_rate(ccy, state)
            allocation_ccy = allocation_gbp * gbp_to_ccy

            qty = allocation_ccy / price if price else 0.0
            shares[ticker] = qty

        state.setdefault(skey, {})["shares"] = shares


# -------------------------
# Valuation (RUNNING TOTAL)
# -------------------------
def value_from_state_shares(portfolio_key: str, portfolio_name: str, state: dict) -> dict:
    """
    Build holdings + total value using fixed quantities stored in state.
    """
    skey = PORT_STATE_KEY[portfolio_key]
    shares = state.get(skey, {}).get("shares", {})
    if not isinstance(shares, dict):
        shares = {}

    holdings = []
    total_gbp = 0.0

    for ticker, qty in shares.items():
        try:
            qty = float(qty)
        except Exception:
            continue
        if qty == 0.0:
            continue

        ticker = str(ticker).strip()
        ccy = infer_currency_from_ticker(ticker)

        if ticker.upper() == "CASH":
            fx = fx_to_gbp_rate(ccy, state)
            value_gbp = qty * fx
            holdings.append(
                {
                    "ticker": "CASH",
                    "qty": round(qty, 8),
                    "currency": ccy,
                    "price": 1.0,
                    "fx_to_gbp": round(fx, 8),
                    "value_gbp": round(value_gbp, 2),
                    "type": "cash",
                }
            )
            total_gbp += value_gbp
            continue

        price = price_for_holding(ticker, state)
        fx = fx_to_gbp_rate(ccy, state)
        value_gbp = (qty * price) * fx

        holdings.append(
            {
                "ticker": ticker,
                "qty": round(qty, 8),
                "currency": ccy,
                "price": round(price, 6),
                "fx_to_gbp": round(fx, 8),
                "value_gbp": round(value_gbp, 2),
                "type": "asset",
            }
        )
        total_gbp += value_gbp

    return {
        "key": portfolio_key,
        "name": portfolio_name,
        "holdings": holdings,
        "total_value_gbp": round(total_gbp, 2),
    }


# -------------------------
# History (daily totals) — update today's row each run
# -------------------------
def upsert_daily_history(latest: dict):
    today = uk_today_iso()
    history = load_json(HISTORY_PATH, [])
    if not isinstance(history, list):
        history = []

    today_row = {"date": today}
    for key, pdata in latest.items():
        if isinstance(pdata, dict) and "total_value_gbp" in pdata:
            today_row[key] = round(float(pdata["total_value_gbp"]), 2)

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

    # ONE-TIME initialisation: only if A/B shares missing
    init_state_shares_from_weights(portfolios_root, state)

    latest = {
        "as_of_utc": utc_now_iso(),
        "as_of_uk_date": uk_today_iso(),
    }

    # value portfolios using FIXED quantities from state
    for pkey, skey in PORT_STATE_KEY.items():
        pdef = portfolios_root.get(pkey, {})
        pname = pdef.get("name") if isinstance(pdef, dict) else None
        if not pname:
            # sensible defaults
            pname = "Portfolio A" if pkey == "portfolio_A" else "Portfolio B"

        latest[pkey] = value_from_state_shares(pkey, pname, state)

    # change vs previous snapshot
    for pkey, pdata in latest.items():
        if not isinstance(pdata, dict) or "total_value_gbp" not in pdata:
            continue
        prev_total = prev_latest.get(pkey, {}).get("total_value_gbp")
        if prev_total is not None:
            try:
                pdata["change_gbp_vs_prev"] = round(float(pdata["total_value_gbp"]) - float(prev_total), 2)
            except Exception:
                pdata["change_gbp_vs_prev"] = 0.0
        else:
            pdata["change_gbp_vs_prev"] = 0.0

    state["last_run_utc"] = latest["as_of_utc"]

    save_json(LATEST_PATH, latest)
    save_json(STATE_PATH, state)

    upsert_daily_history(latest)


if __name__ == "__main__":
    main()
