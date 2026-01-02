#!/usr/bin/env python3
"""
update.py — Investment game updater

What this script does
- Loads portfolio definitions from portfolios.json
- Fetches latest prices from Stooq (free CSV endpoint)
- Converts values into GBP using simple FX rates (also from Stooq)
- Writes:
    - latest.json  (current snapshot)
    - state.json   (cache + metadata)
    - history.json (ONE row per UK day with end-of-day totals, if you schedule it that way)

Expected portfolios.json format (example)
{
  "portfolio_A": {
    "name": "Portfolio A",
    "holdings": [
      {"ticker": "aapl.us", "qty": 10, "currency": "USD"},
      {"ticker": "barc.uk", "qty": 500, "currency": "GBP"},
      {"ticker": "CASH", "qty": 25000, "currency": "GBP"}
    ]
  },
  "portfolio_B": { ... },
  "portfolio_C": { ... }
}

Notes
- For CASH holdings, use ticker "CASH" (any case) and qty as amount in that currency.
- Stooq tickers typically look like:
    - US: aapl.us
    - UK: barc.uk
    - ETFs etc vary; you already have these working in your current setup.
"""

import csv
import json
import os
from datetime import datetime, timezone
from urllib.request import Request, urlopen

try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:
    ZoneInfo = None


ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(ROOT, "state.json")
LATEST_PATH = os.path.join(ROOT, "latest.json")
HISTORY_PATH = os.path.join(ROOT, "history.json")
PORTFOLIOS_PATH = os.path.join(ROOT, "portfolios.json")

STOOQ_QUOTE = "https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv"


