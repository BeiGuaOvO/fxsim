(() => {
  "use strict";

  const PAIRS = ["USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "CHFJPY"];
  const FALLBACK = { leverages: [10, 50, 100, 200, 500], min_amount: 1000, stopout: 50, warn: 130 };
  const REASON = { manual: "手动", stopout: "强平", bust: "爆仓" };

  const $ = (sel, root = document) => root.querySelector(sel);

  const token = localStorage.getItem("fx_token") || "";
  let pair = "USDJPY";
  let lev = Number(localStorage.getItem("fx_real_lev")) || 10;
  let points = [];
  let pending = false;
  let refreshing = false;
  let historySeq = 0;

  const state = {
    prices: {}, quotes: {}, account: null, positions: [],
    config: { ...FALLBACK }, market: {}, trades: [], stats: null, tradeTotal: 0,
  };

  /* ---------------- 工具 ---------------- */

  const esc = (s) =>
    String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));

  const money = (n) =>
    n == null || Number.isNaN(n) ? "—" : Math.round(n).toLocaleString("zh-CN");

  const signed = (n) => `${n >= 0 ? "+" : "-"}¥${money(Math.abs(n))}`;

  const price = (n) => (n == null ? "—" : Number(n).toFixed(3));

  const label = (p) => `${p.slice(0, 3)}/${p.slice(3)}`;

  const timeText = (ts) => {
    if (!ts) return "—";
    const d = new Date(Number(ts) * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  };

  const clockText = (ts) => {
    if (!ts) return "—";
    const d = new Date(Number(ts) * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getHours())}:${p(d.getMinutes())}`;
  };

  function detailText(detail) {
    if (!detail) return "请求失败";
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map((x) => x.msg || "参数错误").join("；");
    return detail.msg || "请求失败";
  }

  function toast(text) {
    const box = $("#realToasts");
    if (!box) return;
    const el = document.createElement("div");
    el.className = "toast";
    el.textContent = text;
    box.appendChild(el);
    while (box.children.length > 5) box.firstElementChild.remove();
    setTimeout(() => el.remove(), 4200);
  }

  async function api(path, opts = {}) {
    const headers = Object.assign({}, opts.headers, { "Content-Type": "application/json" });
    if (token) headers.Authorization = `Bearer ${token}`;
    const res = await fetch(path, Object.assign({}, opts, { headers }));
    let body = null;
    try { body = await res.json(); } catch (e) { /* 空响应 */ }
    if (!res.ok) {
      const err = new Error(detailText(body && body.detail));
      err.status = res.status;
      throw err;
    }
    return body;
  }

  function logout() {
    localStorage.removeItem("fx_token");
    location.replace("/");
  }

  /* ---------------- 状态 ---------------- */

  function applyState(msg) {
    if (msg.prices) state.prices = msg.prices;
    if (msg.quotes) state.quotes = msg.quotes;
    if (msg.account) state.account = msg.account;
    if (msg.positions) state.positions = msg.positions;
    if (msg.market) state.market = msg.market;
    if (msg.trades) state.trades = msg.trades;
    if (msg.stats) state.stats = msg.stats;
    if (msg.trade_total != null) state.tradeTotal = msg.trade_total;
    if (msg.config) {
      state.config = msg.config;
      const levels = state.config.leverages || FALLBACK.leverages;
      if (!levels.includes(lev)) {
        lev = levels[0];
        localStorage.setItem("fx_real_lev", String(lev));
      }
    }
    if (Array.isArray(msg.alerts)) msg.alerts.forEach(toast);
  }

  async function refreshState() {
    if (refreshing || document.hidden) return;
    refreshing = true;
    try {
      applyState(await api("/api/real/state"));
      renderAll();
    } catch (e) {
      if (e.status === 401) return logout();
      toast(e.message);
    } finally {
      refreshing = false;
    }
  }

  async function loadHistory() {
    const seq = ++historySeq;
    try {
      const data = await api(`/api/real/history?pair=${encodeURIComponent(pair)}&limit=400`);
      if (seq !== historySeq) return;
      points = Array.isArray(data.points) ? data.points : [];
      drawChart();
    } catch (e) {
      if (seq !== historySeq) return;
      if (e.status === 401) return logout();
      points = [];
      drawChart();
    }
  }

  /* ---------------- 渲染 ---------------- */

  function renderAll() {
    renderPairs();
    renderQuote();
    renderAccount();
    renderLeverage();
    renderMarket();
    renderRiskBar();
    renderPositions();
    renderTrades();
    updateOrderPanel();
    updateTradingEnabled();
    drawChart();
  }

  function renderPairs() {
    $("#realPairs").innerHTML = PAIRS.map((p) => {
      const v = state.prices[p];
      return `
        <button type="button"
                class="pair-chip${p === pair ? " is-active" : ""}"
                data-real-pair="${p}">
          <span class="pair-code">${label(p)}</span>
          <span class="pair-price num">${v == null ? "—" : price(v)}</span>
        </button>`;
    }).join("");
  }

  function renderQuote() {
    const v = state.prices[pair];
    const held = state.positions.filter((p) => p.pair === pair).length;
    const vals = points.map((p) => Number(p[1])).filter(Number.isFinite);
    const hi = vals.length ? Math.max(...vals) : null;
    const lo = vals.length ? Math.min(...vals) : null;
    const q = state.quotes[pair] || {};
    $("#realQuote").innerHTML = `
      <div><dt>现价</dt><dd class="num">${v == null ? "—" : price(v)}</dd></div>
      <div><dt>区间高</dt><dd class="num up">${hi == null ? "—" : price(hi)}</dd></div>
      <div><dt>区间低</dt><dd class="num down">${lo == null ? "—" : price(lo)}</dd></div>
      <div><dt>最后报价</dt><dd class="num dim">${timeText(q.ts)}</dd></div>
      <div><dt>本品种持仓</dt><dd class="num">${held}</dd></div>`;
    $("#realPairName").textContent = label(pair);
    $("#realOrderPair").textContent = label(pair);
  }

  function renderAccount() {
    const a = state.account;
    if (!a) return;
    const lv = a.margin_level;
    let lvClass = "num";
    if (lv != null) {
      if (lv <= Number(a.stopout) * 1.05) lvClass += " down";
      else if (lv < Number(a.warn)) lvClass += " warn-text";
      else lvClass += " up";
    }
    $("#realAcct").innerHTML = `
      <div><dt>真实盘净值</dt><dd class="num">¥${money(a.equity)}</dd></div>
      <div><dt>余额</dt><dd class="num">¥${money(a.balance)}</dd></div>
      <div><dt>可用资金</dt><dd class="num">¥${money(a.free_margin)}</dd></div>
      <div><dt>占用保证金</dt><dd class="num">¥${money(a.margin)}</dd></div>
      <div><dt>维持率</dt><dd class="${lvClass}">${lv == null ? "—" : lv.toFixed(0) + "%"}</dd></div>`;
  }

  function renderMarket() {
    const m = state.market || {};
    const dot = $("#marketDot");
    const text = $("#marketText");
    const source = $("#marketSource");
    let msg;
    let cls;

    if (!m.last_update && m.error) {
      msg = `行情源异常：${m.error}`;
      cls = "is-error";
    } else if (!m.last_update) {
      msg = "行情加载中…";
      cls = "";
    } else if (m.stale) {
      msg = `休市 / 数据延迟 · 最后报价 ${timeText(m.last_update)}`;
      cls = "is-stale";
    } else {
      msg = `实时行情 · 更新于 ${timeText(m.last_update)}`;
      cls = "is-live";
    }

    dot.className = `market-dot ${cls}`;
    text.textContent = msg;
    $("#marketTicker").textContent = msg;
    source.textContent = m.source ? `数据源：${m.source}` : "";
    $("#realBanner").classList.toggle("is-stale", m.stale);
    $("#realBanner").classList.toggle("is-error", cls === "is-error");
  }

  function renderRiskBar() {
    const a = state.account;
    const bar = $("#realRiskBar");
    if (!a) { bar.hidden = true; return; }
    if (a.status === "blown") {
      bar.hidden = false;
      $("#realRiskText").textContent = "真实盘账户已爆仓，点右上角「重置账户」重新开始。";
      return;
    }
    const lv = a.margin_level;
    if (lv != null && lv < Number(a.warn || 130)) {
      bar.hidden = false;
      $("#realRiskText").textContent =
        `真实盘维持率 ${lv.toFixed(0)}%，接近爆仓线 ${Number(a.stopout || 50)}%，注意减仓。`;
    } else {
      bar.hidden = true;
    }
  }

  function renderLeverage() {
    const list = state.config.leverages || FALLBACK.leverages;
    const html = list.map((v) => `
      <button type="button"
              class="${v === lev ? "is-active" : ""}"
              data-real-lev="${v}"
              aria-pressed="${v === lev}">${Number(v)}×</button>`).join("");
    const box = $("#realLeverageChips");
    if (box.dataset.sig !== html) {
      box.innerHTML = html;
      box.dataset.sig = html;
    }
  }

  function renderPositions() {
    const rows = state.positions.map((p) => `
      <tr>
        <td>${label(p.pair)}</td>
        <td class="${p.side > 0 ? "up" : "down"}">${p.side > 0 ? "做多" : "做空"}</td>
        <td class="num">¥${money(p.amount)}</td>
        <td class="num dim">${Number(p.leverage)}×</td>
        <td class="num dim">¥${money(p.notional)}</td>
        <td class="num">${price(p.entry)}</td>
        <td class="num">${price(p.price)}</td>
        <td class="num ${p.pnl >= 0 ? "up" : "down"}">${signed(p.pnl)}</td>
        <td><button class="btn btn-ghost btn-sm" type="button" data-real-close="${p.id}">平仓</button></td>
      </tr>`).join("");
    $("#realPositionsBody").innerHTML =
      rows || `<tr><td colspan="9" class="empty">暂无真实盘持仓</td></tr>`;
  }

  function statCards(s) {
    const rate = Number(s.win_rate) || 0;
    const pf = s.profit_factor;
    const pfText = pf != null ? pf.toFixed(2) : (s.gross_win > 0 ? "∞" : "—");
    return `
      <div class="stat"><span>已平仓</span><b class="num">${s.total} 笔</b></div>
      <div class="stat"><span>成功率</span><b class="num ${rate >= 50 ? "up" : "down"}">${rate.toFixed(1)}%</b>
        <div class="stat-bar"><i style="width:${Math.max(0, Math.min(100, rate))}%"></i></div>
      </div>
      <div class="stat"><span>盈利 / 亏损</span><b class="num">${s.wins} / ${s.losses}</b></div>
      <div class="stat"><span>净利润</span><b class="num ${s.net >= 0 ? "up" : "down"}">${signed(s.net)}</b></div>
      <div class="stat"><span>盈亏比</span><b class="num">${pfText}</b></div>
      <div class="stat"><span>最大单笔盈利</span><b class="num up">${signed(s.best)}</b></div>
      <div class="stat"><span>最大单笔亏损</span><b class="num down">${signed(s.worst)}</b></div>
      <div class="stat"><span>最长连胜 / 连亏</span><b class="num">${s.best_streak} / ${s.worst_streak}</b></div>`;
  }

  function renderTrades() {
    const s = state.stats;
    const rows = state.trades || [];
    const total = state.tradeTotal || rows.length;
    $("#realTradeMeta").textContent = total ? `显示最近 ${rows.length} / 共 ${total} 笔` : "";
    $("#realStats").innerHTML = s && s.total
      ? statCards(s)
      : `<p class="dim small">还没有真实盘成交。</p>`;

    $("#realTradesBody").innerHTML = rows.length
      ? rows.map((t) => {
          const cls = t.pnl > 0 ? "up" : t.pnl < 0 ? "down" : "dim";
          const tag = REASON[t.reason] || t.reason;
          return `
            <tr>
              <td class="num dim">${timeText(t.closed_at)}</td>
              <td>${label(t.pair)}</td>
              <td class="${t.side > 0 ? "up" : "down"}">${t.side > 0 ? "做多" : "做空"}</td>
              <td class="num">¥${money(t.amount)}</td>
              <td class="num dim">${Number(t.leverage)}×</td>
              <td class="num">${price(t.entry)}</td>
              <td class="num">${price(t.exit)}</td>
              <td class="num ${cls}">${signed(t.pnl)}</td>
              <td><span class="reason reason-${esc(t.reason)}">${esc(tag)}</span></td>
            </tr>`;
        }).join("")
      : `<tr><td colspan="9" class="empty">还没有真实盘成交</td></tr>`;
  }

  function updateOrderPanel() {
    const v = state.prices[pair];
    const amount = Number($("#realAmount").value);
    const hint = $("#realMarginHint");
    const levHint = $("#realLeverageHint");
    const note = $("#realAccountNote");

    if (v == null || !Number.isFinite(amount) || amount <= 0) {
      hint.textContent = v == null ? "等待真实行情加载…" : "";
      levHint.textContent = "";
      levHint.className = "hint";
      return;
    }

    const notional = amount * lev;
    hint.textContent =
      `名义价值 ≈ ¥${money(notional)}　|　汇率每变动 1%，盈亏约 ¥${money(notional * 0.01)}`;

    const a = state.account;
    if (!a) { levHint.textContent = ""; return; }
    const so = Number(a.stopout || FALLBACK.stopout);
    const usedAfter = Number(a.margin || 0) + amount;
    const buffer = Number(a.equity || 0) - (usedAfter * so) / 100;
    const movePct = notional > 0 ? (buffer / notional) * 100 : 0;

    if (movePct <= 0.001) {
      levHint.textContent = "这笔开仓会立刻触及爆仓线，请减少金额或降低杠杆。";
      levHint.className = "hint hint-warn";
    } else {
      levHint.textContent =
        `约可承受反向波动 ${movePct.toFixed(2)}%，即浮亏约 ¥${money(buffer)} 后触及 ${so}% 爆仓线。`;
      levHint.className = movePct < 0.3 ? "hint hint-warn" : "hint";
    }

    if (state.market && state.market.stale) {
      hint.textContent += "　休市/延迟报价，按最后报价成交。";
    }
    note.hidden = !(a.status === "blown");
    note.textContent = a.status === "blown" ? "真实盘账户已爆仓，请先重置。" : "";
  }

  function updateTradingEnabled() {
    const a = state.account;
    const blown = !!a && a.status === "blown";
    const noPrice = state.prices[pair] == null;
    $("#realLongBtn").disabled = blown || pending || noPrice;
    $("#realShortBtn").disabled = blown || pending || noPrice;
  }

  /* ---------------- 操作 ---------------- */

  async function submitOrder(side) {
    if (pending) return;
    const amount = Number($("#realAmount").value);
    const min = state.config.min_amount || FALLBACK.min_amount;
    if (!Number.isFinite(amount) || amount < min) {
      return toast(`投入金额不能少于 ¥${money(min)}`);
    }
    if (state.prices[pair] == null) {
      return toast("真实行情尚未加载");
    }

    pending = true;
    updateTradingEnabled();
    try {
      await api("/api/real/orders", {
        method: "POST",
        body: JSON.stringify({ pair, side, amount, leverage: lev }),
      });
      window.FxSound?.ok();
      toast(`真实盘已${side > 0 ? "做多" : "做空"} ${label(pair)} ¥${money(amount)}`);
      await refreshState();
      await loadHistory();
    } catch (e) {
      toast(e.message);
    } finally {
      pending = false;
      updateTradingEnabled();
    }
  }

  async function closePosition(id) {
    try {
      const r = await api(`/api/real/positions/${id}/close`, { method: "POST" });
      toast(`真实盘已平仓，本次盈亏 ${signed(r.pnl)}`);
      window.FxSound?.ok();
      await refreshState();
      await loadHistory();
    } catch (e) {
      toast(e.message);
    }
  }

  async function resetAccount() {
    if (!confirm("重置真实盘账户？当前持仓和成交记录会清空。")) return;
    try {
      await api("/api/real/reset", { method: "POST" });
      toast("真实盘账户已重置");
      await refreshState();
      await loadHistory();
    } catch (e) {
      toast(e.message);
    }
  }

  async function switchPair(p) {
    if (p === pair) return;
    pair = p;
    points = [];
    renderPairs();
    renderQuote();
    updateOrderPanel();
    drawChart();
    await loadHistory();
    renderAll();
  }

  /* ---------------- 图表 ---------------- */

  function drawChart() {
    const canvas = $("#realChart");
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const W = Math.floor(rect.width);
    const H = Math.floor(rect.height);
    if (W < 20 || H < 20) return;

    const dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(W * dpr) || canvas.height !== Math.round(H * dpr)) {
      canvas.width = Math.round(W * dpr);
      canvas.height = Math.round(H * dpr);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    const padL = 10, padR = 76, padT = 12, padB = 22;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;
    if (plotW < 20 || plotH < 20) return;

    if (points.length < 2) {
      ctx.fillStyle = "#64748b";
      ctx.font = "13px ui-monospace, monospace";
      ctx.textBaseline = "alphabetic";
      ctx.fillText("真实行情数据积累中…", padL, padT + 20);
      return;
    }

    const vals = points.map((p) => Number(p[1]));
    let lo = Math.min(...vals);
    let hi = Math.max(...vals);
    const span = hi - lo || Math.max(hi * 0.0008, 0.001);
    lo -= span * 0.10;
    hi += span * 0.10;

    const X = (i) => padL + (i / (points.length - 1)) * plotW;
    const Y = (v) => padT + (1 - (v - lo) / (hi - lo)) * plotH;

    ctx.strokeStyle = "#1b2637";
    ctx.lineWidth = 1;
    ctx.font = "11px ui-monospace, monospace";
    ctx.fillStyle = "#64748b";
    ctx.textBaseline = "middle";
    ctx.textAlign = "left";
    for (let i = 0; i <= 4; i++) {
      const v = lo + ((hi - lo) * i) / 4;
      const y = Math.round(Y(v)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(padL + plotW, y);
      ctx.stroke();
      ctx.fillText(price(v), padL + plotW + 8, y);
    }

    const rising = vals[vals.length - 1] >= vals[0];
    const color = rising ? "#22c55e" : "#f43f5e";
    const grad = ctx.createLinearGradient(0, padT, 0, padT + plotH);
    grad.addColorStop(0, rising ? "rgba(34,197,94,.22)" : "rgba(244,63,94,.22)");
    grad.addColorStop(1, "rgba(0,0,0,0)");

    ctx.beginPath();
    ctx.moveTo(X(0), Y(vals[0]));
    for (let i = 1; i < vals.length; i++) ctx.lineTo(X(i), Y(vals[i]));
    ctx.lineTo(X(vals.length - 1), padT + plotH);
    ctx.lineTo(X(0), padT + plotH);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    ctx.beginPath();
    ctx.moveTo(X(0), Y(vals[0]));
    for (let i = 1; i < vals.length; i++) ctx.lineTo(X(i), Y(vals[i]));
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.8;
    ctx.lineJoin = "round";
    ctx.stroke();

    const last = vals[vals.length - 1];
    const lastY = Y(last);
    ctx.save();
    ctx.setLineDash([3, 4]);
    ctx.strokeStyle = "rgba(148,163,184,.45)";
    ctx.beginPath();
    ctx.moveTo(padL, Math.round(lastY) + 0.5);
    ctx.lineTo(padL + plotW, Math.round(lastY) + 0.5);
    ctx.stroke();
    ctx.restore();

    const text = price(last);
    ctx.font = "600 11px ui-monospace, monospace";
    const tw = ctx.measureText(text).width + 12;
    ctx.fillStyle = color;
    ctx.fillRect(W - tw - 8, lastY - 9, tw, 18);
    ctx.fillStyle = "#06121a";
    ctx.textAlign = "center";
    ctx.fillText(text, W - tw / 2 - 8, lastY);
    ctx.textAlign = "left";

    ctx.fillStyle = "#475569";
    ctx.font = "10px ui-monospace, monospace";
    ctx.textBaseline = "alphabetic";
    ctx.fillText(clockText(points[0][0]), padL, H - 6);
    ctx.textAlign = "right";
    ctx.fillText(clockText(points[points.length - 1][0]), padL + plotW, H - 6);
    ctx.textAlign = "left";
  }

  /* ---------------- 事件 ---------------- */

  function onClick(e) {
    const pairBtn = e.target.closest("[data-real-pair]");
    if (pairBtn) return switchPair(pairBtn.dataset.realPair);

    const levBtn = e.target.closest("[data-real-lev]");
    if (levBtn) {
      lev = Number(levBtn.dataset.realLev);
      localStorage.setItem("fx_real_lev", String(lev));
      renderLeverage();
      updateOrderPanel();
      return;
    }

    const amountBtn = e.target.closest("[data-real-amount]");
    if (amountBtn) {
      $("#realAmount").value = amountBtn.dataset.realAmount;
      updateOrderPanel();
      return;
    }

    const closeBtn = e.target.closest("[data-real-close]");
    if (closeBtn) return closePosition(Number(closeBtn.dataset.realClose));
  }

  function init() {
    if (!token) {
      location.replace("/");
      return;
    }

    $("#realOrderForm").addEventListener("submit", (e) => e.preventDefault());
    document.addEventListener("click", onClick);
    $("#realAmount").addEventListener("input", updateOrderPanel);
    $("#realLongBtn").addEventListener("click", () => submitOrder(1));
    $("#realShortBtn").addEventListener("click", () => submitOrder(-1));
    $("#realResetBtn").addEventListener("click", resetAccount);
    $("#realLogoutBtn").addEventListener("click", logout);

    if ("ResizeObserver" in window) {
      new ResizeObserver(() => drawChart()).observe($(".real-chart-wrap"));
    } else {
      window.addEventListener("resize", drawChart);
    }

    boot();
    setInterval(refreshState, 3000);
    setInterval(loadHistory, 10000);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) {
        refreshState();
        loadHistory();
      }
    });
  }

  async function boot() {
    try {
      applyState(await api("/api/real/state"));
      renderAll();
      await loadHistory();
    } catch (e) {
      if (e.status === 401) return logout();
      toast(e.message);
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
