#!/usr/bin/env python3
"""
backfill_history.py — ONE-TIME history builder

Pulls historical daily closes from Stooq and rebuilds history.json so:
- portfolio_A, portfolio_B, portfolio_C, benchmark_sp500 all have daily points
- all lines start from 2026-01-01
"""

import csv
import json
import os
from datetime import datetime, timezone
from urllib.request import Request, urlopen

ROOT = os.path.dirname(os.path.abspath(__file__))

STATE_PATH = os.path.join(ROOT, "state.json")
HISTORY_PATH = os.path.join(ROOT, "history.json")

BASELINE_UK_DATE = "2026-01-01"
BASELINE_GBP = 1_000_000.0

# Historical daily endpoint
STOOQ_DAILY = "https://stooq.com/q/d/l/?s={symbol}&i=d"


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


def http_get(url: str, timeout: int = 25) -> str:
    req = Request(url, headers={"User-Agent": "invest-game-bot"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def infer_ccy(ticker: str) -> str:
    t = (ticker or "").upper().strip()
    if t.endswith(".US"):
        return "USD"
    return "GBP"


def stooq_daily_close(symbol: str) -> dict:
    """
    Returns dict date_iso -> close
    """
    url = STOOQ_DAILY.format(symbol=symbol)
    text = http_get(url)
    reader = csv.DictReader(text.splitlines())
    out = {}
    for row in reader:
        d = row.get("Date")
        c = row.get("Close")
        if not d or not c or c == "N/D":
            continue
        try:
            out[d] = float(c)
        except Exception:
            pass
    return out


def build_date_index(*series_dicts):
    dates = set()
    for s in series_dicts:
        dates.update(s.keys())
    dates = sorted([d for d in dates if d >= BASELINE_UK_DATE])
    return dates


def main():
    state = load_json(STATE_PATH, {})
    if not isinstance(state, dict):
        raise SystemExit("state.json missing or invalid")

    # Shares
    A_shares = state.get("A", {}).get("shares", {}) or {}
    B_shares = state.get("B", {}).get("shares", {}) or {}
    C_shares = state.get("C", {}).get("shares", {}) or {}

    # Benchmark shares
    sp_qty = state.get("benchmarks", {}).get("sp500", {}).get("qty", None)
    sp_ticker = state.get("benchmarks", {}).get("sp500", {}).get("ticker", "SPY.US")

    if not A_shares or not B_shares:
        raise SystemExit("A/B shares missing in state.json (run update.py once first).")
    if not C_shares:
        raise SystemExit("C shares missing in state.json (run update.py once first).")
    if sp_qty is None:
        raise SystemExit("Benchmark SP500 shares missing in state.json (run update.py once first).")

    # Download all historical closes we need
    tickers = set([t for t in A_shares.keys()] + [t for t in B_shares.keys()] + [t for t in C_shares.keys()] + [sp_ticker])
    tickers = sorted([t for t in tickers if t.upper() != "CASH"])

    closes = {t: stooq_daily_close(t) for t in tickers}

    # FX: GBPUSD history for USD->GBP conversion
    # Stooq: gbpusd is USD per GBP, so USD->GBP = 1/(gbpusd close)
    gbpusd = stooq_daily_close("gbpusd")

    # Build date set across everything
    dates = build_date_index(*[closes[t] for t in tickers], gbpusd)

    def usd_to_gbp(d):
        r = gbpusd.get(d)
        if r is None or r == 0:
            return None
        return 1.0 / r

    def portfolio_value_for_date(shares: dict, d: str):
        total = 0.0
        # CASH assumed GBP cash
        cash = shares.get("CASH", 0.0)
        try:
            total += float(cash)
        except Exception:
            pass

        for t, q in shares.items():
            if t.upper() == "CASH":
                continue
            px = closes.get(t, {}).get(d)
            if px is None:
                return None  # missing price -> no point for that day
            try:
                q = float(q)
            except Exception:
                return None

            ccy = infer_ccy(t)
            if ccy == "USD":
                fx = usd_to_gbp(d)
                if fx is None:
                    return None
                total += q * px * fx
            else:
                total += q * px

        return total

    def sp500_value_for_date(d: str):
        px = closes.get(sp_ticker, {}).get(d)
        if px is None:
            return None
        fx = usd_to_gbp(d)  # SPY is USD
        if fx is None:
            return None
        return float(sp_qty) * float(px) * float(fx)

    history = []
    for d in dates:
        a = portfolio_value_for_date(A_shares, d)
        b = portfolio_value_for_date(B_shares, d)
        c = portfolio_value_for_date(C_shares, d)
        sp = sp500_value_for_date(d)

        # require all 4 series for a row (keeps chart aligned)
        if a is None or b is None or c is None or sp is None:
            continue

        history.append({
            "date": d,
            "portfolio_A": round(a, 2),
            "portfolio_B": round(b, 2),
            "portfolio_C": round(c, 2),
            "benchmark_sp500": round(sp, 2)
        })

    # Ensure baseline exists (even if markets closed that day)
    if not any(r.get("date") == BASELINE_UK_DATE for r in history):
        history.insert(0, {
            "date": BASELINE_UK_DATE,
            "portfolio_A": BASELINE_GBP,
            "portfolio_B": BASELINE_GBP,
            "portfolio_C": BASELINE_GBP,
            "benchmark_sp500": BASELINE_GBP
        })

    history.sort(key=lambda r: r.get("date", ""))
    save_json(HISTORY_PATH, history)
    print(f"Wrote {len(history)} rows to history.json")

if __name__ == "__main__":
    main()
