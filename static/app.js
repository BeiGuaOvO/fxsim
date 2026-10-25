(() => {
  "use strict";

  const PAIRS = ["USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "CHFJPY"];
  const MAX_CANDLES = 120;
  const DEFAULT_CANDLE_TICKS = 5;
  const BOARD_INTERVAL = 5000;
  const RECONNECT_DELAY = 3000;

  /* 后端 config 没拿到之前的兜底值 */
  const FALLBACK = { leverages: [10, 50, 100, 200, 500], min_amount: 1000, stopout: 50 };

  const $ = (sel, root = document) => root.querySelector(sel);

  let token = localStorage.getItem("fx_token") || "";
  let pair = "USDJPY";
  let lev = Number(localStorage.getItem("fx_lev")) || 100;
  let candles = [];                              // { i, o, h, l, c }
  let candleTicks = DEFAULT_CANDLE_TICKS;
  let historySeq = 0;
  let ws = null;
  let retryTimer = null;
  let boardTimer = null;
  let booted = false;
  let pending = false;
  let lastNewsId = 0;
  let riskActive = false;
  let lastStatus = null;
  let myName = localStorage.getItem("fx_name") || "";
  let profileName = "";
  let tradeLimit = 10;
  const TRADE_PAGE = 10;
  const REASON = { manual: "手动", stopout: "强平", bust: "爆仓" };

  const state = {
    prices: {}, positions: [], account: null,
    config: { ...FALLBACK }, news: [],
    trades: [], stats: null, tradeTotal: 0,
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
    const d = new Date(ts * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} `
         + `${p(d.getHours())}:${p(d.getMinutes())}`;
  };

  const dateText = (ts) => {
    const d = new Date(ts * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  };

  const levText = (v) => `${Number(v)}×`;

  function detailText(detail) {
    if (!detail) return "请求失败";
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map((x) => x.msg || "参数错误").join("；");
    return detail.msg || "请求失败";
  }

  function toast(text) {
    const box = $("#toasts");
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

  function roundRect(ctx, x, y, w, h, r) {
    const rr = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + rr, y);
    ctx.arcTo(x + w, y, x + w, y + h, rr);
    ctx.arcTo(x + w, y + h, x, y + h, rr);
    ctx.arcTo(x, y + h, x, y, rr);
    ctx.arcTo(x, y, x + w, y, rr);
    ctx.closePath();
  }

  /* ---------------- 会话 ---------------- */

  function clearSession() {
    localStorage.removeItem("fx_token");
    token = "";
    if (retryTimer) clearTimeout(retryTimer);
    if (boardTimer) clearInterval(boardTimer);
    if (ws) { ws.onclose = null; ws.close(); ws = null; }
    location.reload();
  }

  async function auth(mode) {
    const username = $("#username").value.trim();
    const password = $("#password").value;

    if (username.length < 2) return toast("用户名至少 2 个字符");
    if (password.length < 6) return toast("密码至少 6 位");

    try {
      const data = await api(`/api/${mode}`, {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });
      token = data.token;
      localStorage.setItem("fx_token", token);
      myName = username;
      localStorage.setItem("fx_name", myName);
      await boot();
    } catch (e) {
      toast(e.message);
    }
  }

  async function boot() {
    if (booted) return;
    try {
      applyState(await api("/api/state"));
      await loadHistory(pair);

      $("#login").hidden = true;
      $("#game").hidden = false;
      booted = true;

      renderAll();
      connect();
      await loadTrades(true);
      loadBoard();
      boardTimer = setInterval(loadBoard, BOARD_INTERVAL);
      syncFromHash();
    } catch (e) {
      if (e.status === 401) return clearSession();
      toast(e.message);
    }
  }

  /* ---------------- 状态 ---------------- */

  function applyState(msg) {
    if (msg.prices) state.prices = msg.prices;
    if (msg.account) state.account = msg.account;
    if (msg.positions) state.positions = msg.positions;
    if (msg.config) {
      state.config = msg.config;
      if (!state.config.leverages.includes(lev)) {
        lev = state.config.leverages[0];
        localStorage.setItem("fx_lev", String(lev));
      }
    }
    if (msg.news) applyNews(msg.news);
    if (msg.headline) $("#ticker").textContent = msg.headline;
    if (msg.account && lastStatus === "active" && msg.account.status === "blown") {
      window.FxSound?.bust();
    }
    if (msg.account) lastStatus = msg.account.status;
  }

  function applyNews(list) {
    if (!Array.isArray(list) || !list.length) return;
    const newest = list[0];
    if (lastNewsId && newest.id > lastNewsId) {
      toast(`【${newest.tag}】${newest.title}`);
      window.FxSound?.news(newest.size);
    }
    lastNewsId = newest.id;
    state.news = list;
  }

  async function refresh() {
    try {
      applyState(await api("/api/state"));
      renderAll();
    } catch (e) {
      if (e.status === 401) return clearSession();
      toast(e.message);
    }
  }

  function bucketOf(tick) {
    return Math.floor(tick / candleTicks);
  }
  /* 把逐 tick 的历史价格聚合成蜡烛 */
  function buildCandles(points, lastTick) {
    const out = [];
    for (let k = 0; k < points.length; k++) {
      const b = bucketOf(lastTick - (points.length - 1 - k));
      const v = points[k];
      const cur = out[out.length - 1];
      if (!cur || cur.i !== b) {
        out.push({ i: b, o: v, h: v, l: v, c: v });
      } else {
        if (v > cur.h) cur.h = v;
        if (v < cur.l) cur.l = v;
        cur.c = v;
      }
    }
    return out.slice(-MAX_CANDLES);
  }
  /* 实时价格进来后更新最后一根蜡烛 */
  function ingest(tick, price) {
    if (!Number.isFinite(price) || !Number.isFinite(tick)) return;
    const b = bucketOf(tick);
    const last = candles[candles.length - 1];
    if (!last) {
      candles.push({ i: b, o: price, h: price, l: price, c: price });
      return;
    }
    if (b > last.i) {
      candles.push({ i: b, o: price, h: price, l: price, c: price });
      if (candles.length > MAX_CANDLES) candles.shift();
    } else if (b === last.i) {
      if (price > last.h) last.h = price;
      if (price < last.l) last.l = price;
      last.c = price;
    }
    /* b < last.i 说明是迟到/乱序的旧数据，直接丢掉 */
  }

  /* ---------------- WebSocket ---------------- */

  function connect() {
    if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
    if (ws) { ws.onclose = null; ws.close(); }

    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${proto}//${location.host}/ws`);

    ws.onopen = () => ws.send(JSON.stringify({ token }));

    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type !== "tick" && msg.type !== "hello") return;

      applyState(msg);
      if (Array.isArray(msg.alerts)) msg.alerts.forEach(toast);
      ingest(msg.tick, state.prices[pair]);
      renderAll();
    };

    ws.onclose = (ev) => {
      if (ev.code === 4401) return clearSession();
      retryTimer = setTimeout(connect, RECONNECT_DELAY);
    };
  }

  /* ---------------- 历史行情 ---------------- */

  async function loadHistory(p) {
    const seq = ++historySeq;
    try {
      const data = await api(`/api/history?pair=${encodeURIComponent(p)}`);
      if (seq !== historySeq) return;
      candleTicks = Math.max(1, Number(data.candle_ticks) || DEFAULT_CANDLE_TICKS);
      candles = buildCandles(data.points, Number(data.tick) || 0);
    } catch (e) {
      if (seq === historySeq) toast(e.message);
    }
  }

  async function switchPair(p) {
    if (p === pair) return;
    pair = p;
    candles = [];
    renderPairs();
    renderQuote();
    updateOrderPanel();
    drawChart();
    await loadHistory(p);
    renderAll();
  }

  /* ---------------- 交易 ---------------- */

  function updateTradingEnabled() {
    const acc = state.account;
    const blown = !!acc && acc.status === "blown";

    $("#longBtn").disabled = blown || pending;
    $("#shortBtn").disabled = blown || pending;

    const note = $("#accountNote");
    note.hidden = !blown;
    note.textContent = blown ? "账户已爆仓，点右上角「重置账户」重新开始。" : "";
  }

  async function submitOrder(side) {
    if (pending) return;

    const amount = Number($("#amount").value);
    const min = state.config.min_amount || FALLBACK.min_amount;

    if (!Number.isFinite(amount) || amount < min) {
      return toast(`投入金额不能少于 ¥${money(min)}`);
    }

    pending = true;
    updateTradingEnabled();
    try {
      await api("/api/orders", {
        method: "POST",
        body: JSON.stringify({ pair, side, amount, leverage: lev }),
      });
      window.FxSound?.ok();
      await refresh();
      loadBoard();
      loadTrades();
    } catch (e) {
      toast(e.message);
    } finally {
      pending = false;
      updateTradingEnabled();
    }
  }

  async function closePosition(id) {
    try {
      const r = await api(`/api/positions/${id}/close`, { method: "POST" });
      toast(`已平仓，本次盈亏 ${signed(r.pnl)}`);
      window.FxSound?.ok();
      await refresh();
      loadBoard();
      loadTrades();
    } catch (e) {
      toast(e.message);
    }
  }

  async function resetAccount() {
    if (!confirm("重置账户？当前持仓和战绩都会清空。")) return;
    try {
      await api("/api/reset", { method: "POST" });
      toast("账户已重置");
      await refresh();
      loadBoard();
      loadTrades(true);
    } catch (e) {
      toast(e.message);
    }
  }

    async function deposit() {
    const amount = Number($("#topupAmount").value);
    if (!Number.isFinite(amount) || amount <= 0) {
      return toast("请输入要追加的金额");
    }
    try {
      const r = await api("/api/deposit", {
        method: "POST",
        body: JSON.stringify({ amount }),
      });
      toast(`已追加 ¥${money(amount)}，累计投入 ¥${money(r.deposited)}`);
      $("#topupAmount").dataset.auto = "1";
      await refresh();
      loadBoard();
    } catch (e) {
      toast(e.message);
    }
  }

  async function closeAll() {
    if (!state.positions.length) return toast("当前没有持仓");
    if (!confirm(`确定平掉全部 ${state.positions.length} 笔持仓？`)) return;
    try {
      for (const p of state.positions) {
        await api(`/api/positions/${p.id}/close`, { method: "POST" });
      }
      toast("已全部平仓");
      await refresh();
      loadBoard();
    } catch (e) {
      toast(e.message);
    }
  }

  /* ---------------- 渲染 ---------------- */

  function renderAll() {
    renderPairs();
    renderQuote();
    renderAccount();
    renderLeverage();
    renderRiskBar();
    renderNews();
    renderPositions();
    updateOrderPanel();
    updateTradingEnabled();
    drawChart();
  }

  function renderPairs() {
    $("#pairs").innerHTML = PAIRS.map((p) => {
      const v = state.prices[p];
      return `
        <button type="button"
                class="pair-chip${p === pair ? " is-active" : ""}"
                data-pair="${p}">
          <span class="pair-code">${label(p)}</span>
          <span class="pair-price num">${v == null ? "—" : price(v)}</span>
        </button>`;
    }).join("");
  }

  function renderQuote() {
    const v = state.prices[pair];
    const held = state.positions.filter((p) => p.pair === pair).length;
    const c = candles[candles.length - 1];
    const ohlc = c
      ? `<div><dt>开盘</dt><dd class="num">${price(c.o)}</dd></div>
         <div><dt>最高</dt><dd class="num up">${price(c.h)}</dd></div>
         <div><dt>最低</dt><dd class="num down">${price(c.l)}</dd></div>`
      : "";
    $("#quote").innerHTML = `
      <div><dt>现价</dt><dd class="num">${v == null ? "—" : price(v)}</dd></div>${ohlc}
      <div><dt>本品种持仓</dt><dd class="num">${held}</dd></div>`;
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

    $("#acct").innerHTML = `
      <div><dt>净值</dt><dd class="num">¥${money(a.equity)}</dd></div>
      <div><dt>余额</dt><dd class="num">¥${money(a.balance)}</dd></div>
      <div><dt>可用资金</dt><dd class="num">¥${money(a.free_margin)}</dd></div>
      <div><dt>占用保证金</dt><dd class="num">¥${money(a.margin)}</dd></div>
      <div><dt>维持率</dt><dd class="${lvClass}">${
        lv == null ? "—" : lv.toFixed(0) + "%"
      }</dd></div>`;

    const pct = a.goal > 0
      ? Math.max(0, Math.min(100, (a.equity / a.goal) * 100))
      : 0;
    const fill = $("#goalFill");
    fill.style.width = pct.toFixed(2) + "%";
    fill.parentElement.setAttribute("aria-valuenow", pct.toFixed(0));
    fill.parentElement.title = `目标进度 ${pct.toFixed(1)}%`;
  }

    /* 预警条：维持率低于预警线时出现，自动填好建议补仓金额 */
  function renderRiskBar() {
    const a = state.account;
    const bar = $("#riskBar");
    const input = $("#topupAmount");
    const btn = $("#topupBtn");

    if (!a) { bar.hidden = true; riskActive = false;return; }

    const lv = a.margin_level;
    const active = a.status === "active" && lv != null && lv < Number(a.warn);

    if (!active) {
      bar.hidden = true;
      riskActive = false;
      input.dataset.auto = "1";   // 下次出现时重新自动填
      return;
    }

    bar.hidden = false;
    if (!riskActive) {
      window.FxSound?.warn();
      riskActive = true;
    }
    bar.classList.toggle("is-urgent", lv <= Number(a.stopout) * 1.2);

    const need = Number(a.topup_need) || 0;
    const left = Math.max(0, Number(a.max_deposit) - Number(a.deposited || 0));
    const suggest = Math.min(need, left);

    let text;
    if (suggest <= 0 && left <= 0) {
      text = `维持率 ${lv.toFixed(0)}%，已达追加上限 ¥${money(a.max_deposit)}，建议减仓。`;
    } else if (suggest <= 0) {
      text = `维持率 ${lv.toFixed(0)}%，缓冲充足。`;
    } else {
      text = `维持率 ${lv.toFixed(0)}%，爆仓线 ${Number(a.stopout)}%。` +
             `建议追加 ¥${money(suggest)} 把维持率拉回 ${Math.round(Number(a.warn))}% 以上。`;
    }
    $("#riskText").textContent = text;

    if (suggest > 0 && input.dataset.auto !== "0") {
      input.value = String(suggest);
    }
    btn.disabled = suggest <= 0;
  }

  function renderLeverage() {
    const list = state.config.leverages || FALLBACK.leverages;
    const html = list.map((v) => `
      <button type="button"
              class="${v === lev ? "is-active" : ""}"
              data-lev="${v}"
              aria-pressed="${v === lev}">${levText(v)}</button>`).join("");

    const box = $("#leverageChips");
    if (box.dataset.sig !== html) {
      box.innerHTML = html;
      box.dataset.sig = html;
    }
  }

  function renderNews() {
    const box = $("#newsFeed");
    if (!box) return;
    const list = state.news || [];
    if (!list.length) {
      box.innerHTML = `<p class="dim small">还没有消息，稍等一会儿。</p>`;
      return;
    }
    const freshId = list[0].id;
    box.innerHTML = list.map((n) => {
      const chips = (n.impact || []).map((im) =>
        `<span class="news-chip ${im.dir > 0 ? "news-up" : "news-down"}">` +
        `${label(im.pair)} ${im.dir > 0 ? "↑" : "↓"}</span>`
      ).join("");
      return `
        <article class="news-item${n.id === freshId ? " is-fresh" : ""}" data-size="${esc(n.size)}">
          <div class="news-head">
            <span class="news-tag">${esc(n.tag)}</span>
            <span class="news-title">${esc(n.title)}</span>
          </div>
          <p class="news-body">${esc(n.body)}</p>
          <div class="news-impact">${chips}</div>
        </article>`;
    }).join("");
  }

  function renderPositions() {
    const rows = state.positions.map((p) => `
      <tr>
        <td>${label(p.pair)}</td>
        <td class="${p.side > 0 ? "up" : "down"}">${p.side > 0 ? "做多" : "做空"}</td>
        <td class="num">¥${money(p.amount)}</td>
        <td class="num dim">${levText(p.leverage)}</td>
        <td class="num dim">¥${money(p.notional)}</td>
        <td class="num">${price(p.entry)}</td>
        <td class="num">${price(p.price)}</td>
        <td class="num ${p.pnl >= 0 ? "up" : "down"}">${signed(p.pnl)}</td>
        <td><button class="btn btn-ghost btn-sm" type="button" data-close="${p.id}">平仓</button></td>
      </tr>`).join("");

    $("#positionsBody").innerHTML =
      rows || `<tr><td colspan="9" class="empty">暂无持仓</td></tr>`;
  }

    function updateOrderPanel() {
    $("#orderPair").textContent = label(pair);

    const a = state.account;
    const v = state.prices[pair];
    const amount = Number($("#amount").value);
    const hint = $("#marginHint");
    const levHint = $("#leverageHint");

    if (v == null || !Number.isFinite(amount) || amount <= 0) {
      hint.textContent = "";
      levHint.textContent = "";
      levHint.className = "hint";
      return;
    }

    const notional = amount * lev;
    hint.textContent =
      `名义价值 ≈ ¥${money(notional)}　|　汇率每变动 1%，盈亏约 ¥${money(notional * 0.01)}`;

    if (!a) { levHint.textContent = ""; return; }

    const so = Number(a.stopout || FALLBACK.stopout);
    const usedAfter = Number(a.margin || 0) + amount;
    const buffer = Number(a.equity || 0) - (usedAfter * so) / 100;
    const movePct = notional > 0 ? (buffer / notional) * 100 : 0;

    if (movePct <= 0.001) {
      levHint.textContent = "这笔开仓会立刻触及爆仓线，请减少金额或降低杠杆。";
      levHint.className = "hint hint-warn";
      return;
    }

    levHint.textContent =
      `约可承受反向波动 ${movePct.toFixed(2)}%，` +
      `即浮亏约 ¥${money(buffer)} 后触及 ${so}% 爆仓线。`;
    levHint.className = movePct < 0.3 ? "hint hint-warn" : "hint";
  }

