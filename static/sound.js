/* 音效：全部由 Web Audio 现场合成，不需要任何音频文件 */
(() => {
  "use strict";

  const KEY = "fx_sound";

  let ctx = null;
  let master = null;
  let enabled = localStorage.getItem(KEY) !== "0";
  let lastAt = 0;

  const now = () => (ctx ? ctx.currentTime : 0);

  /* ---------------- 底层 ---------------- */

  function ensure() {
    if (ctx) return ctx;
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    try {
      ctx = new AC();
      master = ctx.createGain();
      master.gain.value = 0.55;
      master.connect(ctx.destination);
    } catch (e) {
      ctx = null;
    }
    return ctx;
  }

  function unlock() {
    const c = ensure();
    if (!c) return;
    if (c.state === "suspended") c.resume().catch(() => {});
  }

  function gate(ms) {
    const t = Date.now();
    if (t - lastAt < ms) return false;
    lastAt = t;
    return true;
  }

  /* 单个音符：正弦/三角/方波/锯齿，可做频率滑音 */
  function tone({ freq = 880, to = null, at = 0, dur = 0.12,
                  type = "sine", vol = 0.22 } = {}) {
    if (!enabled) return;
    const c = ensure();
    if (!c) return;

    const t0 = now() + at;
    const osc = c.createOscillator();
    const g = c.createGain();

    osc.type = type;
    osc.frequency.setValueAtTime(freq, t0);
    if (to && to !== freq) {
      osc.frequency.exponentialRampToValueAtTime(Math.max(30, to), t0 + dur);
    }

    g.gain.setValueAtTime(0.0001, t0);
    g.gain.linearRampToValueAtTime(vol, t0 + 0.008);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);

    osc.connect(g).connect(master);
    osc.start(t0);
    osc.stop(t0 + dur + 0.04);
  }

  /* 带通扫频噪声：用来模拟"唰"的一下 */
  function noise({ at = 0, dur = 0.2, vol = 0.12, from = 1200, to = 300 } = {}) {
    if (!enabled) return;
    const c = ensure();
    if (!c) return;

    const t0 = now() + at;
    const len = Math.max(1, Math.floor(c.sampleRate * dur));
    const buf = c.createBuffer(1, len, c.sampleRate);
    const data = buf.getChannelData(0);
    for (let i = 0; i < len; i++) data[i] = Math.random() * 2 - 1;

    const src = c.createBufferSource();
    src.buffer = buf;

    const bp = c.createBiquadFilter();
    bp.type = "bandpass";
    bp.Q.value = 1.2;
    bp.frequency.setValueAtTime(from, t0);
    bp.frequency.exponentialRampToValueAtTime(Math.max(80, to), t0 + dur);

    const g = c.createGain();
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.linearRampToValueAtTime(vol, t0 + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);

    src.connect(bp).connect(g).connect(master);
    src.start(t0);
    src.stop(t0 + dur + 0.03);
  }

  /* ---------------- 对外接口 ---------------- */

  const api = {
    get enabled() { return enabled; },

    unlock,

    toggle() {
      enabled = !enabled;
      localStorage.setItem(KEY, enabled ? "1" : "0");
      if (enabled) { unlock(); api.ok(); }
      return enabled;
    },

    /* 新闻：按强度分四档 */
    news(size) {
      if (!enabled || !gate(400)) return;
      unlock();

      switch (size) {
        case "xxl":
          tone({ freq: 130, to: 68, dur: 0.55, type: "triangle", vol: 0.3 });
          tone({ freq: 520, to: 250, dur: 0.22, type: "sawtooth", vol: 0.18 });
          tone({ freq: 520, to: 250, dur: 0.22, type: "sawtooth", vol: 0.18, at: 0.3 });
          noise({ dur: 0.5, vol: 0.14, from: 1800, to: 200 });
          break;

        case "large":
          tone({ freq: 587, dur: 0.1, type: "triangle", vol: 0.2 });
          tone({ freq: 784, dur: 0.1, type: "triangle", vol: 0.2, at: 0.1 });
          tone({ freq: 1046, dur: 0.16, type: "triangle", vol: 0.22, at: 0.2 });
          noise({ at: 0.2, dur: 0.18, vol: 0.07, from: 2400, to: 900 });
          break;

        case "mid":
          tone({ freq: 784, dur: 0.09, vol: 0.18 });
          tone({ freq: 1046, dur: 0.12, vol: 0.2, at: 0.1 });
          break;

        default:
          tone({ freq: 1046, dur: 0.09, vol: 0.14 });
      }
    },

    /* 维持率进入预警区 */
    warn() {
      if (!enabled || !gate(1500)) return;
      unlock();
      tone({ freq: 440, dur: 0.1, type: "square", vol: 0.11 });
      tone({ freq: 330, dur: 0.16, type: "square", vol: 0.11, at: 0.13 });
    },

    /* 爆仓 */
    bust() {
      if (!enabled || !gate(1500)) return;
      unlock();
      tone({ freq: 400, to: 78, dur: 0.7, type: "sawtooth", vol: 0.2 });
      noise({ dur: 0.6, vol: 0.12, from: 900, to: 120 });
    },

    /* 下单 / 平仓成功 */
    ok() {
      if (!enabled) return;
      unlock();
      tone({ freq: 1318, dur: 0.07, vol: 0.1 });
    },
  };

  window.FxSound = api;

  /* 首次交互解锁（浏览器自动播放策略） */
  const once = () => {
    unlock();
    document.removeEventListener("pointerdown", once);
    document.removeEventListener("keydown", once);
  };
  document.addEventListener("pointerdown", once, { passive: true });
  document.addEventListener("keydown", once);
})();