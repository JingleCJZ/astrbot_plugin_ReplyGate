"""出站闸门：模型回答已经生成、还没发送之前的最后一道关。

职责：
  SILENCE  丢弃正文（永久沉默）
  HOLD     暂存正文 wait 秒：期间被新消息打断 → 丢弃；否则按时发送
           两种用途共用同一机制：
             waiting_explanation —— 正文是"对图片的回答"，等用户解释
             waiting_content     —— 正文是"催促语"，等用户补发内容
  回收     被丢弃的**完整**正文存成下一轮的参考草稿

不在本模块做的事：群聊判定、打断在途生成。
本模块拿到的永远是完全生成好的文本，所以回收在这里有效；
生成中被取消的回答拿不到中间文本，无法回收。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain

if __package__:
    from .markers import describe_error, parse_output
    from .models import (HOLD_SCOPE_DEFAULT, HOLD_SCOPE_KEY, REASON_LABEL,
                         REASON_SWITCH, Marker)
else:  # 单文件调试
    from core.markers import describe_error, parse_output
    from core.models import (HOLD_SCOPE_DEFAULT, HOLD_SCOPE_KEY, REASON_LABEL,
                             REASON_SWITCH, Marker)

RESULT_SENT = "sent"
RESULT_DISCARDED = "discarded"
RESULT_HELD = "held"
RESULT_CANCELLED = "cancelled"
RESULT_FAILED = "failed"
RESULT_PENDING = "pending"
RESULT_DRY_RUN = "dry_run"
RESULT_SWITCH_OFF = "switch_off"


class HoldSession:
    """一次 HOLD 等待。"""

    def __init__(self, *, umo: str, sender_id: str, reason: str, scope: str,
                 text: str, seconds: int):
        self.umo = umo
        self.sender_id = str(sender_id or "")
        self.reason = reason
        self.scope = scope
        self.text = text
        self.seconds = seconds
        self.started_at = time.monotonic()
        self.cancelled = False
        self.cancel_reason = ""
        self.row_id = 0          # 那条“等待中”流水的 id，解决后就地改状态

    def matches(self, event: AstrMessageEvent) -> bool:
        if event.unified_msg_origin != self.umo:
            return False
        if self.scope == "target":
            return str(event.get_sender_id() or "") == self.sender_id
        return True

    def cancel(self, reason: str) -> None:
        self.cancelled = True
        self.cancel_reason = reason


class OutboundGate:
    def __init__(self, plugin: Any):
        self.plugin = plugin
        self.holds: dict[str, HoldSession] = {}
        # 回收池：umo -> 被丢弃的完整正文（只存一份，新的覆盖旧的）
        self.recycled: dict[str, dict[str, Any]] = {}

    @property
    def config(self):
        return self.plugin.cfg

    def _log(self, **row) -> None:
        row.setdefault("user_input", getattr(self, "_user", ""))
        row.setdefault("raw_output", getattr(self, "_raw", ""))
        self.plugin.log_event(**row)

    async def _log_pending(self, **row) -> int:
        """写一条“等待中”流水并拿到 id，解决后原地改状态，不再另插一行。"""
        row.setdefault("user_input", getattr(self, "_user", ""))
        row.setdefault("raw_output", getattr(self, "_raw", ""))
        writer = getattr(self.plugin, "log_event_row", None)
        if callable(writer):
            try:
                return await writer(**row)
            except Exception:  # noqa: BLE001 - 落库失败不影响收发
                return 0
        self.plugin.log_event(**row)
        return 0

    # ---------------- 文本读写 ----------------

    @staticmethod
    def _plain_parts(chain: list[Any]) -> list[Plain]:
        return [comp for comp in (chain or []) if isinstance(comp, Plain)]

    def _read_text(self, chain: list[Any]) -> str:
        return "".join(comp.text for comp in self._plain_parts(chain) if comp.text)

    def _write_text(self, chain: list[Any], text: str) -> None:
        """把整段文本写回 Plain 组件，保留 At/Image 等组件位置。"""
        parts = self._plain_parts(chain)
        if not parts:
            if text:
                chain.append(Plain(text=text))
            return
        parts[0].text = text or ""
        for comp in parts[1:]:
            comp.text = ""

    @staticmethod
    def _user_input(event: AstrMessageEvent) -> str:
        """触发这条回答的用户输入，用于流水详情回看。"""
        try:
            return str(getattr(event, "message_str", "") or "").strip()[:2000]
        except Exception:  # noqa: BLE001
            return ""

    def suppress(self, event: AstrMessageEvent, reason: str = "interrupted") -> bool:
        """弱打断：回答已经生成，但按“作废”处理——不发送，正文进回收池。"""
        result = event.get_result()
        if result is None or not getattr(result, "chain", None):
            return False
        text = self._read_text(result.chain)
        self.recycle(event.unified_msg_origin, text, reason, f"suppress:{reason}")
        self._clear(event, result)
        return True

    def _clear(self, event: AstrMessageEvent, result: Any) -> None:
        result.chain = []
        if hasattr(result, "async_stream"):
            result.async_stream = None
        event.stop_event()

    # ---------------- 回收池 ----------------

    def recycle(self, umo: str, text: str, reason: str, source: str) -> None:
        if reason == "waiting_content":
            # 催促语没有回收价值
            return
        if not self.config.bool("recycle_discarded_answer", True):
            return
        limit = self.config.int("recycle_max_chars", 600, 0, 4000)
        cleaned = (text or "").strip()
        if not cleaned or limit <= 0:
            return
        self.recycled[umo] = {
            "text": cleaned[:limit],
            "reason": reason,
            "source": source,
            "at": time.time(),
        }

    def take_recycled(self, umo: str) -> dict[str, Any] | None:
        return self.recycled.pop(umo, None)

    # ---------------- 主流程 ----------------

    async def handle(self, event: AstrMessageEvent) -> None:
        if not self.config.enabled or not self.config.silence_enabled:
            return
        result = event.get_result()
        if result is None or not getattr(result, "chain", None):
            return
        if getattr(result, "async_stream", None) is not None:
            self._log(kind="error", umo=event.unified_msg_origin,
                      sender_id=str(event.get_sender_id() or ""),
                      result=RESULT_FAILED, detail="streaming_output_skipped")
            return
        if event.is_private_chat() and not self.config.bool("silence_private_enabled", True):
            return

        chain = result.chain
        text = self._read_text(chain)
        if not text.strip():
            return
        self._user = self._user_input(event)   # 流水里回看的“用户输入”
        self._raw = text                       # 模型原始输出（含标记）

        parsed = parse_output(
            text,
            silence_marker=self.config.str("silence_marker", "%%SILENCE%%") or "%%SILENCE%%",
            hold_marker=self.config.str("hold_marker", "%%HOLD%%") or "%%HOLD%%",
            hold_max_wait_seconds=self.config.int("hold_max_wait_seconds", 30, 1, 180),
            hold_default_wait_seconds=self.config.int("hold_default_wait_seconds", 8, 1, 180),
            followup_max_chars=self.config.int("silence_followup_max_chars", 200, 10, 1000),
        )
        if parsed.parse_error:
            self._log(kind="error", umo=event.unified_msg_origin,
                      sender_id=str(event.get_sender_id() or ""),
                      result=RESULT_FAILED, detail=parsed.parse_error,
                      reason=describe_error(parsed.parse_error))
            self._write_text(chain, parsed.text)   # 标记行剥掉，正文照发
            return

        marker = parsed.marker
        if marker is None:
            return
        self._write_text(chain, parsed.text)       # 标记行不属于正文

        # 仅观察：只剥标记，别的一概不做（不丢弃、不暂存、不打断、不回收）。
        # 这是唯一一处 dry_run 分支，保证不会从别处漏出副作用。
        if self.config.dry_run:
            self._observe(event, result, marker, parsed.text)
            return

        if marker.is_hold:
            await self._handle_hold(event, result, marker, parsed.text)
        else:
            await self._handle_silence(event, result, marker, parsed.text)

    # ---------------- 仅观察 ----------------

    def _would_do(self, marker: Marker) -> str:
        """描述“关掉仅观察后会发生什么”，只用于流水说明。"""
        switch = REASON_SWITCH.get(marker.reason, "")
        if switch and not self.config.reason_allowed(switch):
            return "放行（该原因未启用）"
        if marker.is_hold:
            return f"暂存 {marker.wait}s 后发送"
        if marker.reason == "consent_trap" and self.config.bool("consent_trap_refuse_reply", False):
            return "放行（抗议改为回一句拒绝）"
        return "丢弃正文"

    def _observe(self, event: AstrMessageEvent, result: Any,
                 marker: Marker, body: str) -> None:
        """仅观察模式：正文（已剥标记）照常发送，只记一条观察流水。

        模型可能只输出标记、不写正文（提示词块就是这么要求的）；剥完没有正文
        就没有可发的东西，直接跳过发送，不做其它任何处理。
        """
        umo = event.unified_msg_origin
        sender_id = str(event.get_sender_id() or "")
        label = REASON_LABEL.get(marker.reason, marker.reason)
        if not body.strip():
            self._clear(event, result)
            self._log(kind=marker.kind, umo=umo, sender_id=sender_id,
                      reason=marker.reason, act=marker.kind,
                      wait_seconds=marker.wait or None, result=RESULT_DRY_RUN,
                      detail=f"{label}｜仅观察：标记外无正文，本轮不发")
            return
        self._log(kind=marker.kind, umo=umo, sender_id=sender_id,
                  reason=marker.reason, act=marker.kind,
                  wait_seconds=marker.wait or None, result=RESULT_DRY_RUN,
                  detail=f"{label}｜仅观察：正文照发（实际执行会{self._would_do(marker)}）")

    # ---------------- SILENCE ----------------

    async def _handle_silence(self, event: AstrMessageEvent, result: Any,
                             marker: Marker, body: str) -> None:
        umo = event.unified_msg_origin
        sender_id = str(event.get_sender_id() or "")
        reason = marker.reason
        switch = REASON_SWITCH.get(reason, "")

        if switch and not self.config.reason_allowed(switch):
            self._log(kind="silence", umo=umo, sender_id=sender_id, reason=reason,
                      result=RESULT_SWITCH_OFF, detail="该沉默原因未启用，正文照发")
            return
        if reason == "consent_trap" and self.config.bool("consent_trap_refuse_reply", False):
            self._log(kind="silence", umo=umo, sender_id=sender_id, reason=reason,
                      result="refuse_passthrough",
                      detail="抗议改为回一句明确拒绝，正文照发")
            return

        self.recycle(umo, body, reason, "silence")
        self._clear(event, result)
        self._log(kind="silence", umo=umo, sender_id=sender_id, reason=reason,
                  act="silence", result=RESULT_DISCARDED,
                  detail=REASON_LABEL.get(reason, reason)
                         + (f"｜{marker.memo}" if marker.memo else ""))

    # ---------------- HOLD ----------------

    async def _handle_hold(self, event: AstrMessageEvent, result: Any,
                           marker: Marker, body: str) -> None:
        umo = event.unified_msg_origin
        sender_id = str(event.get_sender_id() or "")
        reason = marker.reason
        switch = REASON_SWITCH.get(reason, "")

        if switch and not self.config.reason_allowed(switch):
            self._log(kind="hold", umo=umo, sender_id=sender_id, reason=reason,
                      result=RESULT_SWITCH_OFF, wait_seconds=marker.wait,
                      detail="该等待原因未启用，正文立即发送")
            return
        if not body.strip():
            self._log(kind="hold", umo=umo, sender_id=sender_id, reason=reason,
                      result=RESULT_FAILED, detail="HOLD 无可暂存正文")
            return
        scope = self.config.option(
            HOLD_SCOPE_KEY.get(reason, "hold_cancel_scope_waiting_explanation"),
            HOLD_SCOPE_DEFAULT.get(reason, "target"), ("any", "target"))

        # 仅观察已在 handle() 统一拦下，这里只跑实际执行路径
        session = HoldSession(umo=umo, sender_id=sender_id, reason=reason,
                              scope=scope, text=body, seconds=marker.wait)
        self.holds[umo] = session
        scope_label = "任何人开口" if scope == "any" else "本人开口"
        session.row_id = await self._log_pending(
            kind="hold", umo=umo, sender_id=sender_id, reason=reason,
            act="hold", wait_seconds=marker.wait, result=RESULT_PENDING,
            detail=f"{REASON_LABEL.get(reason, reason)}｜等待 {marker.wait}s"
                   f"（{scope_label}即撤销）")

        deadline = time.monotonic() + marker.wait
        while not session.cancelled:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.2, remaining))
        if self.holds.get(umo) is session:
            self.holds.pop(umo, None)

        if session.cancelled:
            self.recycle(umo, body, reason, f"hold:{session.cancel_reason}")
            self._clear(event, result)
            await self._resolve(session, RESULT_CANCELLED,
                                f"被新消息打断（{session.cancel_reason}）")
            return
        await self._resolve(session, RESULT_SENT,
                            f"等待 {marker.wait}s 结束，暂存正文按时发送")

    async def _resolve(self, session: HoldSession, result: str, detail: str) -> None:
        """把“等待中”那条流水改成本次结果；没有 id 时退化为补记一行。"""
        if session.row_id:
            updated = await self.plugin.update_log_status(session.row_id, result, detail)
            if updated:
                return
        self._log(kind="hold", umo=session.umo, sender_id=session.sender_id,
                  reason=session.reason, act="hold",
                  wait_seconds=session.seconds, result=result, detail=detail)

    # ---------------- 被新消息打断 ----------------

    def on_message(self, event: AstrMessageEvent) -> None:
        """任何新消息到达：打断匹配的 HOLD。不参与唤醒判定。"""
        session = self.holds.get(event.unified_msg_origin)
        if session is None or session.cancelled:
            return
        if not session.matches(event):
            return
        session.cancel(f"新消息:{event.get_sender_id()}")
        logger.info("[ReplyGate] HOLD 被新消息打断：%s", event.unified_msg_origin)

    def shutdown(self) -> None:
        for session in list(self.holds.values()):
            session.cancel("插件停止")
        self.holds.clear()
        self.recycled.clear()


__all__ = ["OutboundGate", "HoldSession", "MessageChain"]