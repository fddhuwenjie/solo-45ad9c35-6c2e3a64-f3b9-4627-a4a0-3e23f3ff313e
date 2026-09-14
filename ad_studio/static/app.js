/* 无障碍音频描述配轨校审 —— 前端(原生 JS + Web Audio + Canvas) */
"use strict";

// ---------------------------------------------------------------- 状态
const S = {
  projectId: null,
  source: null,          // {name,duration,framerate,channels,peaks}
  narrations: [],        // [{id,name,duration}]
  dialogue: [], scenes: [], descriptions: [], keysounds: [],
  placements: {},        // descId -> {narration_id, start, duck, gain, accepted}
  settings: { maxRate: 5.5, minGap: 0.4, duck_to: 0.35, duck_pad: 0.15 },
  issues: [],
  undoStack: [],
  pxPerSec: 60,
  playhead: 0,
  loopRegion: null,      // {a,b,issueIdx}
};
const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------- 工具
function fmtTC(t) {
  t = Math.max(0, t);
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  const pad = (n, w = 2) => String(n).padStart(w, "0");
  return `${pad(h)}:${pad(m)}:${pad(Math.floor(s))}.${pad(Math.round((s % 1) * 1000), 3)}`;
}
function parseTC(v) {
  if (typeof v === "number") return v;
  const s = String(v).trim().replace(",", ".");
  const p = s.split(":");
  if (p.length === 1) return parseFloat(p[0]) || 0;
  if (p.length === 2) return parseInt(p[0]) * 60 + parseFloat(p[1]);
  return parseInt(p[0]) * 3600 + parseInt(p[1]) * 60 + parseFloat(p[2]);
}
async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).error || msg; } catch (e) {}
    throw new Error(msg);
  }
  return r.json();
}
function toast(msg) { $("mixStatus").textContent = msg; setTimeout(() => { if ($("mixStatus").textContent === msg) $("mixStatus").textContent = ""; }, 5000); }

// ---------------------------------------------------------------- 项目载入
async function refreshProjects(selectId) {
  const list = await api("/api/projects");
  const sel = $("projSelect");
  sel.innerHTML = "";
  for (const p of list) {
    const o = document.createElement("option");
    o.value = p.id; o.textContent = `#${p.id} ${p.name}`;
    sel.appendChild(o);
  }
  if (selectId) sel.value = selectId;
  return list;
}
async function loadProject(pid) {
  const st = await api(`/api/project/${pid}/state`);
  S.projectId = pid;
  S.source = st.source;
  S.narrations = st.narrations;
  S.dialogue = st.dialogue; S.scenes = st.scenes;
  S.descriptions = st.descriptions; S.keysounds = st.keysounds;
  S.placements = st.placements.placements || {};
  Object.assign(S.settings, st.placements.settings || {});
  S.issues = []; S.undoStack = []; S.loopRegion = null; S.playhead = 0;
  AudioEngine.reset();
  $("setRate").value = S.settings.maxRate;
  $("setMinGap").value = S.settings.minGap;
  $("setDuckTo").value = S.settings.duck_to;
  renderAssets(); renderDescList(); resizeCanvas(); runChecks(); renderRevisions(st.revisions);
  ["btnExportScript", "btnExportReplay"].forEach(id => $(id).disabled = false);
  $("btnExportMix").disabled = false;
}
async function savePlacements() {
  if (!S.projectId) return;
  await api(`/api/project/${S.projectId}/placements`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ placements: S.placements, settings: S.settings }),
  });
}

// ---------------------------------------------------------------- 素材面板
function renderAssets() {
  $("sourceInfo").textContent = S.source
    ? `${S.source.name} · ${S.source.duration.toFixed(3)}s · ${S.source.framerate}Hz · ${S.source.channels}ch`
    : "未载入";
  const nl = $("narrList");
  nl.innerHTML = "";
  for (const n of S.narrations) {
    const li = document.createElement("li");
    li.innerHTML = `<span>${n.name}</span><span class="mono">${n.duration.toFixed(2)}s</span>`;
    nl.appendChild(li);
  }
  const counts = [["对白", S.dialogue.length], ["场景", S.scenes.length],
                  ["描述", S.descriptions.length], ["关键声", S.keysounds.length]];
  $("scriptInfo").textContent = counts.map(([k, v]) => `${k} ${v} 条`).join(" · ");
}

$("fileSource").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (!f || !S.projectId) return;
  const buf = await f.arrayBuffer();
  await api(`/api/project/${S.projectId}/source?name=${encodeURIComponent(f.name)}`,
    { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: buf });
  await loadProject(S.projectId);
});
$("fileNarr").addEventListener("change", async (e) => {
  for (const f of e.target.files) {
    const buf = await f.arrayBuffer();
    await api(`/api/project/${S.projectId}/narration?name=${encodeURIComponent(f.name)}`,
      { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: buf });
  }
  await loadProject(S.projectId);
});

