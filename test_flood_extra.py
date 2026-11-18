# -*- coding: utf-8 -*-
"""anti_flood 新功能测试（v1.2.0）：静默时段、拦截排行报表、每日趋势统计。"""

import asyncio
import os
import sys
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _PLUGIN_DIR)
sys.path.insert(0, os.path.dirname(_PLUGIN_DIR))

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
        "ignore_admin": False,
        "flood_action": "silence",
        "bot_filter_mode": "trigger_only",
        "auto_detect_bot": False,
        "bot_ids": "",
        "bot_name_patterns": "",
        "whitelist_ids": "",
        "persist_stats": False,
        "skip_flood_when_at": True,
    }
    cfg.update(overrides)
    return AntiFloodPlugin(context=None, config=cfg)


class FakeEvent:
    def __init__(self, group_id="123", sender_id="u1", sender_name="小明",
                 message_str="", at_or_wake=False):
        self._group_id = group_id
        self._sender_id = sender_id
        self._sender_name = sender_name
        self.unified_msg_origin = f"onebot:GroupMessage:{group_id}"
        self.is_at_or_wake_command = at_or_wake
        self.message_obj = type("M", (), {"raw_message": None})()
        self.message_str = message_str
        self.stopped = False

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def is_admin(self):
        return False

    def stop_event(self):
        self.stopped = True


def run(coro):
    return asyncio.run(coro)


class TestQuietHours(unittest.TestCase):
    def test_disabled_by_default(self):
        p = make_plugin()
        self.assertIsNone(p._quiet_hours())
        self.assertFalse(p._in_quiet_hours())

    def test_parse_valid(self):
        p = make_plugin(flood_quiet_hours="23:00-07:00")
        self.assertEqual(p._quiet_hours(), ("23:00", "07:00"))

    def test_invalid_formats(self):
        for bad in ("abc", "24:00-25:00", "12:00", "08:00-08:00"):
            p = make_plugin(flood_quiet_hours=bad)
            self.assertIsNone(p._quiet_hours(), bad)

    def test_normal_range(self):
        p = make_plugin(flood_quiet_hours="01:00-06:00")
        self.assertTrue(p._in_quiet_hours(datetime(2026, 1, 1, 3, 0)))
        self.assertFalse(p._in_quiet_hours(datetime(2026, 1, 1, 8, 0)))

    def test_cross_midnight(self):
        p = make_plugin(flood_quiet_hours="23:00-07:00")
        self.assertTrue(p._in_quiet_hours(datetime(2026, 1, 1, 23, 30)))
        self.assertTrue(p._in_quiet_hours(datetime(2026, 1, 2, 5, 0)))
        self.assertFalse(p._in_quiet_hours(datetime(2026, 1, 2, 12, 0)))


class TestRecordBlock(unittest.TestCase):
    def test_users_and_daily_accumulate(self):
        p = make_plugin()
        ev = FakeEvent(group_id="111", sender_id="a", sender_name="甲")
        p._record_block(ev, "a", "甲", "flood")
        p._record_block(ev, "a", "甲", "repeat")
        ev2 = FakeEvent(group_id="111", sender_id="b", sender_name="乙")
        p._record_block(ev2, "b", "乙", "bot")
        users = p.stats["users"]
        self.assertEqual(users["111|a"]["count"], 2)
        self.assertEqual(users["111|b"]["count"], 1)
        self.assertEqual(users["111|a"]["name"], "甲")
        today_key = max(p.stats["daily"].keys())
        day = p.stats["daily"][today_key]
        self.assertEqual(day["flood"], 1)
        self.assertEqual(day["repeat"], 1)
        self.assertEqual(day["bot"], 1)

    def test_private_group_key(self):
        p = make_plugin()
        ev = FakeEvent(group_id="", sender_id="x")
        p._record_block(ev, "x", "", "flood")
        self.assertIn("private|x", p.stats["users"])

    def test_daily_retention_7_days(self):
        p = make_plugin()
        old = "2020-01-01"
        p.stats["daily"][old] = {"bot": 9, "flood": 9, "repeat": 9}
        ev = FakeEvent(group_id="111", sender_id="a")
        p._record_block(ev, "a", "甲", "flood")
        self.assertNotIn(old, p.stats["daily"])


