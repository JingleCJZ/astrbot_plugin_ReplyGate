"""提示词块注入的离线测试：prompts 工具函数 + on_llm_request 钩子。

覆盖：
  - 提示词块是 Markdown（# 沉默机制 / ## 两段 / 5 秒说明）
  - 空系统提示词直接给块；非空则追加在末尾
  - 幂等：已有等价说明（新标题或旧【沉默与等待协议】）不再注入
  - 钩子尊重 prompt_block_inject 与总开关
  - 注入异常不影响回复（只记 error 流水）
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
    mod("astrbot.api.message_components", Plain=object, Image=object, At=object,
        Reply=object)
    mod("astrbot.api.star", Context=object, Star=Star, StarTools=object)
    mod("astrbot.api.provider", LLMResponse=object, ProviderRequest=object)
    mod("astrbot.api.web", json_response=lambda *a, **k: None,
        error_response=lambda *a, **k: None, request=object())


_install_stubs()

import prompts  # noqa: E402
import main as plugin_main  # noqa: E402


class FakeConfig(dict):
    async def save_config_async(self):
        pass


class FakeReq:
    def __init__(self, system_prompt=""):
        self.system_prompt = system_prompt


class FakeEvent:
    unified_msg_origin = "aiocqhttp:GroupMessage:1"


def make_plugin(**overrides):
    config = {"gate_enabled": True, "gate_dry_run": True,
              "prompt_block_inject": True}
    config.update(overrides)
    plugin = plugin_main.Main(None, FakeConfig(config))
    plugin.rows = []
    plugin.log_event = lambda **row: plugin.rows.append(row)
    return plugin


class TestBlockContent(unittest.TestCase):

    def test_markdown_shape(self):
        block = prompts.persona_template()
        self.assertTrue(block.startswith("# 沉默机制"))
        self.assertIn("## 想完全不出声时（本条消息沉默），只输出一行、不要带正文：", block)
        self.assertIn("## 想先等一等再决定（延迟发送），正常写正文，然后在最后另起一行：", block)
        self.assertIn("## 不想沉默就正常说话，不要输出任何标记。", block)
        self.assertIn('%%SILENCE%% {"reason":"<原因>"}', block)
        self.assertIn('%%HOLD%% {"reason":"<原因>","wait":<秒>}', block)
        self.assertIn("若用户在设定时间内发送新消息，则正文会被丢弃", block)
        self.assertIn("标记等待 10 秒左右", block)
        self.assertIn("等待 15 秒左右", block)
        self.assertIn("waiting_explanation", block)
        self.assertIn("waiting_content", block)

    def test_reasons_match_parser(self):
        from core.models import ALL_REASONS
        for reason in ("goodbye", "consent_trap", "no_value",
                       "already_answered", "user_busy", "user_request",
                       "waiting_explanation", "waiting_content"):
            self.assertIn(reason, ALL_REASONS, f"{reason} 不在解析器支持的原因里")
            self.assertIn(reason, prompts.persona_template())


class TestAppendBlock(unittest.TestCase):

    def test_empty_prompt_gets_block(self):
        self.assertEqual(prompts.append_block(""), prompts.PROMPT_BLOCK)
        self.assertEqual(prompts.append_block(None), prompts.PROMPT_BLOCK)

    def test_appended_after_persona(self):
        merged = prompts.append_block("你是小助手。\n")
        self.assertTrue(merged.startswith("你是小助手。\n\n# 沉默机制"))
        self.assertTrue(merged.endswith(prompts.PROMPT_BLOCK))

    def test_idempotent_when_block_present(self):
        once = prompts.append_block("你是小助手。")
        self.assertIsNone(prompts.append_block(once))
        self.assertIsNone(prompts.append_block("# 沉默机制\n自定义内容"))

    def test_legacy_heading_detected(self):
        self.assertIsNone(prompts.append_block("【沉默与等待协议】\n%%SILENCE%% ..."))

    def test_block_present(self):
        self.assertFalse(prompts.block_present("你是小助手。"))
        self.assertTrue(prompts.block_present(prompts.PROMPT_BLOCK))


class TestOnLlmRequestHook(unittest.TestCase):

    def run_hook(self, plugin, req):
        asyncio.run(plugin.on_llm_request(FakeEvent(), req))

    def test_injects_into_system_prompt(self):
        plugin = make_plugin()
        req = FakeReq("你是小助手。")
        self.run_hook(plugin, req)
        self.assertIn(prompts.PROMPT_BLOCK, req.system_prompt)
        self.assertTrue(req.system_prompt.startswith("你是小助手。"))

    def test_second_call_does_not_duplicate(self):
        plugin = make_plugin()
        req = FakeReq("")
        self.run_hook(plugin, req)
        first = req.system_prompt
        self.run_hook(plugin, req)
        self.assertEqual(req.system_prompt, first)
        self.assertEqual(req.system_prompt.count("# 沉默机制"), 1)

    def test_switch_off(self):
        plugin = make_plugin(prompt_block_inject=False)
        req = FakeReq("你是小助手。")
        self.run_hook(plugin, req)
        self.assertEqual(req.system_prompt, "你是小助手。")

    def test_master_switch_off(self):
        plugin = make_plugin(gate_enabled=False)
        req = FakeReq("你是小助手。")
        self.run_hook(plugin, req)
        self.assertEqual(req.system_prompt, "你是小助手。")

    def test_injection_failure_is_swallowed(self):
        plugin = make_plugin()
        req = object()  # 不可写属性，setattr 会抛
        self.run_hook(plugin, req)
        errors = [row for row in plugin.rows if row.get("kind") == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("prompt_block", errors[0]["detail"])


if __name__ == "__main__":
    unittest.main()
