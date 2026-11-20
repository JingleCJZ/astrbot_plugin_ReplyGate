"""运行开关的离线测试：面板接口（console/config）与配置持久化。

覆盖：
  - 运行开关的快照读取（health 里带 switches）
  - 白名单外 key 拒绝、value 类型归一
  - set_switch 写内存 + 写盘 + 记 config 审计流水
  - 落盘失败时降级为 applied_unsaved
"""

import asyncio
import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

STATE = {"body": {}, "query": {}}


def _install_stubs() -> None:
    def mod(name, **attrs):
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module

    class Star:
        def __init__(self, context=None):
            self.context = context

    class Request:
        query: dict = {}

        async def json(self, default=None):
            return STATE["body"] if STATE["body"] is not None else default

    def json_response(payload=None, **kwargs):
        return {"kind": "json", "payload": payload}

    def error_response(message, status_code=400, **kwargs):
        return {"kind": "error", "message": message, "status_code": status_code}

    class Filter:
        class EventMessageType:
            ALL = "all"

        @staticmethod
        def event_message_type(*_args, **_kwargs):
            return lambda fn: fn

        @staticmethod
        def on_decorating_result(**_kwargs):
            return lambda fn: fn

        @staticmethod
        def on_llm_request(**_kwargs):
            return lambda fn: fn

    mod("astrbot")
    mod("astrbot.api", logger=types.SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
        error=lambda *a, **k: None, exception=lambda *a, **k: None,
        debug=lambda *a, **k: None))
    mod("astrbot.api.event", AstrMessageEvent=object, MessageChain=object,
        filter=Filter)
    mod("astrbot.api.message_components", Plain=object, Image=object, At=object,
        Reply=object)
    mod("astrbot.api.star", Context=object, Star=Star, StarTools=object)
    mod("astrbot.api.provider", LLMResponse=object, ProviderRequest=object)
    mod("astrbot.api.web", json_response=json_response,
        error_response=error_response, request=Request())


_install_stubs()

from core.config import Config  # noqa: E402
import main as plugin_main  # noqa: E402
from webui import api as webui_api  # noqa: E402


class FakeConfig(dict):
    """模拟 AstrBotConfig：dict 语义 + 异步落盘。"""

    def __init__(self, data, fail=False):
        super().__init__(data)
        self.saves = 0
        self.fail = fail

    async def save_config_async(self):
        if self.fail:
            raise RuntimeError("disk full")
        self.saves += 1


def load_plugin(config, log=True):
    """构造真实的 Main 实例，把配置换成 FakeConfig，并截获 log_event。"""
    fake_config = FakeConfig(config)
    plugin = plugin_main.Main(None, fake_config)
    plugin.rows = []

    def log_event(**row):
        plugin.rows.append(row)

    if log:
        plugin.log_event = log_event
    return plugin


class FakeGate:
    holds = {}
    recycled = {}


class FakeDB:
    ready = True

    async def row_count(self):
        return 7

    def size_bytes_sync(self):
        return 4096


class FakePlugin:
    """webui 接口层用的最小插件桩。"""

    def __init__(self):
        self.cfg = Config({"gate_enabled": False, "gate_dry_run": True,
                           "silence_enabled": True,
                           "group_window_enabled": False,
                           "group_judge_provider_id": "p1"})
        self.gate = FakeGate()
        self.db = FakeDB()
        self.startup_warnings = []
        self.version = "1.0.0"
        self.calls = []
        self.options = {"buffer_quiet_ms": 1500}
        self.switches = {"gate_enabled": False, "gate_dry_run": True,
                         "silence_enabled": True, "group_window_enabled": False}

    def judge_prompt(self):
        import prompts
        text = self.cfg.str("judge_prompt_text", "")
        return prompts.effective_judge_prompt(text), bool(text.strip())

    def switch_snapshot(self):
        return dict(self.switches)

    def option_snapshot(self):
        return dict(self.options)

    @staticmethod
    def option_types():
        return {"bools": ["gate_enabled", "gate_dry_run", "silence_enabled",
                          "group_window_enabled"],
                "ints": {"buffer_quiet_ms": (0, 10000, 1500)},
                "strs": {"interrupt_mode": ["soft", "hard"],
                          "soft_judge_fail_mode": ["regen", "keep"]},
                "free_strs": {"soft_judge_provider_id": 200}}

    async def set_option(self, key, value):
        if key in self.option_types()["free_strs"]:
            if not isinstance(value, str) or len(value) > self.option_types()["free_strs"][key]:
                raise ValueError(key)
            self.calls.append((key, value.strip()))
            self.options[key] = value.strip()
            return {"key": key, "value": value.strip(), "persisted": True,
                    "switches": self.switch_snapshot(), "options": self.option_snapshot()}
        if key in self.option_types()["strs"]:
            allowed = self.option_types()["strs"][key]
            if value not in allowed:
                raise ValueError(key)
            self.calls.append((key, value))
            self.options[key] = value
            return {"key": key, "value": value, "persisted": True,
                    "switches": self.switch_snapshot(), "options": self.option_snapshot()}
        if key in self.option_types()["ints"]:
            self.calls.append((key, int(value)))
            self.options[key] = int(value)
            return {"key": key, "value": int(value), "persisted": True,
                    "switches": self.switch_snapshot(), "options": self.option_snapshot()}
        return await self.set_switch(key, value)

    async def set_switch(self, key, value):
        if key not in self.switches:
            raise KeyError(key)
        self.calls.append((key, bool(value)))
        self.switches[key] = bool(value)
        return {"key": key, "value": bool(value), "persisted": True,
                "switches": self.switch_snapshot()}


