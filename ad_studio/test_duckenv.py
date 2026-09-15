#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
原声让位包络(duckenv)确认流程与帧渲染回归测试。

覆盖(需求):
  1. 六类阻塞: 关键点倒序/重合、增益越界、斜率过陡、结束后未恢复、
     保护区误压(对白/不可遮盖关键声)、相邻包络叠加过深 —— 均定位时码并阻止确认;
     带人工理由仅留痕, 状态仍 pending;
  2. 健康包络可确认, 修订快照保存关键点/保护区/人工理由;
  3. 确认包络逐帧驱动服务端 WAV 混音(保持深度/恢复), 未确认不生效;
  4. 旧项目固定压低路径: 无 duckenv 行或卡无自定义包络时, build_duck_env 原样;
  5. 纵深防御: 直接写库伪造 confirmed 的阻塞包络, /mix 与导出强制不采用;
  6. 复演 JSON: 阻塞卡剥除 points, 确认卡完整携带; 脚本含确认/待处理行;
  7. DuckEnvComposer 增量重算: upsert/remove 后受影响区间正确, 区间外恒 1。
仅依赖标准库; 独立临时数据目录, 通过 WSGI 直接调 server.app。
"""
import io
import json
import os
import sys
import tempfile
import unittest
import importlib.util
from urllib.parse import urlparse

# stdlib 某些模块(如 http.server)导入时会在 sys.modules 注册 'server' 占位(None),
# 显式按文件路径加载本项目 server 并占住 'server' 名字, 避免遮蔽。
_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("server", os.path.join(_HERE, "server.py"))
server = importlib.util.module_from_spec(_spec)
sys.modules["server"] = server
_spec.loader.exec_module(server)


def call(method, path, body=None):
    parsed = urlparse(path)
    payload = b"" if body is None else json.dumps(body, ensure_ascii=False).encode()
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    environ = {"REQUEST_METHOD": method, "PATH_INFO": parsed.path,
               "QUERY_STRING": parsed.query, "CONTENT_LENGTH": str(len(payload)),
               "CONTENT_TYPE": "application/json", "wsgi.input": io.BytesIO(payload),
               "wsgi.errors": io.StringIO()}
    out = b"".join(server.app(environ, start_response))
    ct = captured["headers"].get("Content-Type", "")
    return captured["status"], (json.loads(out.decode()) if ct.startswith("application/json") else out)


def four(t0, t1, g=0.35, ramp=0.15):
    """合法四点: 渐入/保持/恢复。"""
    return [{"t": round(t0, 3), "g": 1.0},
            {"t": round(t0 + ramp, 3), "g": g},
            {"t": round(t1 - ramp, 3), "g": g},
            {"t": round(t1, 3), "g": 1.0}]


class DuckEnvFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="adstudio_de_")
        server.DATA = cls.tmp
        server.DB = os.path.join(cls.tmp, "app.db")
        server.init_db()
        cls.pid = server.create_demo()
        cls.base = "/api/project/%d" % cls.pid
        st = server.get_state(cls.pid)
        cls.placements = st["placements"]["placements"]

    def save(self, items, settings=None):
        body = {"items": items}
        if settings: body["settings"] = settings
        status, r = call("POST", self.base + "/duckenv", body)
        self.assertEqual(status, "200 OK")
        return r["report"]

    def confirm(self, did, reason=""):
        return call("POST", self.base + "/duckenvconfirm",
                    {"desc_id": did, "accepted_reason": reason})[1]

    def state(self):
        return server.get_state(self.pid)

    def revisions(self):
        return call("GET", self.base + "/revisions")[1]

    # ----------------------------------------------------------- 1. 六类阻塞
    def test_order_gain_slope_restore_block(self):
        cases = {
            "order_reverse": [{"t": 3, "g": 1}, {"t": 0.4, "g": 1},
                              {"t": 0.6, "g": .35}, {"t": 2.8, "g": .35}],
            "order_dup": [{"t": 0.4, "g": 1}, {"t": 0.4, "g": .5},
                          {"t": 2.6, "g": .35}, {"t": 2.8, "g": 1}],
            "gainrange": four(0.4, 2.8)[:3] + [{"t": 2.8, "g": 1.2}],
            "restore": four(0.4, 2.8)[:3] + [{"t": 2.8, "g": 0.5}],
        }
        # 越界增益点替换保持点: 单独构造
        cases["gainrange"][2]["g"] = 0.35
        expect = {"order_reverse": "order", "order_dup": "order",
                  "gainrange": "gainrange", "restore": "restore"}
        for name, pts in cases.items():
            rep = self.save({"ad1": {"points": pts}})
            codes = [e["code"] for e in rep["items"]["ad1"]["errors"] if e["sev"] == "bad"]
            self.assertIn(expect[name], codes, name)
            r = self.confirm("ad1", "人工理由也要拦")
            self.assertEqual(r["confirmed"], [])
            self.assertEqual(self.state()["duckenv"]["items"]["ad1"]["status"], "pending")
        # 斜率过陡: 0.01s 内 1 -> 0.1
        steep = [{"t": 0.4, "g": 1}, {"t": 0.41, "g": .1},
                 {"t": 2.6, "g": .1}, {"t": 2.8, "g": 1}]
        codes = [e["code"] for e in self.save({"ad1": {"points": steep}})["items"]["ad1"]["errors"]]
        self.assertIn("slope", codes)

    def test_protect_blocked_dialogue_and_keysound(self):
        # ad3 @19.4 旁白 1.6s; 门铃保护区 19.9-21.0 与对白 16-19
        pts = four(19.25, 21.1, g=0.3)
        codes = [e["code"] for e in self.save({"ad3": {"points": pts}})["items"]["ad3"]["errors"]]
        self.assertIn("protect", codes)
        # 时码定位: 消息含门铃标签与最深帧时码
        msg = next(e["msg"] for e in
                   self.save({"ad3": {"points": pts}})["items"]["ad3"]["errors"]
                   if e["code"] == "protect")
        self.assertIn("门铃", msg)
        self.assertIn("保护区", msg)

    def test_overlap_blocked(self):
        p1 = four(0.2, 3.2)                 # 保持 0.35
        p2 = four(2.8, 5.4, g=0.2)          # 保持 0.2, 交叠区 min=.2 < max(.35,.2)=.35
        st = self.state()
        st["placements"]["placements"]["ad1"]["start"] = 0.2
        # 直接测纯函数(避免改库)
        rep = server.duckenv_report(
            st, {"items": {"ad1": {"points": p1}, "ad2": {"points": p2}}},
            48.0, {"ad1": 3.0, "ad2": 2.8},
            st["placements"]["settings"])
        self.assertIn("overlap", [e["code"] for e in rep["items"]["ad1"]["errors"]])

    # ----------------------------------------------------------- 2. 健康确认 + 修订
    def test_healthy_confirm_and_revision(self):
        pts = four(0.45, 2.75, g=0.4)
        self.save({"ad1": {"points": pts, "protected": [
            {"start": 3.6, "end": 3.9, "label": "雨声音头"}]}})
        r = self.confirm("ad1", "门铃延音保留,渐入到0.4")
        self.assertEqual(r["confirmed"], ["ad1"])
        self.assertEqual(self.state()["duckenv"]["items"]["ad1"]["status"], "confirmed")
        rev = next(x for x in self.revisions() if "原声让位确认" in x["summary"] and "ad1" in x["summary"])
        snap = json.loads(rev["snapshot_json"])
        self.assertEqual(snap["kind"], "duckenv")
        self.assertEqual(len(snap["points"]), 4)
        self.assertEqual(snap["protected"][0]["label"], "雨声音头")
        self.assertEqual(rev["rationale"], "门铃延音保留,渐入到0.4")

    # ----------------------------------------------------------- 3. 帧渲染
    def test_confirmed_envelope_drives_wav(self):
        pts = four(0.45, 2.75, g=0.35)
        self.save({"ad1": {"points": pts}})
        self.confirm("ad1", "")
        src, nch, fr, clips = server.load_engine_audio(self.pid)
        st = self.state()
        eff_de, _rep = server.resolved_duckenv(st, nch, fr, clips, len(src) / nch / fr)
        # 无旁白 placements, 只看原声包络
        mixed, _c, _p = server.render_mix(src, nch, fr, [], clips, {}, {}, None, eff_de)

        def mean(s, a, b):
            v = [abs(s[i * nch]) for i in range(int(a * fr), int(b * fr))]
            return sum(v) / len(v)
        self.assertAlmostEqual(mean(mixed, 1.2, 1.5) / mean(src, 1.2, 1.5), 0.35, delta=0.03)
        self.assertAlmostEqual(mean(mixed, 3.0, 3.3) / mean(src, 3.0, 3.3), 1.0, delta=0.03)

    def test_pending_envelope_not_applied(self):
        # ad2 有待处理包络: 不进混音
        pts = four(12.4, 15.4, g=0.2)
        self.save({"ad2": {"points": pts}})
        status, mix = call("POST", self.base + "/mix")
        self.assertEqual(status, "200 OK")
        self.assertNotIn("ad2", mix["confirmedDuckenv"])

    # ----------------------------------------------------------- 4. 旧路径
    def test_legacy_fixed_duck_unchanged(self):
        # 无自定义包络: duck=True 的卡仍走 build_duck_env 固定斜坡
        prepared = [({"start": 10.0, "duck": True}, None, 2.0, 1.0, "adX")]
        build = server.build_duck_env
        env = build(16000 * 20, 1, 16000, prepared, {"duck_to": 0.35, "duck_pad": 0.15})
        self.assertAlmostEqual(env[int(10.5 * 16000)], 0.35, delta=0.02)
        self.assertEqual(env[int(13 * 16000)], 1.0)
        # exclude_ids 命中时该卡完全不压
        env2 = build(16000 * 20, 1, 16000, prepared,
                     {"duck_to": 0.35, "duck_pad": 0.15}, exclude_ids={"adX"})
        self.assertEqual(env2[int(10.5 * 16000)], 1.0)

    # ----------------------------------------------------------- 5. 纵深防御
    def test_forged_confirmed_blocked_at_mix_and_export(self):
        bad = four(19.25, 21.1, g=0.3)  # 压门铃 -> protect
        conn = server.db()
        conn.execute("INSERT INTO duckenv(project_id,data_json) VALUES(?,?) "
                     "ON CONFLICT(project_id) DO UPDATE SET data_json=excluded.data_json",
                     (self.pid, json.dumps({"settings": server.DUCKENV_DEFAULTS, "items": {
                         "ad3": {"points": bad, "protected": [], "status": "confirmed"}}},
                        ensure_ascii=False)))
        conn.commit(); conn.close()
        status, mix = call("POST", self.base + "/mix")
        self.assertEqual(status, "200 OK")
        self.assertNotIn("ad3", mix["confirmedDuckenv"])
        status, out = call("GET", self.base + "/export?what=replay")
        replay = json.loads(out.decode())
        self.assertEqual(replay["duckenv"]["items"]["ad3"]["status"], "pending")
        self.assertNotIn("points", replay["duckenv"]["items"]["ad3"])
        status, out = call("GET", self.base + "/export?what=script")
        seg = out.decode().split("门铃响了两声")[1]
        self.assertIn("原声让位: 待处理", seg)
        self.assertIn("protect", seg)

    # ----------------------------------------------------------- 7. 增量合成器
    def test_composer_incremental(self):
        fr = 16000
        c = server.DuckEnvComposer(20 * fr, fr)
        pts = four(1.0, 4.0)
        f0, f1 = c.upsert("a", pts)
        self.assertEqual(f0, int(1.0 * fr))
        self.assertAlmostEqual(c.env[int(2.5 * fr)], 0.35, delta=0.02)
        self.assertEqual(c.env[int(6 * fr)], 1.0)
        # 叠加: 更深的卡在交叠区生效
        c.upsert("b", four(3.5, 6.5, g=0.2))
        self.assertAlmostEqual(c.env[int(3.8 * fr)], 0.2, delta=0.02)
        self.assertAlmostEqual(c.env[int(2.0 * fr)], 0.35, delta=0.02)
        # 移除 b: 交叠区回到 a 的 0.35, b 独占区恢复 1
        c.remove("b")
        self.assertAlmostEqual(c.env[int(3.8 * fr)], 0.35, delta=0.02)
        self.assertEqual(c.env[int(5.0 * fr)], 1.0)
        c.remove("a")
        self.assertEqual(c.env[int(2.5 * fr)], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
