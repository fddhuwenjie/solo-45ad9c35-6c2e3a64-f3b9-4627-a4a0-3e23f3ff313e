/* 无障碍音频描述配轨校审 —— 前端(原生 JS + Web Audio + Canvas) */
"use strict";

// ---------------------------------------------------------------- 状态
const DEFAULT_LV = { targetDb: -20, ceilingDb: -1, maxJumpDb: 3, minSpeech: 0.3,
                     minGainDb: -14, maxGainDb: 15.6 };
const DEFAULT_SPL = { zeroxMs: 2, gapWarnS: 0.05, seamCeilingDb: -1 };
const S = {
  projectId: null,
  source: null,          // {name,duration,framerate,channels,peaks}
  narrations: [],        // [{id,name,duration,levels}]
  dialogue: [], scenes: [], descriptions: [], keysounds: [],
  placements: {},        // descId -> {narration_id, start, duck, gain, accepted}
  settings: { maxRate: 5.5, minGap: 0.4, duck_to: 0.35, duck_pad: 0.15 },
  leveling: { settings: { ...DEFAULT_LV }, items: {} },  // 方案(ranges/gain/status)
  lvReport: null,        // 后端最近一次配平报告
  lvTimer: null,
  splice: { settings: { ...DEFAULT_SPL }, items: {} }, // 拼接方案(takes/anchors/segments)
  spliceReport: null,    // 后端最近一次拼接校审报告
  spliceResolved: {},    // descId -> 确认版合成片段 {id,name,duration,levels,splice}
  spTimer: null,
  abAudio: null,         // {descId, mode, nodes, start, a, b}
  issues: [],
  undoStack: [],
  pxPerSec: 60,
  playhead: 0,
  loopRegion: null,      // {a,b,issueIdx}
};
// 拼接编辑器暂态: did=当前描述卡, sel=候选选区{takeId,a,b}, play=试听节点, draftSel=拖拽中
const spEd = { did: null, sel: null, play: null, drag: null };
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
  S.leveling = { settings: { ...DEFAULT_LV, ...(st.leveling.settings || {}) },
                 items: st.leveling.items || {} };
  S.splice = { settings: { ...DEFAULT_SPL, ...((st.splice || {}).settings || {}) },
               items: (st.splice || {}).items || {} };
  S.spliceResolved = st.spliceResolved || {};
  S.spliceReport = null;
  spEd.did = null; spEd.sel = null; spStop();
  S.lvReport = null; S.abAudio = null;
  S.issues = []; S.undoStack = []; S.loopRegion = null; S.playhead = 0;
  AudioEngine.reset();
  $("setRate").value = S.settings.maxRate;
  $("setMinGap").value = S.settings.minGap;
  $("setDuckTo").value = S.settings.duck_to;
  for (const [k, id] of [["targetDb","lvTarget"],["ceilingDb","lvCeiling"],
      ["maxJumpDb","lvMaxJump"],["minSpeech","lvMinSpeech"],
      ["minGainDb","lvMinGain"],["maxGainDb","lvMaxGain"]])
    $(id).value = S.leveling.settings[k];
  for (const [k, id] of [["zeroxMs","spZerox"],["gapWarnS","spGap"],["seamCeilingDb","spCeil"]])
    $(id).value = S.splice.settings[k];
  renderAssets(); renderDescList(); resizeCanvas(); runChecks(); renderRevisions(st.revisions);
  renderLeveling(); renderSplice();
  ensureLevelCurves().then(computeLeveling);
  if (Object.keys(S.splice.items).length) computeSplice();
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
// 描述卡的有效旁白: 确认版拼接合成片段优先, 否则整段绑定
function effNarrOf(d) {
  const rs = S.spliceResolved[d.id];
  if (rs) return rs;
  const p = S.placements[d.id];
  if (!p || !p.narration_id) return null;
  return S.narrations.find(x => String(x.id) === String(p.narration_id)) || null;
}
function narrDur(p, d) {
  if (d) {
    const n = effNarrOf(d);
    return n ? n.duration : null;
  }
  if (!p || !p.narration_id) return null;
  const n = S.narrations.find(x => String(x.id) === String(p.narration_id));
  return n ? n.duration : null;
}
function estDur(d) {
  const p = S.placements[d.id];
  const abridged = !!(p && p.abridged);
  const rate = abridged ? S.settings.maxRate : S.settings.maxRate * 0.8;
  return Math.max(0.8, (d.text || "").length / rate);
}
// 缩写压缩倍速(>1 缩短); 目标时长 = 字数/目标语速(下限0.8s), 与服务端 abridge_speed 一致
function abridgeFactor(d) {
  const p = S.placements[d.id];
  const nd = narrDur(p, d);
  if (!p || !p.abridged || nd == null) return 1;
  const target = Math.max(0.8, (d.text || "").length / S.settings.maxRate);
  return target < nd ? nd / target : 1;
}
function cardDur(d) {
  const p = S.placements[d.id];
  const nd = narrDur(p, d);
  if (nd != null) return nd / abridgeFactor(d);
  return estDur(d);
}
function ensurePlacement(d) {
  if (!S.placements[d.id]) {
    S.placements[d.id] = { narration_id: null, start: d.start || 0, duck: false, gain: 1.0, abridged: false };
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
    const nd = narrDur(p, d);
    const dur = cardDur(d);
    const rate = (d.text || "").length / dur;
    const spPlan = S.splice.items[d.id];
    const spRes = S.spliceResolved[d.id];
    const badge = spRes
      ? `<span class="badge ok">拼接 ${spRes.splice.segments.length}段 ${dur.toFixed(2)}s</span>`
      : (p && p.narration_id)
        ? (dur < nd - 1e-6
            ? `<span class="badge ok">旁白 ${dur.toFixed(2)}s(缩写自 ${nd.toFixed(2)}s)</span>`
            : `<span class="badge ok">旁白 ${nd.toFixed(2)}s</span>`)
        : `<span class="badge warn">估算 ${dur.toFixed(2)}s</span>`;
    const spBadge = !spRes && spPlan && (spPlan.segments || []).length
      ? `<span class="badge warn">拼接待处理</span>` : "";
    div.innerHTML = `
      <div class="text"><b>${d.id}</b> ${d.text || ""} ${badge}${spBadge}
        <span class="badge ${rate > S.settings.maxRate ? "bad" : "ok"}">${rate.toFixed(1)}字/s</span></div>
      <div class="row">
        <label>旁白 <select class="narr-sel"><option value="">(未绑定)</option></select></label>
        <label>起点 <input class="tc mono" value="${fmtTC(p ? p.start : (d.start || 0))}"></label>
        <label><input type="checkbox" class="duck"> 局部压低原声</label>
        <label><input type="checkbox" class="abridged" ${p && p.abridged ? "checked" : ""}> 缩写稿</label>
        <button class="auto-place">自动找空档</button>
        <button class="go-splice" title="多版本拼接: 挂接多版录音,切分取段合成">🧩 拼接</button>
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
      delete S.leveling.items[d.id];   // 改绑旁白: 旧选区/增益失效
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
      pushUndo(); ensurePlacement(d).abridged = ev.target.checked; afterEdit();
    });
    div.querySelector(".auto-place").addEventListener("click", () => {
      pushUndo();
      const t = findGap(d);
      if (t == null) toast("找不到足够空档,请缩短文本或允许压低");
      else ensurePlacement(d).start = t;
      afterEdit();
    });
    div.querySelector(".go-splice").addEventListener("click", () => {
      spEd.did = d.id;
      renderSplice();
      $("spliceEditor").scrollIntoView({ behavior: "smooth", block: "center" });
    });
    box.appendChild(div);
  }
}

function afterEdit() {
  renderDescList(); draw(); runChecks(); savePlacements(); AudioEngine.invalidateMix();
  renderLeveling(); scheduleLeveling(); renderSplice(); scheduleSplice();
}
function pushUndo() {
  S.undoStack.push(JSON.stringify({ p: S.placements, l: S.leveling.items, s: S.splice.items }));
  if (S.undoStack.length > 50) S.undoStack.shift();
}
$("btnUndo").addEventListener("click", () => {
  const s = S.undoStack.pop();
  if (!s) return;
  const u = JSON.parse(s);
  S.placements = u.p; S.leveling.items = u.l || {}; S.splice.items = u.s || {};
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
  // 7. 响度配平: 相邻描述响度跳变(警告, 非阻塞)
  if (S.lvReport) {
    for (const did of S.lvReport.order) {
      const r = S.lvReport.items[did];
      const jw = r.errors.find(e => e.code === "jump");
      if (jw) {
        issues.push({ type: "loudjump", descId: did, sev: "warn",
          region: [r.start, r.start + r.duration],
          title: `响度跳变 · ${r.jump.prev} → ${did}`,
          detail: jw.msg + "。在响度配平中套建议增益可收敛到目标电平。",
          solutions: ["goLeveling"] });
      }
    }
  }
  // 8. 旁白拼接: 阻塞(锚点倒序/来源缺失/接缝削波/压住对白关键声等)保持待处理
  if (S.spliceReport) {
    for (const did of S.spliceReport.order || []) {
      const r = S.spliceReport.items[did];
      for (const e of r.errors.filter(x => x.sev === "bad")) {
        issues.push({ type: "splice", descId: did, sev: "bad",
          region: [r.start, r.start + Math.max(0.4, r.duration)],
          title: `拼接阻塞 · ${did}`,
          detail: e.msg + "。修正前合成结果不参与配平/混音/导出。",
          solutions: ["goSplice"] });
      }
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
    const labels = { autoMove: "⇢ 移到空档", duck: "🔉 局部压低", abridge: "✂ 缩写稿",
                     accept: "✔ 保留并记录", goLeveling: "🎚 去响度配平", goSplice: "🧩 去旁白拼接" };
    for (const s of it.solutions) {
      const b = document.createElement("button");
      b.textContent = labels[s];
      const d = S.descriptions.find(x => x.id === it.descId);
      const p = d && S.placements[d.id];
      if (s === "duck" && p && p.duck) b.classList.add("on");
      if (s === "abridge" && p && p.abridged) b.classList.add("on");
      b.addEventListener("click", () => applySolution(it, s));
      ops.appendChild(b);
    }
    div.querySelector(".loop").addEventListener("click", () => {
      if (loopOn) { S.loopRegion = null; AudioEngine.stop(); }
      else {
        stopLvPlay();
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
  if (kind === "goLeveling") {
    const el = document.querySelector(`.lv-item[data-did="${it.descId}"]`);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      el.classList.add("flash");
      setTimeout(() => el.classList.remove("flash"), 1600);
    }
    return;
  }
  if (kind === "goSplice") {
    spEd.did = it.descId;
    renderSplice();
    const el = $("spliceEditor");
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    el.classList.add("flash");
    setTimeout(() => el.classList.remove("flash"), 1600);
    return;
  }
  pushUndo();
  const p = ensurePlacement(d);
  if (kind === "autoMove") {
    const t = findGap(d);
    if (t == null) toast("没有足够空档,试试缩写或压低");
    else p.start = t;
  } else if (kind === "duck") {
    p.duck = !p.duck;
  } else if (kind === "abridge") {
    p.abridged = !p.abridged;
  } else if (kind === "accept") {
    p.accepted = !p.accepted;
    if (p.accepted) toast("已标记保留,请在右侧记录采用理由");
  }
  afterEdit();
  // 应用方案后自动回放该区域,便于比较
  if (S.loopRegion) { stopLvPlay(); AudioEngine.play($("playMode").value, S.loopRegion.a, S.loopRegion.b, true); }
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

// ---------------------------------------------------------------- 响度配平
function lvItem(did) {
  if (!S.leveling.items[did])
    S.leveling.items[did] = { ranges: null, gain: null, status: "pending" };
  return S.leveling.items[did];
}
const lvRanges = (did) => {
  const it = S.leveling.items[did];
  const d = S.descriptions.find(x => x.id === did);
  const n = d ? effNarrOf(d) : null;
  const dur = n ? n.duration : 0;
  return (it && it.ranges && it.ranges.length) ? it.ranges : [{ start: 0, end: dur }];
};

async function ensureLevelCurves() {
  if (!S.projectId) return;
  const missing = S.narrations.some(n => !n.levels || !n.levels.peak || !n.levels.peak.length);
  if (missing) {
    await api(`/api/project/${S.projectId}/levelcurves`, { method: "POST" });
    const st = await api(`/api/project/${S.projectId}/state`);
    S.narrations = st.narrations;
  }
}

function renderLeveling() {
  const box = $("levelingList");
  box.innerHTML = "";
  const bound = S.descriptions.filter(d =>
    (S.placements[d.id] && S.placements[d.id].narration_id) || S.spliceResolved[d.id]);
  if (!bound.length) {
    box.innerHTML = `<div class="muted small" style="padding:6px">绑定旁白后即可配平</div>`;
    return;
  }
  for (const d of bound) {
    const p = S.placements[d.id] || { start: d.start || 0 };
    const n = effNarrOf(d);
    if (!n) continue;
    const it = lvItem(d.id);
    const r = S.lvReport && S.lvReport.items[d.id];
    const div = document.createElement("div");
    div.className = "lv-item" + (it.status === "confirmed" ? " confirmed" : "");
    div.dataset.did = d.id;
    const bads = r ? r.errors.filter(e => e.sev === "bad") : [];
    const warns = r ? r.errors.filter(e => e.sev === "warn") : [];
    const appliedDb = it.gain != null ? 20 * Math.log10(it.gain) : 0;
    const gainDb = appliedDb;
    const badge = it.status === "confirmed"
      ? `<span class="badge ok">已确认 ${it.accepted_reason ? "·人工保留" : ""}</span>`
      : (bads.length ? `<span class="badge bad">待处理 · ${bads.length} 个阻塞</span>`
                     : `<span class="badge warn">待处理</span>`);
    div.innerHTML = `
      <div class="lv-head">
        <b>${d.id}</b> ${n.name}
        <span class="mono small muted">${n.duration.toFixed(2)}s @ ${fmtTC(p.start)}</span>
        ${badge}
      </div>
      <canvas class="lv-canvas" height="110"></canvas>
      <div class="lv-metrics mono small">
        ${r ? `<span>有效语音 <b>${r.selDuration.toFixed(2)}s</b></span>
          <span>RMS <b>${r.rmsDb.toFixed(1)}</b> dBFS</span>
          <span>片段峰 <b>${r.peakDb.toFixed(1)}</b></span>
          <span>混音峰 <b class="${r.mixPeakDb != null && r.mixPeakDb > S.leveling.settings.ceilingDb ? "bad" : "ok"}">${r.mixPeakDb != null ? r.mixPeakDb.toFixed(1) : "—"}</b></span>
          <span>建议 <b>${r.suggestGainDb >= 0 ? "+" : ""}${r.suggestGainDb.toFixed(1)}</b> dB${r.suggestClamped ? " <i class='warn'>(受限)</i>" : ""}</span>
          <span>${r.jump ? `相邻跳变 <b class="${Math.abs(r.jump.db) > S.leveling.settings.maxJumpDb ? "bad" : "ok"}">${r.jump.db >= 0 ? "+" : ""}${r.jump.db.toFixed(1)}</b> dB` : ""}</span>`
          : `<span class="muted">计算中…</span>`}
      </div>
      <div class="lv-errs">${[...bads, ...warns].map(e =>
        `<div class="lv-err ${e.sev === "bad" ? "bad" : "warn"}">${e.code === "jump" ? "" : "⛔ "}${e.msg}</div>`).join("")}</div>
      <div class="lv-controls">
        <label class="grow">增益
          <input type="range" class="lv-gain" min="${S.leveling.settings.minGainDb}"
                 max="${S.leveling.settings.maxGainDb}" step="0.1" value="${gainDb.toFixed(1)}">
          <input type="number" class="lv-gain-num mono" step="0.1" value="${gainDb.toFixed(1)}"> dB
        </label>
        <button class="lv-suggest">套建议</button>
        <button class="lv-ab-orig">A 原版</button>
        <button class="lv-ab-new">B 调整版</button>
        <button class="lv-stop">■</button>
        <button class="lv-range-all">整段</button>
        <button class="lv-range-clear">清选区</button>
        <button class="lv-confirm">${it.status === "confirmed" ? "重新确认" : "确认"}</button>
      </div>
      ${bads.length ? `<input class="lv-keep small" placeholder="人工留痕理由(仅写入修订, 不改变阻塞状态, 片段仍保持待处理)">` : ""}`;
    box.appendChild(div);

    const canvas = div.querySelector(".lv-canvas");
    drawLvCurve(canvas, d.id, n);
    canvas.addEventListener("mousedown", (e) => lvDragStart(e, canvas, d.id, n));
    const gainSlider = div.querySelector(".lv-gain");
    gainSlider.addEventListener("input", (e) => {
      // 拖动中: 只写增益与数字框, 不重渲染(避免拖手中断); 重算延后到 change
      const it2 = lvItem(d.id);
      it2.gain = Math.pow(10, (+e.target.value) / 20);
      it2.status = "pending";
      div.querySelector(".lv-gain-num").value = e.target.value;
    });
    gainSlider.addEventListener("change", () => {
      renderLeveling();
      scheduleLeveling();
    });
    div.querySelector(".lv-gain-num").addEventListener("change", (e) => {
      const it2 = lvItem(d.id);
      const v = Math.max(S.leveling.settings.minGainDb,
                        Math.min(S.leveling.settings.maxGainDb, +e.target.value || 0));
      it2.gain = Math.pow(10, v / 20);
      it2.status = "pending";
      e.target.value = v.toFixed(1);
      div.querySelector(".lv-gain").value = v;
      renderLeveling();
      scheduleLeveling();
    });
    div.querySelector(".lv-suggest").addEventListener("click", () => {
      const rr = S.lvReport && S.lvReport.items[d.id];
      if (!rr) return;
      const it2 = lvItem(d.id);
      it2.gain = rr.suggestGain; it2.status = "pending";
      renderLeveling();
      scheduleLeveling();
    });
    div.querySelector(".lv-ab-orig").addEventListener("click", () => lvPlay(d.id, "orig"));
    div.querySelector(".lv-ab-new").addEventListener("click", () => lvPlay(d.id, "new"));
    div.querySelector(".lv-stop").addEventListener("click", stopLvPlay);
    div.querySelector(".lv-range-all").addEventListener("click", () => {
      lvItem(d.id).ranges = [{ start: 0, end: n.duration }];
      lvItem(d.id).status = "pending";
      renderLeveling(); scheduleLeveling();
    });
    div.querySelector(".lv-range-clear").addEventListener("click", () => {
      lvItem(d.id).ranges = [];
      lvItem(d.id).status = "pending";
      renderLeveling(); scheduleLeveling();
    });
    div.querySelector(".lv-confirm").addEventListener("click", () => lvConfirm(d.id, div));
  }
  syncLvAbButtons();
}

// ---- 曲线绘制
function drawLvCurve(canvas, did, narr, draftRanges) {
  const parentW = canvas.parentElement.clientWidth - 28;
  const w = Math.max(320, parentW);
  const dpr = window.devicePixelRatio || 1;
  const h = 110;
  canvas.style.width = w + "px";
  canvas.width = w * dpr; canvas.height = h * dpr;
  const g = canvas.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  const padL = 40, padR = 8, padT = 8, padB = 14;
  const pw = w - padL - padR, ph = h - padT - padB;
  const yOfDb = (db) => padT + (-db / 60) * ph;   // 0dB 顶, -60dB 底
  // 网格
  g.fillStyle = "#0d1015"; g.fillRect(padL, padT, pw, ph);
  g.strokeStyle = "#222a36"; g.fillStyle = "#5b6878"; g.font = "9px monospace";
  for (const db of [0, -12, -24, -36, -48, -60]) {
    const y = yOfDb(db);
    g.beginPath(); g.moveTo(padL, y); g.lineTo(padL + pw, y); g.stroke();
    g.fillText(db + "", 4, y + 3);
  }
  // 目标/上限线
  const stg = S.leveling.settings;
  g.setLineDash([4, 3]);
  g.strokeStyle = "#5fd68a"; g.fillStyle = "#5fd68a";
  g.beginPath(); g.moveTo(padL, yOfDb(stg.targetDb)); g.lineTo(padL + pw, yOfDb(stg.targetDb)); g.stroke();
  g.fillText("目标", padL + pw - 30, yOfDb(stg.targetDb) - 2);
  g.strokeStyle = "#ff6b6b";
  g.beginPath(); g.moveTo(padL, yOfDb(stg.ceilingDb)); g.lineTo(padL + pw, yOfDb(stg.ceilingDb)); g.stroke();
  g.fillStyle = "#ff6b6b"; g.fillText("上限", padL + 4, yOfDb(stg.ceilingDb) - 2);
  g.setLineDash([]);
  // 选区背景
  const xOfT = (t) => padL + (t / narr.duration) * pw;
  const shownRanges = draftRanges || lvRanges(did);
  for (const rg of shownRanges) {
    g.fillStyle = draftRanges ? "rgba(255,184,77,.22)" : "rgba(77,163,255,.16)";
    g.fillRect(xOfT(rg.start), padT, (rg.end - rg.start) / narr.duration * pw, ph);
  }
  // 曲线
  const lv2 = narr.levels;
  if (lv2 && lv2.t.length) {
    const nPts = lv2.t.length;
    g.lineWidth = 1;
    g.strokeStyle = "#8a6bd6"; g.beginPath();
    lv2.peak.forEach((db, i) => {
      const x = padL + (i / (nPts - 1)) * pw, y = yOfDb(db);
      i ? g.lineTo(x, y) : g.moveTo(x, y);
    });
    g.stroke();
    g.strokeStyle = "#ffd166"; g.beginPath();
    lv2.rms.forEach((db, i) => {
      const x = padL + (i / (nPts - 1)) * pw, y = yOfDb(db);
      i ? g.lineTo(x, y) : g.moveTo(x, y);
    });
    g.stroke();
  } else {
    g.fillStyle = "#7f8ca0"; g.fillText("无曲线数据", padL + 10, padT + ph / 2);
  }
  // 时间刻度
  g.fillStyle = "#5b6878";
  const step = narr.duration > 2.5 ? 1 : 0.5;
  for (let t = 0; t <= narr.duration + 1e-6; t += step)
    g.fillText(t.toFixed(1), xOfT(t) - 6, h - 3);
  // A/B 试听窗
  if (S.abAudio && S.abAudio.descId === did) {
    const rg = S.abAudio.region;
    g.strokeStyle = S.abAudio.mode === "orig" ? "#9fd0ff" : "#ffb84d";
    g.lineWidth = 2;
    g.strokeRect(xOfT(rg.start), padT, (rg.end - rg.start) / narr.duration * pw, ph);
    g.lineWidth = 1;
  }
}

// ---- 框选有效语音(横向拖动选区; 点击空白开新区, 点中现有区则拖动边界移动)
let lvDrag = null;
function lvCanvasPos(e, canvas, narr) {
  const r = canvas.getBoundingClientRect();
  const x = e.clientX - r.left;
  const padL = 40, padR = 8;
  const pw = canvas.clientWidth - padL - padR;
  return Math.max(0, Math.min(narr.duration, (x - padL) / pw * narr.duration));
}
function lvDragStart(e, canvas, did, narr) {
  const t = lvCanvasPos(e, canvas, narr);
  const ranges = lvRanges(did);
  const hit = ranges.find(rg => t >= rg.start && t <= rg.end);
  if (e.shiftKey || !hit) {
    lvDrag = { did, canvas, narr, start: t, kind: "new", base: null,
               snap: ranges.map(r => ({ ...r })), draft: null };
  } else {
    const edge = Math.abs(t - hit.start) < 0.08 * narr.duration ? "start"
               : Math.abs(t - hit.end) < 0.08 * narr.duration ? "end" : "move";
    lvDrag = { did, canvas, narr, kind: edge, base: { ...hit },
               snap: ranges.map(r => ({ ...r })), draft: null };
  }
  window.addEventListener("mousemove", lvDragMove);
  window.addEventListener("mouseup", lvDragEnd);
}
function lvDragMove(e) {
  if (!lvDrag) return;
  const t = lvCanvasPos(e, lvDrag.canvas, lvDrag.narr);
  const draft = lvDrag.snap.map(r => ({ ...r }));
  if (lvDrag.kind === "new") {
    draft.push({ start: Math.min(lvDrag.start, t), end: Math.max(lvDrag.start, t) });
  } else {
    const b = lvDrag.base;
    const cur = draft.find(rg => Math.abs(rg.start - b.start) < 1e-9 && Math.abs(rg.end - b.end) < 1e-9);
    if (lvDrag.kind === "start") cur.start = Math.min(t, cur.end - 0.02);
    if (lvDrag.kind === "end") cur.end = Math.max(t, cur.start + 0.02);
    if (lvDrag.kind === "move") {
      const dt = t - (b.start + (b.end - b.start) / 2);
      cur.start = Math.max(0, Math.min(lvDrag.narr.duration - (b.end - b.start), b.start + dt));
      cur.end = cur.start + (b.end - b.start);
    }
  }
  lvDrag.draft = draft;
  drawLvCurve(lvDrag.canvas, lvDrag.did, lvDrag.narr, draft);
}
function lvDragEnd() {
  if (!lvDrag) return;
  const it = lvItem(lvDrag.did);
  let rs = (lvDrag.draft || lvDrag.snap).map(r => ({ start: +r.start.toFixed(3), end: +r.end.toFixed(3) }))
    .filter(r => r.end - r.start >= 0.02).sort((a, b) => a.start - b.start);
  // 合并重叠选区
  const merged = [];
  for (const r of rs) {
    const last = merged[merged.length - 1];
    if (last && r.start <= last.end) last.end = Math.max(last.end, r.end);
    else merged.push(r);
  }
  it.ranges = merged;
  it.status = "pending";
  lvDrag = null;
  window.removeEventListener("mousemove", lvDragMove);
  window.removeEventListener("mouseup", lvDragEnd);
  renderLeveling();
  scheduleLeveling();
}

// ---- 保存并计算(防抖)
async function saveLevelingPlan() {
  if (!S.projectId || !S.source) return;
  const plan = { settings: S.leveling.settings, items: S.leveling.items };
  return api(`/api/project/${S.projectId}/leveling`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(plan),
  });
}
async function computeLeveling() {
  if (!S.projectId || !S.source) return;
  $("lvStatus").textContent = "计算中…";
  try {
    const rep = await saveLevelingPlan();
    S.lvReport = rep;
    renderLeveling(); runChecks(); AudioEngine.invalidateMix();
    const nb = rep.blocked.length;
    $("lvStatus").textContent = nb ? `${nb} 段阻塞待处理` : "✓ 全部可确认";
    $("lvStatus").className = nb ? "small bad" : "small ok";
  } catch (e) {
    $("lvStatus").textContent = e.message;
  }
}
function scheduleLeveling() {
  clearTimeout(S.lvTimer);
  S.lvTimer = setTimeout(computeLeveling, 300);
}

// ---- 确认
async function lvConfirm(did, div) {
  const reasonInput = div.querySelector(".lv-keep");
  const reason = reasonInput ? reasonInput.value.trim() : "";
  try {
    await saveLevelingPlan();
    const r = await api(`/api/project/${S.projectId}/levelconfirm`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ desc_id: did, accepted_reason: reason }),
    });
    const st = await api(`/api/project/${S.projectId}/state`);
    S.leveling.items = st.leveling.items || {};
    S.lvReport = r.report;
    renderRevisions(st.revisions);
    renderLeveling(); runChecks(); AudioEngine.invalidateMix();
    if (r.confirmed.includes(did)) {
      toast(`已确认 ${did}`);
    } else {
      const b = (r.blocked || []).find(x => x.desc_id === did);
      toast(b
        ? `${did} 阻塞待处理,保持 pending${b.reasonLogged ? ",理由已留痕但不改变状态" : ""}:${b.codes.join(",")}`
        : "该片段保持待处理");
    }
  } catch (e) { toast(e.message); }
}
$("lvConfirmAll").addEventListener("click", async () => {
  stopLvPlay();
  // 先保存当前编辑并重算; 阻塞片段即使填理由也不会被确认, 仅确认无阻塞段
  S.lvReport = await saveLevelingPlan();
  renderLeveling(); runChecks();
  const r = await api(`/api/project/${S.projectId}/levelconfirm`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  const st = await api(`/api/project/${S.projectId}/state`);
  S.leveling.items = st.leveling.items || {};
  S.lvReport = r.report;
  renderRevisions(st.revisions);
  renderLeveling(); runChecks(); AudioEngine.invalidateMix();
  const okN = r.confirmed.length, bN = r.blocked.length;
  toast(`确认 ${okN} 段${bN ? `;${bN} 段阻塞保持待处理(须修正选区/格式/增益/削波后才能确认)` : ""}`);
});

// ---- A/B 循环试听(Web Audio)
function syncLvAbButtons() {
  document.querySelectorAll(".lv-item").forEach(div => {
    const on = S.abAudio && S.abAudio.descId === div.dataset.did;
    div.querySelector(".lv-ab-orig").classList.toggle("on", on && S.abAudio.mode === "orig");
    div.querySelector(".lv-ab-new").classList.toggle("on", on && S.abAudio.mode === "new");
  });
}
function stopLvPlay() {
  if (!S.abAudio) return;
  const did = S.abAudio.descId;
  for (const n of S.abAudio.nodes) { try { n.stop(); } catch (e) {} try { n.disconnect(); } catch (e) {} }
  S.abAudio = null;
  syncLvAbButtons();
  drawLvCurveForDid(did);
}
async function lvPlay(did, mode) {
  stopLvPlay();
  const d = S.descriptions.find(x => x.id === did);
  // 确认版拼接: 试听合成片段; 否则整段绑定
  const buf = S.spliceResolved[did]
    ? await buildSpliceBuffer(did)
    : await AudioEngine.loadNarr(S.placements[did].narration_id);
  if (!buf) return;
  const it = S.leveling.items[did] || {};
  const report = S.lvReport && S.lvReport.items[did];
  let ranges = lvRanges(did);
  if (!ranges.length) ranges = [{ start: 0, end: buf.duration }];
  const gain = mode === "new"
    ? (it.gain != null ? it.gain : report ? report.suggestGain : 1.0)
    : 1.0;
  const a = Math.min(...ranges.map(r => r.start));
  const b = Math.max(...ranges.map(r => r.end));
  const ctx = AudioEngine.ensureCtx();
  if (ctx.state === "suspended") await ctx.resume();
  const nodes = [];
  const gn = ctx.createGain(); gn.gain.value = gain; gn.connect(ctx.destination);
  const src = ctx.createBufferSource();
  src.buffer = buf; src.loop = true; src.loopStart = a; src.loopEnd = b;
  src.connect(gn); src.start(0, a);
  nodes.push(src, gn);
  S.abAudio = { descId: did, mode, nodes, region: { start: a, end: b } };
  syncLvAbButtons();
  drawLvCurveForDid(did);
}
function drawLvCurveForDid(did) {
  const div = document.querySelector(`.lv-item[data-did="${did}"]`);
  if (!div) return;
  const d = S.descriptions.find(x => x.id === did);
  const n = d ? effNarrOf(d) : null;
  if (n) drawLvCurve(div.querySelector(".lv-canvas"), did, n);
}

// ---- 配平参数
[["lvTarget","targetDb"],["lvCeiling","ceilingDb"],["lvMaxJump","maxJumpDb"],
 ["lvMinSpeech","minSpeech"],["lvMinGain","minGainDb"],["lvMaxGain","maxGainDb"]]
.forEach(([id, key]) => $(id).addEventListener("change", () => {
  S.leveling.settings[key] = +$(id).value;
  renderLeveling();
  scheduleLeveling();
}));

// ---------------------------------------------------------------- 旁白多版本拼接
// 方案: S.splice.items[descId] = {takes:[录音id], anchors:{录音id:秒},
//   segments:[{take_id,in,out,xfade,gap}], status}; 确认后合成片段经 spliceResolved
// 接管该卡时长/配平/混合预览, 服务端 render_splice 为导出口径。

function spPlan(did) {
  if (!S.splice.items[did]) {
    const p = S.placements[did] || {};
    S.splice.items[did] = { takes: p.narration_id ? [p.narration_id] : [],
                            anchors: {}, segments: [], status: "pending" };
  }
  return S.splice.items[did];
}
// 渲染用只读视图: 不创建方案(仅查看不产生副作用, 编辑操作才经 spPlan 创建)
function spView(did) {
  if (S.splice.items[did]) return S.splice.items[did];
  const p = S.placements[did] || {};
  return { takes: p.narration_id ? [p.narration_id] : [],
           anchors: {}, segments: [], status: "pending" };
}
function spTouch(did) {
  const plan = spPlan(did);
  plan.status = "pending";        // 内容变更须重新确认(后端同样强制)
  delete S.leveling.items[did];   // 合成变更, 该卡旧配平选区/增益失效
  AudioEngine.spliceBufs = {};
  scheduleSplice();
}
function scheduleSplice() {
  clearTimeout(S.spTimer);
  S.spTimer = setTimeout(computeSplice, 300);
}
async function computeSplice() {
  clearTimeout(S.spTimer);
  if (!S.projectId || !S.source) return;
  $("spStatus").textContent = "校审中…";
  try {
    const r = await api(`/api/project/${S.projectId}/splice`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings: S.splice.settings, items: S.splice.items }),
    });
    S.spliceReport = r.report;
    if (r.plans) S.splice.items = r.plans.items || {};
    S.spliceResolved = r.resolved || {};
    const nb = (r.report && r.report.blocked || []).length;
    $("spStatus").textContent = nb ? `${nb} 个拼接方案阻塞待处理` : "";
    renderSplice(); renderDescList(); renderLeveling(); runChecks();
    AudioEngine.invalidateMix(); draw();
  } catch (e) {
    $("spStatus").textContent = e.message;
  }
}

// ---- 拼接轨布局(与服务端 render_splice 同算法): 段 i 起点 = 前段终点 - xfade + gap
function spLayout(plan) {
  let pos = 0;
  return (plan.segments || []).map((sg, i) => {
    const dur = (+sg.out || 0) - (+sg.in || 0);
    const start = i === 0 ? 0 : Math.max(0, pos - (+sg.xfade || 0) + (+sg.gap || 0));
    pos = start + dur;
    return { sg, i, start, end: start + dur };
  });
}
const SP_COLORS = ["#4da3ff", "#5fd68a", "#ffb84d", "#d68bd6", "#6bd6c9", "#ff8f6b"];
function spColor(plan, takeId) {
  const idx = (plan.takes || []).map(String).indexOf(String(takeId));
  return SP_COLORS[Math.max(0, idx) % SP_COLORS.length];
}
const spTakeName = (takeId) => {
  const n = S.narrations.find(x => String(x.id) === String(takeId));
  return n ? n.name : `#${takeId}(缺失)`;
};

// ---- 前端合成(线性交叉淡化, 与服务端 render_splice 一致), 供试听与混合预览
async function buildSpliceBuffer(did) {
  const plan = S.splice.items[did];
  if (!plan || !(plan.segments || []).length) return null;
  const key = JSON.stringify(plan.segments);
  const hit = AudioEngine.spliceBufs[did];
  if (hit && hit.key === key) return hit.buf;
  const segs = [];
  for (const sg of plan.segments) {
    const buf = await AudioEngine.loadNarr(sg.take_id);
    segs.push({ buf, in: +sg.in || 0, out: +sg.out || 0,
                xfade: +sg.xfade || 0, gap: +sg.gap || 0 });
  }
  const sr = segs[0].buf.sampleRate, nch = segs[0].buf.numberOfChannels;
  let pos = 0;
  const lays = segs.map((s, i) => {
    const f0 = Math.max(0, Math.min(s.buf.length, Math.round(s.in * sr)));
    const f1 = Math.max(f0, Math.min(s.buf.length, Math.round(s.out * sr)));
    const frames = f1 - f0;
    const start = i === 0 ? 0 : Math.max(0, pos - Math.round(s.xfade * sr) + Math.round(s.gap * sr));
    pos = start + frames;
    return { ...s, f0, frames, start };
  });
  const ctx = AudioEngine.ensureCtx();
  const out = ctx.createBuffer(nch, Math.max(1, pos), sr);
  for (let k = 0; k < lays.length; k++) {
    const L = lays[k];
    const xfIn = k > 0 ? Math.round(L.xfade * sr) : 0;
    const xfOut = k + 1 < lays.length ? Math.round(lays[k + 1].xfade * sr) : 0;
    for (let c = 0; c < nch; c++) {
      const sd = L.buf.getChannelData(c), od = out.getChannelData(c);
      for (let j = 0; j < L.frames; j++) {
        let w = 1;
        if (xfIn > 0 && j < xfIn) w = j / xfIn;
        if (xfOut > 0) {
          const tail = L.frames - j;
          if (tail <= xfOut) w = Math.min(w, tail / xfOut);
        }
        od[L.start + j] += sd[L.f0 + j] * w;
      }
    }
  }
  AudioEngine.spliceBufs[did] = { key, buf: out };
  return out;
}

// ---- 试听: A 原录 / B 候选选区 / C 合成结果(及各段单听)
function spStop() {
  if (!spEd.play) return;
  for (const n of spEd.play.nodes) { try { n.stop(); } catch (e) {} try { n.disconnect(); } catch (e) {} }
  spEd.play = null;
  syncSpPlayButtons();
}
function syncSpPlayButtons() {
  document.querySelectorAll("#spliceEditor [data-sp-play]").forEach(btn => {
    const on = spEd.play && spEd.play.kind === btn.dataset.spPlay &&
               String(spEd.play.arg == null ? "" : spEd.play.arg) === (btn.dataset.spArg || "");
    btn.classList.toggle("on", !!on);
  });
}
async function spPlay(kind, arg) {
  spStop();
  const did = spEd.did;
  let buf = null, a = 0, b = null;
  if (kind === "take") {
    buf = await AudioEngine.loadNarr(arg);
  } else if (kind === "sel") {
    if (!spEd.sel) return;
    buf = await AudioEngine.loadNarr(spEd.sel.takeId);
    a = spEd.sel.a; b = spEd.sel.b;
  } else if (kind === "seg") {
    const plan = S.splice.items[did];
    const sg = plan && (plan.segments || [])[arg];
    if (!sg) return;
    buf = await AudioEngine.loadNarr(sg.take_id);
    a = +sg.in || 0; b = +sg.out || 0;
  } else if (kind === "comp") {
    buf = await buildSpliceBuffer(did);
  }
  if (!buf) return;
  const ctx = AudioEngine.ensureCtx();
  if (ctx.state === "suspended") await ctx.resume();
  const src = ctx.createBufferSource();
  src.buffer = buf; src.connect(ctx.destination);
  if (b != null) src.start(0, a, Math.max(0.01, b - a));
  else src.start(0, a);
  spEd.play = { kind, arg, nodes: [src] };
  src.onended = () => { if (spEd.play && spEd.play.nodes.includes(src)) spStop(); };
  syncSpPlayButtons();
}

// ---- 波形峰值(从 AudioBuffer 计算, 供 take 波形绘制)
const spPeaks = {};
function bufferPeaks(buf, buckets = 1200) {
  const n = buf.length, ch = buf.getChannelData(0);
  const mins = [], maxs = [];
  const step = n / buckets;
  for (let b = 0; b < buckets; b++) {
    const lo = Math.floor(b * step), hi = Math.min(n, Math.max(lo + 1, Math.floor((b + 1) * step)));
    let mn = 1, mx = -1;
    for (let i = lo; i < hi; i++) { const v = ch[i]; if (v < mn) mn = v; if (v > mx) mx = v; }
    mins.push(mn); maxs.push(mx);
  }
  return { duration: buf.duration, mins, maxs };
}
async function spEnsureBuf(takeId) {
  const buf = await AudioEngine.loadNarr(takeId);
  if (!spPeaks[takeId]) spPeaks[takeId] = bufferPeaks(buf);
  return buf;
}

// ---- 编辑器渲染
function renderSplice() {
  const sel = $("spliceDesc");
  const prev = spEd.did;
  sel.innerHTML = "";
  for (const d of S.descriptions) {
    const o = document.createElement("option");
    o.value = d.id;
    const plan = S.splice.items[d.id];
    const mark = S.spliceResolved[d.id] ? "✓已确认"
               : (plan && (plan.segments || []).length ? "…待处理" : "");
    o.textContent = `${d.id} ${mark} ${(d.text || "").slice(0, 16)}`;
    sel.appendChild(o);
  }
  if (!S.descriptions.length) {
    $("spliceEditor").innerHTML = `<div class="muted small" style="padding:6px">载入描述稿后可拼接</div>`;
    return;
  }
  spEd.did = (prev && S.descriptions.some(d => d.id === prev)) ? prev : S.descriptions[0].id;
  sel.value = spEd.did;
  renderSpEditor();
}

function renderSpEditor() {
  const box = $("spliceEditor");
  box.innerHTML = "";
  const did = spEd.did;
  if (!did) return;
  const plan = spView(did);
  const rep = S.spliceReport && S.spliceReport.items[did];
  const resolved = S.spliceResolved[did];

  // ---- 候选录音(挂接多版 PCM WAV)
  const takesDiv = document.createElement("div");
  takesDiv.className = "sp-takes";
  for (const takeId of plan.takes || []) {
    const row = document.createElement("div");
    row.className = "sp-take";
    const anc = (plan.anchors || {})[takeId];
    row.innerHTML = `
      <div class="sp-take-head">
        <span class="sp-dot" style="background:${spColor(plan, takeId)}"></span>
        <b>${spTakeName(takeId)}</b>
        <span class="mono small muted">锚点 ${anc != null ? fmtTC(anc) : "未设(单击波形设置)"}</span>
        <span class="spacer"></span>
        <button data-sp-play="take" data-sp-arg="${takeId}" title="A 原录: 整版录音">A 原录</button>
        <button class="sp-play-sel" data-take="${takeId}" title="B 候选: 试听当前选区" disabled>B 候选</button>
        <button class="sp-add-seg" data-take="${takeId}" title="把当前选区追加到拼接轨" disabled>↓ 加入拼接轨</button>
        <button class="sp-take-del" title="移除该录音及其拼接段">✕</button>
      </div>
      <canvas class="sp-take-canvas" data-take="${takeId}"></canvas>`;
    takesDiv.appendChild(row);
  }
  box.appendChild(takesDiv);
  // 添加录音
  const addRow = document.createElement("div");
  addRow.className = "sp-add-row";
  const avail = S.narrations.filter(n => !(plan.takes || []).map(String).includes(String(n.id)));
  addRow.innerHTML = avail.length
    ? `<label class="small">挂接录音 <select class="sp-add-sel">${avail.map(n =>
        `<option value="${n.id}">${n.name} (${n.duration.toFixed(2)}s)</option>`).join("")}</select></label>
       <button class="sp-add-take">+ 挂接到本卡</button>`
    : `<span class="muted small">项目旁白已全部挂接; 可在左栏继续上传 PCM WAV</span>`;
  box.appendChild(addRow);

  // ---- 拼接轨
  const lays = spLayout(plan);
  const compDur = lays.length ? lays[lays.length - 1].end : 0;
  const trackHead = document.createElement("div");
  trackHead.className = "sp-track-head";
  trackHead.innerHTML = `
    <b>拼接轨</b>
    <span class="mono small muted">${lays.length} 段 · 合成 ${compDur.toFixed(3)}s${resolved ? " · 已确认生效" : ""}</span>
    <span class="spacer"></span>
    <button data-sp-play="comp" title="C 合成: 试听拼接结果" ${lays.length ? "" : "disabled"}>C 合成</button>
    <button class="sp-stop">■ 停止</button>`;
  box.appendChild(trackHead);
  const trackCanvas = document.createElement("canvas");
  trackCanvas.className = "sp-track-canvas";
  box.appendChild(trackCanvas);

  // ---- 段列表
  const segBox = document.createElement("div");
  segBox.className = "sp-segs";
  lays.forEach((L, k) => {
    const sg = L.sg;
    const row = document.createElement("div");
    row.className = "sp-seg";
    row.innerHTML = `
      <span class="sp-seg-no" style="background:${spColor(plan, sg.take_id)}">${k + 1}</span>
      <span class="sp-seg-name">${spTakeName(sg.take_id)}</span>
      <span class="mono small">${fmtTC(+sg.in || 0)}–${fmtTC(+sg.out || 0)}</span>
      ${k > 0 ? `<label class="small">淡化 <input type="number" class="sp-xf mono" step="0.01" min="0" max="2" value="${(+sg.xfade || 0).toFixed(2)}">s</label>
      <label class="small">间隔 <input type="number" class="sp-gap mono" step="0.01" min="0" max="2" value="${(+sg.gap || 0).toFixed(2)}">s</label>` : `<span class="small muted">首段</span>`}
      <span class="mono small muted">→${fmtTC(L.start)}</span>
      <button data-sp-play="seg" data-sp-arg="${k}" title="试听本段">▶</button>
      <button class="sp-up" ${k === 0 ? "disabled" : ""}>↑</button>
      <button class="sp-down" ${k === lays.length - 1 ? "disabled" : ""}>↓</button>
      <button class="sp-del">✕</button>`;
    segBox.appendChild(row);
  });
  box.appendChild(segBox);

  // ---- 校审报告
  const repDiv = document.createElement("div");
  repDiv.className = "sp-report";
  if (rep) {
    const bads = rep.errors.filter(e => e.sev === "bad");
    const warns = rep.errors.filter(e => e.sev === "warn");
    repDiv.innerHTML = `
      <div class="sp-rep-sum small">
        合成 <b>${rep.duration.toFixed(3)}s</b> @ ${fmtTC(rep.start)} · 接缝 ${rep.seams.length} 个
        ${rep.seams.map((s, i) => `<span class="mono">接缝${i + 1}@${fmtTC(rep.start + s.t)} 峰 ${s.peak_db.toFixed(1)}dB</span>`).join(" · ")}
      </div>
      ${[...bads, ...warns].map(e =>
        `<div class="lv-err ${e.sev === "bad" ? "bad" : "warn"}">${e.sev === "bad" ? "⛔ " : "⚠ "}${e.msg}</div>`).join("")}
      ${!bads.length && !warns.length ? `<div class="small ok">✓ 校审通过,可确认</div>` : ""}`;
  } else {
    repDiv.innerHTML = `<div class="muted small">编辑后自动校审: 采样格式/片段重叠/零交叉/接缝峰值/静音缺口/成片时长/锚点顺序/压住对白关键声</div>`;
  }
  box.appendChild(repDiv);

  // ---- 确认区
  const bads = rep ? rep.errors.filter(e => e.sev === "bad") : [];
  const cf = document.createElement("div");
  cf.className = "sp-confirm";
  cf.innerHTML = `
    <span class="badge ${plan.status === "confirmed" && resolved ? "ok" : (bads.length ? "bad" : "warn")}">
      ${plan.status === "confirmed" && resolved ? "已确认(合成进入配平/混音/导出)"
        : bads.length ? `待处理 · ${bads.length} 个阻塞` : "待处理"}</span>
    <input id="spReason" class="grow" placeholder="采用理由(确认时写入修订; 阻塞时仅留痕不改状态)"
           value="${plan.accepted_reason || ""}">
    <button class="sp-confirm-btn" ${(plan.segments || []).length ? "" : "disabled"}>
      ${plan.status === "confirmed" ? "重新确认" : "确认拼接"}</button>
    <button class="sp-remove" title="拆除拼接方案,回退到整段绑定">拆除方案</button>`;
  box.appendChild(cf);

  // ---- 事件: 挂接/移除录音
  const addSel = addRow.querySelector(".sp-add-sel");
  const addBtn = addRow.querySelector(".sp-add-take");
  if (addBtn) addBtn.addEventListener("click", () => {
    const tid = addSel.value;
    if (!tid) return;
    pushUndo();
    const pl = spPlan(did);
    pl.takes.push(isNaN(+tid) ? tid : +tid);
    spTouch(did); renderSplice();
  });
  takesDiv.querySelectorAll(".sp-take-del").forEach(btn =>
    btn.addEventListener("click", () => {
      const takeId = btn.closest(".sp-take").querySelector(".sp-take-canvas").dataset.take;
      pushUndo();
      const pl = spPlan(did);
      pl.takes = (pl.takes || []).filter(t => String(t) !== String(takeId));
      if (pl.anchors) delete pl.anchors[takeId];
      pl.segments = (pl.segments || []).filter(sg => String(sg.take_id) !== String(takeId));
      if (spEd.sel && String(spEd.sel.takeId) === String(takeId)) spEd.sel = null;
      spTouch(did); renderSplice();
    }));
  // ---- 事件: take 波形(加载/绘制/交互)
  takesDiv.querySelectorAll(".sp-take-canvas").forEach(cv => {
    const takeId = cv.dataset.take;
    drawSpTake(cv, did, takeId);
    spEnsureBuf(takeId).then(() => {
      if (cv.isConnected) drawSpTake(cv, did, takeId);
    });
    cv.addEventListener("mousedown", (e) => {
      const pk = spPeaks[takeId];
      if (!pk) return;
      spEd.drag = { takeId, canvas: cv, a0: spTakePos(e, cv, pk.duration), moved: false };
      e.preventDefault();
    });
  });
  takesDiv.querySelectorAll("[data-sp-play='take']").forEach(btn =>
    btn.addEventListener("click", () => spPlay("take", btn.dataset.spArg)));
  takesDiv.querySelectorAll(".sp-play-sel").forEach(btn =>
    btn.addEventListener("click", () => spPlay("sel")));
  takesDiv.querySelectorAll(".sp-add-seg").forEach(btn =>
    btn.addEventListener("click", () => {
      if (!spEd.sel || String(spEd.sel.takeId) !== String(btn.dataset.take)) return;
      pushUndo();
      const pl = spPlan(did);
      pl.segments = pl.segments || [];
      pl.segments.push({ take_id: isNaN(+spEd.sel.takeId) ? spEd.sel.takeId : +spEd.sel.takeId,
                         in: +spEd.sel.a.toFixed(3), out: +spEd.sel.b.toFixed(3),
                         xfade: pl.segments.length ? 0.03 : 0, gap: 0 });
      spEd.sel = null;
      spTouch(did); renderSplice();
    }));
  // ---- 事件: 拼接轨/段
  trackHead.querySelector("[data-sp-play='comp']").addEventListener("click", () => spPlay("comp"));
  trackHead.querySelector(".sp-stop").addEventListener("click", spStop);
  segBox.querySelectorAll(".sp-seg").forEach((row, k) => {
    row.querySelector("[data-sp-play='seg']").addEventListener("click", () => spPlay("seg", k));
    const xf = row.querySelector(".sp-xf");
    if (xf) xf.addEventListener("change", () => {
      pushUndo();
      plan.segments[k].xfade = Math.max(0, +xf.value || 0);
      spTouch(did); renderSplice();
    });
    const gp = row.querySelector(".sp-gap");
    if (gp) gp.addEventListener("change", () => {
      pushUndo();
      plan.segments[k].gap = Math.max(0, +gp.value || 0);
      spTouch(did); renderSplice();
    });
    row.querySelector(".sp-up").addEventListener("click", () => {
      if (k === 0) return;
      pushUndo();
      [plan.segments[k - 1], plan.segments[k]] = [plan.segments[k], plan.segments[k - 1]];
      spTouch(did); renderSplice();
    });
    row.querySelector(".sp-down").addEventListener("click", () => {
      if (k >= plan.segments.length - 1) return;
      pushUndo();
      [plan.segments[k], plan.segments[k + 1]] = [plan.segments[k + 1], plan.segments[k]];
      spTouch(did); renderSplice();
    });
    row.querySelector(".sp-del").addEventListener("click", () => {
      pushUndo();
      plan.segments.splice(k, 1);
      spTouch(did); renderSplice();
    });
  });
  // ---- 事件: 确认/拆除
  cf.querySelector(".sp-confirm-btn").addEventListener("click", () => spliceConfirm(did));
  cf.querySelector(".sp-remove").addEventListener("click", async () => {
    pushUndo();
    delete S.splice.items[did];
    delete S.leveling.items[did];
    spEd.sel = null;
    await computeSplice();
    toast(`已拆除 ${did} 拼接方案,回退到整段绑定`);
  });

  drawSpTrack(trackCanvas, did);
  syncSpPlayButtons();
  syncSpSelButtons();
}

// ---- take 波形绘制: 波形 + 已在轨段(蓝) + 候选选区(黄) + 锚点(绿)
function spTakePos(e, canvas, dur) {
  const r = canvas.getBoundingClientRect();
  return Math.max(0, Math.min(dur, (e.clientX - r.left) / r.width * dur));
}
function drawSpTake(canvas, did, takeId) {
  const plan = S.splice.items[did] || {};
  const pk = spPeaks[takeId];
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 600, h = 64;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const g = canvas.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.fillStyle = "#0d1015"; g.fillRect(0, 0, w, h);
  if (!pk) {
    g.fillStyle = "#7f8ca0"; g.font = "11px sans-serif";
    g.fillText("载入中…", 8, h / 2);
    return;
  }
  const dur = pk.duration;
  const xOf = t => t / dur * w;
  for (const sg of plan.segments || []) {
    if (String(sg.take_id) !== String(takeId)) continue;
    g.fillStyle = "rgba(77,163,255,.18)";
    g.fillRect(xOf(+sg.in || 0), 0, xOf(+sg.out || 0) - xOf(+sg.in || 0), h);
  }
  if (spEd.sel && String(spEd.sel.takeId) === String(takeId)) {
    g.fillStyle = "rgba(255,184,77,.28)";
    g.fillRect(xOf(spEd.sel.a), 0, xOf(spEd.sel.b) - xOf(spEd.sel.a), h);
  }
  const n = pk.mins.length, mid = h / 2;
  g.fillStyle = "#4da3ff";
  for (let i = 0; i < n; i++) {
    const x = i / n * w, x2 = (i + 1) / n * w;
    const y1 = mid - pk.maxs[i] * (mid - 4), y2 = mid - pk.mins[i] * (mid - 4);
    g.fillRect(x, y1, Math.max(1, x2 - x), Math.max(1, y2 - y1));
  }
  const anc = (plan.anchors || {})[takeId];
  if (anc != null) {
    const x = xOf(anc);
    g.strokeStyle = "#5fd68a"; g.fillStyle = "#5fd68a";
    g.beginPath(); g.moveTo(x, 0); g.lineTo(x, h); g.stroke();
    g.beginPath(); g.moveTo(x - 4, 0); g.lineTo(x + 4, 0); g.lineTo(x, 6); g.fill();
  }
  g.fillStyle = "#5b6878"; g.font = "9px monospace";
  const step = dur > 2.5 ? 1 : 0.5;
  for (let t = 0; t <= dur + 1e-6; t += step) g.fillText(t.toFixed(1), xOf(t) + 2, h - 3);
}

// ---- 拼接轨绘制: 段块(按录音着色) + 交叉淡化区 + 接缝 + 锚点
function drawSpTrack(canvas, did) {
  const plan = S.splice.items[did] || {};
  const lays = spLayout(plan);
  const rep = S.spliceReport && S.spliceReport.items[did];
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 600, h = 70;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const g = canvas.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.fillStyle = "#0d1015"; g.fillRect(0, 0, w, h);
  const total = lays.length ? lays[lays.length - 1].end : 0;
  if (!total) {
    g.fillStyle = "#7f8ca0"; g.font = "11px sans-serif";
    g.fillText("拼接轨为空:在上方候选录音波形上拖出选区,点「↓ 加入拼接轨」", 10, h / 2);
    return;
  }
  const xOf = t => t / total * w;
  lays.forEach((L, k) => {
    const col = spColor(plan, L.sg.take_id);
    const x0 = xOf(L.start), x1 = xOf(L.end);
    g.fillStyle = col + "44";
    g.fillRect(x0, 14, x1 - x0, h - 28);
    g.strokeStyle = col;
    g.strokeRect(x0 + 0.5, 14.5, x1 - x0 - 1, h - 29);
    const xf = +L.sg.xfade || 0;
    if (xf > 0) {
      g.fillStyle = "rgba(255,255,255,.20)";
      g.fillRect(x0, 14, xOf(L.start + xf) - x0, h - 28);
    }
    g.fillStyle = "#dbe2ec"; g.font = "10px sans-serif";
    g.fillText(String(k + 1), x0 + 3, 24);
  });
  // 锚点(绿三角, 按段内偏移映射到合成时间)
  for (const L of lays) {
    const anc = (plan.anchors || {})[L.sg.take_id];
    if (anc == null || anc < (+L.sg.in || 0) - 1e-6 || anc > (+L.sg.out || 0) + 1e-6) continue;
    const x = xOf(L.start + (anc - (+L.sg.in || 0)));
    g.fillStyle = "#5fd68a";
    g.beginPath(); g.moveTo(x - 4, h - 14); g.lineTo(x + 4, h - 14); g.lineTo(x, h - 8); g.fill();
  }
  // 接缝线: 削波红 / 峰值超限黄 / 正常灰
  if (rep) {
    const ceil = S.splice.settings.seamCeilingDb;
    for (const sm of rep.seams || []) {
      const x = xOf(sm.t);
      g.strokeStyle = sm.clip_frames > 0 ? "#ff6b6b"
                    : sm.peak_db > ceil ? "#ffb84d" : "rgba(255,255,255,.35)";
      g.beginPath(); g.moveTo(x, 10); g.lineTo(x, h - 14); g.stroke();
    }
  }
  g.fillStyle = "#5b6878"; g.font = "9px monospace";
  const step = total > 6 ? 1 : 0.5;
  for (let t = 0; t <= total + 1e-6; t += step) g.fillText(t.toFixed(1), xOf(t) + 2, h - 3);
}

// ---- take 波形交互: 拖动=候选选区, 单击=设/移同步锚点
window.addEventListener("mousemove", (e) => {
  const dr = spEd.drag;
  if (!dr) return;
  const pk = spPeaks[dr.takeId];
  if (!pk) return;
  const t = spTakePos(e, dr.canvas, pk.duration);
  if (Math.abs(t - dr.a0) * dr.canvas.clientWidth / pk.duration > 3) dr.moved = true;
  if (dr.moved) {
    spEd.sel = { takeId: dr.takeId, a: Math.min(dr.a0, t), b: Math.max(dr.a0, t) };
    drawSpTake(dr.canvas, spEd.did, dr.takeId);
    syncSpSelButtons();
  }
});
window.addEventListener("mouseup", (e) => {
  const dr = spEd.drag;
  if (!dr) return;
  spEd.drag = null;
  const pk = spPeaks[dr.takeId];
  if (!dr.moved && pk && spEd.did) {
    const plan = spPlan(spEd.did);   // 单击设锚是编辑操作: 无方案则创建
    pushUndo();
    plan.anchors = plan.anchors || {};
    plan.anchors[dr.takeId] = +spTakePos(e, dr.canvas, pk.duration).toFixed(3);
    spTouch(spEd.did);
  } else if (dr.moved) {
    drawSpTake(dr.canvas, spEd.did, dr.takeId);
    syncSpSelButtons();
  }
});
function syncSpSelButtons() {
  document.querySelectorAll("#spliceEditor .sp-add-seg, #spliceEditor .sp-play-sel").forEach(btn => {
    btn.disabled = !(spEd.sel && String(spEd.sel.takeId) === String(btn.dataset.take));
  });
}

// ---- 确认拼接
async function spliceConfirm(did) {
  const reasonInp = $("spReason");
  const reason = reasonInp ? reasonInp.value.trim() : "";
  try {
    await computeSplice();   // 先保存最新方案(后端对变更内容强制重新确认)
    const r = await api(`/api/project/${S.projectId}/spliceconfirm`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ desc_id: did, accepted_reason: reason }),
    });
    const st = await api(`/api/project/${S.projectId}/state`);
    S.splice.items = (st.splice || {}).items || {};
    S.leveling.items = (st.leveling || {}).items || {};
    S.spliceReport = r.report;
    S.spliceResolved = r.resolved || {};
    renderRevisions(st.revisions);
    renderSplice(); renderDescList(); renderLeveling(); runChecks();
    AudioEngine.invalidateMix(); draw();
    if (r.confirmed && r.confirmed.includes(did)) {
      toast(`已确认拼接 ${did},合成片段进入配平/混音/导出`);
    } else {
      const b = (r.blocked || [])[0];
      toast(`${did} 阻塞待处理,保持 pending${b && b.reasonLogged ? ",理由已留痕但不改变状态" : ""}${b ? ":" + b.codes.join(",") : ""}`);
    }
  } catch (e) { toast(e.message); }
}

