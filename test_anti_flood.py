# -*- coding: utf-8 -*-
"""anti_flood 插件单元测试：配置解析、bot 识别、刷屏检测、过期清理"""
import sys
import time
import unittest
from collections import deque

sys.path.insert(0, r"D:\astrbot\data\plugins\astrbot_plugin_anti_flood")
sys.path.insert(0, r"D:\astrbot\data\plugins")

from main import AntiFloodPlugin  # noqa: E402


def make_plugin(**overrides):
    cfg = {
        "flood_window_seconds": 5,
        "repeat_window_seconds": 10,
        "flood_max_messages": 5,
        "repeat_max_count": 3,
        "enable_flood_check": True,
        "enable_repeat_check": True,
        "flood_per_user": True,
        "ignore_admin": True,
        "flood_action": "silence",
        "bot_filter_mode": "trigger_only",
        "auto_detect_bot": False,
        "bot_ids": "",
        "bot_name_patterns": "",
        "whitelist_ids": "",
        "persist_stats": False,
    }
    cfg.update(overrides)
    return AntiFloodPlugin(context=None, config=cfg)


class FakeEvent:
    """anti_flood 用最小 AstrMessageEvent 替身"""

    def __init__(
        self,
        group_id="",
        sender_id="",
        sender_name="",
        is_admin=False,
        origin="onebot:123:gid:10001",
        at_or_wake=False,
        raw_message=None,
        bot=None,
    ):
        self._group_id = group_id
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._is_admin = is_admin
        self.unified_msg_origin = origin
        self.is_at_or_wake_command = at_or_wake
        self.message_obj = type("M", (), {"raw_message": raw_message})()
        self.bot = bot

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def is_admin(self):
        return self._is_admin


class TestConfigParsing(unittest.TestCase):
    def test_bot_id_set(self):
        p = make_plugin(bot_ids="123, 456, 789")
        self.assertEqual(p._bot_id_set(), {"123", "456", "789"})

    def test_name_patterns(self):
        p = make_plugin(bot_name_patterns=r"^bot\d+,official")
        pats = p._name_patterns()
        self.assertEqual(len(pats), 2)
        self.assertTrue(pats[0].search("bot123"))
        self.assertTrue(pats[1].search("OFFICIAL-1"))

    def test_bad_regex_skipped(self):
        p = make_plugin(bot_name_patterns="[bad,^ok$")
        pats = p._name_patterns()
        self.assertEqual(len(pats), 1)

    def test_group_overrides(self):
        p = make_plugin(group_overrides="10001:ask_llm:all\n# 注释\n10002:silence")
        ov = p._parse_group_overrides()
        self.assertEqual(ov["10001"], {"flood_action": "ask_llm", "bot_filter_mode": "all"})
        self.assertEqual(ov["10002"]["flood_action"], "silence")
        self.assertIsNone(ov["10002"]["bot_filter_mode"])

    def test_effective_action_override(self):
        p = make_plugin(group_overrides="10001:ask_llm")
        self.assertEqual(p._effective_action("10001"), "ask_llm")
        self.assertEqual(p._effective_action("10002"), "silence")

    def test_max_window(self):
        p = make_plugin(flood_window_seconds=3, repeat_window_seconds=8)
        self.assertEqual(p._max_window(), 8.0)


