"""M5 缓冲 + M4 打断的契约测试。

M5：消息立即到达，插件内部等安静窗口再合并处理。
  - 首条消息当"主人"并等待；后续消息并入同一批且不进管线（stop_event）。
  - 带媒体的消息不参与合并，自己成批并立即放行。
  - 窗口可被新消息顶住（防抖），但有最长等待上限。

M4：同用户新消息落在打断窗口内 → 停掉在途回答，让这条重新回答。
  - hard：调用 active_event_registry 停止；soft：不停止，直接另起一轮。
  - 次数预算用尽 / 接口不可用 / 没有在途 → 都有明确的流水结果。
"""

import asyncio
import os
import sys
import time
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _install_stubs() -> None:
    def mod(name, **attrs):
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module

    class Plain:
        type = "plain"

        def __init__(self, text=""):
            self.text = text

    class Image:
        type = "image"

    class MessageChain:
        def __init__(self, chain=None):
            self.chain = list(chain or [])

    class Star:
        def __init__(self, context=None):
            self.context = context

    class Filter:
        class EventMessageType:
            ALL = "all"

        @staticmethod
        def event_message_type(*_a, **_k):
            return lambda fn: fn

        @staticmethod
        def on_decorating_result(**_k):
            return lambda fn: fn

        @staticmethod
        def on_llm_request(**_k):
            return lambda fn: fn

    mod("astrbot")
    mod("astrbot.api", logger=types.SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
        error=lambda *a, **k: None, exception=lambda *a, **k: None,
        debug=lambda *a, **k: None))
    mod("astrbot.api.event", AstrMessageEvent=object, MessageChain=MessageChain,
        filter=Filter)
    mod("astrbot.api.message_components", Plain=Plain, Image=Image, At=object, Reply=object)
    mod("astrbot.api.star", Context=object, Star=Star, StarTools=object)
    mod("astrbot.api.provider", LLMResponse=object, ProviderRequest=object)
    mod("astrbot.api.web", json_response=lambda *a, **k: None,
        error_response=lambda *a, **k: None, request=object())


_install_stubs()

from core.buffer import MessageBuffer, has_media  # noqa: E402
import main as plugin_main  # noqa: E402

Plain = sys.modules["astrbot.api.message_components"].Plain
Image = sys.modules["astrbot.api.message_components"].Image


class At:
    type = "at"

    def __init__(self, qq="10000"):
        self.qq = qq


class FakeMessage:
    def __init__(self, *components):
        self.message = list(components)


class FakeResult:
    def __init__(self, *components):
        self.chain = list(components)
        self.async_stream = None


class FakeEvent:
    def __init__(self, text="", umo="aiocqhttp:GroupMessage:1", sender="u1",
                 private=False, wake=False, components=None):
        self.unified_msg_origin = umo
        self.message_str = text
        components = components if components is not None else [Plain(text=text)]
        self.message_obj = FakeMessage(*components)
        self._result = FakeResult(*components)
        self._sender = sender
        self._private = private
        self.is_at_or_wake_command = wake
        self.stopped = False

    def get_sender_id(self):
        return self._sender

    def get_self_id(self):
        return "10000"

    def set_extra(self, key, value):
        self._extras = getattr(self, "_extras", {})
        self._extras[key] = value

    def get_extra(self, key, default=None):
        return getattr(self, "_extras", {}).get(key, default)

    def is_private_chat(self):
        return self._private

    def get_messages(self):
        return self.message_obj.message

    def get_result(self):
        return self._result

    def stop_event(self):
        self.stopped = True

    def is_stopped(self):
        return self.stopped


class FakeConfig(dict):
    async def save_config_async(self):
        pass


class FakeSession:
    def __init__(self, score=None, verdict="keep", reason=""):
        self.score = score
        self.verdict = verdict
        self.reason = reason
        self.why = ""
        self.raw = ""
        self.done = True
        self.row_id = 0
        self.umo = "aiocqhttp:GroupMessage:1"
        self.sender_id = "u1"
        self.seq = 0


class FakeJudge:
    """可控判定器：测试里直接指定 verdict / score。"""

    def __init__(self):
        self.verdict = "keep"
        self.score = 0
        self.reason = ""
        self.calls = []

    def start(self, umo, window, draft="", sender_id="", detail="", trigger=""):
        self.calls.append((umo, list(window)))
        self.drafts = getattr(self, "drafts", [])
        self.drafts.append(draft)
        return FakeSession(score=self.score, verdict=self.verdict, reason=self.reason)

    async def await_verdict(self, umo, deadline=None):
        return self.verdict

    def raw_output(self, session):
        return ""

    def clear(self):
        pass


def make_plugin(**overrides):
    config = {
        "gate_enabled": True,
        "buffer_quiet_ms": 50,
        "buffer_max_wait_ms": 400,
        "interrupt_mode": "hard",
        "interrupt_group_enabled": True,
        "interrupt_private_enabled": True,
        "interrupt_window_ms": 8000,
        "interrupt_cooldown_count": 3,
        "command_bypass": True,
    }
    config.update(overrides)
    plugin = plugin_main.Main(None, FakeConfig(config))
    plugin.rows = []
    plugin.log_event = lambda **row: plugin.rows.append(row)

    async def fake_log_event_row(**row):
        row = dict(row)
        row["id"] = len(plugin.rows) + 1
        plugin.rows.append(row)
        return row["id"]

    async def fake_update_log_status(row_id, result, detail="", **fields):
        for row in plugin.rows:
            if row.get("id") == row_id:
                row["result"] = result
                if detail:
                    row["detail"] = detail
                for key, value in fields.items():
                    if value is not None:
                        row[key] = value
                return True
        return False

    plugin.log_event_row = fake_log_event_row
    plugin.update_log_status = fake_update_log_status
    plugin.stopped_calls = []

    def fake_stop(umo, event):
        plugin.stopped_calls.append((umo, event))
        return 1

    plugin._stop_active = staticmethod(fake_stop)
    plugin.judge = FakeJudge()
    return plugin


