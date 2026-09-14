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

def build_duck_env(n_frames, nch, fr, prepared, settings):
    """按 prepared=[(p, cs, dur, gain)] 生成原声压低包络(每帧增益)。"""
    duck_to = float(settings.get("duck_to", 0.35))
    pad = float(settings.get("duck_pad", 0.15))
    ramp = 0.15
    env = [1.0] * n_frames
    for p, cs, dur, _gain in prepared:
        if not p.get("duck"):
            continue
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
               leveling=None):
    """
    placements: [{narration_id, start, duck, gain, abridged, desc_id}]
    narr_clips: {id: (samples, nch, fr)} 原始片段, 此处转换到工程格式
    desc_texts: {desc_id: text} 用于缩写稿计算目标时长
    leveling: {items:{descId:{status:'confirmed', gain}}} 确认版响度增益
    duck: 在旁白(压缩后)前后 pad 内把原声压到 settings.duck_to, 0.15s 斜坡
    返回 (mixed:array('h'), clip:{frames,peak,first_t}|None, max_lin:float)
    """
    desc_texts = desc_texts or {}
    lv_items = (leveling or {}).get("items", {})
    n_frames = len(src_samples) // nch

    # 预处理各旁白: 格式转换 + 缩写压缩 + 增益(确认版配平优先)
    prepared = []  # (placement, samples, dur, gain)
    for p in placements:
        clip = narr_clips.get(str(p["narration_id"]))
        if not clip:
            continue
        cs, dur = prepare_narr(clip, nch, fr, p,
                               len(desc_texts.get(p.get("desc_id"), "")), settings)
        lv = lv_items.get(p.get("desc_id"))
        gain = float(lv["gain"]) if lv and lv.get("status") == "confirmed" and lv.get("gain") is not None \
            else float(p.get("gain", 1.0))
        prepared.append((p, cs, dur, gain))

    env = build_duck_env(n_frames, nch, fr, prepared, settings)

    out = array("f", [0.0]) * (n_frames * nch)
    for f in range(n_frames):
        g = env[f]
        base = f * nch
        for c in range(nch):
            out[base + c] = src_samples[base + c] * g

    # 叠旁白
    for p, cs, dur, gain in prepared:
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
        plist.append((p, cs, dur, gain0))

    # 原声 + 压低包络(只与 duck 标记有关), 作为各候选混音的底
    base = None
    if src is not None:
        env = build_duck_env(n_frames, src_nch, src_fr, plist, gsettings)
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
        # 非 confirmed 不允许残留确认态; confirmed 仅在无阻塞时保留
        if it.get("status") != "confirmed":
            it["status"] = "pending"
        safe["items"][did] = it
    return safe

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
    revs = conn.execute(
        "SELECT id,created,summary,rationale FROM revisions WHERE project_id=? ORDER BY id DESC",
        (pid,)).fetchall()
    conn.close()

    state = {"project": {"id": proj["id"], "name": proj["name"], "created": proj["created"]},
             "source": None, "narrations": [], "dialogue": [], "scenes": [],
             "descriptions": [], "keysounds": [],
             "placements": {"placements": {}, "settings": {}},
             "leveling": {"settings": default_level_settings(), "items": {}},
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
                             "gain": 1.0, "abridged": False}},
                     "settings": {}}, ensure_ascii=False)))
    conn.execute("INSERT INTO leveling(project_id,data_json) VALUES(?,?)",
                 (pid, json.dumps({"settings": default_level_settings(), "items": {}},
                                  ensure_ascii=False)))
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

def load_engine_audio(pid):
    """读取工程正片与全部旁白片段 (src, nch, fr, {nid:(samples,nch,fr)})。"""
    conn = db()
    rows = conn.execute("SELECT id,file_path FROM assets WHERE project_id=? AND kind='narration'",
                        (pid,)).fetchall()
    conn.close()
    narr_clips = {}
    for r in rows:
        with open(r["file_path"], "rb") as f:
            s, c, fr2 = read_wav(f.read())
        narr_clips[str(r["id"])] = (s, c, fr2)
    with open(os.path.join(proj_dir(pid), "source.wav"), "rb") as f:
        src, nch, fr = read_wav(f.read())
    return src, nch, fr, narr_clips

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
            pdata = st["placements"]
            plist = [dict(v, desc_id=k) for k, v in pdata.get("placements", {}).items()
                     if v.get("narration_id")]
            desc_texts = {d["id"]: d.get("text", "") for d in st["descriptions"]}
            # 确认版增益出口前再校验: 阻塞片段强制 pending, 其增益不进混音 WAV
            stored_lv = st.get("leveling") or {"settings": default_level_settings(), "items": {}}
            lv_report = leveling_report(st, stored_lv, src, nch, fr, narr_clips)
            eff_lv = sanitize_leveling_plan(stored_lv, lv_report)
            mixed, clipinfo, _peak = render_mix(
                src, nch, fr, plist, narr_clips, pdata.get("settings", {}),
                desc_texts, eff_lv)
            wav_bytes = write_wav(mixed, nch, fr)
            with open(os.path.join(proj_dir(pid), "mix.wav"), "wb") as f:
                f.write(wav_bytes)
            return json_resp(start_response, {"ok": True, "duration": (len(mixed) // nch) / fr,
                                              "clip": clipinfo,
                                              "confirmedLeveled": [did for did, it in eff_lv["items"].items()
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
                    # 阻塞: 始终 pending; 有理由则把草稿增益与理由留痕, 但不确认
                    item["status"] = "pending"
                    if keep_reason:
                        item["accepted_reason"] = keep_reason
                        add_revision(did, r, gain, keep_reason, True)
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
            # 即使没有 confirmed(全部被阻塞), 理由也已写修订; plan 中的 pending 备注回存
            elif any(b["reasonLogged"] for b in blocked):
                save_leveling(pid, plan)
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
            # 导出前统一实时校审: 阻塞片段不得在脚本/复演里以 confirmed 与增益出现
            stored_lv = st.get("leveling") or {"settings": default_level_settings(), "items": {}}
            eff_lv = stored_lv
            if st.get("source"):
                try:
                    src2, nc2, fr2, clips2 = load_engine_audio(pid)
                    lv_report = leveling_report(st, stored_lv, src2, nc2, fr2, clips2)
                    eff_lv = sanitize_leveling_plan(stored_lv, lv_report)
                    eff_lv["blocked"] = {did: [e["code"] for e in r["errors"] if e["sev"] == "bad"]
                                         for did, r in lv_report["items"].items()
                                         if any(e["sev"] == "bad" for e in r["errors"])}
                except FileNotFoundError:
                    eff_lv = sanitize_leveling_plan(stored_lv, {"items": {}})
            else:
                eff_lv = sanitize_leveling_plan(stored_lv, {"items": {}})
            if what == "script":
                text = build_script(dict(st, leveling=eff_lv))
                body = text.encode("utf-8")
                start_response("200 OK", [
                    ("Content-Type", "text/plain; charset=utf-8"),
                    ("Content-Disposition", "attachment; filename=ad_script.txt"),
                    ("Content-Length", str(len(body)))])
                return [body]
            if what == "replay":
                replay = {"project": st["project"], "source": st["source"],
                          "narrations": st["narrations"], "dialogue": st["dialogue"],
                          "scenes": st["scenes"], "descriptions": st["descriptions"],
                          "keysounds": st["keysounds"], "placements": st["placements"],
                          "leveling": eff_lv,
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
