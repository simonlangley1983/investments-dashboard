#!/usr/bin/env python3
"""
update.py — Investment game updater (supports holdings OR weights portfolios)

Writes:
- latest.json  : current snapshot (portfolio totals + holdings)
- state.json   : caches (prices + fx) + last_run timestamp
- history.json : ONE row per UK day with portfolio total values

Portfolio input formats supported (portfolios.json):
A) Holdings-based (explicit quantities):
{
  "portfolio_A": {
    "name": "Portfolio A",
    "holdings": [{ "ticker":"CASH", "qty": 1000000, "currency":"GBP" }]
  }
}

B) Weights-based (fractional shares, no leftover):
{
  "start_gbp": 1000000,
  "portfolio_A": {
    "name": "Portfolio A",
    "weights": { "NVDA.US": 0.2, "MSFT.US": 0.1, ... }
  }
}

Notes:
- For weights-based portfolios, this script:
  - allocates start_gbp * weight in GBP,
  - converts to asset currency (USD for *.US by default),
  - buys fractional shares using latest close,
  - and writes derived holdings into latest.json.

- CASH holdings:
  - ticker "CASH" and qty in the holding currency.

Data source:
- Prices + FX are fetched from Stooq CSV endpoints.
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
    """
    Returns dict: {date, time, open, high, low, close, volume}
    Raises ValueError if data is missing.
    """
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
    Returns FX multiplier to convert 1 unit of from_ccy into GBP.
    value_gbp = value_in_from_ccy * fx_to_gbp_rate(from_ccy)

    Uses Stooq FX tickers:
      - gbpusd is USD per GBP, so USD->GBP = 1 / gbpusd
      - eurgbp is GBP per EUR, so EUR->GBP = eurgbp
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
        gbp_usd = float(q["close"])
        if gbp_usd <= 0:
            raise ValueError("Invalid GBPUSD rate from Stooq")
        usd_gbp = 1.0 / gbp_usd
        set_cached("USDGBP", usd_gbp)
        return usd_gbp

    if c == "EUR":
        cached = get_cached("EURGBP")
        if cached is not None:
            return cached
        q = stooq_quote("eurgbp")
        eur_gbp = float(q["close"])
        if eur_gbp <= 0:
            raise ValueError("Invalid EURGBP rate from Stooq")
        set_cached("EURGBP", eur_gbp)
        return eur_gbp

    raise ValueError(f"Unsupported currency for FX: {c} (add mapping in fx_to_gbp_rate)")


def gbp_to_ccy_rate(to_ccy: str, state_cache: dict) -> float:
    """
    Returns FX multiplier to convert 1 GBP into target currency.
    allocation_in_ccy = allocation_gbp * gbp_to_ccy_rate(to_ccy)
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

    if c == "EUR":
        cached = get_cached("GBPEUR")
        if cached is not None:
            return cached
        q = stooq_quote("eurgbp")
        eur_gbp = float(q["close"])  # GBP per EUR
        if eur_gbp <= 0:
            raise ValueError("Invalid EURGBP rate from Stooq")
        gbp_eur = 1.0 / eur_gbp
        set_cached("GBPEUR", gbp_eur)
        return gbp_eur

    raise ValueError(f"Unsupported currency for FX: {c} (add mapping in gbp_to_ccy_rate)")


# -------------------------
# Portfolio valuation
# -------------------------
def is_cash_ticker(ticker: str) -> bool:
    return (ticker or "").strip().upper() == "CASH"


def infer_currency_from_ticker(ticker: str) -> str:
    """
    Conservative currency inference:
    - *.US -> USD
    - CASH -> GBP (default)
    - otherwise -> GBP
    """
    t = (ticker or "").strip().upper()
    if t == "CASH":
        return "GBP"
    if t.endswith(".US"):
        return "USD"
    return "GBP"


def price_for_holding(ticker: str, state_cache: dict) -> float:
    """
    Fetch latest close from Stooq, with a simple state cache.
    """
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


def build_holdings_from_weights(weights: dict, start_gbp: float, state_cache: dict) -> list:
    """
    Converts weights into fractional share holdings with no leftover cash.
    Each ticker gets allocation_gbp = start_gbp * weight.
    Allocation is converted to asset currency, then divided by price to get qty.
    """
    out = []

    for ticker, w in (weights or {}).items():
        if ticker is None:
            continue
        try:
            weight = float(w)
        except Exception:
            continue
        if weight <= 0:
            continue

        ticker = str(ticker).strip()
        ccy = infer_currency_from_ticker(ticker)

        allocation_gbp = float(start_gbp) * weight

        if is_cash_ticker(ticker):
            # If someone includes CASH in weights, treat it as GBP cash allocation.
            fx = fx_to_gbp_rate("GBP", state_cache)
            value_gbp = allocation_gbp
            out.append(
                {
                    "ticker": "CASH",
                    "qty": round(allocation_gbp, 8),  # qty is cash amount in GBP
                    "currency": "GBP",
                    "price": 1.0,
                    "fx_to_gbp": round(fx, 8),
                    "value_gbp": round(value_gbp, 2),
                    "type": "cash",
                    "target_weight": round(weight, 6),
                }
            )
            continue

        price = price_for_holding(ticker, state_cache)

        # convert allocation from GBP -> asset currency
        gbp_to_ccy = gbp_to_ccy_rate(ccy, state_cache)
        allocation_ccy = allocation_gbp * gbp_to_ccy

        # fractional shares
        qty = allocation_ccy / price if price != 0 else 0.0

        # compute value in GBP for reporting
        fx_to_gbp = fx_to_gbp_rate(ccy, state_cache)
        value_gbp = (qty * price) * fx_to_gbp

        out.append(
            {
                "ticker": ticker,
                "qty": round(qty, 8),
                "currency": ccy,
                "price": round(price, 6),
                "fx_to_gbp": round(fx_to_gbp, 8),
                "value_gbp": round(value_gbp, 2),
                "type": "asset",
                "target_weight": round(weight, 6),
            }
        )

    return out


