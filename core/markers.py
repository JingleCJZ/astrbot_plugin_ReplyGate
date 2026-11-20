"""从模型输出里解析尾部控制标记。

两个标记，语义严格分开：

    %%SILENCE%% {"reason": ...}
        丢弃正文，永久不出声（再见/抗议/无价值/已回答过/用户想安静）。
        没有任何后续发送，所以不需要 wait。

    %%HOLD%% {"reason": ..., "wait": N}
        保留正文，先暂存 N 秒：
          - waiting_explanation：正文是"对图片的回答"，等用户补充解释；
            用户解释了就丢弃正文重新回答，不解释就按时发出。
          - waiting_content：正文是"催促语"，等用户补发内容；
            等待期内收到新消息就撤销催促，不发了。
        超时后按原正文发送（保留 AstrBot 的分段与对数延迟）。

设计原则：
1. 标记必须位于行首，且其后内容（去围栏）整体是一个 JSON —— 正文里顺口提到
   标记不会误伤；同时兼容「标记单独一行 + 下一行 JSON」「标记与 JSON 同行」。
2. 任何解析异常都不吞消息 —— 返回原文 + parse_error，由调用方决定照发。
3. 不修补坏 JSON（会掩盖人格提示词问题），只记录原因码。
"""

from __future__ import annotations

import json
import re
from typing import Any

from .models import ALL_REASONS, HOLD_REASONS, SILENCE_REASONS, Marker, ParsedOutput

DEFAULT_SILENCE_MARKER = "%%SILENCE%%"
DEFAULT_HOLD_MARKER = "%%HOLD%%"

_TRAILING_WS = " \t\u3000\r\n"
_NEWLINE = "\n"


def _strip_json_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _locate_marker(text: str, markers: tuple[tuple[str, str], ...]) -> tuple[str, str, str]:
    """定位最后一个标记：返回 (正文, 标记类型, 标记之后的载荷)。"""
    best: tuple[int, int, str, int] | None = None
    for marker, kind in markers:
        marker = (marker or "").strip()
        if not marker:
            continue
        index = text.rfind(marker)
        if index < 0:
            continue
        line_start = text.rfind(_NEWLINE, 0, index) + 1
        prefix = text[line_start:index]
        if prefix.strip(_TRAILING_WS):
            continue            # 标记不在行首（同一行前面还有正文），不算标记
        if best is None or index > best[0]:
            best = (index, line_start, kind, len(marker))
    if best is None:
        return text, "", ""
    index, line_start, kind, length = best
    body = text[:line_start].rstrip(_TRAILING_WS)
    payload = text[index + length:]
    return body, kind, payload


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    if high > 0:
        number = min(number, high)
    return max(low, number)


def _normalize_reason(value: Any, kind: str) -> str:
    reason = str(value or "").strip().lower()
    allowed = SILENCE_REASONS if kind == "silence" else HOLD_REASONS
    return reason if reason in allowed else ""


def parse_output(
    text: str,
    *,
    silence_marker: str = DEFAULT_SILENCE_MARKER,
    hold_marker: str = DEFAULT_HOLD_MARKER,
    hold_max_wait_seconds: int = 30,
    hold_default_wait_seconds: int = 8,
    followup_max_chars: int = 200,
) -> ParsedOutput:
    """解析模型输出。始终返回可发送的正文（可能等于原文）。"""
    if not isinstance(text, str) or not text.strip():
        return ParsedOutput(text=text or "")

    body, kind, payload_raw = _locate_marker(
        text, ((silence_marker, "silence"), (hold_marker, "hold")))
    if not kind:
        return ParsedOutput(text=text)

    payload = _strip_json_fence(payload_raw)
    if not payload:
        return ParsedOutput(text=body, parse_error="marker_without_payload")

    try:
        data = json.loads(payload)
    except ValueError:
        looks_like_json = payload.startswith("{")
        if not looks_like_json:
            # 标记被正文提到，后面不是 JSON：按原文放行，不做任何删改
            return ParsedOutput(text=text)
        return ParsedOutput(text=body, parse_error="marker_bad_json")
    if not isinstance(data, dict):
        return ParsedOutput(text=body, parse_error="marker_not_object")

    reason = _normalize_reason(data.get("reason"), kind)
    if not reason:
        return ParsedOutput(text=body, parse_error="marker_bad_reason")

    memo = str(data.get("memo") or "")[:200]

    if kind == "silence":
        # 真沉默：正文丢弃，也没有任何后续发送
        return ParsedOutput(
            text=body,
            marker=Marker(kind="silence", reason=reason, wait=0, memo=memo,
                          raw_json=payload))

    if not body.strip():
        return ParsedOutput(text="", parse_error="hold_without_body")
    wait = _clamp_int(data.get("wait", hold_default_wait_seconds), 1,
                      hold_max_wait_seconds or 1, hold_default_wait_seconds)
    kept = body
    if reason == "waiting_content":
        # 催促语不该长篇大论；回答类正文（waiting_explanation）不截断
        kept = kept[: max(10, int(followup_max_chars))].strip()
    return ParsedOutput(
        text=kept,
        marker=Marker(kind="hold", reason=reason, wait=wait, memo=memo,
                      raw_json=payload))


def describe_error(code: str) -> str:
    return {
        "marker_without_payload": "标记行没有 JSON 内容",
        "marker_bad_json": "标记后的 JSON 无法解析",
        "marker_not_object": "标记后的 JSON 不是对象",
        "marker_bad_reason": "reason 不在该标记允许的列表内",
        "hold_without_body": "HOLD 标记缺少可暂存的正文",
    }.get(code, code)


def is_known_reason(reason: str) -> bool:
    return reason in ALL_REASONS