def rows_of(plugin, kind):
    return [row for row in plugin.rows if row.get("kind") == kind]


def inbound(plugin, event):
    """跑一次入站；把被吞掉的异常暴露出来。"""
    runner = getattr(plugin, "on_inbound")
    asyncio.run(runner(event))
    bad = [row for row in plugin.rows if row.get("kind") == "error"]
    if bad:
        raise AssertionError(f"入站异常：{bad}")
    return event


class TestBufferUnit(unittest.TestCase):

    def test_first_message_owns_batch(self):
        buf = MessageBuffer()
        batch, owner = buf.offer("u", "你好", "u1", False)
        self.assertTrue(owner)
        batch2, owner2 = buf.offer("u", "在吗", "u1", False)
        self.assertFalse(owner2)
        self.assertIs(batch, batch2)
        self.assertEqual(batch.merged, "你好\n在吗")
        self.assertEqual(batch.count, 2)

    def test_wake_is_sticky(self):
        buf = MessageBuffer()
        batch, _ = buf.offer("u", "a", "u1", False)
        buf.offer("u", "b", "u1", True)
        self.assertTrue(batch.wake)

    def test_settle_returns_after_quiet(self):
        buf = MessageBuffer()

        async def run():
            batch, _ = buf.offer("u", "你好", "u1", False)
            started = time.monotonic()
            await buf.settle(batch, 0.05, 1.0)
            return time.monotonic() - started

        elapsed = asyncio.run(run())
        self.assertGreaterEqual(elapsed, 0.04)
        self.assertLess(elapsed, 0.5)

    def test_settle_extends_on_new_message(self):
        buf = MessageBuffer()

        async def run():
            batch, _ = buf.offer("u", "第一句", "u1", False)
            task = asyncio.ensure_future(buf.settle(batch, 0.08, 2.0))
            await asyncio.sleep(0.03)
            buf.offer("u", "第二句", "u1", False)
            started = time.monotonic()
            await task
            return time.monotonic() - started, batch

        elapsed, batch = asyncio.run(run())
        self.assertGreaterEqual(elapsed, 0.06, "新消息应顶住安静窗口")
        self.assertEqual(batch.merged, "第一句\n第二句")

    def test_max_wait_caps(self):
        buf = MessageBuffer()

        async def run():
            batch, _ = buf.offer("u", "一直发", "u1", False)
            task = asyncio.ensure_future(buf.settle(batch, 0.5, 0.15))
            for _ in range(6):
                await asyncio.sleep(0.04)
                buf.offer("u", "还有", "u1", False)
            await task
            return batch

        started = time.monotonic()
        batch = asyncio.run(run())
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertGreaterEqual(batch.count, 3)

    def test_immediate_batch(self):
        buf = MessageBuffer()

        async def run():
            batch, _ = buf.offer("u", "", "u1", False, immediate=True)
            started = time.monotonic()
            await buf.settle(batch, 5.0, 5.0)
            return time.monotonic() - started

        self.assertLess(asyncio.run(run()), 0.2)

    def test_snapshot_reports_pending(self):
        buf = MessageBuffer()
        buf.offer("u", "hi", "u1", True)
        snap = buf.snapshot()
        self.assertEqual(len(snap), 1)
        self.assertEqual(snap[0]["senders"], ["u1"])
        self.assertTrue(snap[0]["wake"])

    def test_drop_releases_batch(self):
        buf = MessageBuffer()
        buf.offer("u", "hi", "u1", False)
        buf.drop("u")
        self.assertEqual(buf.snapshot(), [])
        batch, owner = buf.offer("u", "重新开始", "u1", False)
        self.assertTrue(owner, "丢弃后下一条应重新成为主人")

    def test_has_media(self):
        self.assertTrue(has_media(FakeEvent("", components=[Image()])))
        self.assertFalse(has_media(FakeEvent("纯文本")))
        self.assertFalse(has_media(object()))


class TestBufferPipeline(unittest.TestCase):

    def test_second_message_is_merged_and_stopped(self):
        plugin = make_plugin()
        owner = FakeEvent("你好", wake=True)

        async def run():
            task = asyncio.ensure_future(plugin.on_inbound(owner))
            await asyncio.sleep(0.01)
            follower = FakeEvent("在吗")
            await plugin.on_inbound(follower)
            self.assertTrue(follower.stopped, "并入批次的消息不应单独走管线")
            await task

        asyncio.run(run())
        self.assertEqual(owner.message_str, "你好\n在吗")
        self.assertTrue(owner.is_at_or_wake_command, "批次里有唤醒消息时主人应保持唤醒")
        texts = [getattr(comp, "text", "") for comp in owner.message_obj.message]
        self.assertEqual("".join(texts), "你好\n在吗")

    def test_disabled_leaves_event_untouched(self):
        plugin = make_plugin(buffer_quiet_ms=0)
        event = FakeEvent("你好", wake=True)
        inbound(plugin, event)
        self.assertEqual(event.message_str, "你好")
        self.assertFalse(event.stopped)
        self.assertIn(event.unified_msg_origin, plugin.inflight)

    def test_media_message_does_not_wait(self):
        plugin = make_plugin(buffer_quiet_ms=5000)
        event = FakeEvent("", components=[Image()])
        started = time.monotonic()
        inbound(plugin, event)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_buffered_followup_keeps_original_texts(self):
        plugin = make_plugin()
        owner = FakeEvent("第一")
        follower = FakeEvent("第二")

        async def run():
            task = asyncio.ensure_future(plugin.on_inbound(owner))
            await asyncio.sleep(0.01)
            await plugin.on_inbound(follower)
            await task

        asyncio.run(run())
        self.assertEqual(owner.message_str, "第一\n第二")


