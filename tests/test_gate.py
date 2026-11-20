"""出站闸门的离线测试：用最小 stub 顶替 astrbot，跑真实的 OutboundGate。

覆盖：SILENCE 丢弃、HOLD 到期发送、HOLD 被同用户打断、仅观察不拦截、回收池。
"""

import asyncio
import os
import sys
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

    class AstrMessageEvent:
        pass

    mod("astrbot")
    mod("astrbot.api", logger=types.SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
        error=lambda *a, **k: None, debug=lambda *a, **k: None))
    mod("astrbot.api.event", AstrMessageEvent=AstrMessageEvent,
        MessageChain=MessageChain, filter=types.SimpleNamespace())
    mod("astrbot.api.message_components", Plain=Plain, Image=object, At=object,
        Reply=object)
    mod("astrbot.api.star", Context=object, Star=object, StarTools=object)
    mod("astrbot.api.provider", LLMResponse=object, ProviderRequest=object)
    mod("astrbot.api.web", json_response=lambda *a, **k: None,
        error_response=lambda *a, **k: None, request=object)


_install_stubs()

from core.config import Config  # noqa: E402
from core.models import LogRow  # noqa: E402
from core.outbound import OutboundGate  # noqa: E402

Plain = sys.modules["astrbot.api.message_components"].Plain


class FakeResult:
    def __init__(self, text):
        self.chain = [Plain(text=text)]
        self.async_stream = None


class FakeEvent:
    def __init__(self, text, umo="wechat:FriendMessage:1", sender="u1", private=True):
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

    def sent(self, kind):
        return [row for row in self.rows if row.get("kind") == kind]


BASE = {
    "gate_enabled": True,
    "gate_dry_run": False,
    "silence_enabled": True,
    "silence_private_enabled": True,
    "group_judge_hang": False,
}


class TestSilence(unittest.TestCase):
    def test_silence_discards_chain_and_recycles(self):
        plugin = FakePlugin(dict(BASE))
        gate = OutboundGate(plugin)
        event = FakeEvent('我明白了\n%%SILENCE%% {"reason":"goodbye"}')
        asyncio.run(gate.handle(event))
        self.assertEqual(event.get_result().chain, [])
        self.assertTrue(event.stopped)
        self.assertEqual(plugin.sent("silence")[0]["result"], "discarded")
        self.assertEqual(gate.recycled[event.unified_msg_origin]["text"], "我明白了")

    def test_switch_off_passes_through(self):
        plugin = FakePlugin(dict(BASE, silence_allow_goodbye=False))
        gate = OutboundGate(plugin)
        event = FakeEvent('正文\n%%SILENCE%% {"reason":"goodbye"}')
        asyncio.run(gate.handle(event))
        self.assertEqual(event.text(), "正文")
        self.assertEqual(plugin.sent("silence")[0]["result"], "switch_off")

    def test_consent_trap_refuse_mode_passes_through(self):
        plugin = FakePlugin(dict(BASE, consent_trap_refuse_reply=True))
        gate = OutboundGate(plugin)
        event = FakeEvent('不行\n%%SILENCE%% {"reason":"consent_trap"}')
        asyncio.run(gate.handle(event))
        self.assertEqual(event.text(), "不行")
        self.assertEqual(plugin.sent("silence")[0]["result"], "refuse_passthrough")

    def test_dry_run_keeps_text(self):
        plugin = FakePlugin(dict(BASE, gate_dry_run=True))
        gate = OutboundGate(plugin)
        event = FakeEvent('我明白了\n%%SILENCE%% {"reason":"goodbye"}')
        asyncio.run(gate.handle(event))
        self.assertEqual(event.text(), "我明白了")
        self.assertEqual(plugin.sent("silence")[0]["result"], "dry_run")
        self.assertEqual(gate.recycled, {})