def value_portfolio(portfolio_key: str, portfolio_def: dict, portfolios_root: dict, state_cache: dict) -> dict:
    """
    Supports either:
    - explicit holdings list
    - weights dict + start_gbp at root
    """
    # weights-based
    if isinstance(portfolio_def.get("weights"), dict):
        start_gbp = portfolios_root.get("start_gbp")
        if start_gbp is None:
            raise ValueError("portfolios.json uses weights but missing top-level start_gbp")
        start_gbp = float(start_gbp)

        holdings = build_holdings_from_weights(portfolio_def.get("weights", {}), start_gbp, state_cache)

    # holdings-based
    else:
        holdings = portfolio_def.get("holdings", []) or []
        # normalize explicit holdings into the same internal shape as produced by weights
        norm = []
        for h in holdings:
            if not isinstance(h, dict):
                continue
            ticker = (h.get("ticker") or "").strip()
            if not ticker:
                continue
            try:
                qty = float(h.get("qty") or 0.0)
            except Exception:
                qty = 0.0
            if qty == 0:
                continue
            ccy = (h.get("currency") or infer_currency_from_ticker(ticker)).upper()
            norm.append({"ticker": ticker, "qty": qty, "currency": ccy})
        holdings = norm

        # enrich with prices + values
        enriched = []
        for h in holdings:
            ticker = h["ticker"]
            qty = float(h["qty"])
            ccy = h["currency"]

            if is_cash_ticker(ticker):
                fx = fx_to_gbp_rate(ccy, state_cache)
                value_gbp = qty * fx
                enriched.append(
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
            else:
                price = price_for_holding(ticker, state_cache)
                fx = fx_to_gbp_rate(ccy, state_cache)
                value_gbp = (qty * price) * fx
                enriched.append(
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

        holdings = enriched

    total_gbp = sum(float(h.get("value_gbp") or 0.0) for h in holdings)

    return {
        "key": portfolio_key,
        "name": portfolio_def.get("name") or portfolio_key,
        "holdings": holdings,
        "total_value_gbp": round(total_gbp, 2),
    }


# -------------------------
# History (daily totals)
# -------------------------
def append_daily_history(latest: dict):
    """
    Appends ONE row per UK day into history.json.

    If today's row already exists but contains 0.0 (or missing) for a portfolio
    while latest has a non-zero total, we patch today's row (safe repair).
    """
    today = uk_today_iso()
    history = load_json(HISTORY_PATH, [])
    if not isinstance(history, list):
        history = []

    # build a row from latest totals
    new_row = {"date": today}
    for key, pdata in latest.items():
        if isinstance(pdata, dict) and "total_value_gbp" in pdata:
            try:
                new_row[key] = round(float(pdata["total_value_gbp"]), 2)
            except Exception:
                pass

    if len(new_row) <= 1:
        return

    # find today's row if exists
    for r in history:
        if isinstance(r, dict) and r.get("date") == today:
            # patch only missing/zero values (don’t overwrite real data)
            changed = False
            for k, v in new_row.items():
                if k == "date":
                    continue
                try:
                    cur = float(r.get(k) or 0.0)
                except Exception:
                    cur = 0.0
                if (k not in r) or (cur == 0.0 and float(v) != 0.0):
                    r[k] = v
                    changed = True
            if changed:
                save_json(HISTORY_PATH, history)
            return

    # no today's row -> append
    history.append(new_row)
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

    latest = {
        "as_of_utc": utc_now_iso(),
        "as_of_uk_date": uk_today_iso(),
    }

    # Determine which keys are portfolios (ignore config keys like start_gbp)
    for pkey, pdef in portfolios_root.items():
        if pkey == "start_gbp":
            continue
        if not isinstance(pdef, dict):
            continue

        latest[pkey] = value_portfolio(pkey, pdef, portfolios_root, state)

    # Optional change vs previous snapshot
    for pkey, pdata in list(latest.items()):
        if not isinstance(pdata, dict) or "total_value_gbp" not in pdata:
            continue
        prev_total = prev_latest.get(pkey, {}).get("total_value_gbp")
        if prev_total is not None:
            try:
                pdata["change_gbp_vs_prev"] = round(float(pdata["total_value_gbp"]) - float(prev_total), 2)
            except Exception:
                pass
        else:
            pdata["change_gbp_vs_prev"] = 0.0

    state["last_run_utc"] = latest["as_of_utc"]

    save_json(LATEST_PATH, latest)
    save_json(STATE_PATH, state)

    append_daily_history(latest)


if __name__ == "__main__":
    main()
