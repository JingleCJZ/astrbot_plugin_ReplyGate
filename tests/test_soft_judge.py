"""弱打断判定模型（core/judge.py）的契约测试。

覆盖：
  - 打分解析（JSON / 纯数字 / 垃圾输出）
  - 判定提示词的占位符替换与自定义
  - 判定会话：重启、等待最新结果、超时、次数用尽
  - provider 缺失 / 输出不可解析 → 按失败策略收尾
  - 评分高于阈值 → 旧回答在出站阶段被作废
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
    mod("astrbot.api.message_components", Plain=Plain, Image=object, At=object, Reply=object)
    mod("astrbot.api.star", Context=object, Star=Star, StarTools=object)
    mod("astrbot.api.provider", LLMResponse=object, ProviderRequest=object)
    mod("astrbot.api.web", json_response=lambda *a, **k: None,
        error_response=lambda *a, **k: None, request=object())


_install_stubs()

import prompts  # noqa: E402
from core.config import Config  # noqa: E402
from core.judge import (JUDGE_SYSTEM_PROMPT, VERDICT_KEEP, VERDICT_REGEN,  # noqa: E402
                        VERDICT_RESTARTED, SoftJudge, normalize_reason,
                        parse_score, parse_verdict)
import main as plugin_main  # noqa: E402


class FakeResponse:
    def __init__(self, text=""):
        self.completion_text = text


class FakeProvider:
    def __init__(self, reply='{"score": 80}', delay=0.0, raise_exc=None):
        self.reply = reply
        self.delay = delay
        self.raise_exc = raise_exc
        self.prompts = []

    async def text_chat(self, prompt=None, system_prompt=None, contexts=None):
        self.prompts.append(prompt)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raise_exc:
            raise self.raise_exc
        return FakeResponse(self.reply)


class FakeContext:
    def __init__(self, provider=None):
        self.provider = provider

    def get_provider_by_id(self, pid):
        return self.provider

    def get_using_provider(self, umo=None):
        return self.provider


class JudgePlugin:
    def __init__(self, cfg, provider=None):
        self.cfg = Config(cfg)
        self.rows = []
        self.context = FakeContext(provider)

    def list_providers(self):
        return plugin_main.Main.list_providers(self)

    def log_event(self, **row):
        self.rows.append(row)

    async def log_event_row(self, **row):
        row = dict(row)
        row["id"] = len(self.rows) + 1
        self.rows.append(row)
        return row["id"]

    async def update_log_status(self, row_id, result, detail="", **fields):
        for row in self.rows:
            if row.get("id") == row_id:
                row["result"] = result
                if detail:
                    row["detail"] = detail
                for key, value in fields.items():
                    if value is not None:
                        row[key] = value
                return True
        return False


BASE = {
    "soft_judge_threshold": 60,
    "soft_judge_timeout_ms": 2000,
    "soft_judge_max_restarts": 3,
    "soft_judge_fail_mode": "regen",
    "soft_judge_context_before": 0,
}


def make(cfg=None, provider=None):
    plugin = JudgePlugin({**BASE, **(cfg or {})}, provider)
    return plugin, SoftJudge(plugin)


class TestParseScore(unittest.TestCase):

    def test_reason_codes(self):
        score, reason, why = parse_verdict(
            '{"score": 90, "reason": "requirement_change", "why": "换了需求"}')
        self.assertEqual((score, reason, why), (90, "requirement_change", "换了需求"))
        self.assertEqual(parse_verdict('{"score": 90, "reason": "info_supplement"}')[1],
                         "info_supplement")
        self.assertEqual(parse_verdict('{"score": 90, "reason": "user_request"}')[1],
                         "user_request")

    def test_reason_accepts_chinese(self):
        self.assertEqual(normalize_reason("用户要求"), "user_request")
        self.assertEqual(normalize_reason("用户需求取消/改变"), "requirement_change")
        self.assertEqual(normalize_reason("需求改变"), "requirement_change")
        self.assertEqual(normalize_reason("用户补充信息"), "info_supplement")
        self.assertEqual(normalize_reason("补充信息"), "info_supplement")

    def test_unknown_reason_is_blank(self):
        self.assertEqual(normalize_reason("就是想聊"), "")
        self.assertEqual(normalize_reason(None), "")
        self.assertEqual(parse_verdict('{"score": 90, "reason": "whatever"}')[1], "",
                         "不在固定集合里的原因不能写进流水")

    def test_judge_system_prompt_lists_reasons(self):
        for code in ("user_request", "requirement_change", "info_supplement"):
            self.assertIn(code, JUDGE_SYSTEM_PROMPT)

    def test_variants(self):
        self.assertEqual(parse_score('{"score": 87, "why": "x"}'), 87)
        self.assertEqual(parse_score('{"score": "95"}'), 95)
        self.assertEqual(parse_score("score: 12"), 12)
        self.assertEqual(parse_score("我觉得 40 分"), 40)
        self.assertEqual(parse_score("```json\n{\"score\":100}\n```"), 100)

    def test_garbage(self):
        for text in ("", "no number", '{"why":"x"}', "1234", None):
            self.assertIsNone(parse_score(text), repr(text))

    def test_out_of_range_clamped(self):
        self.assertEqual(parse_score('{"score": 250}'), 100)
        self.assertEqual(parse_score('{"score": -5}'), 0)


class TestPrompt(unittest.TestCase):

    def test_placeholder_replaced(self):
        rendered = prompts.build_judge_prompt(["A：在吗", "B：这个报错"])
        self.assertNotIn("{{messages}}", rendered)
        self.assertIn("A：在吗", rendered)
        self.assertIn("B：这个报错", rendered)

    def test_empty_window(self):
        self.assertIn("（无可用上下文）", prompts.build_judge_prompt([]))

    def test_custom_template(self):
        rendered = prompts.build_judge_prompt(["x"], "自定义：{{messages}}")
        self.assertTrue(rendered.startswith("自定义：x"))
        self.assertIn("requirement_change", rendered, "自定义提示词没要求 reason 时要自动补上")

    def test_custom_template_with_reason_is_untouched(self):
        rendered = prompts.build_judge_prompt(
            ["x"], '自定义：{{messages}}\n输出 {"score":0,"reason":""}')
        self.assertNotIn(prompts.REASON_INSTRUCTION, rendered)

    def test_custom_blank_falls_back(self):
        self.assertEqual(prompts.effective_judge_prompt("   "), prompts.JUDGE_SCORE_TEMPLATE)


class TestJudgeSession(unittest.TestCase):

    def run_judge(self, cfg=None, provider=None, window=None):
        plugin, judge = make(cfg, provider)

        async def run():
            session = judge.start("u", window or ["A：你好"])
            verdict = await judge.await_verdict("u")
            return plugin, judge, session, verdict

        return asyncio.run(run())

    def test_score_above_threshold_regens(self):
        plugin, _judge, session, verdict = self.run_judge(
            provider=FakeProvider('{"score": 88, "reason": "requirement_change"}'))
        self.assertEqual(verdict, VERDICT_REGEN)
        self.assertEqual(session.score, 88)
        self.assertEqual(session.reason, "requirement_change")
        row = plugin.rows[0]
        self.assertEqual(row["kind"], "judge")
        self.assertEqual(row["result"], "judging", "未结算前只写「判定中」")
        self.assertIn("score=88", row["detail"])
        self.assertGreater(row["id"], 0)

    def test_score_below_threshold_keeps(self):
        _plugin, _judge, session, verdict = self.run_judge(
            provider=FakeProvider('{"score": 10}'))
        self.assertEqual(verdict, VERDICT_KEEP)
        self.assertEqual(session.score, 10)

    def test_threshold_is_exclusive(self):
        _plugin, _judge, _session, verdict = self.run_judge(
            provider=FakeProvider('{"score": 60}'))
        self.assertEqual(verdict, VERDICT_KEEP, "等于阈值不算需要重生成")

    def test_provider_missing_falls_back(self):
        plugin, _judge, session, verdict = self.run_judge(provider=None)
        self.assertEqual(verdict, VERDICT_REGEN, "缺省失败策略是 regen")
        self.assertIsNone(session.score)
        self.assertIn("未配置判定模型", plugin.rows[0]["detail"])
        self.assertEqual(plugin.rows[0]["result"], "judging")

    def test_fail_mode_keep(self):
        _plugin, _judge, _session, verdict = self.run_judge(
            {"soft_judge_fail_mode": "keep"}, provider=None)
        self.assertEqual(verdict, VERDICT_KEEP)

    def test_unparsable_output_falls_back(self):
        plugin, _judge, _session, verdict = self.run_judge(provider=FakeProvider("嗯？"))
        self.assertEqual(verdict, VERDICT_REGEN)
        self.assertIn("嗯？", plugin.rows[0]["detail"], "原始输出应记进说明里")

    def test_provider_error_falls_back(self):
        plugin, _judge, _session, verdict = self.run_judge(
            provider=FakeProvider(raise_exc=RuntimeError("boom")))
        self.assertEqual(verdict, VERDICT_REGEN)
        self.assertIn("RuntimeError", plugin.rows[0]["detail"])

    def test_timeout_falls_back(self):
        plugin, _judge, _session, verdict = self.run_judge(
            {"soft_judge_timeout_ms": 500},
            provider=FakeProvider('{"score": 99}', delay=2.0))
        self.assertEqual(verdict, VERDICT_REGEN)
        self.assertIn("TimeoutError", plugin.rows[0]["detail"], "超时应记进说明")

    def test_prompt_uses_window(self):
        provider = FakeProvider('{"score": 10}')
        _plugin, judge, _session, _verdict = self.run_judge(
            provider=provider, window=["A：第一句", "B：第二句"])
        self.assertIn("A：第一句", provider.prompts[0])
        self.assertIn("B：第二句", provider.prompts[0])

    def test_restart_uses_new_window(self):
        provider = FakeProvider('{"score": 10}', delay=0.3)
        plugin, judge = make(provider=provider)

        async def run():
            first = judge.start("u", ["A：旧窗口"])
            await asyncio.sleep(0.05)
            second = judge.start("u", ["A：新窗口"])
            self.assertNotEqual(first.seq, second.seq)
            self.assertEqual(second.restarts, 1)
            verdict = await judge.await_verdict("u")
            return first, second, verdict, plugin

        first, second, verdict, plugin = asyncio.run(run())
        self.assertTrue(first.done)
        self.assertEqual(first.verdict, "restarted")
        self.assertEqual(verdict, VERDICT_KEEP)
        self.assertIn("A：新窗口", provider.prompts[-1])

    def test_max_restarts_keeps_running_judge(self):
        """重启次数用尽后：不再重判，但**不抛弃**正在跑的那次判定。"""
        provider = FakeProvider('{"score": 10}', delay=0.05)
        plugin, judge = make({"soft_judge_max_restarts": 1,
                              "soft_judge_fail_mode": "keep"}, provider)

        async def run():
            first = judge.start("u", ["1"])
            second = judge.start("u", ["2"])
            ignored = judge.start("u", ["3"])
            verdict = await judge.await_verdict("u")
            return first, second, ignored, verdict, judge

        first, second, ignored, verdict, judge = asyncio.run(run())
        self.assertEqual(second.restarts, 1)
        self.assertIs(ignored, second, "超过上限时应返回当前判定而不是新建")
        self.assertEqual(verdict, VERDICT_KEEP)
        self.assertEqual(second.window, ["2"], "保留的是最近一次已开始的判定")

    def test_await_verdict_has_no_global_timeout(self):
        """判定必须出结果才能继续——不能因为时间到了就放弃。"""
        provider = FakeProvider('{"score": 90}', delay=0.6)
        _plugin, judge = make({"soft_judge_timeout_ms": 5000}, provider)

        async def run():
            started = time.monotonic()
            judge.start("u", ["1"])
            verdict = await judge.await_verdict("u")
            return verdict, time.monotonic() - started

        verdict, elapsed = asyncio.run(run())
        self.assertEqual(verdict, VERDICT_REGEN)
        self.assertGreaterEqual(elapsed, 0.5)

    def test_logs_raw_output_and_reasoning(self):
        class ThinkingProvider(FakeProvider):
            async def text_chat(self, prompt=None, system_prompt=None, contexts=None):
                response = FakeResponse(
                    '{"score": 88, "reason": "info_supplement", "why": "补充了要求"}')
                response.reasoning_content = "先看用户是否改了需求，再看上下文"
                return response

        plugin, judge, session, verdict = self.run_judge(provider=ThinkingProvider())
        row = plugin.rows[0]
        self.assertEqual(row["result"], "judging")
        self.assertEqual(session.reason, "info_supplement")
        self.assertIn("[思考]", judge.raw_output(session))
        self.assertIn("先看用户是否改了需求", judge.raw_output(session))
        self.assertIn("[输出]", judge.raw_output(session))
        self.assertIn("补充了要求", judge.raw_output(session) + row["detail"])
        self.assertIn("A：你好", row["user_input"])

    def test_thinking_level_can_be_minimal(self):
        class P(FakeProvider):
            provider_config = {"id": "p1", "model": "deepseek-v4-flash-0731"}

        plugin, judge = make({"soft_judge_thinking_level": "minimal"}, P('{"score": 10}'))
        used = judge._provider_for_judge("u")
        self.assertEqual(used.provider_config["custom_extra_body"]["reasoning_effort"], "minimal")

    def test_restart_keeps_one_row(self):
        provider = FakeProvider('{"score": 10}', delay=0.5)
        plugin, judge = make(provider=provider)

        async def run():
            judge.start("u", ["旧"])
            await asyncio.sleep(0.05)
            judge.start("u", ["新"])
            await judge.await_verdict("u")

        asyncio.run(run())
        self.assertEqual(len(plugin.rows), 1, "重判不新开一行，一条判定只有一行")
        self.assertEqual(plugin.rows[0]["result"], "judging")
        self.assertIn("第 1 次重判", plugin.rows[0]["detail"])

    def test_thinking_level_override(self):
        class CloneCaptureProvider(FakeProvider):
            provider_config = {"model": "small", "custom_extra_body": {"temperature": 0.2}}

        provider = CloneCaptureProvider('{"score": 10}')
        _plugin, judge, _session, _verdict = self.run_judge(provider=provider)
        used = judge._provider_for_judge("u")
        self.assertIsNot(used, provider, "应使用浅拷贝，避免影响主模型请求")
        self.assertEqual(used.provider_config["custom_extra_body"]["reasoning_effort"], "none",
                         "默认思考等级是 off（对应 reasoning_effort=none）")
        self.assertEqual(used.provider_config["custom_extra_body"]["max_tokens"], 256,
                         "默认输出上限是 256")
        self.assertEqual(provider.provider_config["custom_extra_body"]["temperature"], 0.2)

    def test_cancel_clears(self):
        _plugin, judge = make(provider=FakeProvider('{"score": 10}', delay=5.0))

        async def run():
            judge.start("u", ["1"])
            judge.cancel("u")
            return judge.sessions

        self.assertEqual(asyncio.run(run()), {})


class FakeResult:
    def __init__(self, text):
        self.chain = [sys.modules["astrbot.api.message_components"].Plain(text=text)]
        self.async_stream = None


class FakeEvent:
    def __init__(self, text="", umo="aiocqhttp:GroupMessage:1", sender="u1"):
        self.unified_msg_origin = umo
        self.message_str = text
        self.message_obj = types.SimpleNamespace(
            message=[sys.modules["astrbot.api.message_components"].Plain(text=text)])
        self._sender = sender
        self._result = FakeResult(text)
        self.is_at_or_wake_command = True
        self.stopped = False

    def get_sender_id(self):
        return self._sender

    def get_sender_name(self):
        return self._sender

    def is_private_chat(self):
        return False

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


class TestProviderList(unittest.TestCase):

    class FakeProvider:
        def __init__(self, pid, model):
            self.provider_config = {"id": pid, "model": model, "type": "openai_chat_completion"}

        def get_model(self):
            return self.provider_config["model"]

    class Ctx:
        def __init__(self, providers):
            self.providers = providers

        def get_all_providers(self):
            return self.providers

    def test_lists_and_labels(self):
        plugin = JudgePlugin({**BASE, "soft_judge_provider_id": "qianfan-glm"})
        plugin.context = self.Ctx([
            self.FakeProvider("qianfan-glm", "glm-5.3-flash"),
            self.FakeProvider("qianfan-ds", "deepseek-v4-flash-0731"),
        ])
        single = type("P", (), {})()
        rows = plugin_main.Main.list_providers(single.__class__ and plugin) if False else None
        # 直接调用未绑定方法
        rows = plugin_main.Main.list_providers(plugin)
        self.assertEqual([row["id"] for row in rows], ["qianfan-ds", "qianfan-glm"])
        self.assertEqual(rows[1]["model"], "glm-5.3-flash")
        self.assertEqual(plugin_main.Main.judge_provider_label(plugin),
                         "glm-5.3-flash（qianfan-glm）")

    def test_empty_when_context_missing(self):
        plugin = JudgePlugin(BASE)
        plugin.context = None
        self.assertEqual(plugin_main.Main.list_providers(plugin), [])

    def test_label_falls_back(self):
        plugin = JudgePlugin({**BASE, "soft_judge_provider_id": ""})
        plugin.context = self.Ctx([])
        self.assertEqual(plugin_main.Main.judge_provider_label(plugin), "跟随当前会话模型")


class TestOutboundAwaitsJudge(unittest.TestCase):
    """旧回答完成时若判定未出结果，要等；判定要重答就作废。"""

    def make_plugin(self, gate_calls):
        class Gate:
            def suppress(self, event):
                gate_calls.append("suppress")
                event.get_result().chain = []
                event.stop_event()
                return True

            async def handle(self, event):
                gate_calls.append("send")

        plugin = plugin_main.Main(None, FakeConfig({
            "gate_enabled": True, "soft_judge_threshold": 60}))
        plugin.gate = Gate()
        plugin.rows = []
        plugin.log_event = lambda **row: plugin.rows.append(row)
        plugin._stop_active = staticmethod(lambda umo, event: 1)
        return plugin

    def test_suppresses_when_score_above_threshold(self):
        calls = []
        plugin = self.make_plugin(calls)
        event = FakeEvent("旧回答")
        session = types.SimpleNamespace(score=90, done=True, deadline=time.monotonic() + 5)
        plugin.inflight[event.unified_msg_origin] = [
            {"event": event, "sender_id": "u1", "started_at": time.time(),
             "judge": session},
        ]
        asyncio.run(plugin.on_outbound(event))
        self.assertEqual(calls, ["suppress"])
        self.assertEqual(plugin.rows, [], "旧回答被丢弃不另开一行（判定行已在结算时写成已作废/已打断）")

    def test_sends_when_score_below_threshold(self):
        calls = []
        plugin = self.make_plugin(calls)
        event = FakeEvent("旧回答")
        session = types.SimpleNamespace(score=10, done=True, deadline=time.monotonic() + 5)
        plugin.inflight[event.unified_msg_origin] = [
            {"event": event, "sender_id": "u1", "started_at": time.time(),
             "judge": session},
        ]
        asyncio.run(plugin.on_outbound(event))
        self.assertEqual(calls, ["send"])

    def test_no_judge_round_sends_directly(self):
        calls = []
        plugin = self.make_plugin(calls)
        event = FakeEvent("普通回答")
        asyncio.run(plugin.on_outbound(event))
        self.assertEqual(calls, ["send"])


if __name__ == "__main__":
    unittest.main()