class TestHold(unittest.TestCase):
    def test_hold_sends_after_wait(self):
        plugin = FakePlugin(dict(BASE))
        gate = OutboundGate(plugin)
        event = FakeEvent('这是回答\n%%HOLD%% {"reason":"waiting_explanation","wait":1}')
        asyncio.run(gate.handle(event))
        self.assertEqual(event.text(), "这是回答")
        results = [row["result"] for row in plugin.sent("hold")]
        self.assertIn("pending", results)
        self.assertEqual(results[-1], "sent")
        self.assertFalse(event.stopped)
        self.assertEqual(gate.holds, {})

    def test_hold_cancelled_by_same_user(self):
        plugin = FakePlugin(dict(BASE))
        gate = OutboundGate(plugin)
        event = FakeEvent('这是回答\n%%HOLD%% {"reason":"waiting_explanation","wait":5}')

        async def run():
            task = asyncio.ensure_future(gate.handle(event))
            await asyncio.sleep(0.3)
            intruder = FakeEvent("补充解释", sender="u1")
            gate.on_message(intruder)
            await task

        asyncio.run(run())
        self.assertEqual(event.get_result().chain, [])
        self.assertTrue(event.stopped)
        self.assertEqual(plugin.sent("hold")[-1]["result"], "cancelled")
        self.assertEqual(gate.recycled[event.unified_msg_origin]["reason"],
                         "waiting_explanation")

    def test_hold_ignores_other_user(self):
        plugin = FakePlugin(dict(BASE))
        gate = OutboundGate(plugin)
        event = FakeEvent('这是回答\n%%HOLD%% {"reason":"waiting_explanation","wait":1}')

        async def run():
            task = asyncio.ensure_future(gate.handle(event))
            await asyncio.sleep(0.2)
            gate.on_message(FakeEvent("别人说话", sender="u2"))
            await task

        asyncio.run(run())
        self.assertEqual(event.text(), "这是回答")
        self.assertEqual(plugin.sent("hold")[-1]["result"], "sent")

    def test_waiting_content_any_scope_cancel(self):
        plugin = FakePlugin(dict(BASE))
        gate = OutboundGate(plugin)
        event = FakeEvent('图呢？\n%%HOLD%% {"reason":"waiting_content","wait":5}')

        async def run():
            task = asyncio.ensure_future(gate.handle(event))
            await asyncio.sleep(0.2)
            gate.on_message(FakeEvent("别人说话", sender="u2"))
            await task

        asyncio.run(run())
        self.assertEqual(event.get_result().chain, [])
        self.assertEqual(plugin.sent("hold")[-1]["result"], "cancelled")
        # 催促语没有回收价值
        self.assertNotIn(event.unified_msg_origin, gate.recycled)

    def test_hold_switch_off_sends_immediately(self):
        plugin = FakePlugin(dict(BASE, silence_allow_waiting_explanation=False))
        gate = OutboundGate(plugin)
        event = FakeEvent('回答\n%%HOLD%% {"reason":"waiting_explanation","wait":10}')
        asyncio.run(gate.handle(event))
        self.assertEqual(event.text(), "回答")
        self.assertEqual(plugin.sent("hold")[0]["result"], "switch_off")


class TestErrors(unittest.TestCase):
    def test_bad_marker_sends_body(self):
        plugin = FakePlugin(dict(BASE))
        gate = OutboundGate(plugin)
        event = FakeEvent('正文\n%%SILENCE%% {oops}')
        asyncio.run(gate.handle(event))
        self.assertEqual(event.text(), "正文")
        self.assertEqual(plugin.sent("error")[0]["detail"], "marker_bad_json")

    def test_no_marker_no_log(self):
        plugin = FakePlugin(dict(BASE))
        gate = OutboundGate(plugin)
        event = FakeEvent("普通回复\n$$$第二段")
        asyncio.run(gate.handle(event))
        self.assertEqual(event.text(), "普通回复\n$$$第二段")
        self.assertEqual(plugin.rows, [])

    def test_disabled_plugin_no_change(self):
        plugin = FakePlugin(dict(BASE, gate_enabled=False))
        gate = OutboundGate(plugin)
        event = FakeEvent('正文\n%%SILENCE%% {"reason":"goodbye"}')
        asyncio.run(gate.handle(event))
        self.assertIn("%%SILENCE%%", event.text())


if __name__ == "__main__":
    unittest.main(verbosity=2)