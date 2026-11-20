"""消息缓冲：把同一会话里连续几条消息合并成一次处理。

为什么放在插件里，而不是 AstrWechat：

  AstrWechat 的 `buffer_seconds` 是"传输前合并"——消息先在桥里压 5 秒，
  再推给 AstrBot。这 5 秒里 ReplyGate 什么都看不到，也就无法及时打断在途回答，
  判定层的输入也只能是"压完的合并文本"。

  放到插件里做：消息**立即**到达 AstrBot，ReplyGate 在管线内等一个安静窗口，
  决定"这批要不要一起答"。等待期间新消息会并入同一批而不是各自触发回答，
  在途回答的打断判定也能拿到最新那条消息（见 main.Main._maybe_interrupt）。

语义：
  - 每个会话（umo）同时只有一个批次；
  - 批次由**第一条**消息所属事件当"主人"，它负责在 `settle()` 里等安静窗口，
    最后把合并后的文本写回自己的事件，再由管线继续（唤醒/判定）；
  - 后续消息不进管线（调用方 `event.stop_event()`），文本已并入批次；
  - 带媒体的消息（图片/文件/语音/视频/引用）不参与合并：它自己成批并立即放行，
    避免"合并后图片丢了"。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

# 参与"立即放行"的组件类型名：媒体/引用不该被合并吞掉
IMMEDIATE_COMPONENT_TYPES = frozenset({"Image", "File", "Record", "Video", "Reply"})

POLL_INTERVAL_SECONDS = 0.1


def has_media(event: Any) -> bool:
    """事件里是否带媒体/引用组件（带则不做合并）。"""
    try:
        chain = getattr(getattr(event, "message_obj", None), "message", None)
        if not chain:
            return False
        return any(type(comp).__name__ in IMMEDIATE_COMPONENT_TYPES for comp in chain)
    except Exception:  # noqa: BLE001 - 探测失败按"无媒体"处理
        return False


@dataclass
class Batch:
    """一个会话里正在累积的一批消息。"""

    umo: str
    texts: list[str] = field(default_factory=list)
    senders: set[str] = field(default_factory=set)
    wake: bool = False
    immediate: bool = False
    started_at: float = field(default_factory=time.monotonic)
    last_at: float = field(default_factory=time.monotonic)
    count: int = 0
    last_sender: str = ""
    seqs: list[int] = field(default_factory=list)   # 本批消息的到达序号

    def add(self, text: str, sender_id: str, wake: bool,
            seq: int | None = None) -> None:
        cleaned = (text or "").strip()
        if cleaned:
            self.texts.append(cleaned)
        if sender_id:
            self.senders.add(sender_id)
            self.last_sender = sender_id
        self.wake = self.wake or bool(wake)
        self.last_at = time.monotonic()
        self.count += 1
        if seq is not None:
            self.seqs.append(int(seq))

    @property
    def merged(self) -> str:
        return "\n".join(self.texts).strip()

    @property
    def idle_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.last_at)


class MessageBuffer:
    """按会话维护的防抖缓冲。一个实例由插件持有。"""

    def __init__(self) -> None:
        self._batches: dict[str, Batch] = {}

    def offer(self, umo: str, text: str, sender_id: str, wake: bool,
              immediate: bool = False, seq: int | None = None) -> tuple[Batch, bool]:
        """把一条消息并入批次。

        Returns:
            (批次, 是否为本批主人)。主人需要在 `settle()` 里等安静窗口。
        """
        batch = self._batches.get(umo)
        owner = batch is None
        if batch is None:
            batch = Batch(umo=umo)
            self._batches[umo] = batch
        batch.add(text, sender_id, wake, seq=seq)
        if immediate:
            batch.immediate = True
        return batch, owner

    async def settle(self, batch: Batch, quiet_seconds: float,
                     max_wait_seconds: float) -> Batch:
        """等安静窗口结束（或到达最长等待），然后交还批次并从表里摘除。"""
        quiet = max(0.0, float(quiet_seconds))
        cap = max(quiet, float(max_wait_seconds))
        deadline = batch.started_at + cap
        while True:
            if batch.immediate:
                break
            idle = time.monotonic() - batch.last_at
            if idle >= quiet:
                break
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(min(POLL_INTERVAL_SECONDS, max(0.01, quiet - idle)))
        if self._batches.get(batch.umo) is batch:
            self._batches.pop(batch.umo, None)
        return batch

    def snapshot(self) -> list[dict[str, Any]]:
        """面板用：当前正在累积的批次。"""
        return [{
            "umo": batch.umo,
            "count": batch.count,
            "senders": sorted(batch.senders),
            "idle_ms": int(batch.idle_seconds * 1000),
            "wake": batch.wake,
            "immediate": batch.immediate,
        } for batch in self._batches.values()]

    def drop(self, umo: str) -> None:
        """丢弃某会话的批次（主人被取消/异常时兜底，避免后续消息并入死批次）。"""
        self._batches.pop(umo, None)

    def clear(self) -> None:
        self._batches.clear()