"""ReplyGate：群聊唤醒窗口 + 主模型沉默/等待 + 出站闸门 + 审计流水。

三层分工：
  判定层  群聊窗口内判断"是不是在对机器人说话"（不看图片像素）
  主模型  正常回复，或用 SILENCE / HOLD 标记声明沉默与等待
  出站闸门 解析标记 → 丢弃 / 暂存后发 / 定时催促 / 打断与重生成
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict, deque
from sys import maxsize
from typing import Any, Iterable

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from astrbot.api.message_components import Plain

if __package__:  # 作为插件包加载（AstrBot 的正常路径）
    from .core.buffer import MessageBuffer, has_media
    from .core.commands import is_command_event, normalize_prefixes
    from .core.config import Config
    from .core.judge import (VERDICT_KEEP, VERDICT_NONE, VERDICT_REGEN,
                             SoftJudge)
    from .core.models import (JUDGE_FIXED_REASONS, JUDGE_REASON_LABEL,
                              JUDGE_RESULT_DISCARDED, JUDGE_RESULT_INTERRUPTED,
                              JUDGE_RESULT_JUDGING, JUDGE_RESULT_KEEP,
                              JUDGE_RESULT_PARTIAL, LogRow)
    from .core.outbound import OutboundGate
    from . import prompts
    from .store.db import Database
    from .webui import api as webui
else:  # 单文件调试（直接 python main.py）
    from core.buffer import MessageBuffer, has_media
    from core.commands import is_command_event, normalize_prefixes
    from core.config import Config
    from core.judge import (VERDICT_KEEP, VERDICT_NONE, VERDICT_REGEN,
                            SoftJudge)
    from core.models import (JUDGE_FIXED_REASONS, JUDGE_REASON_LABEL,
                             JUDGE_RESULT_DISCARDED, JUDGE_RESULT_INTERRUPTED,
                             JUDGE_RESULT_JUDGING, JUDGE_RESULT_KEEP,
                             JUDGE_RESULT_PARTIAL, LogRow)
    from core.outbound import OutboundGate
    import prompts
    from store.db import Database
    from webui import api as webui

PLUGIN_NAME = "astrbot_plugin_ReplyGate"
VERSION = "1.0.0"

# 面板可切换的运行开关：key 必须与 _conf_schema.json 一致，值为缺省值。
SWITCH_DEFAULTS: dict[str, bool] = {
    "gate_enabled": False,          # 总开关：关闭后插件完全不介入消息流
    "gate_dry_run": True,           # 仅观察：只判定与记录，不拦截
    "silence_enabled": True,        # 标记解析：解析 %%SILENCE%% / %%HOLD%%
    "prompt_block_inject": True,    # 提示词注入：把「沉默机制」块追加到系统提示词
    "command_bypass": True,         # 命令旁路：/ 开头的消息不进判定层、不算用户补充
    "group_window_enabled": False,  # 群聊 @ 后开启判定窗口（预留，尚未接入）
    "interrupt_group_enabled": False,   # 群聊打断：同用户新消息打断在途回答
    "interrupt_private_enabled": False, # 私聊打断
    "interrupt_other_users": False,     # 群聊里其它用户的消息也参与打断
    "segment_control_enabled": False,   # 分句由插件接管（可被新消息打断）
}

# 面板可用输入框修改的数值项：key -> (最小值, 最大值, 缺省值)
NUMERIC_OPTIONS: dict[str, tuple[int, int, int]] = {
    "buffer_quiet_ms": (0, 10000, 1500),      # 安静窗口（毫秒）
    "buffer_max_wait_ms": (0, 30000, 8000),   # 一批最多等多久（毫秒）
    "interrupt_window_ms": (500, 60000, 8000),  # 可打断的时间窗（毫秒）
    "soft_judge_threshold": (0, 100, 60),       # 弱打断判定阈值
    "soft_judge_context_before": (0, 20, 0),    # 生成点之前再多带几条
    "soft_judge_timeout_ms": (500, 60000, 4000),
    "soft_judge_max_restarts": (0, 20, 3),
    "soft_judge_max_tokens": (16, 1024, 256),   # 判定输出上限（思考模型 64 会全花在 reasoning 上）
    "segment_linear_start_ms": (0, 30000, 800),   # 一次函数：初始延迟
    "segment_linear_rate": (1, 200, 10),          # 一次函数：增长率（字/秒）
}

# 浮点项：key -> (最小, 最大, 缺省)
FLOAT_OPTIONS: dict[str, tuple[float, float, float]] = {
    "segment_log_base": (1.0, 10.0, 2.6),         # 对数间隔底数（同 AstrBot 的 log_base）
}

# 自由字符串项：key -> 最大长度（判定模型 provider id，由面板下拉选择）
FREE_STRINGS: dict[str, int] = {
    "soft_judge_provider_id": 200,
    "segment_separator": 16,
}

# 面板可切换的枚举项：key -> 允许值
STRING_OPTIONS: dict[str, tuple[str, ...]] = {
    "interrupt_mode": ("soft", "hard"),
    "soft_judge_fail_mode": ("regen", "keep"),
    "soft_judge_thinking_level": ("off", "minimal", "low"),
    "segment_delay_mode": ("log", "linear"),
}

# 自由字符串项里的分句标识（默认 $$$）
SEGMENT_SEPARATOR_DEFAULT = "$$$"

INTERRUPT_BUDGET_WINDOW_SECONDS = 600.0   # hard 打断计数的时间窗

# 日志字段白名单：LogRow 的字段即流水列
_LOG_FIELDS = ("kind", "umo", "gid", "sender_id", "sender_name", "reason", "act",
               "confidence", "wait_seconds", "followup", "ms", "model", "result", "detail",
               "user_input", "raw_output")

# 会写进 logger 的重要流水类型（只有这三类 + 异常值得看）
IMPORTANT_KINDS = {"silence", "hold", "judge", "recycle", "error"}


class Main(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.cfg = Config(config)
        self.version = VERSION
        self.startup_warnings: list[str] = []
        self.gate = OutboundGate(self)
        self.buffer = MessageBuffer()
        self.judge = SoftJudge(self)

        # 群聊窗口状态（预留）：umo -> WindowState
        self.windows: dict[str, Any] = {}
        # 在途回答：umo -> [ {event, sender_id, started_at}, ... ]（最多 4 轮）
        self.inflight: dict[str, list[dict[str, Any]]] = {}
        # 判定层每小时计数：umo -> (hour_bucket, count)
        self.judge_budget: dict[str, tuple[int, int]] = {}
        # 已发送待回收标记，避免重复注入同一段草稿
        self._recycle_used: OrderedDict[str, float] = OrderedDict()
        self._log_tasks: set[asyncio.Task] = set()
        # hard 打断预算：umo -> (窗口起点, 已用次数)
        self._interrupt_budget: dict[str, tuple[float, int]] = {}
        # 打断激活窗口：umo -> 截止时刻（从"第一条打断消息"起算）
        self._interrupt_deadline: dict[str, float] = {}
        # 已经决定必须重答（hard / @）：umo -> True，交付主模型时消费
        self._force_answer: dict[str, bool] = {}
        # 进行中的判定（弱打断）：umo -> {session, sender_id, detail}
        self._judge_sessions: dict[str, dict[str, Any]] = {}
        # 当前会话最新一条判定流水：一行到底，结算与后续补充都改这一行
        self._judge_row: dict[str, dict[str, Any]] = {}
        # 分句发送：umo -> 当前发送批次号；被打断时记录该批次号
        self._segment_token: dict[str, int] = {}
        self._segment_abort: dict[str, int] = {}
        # 每条消息的到达序号（umo -> 自增），用于判断"哪些消息没被回答过"
        self._msg_seq: dict[str, int] = {}
        # 生成期间到达、还没被任何回答覆盖的消息：umo -> deque[{seq, sender, text, ts}]
        self._pending: dict[str, deque] = {}
        # 会话消息流水（判定模型的上下文来源）：umo -> deque[{ts, sender, text}]
        self.history: dict[str, deque] = {}

        try:
            self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ReplyGate] 无法创建数据目录: {exc!r}")
            self.data_dir = None
        self.db = Database(self.data_dir / "events.db") if self.data_dir else Database(
            __import__("pathlib").Path("events.db"))

    # ------------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        try:
            await self.db.init()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ReplyGate] 流水库初始化失败: {exc!r}")

        webui.register(self, self.context, PLUGIN_NAME)
        self._check_environment()
        asyncio.ensure_future(self._prune_loop())
        logger.info(
            "[ReplyGate] 已启动（enabled=%s, dry_run=%s, silence=%s）",
            self.cfg.enabled, self.cfg.dry_run, self.cfg.silence_enabled)

    async def terminate(self) -> None:
        self.judge.clear()
        self.buffer.clear()
        self.gate.shutdown()
        for task in list(self._log_tasks):
            task.cancel()
        logger.info("[ReplyGate] 已停止")

    def _check_environment(self) -> None:
        """启动自检：把"会导致本插件失效"的配置问题直接写进日志与面板。"""
        warnings: list[str] = []
        try:
            config = self.context.get_config()
            settings = config.get("provider_settings", {}) if isinstance(config, dict) else {}
            if bool(settings.get("streaming_response", False)):
                warnings.append(
                    "流式输出已开启：出站闸门在流式模式下不保证生效，请关闭 provider_settings.streaming_response")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"读取 AstrBot 配置失败：{type(exc).__name__}")
        try:
            config = self.context.get_config()
            ltm = config.get("provider_ltm_settings", {}) if isinstance(config, dict) else {}
            active = (ltm or {}).get("active_reply", {}) or {}
            if bool(active.get("enable", False)):
                warnings.append(
                    "AstrBot「主动回复概率」已开启：它会绕过 pipeline 独立发送，可能与本插件双发，建议关闭")
        except Exception:
            pass
        warnings.extend(self._check_conflicting_plugins())
        self.startup_warnings = warnings
        for item in warnings:
            logger.warning(f"[ReplyGate] {item}")

    def _check_conflicting_plugins(self) -> list[str]:
        """另一个拦截类插件同时启用时，沉默/暂存可能来自它，排查会很费劲。"""
        names = ("astrbot_plugin_LivingHello", "astrbot_plugin_LivingGate")
        loaded: set[str] = set()
        try:
            for star in self.context.get_all_stars() or []:
                if getattr(star, "activated", False) or getattr(star, "active", False):
                    loaded.add(str(getattr(star, "name", "") or ""))
        except Exception:  # noqa: BLE001 - 拿不到插件列表就不提示
            return []
        hits = [name for name in names if name in loaded]
        if not hits:
            return []
        return [
            f"{'、'.join(hits)} 同时处于启用状态：两套拦截会互掐，"
            "沉默/暂存可能不是本插件做的，请只保留一个"
        ]

    async def _prune_loop(self) -> None:
        """每天清理一次过期流水。"""
        while True:
            try:
                await asyncio.sleep(3600)
                days = self.cfg.int("log_retention_days", 14, 1, 365)
                rows = self.cfg.int("log_max_rows", 50000, 1000, 500000)
                removed = await self.db.prune(days, rows)
                if removed:
                    logger.info(f"[ReplyGate] 已清理 {removed} 条过期流水")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ReplyGate] 清理流水失败: {exc!r}")

    # ------------------------------------------------------------------ 流水

    @staticmethod
    def _record(row: dict[str, Any]) -> dict[str, Any]:
        payload = {key: row.get(key) for key in _LOG_FIELDS if key in row}
        payload.setdefault("kind", "error")
        payload["ts"] = time.time()
        try:
            return LogRow(**payload).as_dict()
        except TypeError:
            return payload

    async def log_event_row(self, **row: Any) -> int:
        """写一条流水并返回自增 id（HOLD 的“等待中”需要后续原地改状态）。"""
        record = self._record(row)
        if not self.cfg.bool("log_enabled", True) or not getattr(self.db, "ready", False):
            return 0
        try:
            return await self.db.insert_one(record)
        except Exception:  # noqa: BLE001
            return 0

    async def update_log_status(self, row_id: int, result: str,
                                detail: str = "", **fields: Any) -> bool:
        """就地改一条流水的状态（HOLD 取消/发出、判定结算都用它）。"""
        if not row_id or not self.cfg.bool("log_enabled", True):
            return False
        payload = {key: value for key, value in fields.items() if value is not None}
        if detail:
            payload["detail"] = detail
        try:
            return await self.db.update_status(row_id, result, **payload)
        except TypeError:  # 旧版 db 只认 detail
            return await self.db.update_status(row_id, result, detail or None)
        except Exception:  # noqa: BLE001
            return False

    def log_event(self, **row: Any) -> None:
        if not self.cfg.bool("log_enabled", True):
            return
        record = self._record(row)
        kind = str(record.get("kind") or "")
        if kind in IMPORTANT_KINDS:
            logger.info(
                "[ReplyGate] %s umo=%s sender=%s reason=%s result=%s detail=%s",
                kind, record.get("umo"), record.get("sender_id"),
                record.get("reason"), record.get("result"), record.get("detail"))

        async def writer() -> None:
            try:
                await self.db.insert([record])
            except Exception:
                pass

        task = asyncio.ensure_future(writer())
        self._log_tasks.add(task)
        task.add_done_callback(self._log_tasks.discard)

    # ------------------------------------------------------------------ 供面板调用

    def wake_prefixes(self) -> tuple[str, ...]:
        """AstrBot 配置里的唤醒/命令前缀（默认 ``/``），用于命令消息旁路。"""
        try:
            config = self.context.get_config()
            raw = config.get("wake_prefix") if isinstance(config, dict) else None
        except Exception as exc:  # noqa: BLE001 - 读不到就退回默认值
            logger.debug(f"[ReplyGate] 读取 wake_prefix 失败: {exc!r}")
            raw = None
        return normalize_prefixes(raw)

    def is_command(self, event: AstrMessageEvent) -> bool:
        """``/`` 开头的命令消息：不算对话，判定层与窗口状态机必须一律跳过。"""
        if not self.cfg.bool("command_bypass", True):
            return False
        return is_command_event(event, self.wake_prefixes())

    def switch_snapshot(self) -> dict[str, bool]:
        """当前运行开关的生效值（读的是内存配置，切换后立即生效）。"""
        return {key: self.cfg.bool(key, default) for key, default in SWITCH_DEFAULTS.items()}

    def option_snapshot(self) -> dict[str, Any]:
        """当前数值项与枚举项的生效值；面板用输入框/按钮修改。"""
        snapshot: dict[str, Any] = {
            key: self.cfg.int(key, default, low, high)
            for key, (low, high, default) in NUMERIC_OPTIONS.items()
        }
        for key, allowed in STRING_OPTIONS.items():
            snapshot[key] = self.cfg.option(key, allowed[0], allowed)
        for key, (_low, _high, default) in FLOAT_OPTIONS.items():
            try:
                snapshot[key] = float(self.cfg.raw.get(key, default))
            except (TypeError, ValueError):
                snapshot[key] = default
        for key in FREE_STRINGS:
            snapshot[key] = self.cfg.str(key, "")
        return snapshot

    @staticmethod
    def option_types() -> dict[str, Any]:
        """给面板/接口做类型判定的白名单。"""
        return {"bools": list(SWITCH_DEFAULTS),
                "ints": dict(NUMERIC_OPTIONS),
                "strs": {key: list(values) for key, values in STRING_OPTIONS.items()},
                "free_strs": dict(FREE_STRINGS),
                "floats": dict(FLOAT_OPTIONS)}

    async def set_option(self, key: str, value: Any) -> dict[str, Any]:
        """改一个运行开关（bool）或数值项（int），写回插件配置。"""
        if key in SWITCH_DEFAULTS:
            after: Any = bool(value)
            before = self.cfg.bool(key, SWITCH_DEFAULTS[key])
        elif key in NUMERIC_OPTIONS:
            low, high, default = NUMERIC_OPTIONS[key]
            try:
                after = int(float(value))
            except (TypeError, ValueError):
                after = default
            after = max(low, min(high, after))
            before = self.cfg.int(key, default, low, high)
        elif key in FLOAT_OPTIONS:
            low, high, default = FLOAT_OPTIONS[key]
            try:
                after = round(float(value), 2)
            except (TypeError, ValueError):
                after = default
            after = max(low, min(high, after))
            before = float(self.cfg.raw.get(key, default) or default)
        elif key in FREE_STRINGS:
            text = str(value or "").strip()
            if len(text) > FREE_STRINGS[key]:
                raise ValueError(key)
            after = text
            before = self.cfg.str(key, "")
        elif key in STRING_OPTIONS:
            allowed = STRING_OPTIONS[key]
            after = str(value or "").strip().lower()
            if after not in allowed:
                raise ValueError(key)
            before = self.cfg.option(key, allowed[0], allowed)
        else:
            raise KeyError(key)

        # 缓冲两个时间保持 "单次 ≤ 最大总" 的关系
        if key == "buffer_quiet_ms":
            total = self.cfg.int("buffer_max_wait_ms", 8000, 0, 30000)
            if after > total:
                self.cfg.raw["buffer_max_wait_ms"] = after
        elif key == "buffer_max_wait_ms":
            single = self.cfg.int("buffer_quiet_ms", 1500, 0, 10000)
            if single and after < single:
                after = single
        self.cfg.raw[key] = after
        persisted = await self._persist_config()
        # 面板按钮/输入框的改动不进流水（只写日志目录）
        logger.info("[ReplyGate] 配置 %s: %s → %s（写盘=%s）", key, before, after, persisted)
        return {"key": key, "value": after, "persisted": persisted,
                "switches": self.switch_snapshot(), "options": self.option_snapshot()}

    async def set_switch(self, key: str, value: bool) -> dict[str, Any]:
        """切换一个运行开关并写回插件配置；返回新的快照。"""
        return await self.set_option(key, value)

    async def _persist_config(self) -> bool:
        """把插件配置写回 data/config/<plugin>_config.json。"""
        raw = self.cfg.raw
        backend = getattr(raw, "save_config_async", None)
        try:
            if callable(backend):
                await backend()
                return True
            fallback = getattr(raw, "save_config", None)
            if callable(fallback):
                await asyncio.to_thread(fallback)
                return True
        except Exception as exc:  # noqa: BLE001 - 落盘失败不影响本次会话生效
            logger.error(f"[ReplyGate] 保存插件配置失败: {exc!r}")
        return False

    def runtime_snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.cfg.enabled,
            "dry_run": self.cfg.dry_run,
            "switches": self.switch_snapshot(),
            "group_window_enabled": self.cfg.bool("group_window_enabled", False),
            "windows": len(self.windows),
            "inflight": sum(len(items) for items in self.inflight.values()),
            "holds": len(self.gate.holds),
            "recycled": len(self.gate.recycled),
            "buffered": len(self.buffer.snapshot()),
            "options": self.option_snapshot(),
            "judge_budget": {key: value[1] for key, value in self.judge_budget.items()},
        }

    def window_snapshot(self) -> dict[str, Any]:
        rows = []
        for umo, state in self.windows.items():
            rows.append({
                "umo": umo,
                "active": getattr(state, "active", True),
                "opened_at": getattr(state, "opened_at", 0),
                "waker": getattr(state, "waker_id", ""),
                "valid_users": sorted(getattr(state, "valid_users", []) or []),
                "streak": getattr(state, "streak_not_for_me", 0),
                "last_activity_at": getattr(state, "last_activity_at", 0),
            })
        holds = [{"umo": session.umo, "reason": session.reason, "scope": session.scope,
                  "seconds": session.seconds, "sender_id": session.sender_id}
                 for session in self.gate.holds.values()]
        inflight = [
            {"umo": umo, "sender_id": info.get("sender_id", ""),
             "elapsed_ms": int((time.time() - float(info.get("started_at") or 0)) * 1000)}
            for umo, rounds in self.inflight.items() for info in rounds
        ]
        return {"windows": rows, "holds": holds, "inflight": inflight,
                "buffering": self.buffer.snapshot()}

    def close_windows(self, umo: str = "") -> int:
        if umo:
            closed = 1 if self.windows.pop(umo, None) is not None else 0
        else:
            closed = len(self.windows)
            self.windows.clear()
        self.gate.holds.clear()
        if closed:
            logger.info(f"[ReplyGate] 手动结束窗口 {umo or '（全部）'}：{closed} 个")
        return closed

    def persona_template(self) -> str:
        return prompts.persona_template()

    # ---- 判定提示词（弱打断，面板可编辑） ----

    def judge_prompt(self) -> tuple[str, bool]:
        custom = self.cfg.str("judge_prompt_text", "")
        return prompts.effective_judge_prompt(custom), bool(custom.strip())

    def list_providers(self) -> list[dict[str, str]]:
        """所有已配置的对话模型，供面板下拉选择判定模型。"""
        rows: list[dict[str, str]] = []
        try:
            providers = self.context.get_all_providers() or []
        except Exception as exc:  # noqa: BLE001 - 拿不到列表就返回空
            logger.debug(f"[ReplyGate] 读取 provider 列表失败: {exc!r}")
            return rows
        for provider in providers:
            try:
                config = getattr(provider, "provider_config", {}) or {}
                getter = getattr(provider, "get_model", None)
                model = str(getter() if callable(getter) else "") or str(config.get("model") or "")
                rows.append({
                    "id": str(config.get("id") or ""),
                    "model": model,
                    "type": str(config.get("type") or ""),
                })
            except Exception:  # noqa: BLE001 - 单个 provider 异常不影响整体
                continue
        rows = [row for row in rows if row.get("id")]
        rows.sort(key=lambda row: (row["id"].casefold(), row["model"].casefold()))
        return rows

    def judge_provider_label(self) -> str:
        """当前判定模型的显示名（面板/流水用）。"""
        pid = self.cfg.str("soft_judge_provider_id", "")
        for row in self.list_providers():
            if row["id"] == pid:
                return f'{row["model"] or row["id"]}（{row["id"]}）' if row["model"] else row["id"]
        return pid or "跟随当前会话模型"

    def judge_prompt_for(self, window: list[str], draft: str = "") -> str:
        """把对话窗口（和可选草稿）填进判定提示词（供 core/judge.py 调用）。"""
        return prompts.build_judge_prompt(
            window, self.cfg.str("judge_prompt_text", ""), draft)

    async def set_judge_prompt(self, text: str | None) -> dict[str, Any]:
        self.cfg.raw["judge_prompt_text"] = "" if text is None else str(text)
        persisted = await self._persist_config()
        block, custom = self.judge_prompt()
        return {"text": block, "is_custom": custom, "persisted": persisted}

    # ---- 提示词块（面板可编辑，存插件配置） ----

    def prompt_block(self) -> tuple[str, bool]:
        """(生效文本, 是否自定义)。"""
        custom = self.cfg.str("prompt_block_text", "")
        return prompts.effective_block(custom), bool(custom.strip())

    async def set_prompt_block(self, text: str | None) -> dict[str, Any]:
        """写入自定义提示词块；text=None 表示还原为插件默认。"""
        if text is None:
            self.cfg.raw.pop("prompt_block_text", None)
            self.cfg.raw["prompt_block_text"] = ""
        else:
            self.cfg.raw["prompt_block_text"] = str(text)
        persisted = await self._persist_config()
        block, custom = self.prompt_block()
        return {"text": block, "is_custom": custom, "persisted": persisted}

    # ------------------------------------------------------------------ 钩子

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-maxsize)
    async def on_inbound(self, event: AstrMessageEvent):
        """消息到达。**除"主模型什么时候收到消息"外，一切都立即发生。**

        顺序：命令旁路 → 撤销 HOLD（立即）→ 打断/判定判定（立即）→ 缓冲（只影响主模型）。

        命令消息（``/`` 开头）直接旁路：
          - 不进判定层 / 窗口状态机（它是对命令插件的指令，不是在跟机器人聊天）
          - 也不打断 HOLD、不参与缓冲（bridge 对命令是立即推送）
          - 出站层不特殊处理：命令不会真正调用模型，正常不会有出站结果
        """
        try:
            if not self.cfg.enabled:
                return
            if self.is_command(event):
                logger.debug("[ReplyGate] 命令消息旁路：%s", event.unified_msg_origin)
                return
            umo = event.unified_msg_origin
            sender_id = str(event.get_sender_id() or "")
            text = str(getattr(event, "message_str", "") or "")

            # 1) 撤销 HOLD：立即（缓冲不参与）
            self.gate.on_message(event)
            # 2) 记会话流水 + 未回复队列（判定上下文用，逐条记，不受缓冲影响）
            seq = self._next_seq(umo)
            self._remember(event, text)
            if self.inflight.get(umo):
                self._note_unanswered(umo, seq, event, text)
            # 3) 打断 / 判定：立即触发（硬打断当场停；弱打断当场起/重启判定模型）
            await self._decide_interrupt(event, sender_id)
            # 4) 缓冲：只决定"主模型什么时候收到这条/这批消息"
            await self._deliver_with_buffer(event, sender_id, text, seq)
        except Exception as exc:  # noqa: BLE001 - 入站异常绝不影响正常回复
            self.log_event(kind="error", umo=event.unified_msg_origin,
                           result="failed", detail=f"inbound:{type(exc).__name__}")

    # ---- 缓冲：只影响主模型 ----

    def buffer_quiet_seconds(self) -> float:
        """单次缓冲时间：每来一条新消息就重新计时（防抖）。"""
        return self.cfg.int("buffer_quiet_ms", 1500, 0, 10000) / 1000.0

    def buffering_enabled(self) -> bool:
        """单次缓冲时间 > 0 才算开启缓冲；0 表示关闭。"""
        return self.buffer_quiet_seconds() > 0

    def buffer_max_wait_seconds(self) -> float:
        """最大总缓冲时间：从第一条被缓冲的消息开始计时，到点必须放行。"""
        total = self.cfg.int("buffer_max_wait_ms", 8000, 0, 30000) / 1000.0
        return max(total, self.buffer_quiet_seconds())

    async def _deliver_with_buffer(self, event: AstrMessageEvent,
                                   sender_id: str, text: str, seq: int = 0) -> None:
        umo = event.unified_msg_origin
        wake = bool(getattr(event, "is_at_or_wake_command", False))

        if not self.buffering_enabled():
            if await self._settle_interrupt(event, umo, sender_id):
                self._prepare_pending_note(event, umo, [seq])
                self._mark_inflight(event, sender_id)
            else:
                event.stop_event()
            return

        batch, owner = self.buffer.offer(
            umo, text, sender_id, wake, immediate=has_media(event), seq=seq)
        if not owner:
            # 已并入上一批：本条不再单独走管线，文本由批次统一带出；
            # 但它的掉线/唤醒标记、以及上面的打断判定都已生效。
            event.stop_event()
            return

        try:
            batch = await self.buffer.settle(
                batch, self.buffer_quiet_seconds(), self.buffer_max_wait_seconds())
            self._apply_batch(event, batch)
            if not await self._settle_interrupt(
                    event, umo, batch.last_sender or sender_id):
                event.stop_event()
                return
            self._prepare_pending_note(event, umo, batch.seqs or [seq])
            self._mark_inflight(event, batch.last_sender or sender_id)
        finally:
            # 正常结束时 settle 已摘除；被取消/异常时这里兜底
            self.buffer.drop(umo)

    # ---- 未回复消息（打断后补答） ----

    def _next_seq(self, umo: str) -> int:
        seq = self._msg_seq.get(umo, 0) + 1
        self._msg_seq[umo] = seq
        return seq

    def _note_unanswered(self, umo: str, seq: int, event: AstrMessageEvent,
                         text: str) -> None:
        """在途回答生成期间到达的消息：它一定没被这条回答覆盖。"""
        cleaned = (text or "").strip()
        if not cleaned:
            return
        name = ""
        try:
            name = str(event.get_sender_name() or "") or str(event.get_sender_id() or "?")
        except Exception:  # noqa: BLE001
            name = "?"
        bucket = self._pending.get(umo)
        if bucket is None:
            bucket = deque(maxlen=10)
            self._pending[umo] = bucket
        bucket.append({"seq": seq, "sender": name, "text": cleaned[:300],
                       "ts": time.time()})

    def _prepare_pending_note(self, event: AstrMessageEvent, umo: str,
                              covered: list[int]) -> None:
        """交付前：把"更早且没被回答过"的消息整理成提示，挂到事件上。

        covered = 本轮消息的序号；序号在它之前的未回复消息需要一并交代。
        """
        bucket = self._pending.get(umo)
        if not bucket:
            return
        oldest = min(covered) if covered else 0
        older = [item for item in bucket if item["seq"] < oldest]
        # 本轮覆盖到的消息已经会被回答 → 从队列里去掉；更早的一并交代后也清掉
        self._pending.pop(umo, None)
        if not older:
            return
        lines = [f'- {item["sender"]}：{item["text"]}' for item in older[-8:]]
        note = ("【尚未回复的消息（在你上一条回答生成期间到达，请在本轮一并回应，"
                "不要漏掉，也不要重复回答已经回过的内容）】\n" + "\n".join(lines))
        try:
            event.set_extra("replygate_unanswered_note", note)
        except Exception:  # noqa: BLE001 - 老版本事件可能不支持 extra
            pass

    # ---- 会话消息流水（判定上下文） ----

    def _remember(self, event: AstrMessageEvent, text: str) -> None:
        cleaned = (text or "").strip()
        if not cleaned:
            return
        name = ""
        try:
            name = str(event.get_sender_name() or "")
        except Exception:  # noqa: BLE001
            name = ""
        entry = {"ts": time.time(),
                 "sender": name or str(event.get_sender_id() or "?"),
                 "text": cleaned}
        bucket = self.history.get(event.unified_msg_origin)
        if bucket is None:
            bucket = deque(maxlen=40)
            self.history[event.unified_msg_origin] = bucket
        bucket.append(entry)

    def _judge_window(self, umo: str, round_info: dict[str, Any]) -> list[str]:
        """从生成点前 x 条，到最新一条（含触发它的那条）。"""
        x = self.cfg.int("soft_judge_context_before", 0, 0, 20)
        entries = list(self.history.get(umo) or [])
        if not entries:
            return []
        started_at = float(round_info.get("started_at") or 0)
        first_after = len(entries)
        for index, entry in enumerate(entries):
            if float(entry.get("ts") or 0) >= started_at:
                first_after = index
                break
        window = entries[max(0, first_after - x):]
        return [f'{entry["sender"]}：{entry["text"]}' for entry in window][-20:]

    @staticmethod
    def _apply_batch(event: AstrMessageEvent, batch: Any) -> None:
        """把批次合并后的文本写回主人事件（保留 At/图片等组件）。"""
        merged = batch.merged
        if merged:
            event.message_str = merged
            try:
                chain = list(getattr(event.message_obj, "message", []) or [])
                kept = [comp for comp in chain if not isinstance(comp, Plain)]
                kept.append(Plain(text=merged))
                event.message_obj.message = kept
            except Exception:  # noqa: BLE001 - 链改写失败时至少有 message_str
                pass
        if batch.wake:
            event.is_at_or_wake_command = True

    # ---- 打断 ----

    def _interrupt_window_open(self, umo: str) -> bool:
        """激活窗口：第一条打断消息开窗，窗口内到达的消息才进入判定/打断。

        窗口关闭后**不再接受新的判定触发**，但已经在跑的判定结果仍然有效，
        回答依旧要等它（on_outbound 负责等待）。
        """
        window = self.cfg.int("interrupt_window_ms", 8000, 500, 60000) / 1000.0
        now = time.monotonic()
        deadline = self._interrupt_deadline.get(umo)
        if deadline is None:
            # 注意：先判断"有没有在途回答"再调用本方法，这里只负责开窗
            self._interrupt_deadline[umo] = now + window
            return True
        return now <= deadline

    def _reset_interrupt_window(self, umo: str) -> None:
        """回答成功发出 / 弱打断判定为重新生成 → 计时归零。"""
        self._interrupt_deadline.pop(umo, None)

    def _is_forced(self, event: AstrMessageEvent) -> bool:
        """消息是否"明确点名了机器人"（@ 或唤醒前缀）。

        注意：私聊里 AstrBot 会把每条消息都标记为 is_at_or_wake_command=True，
        那是"私聊默认唤醒"，不是点名——所以私聊一律不按 forced 处理，否则弱打断永远走不到判定。
        """
        if not getattr(event, "is_at_or_wake_command", False):
            return False
        try:
            if event.is_private_chat():
                return False
        except Exception:  # noqa: BLE001
            return False
        try:
            self_id = str(event.get_self_id() or "")
            for comp in event.get_messages() or []:
                if type(comp).__name__ == "At" and str(getattr(comp, "qq", "")) == self_id:
                    return True
        except Exception:  # noqa: BLE001 - 探测失败按非点名处理
            pass
        return is_command_event(event, self.wake_prefixes())

    def _interrupt_enabled(self, event: AstrMessageEvent) -> bool:
        key = "interrupt_private_enabled" if event.is_private_chat()             else "interrupt_group_enabled"
        return self.cfg.bool(key, False)

    def _hard_budget_left(self, umo: str) -> bool:
        limit = self.cfg.int("interrupt_cooldown_count", 3, 0, 20)
        if limit <= 0:
            return False
        now = time.monotonic()
        start, used = self._interrupt_budget.get(umo, (now, 0))
        if now - start > INTERRUPT_BUDGET_WINDOW_SECONDS:
            start, used = now, 0
        if used >= limit:
            self._interrupt_budget[umo] = (start, used)
            return False
        self._interrupt_budget[umo] = (start, used + 1)
        return True

    @staticmethod
    def _stop_active(umo: str, event: AstrMessageEvent) -> int:
        """请求停止该会话在途的执行；返回停止数，-1 表示内部接口不可用。"""
        try:
            from astrbot.core.utils.active_event_registry import active_event_registry
        except Exception:  # noqa: BLE001 - 版本差异时降级
            return -1
        try:
            return int(active_event_registry.request_agent_stop_all(umo, exclude=event))
        except Exception:  # noqa: BLE001
            return -1

    async def _decide_interrupt(self, event: AstrMessageEvent, sender_id: str) -> None:
        """消息到达时**立刻**做打断/判定的决定（不等缓冲）。

        判定与打断共用一种流水（kind=judge），一条判定自始至终只有一行：
          - 强打断 hard：不调判定模型，当场停掉在途；原因=强打断，置信度=100
          - @ 点名（forced）：同上；原因=用户要求，置信度=100
          - 弱打断 soft：起（或重启）判定模型 → 先记「判定中」，结算时改状态
          - 条件不满足（窗口外、不是同一个人、开关关）：什么都不做，也不记流水
        """
        umo = event.unified_msg_origin
        rounds = self.inflight.get(umo) or []
        if not rounds:
            return
        inflight = rounds[-1]
        elapsed_ms = int((time.time() - float(inflight.get("started_at") or 0)) * 1000)
        window_ms = self.cfg.int("interrupt_window_ms", 8000, 500, 60000)
        if inflight.get("discard") and elapsed_ms > window_ms:
            # 已被打断/作废、且早就过了窗口：这轮不会再产出，静默清掉
            self._drop_round(umo, inflight)
            return

        same_sender = str(inflight.get("sender_id") or "") == str(sender_id or "")
        if not same_sender and not self.cfg.bool("interrupt_other_users", False):
            return
        if not self._interrupt_enabled(event):
            return
        if not self._interrupt_window_open(umo):
            return

        forced = self._is_forced(event)
        mode = self.cfg.option("interrupt_mode", "soft", ("soft", "hard"))
        trigger_text = str(getattr(event, "message_str", "") or "")[:500]
        detail = f"{'同用户' if same_sender else '其它用户'} {sender_id} 又发消息（在途 {elapsed_ms}ms）"

        if mode == "hard":
            if self._hard_budget_left(umo):
                await self._regen(event, umo, sender_id, inflight, detail,
                                  reason_code="hard_interrupt", confidence=100,
                                  user_input=trigger_text)
                return
            detail += "｜强打断次数已用尽，本次改走判定"
            if forced:
                await self._regen(event, umo, sender_id, inflight, detail,
                                  reason_code="user_request", confidence=100,
                                  user_input=trigger_text)
                return
            self._start_judge(event, umo, sender_id, inflight, detail, trigger_text)
            return
        if forced:
            await self._regen(event, umo, sender_id, inflight, detail,
                              reason_code="user_request", confidence=100,
                              user_input=trigger_text)
            return
        self._start_judge(event, umo, sender_id, inflight, detail, trigger_text)

    def _start_judge(self, event: AstrMessageEvent, umo: str, sender_id: str,
                     inflight: dict[str, Any], detail: str,
                     trigger_text: str = "") -> None:
        """起/重启判定模型。判定期间再来新消息会取消上一次、用新窗口重判（流水还是同一行）。"""
        window = self._judge_window(umo, inflight)
        session = self.judge.start(umo, window, sender_id=sender_id, detail=detail,
                                   trigger=trigger_text)
        if session is None:
            return
        inflight["judge"] = session
        self._judge_sessions[umo] = {"session": session, "sender_id": sender_id,
                                     "detail": detail}

    async def _settle_interrupt(self, event: AstrMessageEvent, umo: str,
                                sender_id: str) -> bool:
        """交付主模型前结算判定结果。True = 继续回答；False = 本条不回答。"""
        forced = self._force_answer.pop(umo, False)
        pending = self._judge_sessions.pop(umo, None)
        if forced:
            event.is_at_or_wake_command = True
            return True
        if pending is None:
            return True

        session = pending["session"]
        detail = pending["detail"]
        verdict = await self.judge.await_verdict(umo)
        score = getattr(session, "score", None)
        reason = getattr(session, "reason", "") or ""
        rounds = self.inflight.get(umo) or []
        inflight = rounds[-1] if rounds else None

        if verdict == VERDICT_REGEN:
            await self._regen(event, umo, sender_id, inflight, detail,
                              reason_code=reason or "unspecified", confidence=score,
                              session=session,
                              user_input=str(getattr(event, "message_str", "") or "")[:500])
            self._reset_interrupt_window(umo)
            return True

        if verdict == VERDICT_KEEP:
            event.is_at_or_wake_command = False
            await self._finish_judge_row(
                session, JUDGE_RESULT_KEEP, reason=reason, confidence=score,
                detail=f"{detail}｜不重发（保留旧回答，本条不单独回答）")
            return False

        fail = self.cfg.option("soft_judge_fail_mode", VERDICT_REGEN,
                               (VERDICT_REGEN, VERDICT_KEEP))
        if fail == VERDICT_REGEN:
            await self._regen(event, umo, sender_id, inflight, detail,
                              reason_code="judge_unavailable", confidence=None,
                              session=session,
                              user_input=str(getattr(event, "message_str", "") or "")[:500])
            return True
        event.is_at_or_wake_command = False
        await self._finish_judge_row(
            session, JUDGE_RESULT_KEEP, reason="judge_unavailable", confidence=score,
            detail=f"{detail}｜判定不可用，按失败策略保留旧回答（不重发）")
        return False

    def _verdict_status(self, stopped: int) -> str:
        """按实际动作定状态：真停掉了在途生成 = 已打断，否则旧回答只会被丢弃 = 已作废。"""
        return JUDGE_RESULT_INTERRUPTED if stopped > 0 else JUDGE_RESULT_DISCARDED

    async def _regen(self, event: AstrMessageEvent, umo: str, sender_id: str,
                     inflight: dict[str, Any] | None, detail: str, *,
                     reason_code: str, confidence: int | None = None,
                     session: Any = None, mark_force: bool = True,
                     user_input: str = "") -> None:
        """决定重新回答：停掉在途执行 / 丢弃旧回答，下一步消息必须重答。"""
        if inflight is None:
            # 找不到对应轮次时，至少把"这条事件自己的那轮"标掉
            inflight = self._round_for_event(event)
        if inflight is None:
            rounds = self.inflight.get(umo) or []
            inflight = rounds[-1] if rounds else None
        if inflight is not None:
            inflight["discard"] = True
        token = self._segment_token.get(umo)
        if token is not None:
            self._segment_abort[umo] = token
        stopped = self._stop_active(umo, event)
        if mark_force:
            self._force_answer[umo] = True
        event.is_at_or_wake_command = True

        status = self._verdict_status(stopped)
        notes: list[str] = []
        if reason_code == "hard_interrupt":
            if stopped < 0:
                notes.append("打断接口不可用，已降级为另起一轮")
            elif stopped == 0:
                notes.append("没有在途回答可打断，旧回答直接作废")
            else:
                notes.append(f"已停止 {stopped} 个在途执行")
        elif reason_code == "user_request":
            notes.append("点名消息，跳过判定直接重新回答")
        elif reason_code == "judge_unavailable":
            notes.append("判定不可用，按失败策略重新回答")
        else:
            notes.append(f"判定要求重新回答（已停止 {max(0, stopped)} 个在途执行）")
        if reason_code == "hard_interrupt" and confidence is None:
            confidence = 100
        await self._write_judge_row(
            umo=umo, sender_id=sender_id, status=status, reason=reason_code,
            confidence=confidence, session=session, detail=detail, notes=notes,
            user_input=user_input or str(getattr(event, "message_str", "") or "")[:500])

    # ---- 判定流水（一条判定只有一行：判定中 → 已作废/已打断/中途作废/不重发） ----

    def _judge_detail(self, detail: str, *, reason: str, confidence: int | None,
                      notes: Iterable[str] = (), why: str = "") -> str:
        parts = [detail] if detail else []
        parts.extend(note for note in notes if note)
        label = JUDGE_REASON_LABEL.get(reason or "", "")
        if label:
            parts.append(f"原因={label}")
        if confidence is not None:
            parts.append(f"置信度 {confidence}")
        if why:
            parts.append(why)
        return "｜".join(parts)[:2000]

    async def _write_judge_row(self, *, umo: str, sender_id: str, status: str,
                               reason: str = "", confidence: int | None = None,
                               session: Any = None, detail: str = "",
                               notes: Iterable[str] = (), user_input: str = "") -> int:
        """写/就地改这条判定流水；判定模型给的原因与置信度随之落盘。"""
        why = str(getattr(session, "why", "") or "") if session is not None else ""
        merged_notes = list(notes)
        if session is not None:
            note = str(getattr(session, "verdict_note", "") or "")
            if note and note not in detail:
                merged_notes.insert(0, note)
        text = self._judge_detail(detail, reason=reason, confidence=confidence,
                                  notes=merged_notes, why=why)
        raw = ""
        if session is not None:
            dump = getattr(self.judge, "raw_output", None)
            if callable(dump):
                try:
                    raw = str(dump(session) or "")
                except Exception:  # noqa: BLE001 - 原始输出拿不到不影响状态
                    raw = ""
        row_id = int(getattr(session, "row_id", 0) or 0) if session is not None else 0
        if row_id:
            await self.update_log_status(row_id, status, detail=text,
                                         reason=reason or "", confidence=confidence,
                                         raw_output=raw or None)
        else:
            row_id = int(await self.log_event_row(
                kind="judge", umo=umo, sender_id=sender_id, result=status,
                reason=reason or "", confidence=confidence, detail=text,
                user_input=user_input, raw_output=raw) or 0)
        if row_id:
            self._judge_row[umo] = {"id": row_id, "status": status, "detail": text}
        return row_id

    async def _finish_judge_row(self, session: Any, status: str, *,
                                reason: str = "", confidence: int | None = None,
                                detail: str = "") -> None:
        """把判定行结算成最终状态（不重发 / 已作废 / 已打断）。"""
        await self._write_judge_row(
            umo=str(getattr(session, "umo", "") or ""),
            sender_id=str(getattr(session, "sender_id", "") or ""),
            status=status, reason=reason, confidence=confidence,
            session=session, detail=detail)

    async def _note_judge_row(self, umo: str, note: str,
                              status: str | None = None) -> None:
        """给已结算的判定行补一句说明（中途作废 / 旧回答没发出去）。"""
        info = self._judge_row.get(umo)
        if not info or not note:
            return
        detail = f"{info.get('detail') or ''}｜{note}" if info.get("detail") else note
        target = status or info.get("status") or JUDGE_RESULT_DISCARDED
        if await self.update_log_status(info["id"], target, detail=detail):
            info["status"] = target
            info["detail"] = detail

    def _mark_inflight(self, event: AstrMessageEvent, sender_id: str) -> None:
        """这条消息会真的让模型回答时，记下"谁的在途回答"（按轮次追加）。"""
        if not getattr(event, "is_at_or_wake_command", False):
            return
        stopped = getattr(event, "is_stopped", None)
        if callable(stopped) and stopped():
            return
        rounds = self.inflight.setdefault(event.unified_msg_origin, [])
        rounds.append({
            "event": event,
            "sender_id": str(sender_id or ""),
            "started_at": time.time(),
        })
        del rounds[:-4]

    def _split_segments(self, text: str, separator: str) -> list[str]:
        """按分隔符切段，但跳过"夹在 $ 公式里"的分隔符。

        规则：分隔符前后紧邻的字符不能是 `$`。这样
          - `写错$$$真诚建议`（正常分隔）→ 切
          - `$$a$$$$b$$`（两个公式相邻产生的 $$$）→ 不切
        """
        if not separator or separator not in text:
            return []
        pattern = "(?<!" + re.escape(chr(36)) + ")" + re.escape(separator) + "(?!" + re.escape(chr(36)) + ")"
        parts = re.split(pattern, text)
        return [part.strip() for part in parts if part.strip()]

    def segment_separator(self) -> str:
        """分句标识（模型输出用它分隔多段），面板可改，默认 $$$。"""
        value = self.cfg.str("segment_separator", "") or SEGMENT_SEPARATOR_DEFAULT
        return value[:16]

    def _segment_delay_seconds(self, text: str) -> float:
        """按配置算"这一段之后等多久"。x = 本段字数。

        - log：y = log_<base>(x) 秒（和 AstrBot 的 interval_method=log 一致）
        - linear：y = 初始值 + x / 增长率（增长率单位：字/秒）
        """
        import math

        chars = max(1, len(text))
        mode = self.cfg.option("segment_delay_mode", "log", ("log", "linear"))
        if mode == "linear":
            start_ms = self.cfg.int("segment_linear_start_ms", 800, 0, 30000)
            rate = self.cfg.int("segment_linear_rate", 10, 1, 200)
            delay = start_ms / 1000.0 + chars / float(rate)
        else:
            try:
                base = float(self.cfg.raw.get("segment_log_base", 2.6) or 2.6)
            except (TypeError, ValueError):
                base = 2.6
            base = max(1.01, min(10.0, base))
            delay = math.log(chars, base)
        return max(0.0, min(30.0, delay))

    async def _send_segmented(self, event: AstrMessageEvent) -> int:
        """按 $$$ 把回答拆成多条发送，段间延迟可配（对数 / 一次函数）。

        与"模型还没生成完"完全等同：每段发送前都会结算一次打断判定
        （硬打断当场已置 abort；弱打断在这里等判定结果：regen → 丢弃剩余，keep → 继续发）。
        """
        result = event.get_result()
        if result is None or not getattr(result, "chain", None):
            return 0
        text = self.gate._read_text(result.chain)
        separator = self.segment_separator()
        parts = self._split_segments(text, separator)
        if len(parts) < 2:
            return 0            # 没分段：不记流水（分句事件不进日志）

        umo = event.unified_msg_origin
        token = self._segment_token.get(umo, 0) + 1
        self._segment_token[umo] = token
        # 自己发，别让管线再把整段发一遍
        self.gate._clear(event, result)
        sent = 0
        aborted = False
        try:
            # 发送开始前就有强打断（回答已作废）→ 一段都不发
            if self._force_answer.get(umo):
                if not await self._settle_interrupt(
                        event, umo, str(event.get_sender_id() or "")):
                    pass
            for index, part in enumerate(parts):
                if self._segment_abort.get(umo) == token:
                    aborted = True
                    break
                try:
                    await event.send(MessageChain([Plain(text=part)]))
                    sent += 1
                except Exception as exc:  # noqa: BLE001 - 单段失败不影响后续
                    self.log_event(kind="error", umo=umo, result="failed",
                                   detail=f"segment_send:{type(exc).__name__}")
                    break
                if index == len(parts) - 1:
                    break
                # 段间等待：等待期间结算判定（弱）/检查 abort（强）
                await asyncio.sleep(self._segment_delay_seconds(part))
                if self._judge_sessions.get(umo) or self._force_answer.get(umo):
                    if not await self._settle_interrupt(
                            event, umo, str(event.get_sender_id() or "")):
                        pass    # keep：旧回答继续发完
                if self._segment_abort.get(umo) == token:
                    aborted = True
                    break
        finally:
            self._segment_abort.pop(umo, None)
            self._judge_sessions.pop(umo, None)
            self._force_answer.pop(umo, None)
        if aborted:
            # 已经发出去一部分：这条判定的状态改成「中途作废」
            await self._note_judge_row(
                umo, f"分句发送中途被打断：已发 {sent}/{len(parts)} 段，其余丢弃",
                status=JUDGE_RESULT_PARTIAL)
        return sent

    def _round_for_event(self, event: AstrMessageEvent) -> dict[str, Any] | None:
        for info in self.inflight.get(event.unified_msg_origin, []):
            if info.get("event") is event:
                return info
        return None

    def _should_suppress(self, event: AstrMessageEvent) -> bool:
        """这条事件对应的在途回答是否已被标记作废（弱打断）。"""
        for info in self.inflight.get(event.unified_msg_origin, []):
            if info.get("event") is event:
                return bool(info.get("discard"))
        return False

    def _drop_round(self, umo: str, round_info: dict[str, Any]) -> None:
        rounds = self.inflight.get(umo) or []
        if round_info in rounds:
            rounds.remove(round_info)
        if not rounds:
            self.inflight.pop(umo, None)

    def clear_inflight(self, event: AstrMessageEvent) -> None:
        """这一条的回答已经产出：只清掉它自己那一轮，别动更晚的在途回答。"""
        umo = event.unified_msg_origin
        rounds = self.inflight.get(umo) or []
        for index, info in enumerate(rounds):
            if info.get("event") is event:
                rounds.pop(index)
                break
        else:
            if rounds:
                rounds.pop(0)
        if not rounds:
            self.inflight.pop(umo, None)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: Any):
        """每次 LLM 请求前注入「沉默机制」提示词块。

        不再需要把模板粘进人格设定：这里直接改 req.system_prompt，
        人格里已存在等价说明时自动跳过（幂等），不会重复叠加。
        """
        try:
            if not self.cfg.enabled or not self.cfg.bool("prompt_block_inject", True):
                return
            block, _custom = self.prompt_block()
            base = getattr(req, "system_prompt", "") or ""
            note = ""
            try:
                note = str(event.get_extra("replygate_unanswered_note") or "")
            except Exception:  # noqa: BLE001
                note = ""
            if note:
                base = f"{base.rstrip()}\n\n{note}" if base.strip() else note
            merged = prompts.append_block(base, block)
            if merged is not None:
                req.system_prompt = merged
        except Exception as exc:  # noqa: BLE001 - 注入失败绝不能影响正常回复
            self.log_event(kind="error",
                           umo=getattr(event, "unified_msg_origin", ""),
                           result="failed", detail=f"prompt_block:{type(exc).__name__}")

    @filter.on_decorating_result(priority=-maxsize)
    async def on_outbound(self, event: AstrMessageEvent):
        """回答已生成、尚未发送：出站闸门。"""
        try:
            if not self.cfg.enabled:
                return
            round_info = self._round_for_event(event)
            if round_info is not None and round_info.get("judge") is not None:
                # 主模型已生成、判定还没出结果 → 压住这条回答，等判定再决定发不发
                session = round_info["judge"]
                if not session.done:
                    await self.judge.await_verdict(event.unified_msg_origin)
                threshold = self.cfg.int("soft_judge_threshold", 60, 0, 100)
                if session.score is None:
                    # 判定不可用：按失败策略
                    fail = self.cfg.option("soft_judge_fail_mode", VERDICT_REGEN,
                                           (VERDICT_REGEN, VERDICT_KEEP))
                    if fail == VERDICT_REGEN:
                        round_info["discard"] = True
                elif session.score > threshold:
                    round_info["discard"] = True
            if self._should_suppress(event):
                self.gate.suppress(event)
                await self._note_judge_row(
                    event.unified_msg_origin,
                    "旧回答已生成但被丢弃，没有发出")
                self.clear_inflight(event)
                self._reset_interrupt_window(event.unified_msg_origin)
                return
            if self.cfg.bool("segment_control_enabled", False):
                # 自己接管分句：闸门先解析标记（沉默/暂存），剩下的正文逐段发送
                await self.gate.handle(event)
                if not event.is_stopped():
                    await self._send_segmented(event)
                self.clear_inflight(event)
                self._reset_interrupt_window(event.unified_msg_origin)
                return
            self.clear_inflight(event)
            await self.gate.handle(event)
            self._reset_interrupt_window(event.unified_msg_origin)
        except Exception as exc:  # noqa: BLE001 - 闸门异常一律放行原文
            self.log_event(kind="error", umo=event.unified_msg_origin,
                           result="failed", detail=f"outbound:{type(exc).__name__}")