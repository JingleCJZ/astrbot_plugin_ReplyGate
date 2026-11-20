"""弱打断的判定模型（soft judge）。

用途：机器人正在生成回答时，用户又发了一条消息。
      先让一个小模型打分（0-100，越高越该重答），再决定：

        score > 阈值 → 丢弃旧回答（并取消它），让新消息重新回答
        score ≤ 阈值 → 保留旧回答，新消息不再单独回答

时序（三个角色）：

  旧事件（主模型）          新事件（触发消息）            判定任务（本模块）
  ─────────────────          ─────────────────            ─────────────────
  on_decorating_result ──┐   on_inbound ──┐               start() 起一个 asyncio 任务
    await_verdict()      │     await_verdict()            │  调小模型打分（一次只调一次）
                         └────────► 同一个 verdict ◄──────┘
  回答已生成但**先发出**，等判定；判定期间新消息到达 → 取消并重判。

超时语义（重要）：
  - `interrupt_window_ms` 是**激活窗口**：从"第一条打断消息"开始计时，窗口内到达的消息
    都会进入判定；窗口结束后不再接受新的判定触发，但**已经在跑的判定结果仍然有效**，
    回答照样要等它（由 main 侧的 on_outbound 负责等待）。
  - 本模块的 `soft_judge_timeout_ms` 只是"单次请求超时"：provider 调用多久没返回就按
    失败策略收尾，避免一条回答永远压着发不出去。
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

VERDICT_REGEN = "regen"
VERDICT_KEEP = "keep"
VERDICT_NONE = "none"
VERDICT_RESTARTED = "restarted"

if __package__:
    from .models import JUDGE_REASONS, JUDGE_RESULT_JUDGING
else:  # 单文件调试
    from core.models import JUDGE_REASONS, JUDGE_RESULT_JUDGING

JUDGE_SYSTEM_PROMPT = (
    "你是严格的判定器。只输出一行 JSON："
    '{"score": 0-100, "reason": "user_request|requirement_change|info_supplement", '
    '"why": "不超过30字"}。'
    "reason 只能取这三个值之一：user_request=用户要求、"
    "requirement_change=用户需求取消/改变、info_supplement=用户补充信息。"
    "score 越高表示越应该放弃当前这条回答、重新回答；why 用一句中文说明。"
    "不要解释、不要代码块、不要其它文字。"
)

# 模型可能直接写中文或换成写法，统一归一化成三个原因码
_REASON_ALIASES = {
    "user_request": "user_request",
    "用户要求": "user_request",
    "用户明确要求": "user_request",
    "requirement_change": "requirement_change",
    "需求取消/改变": "requirement_change",
    "需求改变": "requirement_change",
    "需求取消": "requirement_change",
    "用户需求取消/改变": "requirement_change",
    "改变需求": "requirement_change",
    "info_supplement": "info_supplement",
    "用户补充信息": "info_supplement",
    "补充信息": "info_supplement",
    "补充": "info_supplement",
    "added_info": "info_supplement",
}

_SCORE_JSON = re.compile(r"\{[^{}]*\}")
_SCORE_NUM = re.compile(r"(?<!\d)(100|\d{1,2})(?!\d)")

# 面板可选的思考等级 -> 服务商 reasoning_effort
THINKING_LEVELS = {
    "off": "none",
    "minimal": "minimal",
    "low": "low",
}


def parse_score(text: str) -> int | None:
    """从模型输出里取 0-100 的分数；取不到返回 None。"""
    score, _reason, _why = parse_verdict(text)
    return score


def normalize_reason(raw: Any) -> str:
    """把模型给的原因归一化成 JUDGE_REASONS 里的一个码；不认识就返回空串。"""
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    if text in _REASON_ALIASES:
        return _REASON_ALIASES[text]
    for key, code in _REASON_ALIASES.items():
        if key in text:
            return code
    return ""


def parse_verdict(text: str) -> tuple[int | None, str, str]:
    """取 (分数, 原因码, 模型给的理由)。原因码只可能是 JUDGE_REASONS 里的值。"""
    raw = (text or "").strip()
    if not raw:
        return None, "", ""
    for candidate in _SCORE_JSON.findall(raw):
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if not isinstance(data, dict) or "score" not in data:
            continue
        try:
            value = int(float(data["score"]))
        except (TypeError, ValueError):
            continue
        reason = normalize_reason(data.get("reason") or data.get("why_reason"))
        if reason not in JUDGE_REASONS:
            reason = ""
        return max(0, min(100, value)), reason, str(data.get("why") or "").strip()[:60]
    lowered = raw.lower()
    if "score" in lowered:
        tail = lowered.split("score", 1)[1]
        match = _SCORE_NUM.search(tail)
        if match:
            return max(0, min(100, int(match.group(1)))), "", ""
    match = _SCORE_NUM.search(raw)
    if match:
        return max(0, min(100, int(match.group(1)))), "", ""
    return None, "", ""


@dataclass
class JudgeSession:
    umo: str
    seq: int
    window: list[str]
    started_at: float
    draft: str = ""
    restarts: int = 0
    task: asyncio.Task | None = None
    score: int | None = None
    reason: str = ""
    why: str = ""
    raw: str = ""
    reasoning: str = ""
    verdict: str = ""
    detail: str = ""          # 触发细节（谁又发了消息、在途多久）
    verdict_note: str = ""    # 判定结果概况（score/阈值 或 失败原因）
    done: bool = False
    # 流水：一条判定占一行，先写「判定中」，结算时就地改状态
    row_id: int = 0
    sender_id: str = ""
    trigger: str = ""


class SoftJudge:
    """按会话维护"正在进行的判定"。同一会话同时只有一个有效判定。"""

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self.sessions: dict[str, JudgeSession] = {}
        self._seq = 0

    # ---------------- 配置 ----------------

    @property
    def cfg(self) -> Any:
        return self.plugin.cfg

    def threshold(self) -> int:
        return self.cfg.int("soft_judge_threshold", 60, 0, 100)

    def timeout_seconds(self) -> float:
        return self.cfg.int("soft_judge_timeout_ms", 4000, 500, 60000) / 1000.0

    def max_restarts(self) -> int:
        return self.cfg.int("soft_judge_max_restarts", 3, 0, 20)

    def max_tokens(self) -> int:
        return self.cfg.int("soft_judge_max_tokens", 256, 16, 1024)

    def fail_mode(self) -> str:
        return self.cfg.option("soft_judge_fail_mode", VERDICT_REGEN,
                               (VERDICT_REGEN, VERDICT_KEEP))

    def provider_id(self) -> str:
        return (self.cfg.str("soft_judge_provider_id", "")
                or self.cfg.str("group_judge_provider_id", ""))

    def thinking_level(self) -> str:
        return self.cfg.option("soft_judge_thinking_level", "off",
                               tuple(THINKING_LEVELS))

    # ---------------- 会话管理 ----------------

    def get(self, umo: str) -> JudgeSession | None:
        session = self.sessions.get(umo)
        if session is not None and session.done:
            return None
        return session

    def start(self, umo: str, window: list[str],
              draft: str = "", sender_id: str = "", detail: str = "",
              trigger: str = "") -> JudgeSession | None:
        """开一次判定。

        - 已有未完成的判定 → 取消并重判（"立刻中断判定模型，并重新判定"），
          但**流水仍用同一行**（一条判定只有一行，状态一直是「判定中」）；
        - 重启次数超过上限 → **保留当前判定**，本次触发忽略（不抛弃结果）。
        """
        prev = self.sessions.get(umo)
        row_id = 0
        if prev is not None and not prev.done:
            if prev.restarts >= self.max_restarts():
                return prev          # 不再重判，等手里这次出结果
            if prev.task is not None and not prev.task.done():
                prev.task.cancel()
            prev.verdict = VERDICT_RESTARTED
            prev.detail = "被新消息打断，改用最新窗口重判"
            prev.done = True
            row_id = prev.row_id
            restarts = prev.restarts + 1
        else:
            restarts = 0

        self._seq += 1
        session = JudgeSession(
            umo=umo,
            seq=self._seq,
            window=list(window),
            started_at=time.monotonic(),
            draft=draft,
            restarts=restarts,
            row_id=row_id,
            sender_id=str(sender_id or ""),
            trigger=trigger,
        )
        session.detail = detail
        self.sessions[umo] = session
        session.task = asyncio.ensure_future(self._run(session))
        return session

    async def _run(self, session: JudgeSession) -> None:
        # 先把这一行写出去（状态 = 判定中），结算时由 main 就地改状态
        try:
            await self._open_row(session)
        except Exception:  # noqa: BLE001 - 流水失败绝不能让判定卡死
            pass
        score: int | None = None
        reason = ""
        why = ""
        raw = ""
        reasoning = ""
        note = ""
        try:
            score, reason, why, raw, reasoning, note = await self._ask(session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 判定失败按策略收尾
            raw = f"{type(exc).__name__}: {exc}"
        finally:
            if session.done and session.verdict == VERDICT_RESTARTED:
                return
            session.score = score
            session.reason = reason
            session.why = why
            session.raw = raw
            session.reasoning = reasoning
            if score is None:
                session.verdict = self.fail_mode()
                session.verdict_note = raw or "判定模型无有效输出"
            else:
                threshold = self.threshold()
                session.verdict = VERDICT_REGEN if score > threshold else VERDICT_KEEP
                session.verdict_note = f"score={score} 阈值={threshold}"
                if note:
                    session.verdict_note += f"｜{note}"
            session.done = True
            await self._refresh_row(session)

    def row_detail(self, session: JudgeSession) -> str:
        """这一行当前的说明文字（触发细节 + 判定概况）。"""
        parts = [part for part in (session.detail, session.verdict_note) if part]
        if session.restarts:
            parts.append(f"第 {session.restarts} 次重判")
        return "｜".join(parts)

    async def _refresh_row(self, session: JudgeSession) -> None:
        """判定出结果但还没结算时，把分数/失败原因先补到这一行上（状态仍是判定中）。"""
        updater = getattr(self.plugin, "update_log_status", None)
        if not session.row_id or not callable(updater):
            return
        try:
            await updater(session.row_id, JUDGE_RESULT_JUDGING,
                          detail=self.row_detail(session))
        except Exception:  # noqa: BLE001 - 流水失败不影响判定
            pass

    async def _open_row(self, session: JudgeSession) -> None:
        """写（或刷新）这条判定的流水行；一条判定自始至终只有一行。"""
        plugin = self.plugin
        detail = self.row_detail(session) or "判定模型评估是否需要重新回答"
        user_input = "\n".join(session.window)[-2000:]
        if session.row_id:
            updater = getattr(plugin, "update_log_status", None)
            if callable(updater):
                try:
                    await updater(session.row_id, JUDGE_RESULT_JUDGING, detail=detail)
                except Exception:  # noqa: BLE001 - 流水失败不影响判定
                    pass
            return
        creator = getattr(plugin, "log_event_row", None)
        if not callable(creator):
            return
        try:
            session.row_id = int(await creator(
                kind="judge", umo=session.umo, sender_id=session.sender_id,
                result=JUDGE_RESULT_JUDGING, detail=detail,
                user_input=user_input) or 0)
        except Exception:  # noqa: BLE001
            session.row_id = 0

    def raw_output(self, session: JudgeSession) -> str:
        """判定行的「模型原始输出」（含思考），供 main 结算时一并写入。"""
        raw = session.raw
        if session.reasoning:
            raw = f"[思考]\n{session.reasoning}\n\n[输出]\n{raw or '（空）'}"
        return raw[:4000]

    async def _ask(self, session: JudgeSession
                 ) -> tuple[int | None, str, str, str, str, str]:
        provider = self._provider_for_judge(session.umo)
        if provider is None:
            return None, "", "", "未配置判定模型（soft_judge_provider_id）", "", ""
        builder = getattr(self.plugin, "judge_prompt_for", None)
        prompt = (builder(session.window) if callable(builder)
                  else "\n".join(session.window))
        timeout = self.timeout_seconds()
        note = ""
        try:
            response = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, system_prompt=JUDGE_SYSTEM_PROMPT,
                                   contexts=[]),
                timeout=timeout)
        except asyncio.TimeoutError:
            raise
        except Exception:  # noqa: BLE001 - 可能是不支持 reasoning_effort/max_tokens
            plain = self._resolve_provider(session.umo)
            if plain is None or plain is provider:
                raise
            # 服务商拒绝加速参数时，退回不带覆盖的请求再试一次
            response = await asyncio.wait_for(
                plain.text_chat(prompt=prompt, system_prompt=JUDGE_SYSTEM_PROMPT,
                                contexts=[]),
                timeout=timeout)
            note = "已退回不带 reasoning_effort/max_tokens 的请求"
        text = ""
        reasoning = ""
        if response is not None:
            text = getattr(response, "completion_text", "") or ""
            reasoning = getattr(response, "reasoning_content", "") or ""
        score, reason, why = parse_verdict(text)
        return score, reason, why, text.strip(), reasoning.strip(), note

    def _provider_for_judge(self, umo: str) -> Any:
        """取判定 provider；顺手把它调快：限制输出长度、压低思考等级。

        AstrBot 的 OpenAI 兼容层不支持按次传 reasoning_effort / max_tokens，
        只能通过 provider_config["custom_extra_body"] 生效。这里**浅拷贝**一个
        provider 实例再改它的 config，避免影响并行的主模型请求。
        """
        provider = self._resolve_provider(umo)
        if provider is None:
            return None
        level = THINKING_LEVELS.get(self.thinking_level())
        try:
            clone = copy.copy(provider)
            config = dict(getattr(provider, "provider_config", {}) or {})
            extra = dict(config.get("custom_extra_body") or {})
            extra["max_tokens"] = self.max_tokens()
            if level:
                extra["reasoning_effort"] = level
            config["custom_extra_body"] = extra
            clone.provider_config = config
            return clone
        except Exception:  # noqa: BLE001 - 拷不出来就用原实例
            return provider

    def _resolve_provider(self, umo: str) -> Any:
        context = getattr(self.plugin, "context", None)
        if context is None:
            return None
        pid = self.provider_id()
        try:
            if pid:
                return context.get_provider_by_id(pid)
            return context.get_using_provider(umo)
        except Exception:  # noqa: BLE001 - provider 解析失败按"不可用"处理
            return None

    # ---------------- 等待结果 ----------------

    async def await_verdict(self, umo: str) -> str:
        """等到当前有效判定出结果；被重启就继续等最新那次（不设总超时）。"""
        while True:
            session = self.sessions.get(umo)
            if session is None:
                return VERDICT_NONE
            if session.done:
                if session.verdict == VERDICT_RESTARTED:
                    await asyncio.sleep(0.02)
                    continue
                return session.verdict
            await asyncio.sleep(0.05)

    def cancel(self, umo: str) -> None:
        session = self.sessions.pop(umo, None)
        if session and session.task and not session.task.done():
            session.task.cancel()

    def clear(self) -> None:
        for umo in list(self.sessions):
            self.cancel(umo)
        self.sessions.clear()