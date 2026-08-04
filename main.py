"""防刷屏与 Bot 消息过滤插件。

功能：
1. 选择性忽略其他 bot 的消息（手动名单 + 昵称正则 + 协议端自动探测 + 手动标记）
2. 防刷屏检测（滑动窗口条数限制 + 相同内容重复限制），处置方式：
   - silence（默认）：静默拦截，不进入 LLM 决策链
   - ask_llm：刷屏消息照常进入 LLM，插件在系统提示词中注入指令说明
     （附带当前用户真实 ID），LLM 若决定不回答，可在回复中隐蔽输出
     关键指令 <silent />，插件解析后本地决策：不将该条消息发送到群聊；
     不设置任何屏蔽时间
   - 梯度处置：窗口内反复刷屏自动升级（拦截 → ask_llm → 冷却期硬拦截）
   - 按群覆盖：每个群可独立设置 flood_action / bot_filter_mode
3. 拦截统计持久化，重启不丢失
拦截方式均为静默忽略（stop_event），不会发送警告消息，避免插件自身刷屏。
"""

import json
import os
import re
import time
from collections import defaultdict, deque

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event.filter import EventMessageType, PermissionType
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

# 协议端可能返回的机器人标记字段名（非 OneBot v11 标准，属于扩展字段）
_BOT_FLAG_KEYS = ("is_robot", "is_bot", "robot", "is_qbot")

# bot 过滤模式说明
_MODE_DESC = {
    "all": "全部屏蔽",
    "trigger_only": "仅屏蔽触发本 bot 的消息",
    "manual_only": "仅屏蔽手动名单中的 bot",
}

# 刷屏处置方式说明
_ACTION_DESC = {
    "silence": "静默拦截",
    "ask_llm": "交给 LLM 自主决定",
}


