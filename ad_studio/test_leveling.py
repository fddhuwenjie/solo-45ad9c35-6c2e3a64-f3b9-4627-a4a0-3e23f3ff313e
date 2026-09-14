#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
响度配平确认流程的阻塞绕过回归测试。

覆盖(需求):
  1. 有效语音不足 / 通道不一致 / 增益越界 / 混音削波 四类阻塞:
     - 不带理由确认 -> 片段保持 pending, 无修订;
     - 带人工理由确认 -> 片段仍保持 pending, 理由写入修订但不改变状态;
  2. 无阻塞片段 -> 正常 confirmed 并入修订;
  3. 确认版输出(混音 WAV / 描述脚本 / 复演 JSON / /mix 接口)
     绝不采用阻塞片段的增益;
  4. 纵深防御: 直接向 /leveling 写入 status=confirmed 的阻塞片段,
     保存/渲染/导出时被强制改回 pending;
  5. /mix 渲染声明的 confirmedLeveled 不含阻塞片段。

仅依赖标准库; 使用独立临时数据目录, 通过 WSGI 直接调 server.app。
"""
import io
import json
import os
import tempfile
import unittest
import wave
from array import array
from urllib.parse import urlparse

import server


def call(method, path, body=None, raw=False, ctype="application/json"):
    """直接调 WSGI app, 返回 (status, headers, body_bytes)。"""
    parsed = urlparse(path)
    payload = b""
    if body is not None:
        payload = body if raw else json.dumps(body, ensure_ascii=False).encode("utf-8")
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": parsed.path,
        "QUERY_STRING": parsed.query,
        "CONTENT_LENGTH": str(len(payload)),
        "CONTENT_TYPE": ctype,
        "wsgi.input": io.BytesIO(payload),
        "wsgi.errors": io.StringIO(),
    }
    out = b"".join(server.app(environ, start_response))
    return captured["status"], captured["headers"], out


def j(method, path, body=None):
    status, _h, out = call(method, path, body)
    return status, json.loads(out.decode("utf-8"))


def pcm_wav(nch=1, fr=16000, samples=None):
    if samples is None:
        samples = array("h", [0] * (fr // 2))
    return server.write_wav(samples, nch, fr)


class LevelingBlockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="adstudio_test_")
        server.DATA = cls.tmp
        server.DB = os.path.join(cls.tmp, "app.db")
        server.init_db()
        cls.pid = server.create_demo()
        cls.base = "/api/project/%d" % cls.pid
        st = server.get_state(cls.pid)
        cls.placements = st["placements"]["placements"]
        cls.nid = {did: p["narration_id"] for did, p in cls.placements.items()}

    def state(self):
        return server.get_state(self.pid)

    def leveling_state(self):
        return self.state()["leveling"]["items"]

    def save(self, items, settings=None):
        body = {"settings": settings or server.default_level_settings(), "items": items}
        status, rep = j("POST", self.base + "/leveling", body)
        self.assertEqual(status, "200 OK")
        return rep

    def confirm(self, did=None, reason=""):
        body = {"accepted_reason": reason}
        if did:
            body["desc_id"] = did
        return j("POST", self.base + "/levelconfirm", body)

    def revision_summaries(self):
        _s, rows = j("GET", self.base + "/revisions")
        return [(r["summary"], r["rationale"]) for r in rows]

    # ------------------------------------------------------------ 1. 有效语音不足
    def test_speech_blocked_no_reason_and_with_reason(self):
        did = "ad2"
        rep = self.save({did: {"ranges": [{"start": 0.0, "end": 0.10}], "gain": 1.5}})
        self.assertIn(did, rep["blocked"])
        self.assertTrue(any(e["code"] == "speech"
                            for e in rep["items"][did]["errors"] if e["sev"] == "bad"))

        # 1a. 无理由确认 -> 跳过, 状态 pending, 无修订
        before = len(self.revision_summaries())
        status, r = self.confirm(did, "")
        self.assertEqual(status, "200 OK")
        self.assertEqual(r["confirmed"], [])
        self.assertEqual([b["desc_id"] for b in r["blocked"]], [did])
        self.assertFalse(r["blocked"][0]["reasonLogged"])
        self.assertEqual(self.leveling_state()[did]["status"], "pending")
        self.assertEqual(len(self.revision_summaries()), before)

        # 1b. 带人工理由确认 -> 仍然 pending, 但理由写修订(只留痕)
        status, r = self.confirm(did, "保留现场动态")
        self.assertEqual(status, "200 OK")
        self.assertEqual(r["confirmed"], [])
        self.assertTrue(r["blocked"][0]["reasonLogged"])
        item = self.leveling_state()[did]
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item.get("accepted_reason"), "保留现场动态")
        revs = self.revision_summaries()
        self.assertTrue(any("保持待处理" in s and did in s and why == "保留现场动态"
                            for s, why in revs))
        # 快照中标记 blocked, 且不得是确认修订
        _s, rows = j("GET", self.base + "/revisions")
        snap = json.loads(rows[0]["snapshot_json"])
        self.assertTrue(snap["blocked"])
        self.assertIn("speech", snap["blockedErrors"])
        self.assertEqual(snap["kind"], "leveling_pending")

    # ------------------------------------------------------------ 2. 增益越界
    def test_gain_range_blocked_with_reason(self):
        did = "ad1"
        rep = self.save({did: {"gain": 10.0}})  # +20dB > +15.6
        self.assertTrue(any(e["code"] == "gainrange" for e in rep["items"][did]["errors"]))
        status, r = self.confirm(did, "导演要求")
        self.assertEqual(r["confirmed"], [])
        self.assertEqual(self.leveling_state()[did]["status"], "pending")
        # 状态恢复为整段选区, 便于后续用例
        self.save({did: {"ranges": [{"start": 0, "end": rep["items"][did]["duration"]}]}})

    # ------------------------------------------------------------ 3. 混音削波
    def test_mixclip_blocked_with_reason(self):
        did = "ad3"  # 片段峰值约 -0.6 dBFS
        rep = self.save({did: {"gain": 2.0}})  # +6dB => 硬削波
        codes = {e["code"] for e in rep["items"][did]["errors"]}
        self.assertIn("mixclip", codes)
        status, r = self.confirm(did, "保留爆音瞬间,导演要求冲击感")
        self.assertEqual(status, "200 OK")
        self.assertEqual(r["confirmed"], [])
        self.assertEqual(self.leveling_state()[did]["status"], "pending")
        self.assertTrue(any("保持待处理" in s and did in s
                            for s, _ in self.revision_summaries()))

    # ------------------------------------------------------------ 4. 通道格式不一致
    def test_channel_mismatch_blocked(self):
        # 直接改库: 给 ad1 绑定的片段换成 2ch/22050Hz 文件
        st = self.state()
        nid = self.nid["ad1"]
        row = None
        conn = server.db()
        row = conn.execute("SELECT file_path FROM assets WHERE id=?", (nid,)).fetchone()
        orig_path, orig_data = row["file_path"], None
        row2 = conn.execute("SELECT data_json FROM assets WHERE id=?", (nid,)).fetchone()
        orig_data = row2["data_json"]
        bad_path = os.path.join(server.proj_dir(self.pid), "badfmt.wav")
        with open(bad_path, "wb") as f:
            f.write(pcm_wav(nch=2, fr=22050))
        conn.execute("UPDATE assets SET file_path=? WHERE id=?", (bad_path, nid))
        conn.commit(); conn.close()
        try:
            rep = self.save({"ad1": {}})
            self.assertTrue(any(e["code"] == "channel"
                                for e in rep["items"]["ad1"]["errors"]))
            status, r = self.confirm("ad1", "素材只能拿到这版")
            self.assertEqual(status, "200 OK")
            self.assertEqual(r["confirmed"], [])
            self.assertEqual(self.leveling_state()["ad1"]["status"], "pending")
        finally:
            # 恢复原片段, 避免污染其他用例
            conn = server.db()
            conn.execute("UPDATE assets SET file_path=?, data_json=? WHERE id=?",
                         (orig_path, orig_data, nid))
            conn.commit(); conn.close()
            os.remove(bad_path)

    # ------------------------------------------------------------ 5. 健康段正常确认
    def test_healthy_confirm(self):
        did = "ad4"
        # 用建议值, 确保无阻塞
        _s, pre = j("POST", self.base + "/leveling",
                    {"settings": server.default_level_settings(), "items": {}})
        self.save({did: {"gain": pre["items"][did]["suggestGain"],
                         "ranges": pre["items"][did]["ranges"]}})
        status, r = self.confirm(did, "")
        self.assertEqual(status, "200 OK")
        self.assertIn(did, r["confirmed"])
        self.assertEqual(self.leveling_state()[did]["status"], "confirmed")
        self.assertTrue(any("响度配平确认" in s and did in s
                            for s, _ in self.revision_summaries()))

    # ------------------------------------------------------------ 6. 确认版输出不受污染
    def test_confirmed_exports_exclude_blocked_gain(self):
        # 构造: ad3 阻塞(+6dB 削波) 且尝试以各种方式标 confirmed; ad1 健康确认
        _s, pre = j("POST", self.base + "/leveling",
                    {"settings": server.default_level_settings(), "items": {}})
        items = {
            "ad1": {"gain": pre["items"]["ad1"]["suggestGain"],
                    "ranges": pre["items"]["ad1"]["ranges"]},
            # 攻击: 客户端直接伪造 status=confirmed 的阻塞片段
            "ad3": {"gain": 2.0, "ranges": [{"start": 0, "end": 1.6}], "status": "confirmed"},
        }
        status, rep = j("POST", self.base + "/leveling",
                        {"settings": server.default_level_settings(), "items": items})
        self.assertIn("ad3", rep["blocked"])
        # /leveling 保存时已强制纠正: DB 中 ad3 不得是 confirmed
        self.assertEqual(self.leveling_state()["ad3"]["status"], "pending")

        # 确认 ad1
        status, r = self.confirm("ad1", "")
        self.assertIn("ad1", r["confirmed"])

        # 渲染混音: 返回 confirmedLeveled, 且不得含 ad3
        status, mix = j("POST", self.base + "/mix")
        self.assertEqual(status, "200 OK")
        self.assertIn("ad1", mix["confirmedLeveled"])
        self.assertNotIn("ad3", mix["confirmedLeveled"])

        # 下载混音 WAV, 用选区削波帧数验证 ad3 没有按 +6dB 生效。
        # +6dB 被采用时整段正弦波频繁削波(~165 帧/段); 不采用时仅爆音尖峰叠门铃 ~15 帧。
        status, _h, wav_bytes = call("GET", self.base + "/file?which=mix")
        s, c, fr = server.read_wav(wav_bytes)
        p = self.placements["ad3"]
        start_f = int(p["start"] * fr)
        n = int(1.6 * fr)
        clip_count = sum(1 for i in range(n) for cc in range(c)
                         if abs(s[(start_f + i) * c + cc]) >= 32767)
        self.assertLessEqual(clip_count, 20,
                             "阻塞片段 +6dB 增益进入了确认版混音 WAV(削波帧 %d)" % clip_count)

        # 复演 JSON: ad3 必须 pending 且不得携带可应用的增益(连 gain 键都不应出现)
        status, _h, out = call("GET", self.base + "/export?what=replay")
        replay = json.loads(out.decode("utf-8"))
        lv = replay["leveling"]["items"]
        self.assertEqual(lv["ad1"]["status"], "confirmed")
        self.assertEqual(lv["ad3"]["status"], "pending")
        self.assertNotIn("gain", lv["ad3"], "阻塞片段增益泄漏进复演 JSON")
        self.assertIn("gain", lv["ad1"])  # 健康确认段增益保留

        # 描述脚本: 阻塞段必须标注待处理与阻塞原因
        status, _h, out = call("GET", self.base + "/export?what=script")
        text = out.decode("utf-8")
        seg = text.split("门铃响了两声")[1].split("[", 1)[0]
        self.assertIn("待处理", seg)
        self.assertIn("mixclip", seg)
        self.assertNotIn("已确认 增益 +6", seg)

    # ------------------------------------------------------------ 7. 全部确认只处理健康段
    def test_confirm_all_skips_blocked(self):
        items = {"ad3": {"gain": 2.0}}  # 削波阻塞
        self.save(items)
        status, r = self.confirm(None, "统一理由也不该放行阻塞")
        self.assertEqual(status, "200 OK")
        self.assertNotIn("ad3", r["confirmed"])
        blocked_ids = [b["desc_id"] for b in r["blocked"]]
        self.assertIn("ad3", blocked_ids)
        self.assertEqual(self.leveling_state()["ad3"]["status"], "pending")


class ReplayLeakTests(unittest.TestCase):
    """四类阻塞在确认版复演 JSON 中都不得携带可应用增益(有/无人工理由两种情况)。"""

    BLOCK = {
        "ad1": {"code": "gainrange", "item": {"gain": 10.0}},            # +20dB 越界
        "ad2": {"code": "speech", "item": {"ranges": [{"start": 0.0, "end": 0.10}],
                                           "gain": 1.5}},                # 有效语音不足
        "ad3": {"code": "mixclip", "item": {"gain": 2.0}},               # +6dB 削波
        "ad4": {"code": "channel", "item": {"gain": 1.2}},               # 通道不一致
    }

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="adstudio_replay_")
        server.DATA = cls.tmp
        server.DB = os.path.join(cls.tmp, "app.db")
        server.init_db()
        cls.pid = server.create_demo()
        cls.base = "/api/project/%d" % cls.pid
        st = server.get_state(cls.pid)
        cls.placements = st["placements"]["placements"]
        # ad4 绑定片段替换为 2ch/22050Hz(通道不一致), 记录原路径以便还原
        cls.nid4 = cls.placements["ad4"]["narration_id"]
        conn = server.db()
        row = conn.execute("SELECT file_path,data_json FROM assets WHERE id=?",
                           (cls.nid4,)).fetchone()
        cls.orig_path, cls.orig_data = row["file_path"], row["data_json"]
        cls.bad_path = os.path.join(server.proj_dir(cls.pid), "badfmt_replay.wav")
        with open(cls.bad_path, "wb") as f:
            f.write(pcm_wav(nch=2, fr=22050))
        conn.execute("UPDATE assets SET file_path=? WHERE id=?", (cls.bad_path, cls.nid4))
        conn.commit(); conn.close()

    @classmethod
    def tearDownClass(cls):
        conn = server.db()
        conn.execute("UPDATE assets SET file_path=?, data_json=? WHERE id=?",
                     (cls.orig_path, cls.orig_data, cls.nid4))
        conn.commit(); conn.close()
        if os.path.exists(cls.bad_path):
            os.remove(cls.bad_path)

    def save(self):
        body = {"settings": server.default_level_settings(),
                "items": {did: spec["item"] for did, spec in self.BLOCK.items()}}
        status, rep = j("POST", self.base + "/leveling", body)
        self.assertEqual(status, "200 OK")
        return rep

    def confirm_all(self, reason):
        return j("POST", self.base + "/levelconfirm",
                 {"accepted_reason": reason})

    def replay(self):
        status, _h, out = call("GET", self.base + "/export?what=replay")
        self.assertEqual(status, "200 OK")
        return json.loads(out.decode("utf-8"))

    def assert_blocked_codes(self, rep):
        for did, spec in self.BLOCK.items():
            self.assertIn(did, rep["blocked"], did)
            codes = next(e["code"] for e in rep["items"][did]["errors"]
                         if e["sev"] == "bad" and e["code"] == spec["code"])
            self.assertEqual(codes, spec["code"])

    def assert_replay_clean(self, replay, reason):
        """四段 pending、无 gain 键, 但保留选区/状态/(理由)留痕。"""
        items = replay["leveling"]["items"]
        for did in self.BLOCK:
            ent = items[did]
            self.assertEqual(ent["status"], "pending", did)
            self.assertNotIn("gain", ent,
                             "%s 阻塞增益泄漏进复演 JSON: %r" % (did, ent.get("gain")))
            if reason:
                self.assertEqual(ent.get("accepted_reason"), reason, did)
            else:
                self.assertNotIn("accepted_reason", ent, did)

    def test_codes_detected(self):
        rep = self.save()
        self.assert_blocked_codes(rep)
        self.assertEqual(set(rep["blocked"]), set(self.BLOCK))

    def test_replay_no_gain_without_reason(self):
        rep = self.save()
        status, r = self.confirm_all("")
        self.assertEqual(status, "200 OK")
        self.assertEqual(r["confirmed"], [])
        self.assertEqual(len(r["blocked"]), 4)
        self.assertFalse(any(b["reasonLogged"] for b in r["blocked"]))
        self.assert_replay_clean(self.replay(), "")

    def test_replay_no_gain_with_reason(self):
        rep = self.save()
        reason = "保留现场动态"
        status, r = self.confirm_all(reason)
        self.assertEqual(status, "200 OK")
        self.assertEqual(r["confirmed"], [])
        self.assertTrue(all(b["reasonLogged"] for b in r["blocked"]))
        # DB 里草稿增益保留(供继续编辑), 但状态 pending
        st = server.get_state(self.pid)
        for did, spec in self.BLOCK.items():
            db_item = st["leveling"]["items"][did]
            self.assertEqual(db_item["status"], "pending")
            if "gain" in spec["item"]:
                self.assertEqual(db_item.get("gain"), spec["item"]["gain"])
        # 复演导出必须剥除增益, 仅留理由痕迹
        self.assert_replay_clean(self.replay(), reason)
        # 理由确实写进修订(留痕), 但摘要表明仍待处理
        _s, rows = j("GET", self.base + "/revisions")
        keep = [x for x in rows if x["rationale"] == reason]
        self.assertEqual(len(keep), 4)
        self.assertTrue(all("保持待处理" in x["summary"] for x in keep))

    def test_replayed_plan_is_not_applicable_as_gain(self):
        """把复演 JSON 的 leveling 直接喂给 render_mix, ad3 区间不得出现 +6dB 削波。"""
        self.save()
        _s, r = self.confirm_all("")
        replay = self.replay()
        st = server.get_state(self.pid)
        src, nch, fr, clips = server.load_engine_audio(self.pid)
        plist = [dict(v, desc_id=k) for k, v in st["placements"]["placements"].items()
                 if v.get("narration_id") and k != "ad4"]  # ad4 通道异常, 不参与渲染
        dt = {d["id"]: d.get("text", "") for d in st["descriptions"]}
        mixed, _info, _ = server.render_mix(
            src, nch, fr, plist, clips, {}, dt, replay["leveling"])
        f0 = int(self.placements["ad3"]["start"] * fr)
        clip_count = sum(1 for i in range(int(1.6 * fr))
                         if abs(mixed[(f0 + i) * nch]) >= 32767)
        self.assertLessEqual(clip_count, 20,
                             "复演 JSON 仍可应用阻塞增益(ad3 削波帧 %d)" % clip_count)


if __name__ == "__main__":
    unittest.main(verbosity=2)