class TestBufferingOnlyAffectsMainModel(unittest.TestCase):
    """缓冲只决定主模型何时收到消息；打断/判定/撤销 HOLD 必须立即发生。"""

    def test_interrupt_fires_before_buffer_flush(self):
        plugin = make_plugin(buffer_quiet_ms=3000, buffer_max_wait_ms=3000)
        self.prepare(plugin)

        async def run():
            event = FakeEvent("补充", sender="u1")
            task = asyncio.ensure_future(plugin.on_inbound(event))
            await asyncio.sleep(0.25)
            # 缓冲还没结束（3s），但打断已经发生
            self.assertEqual(len(plugin.stopped_calls), 1, "打断必须立即发生")
            self.assertTrue(event.is_at_or_wake_command)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(run())

    def test_soft_judge_starts_before_buffer_flush(self):
        plugin = make_plugin(buffer_quiet_ms=3000, buffer_max_wait_ms=3000,
                             interrupt_mode="soft")
        plugin.judge.verdict, plugin.judge.score = "regen", 90
        self.prepare(plugin)

        async def run():
            task = asyncio.ensure_future(plugin.on_inbound(FakeEvent("补充", sender="u1")))
            await asyncio.sleep(0.25)
            self.assertEqual(len(plugin.judge.calls), 1, "判定模型必须立即启动")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(run())

    def test_hold_cancel_same_tick_as_arrival(self):
        plugin = make_plugin(buffer_quiet_ms=3000, buffer_max_wait_ms=3000)
        calls = []

        class Gate:
            def on_message(self, _event):
                calls.append("hold_cancel")

            async def handle(self, _event):
                return None

        plugin.gate = Gate()

        async def run():
            task = asyncio.ensure_future(plugin.on_inbound(FakeEvent("补充", sender="u1")))
            await asyncio.sleep(0.05)
            self.assertEqual(calls, ["hold_cancel"], "撤销 HOLD 不受缓冲影响")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(run())

    def test_keep_verdict_swallows_after_buffer(self):
        plugin = make_plugin(buffer_quiet_ms=120, buffer_max_wait_ms=400,
                             interrupt_mode="soft")
        plugin.judge.verdict, plugin.judge.score = "keep", 8
        self.prepare(plugin)
        event = FakeEvent("废话", sender="u1")

        async def run():
            await plugin.on_inbound(event)

        asyncio.run(run())
        self.assertTrue(event.stopped, "keep → 本条不回答")
        self.assertFalse(event.is_at_or_wake_command)
        self.assertEqual(rows_of(plugin, "judge")[-1]["result"], "keep")

    def prepare(self, plugin, sender="u1"):
        plugin.inflight["aiocqhttp:GroupMessage:1"] = [{
            "event": None, "sender_id": sender, "started_at": time.time(),
        }]

    def test_debounce_resets_and_max_total_caps(self):
        plugin = make_plugin(buffer_quiet_ms=150, buffer_max_wait_ms=600)
        events = []
        started = time.monotonic()

        async def run():
            owner = FakeEvent("第一条")
            task = asyncio.ensure_future(plugin.on_inbound(owner))
            await asyncio.sleep(0.05)
            await plugin.on_inbound(FakeEvent("第二条"))
            await asyncio.sleep(0.05)
            await plugin.on_inbound(FakeEvent("第三条"))
            await task
            events.append((time.monotonic() - started, owner.message_str))

        asyncio.run(run())
        elapsed, text = events[0]
        self.assertGreaterEqual(elapsed, 0.25, "三条消息应在同一批里")
        self.assertLess(elapsed, 1.0, "受最大总缓冲时间约束")
        self.assertEqual(text, "第一条\n第二条\n第三条")


