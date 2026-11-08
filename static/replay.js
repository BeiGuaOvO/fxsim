(() => {
  "use strict";

  const PAIRS = ["USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "CHFJPY"];
  const FALLBACK = {
    leverages: [10, 50, 100, 200, 500], min_amount: 1000,
    stopout: 50, warn: 130, start_balance: 1000000,
    speeds: [1, 3, 5, 10, 30, 60], default_speed: 5,
  };
  const REASON = { manual: "手动", stopout: "强平", bust: "爆仓" };

  const $ = (sel, root = document) => root.querySelector(sel);
  const token = localStorage.getItem("fx_token") || "";

  let pair = "USDJPY";
  let lev = Number(localStorage.getItem("fx_replay_lev")) || 10;
  let points = [];
  let pending = false;
  let refreshing = false;
  let historySeq = 0;
  let stateTimer = null;
  let historyTimer = null;

  const state = {
    active: false, playing: false, ended: false, speed: 5,
    start_ts: 0, cursor_ts: 0, data_start: 0, data_end: 0, progress: 0,
    account: null, positions: [], prices: {}, trades: [], stats: null,
    trade_total: 0, meta: null, metaError: "",
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

  const toLocalInput = (ts) => {
    const d = new Date(Number(ts) * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}` +
      `T${p(d.getHours())}:${p(d.getMinutes())}`;
  };

  function detailText(detail) {
    if (!detail) return "请求失败";
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map((x) => x.msg || "参数错误").join("；");
    return detail.msg || "请求失败";
  }

  function toast(text) {
    const box = $("#replayToasts");
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
    location.replace("/real");
  }

  /* ---------------- Meta / 控制 ---------------- */

  async function loadMeta() {
    try {
      const meta = await api("/api/replay/meta");
      state.meta = meta;
      state.metaError = "";
      const start = $("#replayStart");
      start.min = toLocalInput(meta.from);
      start.max = toLocalInput(meta.to);
      start.value = toLocalInput(meta.default_start);
      start.step = String(meta.interval || 300);

      const select = $("#replaySpeed");
      select.innerHTML = (meta.speeds || FALLBACK.speeds).map((v) =>
        `<option value="${v}">${v} 根/秒</option>`).join("");
      select.value = String(meta.default_speed || FALLBACK.default_speed);

      $("#replayMeta").textContent =
        `${meta.source || "Yahoo Finance"} · ${meta.range || "30d"} · ` +
        `每根 ${meta.interval_label || "5m"} · 数据源可能延迟`;
      $("#replayStartBtn").disabled = false;
      $("#replayStatus").textContent = "数据已就绪，选择开始时间后点「开始回放」";
      $("#replaySource").textContent = `${meta.range || "30d"} · ${meta.interval_label || "5m"} 历史数据`;
      renderAccount();
      updateOrderPanel();
    } catch (e) {
      state.metaError = e.message;
      $("#replayStatus").textContent = `回放数据加载失败：${e.message}`;
      $("#replayBanner").classList.add("is-error");
      $("#replayDot").className = "market-dot is-error";
      $("#replayStartBtn").disabled = true;
      toast(e.message);
    }
  }

  async function startReplay() {
    if (!state.meta) {
      await loadMeta();
      if (!state.meta) return;
    }
    if (state.active && !confirm("重新开始会清空当前回放账户，继续？")) return;
    points = [];
    const raw = $("#replayStart").value;
    const startTs = raw ? new Date(raw).getTime() / 1000 : null;
    if (!startTs || Number.isNaN(startTs)) return toast("请选择有效的开始时间");
    const speed = Number($("#replaySpeed").value) || state.meta.default_speed;

    try {
      const msg = await api("/api/replay/start", {
        method: "POST",
        body: JSON.stringify({ start_ts: startTs, speed }),
      });
      applyState(msg);
      await loadHistory();
      startPolling();
      toast(`回放已开始：${timeText(state.start_ts)}`);
    } catch (e) {
      toast(e.message);
    }
  }

  async function restartReplay() {
    if (!state.active) return startReplay();
    if (!confirm("重新开始会清空当前回放账户，继续？")) return;
    points = [];
    try {
      const msg = await api("/api/replay/start", {
        method: "POST",
        body: JSON.stringify({ start_ts: state.start_ts, speed: state.speed }),
      });
      applyState(msg);
      await loadHistory();
      toast(`回放已重新开始：${timeText(state.start_ts)}`);
    } catch (e) {
      toast(e.message);
    }
  }

  async function toggleReplay() {
    if (!state.active || state.ended) return;
    try {
      await api("/api/replay/control", {
        method: "POST",
        body: JSON.stringify({ playing: !state.playing }),
      });
      await refreshState();
    } catch (e) {
      toast(e.message);
    }
  }

  async function changeSpeed() {
    const speed = Number($("#replaySpeed").value);
    if (!state.active) return;
    try {
      await api("/api/replay/control", {
        method: "POST",
        body: JSON.stringify({ speed }),
      });
      await refreshState();
    } catch (e) {
      toast(e.message);
    }
  }

  function startPolling() {
    if (stateTimer) return;
    stateTimer = setInterval(refreshState, 1000);
    historyTimer = setInterval(loadHistory, 2000);
  }

  /* ---------------- 状态 ---------------- */

  function applyState(msg) {
    if (!msg.active) {
      state.active = false;
      state.playing = false;
      state.ended = false;
      renderAll();
      return;
    }

    state.active = true;
    state.playing = !!msg.playing;
    state.ended = !!msg.ended;
    state.speed = Number(msg.speed) || state.speed;
    const speedSelect = $("#replaySpeed");
    if (speedSelect && state.speed) speedSelect.value = String(state.speed);
    state.start_ts = msg.start_ts || 0;
    state.cursor_ts = msg.cursor_ts || 0;
    state.data_start = msg.data_start || 0;
    state.data_end = msg.data_end || 0;
    state.progress = Number(msg.progress) || 0;
    state.account = msg.account || null;
    state.positions = msg.positions || [];
    state.prices = msg.prices || {};
    state.trades = msg.trades || [];
    state.stats = msg.stats || null;
    state.trade_total = msg.trade_total || 0;
    if (Array.isArray(msg.alerts)) msg.alerts.forEach(toast);
    renderAll();
  }

  async function refreshState() {
    if (refreshing || document.hidden) return;
    refreshing = true;
    try {
      applyState(await api("/api/replay/state"));
      startPolling();
    } catch (e) {
      if (e.status === 401) return logout();
      toast(e.message);
    } finally {
      refreshing = false;
    }
  }

  async function loadHistory() {
    if (!state.active) return;
    const seq = ++historySeq;
    try {
      const data = await api(
        `/api/replay/history?pair=${encodeURIComponent(pair)}&limit=300`
      );
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
    renderStatus();
    renderProgress();
    renderRiskBar();
    renderPositions();
    renderTrades();
    updateOrderPanel();
    updateTradingEnabled();
    drawChart();
  }

  function renderPairs() {
    $("#replayPairs").innerHTML = PAIRS.map((p) => {
      const v = state.prices[p];
      return `
        <button type="button"
                class="pair-chip${p === pair ? " is-active" : ""}"
                data-replay-pair="${p}">
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
    $("#replayQuote").innerHTML = `
      <div><dt>回放现价</dt><dd class="num">${v == null ? "—" : price(v)}</dd></div>
      <div><dt>区间高</dt><dd class="num up">${hi == null ? "—" : price(hi)}</dd></div>
      <div><dt>区间低</dt><dd class="num down">${lo == null ? "—" : price(lo)}</dd></div>
      <div><dt>光标时间</dt><dd class="num dim">${timeText(state.cursor_ts)}</dd></div>
      <div><dt>本品种持仓</dt><dd class="num">${held}</dd></div>`;
    $("#replayPairName").textContent = label(pair);
    $("#replayOrderPair").textContent = label(pair);
  }

  function renderAccount() {
    const a = state.account || {
      equity: state.meta ? state.meta.start_balance : null,
      balance: state.meta ? state.meta.start_balance : null,
      free_margin: state.meta ? state.meta.start_balance : null,
      margin: 0, margin_level: null, status: "idle",
      stopout: state.meta?.stopout || FALLBACK.stopout,
      warn: state.meta?.warn || FALLBACK.warn,
    };
    if (a.equity == null) return;

    const lv = a.margin_level;
    let lvClass = "num";
    if (lv != null) {
      if (lv <= Number(a.stopout) * 1.05) lvClass += " down";
      else if (lv < Number(a.warn)) lvClass += " warn-text";
      else lvClass += " up";
    }
    $("#replayAcct").innerHTML = `
      <div><dt>回放净值</dt><dd class="num">¥${money(a.equity)}</dd></div>
      <div><dt>余额</dt><dd class="num">¥${money(a.balance)}</dd></div>
      <div><dt>可用资金</dt><dd class="num">¥${money(a.free_margin)}</dd></div>
      <div><dt>占用保证金</dt><dd class="num">¥${money(a.margin)}</dd></div>
      <div><dt>维持率</dt><dd class="${lvClass}">${lv == null ? "—" : lv.toFixed(0) + "%"}</dd></div>`;
  }

  function renderStatus() {
    const dot = $("#replayDot");
    const status = $("#replayStatus");
    let cls = "";
    let text;
    if (state.metaError) {
      text = `回放数据加载失败：${state.metaError}`;
      cls = "is-error";
    } else if (!state.meta) {
      text = "正在加载 30 天数据…";
    } else if (!state.active) {
      text = "未开始 · 选择开始时间后点「开始回放」";
      cls = "is-stale";
    } else if (state.ended) {
      text = `回放结束 · ${timeText(state.cursor_ts)}`;
      cls = "is-stale";
    } else if (state.playing) {
      text = `回放中 · ${timeText(state.cursor_ts)} · ${state.speed} 根/秒`;
      cls = "is-live";
    } else {
      text = `已暂停 · ${timeText(state.cursor_ts)}`;
      cls = "is-stale";
    }
    dot.className = `market-dot ${cls}`;
    status.textContent = text;
    $("#replayTicker").textContent = text;
    $("#replaySource").textContent = state.meta
      ? `${state.meta.source || "Yahoo Finance"} · ${state.meta.range || "30d"} · ` +
        `${state.meta.interval_label || "5m"}`
      : "";
    $("#replayBanner").classList.toggle("is-error", !!state.metaError);

    const toggle = $("#replayToggleBtn");
    toggle.disabled = !state.active || state.ended;
    toggle.textContent = state.active && !state.playing ? "继续" : "暂停";
    $("#replayRestartBtn").disabled = !state.active;
    $("#replayStartBtn").disabled = !state.meta;
  }

  function renderProgress() {
    const fill = $("#replayProgressFill");
    if (!state.active) {
      fill.style.width = "0%";
      $("#replayTimeStart").textContent = "—";
      $("#replayTimeCursor").textContent = "—";
      $("#replayTimeEnd").textContent = "—";
      return;
    }
    fill.style.width = `${Math.max(0, Math.min(100, state.progress * 100))}%`;
    $("#replayTimeStart").textContent = `起点 ${timeText(state.start_ts)}`;
    $("#replayTimeCursor").textContent = `当前 ${timeText(state.cursor_ts)}`;
    $("#replayTimeEnd").textContent = `终点 ${timeText(state.data_end)}`;
  }

  function renderRiskBar() {
    const a = state.account;
    const bar = $("#replayRiskBar");
    if (!a) { bar.hidden = true; return; }
    if (a.status === "blown") {
      bar.hidden = false;
      $("#replayRiskText").textContent = "回放账户已爆仓，点「重新开始」重新回放。";
      return;
    }
    const lv = a.margin_level;
    if (lv != null && lv < Number(a.warn || FALLBACK.warn)) {
      bar.hidden = false;
      $("#replayRiskText").textContent =
        `回放维持率 ${lv.toFixed(0)}%，接近爆仓线 ${Number(a.stopout || FALLBACK.stopout)}%，注意减仓。`;
    } else {
      bar.hidden = true;
    }
  }

  function renderLeverage() {
    const list = state.meta?.leverages || FALLBACK.leverages;
    if (!list.includes(lev)) {
      lev = list[0];
      localStorage.setItem("fx_replay_lev", String(lev));
    }
    const html = list.map((v) => `
      <button type="button"
              class="${v === lev ? "is-active" : ""}"
              data-replay-lev="${v}"
              aria-pressed="${v === lev}">${Number(v)}×</button>`).join("");
    const box = $("#replayLeverageChips");
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
        <td><button class="btn btn-ghost btn-sm" type="button" data-replay-close="${p.id}">平仓</button></td>
      </tr>`).join("");
    $("#replayPositionsBody").innerHTML =
      rows || `<tr><td colspan="9" class="empty">暂无回放持仓</td></tr>`;
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
    const total = state.trade_total || rows.length;
    $("#replayTradeMeta").textContent = total ? `显示最近 ${rows.length} / 共 ${total} 笔` : "";
    $("#replayStats").innerHTML = s && s.total
      ? statCards(s)
      : `<p class="dim small">还没有回放成交。</p>`;

    $("#replayTradesBody").innerHTML = rows.length
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
      : `<tr><td colspan="9" class="empty">还没有回放成交</td></tr>`;
  }

  function updateOrderPanel() {
    const v = state.prices[pair];
    const amount = Number($("#replayAmount").value);
    const hint = $("#replayMarginHint");
    const levHint = $("#replayLeverageHint");
    const note = $("#replayAccountNote");

    const a = state.account || {
      equity: state.meta ? state.meta.start_balance : 0,
      margin: 0, status: "idle",
      stopout: state.meta?.stopout || FALLBACK.stopout,
      warn: state.meta?.warn || FALLBACK.warn,
    };
    note.hidden = !(a.status === "blown");
    note.textContent = a.status === "blown" ? "回放账户已爆仓，请重新开始。" : "";

    if (!state.active || v == null || !Number.isFinite(amount) || amount <= 0) {
      hint.textContent = state.active ? (v == null ? "等待回放价格…" : "") : "开始回放后可以下单";
      levHint.textContent = "";
      levHint.className = "hint";
      return;
    }

    const notional = amount * lev;
    hint.textContent =
      `名义价值 ≈ ¥${money(notional)}　|　汇率每变动 1%，盈亏约 ¥${money(notional * 0.01)}`;

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
  }

  function updateTradingEnabled() {
    const a = state.account;
    const blown = !!a && a.status === "blown";
    const noPrice = state.prices[pair] == null;
    const disabled = !state.active || state.ended || blown || pending || noPrice;
    $("#replayLongBtn").disabled = disabled;
    $("#replayShortBtn").disabled = disabled;
  }

  /* ---------------- 操作 ---------------- */

  async function submitOrder(side) {
    if (pending) return;
    const amount = Number($("#replayAmount").value);
    const min = state.meta?.min_amount || FALLBACK.min_amount;
    if (!Number.isFinite(amount) || amount < min) {
      return toast(`投入金额不能少于 ¥${money(min)}`);
    }
    if (!state.active || state.ended) return toast("回放未开始或已结束");
    if (state.prices[pair] == null) return toast("回放价格尚未就绪");

    pending = true;
    updateTradingEnabled();
    try {
      await api("/api/replay/orders", {
        method: "POST",
        body: JSON.stringify({ pair, side, amount, leverage: lev }),
      });
      window.FxSound?.ok();
      toast(`回放已${side > 0 ? "做多" : "做空"} ${label(pair)} ¥${money(amount)}`);
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
      const r = await api(`/api/replay/positions/${id}/close`, { method: "POST" });
      toast(`回放已平仓，本次盈亏 ${signed(r.pnl)}`);
      window.FxSound?.ok();
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
    const canvas = $("#replayChart");
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

    if (!state.active) {
      ctx.fillStyle = "#64748b";
      ctx.font = "13px ui-monospace, monospace";
      ctx.textBaseline = "alphabetic";
      ctx.fillText("开始回放后显示行情", padL, padT + 20);
      return;
    }
    if (points.length < 2) {
      ctx.fillStyle = "#64748b";
      ctx.font = "13px ui-monospace, monospace";
      ctx.textBaseline = "alphabetic";
      ctx.fillText("正在等待回放数据…", padL, padT + 20);
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
    ctx.fillText(timeText(points[0][0]), padL, H - 6);
    ctx.textAlign = "right";
    ctx.fillText(timeText(points[points.length - 1][0]), padL + plotW, H - 6);
    ctx.textAlign = "left";
  }

  /* ---------------- 事件 ---------------- */

  function onClick(e) {
    const pairBtn = e.target.closest("[data-replay-pair]");
    if (pairBtn) return switchPair(pairBtn.dataset.replayPair);

    const levBtn = e.target.closest("[data-replay-lev]");
    if (levBtn) {
      lev = Number(levBtn.dataset.replayLev);
      localStorage.setItem("fx_replay_lev", String(lev));
      renderLeverage();
      updateOrderPanel();
      return;
    }

    const amountBtn = e.target.closest("[data-replay-amount]");
    if (amountBtn) {
      $("#replayAmount").value = amountBtn.dataset.replayAmount;
      updateOrderPanel();
      return;
    }

    const closeBtn = e.target.closest("[data-replay-close]");
    if (closeBtn) return closePosition(Number(closeBtn.dataset.replayClose));
  }

  function init() {
    if (!token) {
      location.replace("/real");
      return;
    }

    $("#replayOrderForm").addEventListener("submit", (e) => e.preventDefault());
    $("#replayStartBtn").addEventListener("click", startReplay);
    $("#replayToggleBtn").addEventListener("click", toggleReplay);
    $("#replayRestartBtn").addEventListener("click", restartReplay);
    $("#replaySpeed").addEventListener("change", changeSpeed);
    $("#replayAmount").addEventListener("input", updateOrderPanel);
    $("#replayLongBtn").addEventListener("click", () => submitOrder(1));
    $("#replayShortBtn").addEventListener("click", () => submitOrder(-1));
    $("#replayLogoutBtn").addEventListener("click", logout);
    document.addEventListener("click", onClick);

    if ("ResizeObserver" in window) {
      new ResizeObserver(() => drawChart()).observe($(".real-chart-wrap"));
    } else {
      window.addEventListener("resize", drawChart);
    }

    boot();
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && state.active) {
        refreshState();
        loadHistory();
      }
    });
  }

  async function boot() {
    await loadMeta();
    try {
      const msg = await api("/api/replay/state");
      if (msg.active) {
        applyState(msg);
        await loadHistory();
        startPolling();
      } else {
        renderAll();
      }
    } catch (e) {
      if (e.status === 401) return logout();
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
