"""命令消息判定：`/` 开头的消息不属于对话，不进判定层、也不算"用户补充"。

AstrBot 的访问控制在 `waking_check` 阶段做：命中 `wake_prefix` 后**会剥掉前缀**
再写回 `event.message_str`，所以钩子里拿到的 `message_str` 可能已经不带头。
原始文本仍保留在 `event.message_obj.message` 的 Plain 组件里，这里两个都看。
"""

from __future__ import annotations

from typing import Any, Iterable

DEFAULT_WAKE_PREFIXES = ("/",)


def _plain_text(event: Any) -> str:
    """取消息的原始纯文本（不依赖被唤醒层改写过的 message_str）。"""
    parts: list[str] = []
    getter = getattr(event, "get_messages", None)
    if callable(getter):
        for comp in getter() or []:
            text = getattr(comp, "text", None)
            if isinstance(text, str) and text:
                parts.append(text)
    raw = "".join(parts)
    if raw.strip():
        return raw
    return str(getattr(event, "message_str", "") or "")


def normalize_prefixes(value: Any) -> tuple[str, ...]:
    """把配置里的 wake_prefix（列表或逗号串）收敛成前缀元组。"""
    if isinstance(value, str):
        items: Iterable[Any] = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        items = ()
    prefixes = tuple(str(item).strip() for item in items if str(item).strip())
    return prefixes or DEFAULT_WAKE_PREFIXES


def is_command_text(text: str, prefixes: tuple[str, ...] = DEFAULT_WAKE_PREFIXES) -> bool:
    stripped = (text or "").lstrip()
    if not stripped:
        return False
    return any(stripped.startswith(prefix) for prefix in prefixes if prefix)


def is_command_event(event: Any,
                     prefixes: tuple[str, ...] = DEFAULT_WAKE_PREFIXES) -> bool:
    if is_command_text(getattr(event, "message_str", "") or "", prefixes):
        return True
    return is_command_text(_plain_text(event), prefixes)


__all__ = [
    "DEFAULT_WAKE_PREFIXES",
    "is_command_event",
    "is_command_text",
    "normalize_prefixes",
]