let pendingScriptKind = null;
document.querySelectorAll(".script-btns button").forEach(b =>
  b.addEventListener("click", () => { pendingScriptKind = b.dataset.kind; $("fileScript").click(); }));
$("fileScript").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (!f || !pendingScriptKind || !S.projectId) return;
  const items = JSON.parse(await f.text());
  await api(`/api/project/${S.projectId}/script`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind: pendingScriptKind, items }),
  });
  e.target.value = "";
  await loadProject(S.projectId);
});

// ---------------------------------------------------------------- 描述卡列表
function narrDur(p) {
  if (!p || !p.narration_id) return null;
  const n = S.narrations.find(x => String(x.id) === String(p.narration_id));
  return n ? n.duration : null;
}
function estDur(d) {
  const rate = d.abridged ? S.settings.maxRate : S.settings.maxRate * 0.8;
  return Math.max(0.8, (d.text || "").length / rate);
}
function cardDur(d) {
  const p = S.placements[d.id];
  return narrDur(p) || estDur(d);
}
function ensurePlacement(d) {
  if (!S.placements[d.id]) {
    S.placements[d.id] = { narration_id: null, start: d.start || 0, duck: false, gain: 1.0 };
  }
  return S.placements[d.id];
}

function renderDescList() {
  const box = $("descList");
  box.innerHTML = "";
  for (const d of S.descriptions) {
    const p = S.placements[d.id];
    const div = document.createElement("div");
    div.className = "desc-item";
    const dur = cardDur(d);
    const rate = (d.text || "").length / dur;
    const badge = p && p.narration_id
      ? `<span class="badge ok">旁白 ${dur.toFixed(2)}s</span>`
      : `<span class="badge warn">估算 ${dur.toFixed(2)}s</span>`;
    div.innerHTML = `
      <div class="text"><b>${d.id}</b> ${d.text || ""} ${badge}
        <span class="badge ${rate > S.settings.maxRate ? "bad" : "ok"}">${rate.toFixed(1)}字/s</span></div>
      <div class="row">
        <label>旁白 <select class="narr-sel"><option value="">(未绑定)</option></select></label>
        <label>起点 <input class="tc mono" value="${fmtTC(p ? p.start : (d.start || 0))}"></label>
        <label><input type="checkbox" class="duck"> 局部压低原声</label>
        <label><input type="checkbox" class="abridged" ${d.abridged ? "checked" : ""}> 缩写稿</label>
        <button class="auto-place">自动找空档</button>
      </div>`;
    const sel = div.querySelector(".narr-sel");
    for (const n of S.narrations) {
      const o = document.createElement("option");
      o.value = n.id; o.textContent = `${n.name} (${n.duration.toFixed(2)}s)`;
      sel.appendChild(o);
    }
    if (p && p.narration_id) sel.value = p.narration_id;
    sel.addEventListener("change", () => {
      pushUndo();
      const pl = ensurePlacement(d);
      pl.narration_id = sel.value || null;
      afterEdit();
    });
    const tc = div.querySelector(".tc");
    tc.addEventListener("change", () => {
      pushUndo();
      ensurePlacement(d).start = Math.max(0, parseTC(tc.value));
      afterEdit();
    });
    const duck = div.querySelector(".duck");
    duck.checked = !!(p && p.duck);
    duck.addEventListener("change", () => { pushUndo(); ensurePlacement(d).duck = duck.checked; afterEdit(); });
    div.querySelector(".abridged").addEventListener("change", (ev) => {
      pushUndo(); d.abridged = ev.target.checked; afterEdit();
    });
    div.querySelector(".auto-place").addEventListener("click", () => {
      pushUndo();
      const t = findGap(d);
      if (t == null) toast("找不到足够空档,请缩短文本或允许压低");
      else ensurePlacement(d).start = t;
      afterEdit();
    });
    box.appendChild(div);
  }
}

function afterEdit() {
  renderDescList(); draw(); runChecks(); savePlacements(); AudioEngine.invalidateMix();
}
function pushUndo() {
  S.undoStack.push(JSON.stringify({ p: S.placements, d: S.descriptions.map(x => ({ id: x.id, abridged: x.abridged })) }));
  if (S.undoStack.length > 50) S.undoStack.shift();
}
$("btnUndo").addEventListener("click", () => {
  const s = S.undoStack.pop();
  if (!s) return;
  const o = JSON.parse(s);
  S.placements = o.p;
  for (const dd of o.d) { const t = S.descriptions.find(x => x.id === dd.id); if (t) t.abridged = dd.abridged; }
  afterEdit();
});