class TestInterrupt(unittest.TestCase):

    def prepare(self, plugin, sender="u1", age_ms=100, event=None):
        plugin.inflight["aiocqhttp:GroupMessage:1"] = [{
            "event": event,
            "sender_id": sender,
            "started_at": time.time() - age_ms / 1000.0,
        }]

    def test_hard_interrupt_stops_active(self):
        plugin = make_plugin(buffer_quiet_ms=0)
        self.prepare(plugin)
        event = FakeEvent("补充一句", sender="u1")
        inbound(plugin, event)
        self.assertEqual(len(plugin.stopped_calls), 1)
        self.assertTrue(event.is_at_or_wake_command, "打断后要让新消息重新回答")
        row = rows_of(plugin, "judge")[0]
        self.assertEqual(row["result"], "interrupted")
        self.assertEqual(row["reason"], "hard_interrupt")
        self.assertEqual(row["confidence"], 100)
        self.assertIn("已停止 1", row["detail"])
        # 旧那轮保留但标记作废（防止残答被发出），新那轮追加在后面
        rounds = plugin.inflight["aiocqhttp:GroupMessage:1"]
        self.assertEqual(len(rounds), 2)
        self.assertTrue(rounds[0]["discard"], "hard 也要作废旧回答的残答")
        self.assertIs(rounds[1]["event"], event)

    def test_other_sender_is_not_interrupted(self):
        plugin = make_plugin(buffer_quiet_ms=0)
        self.prepare(plugin, sender="u1")
        event = FakeEvent("别人说话", sender="u2")
        inbound(plugin, event)
        self.assertEqual(plugin.stopped_calls, [])
        self.assertEqual(rows_of(plugin, "judge"), [])

    def test_window_closed_stops_new_triggers(self):
        plugin = make_plugin(buffer_quiet_ms=0, interrupt_window_ms=500)
        self.prepare(plugin)
        umo = "aiocqhttp:GroupMessage:1"
        # 第一条消息开窗
        inbound(plugin, FakeEvent("第一条", sender="u1"))
        self.assertIn(umo, plugin._interrupt_deadline)
        # 人为让窗口过期
        plugin._interrupt_deadline[umo] = time.monotonic() - 1
        plugin.stopped_calls.clear()
        plugin.rows.clear()
        event = FakeEvent("窗口外", sender="u1")
        inbound(plugin, event)
        self.assertEqual(plugin.stopped_calls, [], "窗口外不再打断")
        self.assertEqual(rows_of(plugin, "judge"), [], "窗口外不记判定流水")
        self.assertIn(umo, plugin.inflight, "在途记录要留着，回答仍等已有判定结果")

    def test_first_message_opens_window(self):
        plugin = make_plugin(buffer_quiet_ms=0, interrupt_mode="soft")
        plugin.judge.verdict, plugin.judge.score = "keep", 5
        self.prepare(plugin)
        umo = "aiocqhttp:GroupMessage:1"
        self.assertNotIn(umo, plugin._interrupt_deadline)
        inbound(plugin, FakeEvent("补充", sender="u1"))
        self.assertIn(umo, plugin._interrupt_deadline, "第一条打断消息应开窗")
        self.assertEqual(len(plugin.judge.calls), 1)

    def test_send_resets_window(self):
        plugin = make_plugin(buffer_quiet_ms=0)
        umo = "aiocqhttp:GroupMessage:1"
        plugin._interrupt_deadline[umo] = time.monotonic() + 30
        event = FakeEvent("回答", sender="u1")

        class Gate:
            async def handle(self, _event):
                return None

            def suppress(self, _event):
                return True

        plugin.gate = Gate()
        asyncio.run(plugin.on_outbound(event))
        self.assertNotIn(umo, plugin._interrupt_deadline, "回答解决后计时归零")

    def test_soft_regen_resets_window(self):
        plugin = make_plugin(buffer_quiet_ms=0, interrupt_mode="soft")
        plugin.judge.verdict, plugin.judge.score = "regen", 99
        self.prepare(plugin)
        umo = "aiocqhttp:GroupMessage:1"
        plugin._interrupt_deadline[umo] = time.monotonic() + 30
        inbound(plugin, FakeEvent("补充", sender="u1"))
        self.assertNotIn(umo, plugin._interrupt_deadline, "判定重答后计时归零")

    def test_soft_judge_regen_stops_and_reanswers(self):
        plugin = make_plugin(buffer_quiet_ms=0, interrupt_mode="soft")
        plugin.judge.verdict, plugin.judge.score = "regen", 92
        old = FakeEvent("第一问", sender="u1", wake=True)
        self.prepare(plugin, event=old)
        event = FakeEvent("补充", sender="u1")
        inbound(plugin, event)
        self.assertEqual(len(plugin.judge.calls), 1, "应调用判定模型")
        self.assertEqual(len(plugin.stopped_calls), 1, "判定要重答后应停掉在途")
        self.assertTrue(plugin._should_suppress(old))
        row = rows_of(plugin, "judge")[0]
        self.assertEqual(row["result"], "interrupted")
        self.assertEqual(row["confidence"], 92)
        self.assertTrue(event.is_at_or_wake_command)

    def test_soft_judge_keep_swallows_new_message(self):
        plugin = make_plugin(buffer_quiet_ms=0, interrupt_mode="soft")
        plugin.judge.verdict, plugin.judge.score = "keep", 12
        old = FakeEvent("第一问", sender="u1", wake=True)
        self.prepare(plugin, event=old)
        event = FakeEvent("补充一句废话", sender="u1")
        inbound(plugin, event)
        self.assertEqual(plugin.stopped_calls, [], "保留旧回答，不能打断")
        self.assertFalse(plugin._should_suppress(old))
        self.assertTrue(event.stopped, "本条不单独回答")
        self.assertFalse(event.is_at_or_wake_command)
        row = rows_of(plugin, "judge")[0]
        self.assertEqual(row["result"], "keep")
        self.assertEqual(row["confidence"], 12)

    def test_soft_judge_fallback_follows_fail_mode(self):
        plugin = make_plugin(buffer_quiet_ms=0, interrupt_mode="soft",
                             soft_judge_fail_mode="regen")
        plugin.judge.verdict, plugin.judge.score = "none", None
        old = FakeEvent("第一问", sender="u1", wake=True)
        self.prepare(plugin, event=old)
        event = FakeEvent("补充", sender="u1")
        inbound(plugin, event)
        self.assertEqual(len(plugin.stopped_calls), 1)
        self.assertEqual(rows_of(plugin, "judge")[0]["result"], "interrupted")
        self.assertEqual(rows_of(plugin, "judge")[0]["reason"], "judge_unavailable")
        self.assertTrue(plugin._should_suppress(old))

    def test_soft_judge_fallback_keep(self):
        plugin = make_plugin(buffer_quiet_ms=0, interrupt_mode="soft",
                             soft_judge_fail_mode="keep")
        plugin.judge.verdict, plugin.judge.score = "none", None
        old = FakeEvent("第一问", sender="u1", wake=True)
        self.prepare(plugin, event=old)
        event = FakeEvent("补充", sender="u1")
        inbound(plugin, event)
        self.assertEqual(plugin.stopped_calls, [])
        self.assertEqual(rows_of(plugin, "judge")[0]["result"], "keep")
        self.assertTrue(event.stopped)

    def test_at_message_skips_judge(self):
        plugin = make_plugin(buffer_quiet_ms=0, interrupt_mode="soft")
        old = FakeEvent("第一问", sender="u1", wake=True)
        self.prepare(plugin, event=old)
        event = FakeEvent("@机器人", sender="u1", wake=True,
                          components=[At(), Plain(text="@机器人")])
        inbound(plugin, event)
        self.assertEqual(plugin.judge.calls, [], "@ 消息不判定")
        self.assertEqual(len(plugin.stopped_calls), 1)
        self.assertEqual(rows_of(plugin, "judge")[0]["result"], "interrupted")
        self.assertEqual(rows_of(plugin, "judge")[0]["reason"], "user_request")

    def test_other_users_off_does_not_judge(self):
        plugin = make_plugin(buffer_quiet_ms=0, interrupt_mode="soft")
        self.prepare(plugin, sender="u1", event=FakeEvent("问", sender="u1", wake=True))
        inbound(plugin, FakeEvent("别人插话", sender="u2"))
        self.assertEqual(plugin.judge.calls, [])
        self.assertEqual(plugin.stopped_calls, [])

    def test_other_users_on_triggers_judge(self):
        plugin = make_plugin(buffer_quiet_ms=0, interrupt_mode="soft",
                             interrupt_other_users=True)
        plugin.judge.verdict, plugin.judge.score = "regen", 95
        self.prepare(plugin, sender="u1", event=FakeEvent("问", sender="u1", wake=True))
        event = FakeEvent("别人插话", sender="u2")
        inbound(plugin, event)
        self.assertEqual(len(plugin.judge.calls), 1)
        self.assertEqual(len(plugin.stopped_calls), 1)
        self.assertTrue(event.is_at_or_wake_command)

    def test_switch_off(self):
        plugin = make_plugin(buffer_quiet_ms=0, interrupt_group_enabled=False)
        self.prepare(plugin)
        event = FakeEvent("补充", sender="u1")
        inbound(plugin, event)
        self.assertEqual(plugin.stopped_calls, [])
        self.assertEqual(rows_of(plugin, "judge"), [])

    def test_cooldown_degrades_to_judge(self):
        plugin = make_plugin(buffer_quiet_ms=0, interrupt_cooldown_count=1)
        plugin.judge.verdict, plugin.judge.score = "keep", 5
        self.prepare(plugin)
        inbound(plugin, FakeEvent("第一次", sender="u1"))
        self.assertEqual(len(plugin.stopped_calls), 1)
        self.prepare(plugin)
        inbound(plugin, FakeEvent("第二次", sender="u1"))
        results = [row["result"] for row in rows_of(plugin, "judge")]
        self.assertEqual(results, ["interrupted", "keep"], "第一次强打断，第二次降级走判定")
        self.assertIn("强打断次数已用尽", rows_of(plugin, "judge")[1]["detail"])
        self.assertEqual(len(plugin.judge.calls), 1, "降级后应走判定")

    def test_api_unavailable_logs_failed(self):
        plugin = make_plugin(buffer_quiet_ms=0)
        plugin._stop_active = staticmethod(lambda umo, event: -1)
        self.prepare(plugin)
        event = FakeEvent("补充", sender="u1")
        inbound(plugin, event)
        row = rows_of(plugin, "judge")[0]
        self.assertEqual(row["result"], "discarded")
        self.assertIn("接口不可用", row["detail"])

    def test_nothing_inflight(self):
        plugin = make_plugin(buffer_quiet_ms=0)
        event = FakeEvent("补充", sender="u1")
        inbound(plugin, event)
        self.assertEqual(plugin.stopped_calls, [])
        self.assertEqual(rows_of(plugin, "judge"), [])

    def test_inflight_cleared_by_answer(self):
        plugin = make_plugin(buffer_quiet_ms=0)
        answered = FakeEvent("回答", sender="u1", wake=True)
        self.prepare(plugin, event=answered)
        plugin.clear_inflight(answered)
        self.assertEqual(plugin.inflight, {})

    def test_regen_keeps_old_round_and_appends_new(self):
        plugin = make_plugin(buffer_quiet_ms=0, interrupt_mode="soft")
        plugin.judge.verdict, plugin.judge.score = "regen", 88
        old = FakeEvent("第一问", sender="u1", wake=True)
        self.prepare(plugin, event=old)
        event = FakeEvent("补充", sender="u1")
        inbound(plugin, event)
        rounds = plugin.inflight["aiocqhttp:GroupMessage:1"]
        self.assertEqual(len(rounds), 2, "旧回答作废那轮 + 新回答那轮")
        self.assertIs(rounds[0]["event"], old)
        self.assertTrue(rounds[0]["discard"])
        self.assertIs(rounds[1]["event"], event)
        # 旧回答产出后只清掉旧那轮，新那轮仍在
        plugin.clear_inflight(old)
        rounds = plugin.inflight["aiocqhttp:GroupMessage:1"]
        self.assertEqual(len(rounds), 1)
        self.assertIs(rounds[0]["event"], event)
        plugin.clear_inflight(event)
        self.assertEqual(plugin.inflight, {})

    def test_window_closed_keeps_round_for_pending_judge(self):
        plugin = make_plugin(buffer_quiet_ms=0, interrupt_window_ms=500)
        old = FakeEvent("第一问", sender="u1", wake=True)
        self.prepare(plugin, event=old, age_ms=5000)
        umo = "aiocqhttp:GroupMessage:1"
        plugin._interrupt_deadline[umo] = time.monotonic() - 1
        inbound(plugin, FakeEvent("很久以后", sender="u1"))
        self.assertIn(umo, plugin.inflight, "窗口关闭后不清理在途，回答仍要等判定")