class TestReportCommand(unittest.TestCase):
    def test_empty_report(self):
        p = make_plugin()
        text = p._report_text(FakeEvent(group_id="123"))
        self.assertIn("暂无拦截记录", text)

    def test_sorted_top_and_king(self):
        p = make_plugin()
        p.stats["users"] = {
            "123|a": {"name": "甲", "count": 4},
            "123|b": {"name": "乙", "count": 8},
            "123|c": {"name": "丙", "count": 1},
            "999|z": {"name": "外群", "count": 50},
        }
        text = p._report_text(FakeEvent(group_id="123"))
        b_pos = text.find("乙")
        a_pos = text.find("甲")
        c_pos = text.find("丙")
        self.assertTrue(b_pos < a_pos < c_pos)
        self.assertNotIn("外群", text)
        self.assertIn("刷屏大户：乙（8 次）", text)

    def test_trend_section(self):
        p = make_plugin()
        today = datetime.now().strftime("%Y-%m-%d")
        yest = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        p.stats["daily"][today] = {"bot": 2, "flood": 3, "repeat": 1}
        p.stats["daily"][yest] = {"bot": 0, "flood": 6, "repeat": 0}
        text = p._report_text(FakeEvent(group_id="123"))
        self.assertIn("近 7 天拦截趋势", text)
        self.assertIn(today[5:], text)
        self.assertIn(yest[5:], text)


class TestQuietInHandler(unittest.TestCase):
    def _run_intercept(self, p, **ev_kw):
        ev = FakeEvent(**ev_kw)
        run(p.intercept(ev))
        return ev

    def test_handler_releases_in_quiet_time(self):
        # 全天静默 → 刷屏不拦截
        p = make_plugin(flood_quiet_hours="00:00-23:59")
        with mock.patch.object(AntiFloodPlugin, "_in_quiet_hours", return_value=True):
            ev = self._run_intercept(
                p, group_id="123", sender_id="spam",
                message_str=f"msg{time.time()}"
            )
        self.assertFalse(ev.stopped)
        self.assertEqual(p.stats["users"], {})

    def test_handler_blocks_outside_quiet(self):
        p = make_plugin(flood_quiet_hours="03:00-04:00", persist_stats=False)
        with mock.patch.object(AntiFloodPlugin, "_in_quiet_hours", return_value=False):
            now = time.time()
            # 快速灌满窗口触发条数限制（limit=5，第 6 条拦截）
            for i in range(6):
                ev = self._run_intercept(
                    p, group_id="123", sender_id="spammer",
                    message_str=f"unique-{now}-{i}"
                )
        self.assertTrue(ev.stopped)
        self.assertGreaterEqual(p.stats["flood_blocked"], 1)
        self.assertIn("123|spammer", p.stats["users"])
        today_key = max(p.stats["daily"].keys())
        self.assertGreaterEqual(p.stats["daily"][today_key]["flood"], 1)


class TestV121Commands(unittest.TestCase):
    @staticmethod
    def _collect(async_gen):
        async def gather():
            return [x async for x in async_gen]
        return asyncio.run(gather())

    def test_help_reset_and_mark_bot_usage(self):
        p = make_plugin(persist_stats=False)
        ev = FakeEvent()
        ev.plain_result = lambda msg: msg
        help_out = self._collect(p.flood_help(ev))
        self.assertIn("/af status", help_out[0])
        self.assertIn("/af reset", help_out[0])
        usage_out = self._collect(p.mark_bot(ev, ""))
        self.assertIn("/af mark_bot", usage_out[0])
        p.stats["flood_blocked"] = 5
        p.stats["users"]["123|u1"] = {"name": "a", "count": 5}
        reset_out = self._collect(p.flood_reset(ev))
        self.assertIn("已清空", reset_out[0])
        self.assertEqual(p.stats["flood_blocked"], 0)
        self.assertEqual(p.stats["users"], {})
        asyncio.run(p.terminate())


if __name__ == "__main__":
    unittest.main(verbosity=1)