// ---------------------------------------------------------------- 波形时间轴
const canvas = $("wave");
const ctx2d = canvas.getContext("2d");
const LANES = { scene: 0, key: 1, dlg: 2, wave: 3, card: 4 };
const LANE_H = { top: 46, dlg: 26, wave: 110, card: 46, bottom: 8 };
const CANVAS_H = LANE_H.top + LANE_H.dlg + LANE_H.wave + LANE_H.card + LANE_H.bottom;

function t2x(t) { return t * S.pxPerSec; }
function x2t(x) { return x / S.pxPerSec; }

function resizeCanvas() {
  const dur = S.source ? S.source.duration : 60;
  const w = Math.max(600, Math.ceil(dur * S.pxPerSec) + 40);
  const dpr = window.devicePixelRatio || 1;
  canvas.style.width = w + "px";
  canvas.style.height = CANVAS_H + "px";
  canvas.width = w * dpr;
  canvas.height = CANVAS_H * dpr;
  ctx2d.setTransform(dpr, 0, 0, dpr, 0, 0);
  draw();
}

function draw() {
  const w = canvas.width / (window.devicePixelRatio || 1);
  ctx2d.clearRect(0, 0, w, CANVAS_H);
  if (!S.source) {
    ctx2d.fillStyle = "#7f8ca0";
    ctx2d.fillText("请先载入正片 WAV", 20, 40);
    return;
  }
  const dur = S.source.duration;
  const yDlg = LANE_H.top, yWave = yDlg + LANE_H.dlg, yCard = yWave + LANE_H.wave;

  // 时间刻度
  ctx2d.fillStyle = "#5b6878";
  ctx2d.font = "10px monospace";
  const step = S.pxPerSec >= 100 ? 1 : S.pxPerSec >= 50 ? 2 : 5;
  for (let t = 0; t <= dur; t += step) {
    const x = t2x(t);
    ctx2d.fillRect(x, 0, 1, 10);
    ctx2d.fillText(t.toFixed(0) + "s", x + 3, 10);
  }

  // 对白区间
  for (const d of S.dialogue) {
    ctx2d.fillStyle = "rgba(43,74,107,.75)";
    ctx2d.fillRect(t2x(d.start), yDlg, t2x(d.end) - t2x(d.start), LANE_H.dlg - 4);
    ctx2d.fillStyle = "rgba(43,74,107,.25)";
    ctx2d.fillRect(t2x(d.start), yWave, t2x(d.end) - t2x(d.start), LANE_H.wave);
  }
  // 关键声 / 可让位环境声
  for (const k of S.keysounds) {
    ctx2d.fillStyle = k.maskable === false ? "rgba(107,43,43,.8)" : "rgba(43,91,63,.8)";
    ctx2d.fillRect(t2x(k.start), 14, t2x(k.end) - t2x(k.start), LANE_H.top - 20);
    ctx2d.fillStyle = "rgba(255,255,255,.75)";
    ctx2d.font = "10px sans-serif";
    ctx2d.fillText(k.label || "", t2x(k.start) + 2, 22);
    ctx2d.fillStyle = k.maskable === false ? "rgba(255,107,107,.18)" : "rgba(95,214,138,.12)";
    ctx2d.fillRect(t2x(k.start), yWave, t2x(k.end) - t2x(k.start), LANE_H.wave);
  }
  // 场景切点
  ctx2d.fillStyle = "#8a6bd6";
  for (const s of S.scenes) {
    const x = t2x(s.time);
    ctx2d.fillRect(x, 0, 2, yCard + LANE_H.card);
    ctx2d.fillText(s.label || "", x + 4, CANVAS_H - 4);
  }

  // 波形
  const pk = S.source.peaks;
  if (pk && pk.mins.length) {
    const mid = yWave + LANE_H.wave / 2;
    ctx2d.fillStyle = "#4da3ff";
    const n = pk.mins.length;
    for (let i = 0; i < n; i++) {
      const x = (i / n) * t2x(dur);
      const x2 = ((i + 1) / n) * t2x(dur);
      const y1 = mid - pk.maxs[i] * (LANE_H.wave / 2 - 4);
      const y2 = mid - pk.mins[i] * (LANE_H.wave / 2 - 4);
      ctx2d.fillRect(x, y1, Math.max(1, x2 - x), Math.max(1, y2 - y1));
    }
  }

  // 冲突区间高亮
  for (const it of S.issues) {
    if (!it.region) continue;
    ctx2d.fillStyle = "rgba(255,107,107,.22)";
    ctx2d.fillRect(t2x(it.region[0]), yWave, t2x(it.region[1]) - t2x(it.region[0]), LANE_H.wave + LANE_H.card);
  }
  // 循环区间
  if (S.loopRegion) {
    ctx2d.strokeStyle = "#ffb84d";
    ctx2d.lineWidth = 2;
    ctx2d.strokeRect(t2x(S.loopRegion.a), yWave - 2, t2x(S.loopRegion.b) - t2x(S.loopRegion.a), LANE_H.wave + LANE_H.card + 4);
    ctx2d.lineWidth = 1;
  }

  // 描述卡
  for (const d of S.descriptions) {
    const p = S.placements[d.id];
    if (!p) continue;
    const cd = cardDur(d);
    const x = t2x(p.start), wdt = Math.max(14, t2x(cd));
    const hasIssue = S.issues.some(i => i.descId === d.id);
    ctx2d.fillStyle = hasIssue ? "rgba(255,107,107,.9)" : "rgba(77,163,255,.9)";
    ctx2d.fillRect(x, yCard, wdt, LANE_H.card - 8);
    if (!p.narration_id) {  // 未绑定旁白: 斜纹提示估算
      ctx2d.fillStyle = "rgba(0,0,0,.35)";
      for (let sx = x; sx < x + wdt; sx += 6) ctx2d.fillRect(sx, yCard, 2, LANE_H.card - 8);
    }
    ctx2d.fillStyle = "#06121f";
    ctx2d.font = "11px sans-serif";
    ctx2d.fillText(d.id, x + 3, yCard + 13);
    if (p.duck) {
      ctx2d.fillStyle = "#ffb84d";
      ctx2d.fillText("压", x + 3, yCard + 27);
    }
  }

  // 播放头
  ctx2d.fillStyle = "#fff";
  ctx2d.fillRect(t2x(S.playhead), 0, 1.5, CANVAS_H);
}

