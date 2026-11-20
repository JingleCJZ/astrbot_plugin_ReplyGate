"""网页接口：流水查询、统计、窗口快照、运行开关与运维动作。

路由前缀：/astrbot_plugin_ReplyGate/console/...
宿主实际暴露路径：/api/v1/plugins/extensions/astrbot_plugin_ReplyGate/console/...
页面：views/gate/index.html（AstrBot 插件视图机制托管，页面内必须走 AstrBotPluginView 桥）
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

Handler = Callable[[Any], Awaitable[Any]]


def _prompts():
    """包内相对导入优先；测试/单文件调试时回退到绝对导入。

    注意：不能写 `import prompts`——AstrBot 以 data.plugins.<name> 包加载时，
    顶层的 `prompts` 并不存在（这正是之前 "No module named 'core'" 的真凶）。
    """
    if __package__ and "." in __package__:
        from .. import prompts as module  # noqa: PLC0415 - 延迟导入避免循环
        return module
    import prompts as module  # noqa: PLC0415
    return module


def _ok(data: Any = None, message: str = ""):
    payload: dict[str, Any] = {"status": "ok"}
    if data is not None:
        payload["data"] = data
    if message:
        payload["message"] = message
    return json_response(payload)


def _fail(message: str, status_code: int = 400):
    return error_response(message, status_code=status_code)


def _bind(plugin: Any, handler: Handler):
    async def bound():
        try:
            return await handler(plugin)
        except Exception as exc:  # noqa: BLE001 - 面板接口不应抛到宿主
            logger.exception(f"[ReplyGate] 控制台接口失败: {handler.__name__}: {exc}")
            return _fail(f"接口异常：{type(exc).__name__}", 500)

    return bound


def _query_args() -> dict[str, Any]:
    try:
        return dict(request.query)
    except Exception:
        return {}


def _int_arg(args: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(float(args.get(key, default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


async def handle_logs(plugin: Any):
    args = _query_args()
    filters = {key: str(args[key]).strip()
               for key in ("kind", "umo", "gid", "sender_id", "reason", "act", "result")
               if args.get(key)}
    page = _int_arg(args, "page", 1, 1, 100000)
    size = _int_arg(args, "size", 50, 1, 500)
    since = until = None
    for key, target in (("since", "since"), ("until", "until")):
        raw = args.get(key)
        if raw:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if target == "since":
                since = value
            else:
                until = value
    rows, total = await plugin.db.query(
        page=page, size=size, filters=filters, since=since, until=until,
        keyword=str(args.get("q") or "").strip())
    return _ok({"rows": rows, "total": total, "page": page, "size": size})


async def handle_log_detail(plugin: Any):
    args = _query_args()
    try:
        row_id = int(args.get("id", 0))
    except (TypeError, ValueError):
        return _fail("id 需要整数")
    if row_id <= 0:
        return _fail("缺少 id")
    row = await plugin.db.detail(row_id)
    if row is None:
        return _fail("记录不存在", 404)
    return _ok(row)


async def handle_stats(plugin: Any):
    args = _query_args()
    days = _int_arg(args, "days", 1, 1, 365)
    today = await plugin.db.stats(1)
    window = await plugin.db.stats(days)
    return _ok({
        "days": days,
        "today": today,
        "window": window,
        "runtime": plugin.runtime_snapshot(),
    })


async def handle_window(plugin: Any):
    return _ok(plugin.window_snapshot())


async def handle_health(plugin: Any):
    cfg = plugin.cfg
    rows = await plugin.db.row_count()
    return _ok({
        "enabled": cfg.enabled,
        "dry_run": cfg.dry_run,
        "switches": plugin.switch_snapshot(),
        "options": plugin.option_snapshot(),
        "silence_enabled": cfg.silence_enabled,
        "prompt_block_inject": cfg.bool("prompt_block_inject", True),
        "group_window_enabled": cfg.bool("group_window_enabled", False),
        "judge_provider_id": cfg.str("group_judge_provider_id", ""),
        "rows": rows,
        "db_bytes": plugin.db.size_bytes_sync() if plugin.db.ready else 0,
        "db_ready": plugin.db.ready,
        "holds": len(plugin.gate.holds),
        "recycled": len(plugin.gate.recycled),
        "startup_warnings": list(plugin.startup_warnings),
        "version": plugin.version,
        "ts": time.time(),
    })


async def handle_config(plugin: Any):
    """切换开关或修改数值项（白名单），写回插件配置并返回新快照。

    布尔项直接从 JSON 读；数值项（缓冲秒数等）按 option_types() 的
    取值范围钳制，非法值回落到缺省值。
    """
    body: dict[str, Any] = {}
    try:
        body = await request.json(default={}) or {}
    except Exception:
        return _fail("请求体需要 JSON")
    if not isinstance(body, dict):
        return _fail("请求体需要 JSON 对象")
    key = str(body.get("key") or "").strip()
    types = plugin.option_types()
    raw_value = body.get("value")

    if key in types["bools"]:
        if isinstance(raw_value, bool):
            value: Any = raw_value
        elif isinstance(raw_value, (int, float)):
            value = bool(raw_value)
        elif isinstance(raw_value, str):
            value = raw_value.strip().lower() in {"1", "true", "yes", "on"}
        else:
            return _fail("value 需要布尔值")
        label = f"{key} = {'开' if value else '关'}"
    elif key in types.get("strs", {}):
        if not isinstance(raw_value, str):
            return _fail("value 需要字符串")
        allowed = types["strs"][key]
        value = raw_value.strip().lower()
        if value not in allowed:
            return _fail(f"value 只能是 {' / '.join(allowed)}")
        label = f"{key} = {value}"
    elif key in types.get("floats", {}):
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float, str)):
            return _fail("value 需要数字")
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            return _fail("value 需要数字")
        label = f"{key} = {value}"
    elif key in types.get("free_strs", {}):
        if not isinstance(raw_value, str):
            return _fail("value 需要字符串")
        value = raw_value.strip()
        if len(value) > types["free_strs"][key]:
            return _fail("value 过长")
        label = f"{key} = {value or '（默认）'}"
    elif key in types["ints"]:
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float, str)):
            return _fail("value 需要数字")
        try:
            value = int(float(raw_value))
        except (TypeError, ValueError):
            return _fail("value 需要数字")
        label = f"{key} = {value}"
    else:
        return _fail(f"未知配置项：{key or '（空）'}")

    try:
        result = await plugin.set_option(key, value)
    except KeyError:
        return _fail(f"未知配置项：{key}")
    except ValueError:
        return _fail(f"value 不合法：{value}")
    return _ok(result, label)


async def handle_window_end(plugin: Any):
    """运维：手动结束某个会话的窗口。"""
    body = {}
    try:
        body = await request.json(default={})
    except Exception:
        body = {}
    umo = str((body or {}).get("umo") or "").strip()
    closed = plugin.close_windows(umo)
    return _ok({"closed": closed, "umo": umo},
               "已结束窗口" if closed else "没有需要结束的窗口")


async def handle_logs_clear(plugin: Any):
    removed = await plugin.db.clear()
    return _ok({"removed": removed}, "流水已清空")


async def handle_markers(plugin: Any):
    """标记格式、生效的提示词块与注入状态。"""
    block, custom = plugin.prompt_block()
    judge_block, judge_custom = plugin.judge_prompt()
    return _ok({
        "silence_marker": plugin.cfg.str("silence_marker", "%%SILENCE%%"),
        "hold_marker": plugin.cfg.str("hold_marker", "%%HOLD%%"),
        "persona_template": block,
        "block_custom": custom,
        "default_template": plugin.persona_template(),
        "judge_template": judge_block,
        "judge_custom": judge_custom,
        "judge_default_template": _prompts().effective_judge_prompt(""),
        "inject_enabled": plugin.cfg.bool("prompt_block_inject", True),
        "inject_hook": "on_llm_request",
    })


async def handle_models(plugin: Any):
    """所有已配置的对话模型，供面板下拉选择判定模型。"""
    return _ok({"providers": plugin.list_providers(),
                "current": plugin.cfg.str("soft_judge_provider_id", "")})


async def handle_prompt(plugin: Any):
    """保存自定义提示词；target=system|judge，body={"reset": true} 或 {"text": ""} 还原默认。"""
    try:
        body = await request.json(default={}) or {}
    except Exception:
        return _fail("请求体需要 JSON")
    if not isinstance(body, dict):
        return _fail("请求体需要 JSON 对象")
    target = str(body.get("target") or "system").strip().lower()
    if target not in ("system", "judge"):
        return _fail("target 只能是 system 或 judge")
    setter = plugin.set_prompt_block if target == "system" else plugin.set_judge_prompt
    label = "系统提示词块" if target == "system" else "判定提示词"
    if body.get("reset"):
        result = await setter(None)
        return _ok(result, f"已还原为插件默认{label}")
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        return _fail("text 不能为空；要还原默认请用 reset")
    result = await setter(text)
    return _ok(result, f"{label}已保存")


ROUTES: list[tuple[str, str, Handler, str]] = [
    ("console/logs", "GET", handle_logs, "查询流水"),
    ("console/log", "GET", handle_log_detail, "读取单条流水详情"),
    ("console/stats", "GET", handle_stats, "读取统计"),
    ("console/window", "GET", handle_window, "读取窗口快照"),
    ("console/health", "GET", handle_health, "读取插件健康状态与开关"),
    ("console/markers", "GET", handle_markers, "读取标记格式、提示词块与注入状态"),
    ("console/prompt", "POST", handle_prompt, "保存或还原自定义提示词块"),
    ("console/config", "POST", handle_config, "切换运行开关/数值/判定模型"),
    ("console/models", "GET", handle_models, "列出所有已配置的对话模型"),
    ("console/window/end", "POST", handle_window_end, "手动结束窗口"),
    ("console/logs/clear", "POST", handle_logs_clear, "清空流水"),
]


def register(plugin: Any, context: Any, plugin_name: str) -> None:
    ok = 0
    for endpoint, method, handler, description in ROUTES:
        route = f"/{plugin_name}/{endpoint}"
        try:
            context.register_web_api(route, _bind(plugin, handler), [method], description)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ReplyGate] 注册 {method} {route} 失败: {exc}")
    logger.info(f"[ReplyGate] 已注册 {ok}/{len(ROUTES)} 条控制台路由（前缀 /{plugin_name}/console）")