class TestPrivateChatUsesJudge(unittest.TestCase):
    """私聊每条消息都会被 AstrBot 标成 wake；那不等于"点名"，必须走判定。"""

    def test_private_wake_is_not_forced(self):
        plugin = make_plugin(buffer_quiet_ms=0, interrupt_mode="soft")
        plugin.judge.verdict, plugin.judge.score = "regen", 90
        event_private = FakeEvent("第一问", private=True, wake=True)
        plugin.inflight["aiocqhttp:GroupMessage:1"] = [{
            "event": event_private, "sender_id": "u1", "started_at": time.time(),
        }]
        follow = FakeEvent("补充", private=True, wake=True)
        inbound(plugin, follow)
        self.assertEqual(len(plugin.judge.calls), 1, "私聊也必须先判定")
        self.assertEqual(rows_of(plugin, "judge")[0]["result"], "interrupted")

    def test_group_at_is_forced(self):
        plugin = make_plugin(buffer_quiet_ms=0, interrupt_mode="soft")
        plugin.inflight["aiocqhttp:GroupMessage:1"] = [{
            "event": None, "sender_id": "u1", "started_at": time.time(),
        }]
        event = FakeEvent("@机器人 补充", wake=True, components=[At(), Plain(text="@机器人 补充")])
        inbound(plugin, event)
        self.assertEqual(plugin.judge.calls, [])
        self.assertEqual(rows_of(plugin, "judge")[0]["result"], "interrupted")
        self.assertEqual(rows_of(plugin, "judge")[0]["reason"], "user_request")