// 拖动与寻址
let drag = null; // {descId, offsetX} | 'seek'
function hitCard(x, y) {
  const yCard = LANE_H.top + LANE_H.dlg + LANE_H.wave;
  if (y < yCard || y > yCard + LANE_H.card) return null;
  for (const d of S.descriptions) {
    const p = S.placements[d.id];
    if (!p) continue;
    const cx = t2x(p.start), cw = Math.max(14, t2x(cardDur(d)));
    if (x >= cx && x <= cx + cw) return d;
  }
  return null;
}
canvas.addEventListener("mousedown", (e) => {
  const r = canvas.getBoundingClientRect();
  const x = e.clientX - r.left, y = e.clientY - r.top;
  const d = hitCard(x, y);
  if (d) {
    pushUndo();
    drag = { descId: d.id, off: x - t2x(S.placements[d.id].start) };
  } else {
    drag = "seek";
    S.playhead = Math.min(Math.max(0, x2t(x)), S.source ? S.source.duration : 0);
    draw();
  }
});
window.addEventListener("mousemove", (e) => {
  if (!drag) return;
  const r = canvas.getBoundingClientRect();
  const x = e.clientX - r.left;
  if (drag === "seek") {
    S.playhead = Math.min(Math.max(0, x2t(x)), S.source.duration);
    draw();
  } else {
    const p = S.placements[drag.descId];
    p.start = Math.max(0, Math.round(x2t(x - drag.off) * 20) / 20); // 0.05s 吸附
    draw();
  }
});
window.addEventListener("mouseup", () => {
  if (drag && drag !== "seek") afterEdit();
  drag = null;
});
$("zoom").addEventListener("input", () => { S.pxPerSec = +$("zoom").value; resizeCanvas(); });

// ---------------------------------------------------------------- 校审
const overlap = (a0, a1, b0, b1) => Math.max(0, Math.min(a1, b1) - Math.max(a0, b0));