class TestBotFilter(unittest.TestCase):
    def test_in_manual_list_by_id(self):
        p = make_plugin(bot_ids="111")
        self.assertTrue(p._in_manual_list("111", "x"))
        self.assertFalse(p._in_manual_list("222", "x"))

    def test_in_manual_list_by_name_regex(self):
        p = make_plugin(bot_name_patterns="\\bbot")
        self.assertTrue(p._in_manual_list("222", "my-bot-9"))
        self.assertFalse(p._in_manual_list("222", "nobody"))

    def test_in_manual_list_marked(self):
        p = make_plugin()
        p.marked_bots["999"] = {"name": "x", "ts": time.time()}
        self.assertTrue(p._in_manual_list("999", "x"))

    def test_should_ignore_modes(self):
        p = make_plugin(bot_ids="111")
        ev = FakeEvent(sender_id="111", at_or_wake=True)
        self.assertTrue(p._should_ignore_bot_message(ev, "111", "b", "all"))
        self.assertTrue(p._should_ignore_bot_message(ev, "111", "b", "trigger_only"))
        # trigger_only 只看是否 @/唤醒，不看名单
        self.assertTrue(p._should_ignore_bot_message(ev, "222", "b", "trigger_only"))
        ev2 = FakeEvent(sender_id="111", at_or_wake=False)
        self.assertFalse(p._should_ignore_bot_message(ev2, "111", "b", "trigger_only"))
        # manual_only 只看名单
        self.assertTrue(p._should_ignore_bot_message(ev2, "111", "b", "manual_only"))
        self.assertFalse(p._should_ignore_bot_message(ev2, "222", "b", "manual_only"))

    def test_platform_of(self):
        ev = FakeEvent(origin="wechat:wx:1")
        self.assertEqual(AntiFloodPlugin._platform_of(ev), "wechat")
        ev2 = FakeEvent(origin="")
        self.assertEqual(AntiFloodPlugin._platform_of(ev2), "default")

    def test_probe_bot_from_raw_sender(self):
        async def probe():
            ev = FakeEvent(raw_message={"sender": {"is_robot": True}})
            return await make_plugin()._probe_bot(ev, "1")

        import asyncio
        self.assertTrue(asyncio.run(probe()))
        ev_false = FakeEvent(raw_message={"sender": {"is_robot": False}})
        self.assertFalse(asyncio.run(make_plugin()._probe_bot(ev_false, "1")))

    def test_flood_key_user_and_group(self):
        ev = FakeEvent(group_id="10001", sender_id="42")
        p = make_plugin()
        self.assertEqual(p._flood_key(ev, "42"), "onebot:group:10001:user:42")
        p2 = make_plugin(flood_per_user=False)
        self.assertEqual(p2._flood_key(ev, "42"), "onebot:group:10001")
        ev2 = FakeEvent(group_id="", sender_id="42")
        self.assertEqual(p2._flood_key(ev2, "42"), "onebot:private:42")

    def test_flood_key_isolates_groups_for_same_user(self):
        # A1 修复：同一用户在不同群的刷屏记录互不串扰
        ev_a = FakeEvent(group_id="10001", sender_id="42")
        ev_b = FakeEvent(group_id="10002", sender_id="42")
        p = make_plugin()
        self.assertNotEqual(p._flood_key(ev_a, "42"), p._flood_key(ev_b, "42"))
        self.assertNotEqual(p._user_key(ev_a, "42"), p._user_key(ev_b, "42"))

    def test_exempt_admin_and_whitelist(self):
        p = make_plugin(whitelist_ids="888")
        ev_admin = FakeEvent(is_admin=True)
        self.assertTrue(p._is_exempt(ev_admin, "1"))
        ev = FakeEvent()
        self.assertTrue(p._is_exempt(ev, "888"))
        self.assertFalse(p._is_exempt(ev, "999"))


