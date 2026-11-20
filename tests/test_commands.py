"""命令消息旁路测试：`/` 开头的消息不进判定层、不撤销 HOLD。

AstrBot 的 waking_check 会先剥掉 wake_prefix 再写回 message_str，
所以这里同时覆盖"带前缀"和"已被剥掉前缀但原始组件仍带"的情况。
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
    mod("astrbot.api.event", AstrMessageEvent=object, MessageChain=object, filter=Filter)
    mod("astrbot.api.message_components", Plain=Plain, Image=object, At=object,
        Reply=object)
    mod("astrbot.api.star", Context=object, Star=Star, StarTools=object)
    mod("astrbot.api.provider", LLMResponse=object, ProviderRequest=object)
    mod("astrbot.api.web", json_response=lambda *a, **k: None,
        error_response=lambda *a, **k: None, request=object())


_install_stubs()

from core.commands import is_command_event, is_command_text, normalize_prefixes  # noqa: E402
import main as plugin_main  # noqa: E402

Plain = sys.modules["astrbot.api.message_components"].Plain


class FakeEvent:
    def __init__(self, message_str="", components=None, umo="aiocqhttp:GroupMessage:1"):
        self.message_str = message_str
        self.unified_msg_origin = umo
        self._components = components or []

    def get_messages(self):
        return self._components

    def get_sender_id(self):
        return "u1"

    def get_sender_name(self):
        return "用户1"

    def is_private_chat(self):
        return False

    def is_stopped(self):
        return getattr(self, "_stopped", False)

    def stop_event(self):
        self._stopped = True


class TestTextDetection(unittest.TestCase):

    def test_plain_command(self):
        self.assertTrue(is_command_text("/help"))
        self.assertTrue(is_command_text("/新对话"))
        self.assertTrue(is_command_text("   /help"))   # 前后空白不影响
        self.assertTrue(is_command_text("\n\t/reset"))

    def test_non_commands(self):
        for text in ("你好", "1/2 是分数", "a/b", "", "   ", "／全角不算"):
            self.assertFalse(is_command_text(text), text)

    def test_custom_prefixes(self):
        prefixes = normalize_prefixes(["#", "！"])
        self.assertTrue(is_command_text("#帮助", prefixes))
        self.assertTrue(is_command_text("！重开", prefixes))
        self.assertFalse(is_command_text("/help", prefixes))

    def test_prefix_config_shapes(self):
        self.assertEqual(normalize_prefixes(["/", "^"]), ("/", "^"))
        self.assertEqual(normalize_prefixes("/"), ("/",))
        self.assertEqual(normalize_prefixes("/ , ^"), ("/", "^"))
        self.assertEqual(normalize_prefixes(None), ("/",))
        self.assertEqual(normalize_prefixes([]), ("/",))
        self.assertEqual(normalize_prefixes([""]), ("/",))


class TestEventDetection(unittest.TestCase):

    def test_message_str_keeps_prefix(self):
        event = FakeEvent(message_str="/help")
        self.assertTrue(is_command_event(event))

    def test_prefix_already_stripped_from_message_str(self):
        # waking_check 场景：message_str 变成 "help"，原始组件里还有 "/help"
        event = FakeEvent(message_str="help", components=[Plain(text="/help")])
        self.assertTrue(is_command_event(event))

    def test_plain_chat(self):
        event = FakeEvent(message_str="你好", components=[Plain(text="你好")])
        self.assertFalse(is_command_event(event))

    def test_at_plus_command(self):
        event = FakeEvent(message_str="/help", components=[Plain(text="/help")])
        self.assertTrue(is_command_event(event))


class FakeConfig(dict):
    async def save_config_async(self):
        pass


class FakeGate:
    def __init__(self):
        self.holds = {}
        self.notified = []

    def on_message(self, event):
        self.notified.append(event.unified_msg_origin)


def make_plugin(**overrides):
    # 命令旁路与缓冲无关：这里关掉缓冲，避免每条用例都等安静窗口
    config = {"gate_enabled": True, "gate_dry_run": True, "command_bypass": True,
              "buffer_quiet_ms": 0}
    config.update(overrides)
    plugin = plugin_main.Main(None, FakeConfig(config))
    plugin.gate = FakeGate()
    plugin.rows = []
    plugin.log_event = lambda **row: plugin.rows.append(row)
    return plugin


class TestInboundBypass(unittest.TestCase):

    def run_inbound(self, plugin, event):
        asyncio.run(plugin.on_inbound(event))

    def test_command_does_not_notify_gate(self):
        plugin = make_plugin()
        self.run_inbound(plugin, FakeEvent(message_str="/help"))
        self.assertEqual(plugin.gate.notified, [])

    def test_normal_message_still_notifies(self):
        plugin = make_plugin()
        self.run_inbound(plugin, FakeEvent(message_str="你好"))
        self.assertEqual(plugin.gate.notified, ["aiocqhttp:GroupMessage:1"])

    def test_bypass_disabled_lets_command_through(self):
        plugin = make_plugin(command_bypass=False)
        self.run_inbound(plugin, FakeEvent(message_str="/help"))
        self.assertEqual(plugin.gate.notified, ["aiocqhttp:GroupMessage:1"])

    def test_master_switch_off_does_nothing(self):
        plugin = make_plugin(gate_enabled=False)
        self.run_inbound(plugin, FakeEvent(message_str="你好"))
        self.assertEqual(plugin.gate.notified, [])

    def test_is_command_helper(self):
        plugin = make_plugin()
        self.assertTrue(plugin.is_command(FakeEvent(message_str="/help")))
        self.assertFalse(plugin.is_command(FakeEvent(message_str="在吗")))
        plugin_off = make_plugin(command_bypass=False)
        self.assertFalse(plugin_off.is_command(FakeEvent(message_str="/help")))

    def test_wake_prefixes_fallback_without_context(self):
        plugin = make_plugin()
        self.assertEqual(plugin.wake_prefixes(), ("/",))


class TestConflictWarnings(unittest.TestCase):

    def test_warns_when_livinghello_loaded(self):
        plugin = make_plugin()
        star = types.SimpleNamespace(name="astrbot_plugin_LivingHello", activated=True)
        plugin.context = types.SimpleNamespace(get_all_stars=lambda: [star])
        warnings = plugin._check_conflicting_plugins()
        self.assertEqual(len(warnings), 1)
        self.assertIn("LivingHello", warnings[0])

    def test_no_warning_when_inactive(self):
        plugin = make_plugin()
        star = types.SimpleNamespace(name="astrbot_plugin_LivingHello", activated=False)
        plugin.context = types.SimpleNamespace(get_all_stars=lambda: [star])
        self.assertEqual(plugin._check_conflicting_plugins(), [])

    def test_no_warning_without_context(self):
        plugin = make_plugin()
        self.assertEqual(plugin._check_conflicting_plugins(), [])


if __name__ == "__main__":
    unittest.main()