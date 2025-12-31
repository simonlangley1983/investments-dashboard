<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Investments</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 24px; }
    .wrap { max-width: 980px; margin: 0 auto; }

    .grid { display: grid; grid-template-columns: 1fr; gap: 16px; }
    @media (min-width: 720px) { .grid { grid-template-columns: 1fr 1fr; } }

    .card { border: 1px solid #ddd; border-radius: 14px; padding: 16px; }
    .h { font-size: 14px; opacity: .7; margin-bottom: 8px; }
    .v { font-size: 34px; font-weight: 750; margin: 4px 0 8px; }

    .note { margin: 18px 0; padding: 12px 14px; border-radius: 12px; background: #f6f6f6; font-size: 14px; }
    .meta { margin-top: 18px; font-size: 13px; opacity: .7; }
    .err { color: #b00020; }

    table { width: 100%; border-collapse: collapse; margin-top: 10px; }
    td, th { padding: 6px 4px; border-top: 1px solid #eee; font-size: 13px; }
    th { opacity: .7; font-weight: 600; text-align: left; }
    .num { text-align: right; font-variant-numeric: tabular-nums; }
    .small { font-size: 12px; opacity: .7; margin-top: 8px; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="grid">
      <div class="card">
        <div class="h">Portfolio A — High-Conviction Growth</div>
        <div class="v" id="aValue">—</div>
        <div>YTD: <strong id="aYtd">—</strong></div>
        <div id="aHoldings"></div>
      </div>

      <div class="card">
        <div class="h">Portfolio B — Maximum Risk</div>
        <div class="v" id="bValue">—</div>
        <div>YTD: <strong id="bYtd">—</strong></div>
        <div id="bHoldings"></div>
      </div>
    </div>

    <div class="note" id="note" style="display:none;"></div>
    <div class="meta" id="asOf"></div>
    <div class="meta err" id="err"></div>
  </div>

  <script>
    const FEED =
      "https://raw.githubusercontent.com/simonlangley1983/investments-dashboard/main/latest.json";

    const fmtGBP  = (n) => new Intl.NumberFormat("en-GB", { style: "currency", currency: "GBP" }).format(n);
    const fmtGBP0 = (n) => new Intl.NumberFormat("en-GB", { style: "currency", currency: "GBP", maximumFractionDigits: 0 }).format(n);
    const fmtPct  = (n) => `${Number(n).toFixed(2)}%`;
    const sign    = (n) => (n > 0 ? "+" : "");

    function tablePreStart(holdings) {
      if (!Array.isArray(holdings) || holdings.length === 0) return "";
      const rows = holdings.map(h => `
        <tr>
          <td>${h.ticker}</td>
          <td class="num">${Number(h.target_weight_pct).toFixed(2)}%</td>
        </tr>
      `).join("");
      return `
        <table>
          <thead>
            <tr>
              <th>Fund</th>
              <th class="num">Target weight</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      `;
    }

    function tableLive(holdings) {
      if (!Array.isArray(holdings) || holdings.length === 0) return "";
      const rows = holdings.map(h => `
        <tr>
          <td>${h.ticker}</td>
          <td class="num">${fmtGBP0(h.value_gbp)}</td>
          <td class="num">${sign(h.delta_gbp)}${fmtGBP0(h.delta_gbp)}</td>
          <td class="num">${sign(h.delta_pct)}${Number(h.delta_pct).toFixed(2)}%</td>
        </tr>
      `).join("");
      return `
        <table>
          <thead>
            <tr>
              <th>Fund</th>
              <th class="num">Value</th>
              <th class="num">Δ £</th>
              <th class="num">Δ %</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
        <div class="small">Δ is since the previous update.</div>
      `;
    }

    async function load() {
      try {
        const res = await fetch(FEED + "?ts=" + Date.now(), { cache: "no-store" });
        if (!res.ok) throw new Error("HTTP " + res.status);
        const d = await res.json();

        document.getElementById("aValue").textContent = fmtGBP(d.portfolio_A.value_gbp);
        document.getElementById("bValue").textContent = fmtGBP(d.portfolio_B.value_gbp);
        document.getElementById("aYtd").textContent = fmtPct(d.portfolio_A.ytd_return_pct);
        document.getElementById("bYtd").textContent = fmtPct(d.portfolio_B.ytd_return_pct);

        const note = document.getElementById("note");
        if (d.status === "Not started") {
          note.style.display = "block";
          note.textContent = `Challenge starts on ${d.starts_on}. Both portfolios are fixed at £1,000,000 until then.`;

          // show constituent funds + target weights
          document.getElementById("aHoldings").innerHTML = tablePreStart(d.portfolio_A.holdings);
          document.getElementById("bHoldings").innerHTML = tablePreStart(d.portfolio_B.holdings);
        } else {
          note.style.display = "none";

          // show live holdings breakdown
          document.getElementById("aHoldings").innerHTML = tableLive(d.portfolio_A.holdings);
          document.getElementById("bHoldings").innerHTML = tableLive(d.portfolio_B.holdings);
        }

        document.getElementById("asOf").textContent = `As of (UTC): ${d.as_of_utc}`;
        document.getElementById("err").textContent = "";
      } catch (e) {
        document.getElementById("err").textContent = "Couldn’t load feed: " + e.message;
      }
    }

    load();
    setInterval(load, 60_000);
  </script>
</body>
</html>