/* ---------------- 交易记录 ---------------- */
  async function loadTrades(reset) {
    if (reset) tradeLimit = TRADE_PAGE;
    try {
      const d = await api(`/api/trades?limit=${tradeLimit}&offset=0`);
      state.trades = d.rows || [];
      state.stats = d.stats || null;
      state.tradeTotal = d.total || 0;
      renderTrades();
    } catch (e) {
      /* 记录加载失败不打断主流程 */
    }
  }
  function statCards(s) {
    const rate = Number(s.win_rate) || 0;
    const pf = s.profit_factor;
    const pfText = pf != null ? pf.toFixed(2) : (s.gross_win > 0 ? "∞" : "—");
    return `
      <div class="stat"><span>已平仓</span><b class="num">${s.total} 笔</b></div>
      <div class="stat">
        <span>成功率</span>
        <b class="num ${rate >= 50 ? "up" : "down"}">${rate.toFixed(1)}%</b>
        <div class="stat-bar"><i style="width:${Math.max(0, Math.min(100, rate))}%"></i></div>
      </div>
      <div class="stat"><span>盈利 / 亏损</span><b class="num">${s.wins} / ${s.losses}</b></div>
      <div class="stat"><span>净利润</span><b class="num ${s.net >= 0 ? "up" : "down"}">${signed(s.net)}</b></div>
      <div class="stat"><span>盈亏比</span><b class="num">${pfText}</b></div>
      <div class="stat"><span>最大单笔盈利</span><b class="num up">${signed(s.best)}</b></div>
      <div class="stat"><span>最大单笔亏损</span><b class="num down">${signed(s.worst)}</b></div>
      <div class="stat"><span>最长连胜 / 连亏</span><b class="num">${s.best_streak} / ${s.worst_streak}</b></div>`;
  }
  function tradeRow(t) {
    const cls = t.pnl > 0 ? "up" : t.pnl < 0 ? "down" : "dim";
    const tag = REASON[t.reason] || t.reason;
    return `
      <tr>
        <td class="num dim">${timeText(t.closed_at)}</td>
        <td>${label(t.pair)}</td>
        <td class="${t.side > 0 ? "up" : "down"}">${t.side > 0 ? "做多" : "做空"}</td>
        <td class="num">¥${money(t.amount)}</td>
        <td class="num dim">${levText(t.leverage)}</td>
        <td class="num">${price(t.entry)}</td>
        <td class="num">${price(t.exit)}</td>
        <td class="num ${cls}">${signed(t.pnl)}</td>
        <td><span class="reason reason-${esc(t.reason)}">${esc(tag)}</span></td>
      </tr>`;
  }
  function renderTrades() {
    const s = state.stats;
    const rows = state.trades || [];
    const total = state.tradeTotal || rows.length;
    $("#tradeMeta").textContent = total ? `显示 ${rows.length} / 共 ${total} 笔` : "";
    $("#tradeStats").innerHTML = s && s.total
      ? statCards(s)
      : `<p class="dim small">还没有已平仓的交易，平掉一笔后这里会出现记录。</p>`;
    $("#tradeBody").innerHTML = rows.length
      ? rows.map(tradeRow).join("")
      : `<tr><td colspan="9" class="empty">还没有交易记录</td></tr>`;
    $("#moreTradesBtn").hidden = rows.length >= total;
  }
    /* ---------------- 用户主页 ---------------- */

  async function openProfile(name) {
    if (!name) return;
    profileName = name;

    try {
      const p = await api(`/api/users/${encodeURIComponent(name)}`);
      $("#profile").hidden = false;
      document.body.classList.add("no-scroll");
      renderProfile(p);

      const want = `#/u/${encodeURIComponent(name)}`;
      if (location.hash !== want) location.hash = want;
    } catch (e) {
      profileName = "";
      toast(e.message);
    }
  }

  function closeProfile(updateHash) {
    $("#profile").hidden = true;
    document.body.classList.remove("no-scroll");
    profileName = "";
    if (updateHash && location.hash) {
      history.replaceState(null, "", location.pathname + location.search);
    }
  }

  function syncFromHash() {
    const m = (location.hash || "").match(/^#\/u\/(.+)$/);
    if (m) {
      const name = decodeURIComponent(m[1]);
      if (!name || name === profileName) return;
      openProfile(name);
    } else if (!$("#profile").hidden) {
      closeProfile(false);
    }
  }

  function renderProfile(p) {
    $("#pName").textContent = p.name;

    const bits = [];
    if (p.rank) bits.push(`排名 #${p.rank} / ${p.total_players}`);
    if (p.joined_at) bits.push(`加入于 ${dateText(p.joined_at)}`);
    if (p.me) bits.push("这是你自己");
    $("#pMeta").textContent = bits.join(" · ");

    const s = p.stats || {};
    const rate = Number(s.win_rate) || 0;
    const pf = s.profit_factor;
    const pfText = pf != null ? pf.toFixed(2) : (s.gross_win > 0 ? "∞" : "—");

    const cards = `
      <div class="stat"><span>净值</span><b class="num ${(p.return_pct || 0) >= 0 ? "up" : "down"}">¥${money(p.equity)}</b></div>
      <div class="stat"><span>最高净值</span><b class="num">¥${money(p.best)}</b></div>
      <div class="stat"><span>累计投入</span><b class="num dim">¥${money(p.principal)}</b></div>
      <div class="stat"><span>收益率</span><b class="num ${(p.return_pct || 0) >= 0 ? "up" : "down"}">${
        (p.return_pct || 0) >= 0 ? "+" : ""
      }${(p.return_pct || 0).toFixed(1)}%</b></div>
      <div class="stat"><span>当前持仓</span><b class="num">${p.open_positions} 笔</b></div>
      <div class="stat"><span>账户状态</span><b class="${p.status === "blown" ? "down" : "up"}">${
        p.status === "blown" ? "已爆仓" : "存活"
      }</b></div>
      <div class="stat"><span>已平仓</span><b class="num">${s.total || 0} 笔</b></div>
      <div class="stat">
        <span>成功率</span>
        <b class="num ${rate >= 50 ? "up" : "down"}">${rate.toFixed(1)}%</b>
        <div class="stat-bar"><i style="width:${Math.max(0, Math.min(100, rate))}%"></i></div>
      </div>
      <div class="stat"><span>盈亏比</span><b class="num">${pfText}</b></div>
      <div class="stat"><span>净利润</span><b class="num ${(s.net || 0) >= 0 ? "up" : "down"}">${signed(s.net || 0)}</b></div>
      <div class="stat"><span>最大单笔盈利</span><b class="num up">${signed(s.best || 0)}</b></div>
      <div class="stat"><span>最大单笔亏损</span><b class="num down">${signed(s.worst || 0)}</b></div>
      <div class="stat"><span>连胜 / 连亏</span><b class="num">${s.best_streak || 0} / ${s.worst_streak || 0}</b></div>`;

    const rows = (p.trades || []).map((t) => {
      const cls = t.pnl > 0 ? "up" : t.pnl < 0 ? "down" : "dim";
      const tag = REASON[t.reason] || t.reason;
      return `
        <tr>
          <td class="num dim">${timeText(t.closed_at)}</td>
          <td>${label(t.pair)}</td>
          <td class="${t.side > 0 ? "up" : "down"}">${t.side > 0 ? "做多" : "做空"}</td>
          <td class="num">¥${money(t.amount)}</td>
          <td class="num dim">${levText(t.leverage)}</td>
          <td class="num ${cls}">${signed(t.pnl)}</td>
          <td><span class="reason reason-${esc(t.reason)}">${esc(tag)}</span></td>
        </tr>`;
    }).join("");

    const tips = [];
    if ((p.open_positions || 0) > 0 && p.status === "active") {
      tips.push(`当前有 ${p.open_positions} 笔持仓在场，浮动盈亏未计入上面的战绩。`);
    }
    if (p.deposited > 0) {
      tips.push(`累计补仓 ¥${money(p.deposited)}，收益率已按累计投入折算。`);
    }

    $("#profileBody").innerHTML = `
      <section class="profile-section">
        <div class="stat-grid">${cards}</div>
      </section>

      <section class="profile-section">
        <h3>净值走势（最近约 1 小时）</h3>
        <canvas id="pCurve"></canvas>
      </section>

      <section class="profile-section">
        <h3>最近交易${p.trade_total > (p.trades || []).length
          ? `（共 ${p.trade_total} 笔，显示最近 ${(p.trades || []).length} 笔）` : ""}</h3>
        <div class="table-wrap">
          <table>
            <thead>
              <tr><th>时间</th><th>货币对</th><th>方向</th><th>投入</th><th>杠杆</th><th>盈亏</th><th>结算</th></tr>
            </thead>
            <tbody>${rows || `<tr><td colspan="7" class="empty">还没有已平仓的交易</td></tr>`}</tbody>
          </table>
        </div>
      </section>

      ${tips.length ? `<p class="hint">${tips.map(esc).join("<br>")}</p>` : ""}`;

    drawCurve($("#pCurve"), p.curve || [], p.principal || 0);
  }

  function drawCurve(canvas, points, base) {
    if (!canvas) return;

    const rect = canvas.getBoundingClientRect();
    const W = Math.floor(rect.width);
    const H = Math.floor(rect.height);
    if (W < 20 || H < 20) return;

    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);

    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    if (!points || points.length < 2) {
      ctx.fillStyle = "#64748b";
      ctx.font = "12px ui-monospace, monospace";
      ctx.fillText("净值快照不足，多玩一会儿就有了", 10, H / 2);
      return;
    }

    const vals = points.map((x) => Number(x.equity) || 0);
    let lo = Math.min(...vals, base || Infinity);
    let hi = Math.max(...vals, base || -Infinity);
    const span = hi - lo || 1;
    lo -= span * 0.12;
    hi += span * 0.12;

    const padL = 8, padR = 8, padT = 12, padB = 18;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;
    const X = (i) => padL + (i / (vals.length - 1)) * plotW;
    const Y = (v) => padT + (1 - (v - lo) / (hi - lo)) * plotH;

    /* 本金线 */
    if (base > 0) {
      const by = Math.round(Y(base)) + 0.5;
      ctx.save();
      ctx.setLineDash([3, 4]);
      ctx.strokeStyle = "rgba(148,163,184,.45)";
      ctx.beginPath();
      ctx.moveTo(padL, by);
      ctx.lineTo(padL + plotW, by);
      ctx.stroke();
      ctx.restore();
    }

    const rising = vals[vals.length - 1] >= vals[0];
    const color = rising ? "#22c55e" : "#f43f5e";

    const grad = ctx.createLinearGradient(0, padT, 0, padT + plotH);
    grad.addColorStop(0, rising ? "rgba(34,197,94,.25)" : "rgba(244,63,94,.25)");
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

    ctx.fillStyle = "#475569";
    ctx.font = "10px ui-monospace, monospace";
    ctx.textBaseline = "alphabetic";
    ctx.fillText(`¥${money(hi)}`, padL, 10);
    ctx.fillText(`¥${money(lo)}`, padL, H - 4);
    ctx.textAlign = "right";
    ctx.fillText(`¥${money(vals[vals.length - 1])}`, padL + plotW, H - 4);
    ctx.textAlign = "left";
  }

  /* ---------------- 排行榜 ---------------- */

  async function loadBoard() {
    try {
      const data = await api("/api/leaderboard");

      const rows = data.rows.map((r) => `
        <tr class="${r.me ? "is-me" : ""}">
          <td class="num ${r.rank <= 3 ? `rank-${r.rank}` : "dim"}">${r.rank}</td>
          <td class="player"><span data-user="${esc(r.name)}">${esc(r.name)}</span>${
            r.me ? '<span class="tag-you">你</span>' : ""
          }</td>
          <td class="num">¥${money(r.equity)}</td>
          <td class="num dim">¥${money(r.best)}</td>
          <td class="num ${r.return_pct >= 0 ? "up" : "down"}">${
            r.return_pct >= 0 ? "+" : ""
          }${r.return_pct.toFixed(1)}%</td>
          <td class="${r.status === "blown" ? "badge-blown" : "dim"}">${
            r.status === "blown" ? "爆仓" : "存活"
          }</td>
        </tr>`).join("");

      $("#leaderboardBody").innerHTML =
        rows || `<tr><td colspan="6" class="empty">还没有人上榜</td></tr>`;

      $("#leaderboardMeta").textContent =
        `共 ${data.total} 人` + (data.me ? ` · 我的排名 #${data.me}` : "");
        if (!$("#profile").hidden && profileName) refreshProfile();
    } catch (e) {
      /* 排行榜失败不打扰玩家，下一轮自动重试 */
    }
  }

  async function refreshProfile() {
    const body = $("#profileBody");
    const top = body ? body.scrollTop : 0;
    try {
      const p = await api(`/api/users/${encodeURIComponent(profileName)}`);
      if (profileName !== p.name) return;
      renderProfile(p);
      if (body) body.scrollTop = top;
    } catch (e) {
      /* 静默失败 */
    }
  }

  /* ---------------- 图表 ---------------- */

    function drawChart() {
    const canvas = $("#chart");
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

    const UP = "#22c55e";
    const DOWN = "#f43f5e";
    const padL = 10, padR = 74, padT = 12, padB = 22;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;
    if (plotW < 20 || plotH < 20) return;

    if (candles.length < 2) {
      ctx.fillStyle = "#64748b";
      ctx.font = "13px ui-monospace, monospace";
      ctx.textBaseline = "alphabetic";
      ctx.fillText("等待行情数据…", padL, padT + 20);
      return;
    }

    const n = candles.length;
    let lo = Infinity;
    let hi = -Infinity;
    for (const c of candles) {
      if (c.l < lo) lo = c.l;
      if (c.h > hi) hi = c.h;
    }
    const span = hi - lo || Math.max(hi * 0.0008, 0.001);
    lo -= span * 0.10;
    hi += span * 0.10;

    const slot = plotW / n;                       // 每根蜡烛占的宽度
    const X = (i) => padL + (i + 0.5) * slot;     // 蜡烛中心
    const Y = (v) => padT + (1 - (v - lo) / (hi - lo)) * plotH;

    /* ---- 网格与价格刻度 ---- */
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

    /* ---- 蜡烛 ---- */
    const bodyW = Math.max(1, Math.min(slot * 0.66, 16));

    for (let i = 0; i < n; i++) {
      const c = candles[i];
      const x = Math.round(X(i)) + 0.5;           // +0.5 让 1px 线不发虚
      const rising = c.c >= c.o;
      const color = rising ? UP : DOWN;

      ctx.strokeStyle = color;
      ctx.fillStyle = color;

      /* 上下影线 */
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, Y(c.h));
      ctx.lineTo(x, Y(c.l));
      ctx.stroke();

      /* 实体：至少 1px 高，否则十字星会消失 */
      const yO = Y(c.o);
      const yC = Y(c.c);
      const top = Math.min(yO, yC);
      const height = Math.max(1, Math.abs(yC - yO));
      ctx.fillRect(x - bodyW / 2, top, bodyW, height);
    }

    /* ---- 最新价虚线 + 右侧标签 ---- */
    const last = candles[n - 1];
    const lastY = Y(last.c);
    const lastUp = last.c >= last.o;

    ctx.save();
    ctx.setLineDash([3, 4]);
    ctx.strokeStyle = "rgba(148,163,184,.45)";
    ctx.beginPath();
    ctx.moveTo(padL, Math.round(lastY) + 0.5);
    ctx.lineTo(padL + plotW, Math.round(lastY) + 0.5);
    ctx.stroke();
    ctx.restore();

    const text = price(last.c);
    ctx.font = "600 11px ui-monospace, monospace";
    const tw = ctx.measureText(text).width + 12;
    roundRect(ctx, W - tw - 8, lastY - 9, tw, 18, 4);
    ctx.fillStyle = lastUp ? UP : DOWN;
    ctx.fill();
    ctx.fillStyle = "#06121a";
    ctx.textAlign = "center";
    ctx.fillText(text, W - tw / 2 - 8, lastY);
    ctx.textAlign = "left";

    /* ---- 当前品种的持仓成本线 ---- */
    state.positions
      .filter((p) => p.pair === pair)
      .forEach((p) => {
        if (p.entry < lo || p.entry > hi) return;
        const y = Y(p.entry);

        ctx.save();
        ctx.setLineDash([4, 4]);
        ctx.strokeStyle = p.side > 0 ? "rgba(34,197,94,.75)" : "rgba(244,63,94,.75)";
        ctx.beginPath();
        ctx.moveTo(padL, y);
        ctx.lineTo(padL + plotW, y);
        ctx.stroke();
        ctx.restore();

        ctx.fillStyle = "rgba(226,232,240,.8)";
        ctx.font = "10px ui-monospace, monospace";
        ctx.textBaseline = "alphabetic";
        ctx.fillText(
          `${p.side > 0 ? "做多" : "做空"} ¥${money(p.amount)} ${levText(p.leverage)}`,
          padL + 6,
          y < padT + 16 ? y + 13 : y - 6
        );
      });

    /* ---- 底部时间提示 ---- */
    ctx.fillStyle = "#475569";
    ctx.font = "10px ui-monospace, monospace";
    ctx.textBaseline = "alphabetic";
    ctx.textAlign = "left";
    ctx.fillText(`-${((n * candleTicks) / 60).toFixed(1)} 小时`, padL, H - 6);
    ctx.textAlign = "right";
    ctx.fillText(`现在 · 每根 ${candleTicks} 分钟`, padL + plotW, H - 6);
    ctx.textAlign = "left";
  }

  /* ---------------- 事件 ---------------- */

  function onClick(e) {
    const userEl = e.target.closest("[data-user]");
    if (userEl) return openProfile(userEl.dataset.user);
    const pairBtn = e.target.closest("[data-pair]");
    if (pairBtn) return switchPair(pairBtn.dataset.pair);

    const levBtn = e.target.closest("[data-lev]");
    if (levBtn) {
      lev = Number(levBtn.dataset.lev);
      localStorage.setItem("fx_lev", String(lev));
      renderLeverage();
      updateOrderPanel();
      return;
    }

    const amountBtn = e.target.closest("[data-amount]");
    if (amountBtn) {
      $("#amount").value = amountBtn.dataset.amount;
      updateOrderPanel();
      return;
    }

    const closeBtn = e.target.closest("[data-close]");
    if (closeBtn) return closePosition(Number(closeBtn.dataset.close));
  }

  function watchResize() {
    const wrap = document.querySelector(".chart-wrap");
    if (!wrap) return;
    if ("ResizeObserver" in window) {
      new ResizeObserver(() => drawChart()).observe(wrap);
    } else {
      window.addEventListener("resize", drawChart);
    }
  }

  function init() {
    $("#loginForm").addEventListener("submit", (e) => {
      e.preventDefault();
      auth("login");
    });
    $("#registerBtn").addEventListener("click", () => auth("register"));

    $("#meBtn").addEventListener("click", () => {
      if (myName) openProfile(myName);
      else toast("先登录才能看自己的主页");
    });
    $("#pClose").addEventListener("click", () => closeProfile(true));
    $("#profile").addEventListener("click", (e) => {
      if (e.target.id === "profile") closeProfile(true);   // 点遮罩关闭
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !$("#profile").hidden) closeProfile(true);
    });
    window.addEventListener("hashchange", syncFromHash);

    $("#orderForm").addEventListener("submit", (e) => e.preventDefault());
    $("#game").addEventListener("click", onClick);
    $("#amount").addEventListener("input", updateOrderPanel);

    $("#longBtn").addEventListener("click", () => submitOrder(1));
    $("#shortBtn").addEventListener("click", () => submitOrder(-1));

    $("#topupBtn").addEventListener("click", deposit);
    $("#closeAllBtn").addEventListener("click", closeAll);
    $("#topupAmount").addEventListener("input", (e) => {
      e.target.dataset.auto = e.target.value === "" ? "1" : "0";
    });
    const sBtn = $("#soundBtn");
    if (sBtn) {
      const sync = () => {
        const on = !!window.FxSound?.enabled;
        sBtn.textContent = on ? "声音：开" : "声音：关";
        sBtn.classList.toggle("is-muted", !on);
      };
      sync();
      sBtn.addEventListener("click", () => {
        window.FxSound?.toggle();
        sync();
      });
    }
    $("#moreTradesBtn").addEventListener("click", () => {
      tradeLimit += TRADE_PAGE;
      loadTrades(false);
    });
    $("#resetBtn").addEventListener("click", resetAccount);
    $("#logoutBtn").addEventListener("click", () => {
      if (confirm("退出登录？本地会清除登录状态。")) clearSession();
    });

    watchResize();

    if (token) boot();
  }

  document.addEventListener("DOMContentLoaded", init);
})();