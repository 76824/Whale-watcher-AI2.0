(function () {
  const backendBase = "https://whale-watcher-ai2-0.onrender.com";
  const POLL_MS = 10000;
  const els = {
    status: document.getElementById("status-text"),
    updatedAt: document.getElementById("updated-at"),
    refreshBtn: document.getElementById("refresh-btn"),
    krakenList: document.getElementById("kraken-list"),
    krakenCount: document.getElementById("kraken-count"),
    whaleTable: document.getElementById("whale-table").querySelector("tbody"),
    whaleMeta: document.getElementById("whale-meta"),
    metricsPre: document.getElementById("metrics-pre"),
  };
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    if (attrs) for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else el.setAttribute(k, v);
    }
    for (const kid of kids) { if (kid == null) continue; el.appendChild(typeof kid === "string" ? document.createTextNode(kid) : kid); }
    return el;
  }
  function renderSymbols(data) {
    const list = els.krakenList; list.innerHTML = "";
    const items = Array.isArray(data) ? data : (data && data.data ? data.data : []);
    const symbols = [];
    for (const row of items) {
      if (!row || !row.symbol) continue;
      symbols.push(row.symbol);
      const chip = h("div", { class: "chip" },
        h("span", { class: "sym", text: row.symbol }),
        h("span", { text: String(row.price ?? "—") }),
        h("small", { text: row.price_venue ? `  @ ${row.price_venue}` : "" })
      );
      list.appendChild(chip);
    }
    els.krakenCount.textContent = String(symbols.length);
  }
  function formatUsd(v) {
    if (v == null || isNaN(v)) return "—";
    if (v >= 1e9) return (v/1e9).toFixed(2) + "B";
    if (v >= 1e6) return (v/1e6).toFixed(2) + "M";
    if (v >= 1e3) return (v/1e3).toFixed(2) + "K";
    return Number(v).toFixed(0);
  }
  function renderWhales(data) {
    const tbody = els.whaleTable; tbody.innerHTML = "";
    const items = Array.isArray(data) ? data : (data && data.data ? data.data : []);
    const rows = [];
    for (const row of items) {
      if (!row || !row.symbol || !row.whales) continue;
      const sym = row.symbol;
      for (const b of (row.whales.bids || []).slice(0, 5)) rows.push([sym, "BID", b.price, b.qty, b.usd]);
      for (const a of (row.whales.asks || []).slice(0, 5)) rows.push([sym, "ASK", a.price, a.qty, a.usd]);
    }
    rows.sort((A, B) => (B[4]||0) - (A[4]||0));
    const top = rows.slice(0, 20);
    for (const [sym, side, p, q, u] of top) {
      const tr = h("tr", null,
        h("td", { text: sym }),
        h("td", { text: side }),
        h("td", { text: String(p) }),
        h("td", { text: String(q) }),
        h("td", { text: formatUsd(u) })
      ); tbody.appendChild(tr);
    }
    els.whaleMeta.textContent = top.length ? `${top.length} levels` : "no levels";
  }
  async function fetchSignal() {
    const url = backendBase.replace(/\/$/, "") + "/signal";
    try {
      const res = await fetch(url, { mode: "cors", credentials: "omit" });
      if (!res.ok) throw new Error(res.status + " " + res.statusText);
      const data = await res.json();
      const payload = data && (Array.isArray(data) ? data : data.data) || [];
      const nowStr = new Date().toLocaleString();
      els.status.textContent = "✅ Live — " + nowStr;
      els.updatedAt.textContent = nowStr;
      renderSymbols(payload); renderWhales(payload);
      els.metricsPre.textContent = JSON.stringify(data, null, 2);
    } catch (err) {
      els.status.textContent = "❌ " + (err && err.message ? err.message : String(err));
    }
  }
  els.refreshBtn.addEventListener("click", fetchSignal);
  setInterval(fetchSignal, POLL_MS);
  fetchSignal();
})();
