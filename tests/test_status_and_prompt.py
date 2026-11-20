"""流水状态就地更新 + 提示词块编辑/还原。

覆盖：
  - HOLD 只产生一条流水：等待中 → 已发送 / 已取消（update 而不是新插一行）
  - update 失败时退化为补记一行（不丢审计）
  - 流水带 user_input / raw_output，老库自动补列
  - set_prompt_block 保存自定义块、还原默认，并落盘
  - console/prompt 接口：text / reset / 空文本拒绝
"""

import asyncio
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

STATE = {"body": {}}


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

    class Req:
        @staticmethod
        async def json(default=None):
            return STATE["body"] if STATE["body"] is not None else default

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
    mod("astrbot.api.web",
        json_response=lambda payload=None, **k: {"kind": "json", "payload": payload},
        error_response=lambda message, status_code=400, **k:
            {"kind": "error", "message": message, "status_code": status_code},
        request=Req())


_install_stubs()

from core.config import Config  # noqa: E402
from core.outbound import OutboundGate  # noqa: E402
from store.db import Database  # noqa: E402
import prompts  # noqa: E402
import main as plugin_main  # noqa: E402
from webui import api as webui_api  # noqa: E402

Plain = sys.modules["astrbot.api.message_components"].Plain


class FakeResult:
    def __init__(self, text):
        self.chain = [Plain(text=text)]
        self.async_stream = None


class FakeEvent:
    def __init__(self, text, umo="aiocqhttp:GroupMessage:1", sender="u1",
                 message_str="用户刚才说的话"):
        self.unified_msg_origin = umo
        self.message_str = message_str
        self._sender = sender
        self._result = FakeResult(text)
        self.stopped = False

    def get_sender_id(self):
        return self._sender

    def is_private_chat(self):
        return False

    def get_result(self):
        return self._result

    def stop_event(self):
        self.stopped = True

    def text(self):
        return "".join(comp.text for comp in self._result.chain)


class StorePlugin:
    """带真实 SQLite 的插件桩：insert/update 都走 Database。"""

    def __init__(self, config, db_path):
        self.cfg = Config(config)
        self.db = Database(db_path)
        asyncio.run(self.db.init())
        self.rows = []

    def log_event(self, **row):
        # 真实插件是 fire-and-forget 异步写；测试里用同步写保证顺序确定
        self.rows.append(row)
        self.db.insert_sync([{**self._base(), **row}])

    async def log_event_row(self, **row):
        row_id = await self.db.insert_one({**self._base(), **row})
        return row_id

    async def update_log_status(self, row_id, result, detail="", **fields):
        payload = {key: value for key, value in fields.items() if value is not None}
        if detail:
            payload["detail"] = detail
        return await self.db.update_status(row_id, result, **payload)

    def _base(self):
        import time
        return {"ts": time.time(), "kind": "error"}


BASE_CFG = {
    "gate_enabled": True,
    "gate_dry_run": False,
    "silence_enabled": True,
    "silence_private_enabled": True,
    "log_enabled": True,
}


class DBFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "events.db"

    def plugin(self, **overrides):
        return StorePlugin(dict(BASE_CFG, **overrides), self.db_path)

    def all_rows(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY id").fetchall()]
        finally:
            conn.close()


class TestHoldSingleRow(DBFixture):

    def test_hold_sent_updates_same_row(self):
        plugin = self.plugin()
        gate = OutboundGate(plugin)
        event = FakeEvent('回答\n%%HOLD%% {"reason":"waiting_explanation","wait":1}')
        asyncio.run(gate.handle(event))
        rows = self.all_rows()
        self.assertEqual(len(rows), 1, f"应只有一条流水，实际 {len(rows)}")
        self.assertEqual(rows[0]["result"], "sent")
        self.assertEqual(rows[0]["kind"], "hold")
        self.assertIn("按时发送", rows[0]["detail"])
        self.assertEqual(rows[0]["user_input"], "用户刚才说的话")
        self.assertIn("%%HOLD%%", rows[0]["raw_output"])

    def test_hold_cancelled_updates_same_row(self):
        plugin = self.plugin()
        gate = OutboundGate(plugin)
        event = FakeEvent('回答\n%%HOLD%% {"reason":"waiting_explanation","wait":5}')

        async def run():
            task = asyncio.ensure_future(gate.handle(event))
            await asyncio.sleep(0.2)
            gate.on_message(FakeEvent("补充", sender="u1", message_str="补充"))
            await task

        asyncio.run(run())
        rows = self.all_rows()
        self.assertEqual(len(rows), 1, "取消不应另插一行")
        self.assertEqual(rows[0]["result"], "cancelled")
        self.assertIn("打断", rows[0]["detail"])
        self.assertTrue(event.stopped)

    def test_pending_visible_during_wait(self):
        plugin = self.plugin()
        gate = OutboundGate(plugin)
        event = FakeEvent('回答\n%%HOLD%% {"reason":"waiting_content","wait":2}')

        async def run():
            task = asyncio.ensure_future(gate.handle(event))
            await asyncio.sleep(0.2)
            rows = self.all_rows()
            assert rows and rows[0]["result"] == "pending", rows
            await task

        asyncio.run(run())
        self.assertEqual(self.all_rows()[0]["result"], "sent")

    def test_update_failure_falls_back_to_new_row(self):
        plugin = self.plugin()
        gate = OutboundGate(plugin)
        event = FakeEvent('回答\n%%HOLD%% {"reason":"waiting_explanation","wait":1}')

        async def broken(*_a, **_k):
            return False

        plugin.update_log_status = broken
        asyncio.run(gate.handle(event))
        rows = self.all_rows()
        self.assertEqual([r["result"] for r in rows], ["pending", "sent"])


class TestLogColumns(DBFixture):

    def test_old_table_gets_new_columns(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,"
                     " kind TEXT NOT NULL, umo TEXT, gid TEXT, sender_id TEXT, sender_name TEXT,"
                     " reason TEXT, act TEXT, confidence INTEGER, wait_seconds INTEGER,"
                     " followup TEXT, ms INTEGER, model TEXT, result TEXT, detail TEXT)")
        conn.commit()
        conn.close()
        db = Database(self.db_path)
        asyncio.run(db.init())
        with sqlite3.connect(str(self.db_path)) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
        self.assertIn("user_input", columns)
        self.assertIn("raw_output", columns)

    def test_observe_rows_carry_inputs(self):
        plugin = self.plugin(gate_dry_run=True)
        gate = OutboundGate(plugin)
        event = FakeEvent('正文\n%%SILENCE%% {"reason":"goodbye"}', message_str="问个问题")
        asyncio.run(gate.handle(event))
        row = self.all_rows()[0]
        self.assertEqual(row["user_input"], "问个问题")
        self.assertIn("%%SILENCE%%", row["raw_output"])
        self.assertEqual(row["result"], "dry_run")

    def test_keyword_search_covers_inputs(self):
        plugin = self.plugin(gate_dry_run=True)
        gate = OutboundGate(plugin)
        asyncio.run(gate.handle(FakeEvent('正文\n%%SILENCE%% {"reason":"goodbye"}',
                                         message_str="独特关键词ABC")))
        rows, total = asyncio.run(plugin.db.query(keyword="独特关键词ABC"))
        self.assertEqual(total, 1)


class FakeConfig(dict):
    def __init__(self, data):
        super().__init__(data)
        self.saves = 0

    async def save_config_async(self):
        self.saves += 1


def make_main(config=None):
    fake = FakeConfig({"gate_enabled": True, "log_enabled": True, **(config or {})})
    plugin = plugin_main.Main(None, fake)
    plugin.rows = []
    plugin.log_event = lambda **row: plugin.rows.append(row)
    return plugin


class TestPromptBlockEditing(unittest.TestCase):

    def test_default_when_empty(self):
        plugin = make_main()
        text, custom = plugin.prompt_block()
        self.assertEqual(text, prompts.PROMPT_BLOCK)
        self.assertFalse(custom)

    def test_custom_is_used_and_persisted(self):
        plugin = make_main()
        result = asyncio.run(plugin.set_prompt_block("# 自定义\n只输出标记。"))
        self.assertTrue(result["is_custom"])
        self.assertIn("# 自定义", result["text"])
        self.assertTrue(result["persisted"])
        self.assertEqual(plugin.cfg.raw["prompt_block_text"], "# 自定义\n只输出标记。")
        self.assertEqual(plugin.cfg.raw.saves, 1)

    def test_reset_restores_default(self):
        plugin = make_main({"prompt_block_text": "# 自定义\n旧内容"})
        result = asyncio.run(plugin.set_prompt_block(None))
        self.assertFalse(result["is_custom"])
        self.assertEqual(result["text"], prompts.PROMPT_BLOCK)
        self.assertEqual(plugin.cfg.raw["prompt_block_text"], "")

    def test_custom_block_injected_once(self):
        plugin = make_main({"prompt_block_text": "# 自定义块\n内容"})

        class Req:
            system_prompt = "你是助手。"

        asyncio.run(plugin.on_llm_request(FakeEvent("x"), Req))
        self.assertTrue(Req.system_prompt.endswith("# 自定义块\n内容"))
        snapshot = Req.system_prompt
        asyncio.run(plugin.on_llm_request(FakeEvent("x"), Req))
        self.assertEqual(Req.system_prompt, snapshot)


class FakePromptPlugin:
    cfg = Config({"prompt_block_text": ""})
    calls = []

    def prompt_block(self):
        text = self.cfg.str("prompt_block_text", "")
        return prompts.effective_block(text), bool(text.strip())

    def persona_template(self):
        return prompts.PROMPT_BLOCK

    def judge_prompt(self):
        text = self.cfg.str("judge_prompt_text", "")
        return prompts.effective_judge_prompt(text), bool(text.strip())

    async def set_judge_prompt(self, text):
        self.calls.append(("judge", text))
        self.cfg.raw["judge_prompt_text"] = "" if text is None else text
        block, custom = self.judge_prompt()
        return {"text": block, "is_custom": custom, "persisted": True}

    async def set_prompt_block(self, text):
        self.calls.append(text)
        self.cfg.raw["prompt_block_text"] = "" if text is None else text
        block, custom = self.prompt_block()
        return {"text": block, "is_custom": custom, "persisted": True}


class TestPromptEndpoint(unittest.TestCase):

    def setUp(self):
        self.plugin = FakePromptPlugin()
        self.plugin.calls = []

    def post(self, body):
        STATE["body"] = body
        return asyncio.run(webui_api.handle_prompt(self.plugin))

    def test_save_text(self):
        resp = self.post({"text": "# 新块"})
        self.assertEqual(resp["payload"]["status"], "ok")
        self.assertTrue(resp["payload"]["data"]["is_custom"])
        self.assertEqual(self.plugin.calls, ["# 新块"])

    def test_reset(self):
        self.plugin.cfg.raw["prompt_block_text"] = "# 旧"
        resp = self.post({"reset": True})
        self.assertEqual(resp["payload"]["status"], "ok")
        self.assertFalse(resp["payload"]["data"]["is_custom"])
        self.assertEqual(self.plugin.calls, [None])

    def test_empty_text_rejected(self):
        resp = self.post({"text": "   "})
        self.assertEqual(resp["kind"], "error")
        self.assertEqual(self.plugin.calls, [])

    def test_markers_reports_custom_flag(self):
        self.plugin.cfg.raw["prompt_block_text"] = "# 自定义"
        STATE["body"] = {}
        resp = asyncio.run(webui_api.handle_markers(self.plugin))
        data = resp["payload"]["data"]
        self.assertTrue(data["block_custom"])
        self.assertEqual(data["default_template"], prompts.PROMPT_BLOCK)
        self.assertIn("# 自定义", data["persona_template"])

    def test_judge_target_written(self):
        resp = self.post({"target": "judge", "text": "# 判定模板"})
        self.assertEqual(resp["payload"]["status"], "ok")
        self.assertEqual(self.plugin.calls[-1], ("judge", "# 判定模板"))

    def test_judge_target_reset(self):
        resp = self.post({"target": "judge", "reset": True})
        self.assertEqual(resp["payload"]["status"], "ok")
        self.assertEqual(self.plugin.calls[-1], ("judge", None))

    def test_unknown_target_rejected(self):
        resp = self.post({"target": "wild", "text": "x"})
        self.assertEqual(resp["kind"], "error")

    def test_route_registered(self):
        routes = {(route, method) for route, method, _h, _d in webui_api.ROUTES}
        self.assertIn(("console/prompt", "POST"), routes)


if __name__ == "__main__":
    unittest.main()