function runChecks() {
  const issues = [];
  const placed = S.descriptions.filter(d => S.placements[d.id]);
  const intervals = [];
  for (const d of placed) {
    const p = S.placements[d.id];
    const dur = cardDur(d);
    const t0 = p.start, t1 = t0 + dur;
    intervals.push({ d, t0, t1 });
    const rate = (d.text || "").length / dur;

    // 1. 语速
    if (rate > S.settings.maxRate && !p.accepted) {
      issues.push({ type: "rate", descId: d.id, sev: "warn", region: [t0, t1],
        title: `语速过快 · ${d.id}`,
        detail: `${(d.text || "").length} 字 / ${dur.toFixed(2)}s = ${rate.toFixed(1)} 字/s,超过 ${S.settings.maxRate}。盲人观众跟听困难。`,
        solutions: ["abridge", "autoMove", "accept"] });
    }
    // 2. 对白重叠
    for (const g of S.dialogue) {
      const ov = overlap(t0, t1, g.start, g.end);
      if (ov > 0.05 && !p.accepted) {
        issues.push({ type: "dialogue", descId: d.id, sev: p.duck ? "warn" : "bad",
          region: [Math.max(t0, g.start), Math.min(t1, g.end)],
          title: `对白重叠 · ${d.id} × ${g.speaker || "对白"}`,
          detail: `旁白压住对白 ${ov.toFixed(2)}s(“${(g.text || "").slice(0, 18)}…”),会漏掉情节线索。${p.duck ? "已压低原声,仍需确认对白清晰度。" : ""}`,
          solutions: ["autoMove", "duck", "abridge", "accept"] });
      }
    }
    // 3. 关键声遮蔽
    for (const k of S.keysounds) {
      if (k.maskable !== false) continue;
      const ov = overlap(t0, t1, k.start, k.end);
      if (ov > 0.03 && !p.accepted) {
        issues.push({ type: "keysound", descId: d.id, sev: "bad",
          region: [Math.max(t0, k.start), Math.min(t1, k.end)],
          title: `关键声遮蔽 · ${d.id} × ${k.label}`,
          detail: `旁白遮盖不可遮盖关键声“${k.label}” ${ov.toFixed(2)}s,情节线索将丢失。`,
          solutions: ["autoMove", "accept"] });
      }
    }
    // 4. 旁白时长 vs 可用空档
    if (p.narration_id) {
      const nextDlg = S.dialogue.filter(g => g.start >= t1 - 0.001 || g.end > t0)
        .filter(g => g.end > t0).sort((a, b) => a.start - b.start)
        .find(g => g.start >= t0);
      const gapEnd = nextDlg ? nextDlg.start : (S.source ? S.source.duration : t1);
      if (t1 > gapEnd + 0.05 && !p.accepted) {
        issues.push({ type: "duration", descId: d.id, sev: "warn", region: [gapEnd, t1],
          title: `旁白超长 · ${d.id}`,
          detail: `片段 ${dur.toFixed(2)}s 超出当前空档(到 ${fmtTC(gapEnd)}) ${(t1 - gapEnd).toFixed(2)}s。`,
          solutions: ["autoMove", "abridge", "accept"] });
      }
    }
    // 5. 跨场景
    for (const s of S.scenes) {
      if (s.time > t0 + 0.02 && s.time < t1 - 0.02 && !p.accepted) {
        issues.push({ type: "scene", descId: d.id, sev: "warn", region: [t0, t1],
          title: `跨场景 · ${d.id} × ${s.label || fmtTC(s.time)}`,
          detail: `描述横跨场景切点 ${fmtTC(s.time)},前后画面不连续,易误导。`,
          solutions: ["autoMove", "abridge", "accept"] });
      }
    }
  }
  // 6. 相邻描述冲突
  intervals.sort((a, b) => a.t0 - b.t0);
  for (let i = 0; i + 1 < intervals.length; i++) {
    const A = intervals[i], B = intervals[i + 1];
    const gap = B.t0 - A.t1;
    if (gap < S.settings.minGap) {
      const pa = S.placements[A.d.id], pb = S.placements[B.d.id];
      if (pa.accepted || pb.accepted) continue;
      issues.push({ type: "adjacent", descId: B.d.id, sev: gap < 0 ? "bad" : "warn",
        region: [A.t0, Math.max(A.t1, B.t0)],
        title: `相邻描述冲突 · ${A.d.id} / ${B.d.id}`,
        detail: gap < 0 ? `两段描述互相重叠 ${(-gap).toFixed(2)}s。` : `间隔仅 ${gap.toFixed(2)}s,小于 ${S.settings.minGap}s,听众来不及消化。`,
        solutions: ["autoMove", "accept"] });
    }
  }
  S.issues = issues;
  renderIssues();
  draw();
}