class TestMainSetSwitch(unittest.TestCase):

    def test_switch_persists_without_log_row(self):
        plugin = load_plugin({"gate_enabled": True, "gate_dry_run": True})
        result = asyncio.run(plugin.set_switch("gate_dry_run", False))
        self.assertFalse(result["switches"]["gate_dry_run"])
        self.assertTrue(result["persisted"])
        self.assertEqual(plugin.cfg.raw.saves, 1)
        self.assertEqual(plugin.cfg.raw["gate_dry_run"], False)
        self.assertEqual(plugin.rows, [], "按钮更改不进流水")

    def test_switch_takes_effect_immediately(self):
        plugin = load_plugin({"gate_enabled": False})
        self.assertFalse(plugin.cfg.enabled)
        asyncio.run(plugin.set_switch("gate_enabled", True))
        self.assertTrue(plugin.cfg.enabled)

    def test_unknown_key_rejected(self):
        plugin = load_plugin({})
        with self.assertRaises(KeyError):
            asyncio.run(plugin.set_switch("gate_whatever", True))

    def test_save_failure_is_reported(self):
        plugin = load_plugin({})
        plugin.cfg.raw.fail = True
        result = asyncio.run(plugin.set_switch("silence_enabled", False))
        self.assertFalse(result["persisted"])
        self.assertFalse(result["switches"]["silence_enabled"])
        self.assertEqual(plugin.rows, [], "按钮更改（包括落盘失败）都不写流水")


class TestConsoleConfigEndpoint(unittest.TestCase):

    def setUp(self):
        self.plugin = FakePlugin()
        STATE["query"] = {}

    def post(self, body):
        STATE["body"] = body
        return asyncio.run(webui_api.handle_config(self.plugin))

    def test_toggle_on(self):
        resp = self.post({"key": "group_window_enabled", "value": True})
        self.assertEqual(resp["payload"]["status"], "ok")
        self.assertEqual(resp["payload"]["data"]["switches"]["group_window_enabled"], True)
        self.assertEqual(self.plugin.calls, [("group_window_enabled", True)])

    def test_unknown_key(self):
        resp = self.post({"key": "../gate_enabled", "value": True})
        self.assertEqual(resp["kind"], "error")
        self.assertEqual(resp["status_code"], 400)
        self.assertEqual(self.plugin.calls, [])

    def test_string_value_normalized(self):
        self.post({"key": "gate_dry_run", "value": "off"})
        self.assertEqual(self.plugin.calls[-1], ("gate_dry_run", False))
        self.post({"key": "gate_dry_run", "value": "true"})
        self.assertEqual(self.plugin.calls[-1], ("gate_dry_run", True))

    def test_missing_value_rejected(self):
        resp = self.post({"key": "gate_enabled"})
        self.assertEqual(resp["kind"], "error")
        self.assertEqual(self.plugin.calls, [])

    def test_health_exposes_switches(self):
        STATE["body"] = {}
        resp = asyncio.run(webui_api.handle_health(self.plugin))
        data = resp["payload"]["data"]
        self.assertEqual(data["switches"], self.plugin.switch_snapshot())
        self.assertEqual(data["options"], self.plugin.option_snapshot())
        self.assertEqual(data["rows"], 7)

    def test_numeric_option_written(self):
        resp = self.post({"key": "buffer_quiet_ms", "value": 2300})
        self.assertEqual(resp["payload"]["status"], "ok")
        self.assertEqual(self.plugin.calls[-1], ("buffer_quiet_ms", 2300))

    def test_numeric_string_accepted(self):
        self.post({"key": "buffer_quiet_ms", "value": "1200"})
        self.assertEqual(self.plugin.calls[-1], ("buffer_quiet_ms", 1200))

    def test_provider_id_written(self):
        resp = self.post({"key": "soft_judge_provider_id", "value": "baiduqianfan/glm-5.3-flash"})
        self.assertEqual(resp["payload"]["status"], "ok")
        self.assertEqual(self.plugin.calls[-1],
                         ("soft_judge_provider_id", "baiduqianfan/glm-5.3-flash"))

    def test_provider_id_too_long(self):
        resp = self.post({"key": "soft_judge_provider_id", "value": "x" * 201})
        self.assertEqual(resp["kind"], "error")
        self.assertEqual(self.plugin.calls, [])

    def test_models_endpoint(self):
        self.plugin.list_providers = lambda: [
            {"id": "p1", "model": "glm-5.3-flash", "type": "openai"}]
        STATE["body"] = {}
        resp = asyncio.run(webui_api.handle_models(self.plugin))
        self.assertEqual(resp["payload"]["data"]["providers"][0]["model"], "glm-5.3-flash")

    def test_models_route_registered(self):
        routes = {(route, method) for route, method, _h, _d in webui_api.ROUTES}
        self.assertIn(("console/models", "GET"), routes)

    def test_mode_string_written(self):
        resp = self.post({"key": "interrupt_mode", "value": "hard"})
        self.assertEqual(resp["payload"]["status"], "ok")
        self.assertEqual(self.plugin.calls[-1], ("interrupt_mode", "hard"))

    def test_mode_invalid_rejected(self):
        resp = self.post({"key": "interrupt_mode", "value": "wild"})
        self.assertEqual(resp["kind"], "error")
        self.assertEqual(self.plugin.calls, [])

    def test_numeric_invalid_rejected(self):
        resp = self.post({"key": "buffer_quiet_ms", "value": True})
        self.assertEqual(resp["kind"], "error")
        self.assertEqual(self.plugin.calls, [])

    def test_route_registered(self):
        routes = {(route, method) for route, method, _h, _d in webui_api.ROUTES}
        self.assertIn(("console/config", "POST"), routes)
        self.assertIn(("console/health", "GET"), routes)

    def test_bind_wraps_exceptions(self):
        async def boom(_plugin):
            raise RuntimeError("kaboom")

        bound = webui_api._bind(self.plugin, boom)
        resp = asyncio.run(bound())
        self.assertEqual(resp["kind"], "error")
        self.assertEqual(resp["status_code"], 500)


