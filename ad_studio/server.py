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

# ---------------------------------------------------------------- 混音

def render_mix(src_samples, nch, fr, placements, narr_clips, settings):
    """
    placements: [{narration_id, start, duck, gain}]
    narr_clips: {id: (samples, nch, fr)} 原始片段, 此处转换到工程格式
    duck: 在旁白前后 pad 内把原声压到 settings.duck_to, 0.15s 斜坡
    """
    n_frames = len(src_samples) // nch
    duck_to = float(settings.get("duck_to", 0.35))
    pad = float(settings.get("duck_pad", 0.15))
    ramp = 0.15

    # 压低包络(每帧增益)
    env = [1.0] * n_frames
    for p in placements:
        if not p.get("duck"):
            continue
        clip = narr_clips.get(str(p["narration_id"]))
        if not clip:
            continue
        cs, cc, cf = clip
        dur = (len(cs) // cc) / cf
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

    out = array("f", [0.0]) * (n_frames * nch)
    for f in range(n_frames):
        g = env[f]
        base = f * nch
        for c in range(nch):
            out[base + c] = src_samples[base + c] * g

    # 叠旁白
    for p in placements:
        clip = narr_clips.get(str(p["narration_id"]))
        if not clip:
            continue
        cs, cc, cf = clip
        cs = convert(cs, cc, cf, nch, fr)
        gain = float(p.get("gain", 1.0))
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
    for v in out:
        if v > 32767: v = 32767
        elif v < -32768: v = -32768
        mixed.append(int(v))
    return mixed

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
    revs = conn.execute(
        "SELECT id,created,summary,rationale FROM revisions WHERE project_id=? ORDER BY id DESC",
        (pid,)).fetchall()
    conn.close()

    state = {"project": {"id": proj["id"], "name": proj["name"], "created": proj["created"]},
             "source": None, "narrations": [], "dialogue": [], "scenes": [],
             "descriptions": [], "keysounds": [],
             "placements": {"placements": {}, "settings": {}},
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
    return state

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

    narrs = []
    for idx, d in enumerate([2.0, 2.8, 1.6, 3.4], 1):
        m = int(fr * d)
        clip = array("h")
        for i in range(m):
            t = i / fr
            v = math.sin(2 * math.pi * (220 + 20 * math.sin(2 * math.pi * 5 * t)) * t) * 10000
            clip.append(int(v))
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
    for name, path, d in narrs:
        with open(path, "rb") as f:
            s2, c2, f2 = read_wav(f.read())
        save_asset(pid, "narration", name, {
            "duration": round(d, 3), "framerate": f2, "channels": c2}, path)

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
                "duration": round(dur, 3), "framerate": fr, "channels": nch}, fp)
            return json_resp(start_response, {"ok": True, "id": aid, "duration": dur})

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
            pdata = st["placements"]
            plist = [dict(v, desc_id=k) for k, v in pdata.get("placements", {}).items()
                     if v.get("narration_id")]
            mixed = render_mix(src, nch, fr, plist, narr_clips, pdata.get("settings", {}))
            wav_bytes = write_wav(mixed, nch, fr)
            with open(os.path.join(proj_dir(pid), "mix.wav"), "wb") as f:
                f.write(wav_bytes)
            return json_resp(start_response, {"ok": True, "duration": (len(mixed) // nch) / fr,
                                              "url": "/api/project/%d/file?which=mix" % pid})

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
            if what == "script":
                text = build_script(st)
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
    rows = []
    for did, p in st["placements"].get("placements", {}).items():
        d = desc_by_id.get(did)
        if not d:
            continue
        narr = narr_by_id.get(str(p.get("narration_id")))
        dur = narr["duration"] if narr else None
        rows.append((float(p.get("start", 0)), d, p, narr, dur))
    rows.sort()
    for start, d, p, narr, dur in rows:
        end = start + dur if dur else start
        flags = []
        if p.get("duck"):
            flags.append("局部压低原声")
        if p.get("accepted"):
            flags.append("保留冲突(已记录理由)")
        L.append("[%s -> %s] %s" % (fmt_tc(start), fmt_tc(end), d.get("text", "")))
        L.append("    旁白素材: %s%s%s" % (
            narr["name"] if narr else "(未绑定, 按语速估算)",
            "  时长 %.3fs" % dur if dur else "",
            "  标记: " + "、".join(flags) if flags else ""))
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