function renderIssues() {
  const box = $("issueList");
  box.innerHTML = "";
  if (!S.issues.length) {
    box.innerHTML = `<div class="muted" style="padding:6px">✓ 当前无冲突</div>`;
    return;
  }
  S.issues.forEach((it, idx) => {
    const div = document.createElement("div");
    div.className = "issue" + (it.sev === "bad" ? " bad" : "");
    const loopOn = S.loopRegion && S.loopRegion.issueIdx === idx;
    div.innerHTML = `<div class="ttl">${it.title}</div><div class="detail">${it.detail}</div>
      <div class="ops">
        <button class="loop ${loopOn ? "on" : ""}">${loopOn ? "■ 停止循环" : "🔁 循环试听"}</button>
      </div>`;
    const ops = div.querySelector(".ops");
    const labels = { autoMove: "⇢ 移到空档", duck: "🔉 局部压低", abridge: "✂ 缩写稿", accept: "✔ 保留并记录" };
    for (const s of it.solutions) {
      const b = document.createElement("button");
      b.textContent = labels[s];
      const d = S.descriptions.find(x => x.id === it.descId);
      const p = d && S.placements[d.id];
      if (s === "duck" && p && p.duck) b.classList.add("on");
      if (s === "abridge" && d && d.abridged) b.classList.add("on");
      b.addEventListener("click", () => applySolution(it, s));
      ops.appendChild(b);
    }
    div.querySelector(".loop").addEventListener("click", () => {
      if (loopOn) { S.loopRegion = null; AudioEngine.stop(); }
      else {
        const pad = 0.6;
        S.loopRegion = { a: Math.max(0, it.region[0] - pad), b: it.region[1] + pad, issueIdx: idx };
        AudioEngine.play($("playMode").value, S.loopRegion.a, S.loopRegion.b, true);
      }
      renderIssues(); draw();
    });
    box.appendChild(div);
  });
}

function applySolution(it, kind) {
  const d = S.descriptions.find(x => x.id === it.descId);
  if (!d) return;
  pushUndo();
  const p = ensurePlacement(d);
  if (kind === "autoMove") {
    const t = findGap(d);
    if (t == null) toast("没有足够空档,试试缩写或压低");
    else p.start = t;
  } else if (kind === "duck") {
    p.duck = !p.duck;
  } else if (kind === "abridge") {
    d.abridged = !d.abridged;
  } else if (kind === "accept") {
    p.accepted = !p.accepted;
    if (p.accepted) toast("已标记保留,请在右侧记录采用理由");
  }
  afterEdit();
  // 应用方案后自动回放该区域,便于比较
  if (S.loopRegion) AudioEngine.play($("playMode").value, S.loopRegion.a, S.loopRegion.b, true);
}

// 在 [t, t+dur] 是否与对白/关键声/场景线/其他描述冲突
function fitsAt(d, t, dur) {
  const t1 = t + dur;
  for (const g of S.dialogue) if (overlap(t, t1, g.start, g.end) > 0.02) return false;
  for (const k of S.keysounds) if (k.maskable === false && overlap(t, t1, k.start, k.end) > 0.02) return false;
  for (const s of S.scenes) if (s.time > t + 0.02 && s.time < t1 - 0.02) return false;
  for (const o of S.descriptions) {
    if (o.id === d.id) continue;
    const op = S.placements[o.id];
    if (!op) continue;
    const o1 = op.start + cardDur(o);
    if (overlap(t - S.settings.minGap, t1 + S.settings.minGap, op.start, o1) > 0) return false;
  }
  return true;
}
function findGap(d) {
  const dur = cardDur(d);
  const end = S.source ? S.source.duration : 60;
  const cur = S.placements[d.id] ? S.placements[d.id].start : (d.start || 0);
  // 候选起点: 0、当前位置、每个对白/关键声/场景/描述结束之后
  const cands = new Set([0, cur]);
  for (const g of S.dialogue) cands.add(g.end + 0.05);
  for (const k of S.keysounds) cands.add(k.end + 0.05);
  for (const s of S.scenes) cands.add(s.time + 0.05);
  for (const o of S.descriptions) {
    const op = S.placements[o.id];
    if (op && o.id !== d.id) cands.add(op.start + cardDur(o) + S.settings.minGap);
  }
  const sorted = [...cands].filter(t => t >= 0 && t + dur <= end).sort((a, b) => a - b);
  // 优先当前位置之后最近的空档,其次全局最早
  const after = sorted.filter(t => t >= cur - 0.001);
  for (const t of after) if (fitsAt(d, t, dur)) return t;
  for (const t of sorted) if (fitsAt(d, t, dur)) return t;
  return null;
}