@register("astrbot_plugin_anti_flood", "Administrator", "防刷屏与 Bot 消息过滤器", "1.1.0")
class AntiFloodPlugin(Star):
    """防止自动刷屏，选择性忽略其他 bot 的消息。"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        # 协议端探测结果缓存: {user_id: (是否为 bot, 过期时间戳)}
        self._bot_cache: dict[str, tuple[bool, float]] = {}
        # 管理员手动标记的 bot 名单: {user_id: {"name": str, "ts": float}}
        self.marked_bots: dict[str, dict] = {}
        # 刷屏检测记录: {统计键: 时间戳队列}
        self._flood_history: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=100)
        )
        # 重复内容检测记录: {统计键: {内容: 时间戳队列}}
        self._repeat_history: dict[str, dict[str, deque[float]]] = defaultdict(
            lambda: defaultdict(lambda: deque(maxlen=100))
        )
        # 拦截统计
        self.stats: dict[str, int] = {
            "bot_ignored": 0,
            "flood_blocked": 0,
            "repeat_blocked": 0,
            "llm_judged": 0,
            "llm_held": 0,
        }
        # ask_llm 模式下命中刷屏的用户: {user_id: 提醒过期时间戳}
        self._llm_ask_targets: dict[str, float] = {}
        # 梯度处置记录: {user_id: {"count": int, "first_ts": float, "block_until": float}}
        self._flood_levels: dict[str, dict] = {}
        self._msg_counter = 0
        self._load_marked()
        self._load_stats()

    # ==================== 配置辅助 ====================

    def _bot_id_set(self) -> set[str]:
        """解析手动配置的 bot 名单"""
        raw = str(self.config.get("bot_ids", "") or "")
        return {x.strip() for x in raw.split(",") if x.strip()}

    def _name_patterns(self) -> list[re.Pattern]:
        """编译昵称/ID 正则列表（每次解析以支持热修改）"""
        patterns: list[re.Pattern] = []
        raw = str(self.config.get("bot_name_patterns", "") or "")
        for p in raw.split(","):
            p = p.strip()
            if not p:
                continue
            try:
                patterns.append(re.compile(p, re.IGNORECASE))
            except re.error as e:
                logger.error(f"无效的正则表达式 {p!r}: {e}")
        return patterns

    def _cache_ttl(self) -> float:
        return float(self.config.get("detect_cache_seconds", 3600))

    def _max_window(self) -> float:
        """所有检测时间窗口的最大值，用于清理过期记录"""
        return max(
            float(self.config.get("flood_window_seconds", 5)),
            float(self.config.get("repeat_window_seconds", 10)),
        )

    def _parse_group_overrides(self) -> dict[str, dict]:
        """解析按群覆盖配置，格式每行：群号:flood_action[:bot_filter_mode]。

        每次调用实时解析，支持 WebUI 修改后热更新（无需重载插件）。
        """
        overrides: dict[str, dict] = {}
        raw = str(self.config.get("group_overrides", "") or "")
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(":")]
            if len(parts) < 2 or not parts[0].isdigit():
                logger.warning(f"无效的按群覆盖配置: {line!r}")
                continue
            group_id = parts[0]
            action = parts[1] if parts[1] in ("silence", "ask_llm") else None
            mode = (
                parts[2]
                if len(parts) > 2 and parts[2] in ("all", "trigger_only", "manual_only")
                else None
            )
            overrides[group_id] = {
                "flood_action": action,
                "bot_filter_mode": mode,
            }
        return overrides

    def _effective_action(self, group_id) -> str:
        """按群覆盖后的刷屏处置方式，未覆盖则用全局配置（实时解析，支持热更新）"""
        if group_id:
            override = self._parse_group_overrides().get(group_id)
            if override and override["flood_action"]:
                return override["flood_action"]
        return str(self.config.get("flood_action", "silence"))

    def _effective_mode(self, group_id) -> str:
        """按群覆盖后的 bot 过滤模式，未覆盖则用全局配置（实时解析，支持热更新）"""
        if group_id:
            override = self._parse_group_overrides().get(group_id)
            if override and override["bot_filter_mode"]:
                return override["bot_filter_mode"]
        return str(self.config.get("bot_filter_mode", "trigger_only"))

    def _stats_path(self) -> str:
        base = os.path.join(
            get_astrbot_data_path(), "plugin_data", "astrbot_plugin_anti_flood"
        )
        return os.path.join(base, "stats.json")

    def _load_stats(self):
        """启动时加载持久化统计"""
        try:
            path = self._stats_path()
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for k, v in data.items():
                        if k in self.stats and isinstance(v, int):
                            self.stats[k] = v
        except Exception as e:
            logger.error(f"加载拦截统计失败: {e}")

    def _save_stats(self):
        """持久化拦截统计"""
        if not self.config.get("persist_stats", True):
            return
        try:
            path = self._stats_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.stats, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存拦截统计失败: {e}")

    # ==================== bot 识别 ====================

    def _in_manual_list(self, user_id: str, sender_name: str) -> bool:
        """按手动名单 / 昵称正则 / 学习标记判断是否为 bot"""
        if user_id in self._bot_id_set():
            return True
        if user_id in self.marked_bots:
            return True
        for pattern in self._name_patterns():
            if pattern.search(user_id) or (
                sender_name and pattern.search(sender_name)
            ):
                return True
        return False

    def _should_ignore_bot_message(
        self,
        event: AstrMessageEvent,
        sender_id: str,
        sender_name: str,
        mode: str,
    ) -> bool:
        """根据过滤模式决定是否屏蔽该条 bot 消息。

        all: 屏蔽所有识别为 bot 的消息
        trigger_only: 仅屏蔽会触发本 bot 响应的消息（@ 本 bot / 带唤醒前缀），
            切断 bot 互 @ 刷屏循环，bot 的普通消息放行
        manual_only: 仅屏蔽手动名单（bot_ids / 标记名单）中的 bot，
            自动探测结果不用于屏蔽
        """
        if mode == "all":
            return True
        if mode == "trigger_only":
            return bool(getattr(event, "is_at_or_wake_command", False))
        if mode == "manual_only":
            return self._in_manual_list(sender_id, sender_name)
        return False

    async def _is_bot(
        self, event: AstrMessageEvent, user_id: str, sender_name: str
    ) -> bool:
        """判断发送者是否为 bot：手动判定优先（即时生效），再走协议端探测（带缓存）"""
        if self._in_manual_list(user_id, sender_name):
            return True
        if not self.config.get("auto_detect_bot", True):
            return False
        now = time.time()
        cached = self._bot_cache.get(user_id)
        if cached and cached[1] > now:
            return cached[0]
        try:
            is_bot = await self._probe_bot(event, user_id)
        except Exception as e:
            logger.debug(f"bot 探测失败 user_id={user_id}: {e}")
            is_bot = False
        self._bot_cache[user_id] = (is_bot, now + self._cache_ttl())
        return is_bot

    async def _probe_bot(self, event: AstrMessageEvent, user_id: str) -> bool:
        """探测发送者是否为机器人。

        优先检查消息原始数据 sender 中的扩展字段，其次调用协议端
        get_stranger_info 查询返回的扩展字段。OneBot v11 标准字段中
        无机器人标记，因此该探测依赖 NapCat 等协议端的扩展能力。
        """
        raw = getattr(event.message_obj, "raw_message", None)
        sender = None
        if isinstance(raw, dict):
            sender = raw.get("sender")
        if isinstance(sender, dict):
            for key in _BOT_FLAG_KEYS:
                if key in sender:
                    return bool(sender[key])
        call_action = self._get_call_action(event)
        if call_action is None:
            return False
        try:
            result = await call_action(
                "get_stranger_info", user_id=user_id, no_cache=True
            )
            if isinstance(result, dict):
                for key in _BOT_FLAG_KEYS:
                    if key in result:
                        return bool(result[key])
        except Exception as e:
            logger.debug(f"get_stranger_info 探测失败 user_id={user_id}: {e}")
        return False

    @staticmethod
    def _get_call_action(event: AstrMessageEvent):
        """兼容不同版本 aiocqhttp 客户端，提取 call_action 可调用对象"""
        bot = getattr(event, "bot", None)
        if bot is None:
            return None
        api = getattr(bot, "api", None)
        if api is not None:
            call_action = getattr(api, "call_action", None)
            if callable(call_action):
                return call_action
        call_action = getattr(bot, "call_action", None)
        return call_action if callable(call_action) else None

    # ==================== 刷屏检测 ====================

    @staticmethod
    def _platform_of(event: AstrMessageEvent) -> str:
        """提取事件所属平台（onebot/wechat/telegram 等），防止跨平台 ID 串扰"""
        origin = getattr(event, "unified_msg_origin", "") or ""
        if ":" in origin:
            return origin.split(":", 1)[0] or "default"
        return "default"

    def _user_key(self, event: AstrMessageEvent, sender_id: str) -> str:
        """带平台前缀的用户维度统计键"""
        return f"{self._platform_of(event)}:user:{sender_id}"

    def _flood_key(self, event: AstrMessageEvent, sender_id: str) -> str:
        """刷屏检测的统计维度：按用户或按会话（均带平台前缀）"""
        platform = self._platform_of(event)
        if self.config.get("flood_per_user", True):
            return f"{platform}:user:{sender_id}"
        group_id = event.get_group_id()
        if group_id:
            return f"{platform}:group:{group_id}"
        return f"{platform}:private:{sender_id}"

    def _is_exempt(self, event: AstrMessageEvent, sender_id: str) -> bool:
        """管理员与白名单用户豁免刷屏检测"""
        if self.config.get("ignore_admin", True) and event.is_admin():
            return True
        whitelist = {
            x.strip()
            for x in str(self.config.get("whitelist_ids", "")).split(",")
            if x.strip()
        }
        return sender_id in whitelist

    def _check_flood(
        self, event: AstrMessageEvent, sender_id: str, content: str, now: float
    ) -> tuple[bool, bool]:
        """刷屏检测，返回 (是否触发条数限制, 是否触发重复限制)"""
        flood_hit = False
        repeat_hit = False
        # 1. 滑动窗口条数限制
        if self.config.get("enable_flood_check", True):
            window = float(self.config.get("flood_window_seconds", 5))
            limit = int(self.config.get("flood_max_messages", 5))
            if window > 0 and limit > 0:
                dq = self._flood_history[self._flood_key(event, sender_id)]
                while dq and now - dq[0] > window:
                    dq.popleft()
                dq.append(now)
                flood_hit = len(dq) > limit
        # 2. 相同内容重复限制
        if self.config.get("enable_repeat_check", True):
            window = float(self.config.get("repeat_window_seconds", 10))
            limit = int(self.config.get("repeat_max_count", 3))
            if window > 0 and limit > 0 and content:
                dq = self._repeat_history[self._user_key(event, sender_id)][
                    content
                ]
                while dq and now - dq[0] > window:
                    dq.popleft()
                dq.append(now)
                repeat_hit = len(dq) > limit
        return flood_hit, repeat_hit

    def _cleanup(self):
        """清理过期的检测记录与 bot 探测缓存，防止内存膨胀"""
        now = time.time()
        max_window = self._max_window()
        for key in list(self._flood_history.keys()):
            dq = self._flood_history[key]
            while dq and now - dq[0] > max_window:
                dq.popleft()
            if not dq:
                del self._flood_history[key]
        for uid in list(self._repeat_history.keys()):
            inner = self._repeat_history[uid]
            for content in list(inner.keys()):
                dq = inner[content]
                while dq and now - dq[0] > max_window:
                    dq.popleft()
                if not dq:
                    del inner[content]
            if not inner:
                del self._repeat_history[uid]
        for uid in list(self._bot_cache.keys()):
            if self._bot_cache[uid][1] <= now:
                del self._bot_cache[uid]
        for uid in list(self._llm_ask_targets.keys()):
            if self._llm_ask_targets[uid] <= now:
                del self._llm_ask_targets[uid]
        for uid in list(self._flood_levels.keys()):
            rec = self._flood_levels[uid]
            if (
                rec["block_until"] <= now
                and now - rec["first_ts"] > float(
                    self.config.get("gradient_interval_seconds", 300)
                )
            ):
                del self._flood_levels[uid]

    # ==================== LLM 决策钩子 ====================

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """ask_llm 模式下，命中刷屏的用户发消息时向系统提示词输出指令说明。

        指令说明附带当前用户真实 ID（{user_id}），引导 LLM 自主选择
        是否回答刷屏信息：若决定不回答，可在回复中隐蔽输出关键指令
        <silent />，插件会在本地解析并决策不向群聊发送该条消息。
        本插件不设置任何屏蔽时间，决策完全本地执行。
        """
        group_id = event.get_group_id()
        if self._effective_action(str(group_id) if group_id else "") != "ask_llm":
            return
        sender_id = str(event.get_sender_id())
        now = time.time()
        expire = self._llm_ask_targets.get(
            self._user_key(event, sender_id), 0
        )
        if expire <= now:
            return
        prompt = str(self.config.get("flood_llm_prompt", "") or "")
        if not prompt:
            return
        self.stats["llm_judged"] += 1
        text = (
            prompt.replace("{user_id}", sender_id)
            .replace(
                "{user_name}",
                event.get_sender_name() or sender_id,
            )
            .strip()
        )
        if req.system_prompt:
            req.system_prompt += "\n" + text
        else:
            req.system_prompt = text

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp):
        """解析 LLM 输出中的隐蔽关键指令，本地决策是否发送到群聊。

        支持的指令格式（默认 <silent />，可配置）：
        - <silent />
        - <silent>任意文本</silent>
        - <silent reason="原因说明" />
        指令存在 → 本地决策：不发送本条消息（清空输出）。
        指令始终从输出中过滤，防止泄露给群聊。
        """
        group_id = event.get_group_id()
        if self._effective_action(str(group_id) if group_id else "") != "ask_llm":
            return
        text = getattr(resp, "completion_text", None)
        if not text:
            return
        tag = str(self.config.get("silent_tag", "silent")).strip() or "silent"
        pattern = re.compile(
            rf"<\s*{re.escape(tag)}(?:\s+[^>]*)?>.*?</\s*{re.escape(tag)}\s*>|"
            rf"<\s*{re.escape(tag)}(?:\s+[^>]*)?\s*/>",
            re.DOTALL | re.IGNORECASE,
        )
        if pattern.search(text):
            self.stats["llm_held"] += 1
            reason = "未说明"
            attr_match = re.search(
                rf'<\s*{re.escape(tag)}(?:\s+([^>]*?))?\s*/?>',
                text,
                re.DOTALL | re.IGNORECASE,
            )
            if attr_match and attr_match.group(1):
                reason_match = re.search(
                    r"reason\s*=\s*[\"']([^\"']*)[\"']",
                    attr_match.group(1),
                    re.IGNORECASE,
                )
                if reason_match:
                    reason = reason_match.group(1)
            logger.info(
                f"AI 输出 <{tag}> 关键指令，插件本地决策不发送本条消息"
                f"(原因: {reason})。"
            )
            resp.completion_text = ""
            return
        resp.completion_text = pattern.sub("", text).strip()

    @filter.event_message_type(EventMessageType.ALL, priority=100)
    async def intercept(self, event: AstrMessageEvent):
        """全局监听：选择性忽略 bot 消息 + 防刷屏检测"""
        self._msg_counter += 1
        if self._msg_counter % 100 == 0:
            self._cleanup()
            self._save_stats()
        try:
            sender_id = str(event.get_sender_id())
            if not sender_id:
                return
            # 跳过机器人自己发送的消息
            self_id = str(getattr(event.message_obj, "self_id", "") or "")
            if self_id and sender_id == self_id:
                return
            # 仅群聊模式
            if self.config.get("only_group", False) and not event.get_group_id():
                return

            sender_name = event.get_sender_name() or sender_id
            group_id = event.get_group_id()
            group_id_str = str(group_id) if group_id else ""
            user_key = self._user_key(event, sender_id)

            # === 0. 梯度硬拦截冷却期检查 ===
            rec = self._flood_levels.get(user_key)
            now0 = time.time()
            if rec and rec.get("block_until", 0) > now0:
                self.stats["flood_blocked"] += 1
                if self.config.get("log_ignored", True):
                    logger.info(
                        f"冷却期内拦截刷屏用户: {sender_name}({sender_id}) "
                        f"剩余 {int((rec['block_until'] - now0) / 60)} 分钟"
                    )
                event.stop_event()
                return

            # === 1. 选择性忽略其他 bot 的消息 ===
            if self.config.get("enable_bot_filter", True):
                mode = self._effective_mode(group_id_str)
                # manual_only 模式仅查手动名单，无需协议端探测
                if mode == "manual_only":
                    is_bot = self._in_manual_list(sender_id, sender_name)
                else:
                    is_bot = await self._is_bot(event, sender_id, sender_name)
                if is_bot and self._should_ignore_bot_message(
                    event, sender_id, sender_name, mode
                ):
                    self.stats["bot_ignored"] += 1
                    if self.config.get("log_ignored", True):
                        logger.info(
                            f"已忽略 bot 消息"
                            f"({_MODE_DESC.get(mode, mode)}): "
                            f"{sender_name}({sender_id})"
                        )
                    event.stop_event()
                    return

            # === 2. 防刷屏检测 ===
            flood_on = self.config.get("enable_flood_check", True)
            repeat_on = self.config.get("enable_repeat_check", True)
            if not flood_on and not repeat_on:
                return
            if self._is_exempt(event, sender_id):
                return
            # 被 @ 或带唤醒前缀的主动交互消息不参与刷屏检测
            if self.config.get("skip_flood_when_at", True) and getattr(
                event, "is_at_or_wake_command", False
            ):
                return

            now = time.time()
            content = event.message_str.strip()
            flood_hit, repeat_hit = self._check_flood(
                event, sender_id, content, now
            )
            if flood_hit or repeat_hit:
                reason = "条数超限" if flood_hit else "重复内容"
                action = self._effective_action(group_id_str)
                # 梯度处置：窗口内反复刷屏自动升级
                if self.config.get("flood_gradient", True):
                    rec = self._flood_levels.get(user_key)
                    if not rec:
                        rec = {"count": 0, "first_ts": now, "block_until": 0}
                        self._flood_levels[user_key] = rec
                    elif now - rec["first_ts"] > float(
                        self.config.get("gradient_interval_seconds", 300)
                    ):
                        rec.update(
                            {"count": 0, "first_ts": now, "block_until": 0}
                        )
                    rec["count"] += 1
                    hard_threshold = int(
                        self.config.get("gradient_hard_threshold", 3)
                    )
                    if rec["count"] >= hard_threshold:
                        # 硬拦截 + 冷却期
                        rec["block_until"] = now + float(
                            self.config.get("gradient_block_minutes", 10)
                        ) * 60
                        self.stats["flood_blocked"] += 1
                        if self.config.get("log_ignored", True):
                            logger.info(
                                f"刷屏命中 {rec['count']} 次，进入冷却期硬拦截: "
                                f"{sender_name}({sender_id}) 内容={content[:50]}"
                            )
                        event.stop_event()
                        return
                    if rec["count"] >= 2 and action == "silence":
                        # 第 2 次起升级为 ask_llm，给 AI 一次判断机会
                        action = "ask_llm"
                if action == "ask_llm":
                    # 交给 LLM 自主决定是否回答：记录提醒目标并放行消息
                    self._llm_ask_targets[user_key] = now + float(
                        self.config.get("ask_llm_window_seconds", 60)
                    )
                    if self.config.get("log_ignored", True):
                        logger.info(
                            f"刷屏命中 ({reason})，已交给 LLM 自行判断: "
                            f"{sender_name}({sender_id})"
                        )
                    return
                if flood_hit:
                    self.stats["flood_blocked"] += 1
                else:
                    self.stats["repeat_blocked"] += 1
                if self.config.get("log_ignored", True):
                    logger.info(
                        f"已拦截刷屏消息 ({reason}): {sender_name}({sender_id}) "
                        f"内容={content[:50]}"
                    )
                event.stop_event()
        except Exception as e:
            logger.error(f"拦截处理出错: {e}")

    # ==================== 管理指令 ====================

    @filter.command_group("af", alias={"防刷"})
    def flood(self):
        """防刷屏插件管理指令组"""

    @filter.permission_type(PermissionType.ADMIN)
    @flood.command("status")
    async def flood_status(self, event: AstrMessageEvent):
        """查看插件状态与拦截统计"""
        lines = [
            "【防刷屏插件状态】",
            f"- Bot 过滤: {'开启' if self.config.get('enable_bot_filter', True) else '关闭'}",
            f"- 过滤模式: "
            f"{_MODE_DESC.get(str(self.config.get('bot_filter_mode', 'trigger_only')), '未知')}",
            f"- 自动探测: {'开启' if self.config.get('auto_detect_bot', True) else '关闭'}",
            f"- 条数限制: "
            f"{self.config.get('flood_window_seconds', 5)} 秒内 "
            f"{self.config.get('flood_max_messages', 5)} 条"
            + (
                ""
                if self.config.get("enable_flood_check", True)
                else " (已关闭)"
            ),
            f"- 重复限制: "
            f"{self.config.get('repeat_window_seconds', 10)} 秒内 "
            f"{self.config.get('repeat_max_count', 3)} 次"
            + (
                ""
                if self.config.get("enable_repeat_check", True)
                else " (已关闭)"
            ),
            f"- 刷屏处置: "
            f"{_ACTION_DESC.get(str(self.config.get('flood_action', 'silence')), '未知')}"
            + (
                "（LLM 自主决定是否回答）"
                if self.config.get("flood_action") == "ask_llm"
                else ""
            ),
            f"- 梯度处置: {'开启' if self.config.get('flood_gradient', True) else '关闭'}"
            f"（{self.config.get('gradient_interval_seconds', 300)} 秒内 "
            f"{self.config.get('gradient_hard_threshold', 3)} 次进冷却 "
            f"{self.config.get('gradient_block_minutes', 10)} 分钟）",
            f"- 按群覆盖: {len(self._parse_group_overrides())} 个群",
            f"- 冷却中用户: "
            f"{', '.join(sorted(key.split(':user:', 1)[1] for key, r in self._flood_levels.items() if r.get('block_until', 0) > time.time())) or '无'}",
            f"- 手动 bot 名单: {', '.join(sorted(self._bot_id_set())) or '无'}",
            f"- 手动标记 bot: "
            f"{', '.join(sorted(self.marked_bots.keys())) or '无'}",
            f"- 昵称正则: "
            f"{str(self.config.get('bot_name_patterns', '') or '').strip() or '无'}",
            "",
            "【拦截统计】(重启后清零)",
            f"- 忽略 bot 消息: {self.stats['bot_ignored']}",
            f"- 拦截条数超限: {self.stats['flood_blocked']}",
            f"- 拦截重复内容: {self.stats['repeat_blocked']}",
            f"- LLM 判定刷屏: {self.stats['llm_judged']}",
            f"- LLM 指令不发送: {self.stats['llm_held']}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(PermissionType.ADMIN)
    @flood.command("mark_bot")
    async def mark_bot(self, event: AstrMessageEvent, user_id: str):
        """标记某用户为 bot，其消息将被忽略"""
        user_id = user_id.strip()
        if not user_id:
            yield event.plain_result("用法: /flood mark_bot <QQ号>")
            return
        self.marked_bots[user_id] = {"name": "", "ts": time.time()}
        self._save_marked()
        yield event.plain_result(f"已将 {user_id} 标记为 bot，其消息将被静默忽略。")

    @filter.permission_type(PermissionType.ADMIN)
    @flood.command("unmark_bot")
    async def unmark_bot(self, event: AstrMessageEvent, user_id: str):
        """取消对某用户的 bot 标记"""
        user_id = user_id.strip()
        if user_id in self.marked_bots:
            del self.marked_bots[user_id]
            self._save_marked()
            yield event.plain_result(f"已取消 {user_id} 的 bot 标记。")
        else:
            yield event.plain_result(f"{user_id} 不在标记名单中。")

    @filter.permission_type(PermissionType.ADMIN)
    @flood.command("marklist")
    async def marklist(self, event: AstrMessageEvent):
        """查看当前手动标记的 bot 列表"""
        if not self.marked_bots:
            yield event.plain_result("当前没有手动标记的 bot。")
            return
        lines = ["【手动标记的 bot 列表】"]
        for uid, info in self.marked_bots.items():
            name = info.get("name") or "未知昵称"
            lines.append(f"- {name} ({uid})")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(PermissionType.ADMIN)
    @flood.command("block")
    async def block(self, event: AstrMessageEvent, user_id: str, minutes: str = ""):
        """手动将用户拉入刷屏冷却，用法：/af block <用户号> [分钟]"""
        user_id = user_id.strip()
        if not user_id:
            yield event.plain_result("用法: /af block <用户号> [分钟]")
            return
        if minutes.strip().isdigit():
            duration = max(1, int(minutes))
        else:
            duration = int(self.config.get("gradient_block_minutes", 10))
        user_key = self._user_key(event, user_id)
        self._flood_levels[user_key] = {
            "count": int(self.config.get("gradient_hard_threshold", 3)),
            "first_ts": time.time(),
            "block_until": time.time() + duration * 60,
        }
        logger.info(
            f"管理员手动将 {user_id} 拉入刷屏冷却 {duration} 分钟。"
        )
        yield event.plain_result(f"已将 {user_id} 拉入刷屏冷却 {duration} 分钟。")

    @filter.permission_type(PermissionType.ADMIN)
    @flood.command("unblock")
    async def unblock(self, event: AstrMessageEvent, user_id: str):
        """手动解除用户的刷屏冷却，用法：/af unblock <用户号>"""
        user_id = user_id.strip()
        if not user_id:
            yield event.plain_result("用法: /af unblock <用户号>")
            return
        removed = False
        for key in list(self._flood_levels.keys()):
            if key.endswith(f":user:{user_id}"):
                del self._flood_levels[key]
                removed = True
        for key in list(self._llm_ask_targets.keys()):
            if key.endswith(f":user:{user_id}"):
                del self._llm_ask_targets[key]
        yield event.plain_result(
            f"已解除 {user_id} 的刷屏冷却。" if removed else f"{user_id} 不在冷却名单中。"
        )

    # ==================== 学习名单持久化 ====================

    def _marked_path(self) -> str:
        base = os.path.join(
            get_astrbot_data_path(), "plugin_data", "astrbot_plugin_anti_flood"
        )
        return os.path.join(base, "marked_bots.json")

    def _load_marked(self):
        """启动时加载手动标记名单"""
        try:
            path = self._marked_path()
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self.marked_bots = data
        except Exception as e:
            logger.error(f"加载标记名单失败: {e}")

    def _save_marked(self):
        """持久化手动标记名单"""
        try:
            path = self._marked_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.marked_bots, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存标记名单失败: {e}")

    async def terminate(self):
        """插件卸载/停用时保存数据"""
        self._save_marked()
        self._save_stats()
