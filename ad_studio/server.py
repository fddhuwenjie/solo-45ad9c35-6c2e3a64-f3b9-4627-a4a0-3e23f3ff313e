#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
无障碍音频描述配轨校审工具 —— 后端
仅使用 Python 标准库: wsgiref / wave / json / sqlite3 / array
负责: 素材解析(WAV/时码稿)、混音渲染、修订留痕、导出。
"""
import io
import json
import math
import os
import random
import sqlite3
import time
import wave
from array import array
from urllib.parse import urlparse, parse_qs
from wsgiref.simple_server import make_server

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
STATIC = os.path.join(BASE, "static")
DB = os.path.join(DATA, "app.db")
os.makedirs(DATA, exist_ok=True)

# ---------------------------------------------------------------- 数据库

def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS projects(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS assets(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        kind TEXT NOT NULL,            -- source | narration | dialogue | scenes | descriptions | keysounds
        name TEXT,
        data_json TEXT,                -- 解析结果(峰值/时码/条目)
        file_path TEXT,                -- WAV 文件路径(音频类)
        created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS placements(
        project_id INTEGER PRIMARY KEY,
        data_json TEXT NOT NULL);      -- {placements:{descId:{...}}, settings:{...}}
    CREATE TABLE IF NOT EXISTS revisions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        created REAL NOT NULL,
        summary TEXT NOT NULL,         -- 素材摘要+时码说明
        rationale TEXT NOT NULL,       -- 采用理由
        snapshot_json TEXT NOT NULL);  -- 当时 placements/settings/校审结果
    CREATE TABLE IF NOT EXISTS leveling(
        project_id INTEGER PRIMARY KEY,
        data_json TEXT NOT NULL);      -- {settings, items:{descId:{ranges,gain,...}}}
    CREATE TABLE IF NOT EXISTS splice(
        project_id INTEGER PRIMARY KEY,
        data_json TEXT NOT NULL);      -- {settings, items:{descId:{takes,anchors,segments,status}}}
    CREATE TABLE IF NOT EXISTS duckenv(
        project_id INTEGER PRIMARY KEY,
        data_json TEXT NOT NULL);      -- {settings, items:{descId:{points,protected,status}}}
    """)
    conn.commit()
    conn.close()

# ---------------------------------------------------------------- WAV 工具

def read_wav(raw: bytes):
    """返回 (samples: array('h') 交错, nch, framerate)。支持 8/16/32bit PCM。"""
    with wave.open(io.BytesIO(raw), "rb") as w:
        nch, sw, fr, nframes = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        frames = w.readframes(nframes)
    if sw == 2:
        samples = array("h")
        samples.frombytes(frames)
    elif sw == 1:  # 8bit 无符号 -> 16bit
        samples = array("h", ((b - 128) << 8 for b in frames))
    elif sw == 4:  # 32bit 有符号 -> 16bit
        s32 = array("i")
        s32.frombytes(frames)
        samples = array("h", (s >> 16 for s in s32))
    else:
        raise ValueError("不支持的采样位宽: %d" % sw)
    return samples, nch, fr

def write_wav(samples, nch, fr) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(nch)
        w.setsampwidth(2)
        w.setframerate(fr)
        w.writeframes(samples.tobytes())
    return buf.getvalue()

def convert(samples, src_ch, src_fr, dst_ch, dst_fr):
    """声道转换 + 线性插值重采样到目标格式。"""
    # 声道
    if src_ch != dst_ch:
        out = array("h")
        if dst_ch == 1:  # 多声道 -> 单声道(平均)
            for i in range(0, len(samples), src_ch):
                out.append(int(sum(samples[i:i + src_ch]) / src_ch))
        else:            # 单声道 -> 多声道(复制)
            for s in samples:
                out.extend([s] * dst_ch)
        samples = out
        src_ch = dst_ch
    # 重采样
    if src_fr != dst_fr:
        n_src = len(samples) // src_ch
        n_dst = int(round(n_src * dst_fr / src_fr))
        out = array("h")
        for j in range(n_dst):
            pos = j * src_fr / dst_fr
            i0 = int(pos)
            i1 = min(i0 + 1, n_src - 1)
            frac = pos - i0
            for c in range(dst_ch):
                v = samples[i0 * dst_ch + c] * (1 - frac) + samples[i1 * dst_ch + c] * frac
                out.append(int(v))
        samples = out
    return samples

def time_compress(samples, nch, factor):
    """线性插值按 factor(>1 缩短)压缩时长, 与前端 resampleBuffer 同算法。"""
    n_src = len(samples) // nch
    n_dst = max(1, int(round(n_src / factor)))
    out = array("h")
    for j in range(n_dst):
        pos = j * factor
        i0 = int(pos)
        i1 = min(i0 + 1, n_src - 1)
        frac = pos - i0
        for c in range(nch):
            v = samples[i0 * nch + c] * (1 - frac) + samples[i1 * nch + c] * frac
            out.append(int(v))
    return out

def abridge_speed(p, clip_dur, text_len, settings):
    """缩写稿: 目标时长 = 字数/目标语速(下限0.8s); 返回压缩倍速(1=不压缩)。"""
    if not p.get("abridged"):
        return 1.0
    max_rate = float(settings.get("maxRate", 5.5))
    if max_rate <= 0 or clip_dur <= 0:
        return 1.0
    target = max(0.8, text_len / max_rate)
    return clip_dur / target if target < clip_dur else 1.0

def make_peaks(samples, nch, fr, buckets=4000):
    """供前端波形绘制的 min/max 峰值包络。"""
    n = len(samples) // nch
    if n == 0:
        return {"duration": 0.0, "mins": [], "maxs": []}
    buckets = max(1, min(buckets, n))
    step = n / buckets
    mins, maxs = [], []
    for b in range(buckets):
        lo, hi = int(b * step), max(int(b * step) + 1, int((b + 1) * step))
        hi = min(hi, n)
        mn, mx = 32767, -32768
        for i in range(lo, hi):
            v = samples[i * nch]
            if v < mn: mn = v
            if v > mx: mx = v
        mins.append(round(mn / 32768.0, 4))
        maxs.append(round(mx / 32768.0, 4))
    return {"duration": n / fr, "mins": mins, "maxs": maxs}

# ---------------------------------------------------------------- 响度分析

EPS_DB = -120.0

def dbfs(x):
    """线性幅度(0..1 满刻度 32768) -> dBFS。"""
    return 20.0 * math.log10(x) if x > 1e-6 else EPS_DB

def level_curves(samples, nch, fr, win_ms=20, points=600):
    """
    短时 RMS/峰值曲线。窗长 win_ms(不重叠), 再按 points 等距重采样供画布使用。
    多声道取每帧各声道平方均值。返回 {win, points:[{t,rms,peak}], duration}。
    """
    n = len(samples) // nch
    win = max(1, int(fr * win_ms / 1000.0))
    raw_t, raw_rms, raw_peak = [], [], []
    for f0 in range(0, n, win):
        f1 = min(f0 + win, n)
        if f1 <= f0:
            continue
        ss = 0.0; mx = 0
        for i in range(f0, f1):
            base = i * nch
            if nch == 1:
                v = samples[base]
                ss += v * v
                if abs(v) > mx: mx = abs(v)
            else:
                s2 = 0; m2 = 0
                for c in range(nch):
                    v = samples[base + c]
                    s2 += v * v
                    if abs(v) > m2: m2 = abs(v)
                ss += s2 / nch          # 每帧各声道平方均值
                if m2 > mx: mx = m2
        rms = math.sqrt(ss / (f1 - f0)) / 32768.0
        peak = mx / 32768.0
        raw_t.append((f0 + (f1 - f0) / 2.0) / fr)
        raw_rms.append(round(dbfs(rms), 2))
        raw_peak.append(round(dbfs(max(peak, 1e-6)), 2))
    # 等距重采样到 points 个点
    if not raw_t:
        return {"win": round(win / fr, 4), "duration": n / fr, "t": [], "rms": [], "peak": []}
    if len(raw_t) > points:
        t2, r2, p2 = [], [], []
        step = len(raw_t) / points
        for k in range(points):
            i = int(k * step)
            t2.append(round(raw_t[i], 4)); r2.append(raw_rms[i]); p2.append(raw_peak[i])
        raw_t, raw_rms, raw_peak = t2, r2, p2
    return {"win": round(win / fr, 4), "duration": n / fr,
            "t": raw_t, "rms": raw_rms, "peak": raw_peak}

def region_level(samples, nch, fr, ranges):
    """
    在选区 ranges:[{start,end}](秒, 相对片段) 内统计有效语音电平。
    返回 {rms_db, peak_db, peak_t, frames, duration}; 空选区返回 frames=0。
    """
    n = len(samples) // nch
    acc = 0.0          # 平方和(每帧)
    count = 0
    mx = 0; mx_f = 0
    for rg in ranges or []:
        f0 = max(0, int(float(rg["start"]) * fr))
        f1 = min(n, int(float(rg["end"]) * fr))
        for i in range(f0, f1):
            base = i * nch
            if nch == 1:
                v = samples[base]; acc += v * v
                if abs(v) > mx: mx = abs(v); mx_f = i
            else:
                s2 = 0; m2 = 0
                for c in range(nch):
                    v = samples[base + c]; s2 += v * v
                    if abs(v) > m2: m2 = abs(v)
                acc += s2 / nch
                if m2 > mx: mx = m2; mx_f = i
            count += 1
    if count == 0:
        return {"rms_db": EPS_DB, "peak_db": EPS_DB, "peak_t": 0.0,
                "frames": 0, "duration": 0.0}
    rms = math.sqrt(acc / count) / 32768.0
    return {"rms_db": round(dbfs(rms), 2), "peak_db": round(dbfs(mx / 32768.0), 2),
            "peak_t": round(mx_f / fr, 4), "frames": count,
            "duration": round(count / fr, 4)}

# ---------------------------------------------------------------- 混音