class TestPluginMetadata(unittest.TestCase):
    """插件市场要求的关键元数据不能漂。"""

    def load_metadata(self):
        """不依赖 PyYAML：这些字段都是顶层标量，直接正则取。"""
        import re
        with open(os.path.join(ROOT, "metadata.yaml"), encoding="utf-8") as handle:
            text = handle.read()
        meta = {}
        for key in ("name", "display_name", "short_desc", "version", "author",
                    "repo", "astrbot_version"):
            match = re.search(rf'^{key}:\s*(.+)$', text, re.M)
            if match:
                value = match.group(1).strip().strip("\"'")
                # YAML 块标量（>- / |）在测试里只当作“有内容”
                meta[key] = "block" if value in (">-", ">", "|", "|-") else value
        meta["desc"] = "block" if re.search(r'^desc:\s*[>|]', text, re.M) else ""
        meta["views"] = [{"name": name}
                        for name in re.findall(r'^\s*-\s*name:\s*(\S+)\s*$', text, re.M)]
        return meta

    def test_plugin_name_matches_folder(self):
        folder = os.path.basename(ROOT)
        self.assertEqual(plugin_main.PLUGIN_NAME, folder)
        self.assertEqual(self.load_metadata()["name"], folder)

    def test_version_matches_code(self):
        """改版本号必须三处同步：metadata.yaml / main.VERSION / CHANGELOG。"""
        version = self.load_metadata()["version"]
        self.assertEqual(plugin_main.VERSION, version)
        with open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as handle:
            self.assertIn(f"## [{version}]", handle.read())

    def test_marketplace_fields_present(self):
        meta = self.load_metadata()
        for field in ("name", "desc", "version", "author", "display_name",
                      "short_desc", "repo", "astrbot_version", "views"):
            self.assertTrue(str(meta.get(field) or "").strip(), f"{field} 不能为空")
        self.assertLessEqual(len(str(meta["short_desc"])), 60, "短描述要能放进卡片")
        names = [view["name"] for view in meta["views"]]
        for name in names:
            page = os.path.join(ROOT, "views", name, "index.html")
            self.assertTrue(os.path.isfile(page), f"视图 {name} 缺少 views/{name}/index.html")

    def test_switch_defaults_cover_schema(self):
        import json
        with open(os.path.join(ROOT, "_conf_schema.json"), encoding="utf-8") as handle:
            schema = json.load(handle)
        for key in plugin_main.SWITCH_DEFAULTS:
            self.assertIn(key, schema, f"{key} 不在 _conf_schema.json 里")
            self.assertEqual(schema[key]["type"], "bool")

    def test_no_stale_docs(self):
        """架构与规划只允许写在 HANDOFF，README 不得再指向已删除的 DESIGN.md。"""
        self.assertFalse(os.path.exists(os.path.join(ROOT, "DESIGN.md")))
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as handle:
            self.assertNotIn("DESIGN.md", handle.read())


if __name__ == "__main__":
    unittest.main()