class TestUnansweredMessages(unittest.TestCase):
    """生成期间到达的消息要标记出来，下一轮一并交给模型。"""

    def test_note_injected_on_next_turn(self):
        plugin = make_plugin(buffer_quiet_ms=0, interrupt_mode="soft",
                             soft_judge_fail_mode="keep")
        plugin.judge.verdict, plugin.judge.score = "keep", 5
        plugin.inflight["aiocqhttp:GroupMessage:1"] = [{
            "event": None, "sender_id": "u1", "started_at": time.time(),
        }]
        first = FakeEvent("第一问", sender="u1", wake=True)
        inbound(plugin, first)   # 判定 keep → 本条不回答，但进入未回复队列
        self.assertEqual(len(plugin._pending["aiocqhttp:GroupMessage:1"]), 1)

        # 旧回答结束；后续消息触发新一轮
        plugin.inflight.pop("aiocqhttp:GroupMessage:1", None)
        second = FakeEvent("第二问", sender="u1", wake=True)
        inbound(plugin, second)
        note = second.get_extra("replygate_unanswered_note")
        self.assertTrue(note, "应挂上未回复提示")
        self.assertIn("第一问", note)

        class Req:
            system_prompt = "人格"

        asyncio.run(plugin.on_llm_request(second, Req))
        self.assertIn("尚未回复的消息", Req.system_prompt)
        self.assertIn("第一问", Req.system_prompt)
        self.assertEqual(plugin._pending, {}, "注入后队列清空")

    def test_no_note_without_pending(self):
        plugin = make_plugin(buffer_quiet_ms=0)
        event = FakeEvent("你好", wake=True)
        inbound(plugin, event)
        self.assertFalse(event.get_extra("replygate_unanswered_note"))


class TestInterruptLogCarriesMessage(unittest.TestCase):

    def test_user_input_recorded(self):
        plugin = make_plugin(buffer_quiet_ms=0)
        plugin.inflight["aiocqhttp:GroupMessage:1"] = [{
            "event": None, "sender_id": "u1", "started_at": time.time(),
        }]
        inbound(plugin, FakeEvent("算了换个话题", sender="u1"))
        row = rows_of(plugin, "judge")[0]
        self.assertEqual(row["user_input"], "算了换个话题")


class TestSegmentControl(unittest.TestCase):

    def test_splits_and_sends_in_order(self):
        plugin = make_plugin(buffer_quiet_ms=0, segment_control_enabled=True,
                             segment_delay_ms=10)
        event = FakeEvent("第一段$$$第二段$$$第三段", sender="u1")
        sent = []

        async def fake_send(chain):
            sent.append("".join(getattr(c, "text", "") for c in chain.chain))

        event.send = fake_send
        count = asyncio.run(plugin._send_segmented(event))
        self.assertEqual(count, 3)
        self.assertEqual(sent, ["第一段", "第二段", "第三段"])
        self.assertTrue(event.stopped, "接管后要阻止管线再发整段")

    def test_abort_stops_remaining_segments(self):
        plugin = make_plugin(buffer_quiet_ms=0, segment_control_enabled=True,
                             segment_delay_ms=120)
        event = FakeEvent("第一段$$$第二段$$$第三段", sender="u1")
        sent = []

        async def fake_send(chain):
            sent.append("".join(getattr(c, "text", "") for c in chain.chain))

        event.send = fake_send
        umo = event.unified_msg_origin

        async def run():
            task = asyncio.ensure_future(plugin._send_segmented(event))
            await asyncio.sleep(0.05)
            plugin._segment_abort[umo] = plugin._segment_token.get(umo)
            await task

        asyncio.run(run())
        self.assertEqual(sent, ["第一段"], "被打断后不再发剩余分段")
        self.assertEqual(plugin.rows, [], "分句事件不进日志")

    def test_single_segment_is_untouched(self):
        plugin = make_plugin(buffer_quiet_ms=0, segment_control_enabled=True)
        event = FakeEvent("只有一段", sender="u1")
        sent = []

        async def fake_send(chain):
            sent.append(chain)

        event.send = fake_send
        self.assertEqual(asyncio.run(plugin._send_segmented(event)), 0)
        self.assertEqual(sent, [])


class TestSegmentDelayMath(unittest.TestCase):

    def test_log_delay(self):
        plugin = make_plugin(segment_delay_mode="log", segment_log_base=2.6)
        delay = plugin._segment_delay_seconds("1234567890")   # 10 字
        self.assertAlmostEqual(delay, 2.30, delta=0.15)

    def test_linear_delay(self):
        plugin = make_plugin(segment_delay_mode="linear",
                             segment_linear_start_ms=800, segment_linear_rate=10)
        # 初始 0.8s + 10 字 / 10 字每秒 = 1.8s
        self.assertAlmostEqual(plugin._segment_delay_seconds("1234567890"), 1.8, delta=0.05)

    def test_base_clamped(self):
        plugin = make_plugin(segment_delay_mode="log", segment_log_base=99)
        self.assertLessEqual(plugin._segment_delay_seconds("1234"), 30.0)