def prepare_narr(clip, nch, fr, p, text_len, settings):
    """旁白片段: 转工程格式 + 缩写压缩。返回 (samples, dur)。"""
    cs, cc, cf = clip
    cs = convert(cs, cc, cf, nch, fr)
    speed = abridge_speed(p, (len(cs) // nch) / fr, text_len, settings)
    if speed != 1.0:
        cs = time_compress(cs, nch, speed)
    return cs, (len(cs) // nch) / fr

def build_duck_env(n_frames, nch, fr, prepared, settings, exclude_ids=None):
    """
    按 prepared 项生成旧版固定斜坡压低包络(每帧增益)。
    prepared 元素为 (p, cs, dur[, _gain[, desc_id]]); exclude_ids 中的卡
    (已确认自定义原声让位包络)跳过, 由 DuckEnvComposer 接管, 不重复压低。
    """
    exclude_ids = exclude_ids or set()
    duck_to = float(settings.get("duck_to", 0.35))
    pad = float(settings.get("duck_pad", 0.15))
    ramp = 0.15
    env = [1.0] * n_frames
    for item in prepared:
        p = item[0]
        did = item[4] if len(item) > 4 else None
        if did is not None and str(did) in exclude_ids:
            continue
        if not p.get("duck"):
            continue
        cs, dur = item[1], item[2]
        t0 = float(p["start"]) - pad
        t1 = float(p["start"]) + dur + pad
        f0, f1 = max(0, int(t0 * fr)), min(n_frames, int(t1 * fr))
        r = int(ramp * fr)
        for f in range(f0, f1):
            d_in = f - f0
            d_out = f1 - f
            g = duck_to
            if d_in < r:
                g = 1.0 - (1.0 - duck_to) * d_in / max(1, r)
            elif d_out < r:
                g = 1.0 - (1.0 - duck_to) * d_out / max(1, r)
            if g < env[f]:
                env[f] = g
    return env

def render_mix(src_samples, nch, fr, placements, narr_clips, settings, desc_texts=None,
               leveling=None, duckenv=None):
    """
    placements: [{narration_id, start, duck, gain, abridged, desc_id}]
    narr_clips: {id: (samples, nch, fr)} 原始片段, 此处转换到工程格式
    desc_texts: {desc_id: text} 用于缩写稿计算目标时长
    leveling: {items:{descId:{status:'confirmed', gain}}} 确认版响度增益
    duckenv: {items:{descId:{status:'confirmed', points}}} 确认版原声让位包络;
             无自定义包络的卡(含旧项目)仍走固定斜坡
    duck: 在旁白(压缩后)前后 pad 内把原声压到 settings.duck_to, 0.15s 斜坡
    返回 (mixed:array('h'), clip:{frames,peak,first_t}|None, max_lin:float)
    """
    desc_texts = desc_texts or {}
    lv_items = (leveling or {}).get("items", {})
    de_items = (duckenv or {}).get("items", {})
    n_frames = len(src_samples) // nch

    # 预处理各旁白: 格式转换 + 缩写压缩 + 增益(确认版配平优先)
    prepared = []  # (placement, samples, dur, gain, desc_id)
    for p in placements:
        clip = narr_clips.get(str(p["narration_id"]))
        if not clip:
            continue
        cs, dur = prepare_narr(clip, nch, fr, p,
                               len(desc_texts.get(p.get("desc_id"), "")), settings)
        lv = lv_items.get(p.get("desc_id"))
        gain = float(lv["gain"]) if lv and lv.get("status") == "confirmed" and lv.get("gain") is not None \
            else float(p.get("gain", 1.0))
        prepared.append((p, cs, dur, gain, p.get("desc_id")))

    # 确认的自定义包络接管原声增益(增量 min 合成); 其余卡走旧版固定斜坡
    custom = {str(did): it for did, it in de_items.items()
              if it.get("status") == "confirmed" and it.get("points")}
    composer = DuckEnvComposer(n_frames, fr)
    legacy = build_duck_env(n_frames, nch, fr, prepared, settings, exclude_ids=set(custom))
    for i, g in enumerate(legacy):
        composer.env[i] = g
    for did, it in custom.items():
        composer.upsert(did, [{"t": float(q["t"]), "g": float(q["g"])} for q in it["points"]])
    env = composer.env

    out = array("f", [0.0]) * (n_frames * nch)
    for f in range(n_frames):
        g = env[f]
        base = f * nch
        for c in range(nch):
            out[base + c] = src_samples[base + c] * g

    # 叠旁白
    for p, cs, dur, gain, _did in prepared:
        start_f = int(float(p["start"]) * fr)
        cn = len(cs) // nch
        for j in range(cn):
            f = start_f + j
            if f < 0 or f >= n_frames:
                continue
            base = f * nch
            jb = j * nch
            for c in range(nch):
                out[base + c] += cs[jb + c] * gain

    mixed = array("h")
    clip_frames = 0; clip_max = 0.0; clip_first_t = None
    abs_max = 0.0
    for idx, v in enumerate(out):
        if v > 32767 or v < -32768:
            clip_frames += 1
            av = abs(v)
            if av > clip_max:
                clip_max = av
                clip_first_t = (idx // nch) / fr
            if abs(v) > abs_max: abs_max = abs(v)
            v = 32767 if v > 32767 else -32768
        elif abs(v) > abs_max:
            abs_max = abs(v)
        mixed.append(int(v))
    clip = {"frames": clip_frames, "peak_db": round(dbfs(clip_max / 32768.0), 2),
            "first_t": round(clip_first_t or 0.0, 4)} if clip_frames else None
    return mixed, clip, abs_max / 32768.0

# ---------------------------------------------------------------- 响度配平

def default_level_settings():
    return {"targetDb": -20.0,     # 目标响度(有效语音 RMS)
            "ceilingDb": -1.0,     # 混音峰值上限
            "maxJumpDb": 3.0,      # 相邻描述响度跳变阈值
            "minSpeech": 0.3,      # 有效语音最小时长(s)
            "minGainDb": -14.0,    # 增益下限 dB
            "maxGainDb": 15.6}     # 增益上限 dB

def _gain_db(g):
    return 20.0 * math.log10(g) if g > 1e-6 else EPS_DB

def leveling_report(st, plan, src, src_nch, src_fr, narr_clips):
    """
    纯计算(不写库): 按选区计算每段片段电平、建议增益、混音峰值、相邻响度跳变。
    返回 {settings, items:{descId:result}, order:[descId], blocked:[descId]}。
    阻塞错误(通道不一致/有效语音不足/增益越界/混音削波)使该片段保持待处理。
    """
    settings = default_level_settings()
    settings.update(plan.get("settings") or {})
    pdata = st["placements"]
    placements = pdata.get("placements", {})
    gsettings = pdata.get("settings", {})
    desc_texts = {d["id"]: d.get("text", "") for d in st["descriptions"]}
    desc_by_id = {d["id"]: d for d in st["descriptions"]}
    plan_items = plan.get("items") or {}

    target = float(settings["targetDb"]); ceiling = float(settings["ceilingDb"])
    gmin = 10.0 ** (float(settings["minGainDb"]) / 20.0)
    gmax = 10.0 ** (float(settings["maxGainDb"]) / 20.0)
    min_speech = float(settings["minSpeech"])

    n_frames = (len(src) // src_nch) if src else 0
    speed_of = {}
    prepared = {}   # did -> {p, cs, dur, gain0, raw_ch, raw_fr, ranges, speed}
    plist = []
    for did, p in placements.items():
        nid = p.get("narration_id")
        if not nid or str(nid) not in narr_clips:
            continue
        clip = narr_clips[str(nid)]
        raw_s, raw_ch, raw_fr = clip
        cs, dur = prepare_narr(clip, src_nch, src_fr, p, len(desc_texts.get(did, "")), gsettings)
        speed = (len(raw_s) // raw_ch) / max(1, len(cs) // src_nch) if len(cs) else 1.0
        speed_of[did] = speed
        item = plan_items.get(did) or {}
        clip_dur = (len(raw_s) // raw_ch) / raw_fr
        ranges = item.get("ranges") or [{"start": 0.0, "end": clip_dur}]
        lv = item if item.get("status") == "confirmed" else None
        gain0 = float(lv["gain"]) if lv and item.get("gain") is not None else float(p.get("gain", 1.0))
        prepared[did] = {"p": p, "cs": cs, "dur": dur, "gain0": gain0,
                         "raw_ch": raw_ch, "raw_fr": raw_fr, "raw_dur": clip_dur,
                         "ranges": ranges, "speed": speed, "narration_id": nid}
        plist.append((p, cs, dur, gain0, did))

    # 原声 + 压低包络: 确认自定义包络接管的卡跳过固定斜坡(由 DuckEnvComposer 合成)
    base = None
    if src is not None:
        de_items = (st.get("duckenv") or {}).get("items", {})
        custom = {str(did) for did, it in de_items.items()
                  if it.get("status") == "confirmed" and it.get("points")}
        composer = DuckEnvComposer(n_frames, src_fr)
        legacy = build_duck_env(n_frames, src_nch, src_fr, plist, gsettings, exclude_ids=custom)
        for i, g in enumerate(legacy):
            composer.env[i] = g
        for did in custom:
            it = de_items[did]
            composer.upsert(did, [{"t": float(q["t"]), "g": float(q["g"])} for q in it["points"]])
        env = composer.env
        base = array("f", [0.0]) * (n_frames * src_nch)
        for f in range(n_frames):
            b = f * src_nch; g = env[f]
            for c in range(src_nch):
                base[b + c] = src[b + c] * g

    def mix_with(target_did, gain):
        """底声 + 其他旁白(当前增益) + 目标旁白(gain), 返回浮点混音。"""
        out = array("f", base) if base is not None else array("f")
        for did, info in prepared.items():
            g = gain if did == target_did else info["gain0"]
            if g == 0:
                continue
            cs = info["cs"]; start_f = int(float(info["p"]["start"]) * src_fr)
            cn = len(cs) // src_nch
            for j in range(cn):
                f = start_f + j
                if f < 0 or f >= n_frames:
                    continue
                b = f * src_nch; jb = j * src_nch
                for c in range(src_nch):
                    out[b + c] += cs[jb + c] * g
        return out

    def sim_peak(did, info, gain):
        """目标片段选区内的混音峰值与削波帧数(含成片/片段内时码)。"""
        out = mix_with(did, gain)
        start_f = int(float(info["p"]["start"]) * src_fr)
        mx = 0.0; nclip = 0; first_j = None
        for rg in info["ranges"]:
            # 缩写压缩: 原片段时间 t 映射到压缩后 t/speed
            j0 = max(0, int(float(rg["start"]) / info["speed"] * src_fr))
            j1 = min(len(info["cs"]) // src_nch,
                     int(float(rg["end"]) / info["speed"] * src_fr))
            for j in range(j0, j1):
                f = start_f + j
                if f < 0 or f >= n_frames:
                    continue
                b = f * src_nch
                m2 = 0.0; clipped = False
                for c in range(src_nch):
                    v = out[b + c]
                    if abs(v) > m2: m2 = abs(v)
                    if v > 32767 or v < -32768:
                        clipped = True
                if clipped:
                    nclip += 1
                    if first_j is None: first_j = j
                if m2 > mx: mx = m2
        if first_j is None and mx == 0.0:
            return {"peak_db": EPS_DB, "clip_frames": 0, "clip_t": 0.0, "clip_project_t": 0.0}
        return {"peak_db": round(dbfs(mx / 32768.0), 2), "clip_frames": nclip,
                "clip_t": round((first_j or 0) / src_fr, 4),
                "clip_project_t": round((start_f + (first_j or 0)) / src_fr, 4)}

    results = {}
    order = sorted(prepared.keys(), key=lambda d: float(prepared[d]["p"].get("start", 0)))
    blocked = []
    for did in order:
        info = prepared[did]
        p = info["p"]
        raw = narr_clips[str(info["narration_id"])]
        item = plan_items.get(did) or {}
        name = next((n.get("name") for n in st["narrations"] if str(n["id"]) == str(info["narration_id"])), str(info["narration_id"]))
        errors = []
        sel_dur = sum(max(0.0, float(r["end"]) - float(r["start"])) for r in info["ranges"])

        # 1) 通道/采样率格式一致性
        if src is None or (info["raw_ch"], info["raw_fr"]) != (src_nch, src_fr):
            errors.append({"code": "channel", "sev": "bad", "msg":
                "通道格式不一致:片段 %dch/%dHz,工程 %dch/%dHz,无法可靠叠加" % (
                    info["raw_ch"], info["raw_fr"], src_nch or 0, src_fr or 0)})
            results[did] = {
                "desc_id": did, "narration_id": info["narration_id"], "name": name,
                "duration": round(info["raw_dur"], 3), "start": float(p.get("start", 0)),
                "ranges": [{"start": round(float(r["start"]), 4), "end": round(float(r["end"]), 4)}
                           for r in info["ranges"]],
                "selDuration": round(sel_dur, 3),
                "rmsDb": EPS_DB, "peakDb": EPS_DB, "peakT": 0.0,
                "suggestGain": 1.0, "suggestGainDb": 0.0, "suggestClamped": False,
                "gain": item.get("gain"), "gainDb": 0.0, "actualGainDb": 0.0,
                "mixPeakDb": None, "status": item.get("status", "pending"),
                "acceptedReason": item.get("accepted_reason", ""),
                "errors": errors, "jump": None}
            continue

        # 2) 有效语音电平(选区)
        lv_sel = region_level(raw[0], raw[1], raw[2], info["ranges"])
        if lv_sel["frames"] == 0 or sel_dur < min_speech:
            errors.append({"code": "speech", "sev": "bad", "msg":
                "有效语音不足:选区仅 %.2fs(< %.2fs),请重新框选,排除静音/爆音/接带噪声 [%s 内 %s–%s]" % (
                    sel_dur, min_speech, name,
                    fmt_tc(info["ranges"][0]["start"] if info["ranges"] else 0),
                    fmt_tc(info["ranges"][-1]["end"] if info["ranges"] else 0))})

        # 建议增益: 收敛到目标 RMS, 再受峰值上限与增益上下限约束
        rms_db = lv_sel["rms_db"]; peak_db = lv_sel["peak_db"]
        suggest = 10.0 ** ((target - rms_db) / 20.0) if lv_sel["frames"] else 1.0
        clamped = False
        if suggest < gmin or suggest > gmax:
            clamped = True
        suggest = max(gmin, min(gmax, suggest))
        sim = None
        if src is not None and lv_sel["frames"]:
            sim = sim_peak(did, info, suggest)
            if sim["peak_db"] > ceiling:
                suggest *= 10.0 ** ((ceiling - sim["peak_db"]) / 20.0)
                if suggest < gmin: clamped = True
                suggest = max(gmin, min(gmax, suggest))
                sim = sim_peak(did, info, suggest)

        gain = item.get("gain")
        gain = float(gain) if gain is not None else None
        applied = gain if gain is not None else suggest
        # 3) 增益越界
        if gain is not None and (gain < gmin - 1e-9 or gain > gmax + 1e-9):
            errors.append({"code": "gainrange", "sev": "bad", "msg":
                "增益越界:%+.2f dB,允许范围 %+.1f ~ %+.1f dB" % (
                    _gain_db(gain), float(settings["minGainDb"]), float(settings["maxGainDb"]))})
        # 4) 混音峰值 / 削波
        cur_sim = sim if gain is None else (
            sim_peak(did, info, gain) if src is not None and lv_sel["frames"] else None)
        if cur_sim and (cur_sim["peak_db"] > ceiling or cur_sim["clip_frames"] > 0):
            why = "硬削波(>0 dBFS)" if cur_sim["clip_frames"] else "超过峰值上限"
            errors.append({"code": "mixclip", "sev": "bad", "msg":
                "混音%s:峰值 %.1f dBFS(上限 %.1f dBFS),成片时码 %s / 片段内 %s,共 %d 个采样越界" % (
                    why, cur_sim["peak_db"], ceiling,
                    fmt_tc(cur_sim["clip_project_t"]), fmt_tc(cur_sim["clip_t"]),
                    cur_sim["clip_frames"])})

        results[did] = {
            "desc_id": did, "narration_id": info["narration_id"], "name": name,
            "duration": round(info["raw_dur"], 3),
            "start": float(p.get("start", 0)),
            "ranges": [{"start": round(float(r["start"]), 4), "end": round(float(r["end"]), 4)}
                       for r in info["ranges"]],
            "selDuration": round(sel_dur, 3),
            "rmsDb": rms_db, "peakDb": peak_db, "peakT": lv_sel["peak_t"],
            "suggestGain": round(suggest, 4), "suggestGainDb": round(_gain_db(suggest), 2),
            "suggestClamped": clamped,
            "gain": (round(gain, 4) if gain is not None else None),
            "gainDb": (round(_gain_db(applied), 2)),
            # 混音实际生效增益: 已确认用确认值, 待处理用原 placement.gain(默认 1.0)
            "actualGainDb": round(_gain_db(float(p.get("gain", 1.0)))
                                  if item.get("status") != "confirmed"
                                  else _gain_db(float(item.get("gain", p.get("gain", 1.0)))), 2),
            "mixPeakDb": cur_sim["peak_db"] if cur_sim else None,
            "status": item.get("status", "pending"),
            "acceptedReason": item.get("accepted_reason", ""),
            "errors": errors, "jump": None}

    # 相邻描述响度跳变(按成片顺序, 用混音实际生效增益算, 配平前忽大忽小、确认后趋平)
    max_jump = float(settings["maxJumpDb"])
    for i in range(1, len(order)):
        a, b = order[i - 1], order[i]
        ra, rb = results[a], results[b]
        if ra["selDuration"] < min_speech or rb["selDuration"] < min_speech:
            continue
        la = ra["rmsDb"] + ra["actualGainDb"]
        lb = rb["rmsDb"] + rb["actualGainDb"]
        diff = round(lb - la, 2)
        rb["jump"] = {"prev": a, "db": diff}
        if abs(diff) > max_jump:
            rb["errors"].append({"code": "jump", "sev": "warn", "msg":
                "与上一段 %s 响度跳变 %+.1f dB(阈值 %.1f dB),听感会忽大忽小" % (
                    a, diff, max_jump)})

    for did, r in results.items():
        if any(e["sev"] == "bad" for e in r["errors"]):
            blocked.append(did)
    return {"settings": settings, "items": results, "order": order, "blocked": blocked}

def sanitize_leveling_plan(plan, report):
    """
    按实时校审报告纠正方案状态(纵深防御, 不相信库里的 confirmed 标记):
    - 存在 bad 阻塞错误(有效语音不足/通道不一致/增益越界/混音削波)的片段强制 pending;
    - 未绑定旁白(不在报告里)的片段同样强制 pending;
    - 仅无阻塞且 status=confirmed 的片段保留 confirmed, 其增益才会进入确认版混音/导出。
    返回全量 plan(items 保留, 仅纠正 status), 可直接喂给 render_mix/build_script/复演导出。
    """
    safe = {"settings": (plan or {}).get("settings", default_level_settings()), "items": {}}
    for did, item in ((plan or {}).get("items") or {}).items():
        it = dict(item)
        r = report["items"].get(did)
        bad = r is not None and any(e["sev"] == "bad" for e in r["errors"])
        if bad or r is None:
            it["status"] = "pending"
            if not it.get("accepted_reason"):
                it.pop("accepted_reason", None)
        # 非 confirmed 不允许残留确认态; confirmed 仅在无阻塞时保留
        if it.get("status") != "confirmed":
            it["status"] = "pending"
        safe["items"][did] = it
    return safe

# ---------------------------------------------------------------- 旁白多版本拼接

SPLICE_DEFAULTS = {"zeroxMs": 2.0,        # 切点距零交叉容差(ms, 硬切边)
                   "gapWarnS": 0.05,      # 静音缺口告警阈值(s)
                   "seamCeilingDb": -1.0} # 接缝峰值上限(dBFS)

def zero_cross_dist_ms(samples, nch, fr, t, win_ms=12.0):
    """切点 t(秒)距最近过零点的距离(ms), 取第一声道。无过零返回窗宽。"""
    n = len(samples) // nch
    if n < 2:
        return 0.0
    c0 = max(1, min(n - 1, int(round(t * fr))))
    w = max(1, int(win_ms / 1000.0 * fr))
    best = None
    for i in range(max(1, c0 - w), min(n, c0 + w)):
        a = samples[(i - 1) * nch]; b = samples[i * nch]
        if (a < 0) != (b < 0):
            d = abs(i - c0)
            if best is None or d < best:
                best = d
    return (best / fr * 1000.0) if best is not None else float(win_ms)

def render_splice(segments, narr_clips, nch, fr):
    """
    按拼接段合成旁白 PCM(线性交叉淡化, 与前端 buildSpliceBuffer 同算法)。
    segments: [{take_id,in,out,xfade,gap}]  xfade/gap 相对前一段(秒)。
    缺失/越界段跳过(由 splice_report 负责报错)。
    返回 (samples, duration, layout, seams, clip_frames):
    layout: [{seg,take_id,in,out,xfade,gap,comp_start,comp_end}](秒, 仅有效段)
    seams:   [{seg,t,xfade,peak_db,clip_frames,clip_t}]  seg 为后一段序号
    """
    valid = []
    for idx, sg in enumerate(segments or []):
        clip = narr_clips.get(str(sg.get("take_id")))
        if not clip:
            continue
        s, c, f = clip
        s = convert(s, c, f, nch, fr)
        total = len(s) // nch
        f0 = max(0, min(total, int(round(float(sg.get("in", 0.0)) * fr))))
        f1 = max(f0, min(total, int(round(float(sg.get("out", 0.0)) * fr))))
        if f1 <= f0:
            continue
        valid.append({"seg": idx, "take_id": sg.get("take_id"),
                      "in": float(sg.get("in", 0.0)), "out": float(sg.get("out", 0.0)),
                      "pcm": s[f0 * nch:f1 * nch], "frames": f1 - f0,
                      "xfade": max(0.0, float(sg.get("xfade") or 0.0)),
                      "gap": max(0.0, float(sg.get("gap") or 0.0))})
    # 布局(帧): 段 i 起点 = 前段终点 - xfade + gap
    pos = 0
    for k, v in enumerate(valid):
        start = 0 if k == 0 else pos - int(round(v["xfade"] * fr)) + int(round(v["gap"] * fr))
        v["start_f"] = max(0, start)
        pos = v["start_f"] + v["frames"]
    total = pos
    comp = array("f", [0.0]) * (total * nch)
    # 加权累加: 头部 xfade 线性淡入, 尾部(下一段 xfade)线性淡出, 重叠区权重和为 1
    for k, v in enumerate(valid):
        xf_in = int(round(v["xfade"] * fr)) if k > 0 else 0
        xf_out = int(round(valid[k + 1]["xfade"] * fr)) if k + 1 < len(valid) else 0
        pcm = v["pcm"]; nf = v["frames"]; s0 = v["start_f"]
        for j in range(nf):
            w = 1.0
            if xf_in > 0 and j < xf_in:
                w = j / xf_in
            if xf_out > 0:
                tail = nf - j
                if tail <= xf_out:
                    tw = tail / xf_out
                    if tw < w:
                        w = tw
            base = (s0 + j) * nch; jb = j * nch
            for c in range(nch):
                comp[base + c] += pcm[jb + c] * w
    out = array("h")
    clip_frames = 0
    for i in range(total):
        clipped = False
        base = i * nch
        for c in range(nch):
            v = comp[base + c]
            if v > 32767 or v < -32768:
                clipped = True
                v = 32767 if v > 32767 else -32768
            out.append(int(v))
        if clipped:
            clip_frames += 1
    seams = []
    for k in range(1, len(valid)):
        v = valid[k]
        s0 = v["start_f"]
        xf_f = int(round(v["xfade"] * fr))
        if xf_f > 0:
            w0, w1 = s0, min(total, s0 + xf_f)
        else:
            half = int(0.005 * fr)
            w0, w1 = max(0, s0 - half), min(total, s0 + half)
        mx = 0.0; nclip = 0; first = None
        for i in range(w0, w1):
            base = i * nch; m2 = 0.0; cl = False
            for c in range(nch):
                x = comp[base + c]
                if abs(x) > m2: m2 = abs(x)
                if x > 32767 or x < -32768: cl = True
            if cl:
                nclip += 1
                if first is None: first = i
            if m2 > mx: mx = m2
        seams.append({"seg": v["seg"], "t": round(s0 / fr, 4), "xfade": v["xfade"],
                      "peak_db": round(dbfs(max(mx, 1e-6) / 32768.0), 2),
                      "clip_frames": nclip,
                      "clip_t": round((first if first is not None else s0) / fr, 4)})
    layout = [{"seg": v["seg"], "take_id": v["take_id"], "in": v["in"], "out": v["out"],
               "xfade": v["xfade"], "gap": v["gap"],
               "comp_start": round(v["start_f"] / fr, 4),
               "comp_end": round((v["start_f"] + v["frames"]) / fr, 4)} for v in valid]
    return out, (total / fr if fr else 0.0), layout, seams, clip_frames

def splice_report(st, plans, src_nch, src_fr, src_dur, narr_clips):
    """
    拼接方案校审(纯计算, 不写库): 采样格式/片段重叠/零交叉距离/接缝峰值/
    静音缺口/成片时长, 以及锚点倒序、来源缺失、接缝削波、合成后压住对白/关键声。
    bad 级错误使方案保持待处理(时码定位到成片)。返回 {settings, items, order, blocked}。
    """
    settings = dict(SPLICE_DEFAULTS)
    settings.update((plans or {}).get("settings") or {})
    items = {}
    placements = st["placements"].get("placements", {})
    desc_by_id = {d["id"]: d for d in st["descriptions"]}
    narr_name = {str(n["id"]): n.get("name", str(n["id"])) for n in st["narrations"]}
    for did, plan in ((plans or {}).get("items") or {}).items():
        errors = []
        segments = plan.get("segments") or []
        anchors = plan.get("anchors") or {}
        p = placements.get(did) or {}
        start = float(p.get("start", (desc_by_id.get(did) or {}).get("start", 0) or 0))
        # 1) 来源缺失 / 采样格式 / 切点范围
        valid = []
        for idx, sg in enumerate(segments):
            tid = str(sg.get("take_id"))
            clip = narr_clips.get(tid)
            nm = narr_name.get(tid, tid)
            if clip is None:
                errors.append({"code": "missing", "sev": "bad", "msg":
                    "来源缺失:第%d段引用的旁白录音 %s 已不存在,请重新挂接" % (idx + 1, nm)})
                continue
            if src_nch is None or (clip[1], clip[2]) != (src_nch, src_fr):
                errors.append({"code": "format", "sev": "bad", "msg":
                    "采样格式不一致:第%d段「%s」为 %dch/%dHz,工程为 %dch/%dHz,无法可靠拼接" % (
                        idx + 1, nm, clip[1], clip[2], src_nch or 0, src_fr or 0)})
                continue
            dur = (len(clip[0]) // clip[1]) / clip[2]
            i0 = float(sg.get("in", 0.0)); i1 = float(sg.get("out", 0.0))
            if not (0.0 <= i0 < i1 <= dur + 1e-6):
                errors.append({"code": "range", "sev": "bad", "msg":
                    "切点越界:第%d段 [%s–%s] 超出「%s」时长 %s" % (
                        idx + 1, fmt_tc(i0), fmt_tc(i1), nm, fmt_tc(dur))})
                continue
            valid.append((idx, sg, clip))
        if not segments:
            errors.append({"code": "empty", "sev": "bad", "msg":
                "拼接轨为空:在候选录音波形上拖出选区,再「加入拼接轨」"})
        # 2) 片段重叠: 交叉淡化越界 / 前后淡化之和超过中段(三重叠加)
        durs = {idx: float(sg["out"]) - float(sg["in"]) for idx, sg, _ in valid}
        xfs = {idx: max(0.0, float(sg.get("xfade") or 0.0)) for idx, sg, _ in valid}
        for k in range(1, len(valid)):
            idx = valid[k][0]; pidx = valid[k - 1][0]
            if xfs[idx] > min(durs[pidx], durs[idx]) + 1e-6:
                errors.append({"code": "overlap", "sev": "bad", "msg":
                    "片段重叠:第%d段交叉淡化 %.3fs 超过相邻段时长(%.3fs/%.3fs)" % (
                        idx + 1, xfs[idx], durs[pidx], durs[idx])})
        for k in range(1, len(valid) - 1):
            idx = valid[k][0]
            s = xfs[idx] + xfs[valid[k + 1][0]]
            if s > durs[idx] + 1e-6:
                errors.append({"code": "overlap", "sev": "bad", "msg":
                    "片段重叠:第%d段前后交叉淡化之和 %.3fs 超过本段时长 %.3fs,会出现三重叠加" % (
                        idx + 1, s, durs[idx])})
        # 3) 合成(存在 bad 时先修来源/格式/重叠, 不合成)
        comp_dur = 0.0; layout = []; seams = []
        if valid and not any(e["sev"] == "bad" for e in errors):
            _sm, comp_dur, layout, seams, _cf = render_splice(
                segments, narr_clips, src_nch, src_fr)
        lay_by_seg = {l["seg"]: l for l in layout}
        # 4) 零交叉距离(仅内部接缝的硬切边, 有淡化保护的边不报)
        zerox = []
        for k in range(1, len(valid)):
            idx, sg, clip = valid[k]
            pidx, psg, pclip = valid[k - 1]
            if xfs[idx] >= 0.005:
                continue
            for label, c, t in (("第%d段出点" % (pidx + 1), pclip, float(psg["out"])),
                                ("第%d段入点" % (idx + 1), clip, float(sg["in"]))):
                d = zero_cross_dist_ms(c[0], c[1], c[2], t)
                zerox.append({"seg": idx, "label": label, "t": round(t, 4),
                              "dist_ms": round(d, 2)})
        if zerox:
            worst = max(zerox, key=lambda z: z["dist_ms"])
            if worst["dist_ms"] > float(settings["zeroxMs"]):
                errors.append({"code": "zerox", "sev": "warn", "msg":
                    "零交叉距离:%s %s 距最近过零 %.1fms(容差 %.1fms),硬切可能爆音,建议微调切点或加交叉淡化" % (
                        worst["label"], fmt_tc(worst["t"]), worst["dist_ms"],
                        float(settings["zeroxMs"]))})
        # 5) 静音缺口
        for l in layout:
            if l["gap"] > float(settings["gapWarnS"]):
                errors.append({"code": "gap", "sev": "warn", "msg":
                    "静音缺口:第%d段前留有 %.3fs 静音(成片时码 %s),听感会断气" % (
                        l["seg"] + 1, l["gap"], fmt_tc(start + l["comp_start"]))})
        # 6) 接缝峰值 / 接缝削波
        for si, sm in enumerate(seams):
            at = fmt_tc(start + sm["t"])
            if sm["clip_frames"] > 0:
                errors.append({"code": "seamclip", "sev": "bad", "msg":
                    "接缝削波:接缝%d(%s)%d 个采样越界,首次 %s,请缩短交叉淡化或移开切点" % (
                        si + 1, at, sm["clip_frames"], fmt_tc(start + sm["clip_t"]))})
            elif sm["peak_db"] > float(settings["seamCeilingDb"]):
                errors.append({"code": "seampeak", "sev": "warn", "msg":
                    "接缝峰值:接缝%d(%s)峰值 %.1f dBFS 超过上限 %.1f dBFS" % (
                        si + 1, at, sm["peak_db"], float(settings["seamCeilingDb"]))})
        # 7) 锚点倒序(同步锚点在成片时间轴上必须递增)
        anchor_pts = []
        for idx, sg, clip in valid:
            tid = str(sg.get("take_id"))
            if tid not in anchors:
                continue
            a = float(anchors[tid])
            lay = lay_by_seg.get(idx)
            if lay is None:
                continue
            if not (float(sg["in"]) - 1e-6 <= a <= float(sg["out"]) + 1e-6):
                errors.append({"code": "anchor", "sev": "warn", "msg":
                    "锚点未落入选区:「%s」锚点 %s 不在第%d段选区 [%s–%s] 内,对齐参考失效" % (
                        narr_name.get(tid, tid), fmt_tc(a), idx + 1,
                        fmt_tc(float(sg["in"])), fmt_tc(float(sg["out"])))})
                continue
            anchor_pts.append({"seg": idx, "take_id": tid, "anchor": a,
                               "comp_t": round(lay["comp_start"] + (a - float(sg["in"])), 4)})
        for k in range(1, len(anchor_pts)):
            if anchor_pts[k]["comp_t"] <= anchor_pts[k - 1]["comp_t"] + 1e-9:
                errors.append({"code": "anchororder", "sev": "bad", "msg":
                    "锚点倒序:第%d段锚点(成片 %s)不晚于第%d段(成片 %s),拼接段顺序可能有误" % (
                        anchor_pts[k]["seg"] + 1, fmt_tc(start + anchor_pts[k]["comp_t"]),
                        anchor_pts[k - 1]["seg"] + 1, fmt_tc(start + anchor_pts[k - 1]["comp_t"]))})
        # 8) 合成后压住对白/关键声(按成片区间)
        end = start + comp_dur
        if comp_dur > 0:
            for g in st["dialogue"]:
                ov = max(0.0, min(end, g["end"]) - max(start, g["start"]))
                if ov > 0.05:
                    errors.append({"code": "dialogue",
                        "sev": "warn" if p.get("duck") else "bad", "msg":
                        "合成后压住对白:与「%s」交叠 %.2fs(%s–%s)%s" % (
                            (g.get("text") or "")[:18], ov,
                            fmt_tc(max(start, g["start"])), fmt_tc(min(end, g["end"])),
                            ",已压低原声仍需确认清晰度" if p.get("duck") else "")})
            for k2 in st["keysounds"]:
                if k2.get("maskable") is False:
                    ov = max(0.0, min(end, k2["end"]) - max(start, k2["start"]))
                    if ov > 0.03:
                        errors.append({"code": "keysound", "sev": "bad", "msg":
                            "合成后压住关键声「%s」 %.2fs(%s–%s),情节线索将丢失" % (
                                k2.get("label"), ov, fmt_tc(max(start, k2["start"])),
                                fmt_tc(min(end, k2["end"])))})
            # 9) 成片时长 vs 当前空档
            nxt = min([g["start"] for g in st["dialogue"] if g["start"] >= start] or [src_dur])
            if end > nxt + 0.05:
                errors.append({"code": "duration", "sev": "warn", "msg":
                    "成片时长:合成 %.2fs 超出当前空档(到 %s) %.2fs" % (
                        comp_dur, fmt_tc(nxt), end - nxt)})
        seg_out = []
        for idx, sg, clip in valid:
            lay = lay_by_seg.get(idx) or {}
            seg_out.append({"seg": idx, "take_id": sg.get("take_id"),
                            "name": narr_name.get(str(sg.get("take_id")), str(sg.get("take_id"))),
                            "in": float(sg.get("in", 0.0)), "out": float(sg.get("out", 0.0)),
                            "xfade": xfs[idx], "gap": max(0.0, float(sg.get("gap") or 0.0)),
                            "comp_start": lay.get("comp_start"), "comp_end": lay.get("comp_end")})
        items[did] = {"desc_id": did, "start": start, "duration": round(comp_dur, 3),
                      "segments": seg_out, "seams": seams, "anchors": anchor_pts,
                      "zerox": zerox,
                      "sources": [{"take_id": t, "name": narr_name.get(t, t)}
                                  for t in dict.fromkeys(str(sg.get("take_id"))
                                                         for sg in segments)],
                      "status": plan.get("status", "pending"),
                      "acceptedReason": plan.get("accepted_reason", ""),
                      "errors": errors}
    order = sorted(items.keys(), key=lambda d: items[d]["start"])
    blocked = [d for d in order if any(e["sev"] == "bad" for e in items[d]["errors"])]
    return {"settings": settings, "items": items, "order": order, "blocked": blocked}

def sanitize_splice(plans, report):
    """
    按实时校审纠正拼接方案状态(纵深防御, 不相信库里的 confirmed 标记):
    存在 bad 阻塞(锚点倒序/来源缺失/接缝削波/压住对白关键声/格式不一致等)或
    无报告的方案强制 pending; 仅无阻塞且 status=confirmed 的方案保留 confirmed,
    其合成结果才会进入配平/混音/脚本/复演。
    """
    safe = {"settings": (plans or {}).get("settings") or dict(SPLICE_DEFAULTS), "items": {}}
    for did, plan in ((plans or {}).get("items") or {}).items():
        p2 = dict(plan)
        r = (report.get("items") or {}).get(did)
        bad = r is not None and any(e["sev"] == "bad" for e in r["errors"])
        if bad or r is None:
            p2["status"] = "pending"
            if not p2.get("accepted_reason"):
                p2.pop("accepted_reason", None)
        if p2.get("status") != "confirmed":
            p2["status"] = "pending"
        safe["items"][did] = p2
    return safe

def splice_content(plan):
    """参与确认状态的内容(变更即需重新确认): 挂接录音/锚点/拼接段。"""
    def rnd(x):
        return round(float(x), 4)
    return {"takes": [str(t) for t in (plan.get("takes") or [])],
            "anchors": {str(k): rnd(v) for k, v in (plan.get("anchors") or {}).items()},
            "segments": [{"take_id": str(s.get("take_id")), "in": rnd(s.get("in", 0)),
                          "out": rnd(s.get("out", 0)), "xfade": rnd(s.get("xfade") or 0),
                          "gap": rnd(s.get("gap") or 0)}
                         for s in (plan.get("segments") or [])]}

def apply_splice_inject(st, narr_clips, nch, fr, report=None):
    """
    确认且通过实时校审的拼接方案 -> 合成 PCM 注入 narr_clips(键 splice:<descId>)。
    返回 (placements覆盖:{did:placement}, 虚拟旁白:{did:info}); 调用方自行合并,
    本函数不修改 st。阻塞/待处理方案不注入, 描述卡回退到原整段绑定。
    """
    plans = st.get("splice") or {}
    items = plans.get("items") or {}
    confirmed = {d: p for d, p in items.items() if p.get("status") == "confirmed"}
    if not confirmed:
        return {}, {}
    if report is None:
        smeta = st.get("source") or {}
        report = splice_report(st, plans, nch, fr, smeta.get("duration", 0.0), narr_clips)
    overrides, injected = {}, {}
    for did, plan in confirmed.items():
        r = report["items"].get(did)
        if not r or any(e["sev"] == "bad" for e in r["errors"]):
            continue
        samples, dur, _layout, _seams, _cf = render_splice(
            plan.get("segments") or [], narr_clips, nch, fr)
        vid = "splice:%s" % did
        narr_clips[vid] = (samples, nch, fr)
        base = dict((st["placements"].get("placements", {}).get(did)) or {})
        base["narration_id"] = vid
        overrides[did] = base
        names = []
        for s in r["sources"]:
            if s["name"] not in names:
                names.append(s["name"])
        injected[did] = {"id": vid, "name": "拼接·%s(%s)" % (did, "+".join(names)),
                         "duration": round(dur, 3), "framerate": fr, "channels": nch,
                         "levels": level_curves(samples, nch, fr),
                         "splice": {"desc_id": did, "segments": r["segments"],
                                    "anchors": r.get("anchors", []),
                                    "sources": r["sources"]}}
    return overrides, injected

def spliced_state(st, overrides, injected):
    """把拼接注入(覆盖 placements/追加虚拟旁白)合并到 st 的浅拷贝, 供校审/导出使用。"""
    if not overrides:
        return st
    st2 = dict(st)
    pl = dict(st["placements"].get("placements", {}))
    pl.update(overrides)
    st2["placements"] = {"placements": pl, "settings": st["placements"].get("settings", {})}
    st2["narrations"] = list(st["narrations"]) + list(injected.values())
    return st2

def save_splice(pid, plans):
    conn = db()
    conn.execute("INSERT INTO splice(project_id,data_json) VALUES(?,?) "
                 "ON CONFLICT(project_id) DO UPDATE SET data_json=excluded.data_json",
                 (pid, json.dumps(plans, ensure_ascii=False)))
    conn.commit()
    conn.close()

# ---------------------------------------------------------------- 原声让位包络
#
# 每张描述卡可保存一个可编辑包络: 关键点 [{t(成片时间, 秒), g(线性增益 0..1)}],
# 关键点之间逐帧线性插值, 覆盖区外原声增益恒为 1。确认后:
#   - 该卡不再套用旧版固定斜坡(build_duck_env 按 exclude_ids 跳过);
#   - 多卡包络按逐帧 min 叠加(与旧斜坡的叠加口径一致);
#   - 包络同时驱动服务端 WAV 混音、描述脚本与复演 JSON。
# 无自定义包络(或未确认)的卡(含全部旧项目)完全沿用现有固定压低方式。

DUCKENV_DEFAULTS = {"minGain": 0.05,        # 关键点增益下限(线性, 约 -26 dB)
                    "maxGain": 1.0,         # 关键点增益上限(禁止提升原声)
                    "maxSlopePerSec": 6.0,  # 单段斜率上限(线性增益/秒, 0.15s 旧斜坡约 4.3)
                    "restoreTol": 0.02,     # 结束后恢复判定: 末尾点增益须 ≥ 1-容差
                    "protectDuckMin": 0.92} # 保护区内原声增益下限

def duckenv_default_points(p, dur, settings):
    """
    以旧版固定斜坡为模板生成初始四点包络(渐入/保持/恢复), 与 build_duck_env 同形状:
    保持深度取 settings.duck_to, 前后各扩 duck_pad, 斜坡 0.15s。
    """
    duck_to = float(settings.get("duck_to", 0.35))
    pad = float(settings.get("duck_pad", 0.15))
    ramp = 0.15
    start = float(p.get("start", 0.0))
    t0 = max(0.0, start - pad)
    t3 = start + float(dur) + pad
    t1 = t0 + ramp
    t2 = max(t1, t3 - ramp)
    return [{"t": round(t0, 4), "g": 1.0},
            {"t": round(t1, 4), "g": round(duck_to, 4)},
            {"t": round(t2, 4), "g": round(duck_to, 4)},
            {"t": round(t3, 4), "g": 1.0}]

class DuckEnvComposer:
    """
    逐帧包络增量合成器(与 wave 后端同一套帧语义)。
    env 为全片每帧原声增益; upsert/remove 只重算受影响帧区间:
    修改某卡时, 旧关键点边界与新关键点边界的并集中, 凡曾由该卡决定(min 中命中)
    或落入新多边形的帧才重算, 其余帧不动。
    """
    def __init__(self, n_frames, fr):
        self.n = n_frames
        self.fr = fr
        self.env = [1.0] * n_frames
        self._owners = {}   # did -> 归一化关键点 [(t,g)]
        self._bounds = {}   # did -> (f0,f1) 旧影响帧边界

    def _seg_gain(self, pts, f):
        t = f / self.fr
        if t <= pts[0][0]:
            return pts[0][1]
        if t >= pts[-1][0]:
            return pts[-1][1]
        for k in range(len(pts) - 1):
            t0, g0 = pts[k]; t1, g1 = pts[k + 1]
            if t0 <= t <= t1:
                if t1 == t0:
                    return min(g0, g1)
                return g0 + (g1 - g0) * (t - t0) / (t1 - t0)
        return 1.0

    def _bounds_of(self, pts):
        f0 = max(0, int(pts[0][0] * self.fr))
        f1 = min(self.n, max(f0 + 1, int(pts[-1][0] * self.fr) + 1))
        return f0, f1

    def _rebuild_range(self, f0, f1, skip=None):
        """在 [f0,f1) 内按其余已登记关键点重算(跳过 skip 卡)。"""
        owners = self._owners
        for f in range(f0, f1):
            g = 1.0
            for did, pts in owners.items():
                if did == skip:
                    continue
                if f < pts[0][0] * self.fr - 1 or f > pts[-1][0] * self.fr + 1:
                    continue
                v = self._seg_gain(pts, f)
                if v < g:
                    g = v
            self.env[f] = g

    def upsert(self, did, raw_pts):
        """写入/替换某卡包络, 只重算新旧影响区间的并集。返回 (f0,f1)。"""
        pts = sorted((float(q["t"]), max(0.0, min(1.0, float(q["g"])))) for q in raw_pts)
        nf0, nf1 = self._bounds_of(pts)
        old = self._bounds.get(did)
        r0, r1 = (nf0, nf1) if old is None else (min(old[0], nf0), max(old[1], nf1))
        self._rebuild_range(r0, r1, skip=did)
        self._owners[did] = pts
        self._bounds[did] = (nf0, nf1)
        for f in range(nf0, nf1):
            v = self._seg_gain(pts, f)
            if v < self.env[f]:
                self.env[f] = v
        return nf0, nf1

    def remove(self, did):
        """拆除某卡包络, 只重算其旧影响区间。"""
        old = self._bounds.pop(did, None)
        self._owners.pop(did, None)
        if old is None:
            return None
        self._rebuild_range(old[0], old[1], skip=did)
        return old

def _protected_intervals(st):
    """保护区 = 全部对白 + 不可遮盖(maskable=false)关键声, 返回成片时间区间表。"""
    zones = [{"kind": "对白", "start": float(g["start"]), "end": float(g["end"]),
              "label": (g.get("text") or g.get("speaker") or "对白")[:18]}
             for g in st.get("dialogue") or []]
    zones += [{"kind": "关键声", "start": float(k["start"]), "end": float(k["end"]),
               "label": (k.get("label") or "关键声")[:18]}
              for k in st.get("keysounds") or [] if k.get("maskable") is False]
    zones.sort(key=lambda z: z["start"])
    return zones

def duckenv_report(st, plans, src_dur, prepared_durs, gsettings=None):
    """
    让位包络校审(纯计算, 不写库), 逐卡检查:
      pointcount  关键点不足(渐入/保持/恢复至少 4 点)
      order       关键点倒序/重合
      gainrange   增益越界(允许 0..1, 不允许提升原声)
      slope       斜率过陡(瞬时塌陷/恢复, 定位段首时码与 dB/s)
      restore     结束后未恢复到 1
      protect     保护区误压(对白/不可遮盖关键声被压低, 定位保护区与最深帧时码)
      overlap     相邻包络叠加过深(交叠区合成增益低于两者最深保持值, 定位时码)
    bad 阻塞确认; 同 splice/leveling 口径, 返回 {settings, items, order, blocked}。
    prepared_durs: {did: 压缩后旁白时长}(与帧渲染同口径), 缺省时用绑定素材原长。
    """
    gsettings = gsettings or st.get("placements", {}).get("settings", {}) or {}
    settings = dict(DUCKENV_DEFAULTS)
    settings.update((plans or {}).get("settings") or {})
    gmin, gmax = float(settings["minGain"]), float(settings["maxGain"])
    max_slope = float(settings["maxSlopePerSec"])
    tol = float(settings["restoreTol"])
    protect_floor = float(settings["protectDuckMin"])
    placements = st.get("placements", {}).get("placements", {})
    desc_by_id = {d["id"]: d for d in st.get("descriptions") or []}
    zones = _protected_intervals(st)

    # 归一化关键点 + 结构性检查(不依赖音频)
    raw_items = {}
    for did, plan in ((plans or {}).get("items") or {}).items():
        errors = []
        raw = plan.get("points") or []
        pts = []
        for q in raw:
            try:
                pts.append({"t": float(q["t"]), "g": float(q["g"])})
            except (TypeError, ValueError, KeyError):
                continue
        p = placements.get(did) or {}
        start = float(p.get("start", (desc_by_id.get(did) or {}).get("start", 0) or 0))
        dur = float((prepared_durs or {}).get(did) or 0.0)
        pts_sorted = sorted(pts, key=lambda q: q["t"])

        if len(pts_sorted) < 4:
            errors.append({"code": "pointcount", "sev": "bad", "msg":
                "关键点不足:仅 %d 个点,让位包络至少需要 渐入/保持/恢复 4 个关键点" % len(pts_sorted)})
        # 倒序(原序与时序不一致或存在重合时间)
        if any(pts[i]["t"] > pts[i + 1]["t"] + 1e-9 for i in range(len(pts) - 1)):
            bad_i = next(i for i in range(len(pts) - 1) if pts[i]["t"] > pts[i + 1]["t"])
            errors.append({"code": "order", "sev": "bad", "msg":
                "关键点倒序:第%d点 %s 晚于第%d点 %s,请按时间重排" % (
                    bad_i + 1, fmt_tc(pts[bad_i]["t"]), bad_i + 2, fmt_tc(pts[bad_i + 1]["t"]))})
        if any(abs(pts_sorted[i]["t"] - pts_sorted[i + 1]["t"]) < 1e-6
               for i in range(len(pts_sorted) - 1)):
            dup = next(pts_sorted[i]["t"] for i in range(len(pts_sorted) - 1)
                       if abs(pts_sorted[i]["t"] - pts_sorted[i + 1]["t"]) < 1e-6)
            errors.append({"code": "order", "sev": "bad", "msg":
                "关键点重合:存在两个关键点落在同一时码 %s" % fmt_tc(dup)})
        # 增益越界(0..1: 不允许负增益/提升原声)
        for i, q in enumerate(pts_sorted):
            if q["g"] < gmin - 1e-9 or q["g"] > gmax + 1e-9:
                errors.append({"code": "gainrange", "sev": "bad", "msg":
                    "增益越界:关键点 %s 增益 %.2f,允许范围 %.2f~%.2f(原声让位只可衰减不可提升)" % (
                        fmt_tc(q["t"]), q["g"], gmin, gmax)})
                break
        # 斜率过陡 + 收集保持深度
        hold_floor = 1.0
        for i in range(len(pts_sorted) - 1):
            t0, g0 = pts_sorted[i]["t"], pts_sorted[i]["g"]
            t1, g1 = pts_sorted[i + 1]["t"], pts_sorted[i + 1]["g"]
            dt = t1 - t0
            if dt <= 1e-6:
                continue
            slope = abs(g1 - g0) / dt
            if g0 < hold_floor: hold_floor = g0
            if g1 < hold_floor: hold_floor = g1
            if slope > max_slope:
                db_s = abs(dbfs(max(g1, 1e-6)) - dbfs(max(g0, 1e-6))) / dt
                errors.append({"code": "slope", "sev": "bad", "msg":
                    "斜率过陡:%s–%s 段增益变化 %.2f(%.1f dB/s,上限 %.1f/s),"
                    "原声会瞬时塌陷或恢复,请拉长渐入/恢复" % (
                        fmt_tc(t0), fmt_tc(t1), abs(g1 - g0), db_s, max_slope)})
        # 结束后未恢复: 最后一个关键点必须回到 1(容差 tol)
        if pts_sorted and pts_sorted[-1]["g"] < 1.0 - tol:
            errors.append({"code": "restore", "sev": "bad", "msg":
                "结束后未恢复:末关键点 %s 增益 %.2f(< %.2f),旁白过后原声仍被压低,请把恢复点拉回 1.0" % (
                    fmt_tc(pts_sorted[-1]["t"]), pts_sorted[-1]["g"], 1.0 - tol)})
        # 越出片长
        if src_dur and pts_sorted and (pts_sorted[0]["t"] < -1e-6 or pts_sorted[-1]["t"] > src_dur + 1e-6):
            errors.append({"code": "order", "sev": "bad", "msg":
                "关键点越出正片:包络区间 %s–%s 超出片长 %s" % (
                    fmt_tc(pts_sorted[0]["t"]), fmt_tc(pts_sorted[-1]["t"]), fmt_tc(src_dur))})
        raw_items[did] = {"plan": plan, "pts": pts_sorted, "errors": errors,
                          "start": start, "dur": dur, "hold_floor": hold_floor}

    # 保护区误压(多边形采样, 定位最深帧)与相邻叠加过深(成对区间)
    SAMPLE_DT = 0.002
    for did, info in raw_items.items():
        pts = info["pts"]
        if not pts:
            continue
        t_a, t_b = pts[0]["t"], pts[-1]["t"]
        # 自动保护区(对白+不可遮盖关键声) + 该卡人工锁定保护区
        manual = []
        mp = info["plan"].get("protected")
        if isinstance(mp, list):
            for z in mp:
                if isinstance(z, dict) and "start" in z and "end" in z:
                    try:
                        manual.append({"kind": "人工保护区", "start": float(z["start"]),
                                       "end": float(z["end"]),
                                       "label": str(z.get("label") or "自定义")[:18]})
                    except (TypeError, ValueError):
                        continue
        card_zones = zones + manual
        def gain_at(t):
            if t <= pts[0]["t"]: return pts[0]["g"]
            if t >= pts[-1]["t"]: return pts[-1]["g"]
            for k in range(len(pts) - 1):
                t0, g0 = pts[k]["t"], pts[k]["g"]; t1, g1 = pts[k + 1]["t"], pts[k + 1]["g"]
                if t0 <= t <= t1:
                    return g0 + (g1 - g0) * (t - t0) / (t1 - t0) if t1 > t0 else min(g0, g1)
            return 1.0
        for z in card_zones:
            lo, hi = max(t_a, z["start"]), min(t_b, z["end"])
            if hi - lo <= 1e-6:
                continue
            worst_t, worst_g = None, protect_floor
            n = max(1, int((hi - lo) / SAMPLE_DT))
            for k in range(n + 1):
                t = min(hi, lo + (hi - lo) * k / n)
                g = gain_at(t)
                if g < worst_g - 1e-9:
                    worst_g, worst_t = g, t
            if worst_t is not None:
                info["errors"].append({"code": "protect", "sev": "bad", "msg":
                    "保护区误压:%s「%s」%s–%s 内原声被压到 %.2f(最深 %s),"
                    "对白/不可遮盖关键声须锁为保护区,请上抬或移开包络" % (
                        z["kind"], z["label"], fmt_tc(z["start"]), fmt_tc(z["end"]),
                        worst_g, fmt_tc(worst_t))})
        # 相邻包络叠加: 交叠区逐帧 min 低于两者最深保持值(刻意双层让位除外需显式重做)
        for did2, info2 in raw_items.items():
            if did2 <= did:
                continue
            q2 = info2["pts"]
            if not q2:
                continue
            lo, hi = max(t_a, q2[0]["t"]), min(t_b, q2[-1]["t"])
            if hi - lo <= 1e-6:
                continue
            floor = max(info["hold_floor"], info2["hold_floor"])  # 叠加后不应比单层更深
            def gain2_at(t):
                if t <= q2[0]["t"]: return q2[0]["g"]
                if t >= q2[-1]["t"]: return q2[-1]["g"]
                for k in range(len(q2) - 1):
                    t0, g0 = q2[k]["t"], q2[k]["g"]; t1, g1 = q2[k + 1]["t"], q2[k + 1]["g"]
                    if t0 <= t <= t1:
                        return g0 + (g1 - g0) * (t - t0) / (t1 - t0) if t1 > t0 else min(g0, g1)
                return 1.0
            worst_t, worst_g = None, floor
            n = max(1, int((hi - lo) / SAMPLE_DT))
            for k in range(n + 1):
                t = min(hi, lo + (hi - lo) * k / n)
                g = min(gain_at(t), gain2_at(t))
                if g < worst_g - 1e-9:
                    worst_g, worst_t = g, t
            if worst_t is not None:
                info["errors"].append({"code": "overlap", "sev": "bad", "msg":
                    "相邻包络叠加过深:%s 与 %s 在 %s–%s 交叠,合成增益最低 %.2f(最深 %s),"
                    "低于单层保持 %.2f,两段让位会互相踩踏,请错开恢复/渐入" % (
                        did, did2, fmt_tc(lo), fmt_tc(hi), worst_g, fmt_tc(worst_t), floor)})

    items = {}
    for did, info in raw_items.items():
        plan = info["plan"]
        pts = info["pts"]
        manual_norm = []
        mp = plan.get("protected")
        if isinstance(mp, list):
            for z in mp:
                if isinstance(z, dict) and "start" in z and "end" in z:
                    try:
                        manual_norm.append({"start": round(float(z["start"]), 4),
                                            "end": round(float(z["end"]), 4),
                                            "label": str(z.get("label") or "自定义")[:18]})
                    except (TypeError, ValueError):
                        continue
        items[did] = {
            "desc_id": did, "start": info["start"], "duration": round(info["dur"], 3),
            "points": [{"t": round(q["t"], 4), "g": round(max(gmin, min(gmax, q["g"])), 4)}
                       for q in pts],
            "protected": manual_norm,
            "autoProtected": [{"kind": z["kind"], "start": z["start"], "end": z["end"],
                               "label": z["label"]} for z in zones],
            "span": [round(pts[0]["t"], 4), round(pts[-1]["t"], 4)] if pts else [0, 0],
            "holdDb": round(dbfs(max(info["hold_floor"], 1e-6)), 2),
            "status": plan.get("status", "pending"),
            "acceptedReason": plan.get("accepted_reason", ""),
            "errors": info["errors"]}
    order = sorted(items.keys(), key=lambda d: items[d]["start"])
    blocked = [d for d in order if any(e["sev"] == "bad" for e in items[d]["errors"])]
    return {"settings": settings, "items": items, "order": order, "blocked": blocked}

def sanitize_duckenv(plans, report):
    """
    按实时校审纠正包络方案状态(纵深防御, 同 leveling/splice):
    存在 bad 阻塞(倒序/越界/过陡/未恢复/误压保护区/叠加过深等)或无报告的方案
    强制 pending; 仅无阻塞且 status=confirmed 的方案保留 confirmed, 其关键点才进
    确认版混音/脚本/复演。注意: 不得替编辑者排序/夹幅 —— 倒序与增益越界必须持续
    阻塞到人工修正(帧级合成本身对增益做 0..1 防御性夹幅, 与确认状态无关)。
    """
    safe = {"settings": (plans or {}).get("settings") or dict(DUCKENV_DEFAULTS), "items": {}}
    rep_items = (report or {}).get("items") or {}
    for did, plan in ((plans or {}).get("items") or {}).items():
        it = dict(plan)
        r = rep_items.get(did)
        bad = r is not None and any(e["sev"] == "bad" for e in r["errors"])
        if bad or r is None or not it.get("points"):
            it["status"] = "pending"
            if not it.get("accepted_reason"):
                it.pop("accepted_reason", None)
        if it.get("status") != "confirmed":
            it["status"] = "pending"
        # 保留原始顺序与数值(仅四舍五入); span/holdDb 等派生信息取自报告
        it["points"] = [{"t": round(float(q.get("t", 0)), 4),
                         "g": round(float(q.get("g", 1)), 4)}
                        for q in it.get("points") or []]
        if r is not None:
            it["span"] = list(r.get("span") or [0, 0])
            it["holdDb"] = r.get("holdDb", EPS_DB)
        if not isinstance(it.get("protected"), list):
            it["protected"] = []
        safe["items"][did] = it
    return safe

def duckenv_content(plan):
    """参与确认状态的内容(变更即需重新确认): 关键点与人工保护区。"""
    return {"points": [{"t": round(float(q.get("t", 0)), 4),
                        "g": round(float(q.get("g", 1)), 4)}
                       for q in (plan.get("points") or [])],
            "protected": sorted(str(z) for z in (plan.get("protected") or []))}

def save_duckenv(pid, plans):
    conn = db()
    conn.execute("INSERT INTO duckenv(project_id,data_json) VALUES(?,?) "
                 "ON CONFLICT(project_id) DO UPDATE SET data_json=excluded.data_json",
                 (pid, json.dumps(plans, ensure_ascii=False)))
    conn.commit()
    conn.close()

# ---------------------------------------------------------------- 时码解析

def parse_tc(v):
    """接受 秒数 / 'MM:SS.mmm' / 'HH:MM:SS.mmm' / 'HH:MM:SS,mmm'。"""
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", ".")
    parts = s.split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        raise ValueError("无法解析时码: %r" % v)

def fmt_tc(t):
    t = max(0.0, float(t))
    h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
    return "%02d:%02d:%06.3f" % (h, m, s)

def norm_items(kind, items):
    """把上传的稿件条目规范化为统一结构。"""
    out = []
    for i, it in enumerate(items):
        d = dict(it)
        if "start" in d: d["start"] = parse_tc(d["start"])
        if "end" in d: d["end"] = parse_tc(d["end"])
        if "time" in d: d["time"] = parse_tc(d["time"])
        d.setdefault("id", "%s-%d" % (kind, i + 1))
        out.append(d)
    return out

# ---------------------------------------------------------------- 项目存取

def proj_dir(pid):
    d = os.path.join(DATA, str(pid))
    os.makedirs(d, exist_ok=True)
    return d

def get_state(pid):
    conn = db()
    proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        conn.close()
        return None
    assets = conn.execute(
        "SELECT * FROM assets WHERE project_id=? ORDER BY id", (pid,)).fetchall()
    pl = conn.execute("SELECT data_json FROM placements WHERE project_id=?", (pid,)).fetchone()
    lv = conn.execute("SELECT data_json FROM leveling WHERE project_id=?", (pid,)).fetchone()
    sp = conn.execute("SELECT data_json FROM splice WHERE project_id=?", (pid,)).fetchone()
    de = conn.execute("SELECT data_json FROM duckenv WHERE project_id=?", (pid,)).fetchone()
    revs = conn.execute(
        "SELECT id,created,summary,rationale FROM revisions WHERE project_id=? ORDER BY id DESC",
        (pid,)).fetchall()
    conn.close()

    state = {"project": {"id": proj["id"], "name": proj["name"], "created": proj["created"]},
             "source": None, "narrations": [], "dialogue": [], "scenes": [],
             "descriptions": [], "keysounds": [],
             "placements": {"placements": {}, "settings": {}},
             "leveling": {"settings": default_level_settings(), "items": {}},
             "splice": {"settings": dict(SPLICE_DEFAULTS), "items": {}},
             "duckenv": {"settings": dict(DUCKENV_DEFAULTS), "items": {}},
             "spliceResolved": {},
             "revisions": [dict(r) for r in revs]}
    for a in assets:
        data = json.loads(a["data_json"] or "{}")
        if a["kind"] == "source":
            data["name"] = a["name"]
            state["source"] = data
        elif a["kind"] == "narration":
            data["name"] = a["name"]
            data["id"] = a["id"]
            state["narrations"].append(data)
        elif a["kind"] in ("dialogue", "scenes", "descriptions", "keysounds"):
            state[a["kind"]] = data.get("items", [])
    if pl:
        state["placements"] = json.loads(pl["data_json"])
    if lv:
        state["leveling"] = json.loads(lv["data_json"])
    if sp:
        state["splice"] = json.loads(sp["data_json"])
    if de:
        state["duckenv"] = json.loads(de["data_json"])
    # 确认且健康的拼接方案: 实时校审 + 合成, 供前端显示合成时长/电平曲线
    sp_items = state["splice"].get("items") or {}
    if state["source"] and any(p.get("status") == "confirmed" for p in sp_items.values()):
        try:
            clips = load_narr_clips(pid)
            smeta = state["source"]
            rep = splice_report(state, state["splice"], smeta["channels"],
                                smeta["framerate"], smeta["duration"], clips)
            _ov, injected = apply_splice_inject(state, clips, smeta["channels"],
                                                smeta["framerate"], rep)
            state["spliceResolved"] = injected
        except (FileNotFoundError, KeyError):
            pass
    return state

def save_leveling(pid, plan):
    conn = db()
    conn.execute("INSERT INTO leveling(project_id,data_json) VALUES(?,?) "
                 "ON CONFLICT(project_id) DO UPDATE SET data_json=excluded.data_json",
                 (pid, json.dumps(plan, ensure_ascii=False)))
    conn.commit()
    conn.close()

def ensure_level_curves(pid):
    """为缺少 RMS/峰值曲线的旁白素材补算并落库。"""
    conn = db()
    rows = conn.execute("SELECT id,data_json,file_path FROM assets "
                        "WHERE project_id=? AND kind='narration'", (pid,)).fetchall()
    changed = False
    for r in rows:
        data = json.loads(r["data_json"] or "{}")
        if data.get("levels") or not r["file_path"] or not os.path.isfile(r["file_path"]):
            continue
        with open(r["file_path"], "rb") as f:
            s, c, fr = read_wav(f.read())
        data["levels"] = level_curves(s, c, fr)
        conn.execute("UPDATE assets SET data_json=? WHERE id=?",
                     (json.dumps(data, ensure_ascii=False), r["id"]))
        changed = True
    if changed:
        conn.commit()
    conn.close()

def save_asset(pid, kind, name, data, file_path=None):
    conn = db()
    if kind in ("dialogue", "scenes", "descriptions", "keysounds", "source"):
        conn.execute("DELETE FROM assets WHERE project_id=? AND kind=?", (pid, kind))
    cur = conn.execute(
        "INSERT INTO assets(project_id,kind,name,data_json,file_path,created) VALUES(?,?,?,?,?,?)",
        (pid, kind, name, json.dumps(data, ensure_ascii=False), file_path, time.time()))
    conn.commit()
    aid = cur.lastrowid
    conn.close()
    return aid

# ---------------------------------------------------------------- 演示素材

def gen_demo_wavs(pdir):
    """合成一套可立即试听的演示素材: 环境声+对白提示音+关键声, 以及 4 段旁白。"""
    fr = 16000
    dur = 48.0
    n = int(fr * dur)
    random.seed(7)
    src = array("f", [0.0]) * n
    # 环境底噪(可让位)
    for i in range(n):
        src[i] = random.uniform(-1, 1) * 900
    def tone(t0, t1, freq, amp):
        for i in range(max(0, int(t0 * fr)), min(n, int(t1 * fr))):
            t = i / fr
            src[i] += math.sin(2 * math.pi * freq * t) * amp
    # 对白(蜂鸣代替人声)
    for (a, b, f) in [(4, 7.5, 440), (9, 12, 520), (16, 19, 470), (22, 26, 540),
                      (34, 38, 500), (40, 44, 460)]:
        tone(a, b, f, 9000)
    # 关键声: 门铃 20s(双音)、脚步 30-33s
    tone(20.0, 20.4, 880, 12000)
    tone(20.5, 20.9, 660, 12000)
    for k in range(6):
        t0 = 30.0 + k * 0.5
        for i in range(int(t0 * fr), min(n, int((t0 + 0.12) * fr))):
            src[i] += random.uniform(-1, 1) * 7000
    src16 = array("h", (max(-32768, min(32767, int(v))) for v in src))
    with open(os.path.join(pdir, "source.wav"), "wb") as f:
        f.write(write_wav(src16, 1, fr))

    # 4 段「不同录音棚分批补录」的旁白: 电平/底噪各异, 分别带 头静音 / 爆音 / 接带噪声
    # (amp 为主体幅度, 直接体现片段峰值接近、RMS 忽大忽小)
    narrs = []
    specs = [
        # 时长, 主体幅度, 前静音, 爆音(时刻,幅度), 接带噪声(尾段)
        (2.0, 11500, 0.00, None, 0.0),          # 录音棚A 正常
        (2.8, 4200,  0.35, None, 0.0),          # 录音棚B 偏小 + 头静音
        (1.6, 10500, 0.05, (0.72, 20000), 0.0), # 录音棚C 爆音
        (3.4, 6800,  0.00, None, 0.28),         # 录音棚D 接带噪声(尾段)
    ]
    for idx, (d, amp, head, pop, tailn) in enumerate(specs, 1):
        m = int(fr * d)
        hs = int(fr * head)
        clip = array("h")
        for i in range(m):
            t = i / fr
            v = 0.0
            if i >= hs:
                v = math.sin(2 * math.pi * (220 + 20 * math.sin(2 * math.pi * 5 * t)) * t) * amp
                v += random.uniform(-1, 1) * (40 + amp * 0.006)  # 各棚底噪不同
            if pop is not None and abs(t - pop[0]) < 0.004:
                v += pop[1]                       # 爆音尖峰
            if tailn and t > d - tailn:          # 接带噪声: 尾部高频沙沙
                v += random.uniform(-1, 1) * amp * (0.9 + 0.3 * math.sin(2 * math.pi * 30 * t))
            clip.append(max(-32768, min(32767, int(v))))
        path = os.path.join(pdir, "narr_demo_%d.wav" % idx)
        with open(path, "wb") as f:
            f.write(write_wav(clip, 1, fr))
        narrs.append(("旁白片段%d.wav" % idx, path, d))

    # 片段5/6: 同一句话的两次补录(多版本拼接素材)。
    # 片段5 前半咬字清楚、后半喷麦(低频爆震); 片段6 前半弱且含糊、后半干净。
    d5 = 2.4
    m = int(fr * d5)
    clip5 = array("h")
    clip6 = array("h")
    for i in range(m):
        t = i / fr
        v = math.sin(2 * math.pi * (230 + 15 * math.sin(2 * math.pi * 4 * t)) * t) * 11000
        v += random.uniform(-1, 1) * 60
        if t > 1.2:   # 喷麦: 低频爆震
            v += math.sin(2 * math.pi * 65 * t) * 15000 * (0.6 + 0.4 * math.sin(2 * math.pi * 2.5 * t))
        clip5.append(max(-32768, min(32767, int(v))))
        if t < 1.2:   # 前半弱且含糊(低频、小音量)
            w = math.sin(2 * math.pi * 170 * t) * 2600 + random.uniform(-1, 1) * 50
        else:         # 后半干净
            w = math.sin(2 * math.pi * (230 + 15 * math.sin(2 * math.pi * 4 * t)) * t) * 11000
            w += random.uniform(-1, 1) * 60
        clip6.append(max(-32768, min(32767, int(w))))
    for idx, clip in ((5, clip5), (6, clip6)):
        path = os.path.join(pdir, "narr_demo_%d.wav" % idx)
        with open(path, "wb") as f:
            f.write(write_wav(clip, 1, fr))
        narrs.append(("旁白片段%d.wav" % idx, path, d5))
    return narrs

def create_demo():
    conn = db()
    cur = conn.execute("INSERT INTO projects(name,created) VALUES(?,?)",
                       ("演示项目 · 雨夜门铃", time.time()))
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    pdir = proj_dir(pid)
    narrs = gen_demo_wavs(pdir)

    with open(os.path.join(pdir, "source.wav"), "rb") as f:
        samples, nch, fr = read_wav(f.read())
    save_asset(pid, "source", "正片.wav", {
        "duration": 48.0, "framerate": fr, "channels": nch,
        "peaks": make_peaks(samples, nch, fr)}, os.path.join(pdir, "source.wav"))
    narr_ids = []
    for name, path, d in narrs:
        with open(path, "rb") as f:
            s2, c2, f2 = read_wav(f.read())
        aid = save_asset(pid, "narration", name, {
            "duration": round(d, 3), "framerate": f2, "channels": c2,
            "levels": level_curves(s2, c2, f2)}, path)
        narr_ids.append(aid)

    save_asset(pid, "dialogue", "对白稿", {"items": [
        {"id": "d1", "start": 4, "end": 7.5, "speaker": "林", "text": "外面还在下雨吗？"},
        {"id": "d2", "start": 9, "end": 12, "speaker": "周", "text": "小了。你听见没有，楼下好像有人。"},
        {"id": "d3", "start": 16, "end": 19, "speaker": "林", "text": "别开门，先看看猫眼。"},
        {"id": "d4", "start": 22, "end": 26, "speaker": "周", "text": "没人。可是伞架上多了一把黑伞。"},
        {"id": "d5", "start": 34, "end": 38, "speaker": "林", "text": "那是我的。我下午回来过。"},
        {"id": "d6", "start": 40, "end": 44, "speaker": "周", "text": "你下午不是一直在公司？"},
    ]})
    save_asset(pid, "scenes", "场景切点", {"items": [
        {"id": "s1", "time": 0, "label": "场1 客厅·夜"},
        {"id": "s2", "time": 15, "label": "场2 门厅·夜"},
        {"id": "s3", "time": 28, "label": "场3 楼梯间"},
        {"id": "s4", "time": 40, "label": "场4 客厅·深夜"},
    ]})
    save_asset(pid, "descriptions", "描述稿", {"items": [
        {"id": "ad1", "start": 0.6, "text": "雨夜，老式公寓客厅，台灯昏黄。"},
        {"id": "ad2", "start": 12.6, "text": "周放下茶杯，走到门边，手停在门把上。"},
        {"id": "ad3", "start": 19.4, "text": "门铃响了两声。"},
        {"id": "ad4", "start": 27.0, "text": "楼道里脚步声由远及近，又停在门外。"},
        {"id": "ad5", "start": 45.0, "text": "她撑开黑伞，走进雨里。"},
    ]})
    save_asset(pid, "keysounds", "关键声", {"items": [
        {"id": "k1", "start": 19.9, "end": 21.0, "label": "门铃", "maskable": False},
        {"id": "k2", "start": 30.0, "end": 33.0, "label": "脚步声", "maskable": False},
        {"id": "k3", "start": 44.5, "end": 48.0, "label": "雨声渐强", "maskable": True},
    ]})

    # 预置绑定(描述卡 -> 旁白片段), 响度配平方案初始全部待处理
    conn = db()
    conn.execute("INSERT INTO placements(project_id,data_json) VALUES(?,?)",
                 (pid, json.dumps({"placements": {
                     "ad1": {"narration_id": narr_ids[0], "start": 0.6, "duck": False,
                             "gain": 1.0, "abridged": False},
                     "ad2": {"narration_id": narr_ids[1], "start": 12.6, "duck": False,
                             "gain": 1.0, "abridged": False},
                     "ad3": {"narration_id": narr_ids[2], "start": 19.4, "duck": False,
                             "gain": 1.0, "abridged": False},
                     "ad4": {"narration_id": narr_ids[3], "start": 27.0, "duck": False,
                             "gain": 1.0, "abridged": False},
                     "ad5": {"narration_id": narr_ids[4], "start": 45.0, "duck": False,
                             "gain": 1.0, "abridged": False}},
                     "settings": {}}, ensure_ascii=False)))
    conn.execute("INSERT INTO leveling(project_id,data_json) VALUES(?,?)",
                 (pid, json.dumps({"settings": default_level_settings(), "items": {}},
                                  ensure_ascii=False)))
    # 预置拼接方案(ad5, 待处理): 片段5 取前半(咬字清楚) + 片段6 取后半(避开喷麦),
    # 锚点对齐同一语义点, 0.03s 交叉淡化; 确认后合成片段进入配平/混音/导出。
    conn.execute("INSERT INTO splice(project_id,data_json) VALUES(?,?)",
                 (pid, json.dumps({"settings": dict(SPLICE_DEFAULTS), "items": {
                     "ad5": {"takes": [narr_ids[4], narr_ids[5]],
                             "anchors": {str(narr_ids[4]): 0.4, str(narr_ids[5]): 1.6},
                             "segments": [
                                 {"take_id": narr_ids[4], "in": 0.0, "out": 1.25,
                                  "xfade": 0.0, "gap": 0.0},
                                 {"take_id": narr_ids[5], "in": 1.2, "out": 2.4,
                                  "xfade": 0.03, "gap": 0.0}],
                             "status": "pending"}}}, ensure_ascii=False)))
    conn.commit()
    conn.close()
    return pid

# ---------------------------------------------------------------- HTTP

def json_resp(start_response, obj, status="200 OK", headers=None):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    hs = [("Content-Type", "application/json; charset=utf-8"),
          ("Content-Length", str(len(body)))]
    if headers:
        hs += headers
    start_response(status, hs)
    return [body]

def err(start_response, msg, status="400 Bad Request"):
    return json_resp(start_response, {"error": msg}, status)

def read_body(environ):
    n = int(environ.get("CONTENT_LENGTH") or 0)
    return environ["wsgi.input"].read(n) if n else b""

def load_narr_clips(pid):
    """读取项目全部旁白片段 {nid: (samples, nch, fr)}。"""
    conn = db()
    rows = conn.execute("SELECT id,file_path FROM assets WHERE project_id=? AND kind='narration'",
                        (pid,)).fetchall()
    conn.close()
    narr_clips = {}
    for r in rows:
        with open(r["file_path"], "rb") as f:
            narr_clips[str(r["id"])] = read_wav(f.read())
    return narr_clips

def load_engine_audio(pid):
    """读取工程正片与全部旁白片段 (src, nch, fr, {nid:(samples,nch,fr)})。"""
    narr_clips = load_narr_clips(pid)
    with open(os.path.join(proj_dir(pid), "source.wav"), "rb") as f:
        src, nch, fr = read_wav(f.read())
    return src, nch, fr, narr_clips

def resolved_duckenv(st, nch, fr, narr_clips, src_dur):
    """
    对当前方案做实时校审并按纵深防御归一化(供混音/导出统一调用)。
    返回 (safe_plans, report); 拼接注入后的状态 st 与混音口径一致。
    """
    plans = st.get("duckenv") or {"settings": dict(DUCKENV_DEFAULTS), "items": {}}
    gsettings = st["placements"].get("settings", {})
    desc_texts = {d["id"]: d.get("text", "") for d in st.get("descriptions") or []}
    durs = {}
    for did, p in (st["placements"].get("placements", {}) or {}).items():
        clip = narr_clips.get(str(p.get("narration_id")))
        if clip:
            _cs, dur = prepare_narr(clip, nch, fr, p, len(desc_texts.get(did, "")), gsettings)
            durs[did] = dur
    report = duckenv_report(st, plans, src_dur, durs, gsettings)
    return sanitize_duckenv(plans, report), report

def app(environ, start_response):
    try:
        return route(environ, start_response)
    except Exception as e:  # 统一错误出口
        import traceback; traceback.print_exc()
        return err(start_response, "%s: %s" % (type(e).__name__, e), "500 Internal Server Error")

def route(environ, start_response):
    method = environ["REQUEST_METHOD"]
    path = urlparse(environ["PATH_INFO"]).path
    qs = parse_qs(environ.get("QUERY_STRING", ""))

    # ---- 静态
    if method == "GET" and (path == "/" or path.startswith("/static/")):
        fp = os.path.join(STATIC, "index.html") if path == "/" else os.path.join(STATIC, path[len("/static/"):])
        fp = os.path.normpath(fp)
        if not fp.startswith(STATIC) or not os.path.isfile(fp):
            return err(start_response, "not found", "404 Not Found")
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8"}.get(os.path.splitext(fp)[1], "application/octet-stream")
        with open(fp, "rb") as f:
            body = f.read()
        start_response("200 OK", [("Content-Type", ctype), ("Content-Length", str(len(body)))])
        return [body]

    # ---- 项目
    if path == "/api/projects" and method == "GET":
        conn = db()
        rows = conn.execute("SELECT * FROM projects ORDER BY id DESC").fetchall()
        conn.close()
        return json_resp(start_response, [dict(r) for r in rows])

    if path == "/api/project" and method == "POST":
        body = json.loads(read_body(environ) or b"{}")
        name = (body.get("name") or "未命名项目").strip()
        conn = db()
        cur = conn.execute("INSERT INTO projects(name,created) VALUES(?,?)", (name, time.time()))
        conn.commit()
        pid = cur.lastrowid
        conn.close()
        proj_dir(pid)
        return json_resp(start_response, {"id": pid, "name": name})

    if path == "/api/demo" and method == "POST":
        pid = create_demo()
        return json_resp(start_response, {"id": pid})

    parts = [p for p in path.split("/") if p]
    # /api/project/<pid>/...
    if len(parts) >= 3 and parts[0] == "api" and parts[1] == "project":
        pid = int(parts[2])
        sub = parts[3] if len(parts) > 3 else ""

        if sub == "state" and method == "GET":
            st = get_state(pid)
            return json_resp(start_response, st) if st else err(start_response, "项目不存在", "404 Not Found")

        if sub == "source" and method == "POST":
            raw = read_body(environ)
            samples, nch, fr = read_wav(raw)
            fp = os.path.join(proj_dir(pid), "source.wav")
            with open(fp, "wb") as f:
                f.write(write_wav(samples, nch, fr))
            dur = (len(samples) // nch) / fr
            save_asset(pid, "source", qs.get("name", ["正片.wav"])[0], {
                "duration": round(dur, 3), "framerate": fr, "channels": nch,
                "peaks": make_peaks(samples, nch, fr)}, fp)
            return json_resp(start_response, {"ok": True, "duration": dur})

        if sub == "narration" and method == "POST":
            raw = read_body(environ)
            samples, nch, fr = read_wav(raw)
            name = qs.get("name", ["旁白.wav"])[0]
            safe = "narr_%d_%s" % (int(time.time() * 1000), os.path.basename(name).replace("/", "_"))
            fp = os.path.join(proj_dir(pid), safe)
            with open(fp, "wb") as f:
                f.write(write_wav(samples, nch, fr))
            dur = (len(samples) // nch) / fr
            aid = save_asset(pid, "narration", name, {
                "duration": round(dur, 3), "framerate": fr, "channels": nch,
                "levels": level_curves(samples, nch, fr)}, fp)
            return json_resp(start_response, {"ok": True, "id": aid, "duration": dur})

        if sub == "levelcurves" and method == "POST":
            ensure_level_curves(pid)
            return json_resp(start_response, {"ok": True})

        if sub == "script" and method == "POST":
            body = json.loads(read_body(environ) or b"{}")
            kind = body.get("kind")
            if kind not in ("dialogue", "scenes", "descriptions", "keysounds"):
                return err(start_response, "kind 须为 dialogue/scenes/descriptions/keysounds")
            items = norm_items(kind, body.get("items", []))
            save_asset(pid, kind, body.get("name") or kind, {"items": items})
            return json_resp(start_response, {"ok": True, "count": len(items)})

        if sub == "placements" and method == "POST":
            body = read_body(environ)
            json.loads(body or b"{}")  # 校验
            conn = db()
            conn.execute("INSERT INTO placements(project_id,data_json) VALUES(?,?) "
                         "ON CONFLICT(project_id) DO UPDATE SET data_json=excluded.data_json",
                         (pid, body.decode("utf-8")))
            conn.commit()
            conn.close()
            return json_resp(start_response, {"ok": True})

        if sub == "mix" and method == "POST":
            st = get_state(pid)
            if not st or not st["source"]:
                return err(start_response, "请先载入正片 WAV")
            src, nch, fr, narr_clips = load_engine_audio(pid)
            # 确认的拼接方案合成注入(阻塞/待处理不注入, 回退原整段绑定)
            sp_rep = splice_report(st, st.get("splice") or {}, nch, fr,
                                   (len(src) // nch) / fr, narr_clips)
            ov, injected = apply_splice_inject(st, narr_clips, nch, fr, sp_rep)
            st = spliced_state(st, ov, injected)
            pdata = st["placements"]
            plist = [dict(v, desc_id=k) for k, v in pdata.get("placements", {}).items()
                     if v.get("narration_id")]
            desc_texts = {d["id"]: d.get("text", "") for d in st["descriptions"]}
            # 确认版增益出口前再校验: 阻塞片段强制 pending, 其增益不进混音 WAV
            stored_lv = st.get("leveling") or {"settings": default_level_settings(), "items": {}}
            lv_report = leveling_report(st, stored_lv, src, nch, fr, narr_clips)
            eff_lv = sanitize_leveling_plan(stored_lv, lv_report)
            # 确认版原声让位包络出口前再校验: 阻塞卡强制 pending, 不进混音 WAV
            eff_de, de_report = resolved_duckenv(
                st, nch, fr, narr_clips, (len(src) // nch) / fr)
            mixed, clipinfo, _peak = render_mix(
                src, nch, fr, plist, narr_clips, pdata.get("settings", {}),
                desc_texts, eff_lv, eff_de)
            wav_bytes = write_wav(mixed, nch, fr)
            with open(os.path.join(proj_dir(pid), "mix.wav"), "wb") as f:
                f.write(wav_bytes)
            return json_resp(start_response, {"ok": True, "duration": (len(mixed) // nch) / fr,
                                              "clip": clipinfo,
                                              "confirmedLeveled": [did for did, it in eff_lv["items"].items()
                                                                   if it.get("status") == "confirmed"],
                                              "confirmedSpliced": sorted(injected.keys()),
                                              "confirmedDuckenv": [did for did, it in eff_de["items"].items()
                                                                   if it.get("status") == "confirmed"],
                                              "url": "/api/project/%d/file?which=mix" % pid})

        if sub == "leveling" and method == "POST":
            # 保存方案 + 计算各片段电平/建议增益/混音峰值/跳变, 返回报告。
            # 阻塞片段一律强制 pending: 人工理由不改变阻塞状态, 其增益永不进确认版。
            body = json.loads(read_body(environ) or b"{}")
            plan = {"settings": body.get("settings") or default_level_settings(),
                    "items": body.get("items") or {}}
            st = get_state(pid)
            if not st or not st["source"]:
                return err(start_response, "请先载入正片 WAV")
            src, nch, fr, narr_clips = load_engine_audio(pid)
            # 确认的拼接方案以合成片段参与配平
            sp_rep = splice_report(st, st.get("splice") or {}, nch, fr,
                                   (len(src) // nch) / fr, narr_clips)
            ov, injected = apply_splice_inject(st, narr_clips, nch, fr, sp_rep)
            st = spliced_state(st, ov, injected)
            report = leveling_report(st, plan, src, nch, fr, narr_clips)
            safe_plan = sanitize_leveling_plan(plan, report)
            save_leveling(pid, safe_plan)
            return json_resp(start_response, report)

        if sub == "levelconfirm" and method == "POST":
            # 确认单段或全部。硬规则: 有效语音不足/通道不一致/增益越界/混音削波时,
            # 无论是否填写人工理由, 片段都保持 pending, 增益不进确认版混音/脚本/复演。
            # 人工理由可以写入修订与方案备注, 但不改变阻塞状态。
            body = json.loads(read_body(environ) or b"{}")
            target_did = body.get("desc_id")
            keep_reason = (body.get("accepted_reason") or "").strip()
            st = get_state(pid)
            if not st or not st["source"]:
                return err(start_response, "请先载入正片 WAV")
            plan = st.get("leveling") or {"settings": default_level_settings(), "items": {}}
            src, nch, fr, narr_clips = load_engine_audio(pid)
            # 确认的拼接方案以合成片段参与配平校审
            sp_rep = splice_report(st, st.get("splice") or {}, nch, fr,
                                   (len(src) // nch) / fr, narr_clips)
            ov, injected = apply_splice_inject(st, narr_clips, nch, fr, sp_rep)
            st = spliced_state(st, ov, injected)
            report = leveling_report(st, plan, src, nch, fr, narr_clips)
            desc_by_id = {d["id"]: d for d in st["descriptions"]}
            to_confirm = [target_did] if target_did else report["order"]
            confirmed, blocked, unbound = [], [], []

            def add_revision(did, r, gain, reason, blocked_flag):
                ranges_txt = ", ".join("%s–%s" % (fmt_tc(q["start"]), fmt_tc(q["end"]))
                                       for q in r["ranges"])
                snap = {"kind": "leveling_pending" if blocked_flag else "leveling",
                        "desc_id": did, "ranges": r["ranges"], "rangeText": ranges_txt,
                        "rmsDb": r["rmsDb"], "gain": gain,
                        "gainDb": _gain_db(gain) if gain is not None else None,
                        "mixPeakDb": r["mixPeakDb"],
                        "blocked": blocked_flag,
                        "blockedErrors": [e["code"] for e in r["errors"] if e["sev"] == "bad"],
                        "accepted_reason": reason, "settings": plan["settings"]}
                if blocked_flag:
                    summary = "响度配平保持待处理 %s [%s] 片段 %s" % (did, fmt_tc(r["start"]), r["name"])
                else:
                    summary = "响度配平确认 %s [%s] 片段 %s" % (did, fmt_tc(r["start"]), r["name"])
                conn2 = db()
                conn2.execute("INSERT INTO revisions(project_id,created,summary,rationale,snapshot_json) "
                              "VALUES(?,?,?,?,?)",
                              (pid, time.time(), summary, reason,
                               json.dumps(snap, ensure_ascii=False)))
                conn2.commit(); conn2.close()

            for did in to_confirm:
                r = report["items"].get(did)
                if not r:
                    unbound.append({"desc_id": did, "reason": "未绑定旁白"})
                    continue
                bad = [e for e in r["errors"] if e["sev"] == "bad"]
                item = plan["items"].get(did) or {}
                item["ranges"] = r["ranges"]
                gain = r["gain"] if r["gain"] is not None else r["suggestGain"]
                if bad:
                    # 阻塞: 始终 pending; 有理由则把草稿增益与理由留痕, 但不确认;
                    # 无理由则清掉历史残留理由, 避免旧留痕被误当作本次豁免。
                    item["status"] = "pending"
                    if keep_reason:
                        item["accepted_reason"] = keep_reason
                        add_revision(did, r, gain, keep_reason, True)
                    else:
                        item.pop("accepted_reason", None)
                    plan["items"][did] = item
                    blocked.append({"desc_id": did, "reason": "存在阻塞错误,片段保持待处理",
                                    "errors": [e["msg"] for e in bad],
                                    "codes": [e["code"] for e in bad],
                                    "reasonLogged": bool(keep_reason)})
                    continue
                # 无阻塞: 确认; 理由仅作人工备注, 不影响状态
                item["gain"] = gain
                item["status"] = "confirmed"
                if keep_reason:
                    item["accepted_reason"] = keep_reason
                elif "accepted_reason" in item:
                    del item["accepted_reason"]
                plan["items"][did] = item
                confirmed.append(did)
                add_revision(did, r, gain, keep_reason or
                             ("配平至目标 %.1f dBFS,混音峰值 %.1f dBFS" % (
                                 plan["settings"]["targetDb"],
                                 r["mixPeakDb"] if r["mixPeakDb"] is not None else 0.0)), False)

            if confirmed:
                safe_plan = sanitize_leveling_plan(plan, report)
                save_leveling(pid, safe_plan)
            # 即使没有 confirmed(全部被阻塞), 也回存 pending 状态(可能清掉了残留理由)
            elif blocked:
                save_leveling(pid, sanitize_leveling_plan(plan, report))
            st2 = get_state(pid)
            report2 = leveling_report(st2, st2.get("leveling") or plan,
                                       src, nch, fr, narr_clips)
            return json_resp(start_response, {"ok": bool(confirmed),
                                              "confirmed": confirmed,
                                              "blocked": blocked,
                                              "unbound": unbound,
                                              "skipped": blocked + unbound,
                                              "report": report2,
                                              "revisions": st2["revisions"]})

        if sub == "splice" and method == "POST":
            # 保存拼接方案(挂接录音/锚点/拼接段)并返回校审报告。
            # 已确认方案的内容(takes/anchors/segments)一旦被改, 自动回到待处理,
            # 该卡旧的配平选区/增益同时失效; 阻塞方案一律强制 pending。
            body = json.loads(read_body(environ) or b"{}")
            st = get_state(pid)
            if not st or not st["source"]:
                return err(start_response, "请先载入正片 WAV")
            plans = {"settings": body.get("settings") or dict(SPLICE_DEFAULTS),
                     "items": body.get("items") or {}}
            old_items = (st.get("splice") or {}).get("items", {})
            lv = st.get("leveling") or {"settings": default_level_settings(), "items": {}}
            de = st.get("duckenv") or {"settings": dict(DUCKENV_DEFAULTS), "items": {}}
            lv_changed = de_changed = False
            for did, plan in plans["items"].items():
                old = old_items.get(did)
                if old and old.get("status") == "confirmed" and \
                        splice_content(old) != splice_content(plan):
                    plan["status"] = "pending"
                    if did in (lv.get("items") or {}):
                        del lv["items"][did]
                        lv_changed = True
                    # 合成片段时长口径变化: 该卡已确认让位包络区间可能失配, 强制重确认
                    de_it = (de.get("items") or {}).get(did)
                    if de_it and de_it.get("status") == "confirmed":
                        de_it["status"] = "pending"
                        de_changed = True
            if lv_changed:
                save_leveling(pid, lv)
            if de_changed:
                save_duckenv(pid, de)
            clips = load_narr_clips(pid)
            smeta = st["source"]
            report = splice_report(st, plans, smeta["channels"], smeta["framerate"],
                                   smeta["duration"], clips)
            safe = sanitize_splice(plans, report)
            save_splice(pid, safe)
            st["splice"] = safe
            _ov, injected = apply_splice_inject(st, clips, smeta["channels"],
                                                smeta["framerate"], report)
            return json_resp(start_response, {"report": report, "plans": safe,
                                              "resolved": injected})

        if sub == "spliceconfirm" and method == "POST":
            # 确认单个拼接方案。硬规则: 锚点倒序/来源缺失/接缝削波/压住对白关键声/
            # 格式不一致/重叠越界等阻塞存在时, 无论是否填写人工理由都保持 pending,
            # 合成结果不进配平/混音/脚本/复演; 理由只写入修订留痕。
            body = json.loads(read_body(environ) or b"{}")
            did = body.get("desc_id")
            reason = (body.get("accepted_reason") or "").strip()
            st = get_state(pid)
            if not st or not st["source"]:
                return err(start_response, "请先载入正片 WAV")
            plans = st.get("splice") or {"settings": dict(SPLICE_DEFAULTS), "items": {}}
            plan = plans.get("items", {}).get(did)
            if not plan:
                return err(start_response, "该描述卡没有拼接方案")
            clips = load_narr_clips(pid)
            smeta = st["source"]
            report = splice_report(st, plans, smeta["channels"], smeta["framerate"],
                                   smeta["duration"], clips)
            r = report["items"].get(did)
            bad = [e for e in r["errors"] if e["sev"] == "bad"] if r else []

            def add_splice_revision(blocked_flag):
                src_names = "+".join(s["name"] for s in r["sources"]) if r else ""
                cuts = "; ".join("%s[%s–%s]⤿%.2fs" % (
                    s["name"], fmt_tc(s["in"]), fmt_tc(s["out"]), s["xfade"])
                    for s in (r["segments"] if r else []))
                snap = {"kind": "splice_pending" if blocked_flag else "splice",
                        "desc_id": did,
                        "segments": r["segments"] if r else [],
                        "anchors": plan.get("anchors") or {},
                        "duration": r["duration"] if r else 0.0,
                        "sources": r["sources"] if r else [],
                        "seams": r["seams"] if r else [],
                        "blocked": blocked_flag,
                        "blockedErrors": [e["code"] for e in bad] if blocked_flag else [],
                        "accepted_reason": reason, "settings": plans["settings"]}
                if blocked_flag:
                    summary = "旁白拼接保持待处理 %s [%s] %s" % (
                        did, fmt_tc(r["start"] if r else 0), src_names)
                    rationale = reason
                else:
                    summary = "旁白拼接确认 %s [%s] %d段·合成%.3fs(%s)" % (
                        did, fmt_tc(r["start"]), len(r["segments"]), r["duration"], src_names)
                    rationale = reason or "两次补录各取一段,切点: %s" % cuts
                conn2 = db()
                conn2.execute("INSERT INTO revisions(project_id,created,summary,rationale,snapshot_json) "
                              "VALUES(?,?,?,?,?)",
                              (pid, time.time(), summary, rationale,
                               json.dumps(snap, ensure_ascii=False)))
                conn2.commit(); conn2.close()

            if bad or r is None:
                plan["status"] = "pending"
                if reason:
                    plan["accepted_reason"] = reason
                    add_splice_revision(True)
                else:
                    plan.pop("accepted_reason", None)
                plans["items"][did] = plan
                save_splice(pid, sanitize_splice(plans, report))
                st2 = get_state(pid)
                return json_resp(start_response, {
                    "ok": False, "confirmed": [],
                    "blocked": [{"desc_id": did,
                                 "errors": [e["msg"] for e in bad],
                                 "codes": [e["code"] for e in bad],
                                 "reasonLogged": bool(reason)}],
                    "report": report, "resolved": st2["spliceResolved"],
                    "revisions": st2["revisions"]})
            # 无阻塞: 确认; 合成片段替代原整段绑定, 该卡旧配平选区/增益失效
            plan["status"] = "confirmed"
            if reason:
                plan["accepted_reason"] = reason
            else:
                plan.pop("accepted_reason", None)
            plans["items"][did] = plan
            add_splice_revision(False)
            lv = st.get("leveling") or {"items": {}}
            if did in (lv.get("items") or {}):
                del lv["items"][did]
                save_leveling(pid, lv)
            # 合成片段替代整段绑定: 旧让位包络区间按新时长重确认
            de = st.get("duckenv") or {"settings": dict(DUCKENV_DEFAULTS), "items": {}}
            de_it = (de.get("items") or {}).get(did)
            if de_it and de_it.get("status") == "confirmed":
                de_it["status"] = "pending"
                save_duckenv(pid, de)
            save_splice(pid, sanitize_splice(plans, report))
            st2 = get_state(pid)
            return json_resp(start_response, {"ok": True, "confirmed": [did], "blocked": [],
                                              "report": report,
                                              "resolved": st2["spliceResolved"],
                                              "revisions": st2["revisions"]})

        if sub == "duckenv" and method == "POST":
            # 保存包络方案(关键点/人工保护区)并返回校审报告。阻塞一律强制 pending;
            # 已确认方案的关键点/保护区被改时后端同样强制回到待处理。
            body = json.loads(read_body(environ) or b"{}")
            st = get_state(pid)
            if not st or not st["source"]:
                return err(start_response, "请先载入正片 WAV")
            # 与 /splice 同口径: 客户端提交全量 items 覆盖; 合并缺失卡可避免
            # 旧客户端只传单卡时丢掉其他卡的草稿/确认(前端始终发送全量)。
            old_items = (st.get("duckenv") or {}).get("items") or {}
            merged_items = dict(old_items)
            merged_items.update(body.get("items") or {})
            plans = {"settings": body.get("settings") or dict(DUCKENV_DEFAULTS),
                     "items": merged_items}
            src, nch, fr, narr_clips = load_engine_audio(pid)
            # 确认的拼接方案以合成片段参与区间计算
            sp_rep = splice_report(st, st.get("splice") or {}, nch, fr,
                                   (len(src) // nch) / fr, narr_clips)
            ov, injected = apply_splice_inject(st, narr_clips, nch, fr, sp_rep)
            st = spliced_state(st, ov, injected)
            src_dur = (len(src) // nch) / fr
            gsettings = st["placements"].get("settings", {})
            desc_texts = {d["id"]: d.get("text", "") for d in st["descriptions"]}
            durs = {}
            for did, p in st["placements"].get("placements", {}).items():
                clip = narr_clips.get(str(p.get("narration_id")))
                if clip:
                    _cs, d = prepare_narr(clip, nch, fr, p, len(desc_texts.get(did, "")), gsettings)
                    durs[did] = d
            report = duckenv_report(st, plans, src_dur, durs, gsettings)
            safe = sanitize_duckenv(plans, report)
            save_duckenv(pid, safe)
            return json_resp(start_response, {"report": report, "plans": safe})

        if sub == "duckenvconfirm" and method == "POST":
            # 确认单个包络。硬规则: 关键点倒序/重合、增益越界、斜率过陡、结束后未恢复、
            # 保护区误压、相邻叠加过深任一存在时, 无论是否填写人工理由都保持 pending,
            # 关键点不进确认版混音/脚本/复演; 理由只写入修订留痕。
            body = json.loads(read_body(environ) or b"{}")
            did = body.get("desc_id")
            reason = (body.get("accepted_reason") or "").strip()
            st = get_state(pid)
            if not st or not st["source"]:
                return err(start_response, "请先载入正片 WAV")
            if not did:
                return err(start_response, "请指定要确认的描述卡")
            src, nch, fr, narr_clips = load_engine_audio(pid)
            sp_rep = splice_report(st, st.get("splice") or {}, nch, fr,
                                   (len(src) // nch) / fr, narr_clips)
            ov, injected = apply_splice_inject(st, narr_clips, nch, fr, sp_rep)
            st = spliced_state(st, ov, injected)
            src_dur = (len(src) // nch) / fr
            plans = st.get("duckenv") or {"settings": dict(DUCKENV_DEFAULTS), "items": {}}
            if did not in (plans.get("items") or {}):
                return err(start_response, "该描述卡没有让位包络")
            safe, report = resolved_duckenv(st, nch, fr, narr_clips, src_dur)
            r = report["items"].get(did)
            bad = [e for e in r["errors"] if e["sev"] == "bad"] if r else []

            def add_de_revision(blocked_flag):
                pts_txt = "; ".join("%s@%.2f" % (fmt_tc(q["t"]), q["g"])
                                    for q in (r["points"] if r else []))
                snap = {"kind": "duckenv_pending" if blocked_flag else "duckenv",
                        "desc_id": did, "points": r["points"] if r else [],
                        "protected": (safe["items"].get(did) or {}).get("protected", []),
                        "span": r["span"] if r else [0, 0], "holdDb": r["holdDb"] if r else EPS_DB,
                        "blocked": blocked_flag,
                        "blockedErrors": [e["code"] for e in bad] if blocked_flag else [],
                        "accepted_reason": reason, "settings": plans.get("settings", {})}
                if blocked_flag:
                    summary = "原声让位保持待处理 %s [%s–%s]" % (
                        did, fmt_tc((r["span"] or [0])[0]), fmt_tc((r["span"] or [0])[1]))
                    rationale = reason
                else:
                    summary = "原声让位确认 %s [%s–%s] 保持 %.1f dB" % (
                        did, fmt_tc(r["span"][0]), fmt_tc(r["span"][1]), r["holdDb"])
                    rationale = reason or "渐入/保持/恢复关键点: %s" % pts_txt
                conn2 = db()
                conn2.execute("INSERT INTO revisions(project_id,created,summary,rationale,snapshot_json) "
                              "VALUES(?,?,?,?,?)",
                              (pid, time.time(), summary, rationale,
                               json.dumps(snap, ensure_ascii=False)))
                conn2.commit(); conn2.close()

            if bad or r is None:
                safe["items"][did]["status"] = "pending"
                if reason:
                    safe["items"][did]["accepted_reason"] = reason
                    add_de_revision(True)
                else:
                    safe["items"][did].pop("accepted_reason", None)
                save_duckenv(pid, safe)
                st2 = get_state(pid)
                return json_resp(start_response, {
                    "ok": False, "confirmed": [],
                    "blocked": [{"desc_id": did,
                                 "errors": [e["msg"] for e in bad],
                                 "codes": [e["code"] for e in bad],
                                 "reasonLogged": bool(reason)}],
                    "report": report, "plans": safe,
                    "revisions": st2["revisions"]})
            # 无阻塞: 确认
            item = safe["items"][did]
            item["status"] = "confirmed"
            if reason:
                item["accepted_reason"] = reason
            else:
                item.pop("accepted_reason", None)
            save_duckenv(pid, safe)
            add_de_revision(False)
            st2 = get_state(pid)
            return json_resp(start_response, {"ok": True, "confirmed": [did], "blocked": [],
                                              "report": report, "plans": safe,
                                              "revisions": st2["revisions"]})

        if sub == "file" and method == "GET":
            which = qs.get("which", ["source"])[0]
            if which in ("source", "mix"):
                fp = os.path.join(proj_dir(pid), which + ".wav")
            elif which.startswith("narr_"):
                conn = db()
                row = conn.execute("SELECT file_path FROM assets WHERE id=? AND project_id=?",
                                   (int(which[5:]), pid)).fetchone()
                conn.close()
                fp = row["file_path"] if row else ""
            else:
                fp = ""
            if not fp or not os.path.isfile(fp):
                return err(start_response, "文件不存在", "404 Not Found")
            with open(fp, "rb") as f:
                body = f.read()
            start_response("200 OK", [("Content-Type", "audio/wav"),
                                      ("Content-Length", str(len(body)))])
            return [body]

        if sub == "revision" and method == "POST":
            body = json.loads(read_body(environ) or b"{}")
            summary = (body.get("summary") or "").strip()
            rationale = (body.get("rationale") or "").strip()
            if not summary or not rationale:
                return err(start_response, "修订须填写素材摘要/时码与采用理由")
            conn = db()
            conn.execute("INSERT INTO revisions(project_id,created,summary,rationale,snapshot_json) "
                         "VALUES(?,?,?,?,?)",
                         (pid, time.time(), summary, rationale,
                          json.dumps(body.get("snapshot", {}), ensure_ascii=False)))
            conn.commit()
            conn.close()
            return json_resp(start_response, {"ok": True})

        if sub == "revisions" and method == "GET":
            conn = db()
            rows = conn.execute("SELECT * FROM revisions WHERE project_id=? ORDER BY id DESC",
                                (pid,)).fetchall()
            conn.close()
            return json_resp(start_response, [dict(r) for r in rows])

        if sub == "export" and method == "GET":
            what = qs.get("what", [""])[0]
            st = get_state(pid)
            if not st:
                return err(start_response, "项目不存在", "404 Not Found")
            # 导出前统一实时校审: 阻塞片段不得在脚本/复演里以 confirmed 与增益出现;
            # 拼接方案同样: 阻塞/待处理不注入, 确认版合成片段进入配平/脚本/复演。
            stored_lv = st.get("leveling") or {"settings": default_level_settings(), "items": {}}
            sp_stored = st.get("splice") or {"settings": dict(SPLICE_DEFAULTS), "items": {}}
            de_stored = st.get("duckenv") or {"settings": dict(DUCKENV_DEFAULTS), "items": {}}
            sp_eff = sp_stored
            sp_blocked = {}
            de_eff = de_stored
            de_blocked = {}
            lv_report = None
            if st.get("source"):
                try:
                    src2, nc2, fr2, clips2 = load_engine_audio(pid)
                    sp_rep = splice_report(st, sp_stored, nc2, fr2,
                                           st["source"]["duration"], clips2)
                    sp_eff = sanitize_splice(sp_stored, sp_rep)
                    ov, injected = apply_splice_inject(st, clips2, nc2, fr2, sp_rep)
                    st = spliced_state(st, ov, injected)
                    sp_blocked = {did: [e["code"] for e in r["errors"] if e["sev"] == "bad"]
                                  for did, r in sp_rep["items"].items()
                                  if any(e["sev"] == "bad" for e in r["errors"])}
                    # 让位包络同样: 阻塞/待处理不进确认版, 只有确认包络进入脚本/复演
                    de_eff, de_rep = resolved_duckenv(st, nc2, fr2, clips2,
                                                      st["source"]["duration"])
                    de_blocked = {did: [e["code"] for e in r["errors"] if e["sev"] == "bad"]
                                  for did, r in de_rep["items"].items()
                                  if any(e["sev"] == "bad" for e in r["errors"])}
                    lv_report = leveling_report(st, stored_lv, src2, nc2, fr2, clips2)
                except FileNotFoundError:
                    lv_report = None
                    sp_eff = sanitize_splice(sp_stored, {"items": {}})
                    de_eff = sanitize_duckenv(de_stored, {"items": {}})
                    st["splice"] = sp_eff
            else:
                sp_eff = sanitize_splice(sp_stored, {"items": {}})
                de_eff = sanitize_duckenv(de_stored, {"items": {}})
            st["splice"] = sp_eff
            st["spliceBlocked"] = sp_blocked
            st["duckenv"] = de_eff
            st["duckenvBlocked"] = de_blocked
            if lv_report is not None:
                eff_lv = sanitize_leveling_plan(stored_lv, lv_report)
                blocked_map = {did: [e["code"] for e in r["errors"] if e["sev"] == "bad"]
                               for did, r in lv_report["items"].items()
                               if any(e["sev"] == "bad" for e in r["errors"])}
                report_ids = set(lv_report["items"].keys())
            else:
                # 正片/音频缺失无法校审: 全部降级 pending, 阻塞码置空(按不可确认处理)
                eff_lv = sanitize_leveling_plan(stored_lv, {"items": {}})
                blocked_map = {}
                report_ids = set()
            eff_lv["blocked"] = blocked_map
            if what == "script":
                text = build_script(dict(st, leveling=eff_lv))
                body = text.encode("utf-8")
                start_response("200 OK", [
                    ("Content-Type", "text/plain; charset=utf-8"),
                    ("Content-Disposition", "attachment; filename=ad_script.txt"),
                    ("Content-Length", str(len(body)))])
                return [body]
            if what == "replay":
                # 复演 JSON 是可再导入的确认版参数: 阻塞片段(status=pending 且有阻塞码,
                # 或未绑定)只保留状态/选区/人工理由留痕, 剥除 gain, 使复演无法应用
                # 本应排除的增益草稿。
                replay_items = {}
                for did, it in eff_lv["items"].items():
                    entry = {k: v for k, v in it.items() if k != "blocked"}
                    unbound = did not in report_ids
                    if entry.get("status") != "confirmed" and (did in blocked_map or unbound):
                        entry.pop("gain", None)
                    replay_items[did] = entry
                replay_lv = {"settings": eff_lv["settings"], "items": replay_items}
                # 让位包络: 阻塞/待处理卡剥除关键点(复演文件无法应用本应排除的包络),
                # 只保留状态/人工保护区/理由留痕; 健康确认卡完整携带 points 驱动复演混音。
                replay_de_items = {}
                for did2, it in (de_eff.get("items") or {}).items():
                    entry = {k: v for k, v in it.items()}
                    if entry.get("status") != "confirmed" and (did2 in de_blocked):
                        entry.pop("points", None)
                    replay_de_items[did2] = entry
                replay_de = {"settings": de_eff.get("settings", dict(DUCKENV_DEFAULTS)),
                             "items": replay_de_items}
                replay = {"project": st["project"], "source": st["source"],
                          "narrations": st["narrations"], "dialogue": st["dialogue"],
                          "scenes": st["scenes"], "descriptions": st["descriptions"],
                          "keysounds": st["keysounds"], "placements": st["placements"],
                          "leveling": replay_lv,
                          "splice": sp_eff,
                          "duckenv": replay_de,
                          "exported_at": time.time()}
                body = json.dumps(replay, ensure_ascii=False, indent=2).encode("utf-8")
                start_response("200 OK", [
                    ("Content-Type", "application/json; charset=utf-8"),
                    ("Content-Disposition", "attachment; filename=ad_replay.json"),
                    ("Content-Length", str(len(body)))])
                return [body]
            if what == "mix":
                fp = os.path.join(proj_dir(pid), "mix.wav")
                if not os.path.isfile(fp):
                    return err(start_response, "请先在页面中渲染混音", "404 Not Found")
                with open(fp, "rb") as f:
                    body = f.read()
                start_response("200 OK", [
                    ("Content-Type", "audio/wav"),
                    ("Content-Disposition", "attachment; filename=ad_mix.wav"),
                    ("Content-Length", str(len(body)))])
                return [body]
            return err(start_response, "what 须为 script/replay/mix")

    return err(start_response, "not found", "404 Not Found")

def build_script(st):
    """导出描述脚本(纯文本, 含时码/旁白绑定/压低标记/修订记录)。"""
    L = []
    L.append("# 音频描述脚本 · %s" % st["project"]["name"])
    L.append("# 导出时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    src = st.get("source") or {}
    L.append("# 正片: %s  时长 %.3fs  %dHz %dch" % (
        src.get("name", "-"), src.get("duration", 0), src.get("framerate", 0), src.get("channels", 0)))
    L.append("")
    narr_by_id = {str(n["id"]): n for n in st["narrations"]}
    desc_by_id = {d["id"]: d for d in st["descriptions"]}
    settings = st["placements"].get("settings", {})
    max_rate = float(settings.get("maxRate", 5.5)) or 5.5
    leveling = st.get("leveling") or {"items": {}}
    lv_items = leveling.get("items", {})
    lv_settings = default_level_settings()
    lv_settings.update(leveling.get("settings") or {})
    de_items = (st.get("duckenv") or {}).get("items", {})
    de_blocked = st.get("duckenvBlocked") or {}
    L.append("")
    L.append("# 响度配平目标: %.1f dBFS · 峰值上限 %.1f dBFS · 相邻跳变阈值 %.1f dB" % (
        lv_settings["targetDb"], lv_settings["ceilingDb"], lv_settings["maxJumpDb"]))
    L.append("")

    def eff_duration(d, p, narr):
        """与前端 cardDur 一致: 绑定片段按缩写压缩, 未绑定按语速估算。"""
        text_len = len(d.get("text", ""))
        if narr:
            dur = narr["duration"]
            if p.get("abridged"):
                target = max(0.8, text_len / max_rate)
                return min(dur, target), dur
            return dur, None
        rate = max_rate if p.get("abridged") else max_rate * 0.8
        return max(0.8, text_len / rate), None

    rows = []
    for did, p in st["placements"].get("placements", {}).items():
        d = desc_by_id.get(did)
        if not d:
            continue
        narr = narr_by_id.get(str(p.get("narration_id")))
        dur, orig = eff_duration(d, p, narr)
        rows.append((float(p.get("start", 0)), d, p, narr, dur, orig))
    rows.sort()
    for start, d, p, narr, dur, orig in rows:
        end = start + dur
        flags = []
        if p.get("abridged"):
            flags.append("缩写稿" + ("(原 %.3fs)" % orig if orig else ""))
        if p.get("duck"):
            flags.append("局部压低原声")
        if p.get("accepted"):
            flags.append("保留冲突(已记录理由)")
        L.append("[%s -> %s] %s" % (fmt_tc(start), fmt_tc(end), d.get("text", "")))
        L.append("    旁白素材: %s  时长 %.3fs%s" % (
            narr["name"] if narr else "(未绑定, 按语速估算)",
            dur,
            "  标记: " + "、".join(flags) if flags else ""))
        lv = lv_items.get(d["id"])
        if lv:
            if lv.get("status") == "confirmed":
                rng = ", ".join("%s–%s" % (fmt_tc(q["start"]), fmt_tc(q["end"]))
                                for q in lv.get("ranges", []))
                L.append("    响度配平: 已确认 增益 %+.2f dB(×%.3f)  有效区间: %s%s" % (
                    _gain_db(float(lv.get("gain", 1.0))), float(lv.get("gain", 1.0)), rng,
                    "  人工保留: " + lv["accepted_reason"] if lv.get("accepted_reason") else ""))
            else:
                codes = (leveling.get("blocked") or {}).get(d["id"])
                note = "待处理(未确认,增益不参与混音/导出)"
                if codes:
                    note += " 阻塞: " + "、".join(codes)
                if lv.get("accepted_reason"):
                    note += " 人工留痕(不改状态): " + lv["accepted_reason"]
                L.append("    响度配平: " + note)
        # 原声让位包络: 确认版列出关键点/保持深度/保护区; 待处理/阻塞只标注, 不进确认版
        de = de_items.get(d["id"])
        if de and de.get("points"):
            if de.get("status") == "confirmed":
                pts = " → ".join("%s@%.2f" % (fmt_tc(q["t"]), q["g"]) for q in de["points"])
                span_txt = "[%s–%s]" % (fmt_tc(de["span"][0]), fmt_tc(de["span"][1])) \
                    if de.get("span") else ""
                prot = "  人工保护区: " + "、".join(str(x) for x in de.get("protected") or []) \
                    if de.get("protected") else ""
                why = "  人工理由: " + de["accepted_reason"] if de.get("accepted_reason") else ""
                L.append("    原声让位: 已确认 %s 关键点: %s%s%s" % (span_txt, pts, prot, why))
            else:
                note = "待处理(未确认,包络不参与混音/复演)"
                if de_blocked.get(d["id"]):
                    note += " 阻塞: " + "、".join(de_blocked[d["id"]])
                if de.get("accepted_reason"):
                    note += " 人工留痕(不改状态): " + de["accepted_reason"]
                L.append("    原声让位: " + note)
        # 旁白拼接: 确认版给出来源与切点; 待处理/阻塞方案只标注, 合成不进确认版
        if narr and narr.get("splice"):
            spi = narr["splice"]
            cuts = " + ".join("%s[%s–%s]%s" % (
                s["name"], fmt_tc(s["in"]), fmt_tc(s["out"]),
                ("⤿%.2fs" % s["xfade"]) if s.get("xfade") else "")
                for s in spi.get("segments", []))
            L.append("    旁白拼接: 已确认 %d段合成 %.3fs = %s" % (
                len(spi.get("segments", [])), narr["duration"], cuts))
        sp_items = (st.get("splice") or {}).get("items", {})
        sp_blocked = st.get("spliceBlocked") or {}
        plan2 = sp_items.get(d["id"])
        if plan2 and not (narr and narr.get("splice")):
            note = "待处理(未确认,合成不参与混音/配平/导出)"
            if sp_blocked.get(d["id"]):
                note += " 阻塞: " + "、".join(sp_blocked[d["id"]])
            if plan2.get("accepted_reason"):
                note += " 人工留痕(不改状态): " + plan2["accepted_reason"]
            L.append("    旁白拼接: " + note)
    if st.get("revisions"):
        L.append("")
        L.append("# 修订记录")
        for r in reversed(st["revisions"]):
            L.append("- [%s] %s —— 理由: %s" % (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["created"])),
                r["summary"], r["rationale"]))
    return "\n".join(L) + "\n"

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "8000"))
    print("音频描述校审工具: http://127.0.0.1:%d" % port)
    make_server("0.0.0.0", port, app).serve_forever()