// ---------------------------------------------------------------- 播放引擎(Web Audio)
const AudioEngine = {
  ctx: null, srcBuf: null, narrBufs: {}, mixBuf: null, nodes: [], playing: false,
  reset() { this.stop(); this.srcBuf = null; this.narrBufs = {}; this.mixBuf = null; },
  invalidateMix() { this.mixBuf = null; },
  ensureCtx() { if (!this.ctx) this.ctx = new (window.AudioContext || window.webkitAudioContext)(); return this.ctx; },
  async decode(url) {
    const ctx = this.ensureCtx();
    const buf = await (await fetch(url)).arrayBuffer();
    return await ctx.decodeAudioData(buf);
  },
  async loadSource() {
    if (!this.srcBuf && S.projectId && S.source)
      this.srcBuf = await this.decode(`/api/project/${S.projectId}/file?which=source`);
    return this.srcBuf;
  },
  async loadNarr(id) {
    if (!this.narrBufs[id])
      this.narrBufs[id] = await this.decode(`/api/project/${S.projectId}/file?which=narr_${id}`);
    return this.narrBufs[id];
  },
  // 客户端混合预览: 原声 × 压低包络 + 各旁白(与 Python render_mix 同规则)
  async buildMix() {
    if (this.mixBuf) return this.mixBuf;
    const src = await this.loadSource();
    const ctx = this.ensureCtx();
    const sr = src.sampleRate, n = src.length, nch = src.numberOfChannels;
    const out = ctx.createBuffer(nch, n, sr);
    const duckTo = S.settings.duck_to, pad = S.settings.duck_pad, ramp = 0.15;
    const env = new Float32Array(n).fill(1);
    const jobs = [];
    for (const d of S.descriptions) {
      const p = S.placements[d.id];
      if (!p || !p.narration_id) continue;
      const nb = await this.loadNarr(p.narration_id);
      jobs.push({ p, nb });
      if (p.duck) {
        const dur = nb.duration;
        const f0 = Math.max(0, Math.floor((p.start - pad) * sr));
        const f1 = Math.min(n, Math.ceil((p.start + dur + pad) * sr));
        const r = Math.floor(ramp * sr);
        for (let f = f0; f < f1; f++) {
          let g = duckTo;
          if (f - f0 < r) g = 1 - (1 - duckTo) * (f - f0) / r;
          else if (f1 - f < r) g = 1 - (1 - duckTo) * (f1 - f) / r;
          if (g < env[f]) env[f] = g;
        }
      }
    }
    for (let c = 0; c < nch; c++) {
      const sd = src.getChannelData(Math.min(c, src.numberOfChannels - 1));
      const od = out.getChannelData(c);
      for (let i = 0; i < n; i++) od[i] = sd[i] * env[i];
    }
    for (const { p, nb } of jobs) {
      const gain = p.gain == null ? 1 : p.gain;
      const startF = Math.round(p.start * sr);
      for (let c = 0; c < nch; c++) {
        const nd = nb.getChannelData(Math.min(c, nb.numberOfChannels - 1));
        const od = out.getChannelData(c);
        // 采样率不一致时按最近邻对齐(导出仍以服务端线性插值为准)
        const ratio = nb.sampleRate / sr;
        const m = Math.min(nb.length, Math.floor((n - startF) / ratio));
        for (let j = 0; j < m; j++) {
          const idx = startF + j;
          if (idx >= 0 && idx < n) od[idx] += nd[Math.floor(j * ratio)] * gain;
        }
      }
    }
    this.mixBuf = out;
    return out;
  },
  async play(mode, offset = 0, end = null, loop = false) {
    this.stop();
    const ctx = this.ensureCtx();
    if (ctx.state === "suspended") await ctx.resume();
    let buf;
    if (mode === "source") buf = await this.loadSource();
    else if (mode === "mix") buf = await this.buildMix();
    else { // 仅旁白: 逐段调度
      const t0 = ctx.currentTime + 0.05;
      for (const d of S.descriptions) {
        const p = S.placements[d.id];
        if (!p || !p.narration_id) continue;
        const nb = await this.loadNarr(p.narration_id);
        const a = p.start, b = p.start + nb.duration;
        if (end != null && (b < offset || a > end)) continue;
        const src = ctx.createBufferSource();
        src.buffer = nb;
        src.connect(ctx.destination);
        if (loop && end != null) {
          // 循环窗口内: 只取落在窗口里的部分,随主循环重排由 setInterval 复杂化——简化为一次性
        }
        const when = t0 + Math.max(0, a - offset);
        const off = Math.max(0, offset - a);
        const dur = Math.min(nb.duration - off, end != null ? end - Math.max(a, offset) : nb.duration - off);
        if (dur > 0) src.start(when, off, dur);
        this.nodes.push(src);
      }
      this.playing = true;
      this.tick(loop ? offset : null, end);
      return;
    }
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(ctx.destination);
    if (loop && end != null) {
      src.loop = true; src.loopStart = offset; src.loopEnd = end;
      src.start(0, offset);
    } else if (end != null) {
      src.start(0, offset, end - offset);
    } else {
      src.start(0, offset);
    }
    this.nodes.push(src);
    this.playing = true;
    this.tick(offset, loop ? null : end, loop ? end : null);
  },
  tick(offset, end, loopEnd) {
    const ctx = this.ensureCtx();
    const t0 = ctx.currentTime;
    const step = () => {
      if (!this.playing) return;
      let t = offset + (ctx.currentTime - t0);
      if (loopEnd != null && t >= loopEnd) t = offset + ((t - offset) % (loopEnd - offset));
      S.playhead = t;
      if (end != null && t >= end) { this.stop(); return; }
      if (loopEnd == null && S.source && S.playhead > S.source.duration) { this.stop(); return; }
      $("timeLabel").textContent = fmtTC(S.playhead);
      draw();
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  },
  stop() {
    this.playing = false;
    for (const n of this.nodes) { try { n.stop(); } catch (e) {} try { n.disconnect(); } catch (e) {} }
    this.nodes = [];
  },
};

$("btnPlay").addEventListener("click", () => {
  if (!S.source) return;
  S.loopRegion = null;
  renderIssues(); draw();
  AudioEngine.play($("playMode").value, S.playhead);
});
$("btnStop").addEventListener("click", () => { AudioEngine.stop(); S.loopRegion = null; renderIssues(); draw(); });

// ---------------------------------------------------------------- 混音渲染与导出
$("btnRenderMix").addEventListener("click", async () => {
  if (!S.projectId) return;
  await savePlacements();
  toast("渲染中…");
  const r = await api(`/api/project/${S.projectId}/mix`, { method: "POST" });
  toast(`混音完成 ${r.duration.toFixed(2)}s,可导出`);
});
$("btnExportScript").addEventListener("click", async () => {
  await savePlacements();
  location.href = `/api/project/${S.projectId}/export?what=script`;
});
$("btnExportReplay").addEventListener("click", async () => {
  await savePlacements();
  location.href = `/api/project/${S.projectId}/export?what=replay`;
});
$("btnExportMix").addEventListener("click", async () => {
  await savePlacements();
  await api(`/api/project/${S.projectId}/mix`, { method: "POST" }); // 确保是最新 placements
  location.href = `/api/project/${S.projectId}/export?what=mix`;
});

// ---------------------------------------------------------------- 修订
$("btnSaveRev").addEventListener("click", async () => {
  const summary = $("revSummary").value.trim();
  const rationale = $("revRationale").value.trim();
  if (!summary || !rationale) { toast("请填写素材摘要/时码与采用理由"); return; }
  const snapshot = {
    placements: S.placements, settings: S.settings,
    issues: S.issues.map(i => ({ type: i.type, descId: i.descId, title: i.title })),
    assets: {
      source: S.source ? { name: S.source.name, duration: S.source.duration } : null,
      narrations: S.narrations.map(n => ({ id: n.id, name: n.name, duration: n.duration })),
      counts: { dialogue: S.dialogue.length, scenes: S.scenes.length,
                descriptions: S.descriptions.length, keysounds: S.keysounds.length },
    },
  };
  await api(`/api/project/${S.projectId}/revision`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ summary, rationale, snapshot }),
  });
  $("revSummary").value = ""; $("revRationale").value = "";
  const revs = await api(`/api/project/${S.projectId}/revisions`);
  renderRevisions(revs);
  toast("修订已记录");
});
function renderRevisions(revs) {
  const ul = $("revList");
  ul.innerHTML = "";
  for (const r of revs || []) {
    const li = document.createElement("li");
    const d = new Date(r.created * 1000);
    li.innerHTML = `<span class="r-sum">#${r.id} ${r.summary}</span>
      <span class="r-why">${d.toLocaleString()} · 理由:${r.rationale}</span>`;
    ul.appendChild(li);
  }
}

// ---------------------------------------------------------------- 参数与项目切换
$("setRate").addEventListener("change", () => { S.settings.maxRate = +$("setRate").value; afterEdit(); });
$("setMinGap").addEventListener("change", () => { S.settings.minGap = +$("setMinGap").value; afterEdit(); });
$("setDuckTo").addEventListener("change", () => { S.settings.duck_to = +$("setDuckTo").value; afterEdit(); });

$("btnNewProj").addEventListener("click", async () => {
  const name = $("newProjName").value.trim() || "未命名项目";
  const r = await api("/api/project", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  await refreshProjects(r.id);
  await loadProject(r.id);
});
$("btnDemo").addEventListener("click", async () => {
  const r = await api("/api/demo", { method: "POST" });
  await refreshProjects(r.id);
  await loadProject(r.id);
});
$("projSelect").addEventListener("change", () => loadProject(+$("projSelect").value));

// ---------------------------------------------------------------- 启动
(async function init() {
  const list = await refreshProjects();
  if (list.length) await loadProject(list[0].id);
  else resizeCanvas();
})();