class TestSegmentInterruptDuringSend(unittest.TestCase):
    """分句发到一半被打断：行为等价于"生成到一半被打断"。"""

    def make(self, verdict="regen", score=90):
        plugin = make_plugin(buffer_quiet_ms=0, segment_control_enabled=True)
        plugin.judge.verdict, plugin.judge.score = verdict, score
        event = FakeEvent("第一段$$$第二段$$$第三段", sender="u1")
        sent = []

        async def fake_send(chain):
            sent.append("".join(getattr(c, "text", "") for c in chain.chain))

        event.send = fake_send
        return plugin, event, sent

    def test_regen_aborts_remaining(self):
        plugin, event, sent = self.make("regen", 95)
        plugin.inflight[event.unified_msg_origin] = [{
            "event": None, "sender_id": "u1", "started_at": time.time(),
        }]
        plugin._judge_sessions[event.unified_msg_origin] = {
            "session": FakeSession(score=95, verdict="regen"),
            "sender_id": "u1", "detail": "分段发送中到达",
        }

        async def run():
            return await plugin._send_segmented(event)

        count = asyncio.run(run())
        self.assertEqual(count, 1, "判定重答后，剩余分段不再发送")
        self.assertEqual(len(plugin.stopped_calls), 1)
        row = rows_of(plugin, "judge")[-1]
        self.assertEqual(row["result"], "partial_discarded", "已发一部分后被打断 = 中途作废")
        self.assertIn("已发 1/3 段", row["detail"])

    def test_keep_continues_sending(self):
        plugin, event, sent = self.make("keep", 5)
        plugin.inflight[event.unified_msg_origin] = [{
            "event": None, "sender_id": "u1", "started_at": time.time(),
        }]
        plugin._judge_sessions[event.unified_msg_origin] = {
            "session": FakeSession(score=5, verdict="keep"),
            "sender_id": "u1", "detail": "分段发送中到达",
        }

        async def run():
            return await plugin._send_segmented(event)

        count = asyncio.run(run())
        self.assertEqual(count, 3, "keep 时旧回答继续发完")
        self.assertEqual(rows_of(plugin, "judge")[-1]["result"], "keep")


class TestJudgeDraft(unittest.TestCase):

    def test_prompt_contains_draft(self):
        import prompts
        rendered = prompts.build_judge_prompt(["A：你好"], None, "这是主模型刚生成的回答")
        self.assertIn("主模型已经生成的回答", rendered)
        self.assertIn("这是主模型刚生成的回答", rendered)

    def test_no_draft_no_section(self):
        import prompts
        self.assertNotIn("主模型已经生成的回答", prompts.build_judge_prompt(["A：你好"]))


class TestOutboundHoldsForJudge(unittest.TestCase):
    """主模型已生成、判定未完成 → 压住不发，等判定；且只判定一次。"""

    def make(self, verdict="regen", score=90, delay=0.15):
        events = []

        class SlowJudge:
            def __init__(self):
                self.start_calls = 0

            def start(self, umo, window, draft=""):
                self.start_calls += 1
                return FakeSession(score=score, verdict=verdict)

            async def await_verdict(self, umo):
                await asyncio.sleep(delay)
                events.append("judge_done")
                return verdict

        class Gate:
            def suppress(self, _event):
                events.append("suppress")
                _event.get_result().chain = []
                _event.stop_event()
                return True

            async def handle(self, _event):
                events.append("send")

        plugin = make_plugin(buffer_quiet_ms=0, interrupt_mode="soft")
        plugin.judge = SlowJudge()
        plugin.gate = Gate()
        event = FakeEvent("旧回答", sender="u1", wake=True)
        plugin.inflight[event.unified_msg_origin] = [{
            "event": event, "sender_id": "u1", "started_at": time.time(),
            "judge": types.SimpleNamespace(score=score, done=False,
                                           deadline=time.monotonic() + 5),
        }]
        return plugin, event, events

    def test_waits_then_suppresses(self):
        plugin, event, events = self.make("regen", 90)
        asyncio.run(plugin.on_outbound(event))
        self.assertEqual(events, ["judge_done", "suppress"], "必须先等判定再决定")
        self.assertEqual(plugin.judge.start_calls, 0, "出站阶段不应再次判定")

    def test_waits_then_sends_when_keep(self):
        plugin, event, events = self.make("keep", 10)
        asyncio.run(plugin.on_outbound(event))
        self.assertEqual(events, ["judge_done", "send"])

    def test_judge_failure_follows_fail_mode(self):
        plugin, event, events = self.make("none", None)
        asyncio.run(plugin.on_outbound(event))
        self.assertEqual(events, ["judge_done", "suppress"],
                         "判定无分数时按失败策略（默认 regen）作废")


class TestSplitSegments(unittest.TestCase):
    """分隔符不能切在 $$公式$$ 里面。"""

    D = chr(36)

    def test_plain_split(self):
        plugin = make_plugin(buffer_quiet_ms=0)
        sep = self.D * 3
        self.assertEqual(plugin._split_segments(f"甲{sep}乙{sep}丙", sep), ["甲", "乙", "丙"])

    def test_adjacent_formulas_not_split(self):
        plugin = make_plugin(buffer_quiet_ms=0)
        text = self.D * 2 + "a" + self.D * 2 + self.D * 2 + "b" + self.D * 2   # $$a$$$$b$$
        parts = plugin._split_segments(text, self.D * 3)
        self.assertEqual(parts, [text], "两个公式相邻产生的 $$$ 不应切段")

    def test_formulas_kept_intact(self):
        plugin = make_plugin(buffer_quiet_ms=0)
        sep = self.D * 3
        text = self.D * 2 + "a" + self.D * 2 + " " + sep + " 下一段"
        parts = plugin._split_segments(text, sep)
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0], self.D * 2 + "a" + self.D * 2)
        self.assertEqual(parts[1], "下一段")

    def test_inline_formula_kept(self):
        plugin = make_plugin(buffer_quiet_ms=0)
        sep = self.D * 3
        parts = plugin._split_segments(f"值是 {self.D}x{self.D} {sep} 下一段", sep)
        self.assertEqual(parts, [f"值是 {self.D}x{self.D}", "下一段"])

    def test_no_separator(self):
        plugin = make_plugin(buffer_quiet_ms=0)
        self.assertEqual(plugin._split_segments("没有分隔符", self.D * 3), [])

    def test_skip_is_not_logged(self):
        plugin = make_plugin(buffer_quiet_ms=0, segment_control_enabled=True)
        event = FakeEvent("这条没有分隔符", sender="u1")

        async def fake_send(chain):
            raise AssertionError("不该发送")

        event.send = fake_send
        self.assertEqual(asyncio.run(plugin._send_segmented(event)), 0)
        self.assertEqual(plugin.rows, [], "分句事件不进日志")