class TestFloodCheck(unittest.TestCase):
    def test_flood_window_limit(self):
        p = make_plugin(flood_max_messages=3)
        ev = FakeEvent(group_id="g1", sender_id="u1")
        now = time.time()
        hits = []
        for i in range(6):
            hits.append(p._check_flood(ev, "u1", f"内容{i}", now + i * 0.5))
        # 第 4 条开始触发条数限制
        self.assertFalse(hits[2][0])
        self.assertTrue(hits[3][0])

    def test_flood_window_expire(self):
        p = make_plugin(flood_max_messages=3)
        ev = FakeEvent(group_id="g1", sender_id="u1")
        now = time.time()
        for i in range(3):
            p._check_flood(ev, "u1", "x", now + i)
        # 窗口 5 秒后旧记录过期，不再触发
        self.assertFalse(p._check_flood(ev, "u1", "x", now + 6)[0])

    def test_repeat_limit(self):
        p = make_plugin(repeat_max_count=3)
        ev = FakeEvent(group_id="g1", sender_id="u1")
        now = time.time()
        for i in range(3):
            self.assertFalse(p._check_flood(ev, "u1", "相同内容", now + i)[1])
        hit, repeat = p._check_flood(ev, "u1", "相同内容", now + 3)
        self.assertTrue(repeat)

    def test_repeat_disabled(self):
        p = make_plugin(enable_repeat_check=False)
        ev = FakeEvent(group_id="g1", sender_id="u1")
        now = time.time()
        for i in range(5):
            hit, repeat = p._check_flood(ev, "u1", "相同内容", now + i)
            self.assertFalse(repeat)

    def test_cleanup_removes_stale(self):
        p = make_plugin(flood_window_seconds=1, repeat_window_seconds=1)
        ev = FakeEvent(group_id="g1", sender_id="u1")
        now = time.time()
        p._check_flood(ev, "u1", "a", now)
        p._check_flood(ev, "u1", "b", now)
        # 篡改记录时间戳使其过期，验证 cleanup 清除
        for dq in p._flood_history.values():
            for i in range(len(dq)):
                dq[i] = now - 10
        for inner in p._repeat_history.values():
            for dq in inner.values():
                for i in range(len(dq)):
                    dq[i] = now - 10
        p._bot_cache["u2"] = (True, now - 10)
        p._cleanup()  # 全部过期
        self.assertEqual(len(p._flood_history), 0)
        self.assertEqual(len(p._repeat_history), 0)
        self.assertEqual(p._bot_cache, {})

    def test_per_user_isolation(self):
        p = make_plugin(flood_max_messages=2)
        ev1 = FakeEvent(group_id="g1", sender_id="u1")
        ev2 = FakeEvent(group_id="g1", sender_id="u2")
        now = time.time()
        p._check_flood(ev1, "u1", "a", now)
        p._check_flood(ev1, "u1", "b", now + 0.1)
        hit, _ = p._check_flood(ev1, "u1", "c", now + 0.2)
        self.assertTrue(hit)
        # 另一用户不受影响
        hit, _ = p._check_flood(ev2, "u2", "d", now + 0.3)
        self.assertFalse(hit)

    def test_dirty_config_falls_back(self):
        # A2 修复：脏配置不影响检测与清理
        p = make_plugin(
            flood_window_seconds="abc",
            flood_max_messages="x",
            repeat_window_seconds=None,
            repeat_max_count="",
            gradient_interval_seconds="bad",
            gradient_hard_threshold=None,
            gradient_block_minutes="",
            ask_llm_window_seconds="zz",
            detect_cache_seconds="nan!",
        )
        self.assertEqual(p._safe_float(p.config.get("flood_window_seconds"), 5), 5.0)
        self.assertEqual(p._safe_int(p.config.get("flood_max_messages", 5), 5), 5)
        self.assertGreaterEqual(p._max_window(), 0)
        ev = FakeEvent(group_id="g1", sender_id="u1")
        hit, _ = p._check_flood(ev, "u1", "a", time.time())
        self.assertFalse(hit)
        p._cleanup()  # 不得抛异常

    def test_out_of_order_timestamps(self):
        # A3 修复：并发乱序时间戳插入后队列仍有序，窗口统计正确
        ev = FakeEvent(group_id="g1", sender_id="u1")
        p = make_plugin(flood_max_messages=2, flood_window_seconds=20)
        now = 1000.0
        # 先到达较晚时间戳，再到达较早时间戳（乱序）
        p._check_flood(ev, "u1", "a", now + 10)
        p._check_flood(ev, "u1", "b", now)
        dq = p._flood_history[p._flood_key(ev, "u1")]
        self.assertEqual(list(dq), sorted(dq))
        # 20 秒窗口内 3 条记录 → 命中
        hit, _ = p._check_flood(ev, "u1", "c", now + 11)
        self.assertTrue(hit)
        # 30 秒后最早记录超出窗口被弹出，队首为窗口内最早
        p._check_flood(ev, "u1", "d", now + 30)
        dq = p._flood_history[p._flood_key(ev, "u1")]
        self.assertEqual(list(dq), sorted(dq))
        self.assertGreaterEqual(dq[0], now + 30 - 20)

    def test_at_prefix_stripped_from_content(self):
        # A6 修复：纯文本 @ 提及前缀不参与重复内容比较
        ev = FakeEvent(group_id="g1", sender_id="u1")
        p = make_plugin(repeat_max_count=1)
        content = p._normalize_content("@bot 晚上好")
        self.assertEqual(content, "晚上好")

    def test_cleanup_never_raises(self):
        # A2 修复：脏数据 + 空历史时 _cleanup 不抛异常
        p = make_plugin()
        p._flood_history["onebot:group:g:user:u"] = deque([time.time() - 9999])
        p._bot_cache["x"] = (True, time.time() - 1)
        p._cleanup()
        self.assertNotIn("onebot:group:g:user:u", p._flood_history)
        self.assertNotIn("x", p._bot_cache)


if __name__ == "__main__":
    unittest.main()
