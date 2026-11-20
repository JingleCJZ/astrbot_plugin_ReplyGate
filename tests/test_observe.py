"""仅观察（gate_dry_run）的严格契约测试。

仅观察时插件对一条回答只能做两件事：
  1. 去掉消息末尾的控制标记
  2. 记一条观察流水

不允许：stop_event、清空 chain、进入 HOLD 等待（sleep）、写回收池、取消任何在途 HOLD。
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
        def __init__(self, text=""):
            self.text = text

    class MessageChain:
        def __init__(self, chain=None):
            self.chain = chain or []

    mod("astrbot")
    mod("astrbot.api", logger=types.SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
        error=lambda *a, **k: None, exception=lambda *a, **k: None,
        debug=lambda *a, **k: None))
    mod("astrbot.api.event", AstrMessageEvent=object, MessageChain=MessageChain,
        filter=types.SimpleNamespace())
    mod("astrbot.api.message_components", Plain=Plain, Image=object, At=object,
        Reply=object)
    mod("astrbot.api.star", Context=object, Star=object, StarTools=object)
    mod("astrbot.api.provider", LLMResponse=object, ProviderRequest=object)
    mod("astrbot.api.web", json_response=lambda *a, **k: None,
        error_response=lambda *a, **k: None, request=object())


_install_stubs()

from core.config import Config  # noqa: E402
from core.outbound import OutboundGate  # noqa: E402

Plain = sys.modules["astrbot.api.message_components"].Plain


class FakeResult:
    def __init__(self, text):
        self.chain = [Plain(text=text)]
        self.async_stream = None


class FakeEvent:
    def __init__(self, text, umo="aiocqhttp:GroupMessage:1", sender="u1", private=False):
        self.unified_msg_origin = umo
        self._sender = sender
        self._private = private
        self._result = FakeResult(text)
        self.stopped = False

    def get_sender_id(self):
        return self._sender

    def is_private_chat(self):
        return self._private

    def get_result(self):
        return self._result

    def stop_event(self):
        self.stopped = True

    def text(self):
        return "".join(comp.text for comp in self._result.chain)


class FakePlugin:
    def __init__(self, config):
        self.cfg = Config(config)
        self.rows = []
        self.context = types.SimpleNamespace(send_message=None,
                                             register_task=lambda *a, **k: None)

    def log_event(self, **row):
        self.rows.append(row)


BASE = {
    "gate_enabled": True,
    "gate_dry_run": True,          # 本文件的重点：仅观察
    "silence_enabled": True,
    "silence_private_enabled": True,
}


def observe(config=None, text=""):
    plugin = FakePlugin(dict(BASE, **(config or {})))
    gate = OutboundGate(plugin)
    event = FakeEvent(text)
    started = time.monotonic()
    asyncio.run(gate.handle(event))
    return plugin, gate, event, time.monotonic() - started


class TestObserveOnlyStripsMarker(unittest.TestCase):

    def test_silence_marker_stripped_text_sent(self):
        plugin, gate, event, _ = observe(
            text='我明白了\n%%SILENCE%% {"reason":"goodbye"}')
        self.assertEqual(event.text(), "我明白了")
        self.assertNotIn("%%SILENCE%%", event.text())
        self.assertFalse(event.stopped, "仅观察不得 stop_event")
        self.assertEqual(len(event.get_result().chain), 1)
        self.assertEqual(gate.recycled, {}, "仅观察不得写回收池")
        self.assertEqual(gate.holds, {}, "仅观察不得进 HOLD")

    def test_hold_marker_not_delayed(self):
        # wait=30 若真的进入等待，测试会挂 30 秒；断言耗时 < 0.5s 证明没等
        plugin, gate, event, elapsed = observe(
            text='这是回答\n%%HOLD%% {"reason":"waiting_explanation","wait":30}')
        self.assertEqual(event.text(), "这是回答")
        self.assertLess(elapsed, 0.5, f"仅观察不得延时，实际耗时 {elapsed:.2f}s")
        self.assertEqual(gate.holds, {})
        self.assertFalse(event.stopped)

    def test_one_observation_row(self):
        plugin, _gate, _event, _ = observe(
            text='我明白了\n%%SILENCE%% {"reason":"goodbye"}')
        self.assertEqual(len(plugin.rows), 1)
        row = plugin.rows[0]
        self.assertEqual(row["result"], "dry_run")
        self.assertEqual(row["reason"], "goodbye")
        self.assertEqual(row["kind"], "silence")
        self.assertIn("仅观察", row["detail"])
        self.assertIn("正文照发", row["detail"])

    def test_hold_row_records_wait_and_plan(self):
        plugin, _gate, _event, _ = observe(
            text='回答\n%%HOLD%% {"reason":"waiting_explanation","wait":7}')
        row = plugin.rows[0]
        self.assertEqual(row["kind"], "hold")
        self.assertEqual(row["wait_seconds"], 7)
        self.assertIn("暂存 7s 后发送", row["detail"])

    def test_reason_switch_off_does_not_change_behavior(self):
        plugin, _gate, event, _ = observe(
            {"silence_allow_goodbye": False},
            text='正文\n%%SILENCE%% {"reason":"goodbye"}')
        self.assertEqual(event.text(), "正文")
        self.assertEqual(len(plugin.rows), 1)
        self.assertEqual(plugin.rows[0]["result"], "dry_run")
        self.assertIn("放行", plugin.rows[0]["detail"])

    def test_consent_trap_refuse_mode_does_not_change_behavior(self):
        plugin, _gate, event, _ = observe(
            {"consent_trap_refuse_reply": True},
            text='不行\n%%SILENCE%% {"reason":"consent_trap"}')
        self.assertEqual(event.text(), "不行")
        self.assertEqual(plugin.rows[0]["result"], "dry_run")

    def test_marker_only_reply_sends_nothing(self):
        # 只有标记没有正文：剥完就是空消息，不发空气泡
        plugin, gate, event, _ = observe(text='%%SILENCE%% {"reason":"user_request"}')
        self.assertEqual(event.get_result().chain, [])
        self.assertTrue(event.stopped)
        self.assertEqual(len(plugin.rows), 1)
        self.assertIn("标记外无正文", plugin.rows[0]["detail"])
        self.assertEqual(gate.recycled, {})

    def test_bad_marker_without_body_sends_nothing(self):
        plugin, _gate, event, _ = observe(text='%%SILENCE%% {oops}')
        self.assertEqual(plugin.rows[0]["kind"], "error")
        self.assertEqual("".join(c.text for c in event.get_result().chain), "")

    def test_no_marker_no_row(self):
        plugin, _gate, event, _ = observe(text="普通回复")
        self.assertEqual(event.text(), "普通回复")
        self.assertEqual(plugin.rows, [])

    def test_bad_marker_still_logs_error_and_sends(self):
        plugin, _gate, event, _ = observe(text='正文\n%%SILENCE%% {oops}')
        self.assertEqual(event.text(), "正文")
        self.assertEqual(plugin.rows[0]["kind"], "error")

    def test_user_request_reason_supported(self):
        plugin, _gate, event, _ = observe(
            text='好\n%%SILENCE%% {"reason":"user_request"}')
        self.assertEqual(event.text(), "好")
        self.assertEqual(plugin.rows[0]["reason"], "user_request")
        self.assertEqual(plugin.rows[0]["result"], "dry_run")


class TestObserveLeavesRunningHoldsAlone(unittest.TestCase):

    def test_observe_does_not_schedule_holds(self):
        plugin = FakePlugin(dict(BASE))
        gate = OutboundGate(plugin)
        for reason in ("waiting_explanation", "waiting_content", "user_request"):
            event = FakeEvent(f'正文\n%%HOLD%% {{"reason":"{reason}","wait":2}}')
            asyncio.run(gate.handle(event))
            self.assertEqual(gate.holds, {}, f"{reason} 不应留下在途 HOLD")
        self.assertEqual(gate.recycled, {})


class TestActualExecutionUnchanged(unittest.TestCase):
    """反证：关掉仅观察后，沉默与暂存仍然是真拦截。"""

    def test_silence_discards(self):
        plugin, gate, event, _ = observe(
            {"gate_dry_run": False}, text='我明白了\n%%SILENCE%% {"reason":"goodbye"}')
        self.assertEqual(event.get_result().chain, [])
        self.assertTrue(event.stopped)
        self.assertEqual(plugin.rows[0]["result"], "discarded")
        self.assertEqual(gate.recycled["aiocqhttp:GroupMessage:1"]["text"], "我明白了")

    def test_hold_waits(self):
        plugin, gate, event, elapsed = observe(
            {"gate_dry_run": False},
            text='回答\n%%HOLD%% {"reason":"user_request","wait":1}')
        self.assertGreaterEqual(elapsed, 0.9)
        self.assertEqual(event.text(), "回答")
        self.assertFalse(event.stopped)
        self.assertEqual([row["result"] for row in plugin.rows],
                         ["pending", "sent"])


if __name__ == "__main__":
    unittest.main()