class TestSeparatorConfig(unittest.TestCase):

    def test_custom_separator(self):
        plugin = make_plugin(buffer_quiet_ms=0, segment_control_enabled=True,
                             segment_separator="###")
        event = FakeEvent("第一段###第二段", sender="u1")
        sent = []

        async def fake_send(chain):
            sent.append("".join(getattr(c, "text", "") for c in chain.chain))

        event.send = fake_send
        self.assertEqual(asyncio.run(plugin._send_segmented(event)), 2)
        self.assertEqual(sent, ["第一段", "第二段"])

    def test_default_separator(self):
        plugin = make_plugin(buffer_quiet_ms=0)
        self.assertEqual(plugin.segment_separator(), "$$$")


class TestSuppressOldAnswer(unittest.TestCase):
    """弱打断的落点：旧回答在出站阶段被丢弃，不发送。"""

    class GatePlugin:
        def __init__(self):
            self.rows = []

        def log_event(self, **row):
            self.rows.append(row)

    def make(self):
        from core.outbound import OutboundGate
        plugin = self.GatePlugin()
        plugin.cfg = type("Cfg", (), {
            "enabled": True, "silence_enabled": True, "dry_run": True,
            "bool": lambda self, key, default=False: default,
            "int": lambda self, key, default=0, low=None, high=None: default,
            "str": lambda self, key, default="": default,
            "option": lambda self, key, default, allowed: default,
            "reason_allowed": lambda self, key: True,
        })()
        return plugin, OutboundGate(plugin)

    def test_suppress_clears_chain_and_recycles(self):
        plugin, gate = self.make()
        event = FakeEvent('旧回答正文' + '\n' + '%%SILENCE%% {"reason":"no_value"}')
        self.assertTrue(gate.suppress(event))
        self.assertTrue(event.stopped)
        self.assertEqual(event.get_result().chain, [])

    def test_outbound_suppresses_only_marked_round(self):
        plugin = make_plugin(buffer_quiet_ms=0, interrupt_mode="soft")
        old = FakeEvent("第一问", sender="u1", wake=True)
        self.prepare_round(plugin, old)
        plugin.inflight[old.unified_msg_origin][-1]["discard"] = True
        self.assertTrue(plugin._should_suppress(old))
        other = FakeEvent("别的回答", sender="u1", wake=True)
        self.assertFalse(plugin._should_suppress(other))

    @staticmethod
    def prepare_round(plugin, event):
        plugin.inflight.setdefault(event.unified_msg_origin, []).append({
            "event": event, "sender_id": "u1", "started_at": time.time(),
        })


class TestOptionApi(unittest.TestCase):

    def test_clamps_and_persists(self):
        plugin = make_plugin()
        result = asyncio.run(plugin.set_option("buffer_quiet_ms", 999999))
        self.assertEqual(result["value"], 10000)
        self.assertTrue(result["persisted"])
        self.assertEqual(plugin.cfg.raw.saves if hasattr(plugin.cfg.raw, "saves") else 1, 1)

    def test_rejects_non_numeric(self):
        plugin = make_plugin()
        result = asyncio.run(plugin.set_option("buffer_quiet_ms", "abc"))
        self.assertEqual(result["value"], 1500, "非法值回落到缺省")

    def test_unknown_key(self):
        plugin = make_plugin()
        with self.assertRaises(KeyError):
            asyncio.run(plugin.set_option("nope", 1))

    def test_option_types(self):
        types_map = plugin_main.Main.option_types()
        self.assertIn("interrupt_group_enabled", types_map["bools"])
        self.assertNotIn("buffer_enabled", types_map["bools"], "缓冲开关由静默窗口派生")
        self.assertIn("buffer_quiet_ms", types_map["ints"])
        self.assertEqual(types_map["strs"]["interrupt_mode"], ["soft", "hard"])

    def test_buffer_off_when_quiet_zero(self):
        plugin = make_plugin(buffer_quiet_ms=0)
        self.assertFalse(plugin.buffering_enabled())
        plugin = make_plugin(buffer_quiet_ms=1200)
        self.assertTrue(plugin.buffering_enabled())

    def test_interrupt_mode_switch(self):
        plugin = make_plugin()
        result = asyncio.run(plugin.set_option("interrupt_mode", "hard"))
        self.assertEqual(result["options"]["interrupt_mode"], "hard")
        with self.assertRaises(ValueError):
            asyncio.run(plugin.set_option("interrupt_mode", "wild"))

    def test_interrupt_mode_in_snapshot(self):
        plugin = make_plugin(interrupt_mode="hard")
        self.assertEqual(plugin.option_snapshot()["interrupt_mode"], "hard")

    def test_snapshot_reads_config(self):
        plugin = make_plugin(buffer_quiet_ms=2300)
        self.assertEqual(plugin.option_snapshot()["buffer_quiet_ms"], 2300)


if __name__ == "__main__":
    unittest.main()