# -------------------------
# Time helpers
# -------------------------
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def uk_today_iso() -> str:
    # Use real UK local date if available; otherwise fall back to UTC date.
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
    """
    Returns dict: {date, time, open, high, low, close, volume}
    Raises ValueError if data is missing.
    """
    url = STOOQ_QUOTE.format(symbol=symbol)
    text = http_get(url)

    # Stooq returns CSV with header row.
    reader = csv.DictReader(text.splitlines())
    rows = list(reader)
    if not rows:
        raise ValueError(f"No quote rows returned for {symbol}")

    row = rows[0]
    # Stooq sometimes returns "N/D" for non-trading / invalid
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
    Returns FX multiplier to convert 1 unit of from_ccy into GBP.
    E.g. value_gbp = value_in_from_ccy * fx_to_gbp_rate(from_ccy)

    Uses Stooq FX tickers:
      - GBPUSD (gbpusd) is "USD per GBP", so USD->GBP = 1 / (GBPUSD)
      - EURGBP (eurgbp) is "GBP per EUR", so EUR->GBP = EURGBP
      - GBPJPY (gbpjpy) etc (not currently implemented)

    If from_ccy == GBP => 1.0
    """
    c = (from_ccy or "GBP").upper()
    if c == "GBP":
        return 1.0

    fx_cache = state_cache.setdefault("fx_cache", {})
    now = utc_now_iso()

    def get_cached(key: str):
        v = fx_cache.get(key)
        if isinstance(v, dict) and "rate" in v:
            return float(v["rate"])
        return None

    def set_cached(key: str, rate: float):
        fx_cache[key] = {"rate": rate, "updated_at": now}

    # USD -> GBP via gbpusd (USD per GBP)
    if c == "USD":
        cached = get_cached("USDGBP")
        if cached is not None:
            return cached
        q = stooq_quote("gbpusd")
        gbp_usd = float(q["close"])
        if gbp_usd <= 0:
            raise ValueError("Invalid GBPUSD rate")
        usd_gbp = 1.0 / gbp_usd
        set_cached("USDGBP", usd_gbp)
        return usd_gbp

    # EUR -> GBP via eurgbp (GBP per EUR)
    if c == "EUR":
        cached = get_cached("EURGBP")
        if cached is not None:
            return cached
        q = stooq_quote("eurgbp")
        eur_gbp = float(q["close"])
        if eur_gbp <= 0:
            raise ValueError("Invalid EURGBP rate")
        set_cached("EURGBP", eur_gbp)
        return eur_gbp

    # Add more if you need them
    raise ValueError(f"Unsupported currency for FX: {c} (add mapping in fx_to_gbp_rate)")


# -------------------------
# Portfolio valuation
# -------------------------
def is_cash_ticker(ticker: str) -> bool:
    return (ticker or "").strip().upper() in ("CASH", "GBP", "USD", "EUR")


def price_for_holding(ticker: str, state_cache: dict) -> float:
    """
    Fetches latest close from Stooq, with a simple state cache.
    """
    px_cache = state_cache.setdefault("price_cache", {})
    now = utc_now_iso()

    cached = px_cache.get(ticker)
    if isinstance(cached, dict) and "price" in cached:
        # We intentionally do NOT enforce freshness here; your scheduler controls that.
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


def value_portfolio(portfolio_key: str, portfolio_def: dict, state_cache: dict) -> dict:
    """
    Returns:
      {
        "key": "portfolio_A",
        "name": "...",
        "holdings": [...],
        "total_value_gbp": 123.45
      }
    """
    holdings = portfolio_def.get("holdings", []) or []
    out_holdings = []
    total_gbp = 0.0

    for h in holdings:
        ticker = (h.get("ticker") or "").strip()
        qty = float(h.get("qty") or 0.0)
        ccy = (h.get("currency") or "GBP").upper()

        if qty == 0:
            continue

        if is_cash_ticker(ticker):
            # For cash, qty is already a currency amount.
            fx = fx_to_gbp_rate(ccy, state_cache)
            value_gbp = qty * fx
            out_holdings.append(
                {
                    "ticker": ticker or "CASH",
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

        price = price_for_holding(ticker, state_cache)
        fx = fx_to_gbp_rate(ccy, state_cache)
        value_ccy = qty * price
        value_gbp = value_ccy * fx

        out_holdings.append(
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
        "name": portfolio_def.get("name") or portfolio_key,
        "holdings": out_holdings,
        "total_value_gbp": round(total_gbp, 2),
    }


# -------------------------
# History (daily totals)
# -------------------------
def append_daily_history(latest: dict):
    """
    Appends ONE row per UK day into history.json:
      { date, portfolio_A, portfolio_B, portfolio_C }
    Skips if today's row already exists.
    """
    today = uk_today_iso()
    history = load_json(HISTORY_PATH, [])

    if history and history[-1].get("date") == today:
        return

    row = {"date": today}

    for key in ("portfolio_A", "portfolio_B", "portfolio_C"):
        total = latest.get(key, {}).get("total_value_gbp")
        if total is not None:
            row[key] = round(float(total), 2)

    if len(row) > 1:
        history.append(row)
        save_json(HISTORY_PATH, history)


# -------------------------
# Main
# -------------------------
def main():
    portfolios = load_json(PORTFOLIOS_PATH, {})
    if not isinstance(portfolios, dict) or not portfolios:
        raise SystemExit("portfolios.json missing or empty")

    state = load_json(STATE_PATH, {})
    if not isinstance(state, dict):
        state = {}

    prev_latest = load_json(LATEST_PATH, {}) if os.path.exists(LATEST_PATH) else {}
    if not isinstance(prev_latest, dict):
        prev_latest = {}

    latest = {
        "as_of_utc": utc_now_iso(),
        "as_of_uk_date": uk_today_iso(),
    }

    # Value each portfolio present in portfolios.json (but we’ll still write keys A/B/C if present)
    for pkey, pdef in portfolios.items():
        if not isinstance(pdef, dict):
            continue
        latest[pkey] = value_portfolio(pkey, pdef, state)

    # Optional: daily change vs previous snapshot (if available)
    for pkey, pdata in list(latest.items()):
        if not isinstance(pdata, dict) or "total_value_gbp" not in pdata:
            continue
        prev_total = None
        try:
            prev_total = prev_latest.get(pkey, {}).get("total_value_gbp")
        except Exception:
            prev_total = None
        if prev_total is not None:
            try:
                pdata["change_gbp_vs_prev"] = round(float(pdata["total_value_gbp"]) - float(prev_total), 2)
            except Exception:
                pass

    # Update state metadata
    state["last_run_utc"] = latest["as_of_utc"]

    # Write outputs
    save_json(LATEST_PATH, latest)
    save_json(STATE_PATH, state)

    # Append ONE daily history row (schedule your daily run after markets close for "end-of-day")
    append_daily_history(latest)


if __name__ == "__main__":
    main()
