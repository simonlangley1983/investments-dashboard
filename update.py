import json
import os
import time
from datetime import datetime, timezone, date
from urllib.request import urlopen, Request

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(ROOT, "state.json")
LATEST_PATH = os.path.join(ROOT, "latest.json")
PORTFOLIOS_PATH = os.path.join(ROOT, "portfolios.json")

STOOQ_QUOTE = "https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def http_get(url: str, timeout: int = 20) -> str:
    req = Request(url, headers={"User-Agent": "invest-game-bot"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def stooq_close(symbol: str) -> float:
    url = STOOQ_QUOTE.format(symbol=symbol.lower())
    csv = http_get(url)
    lines = [ln.strip() for ln in csv.strip().splitlines() if ln.strip()]
    if len(lines) < 2:
        raise RuntimeError(f"No quote lines for {symbol}")
    header = [h.strip() for h in lines[0].split(",")]
    row = [c.strip() for c in lines[1].split(",")]
    data = dict(zip(header, row))
    if "Close" not in data:
        raise RuntimeError(f"No Close for {symbol}")
    px = float(data["Close"])
    if px <= 0:
        raise RuntimeError(f"Bad Close for {symbol}: {px}")
    return px


def normalise_weights(w: dict) -> dict:
    s = sum(w.values())
    if s <= 0:
        raise RuntimeError("Weights sum to zero")
    return {k: v / s for k, v in w.items()}


def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, obj) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=False)


def init_shares(weights: dict, start_gbp: float, usd_to_gbp: float) -> dict:
    weights = normalise_weights(weights)
    shares = {}
    for ticker, wt in weights.items():
        alloc_gbp = start_gbp * wt
        px_usd = stooq_close(ticker)
        px_gbp = px_usd * usd_to_gbp
        shares[ticker] = alloc_gbp / px_gbp
    return shares


def value_with_deltas(shares: dict, usd_to_gbp: float, prev_values_by_ticker: dict):
    holdings = []
    total = 0.0
    for ticker, sh in shares.items():
        px_usd = stooq_close(ticker)
        value_gbp = sh * px_usd * usd_to_gbp

        prev = prev_values_by_ticker.get(ticker)
        delta_gbp = 0.0 if prev is None else (value_gbp - prev)
        delta_pct = 0.0 if (prev is None or prev == 0) else ((value_gbp / prev) - 1.0) * 100.0

        holdings.append(
            {
                "ticker": ticker,
                "value_gbp": round(value_gbp, 2),
                "delta_gbp": round(delta_gbp, 2),
                "delta_pct": round(delta_pct, 2),
            }
        )
        total += value_gbp

    holdings.sort(key=lambda x: x["value_gbp"], reverse=True)
    return round(total, 2), holdings


def main():
    # ==============================
    # Challenge start gate (HARD)
    # ==============================
    CHALLENGE_START = date(2026, 1, 1)
    today = date.today()

    if today < CHALLENGE_START:
        # Ensure nothing can "start early" even if state.json exists from prior tests
        if os.path.exists(STATE_PATH):
            os.remove(STATE_PATH)

        placeholder = {
            "as_of_utc": utc_now_iso(),
            "currency": "GBP",
            "status": "Not started",
            "starts_on": "2026-01-01",
            "portfolio_A": {"value_gbp": 1000000.00, "ytd_return_pct": 0.00, "holdings": []},
            "portfolio_B": {"value_gbp": 1000000.00, "ytd_return_pct": 0.00, "holdings": []},
        }
        save_json(LATEST_PATH, placeholder)
        print(json.dumps(placeholder, indent=2))
        return

    # ==============================
    # Live mode (from 2026-01-01)
    # ==============================
    portfolios = load_json(PORTFOLIOS_PATH, None)
    if not portfolios:
        raise RuntimeError("Missing portfolios.json")

    start_gbp = float(portfolios["start_gbp"])
    pA = portfolios["portfolio_A"]
    pB = portfolios["portfolio_B"]

    usd_to_gbp = stooq_close("USDGBP")

    prev_latest = load_json(LATEST_PATH, {})
    prevA = {h["ticker"]: float(h["value_gbp"]) for h in prev_latest.get("portfolio_A", {}).get("holdings", [])} if prev_latest else {}
    prevB = {h["ticker"]: float(h["value_gbp"]) for h in prev_latest.get("portfolio_B", {}).get("holdings", [])} if prev_latest else {}

    state = load_json(STATE_PATH, {})
    if "A" not in state or "B" not in state:
        state = {
            "created_utc": utc_now_iso(),
            "fx_usd_to_gbp_at_init": usd_to_gbp,
            "A": {"shares": init_shares(pA, start_gbp, usd_to_gbp)},
            "B": {"shares": init_shares(pB, start_gbp, usd_to_gbp)},
        }
        save_json(STATE_PATH, state)

    a_total, a_holdings = value_with_deltas(state["A"]["shares"], usd_to_gbp, prevA)
    b_total, b_holdings = value_with_deltas(state["B"]["shares"], usd_to_gbp, prevB)

    latest = {
        "as_of_utc": utc_now_iso(),
        "currency": "GBP",
        "fx_usd_to_gbp": round(usd_to_gbp, 6),
        "portfolio_A": {
            "value_gbp": a_total,
            "ytd_return_pct": round(((a_total / start_gbp) - 1.0) * 100.0, 2),
            "holdings": a_holdings,
        },
        "portfolio_B": {
            "value_gbp": b_total,
            "ytd_return_pct": round(((b_total / start_gbp) - 1.0) * 100.0, 2),
            "holdings": b_holdings,
        },
    }
    save_json(LATEST_PATH, latest)
    print(json.dumps(latest, indent=2))


if __name__ == "__main__":
    for i in range(3):
        try:
            main()
            break
        except Exception:
            if i == 2:
                raise
            time.sleep(3)