// ---- 拼接参数与描述卡切换
[["spZerox", "zeroxMs"], ["spGap", "gapWarnS"], ["spCeil", "seamCeilingDb"]]
.forEach(([id, key]) => $(id).addEventListener("change", () => {
  S.splice.settings[key] = +$(id).value;
  scheduleSplice();
}));
$("spliceDesc").addEventListener("change", () => {
  spEd.did = $("spliceDesc").value;
  spEd.sel = null;
  renderSplice();
});

// ---------------------------------------------------------------- 播放引擎(Web Audio)
const AudioEngine = {
  ctx: null, srcBuf: null, narrBufs: {}, mixBuf: null, spliceBufs: {}, nodes: [], playing: false,
  reset() { this.stop(); this.srcBuf = null; this.narrBufs = {}; this.mixBuf = null; this.spliceBufs = {}; },
  invalidateMix() { this.mixBuf = null; this.spliceBufs = {}; },
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
  // 线性插值压缩时长(缩写稿), 与服务端 time_compress 同算法
  resampleBuffer(buf, factor) {
    if (factor === 1) return buf;
    const ctx = this.ensureCtx();
    const n = Math.max(1, Math.round(buf.length / factor));
    const out = ctx.createBuffer(buf.numberOfChannels, n, buf.sampleRate);
    for (let c = 0; c < buf.numberOfChannels; c++) {
      const sd = buf.getChannelData(c), od = out.getChannelData(c);
      for (let j = 0; j < n; j++) {
        const pos = j * factor;
        const i0 = Math.floor(pos), i1 = Math.min(i0 + 1, buf.length - 1), fr = pos - i0;
        od[j] = sd[i0] * (1 - fr) + sd[i1] * fr;
      }
    }
    return out;
  },
  // 旁白有效片段(缩写则压缩); 带缓存, placements 变化时 invalidateMix 清掉
  async effectiveNarr(d) {
    // 确认版拼接: 用前端合成的拼接片段(与服务端 render_splice 同算法)
    let nb = S.spliceResolved[d.id]
      ? await buildSpliceBuffer(d.id)
      : await this.loadNarr(S.placements[d.id].narration_id);
    const f = abridgeFactor(d);
    if (f !== 1) nb = this.resampleBuffer(nb, f);
    return nb;
  },
  // 客户端混合预览: 原声 × 压低包络 + 各旁白(与 Python render_mix 同规则)
  async buildMix() {
    if (this.mixBuf) return this.mixBuf;
    const src = await this.loadSource();
    const ctx = this.ensureCtx();
    const sr = src.sampleRate, n = src.length, nch = src.numberOfChannels;
    const out = ctx.createBuffer(nch, n, sr);
    const duckTo = S.settings.duck_to, pad = S.settings.duck_pad, ramp = 0.15;
    // 先备好各旁白有效片段(缩写已压缩), 再算压低包络
    const jobs = [];
    for (const d of S.descriptions) {
      const p = S.placements[d.id];
      if (!p || (!p.narration_id && !S.spliceResolved[d.id])) continue;
      jobs.push({ p, _did: d.id, nb: await this.effectiveNarr(d) });
    }
    const env = new Float32Array(n).fill(1);
    for (const { p, nb } of jobs) {
      if (!p.duck) continue;
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
    for (let c = 0; c < nch; c++) {
      const sd = src.getChannelData(Math.min(c, src.numberOfChannels - 1));
      const od = out.getChannelData(c);
      for (let i = 0; i < n; i++) od[i] = sd[i] * env[i];
    }
    for (const { p, _did, nb } of jobs) {
      let gain = p.gain == null ? 1 : p.gain;
      const lvi = S.leveling.items[_did];
      if (lvi && lvi.status === "confirmed" && lvi.gain != null) gain = lvi.gain;
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
    else { // 仅旁白: 逐段调度(缩写片段用压缩后的有效音频)
      const t0 = ctx.currentTime + 0.05;
      for (const d of S.descriptions) {
        const p = S.placements[d.id];
        if (!p || !p.narration_id) continue;
        const nb = await this.effectiveNarr(d);
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
  stopLvPlay();
  S.loopRegion = null;
  renderIssues(); draw();
  AudioEngine.play($("playMode").value, S.playhead);
});
$("btnStop").addEventListener("click", () => { AudioEngine.stop(); S.loopRegion = null; stopLvPlay(); renderIssues(); draw(); });

// ---------------------------------------------------------------- 混音渲染与导出
$("btnRenderMix").addEventListener("click", async () => {
  if (!S.projectId) return;
  await savePlacements();
  toast("渲染中…");
  const r = await api(`/api/project/${S.projectId}/mix`, { method: "POST" });
  toast(r.clip
    ? `⚠ 混音含 ${r.clip.frames} 个削波采样,峰值 ${r.clip.peak_db} dBFS,首次 ${fmtTC(r.clip.first_t)}`
    : `混音完成 ${r.duration.toFixed(2)}s,可导出`);
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
