# ReplyGate · 开发与交接文档（HANDOFF）

> 版本 **1.0.0** ｜ 最后更新：2026-11-18
> 读者：继续开发的 agent / 维护者。**用户视角的用法与配置见 [`README.md`](README.md)**，全部配置项以 `_conf_schema.json` 为准。
> 本文包含：架构与模块地图 · 契约与踩坑 · 里程碑与路线图 · 测试与发布流程 · 附录（原 `DESIGN.md` 详细设计规格）。

---

## 0. 新会话 5 步上手

1. 读 §1（架构）、§2（环境）、§5（契约与坑），对齐进度；
2. `cd D:/jingle/astrbot_plugin_ReplyGate && python -m py_compile main.py prompts.py core/*.py store/*.py webui/*.py`；
3. 跑全部测试：`for t in tests/*.py; do python "$t"; done`，再 `node tests/test_page_smoke.mjs`；
4. 改代码前先看 §5，那里每条都是踩过的坑；
5. 改完按 §4.2 重打包 zip，产出 `D:/jingle/astrbot_plugin_ReplyGate.zip`。

---

## 1. 架构

### 1.1 三层职责

```
① 判定层（可选，小模型）    判断"这算不算在跟机器人说话 / 值不值得重答"，不看图片像素
② 主模型                    正常回答，或用 %%SILENCE%% / %%HOLD%% 声明沉默与等待
③ 出站闸门（core/outbound）  解析标记 → 丢弃 / 暂存后发 / 定时催促 / 打断与重答 / 回收
```

- 判定层与出站闸门都只做"执行与记录"，**不生成文案**；
- 主模型永远是唯一产出自然语言的地方；
- 所有决策写入 SQLite（`store/db.py`），面板（`views/gate/index.html`）只读 + 改开关。

### 1.2 一次消息的完整链路（顺序是契约，不要改）

```
on_inbound(event)
  ├─ 命令旁路（/ 开头）                        → return
  ├─ 撤销 HOLD（立即）        gate.on_message()
  ├─ 记会话流水 + 未回复队列   _remember / _note_unanswered
  ├─ 打断 / 判定（立即）      _decide_interrupt → 强打断当场停；弱打断起判定模型
  └─ 缓冲交付（只影响主模型） _deliver_with_buffer → settle → _settle_interrupt
                                              ↓ 决定本条是否回答、是否重答
on_llm_request(event, req)   注入「沉默机制」块 + 未回复提示（幂等）
主模型生成……
on_decorating_result(event)  ① 等弱打断判定（压住回答）→ 需要重答则作废
                             ② 分句接管（可被打断）或直接交出站闸门
                             ③ gate.handle()：沉默 / 暂存 / 回收 / 正常发送
```

**判断"要不要回答 / 要不要打断"绝不能等缓冲**；缓冲只推迟主模型收到消息的时间。

### 1.3 模块地图

| 文件 | 职责 | 备注 |
|---|---|---|
| `main.py`（~60KB） | 插件入口、钩子编排、打断/判定结算、缓冲、分句、未回复队列、配置读写、provider 列表 | 核心，改动最多 |
| `core/outbound.py` | 出站闸门：解析标记、丢弃/暂存/催促、`suppress()` 作废 | 纯执行，不做判定 |
| `core/markers.py` | 标记解析（行首 + JSON + 围栏剥离），失败不吞消息 | 契约稳定 |
| `core/models.py` | 数据契约：`Marker`/`ParsedOutput`/`LogRow`、沉默与暂存原因、**判定原因与判定状态码** | 改原因要同步 `prompts.py` / `_conf_schema.json` |
| `core/buffer.py` | 防抖缓冲：`offer/settle/drop`，带 `seqs` | 纯逻辑 |
| `core/judge.py` | 判定模型：会话、重判、超时、失败策略、加速参数浅拷贝、`reason` 解析、判定行创建（只建行不结算） | 无跨包导入 |
| `core/commands.py` | 命令旁路判定（兼容 wake_prefix 被剥的场景） | |
| `core/config.py` | 配置读取与钳制 | |
| `store/db.py` | SQLite：建表、迁移、`insert_one`、`update_status`（可补 `reason/confidence/raw_output`） | |
| `webui/api.py` | 11 条控制台路由 | 页面唯一入口 |
| `views/gate/index.html` | 面板（单文件，含全部 JS/CSS） | 沙箱 iframe 必须走桥；AstrBot 优先扫 `views/`，`pages/` 是别名 |
| `prompts.py` | 沉默机制提示词块、判定提示词、注入工具 | 面板可覆盖，内置为默认 |
| `tests/` | 10 个 py 测试 + 1 个 node 冒烟 | 见 §4.1 |

### 1.4 判定（打断）状态机

```
消息到达 & 有在途回答 & 同用户(或开启了其它用户) & 开关开 & 在窗口内
   ├─ 强打断 hard（有预算）      → 停掉在途 → 判定行：已打断 / 已作废（原因=强打断，置信度 100）
   ├─ 强打断 hard（预算用尽）    → 降级走弱打断
   ├─ @ 点名 / 唤醒前缀          → 跳过判定 → 判定行：已打断（原因=用户要求，置信度 100）
   └─ 弱打断 soft                → 判定模型打分
         ├─ score > 阈值          → 重新回答 → 已打断 / 已作废 / 中途作废
         ├─ score ≤ 阈值          → 保留旧回答，本条不回答 → 不重发
         └─ 判定不可用            → 按 soft_judge_fail_mode：regen → 已作废(原因=判定不可用) / keep → 不重发
窗口外、开关关、不是当事人  → 什么都不做（也不写流水）
```

一条判定**只有一行**流水：判定任务先写 `judging`（判定中），结算时（`_settle_interrupt` / `_regen`）就地改状态并补齐 `reason` + `confidence` + `raw_output`；分句发到一半被打断时再改成 `partial_discarded`（中途作废）。

### 1.5 标记契约

见附录 A §5；简化版：

- 标记必须在**最后一行**，格式 `%%SILENCE%% {...}` / `%%HOLD%% {...,"wait":N}`；
- 解析失败不吞消息（正文照发，流水记 `error`）；
- SILENCE 原因：`goodbye` / `consent_trap` / `no_value` / `already_answered` / `user_busy` / `user_request`；
- HOLD 原因：`waiting_explanation` / `waiting_content` / `user_request`（撤销范围可配 `any` / `target`）。

---

## 2. 环境与路径

| 项 | 值 |
|---|---|
| 开发机（当前机器） | Windows，工作目录 `C:/Users/Jingle`，项目在 **`D:/jingle/astrbot_plugin_ReplyGate`** |
| 运行环境（**不是本机**） | 另一台 Windows（AstrBot 日志里出现 `C:\Users\Jingle\data\plugins\...`、`C:\Users\Jingle\AppData\Roaming\uv\tools\astrbot\...`）。本机无 `C:/Users/Jingle/data`、无 uv 工具，已核实 |
| AstrBot 版本 | 运行端 **4.29.4**；`metadata.yaml` 要求 `>=4.29.1` |
| 插件 id / 目录名 | `astrbot_plugin_ReplyGate`（必须与目录名一致，测试会断言） |
| 版本号 | `1.0.0`（`main.py:VERSION` 与 `metadata.yaml` 必须一致，有测试断言） |
| 插件市场 | 发布需要 GitHub 仓库 + `repo` 字段；压缩包 ≤ 16MB |
| 微信桥 | **AstrWechat 1.5.1**，源码在 `D:/jingle/Astrwechat/Astrwechat/1.5.1/`（另有多份旧版本目录，不要改错） |
| 桥↔AstrBot | OneBot v11（aiocqhttp），wechat → AstrBot 反向 WS |
| AstrBot 源码参考 | `/tmp/ab/AstrBot-master`（本机 clone，只读参考，**不要在它里面改东西**） |
| 可能用于部署的 SSH 主机 | `~/.ssh/known_hosts` 里有 `192.168.1.9` / `192.168.31.9`（未确认哪个是部署机） |

**部署方式**：AstrBot 面板「插件 → 安装/更新」上传 zip；安装前先删 `data/plugins/astrbot_plugin_ReplyGate`。

---

## 3. 实现状态、里程碑与路线图

### 3.1 里程碑

| 阶段 | 状态 | 内容 |
|---|---|---|
| M1 | ✅ 完成 | 出站闸门（`%%SILENCE%%` / `%%HOLD%%`）、催促语定时与撤销、回收池、SQLite 流水、网页看板、仅观察、运行开关 |
| M2 | ❌ 未实现 | 群聊唤醒窗口状态机 + 二元判定层（不看图）。配置项（`group_*`）已在 schema 里预留，逻辑未写 |
| M3 | 🟡 部分 | HOLD 已覆盖大部分；按 reason 细化未做完 |
| M4 | 🟡 部分 | 同用户打断（强/弱 + 判定模型 + 分句中断）已实现；**出站重生成门、回收注入未做** |
| M5 | ✅ 完成 | 插件内消息缓冲（只影响主模型交付） |

### 3.2 规模与校验值

- 配置项：`_conf_schema.json` **69** 个键（其中 17 个是为 M2/M4 预留、1.0.0 未生效，见 README「预留配置项」）；
- 控制台路由 **11** 条；面板分组 **5** 个；流水类型只剩 **5** 种（`judge/silence/hold/recycle/error`）；
- 测试：**240 个 Python 用例**（74 / 17 / 10 / 12 / 21 / 14 / 12 / 35 / 19 / 26）+ 1 个 node 冒烟；
- 发布包：**35** 个文件、约 164KB（无 `__pycache__`/`.pyc`/`.github`，条目分隔符统一 `/`）；插件市场限制 ≤16MB；
- CI：`.github/workflows/ci.yml`（Python 3.10/3.12 + Node 20，跑全部离线测试并产出 zip artifact）。

### 3.3 路线图（下一步，按建议顺序）

1. **AstrWechat mentioned 补丁**（M2 前置，改的是桥不是本插件）
   文件 `D:/jingle/Astrwechat/Astrwechat/1.5.1/bridge/bridge_core.py`，4 处约 6 行：
   1. `:641` buffer entry 初始化加 `"mentioned": False`；
   2. `:648` `entry["mentioned"] = entry.get("mentioned") or bool(is_explicit_mention)`；
   3. `:702-707` 出队时 `mentioned = bool(entry.pop("mentioned", False))`；
   4. `:780` 条件改为 `if mentioned or state.group_reply_mode == "mention" or config.ACTIVE_REPLY_ENABLED:`。
   配置：`group_reply_mode="all"`、`active_reply_enabled=false`、`buffer_seconds=0`。
   > 用户要求过"先不动 AstrWechat"，动手前确认。
2. **M2 判定层 + 窗口状态机**（落点 `main.py`，跑在缓冲之前）
   `@` 开窗 → 窗口内逐条小模型判定（`group_judge_provider_id` / `group_not_for_me_threshold` / `group_judge_fail_mode`）→ `directed` 时置 `is_at_or_wake_command=True`；终止条件 `streak` / TTL / 有效用户为空。
3. **出站重生成门**（M4 剩余，`regen_*` 已预留）
   在 `on_outbound` 里对"已生成的回答"再问一次小模型，判定丢弃则 `suppress()` 并重答。
4. **回收注入**（M4 剩余）
   `gate.take_recycled(umo)` 在下一轮 `on_llm_request` 里作为参考草稿注入（与未回复队列合并展示）。
5. **@ 无正文补占位**：AstrBot `has_input` 只认文本/图片，纯 `At` 事件不调模型；在 `on_inbound` 给这种情况补一句占位正文。
6. **分句兜底**：分句失败/切不出两段时可降级为整段发送（当前直接整段发，无流水）。
7. **其它**：流水统计卡加"判定平均耗时/失败率"；`group_*`/`regen_*` 在功能落地前不要暴露到面板。

---

## 4. 运行、测试、打包、发布

### 4.1 本地校验

```bash
cd D:/jingle/astrbot_plugin_ReplyGate

# 语法
python -m py_compile main.py prompts.py core/*.py store/*.py webui/*.py tests/*.py

# 全量测试（各自独立，无 astrbot 也能跑：测试内自带桩）
for t in tests/*.py; do echo "== $t"; python "$t"; done
node tests/test_page_smoke.mjs      # 面板冒烟：抓 "xxx is not defined" 这类前端错误
```

| 测试文件 | 覆盖 |
|---|---|
| `tests/test_buffer_and_interrupt.py` | 缓冲、打断、判定结算、分句、未回复队列 |
| `tests/test_soft_judge.py` | 判定模型会话、超时/失败策略、原因解析、提示词 |
| `tests/test_gate.py` / `test_markers.py` | 出站闸门、标记解析 |
| `tests/test_observe.py` | 仅观察模式 |
| `tests/test_status_and_prompt.py` | HOLD 一行到底、提示词块 |
| `tests/test_webui_switch.py` | 开关/接口、插件元数据一致性（目录名、版本号、schema 覆盖） |
| `tests/test_commands.py` / `test_db.py` / `test_prompt_block.py` | 命令旁路、SQLite、提示词注入 |
| `tests/test_page_smoke.mjs` | 面板脚本可执行、各分组渲染 |

### 4.2 打包 zip

```bash
cd D:/jingle && rm -rf astrbot_plugin_ReplyGate/__pycache__ astrbot_plugin_ReplyGate/*/__pycache__ && python - <<'PY'
import zipfile
from pathlib import Path
root = Path("astrbot_plugin_ReplyGate"); target = Path("astrbot_plugin_ReplyGate.zip")
target.unlink(missing_ok=True)
skip = {"__pycache__", ".github", ".git", "node_modules"}
with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
    for path in sorted(root.rglob("*")):
        if skip & set(path.parts) or path.suffix == ".pyc" or path.is_dir():
            continue
        z.write(path, f"astrbot_plugin_ReplyGate/{path.relative_to(root).as_posix()}")
print(len(zipfile.ZipFile(target).namelist()), "files")
PY
```

> 仓库里有 `.github/`（CI）与 `.gitignore`；打包时排除 `.github`，但保留 `LICENSE`（AGPL 分发要求）、`README`、`CHANGELOG`。

> 必须用 Python 打包，别用 `Compress-Archive`（会写反斜杠路径，插件市场/解压端会出问题）。

部署：删 `data/plugins/astrbot_plugin_ReplyGate` → 上传 zip → 重载插件 → 浏览器 **Ctrl+F5**。

### 4.3 发布到 GitHub / 插件市场

1. 版本号三处一起改：`metadata.yaml:version`、`main.py:VERSION`、`CHANGELOG.md` 新增条目（测试会断言前两者一致）；
2. `README.md` 只放**现有功能 / 配置项 / 部署方法**；架构、契约、路线图留在本文件；
3. 推送到 GitHub（仓库地址同时写进 `metadata.yaml:repo`）；
4. 到 <https://cloud.astrbot.app/publish> 提交仓库地址发布到插件市场（需要 AstrBot Cloud 账号）；
5. 发布前自检：`__pycache__`、`*.pyc`、`events.db`、`*.zip` 不进仓库（见 `.gitignore`）；zip ≤ 16MB。

---

## 5. 契约与坑（每条都是实际踩过的）

1. **包加载**：AstrBot 以 `data.plugins.<目录名>.<模块>` 加载。**禁止**顶层绝对导入兄弟模块（`import prompts` 会 `ModuleNotFoundError`，进而被旧兜底掩盖成 `No module named 'core'`）。统一写法：
   ```python
   if __package__:
       from .core.x import y
   else:            # 单文件调试
       from core.x import y
   ```
   `webui/api.py` 里用 `_prompts()`：`__package__` 含 `.` 才 `from .. import prompts`。
2. **面板沙箱**：iframe 是 `sandbox="allow-scripts allow-forms allow-downloads"`，**无 `allow-same-origin`** → 页面里 `fetch/localStorage/alert/confirm/prompt` 全部不可用。必须走 `window.AstrBotPluginView.apiGet/apiPost`。直接用浏览器打开 html 会提示"无法连接仪表盘桥"。
3. **仅观察**：只剥标记、正文照发；不丢弃/不暂存/不 sleep/不写回收池；只记一条 `dry_run` 流水。唯一边界：标记外无正文 → 不发。
4. **缓冲只影响主模型交付**：`on_inbound` 顺序固定为 `命令旁路 → 撤销 HOLD（立即）→ 记流水/未回复队列 → 打断判定（立即）→ 缓冲交付`。判断"要不要回答/打断"绝不能等缓冲。
5. **缓冲语义**：单次 `buffer_quiet_ms`（每来一条重置）+ 总上限 `buffer_max_wait_ms`（从第一条起算），后端强制"单次 ≤ 总上限"。
6. **打断窗口 = 激活窗口**：`interrupt_window_ms` 从**第一条打断消息**起算，**强/弱共用**；窗口外既不强打断也不开判定，**且不写流水**；回答发出或开始重答后计时归零。
7. **弱打断只判一次**：消息到达即起判定；主模型先完成则**压住回答等判定**（regen 丢弃 / keep 发送）。**不要**为了"补草稿"二次判定（曾经的 `soft_judge_use_draft` 已删除）。
8. **"不重答"不能只 stop**：`ProcessStage` 的条件是 `(result and not stopped) or not result`，空结果即使 stop 也会调模型；必须同时把 `is_at_or_wake_command=False`。
9. **私聊 wake ≠ 点名**：AstrBot 对每条私聊都设 `is_at_or_wake_command=True`。`_is_forced()` 只在群聊把 `At(自己)`/唤醒前缀算点名，否则私聊永远走成"强制重答"。@ 点名会跳过判定，直接停掉在途重答（原因=用户要求、置信度 100）。
10. **判定模型加速**：OpenAI 兼容层不支持按次传 `reasoning_effort`/`max_tokens` → 浅拷贝 provider 改 `provider_config["custom_extra_body"]`（不影响主模型）；服务商拒绝时退回普通请求。
11. **分句接管**：按 `segment_separator`（默认 `$$$`）切；**前后紧邻 `$` 的分隔符不切**（保护 `$$公式$$` 与相邻公式产生的 `$$$`）；段间结算判定，regen 丢弃剩余、keep 发完；每段发送前/后都查 `_segment_abort`。开启前必须关 AstrBot 的「分段回复」(`platform_settings.segmented_reply`)。
12. **流水**：判定行（`kind=judge`）必须带 `user_input`（判定窗口或触发消息）；一条判定**只有一行**（`log_event_row` 先写「判定中」→ `update_log_status` 就地改状态并补 `reason/confidence/raw_output`）；HOLD 同理一行到底；**分句事件与面板按钮/输入框改动不写流水**。状态色在面板 `STATUS_TONE`。
13. **判定原因必须落在固定集合**：模型只能在 `user_request` / `requirement_change` / `info_supplement` 里选（`core/models.py:JUDGE_REASONS`）；不认识的原因一律丢弃，避免流水里出现自由文本。自定义判定提示词没要求 `reason` 时 `prompts.build_judge_prompt` 会自动追加约束；模型仍没给时记 `unspecified`。
14. **提示词注入幂等**：系统提示词里已有 `# 沉默机制` 或 `【沉默与等待协议】` 就跳过；自定义块用首行判定。
15. **未回复队列**：生成期间到达的消息记 `_pending`；交付时把"更早、未被覆盖"的消息以 `【尚未回复的消息…】` 注入系统提示词（最多 8 条），注入后清空。

---

## 6. 配置与推荐值

### 6.1 运行开关（面板可点，`SWITCH_DEFAULTS`）

`gate_enabled=false`（总开关）、`gate_dry_run=true`（实际拦截=取反）、`silence_enabled=true`、`prompt_block_inject=true`、`command_bypass=true`、`group_window_enabled=false`（M2 占位，**面板不暴露**）、`interrupt_group_enabled=false`、`interrupt_private_enabled=false`、`interrupt_other_users=false`、`segment_control_enabled=false`（在「分句发送」组）

### 6.2 数值/枚举/自由字符串（面板输入框）

| 键 | 默认 | 说明 |
|---|---|---|
| `buffer_quiet_ms` / `buffer_max_wait_ms` | 1500 / 8000 | 单次 / 总上限，0 = 关闭缓冲 |
| `interrupt_window_ms` | 8000 | 激活窗口（强/弱共用） |
| `interrupt_mode` | `soft` | `soft` 判定 / `hard` 强制 |
| `interrupt_cooldown_count` | 3 | hard 次数预算（10 分钟） |
| `soft_judge_threshold` | 60 | 弱打断阈值（实测"改需求"常 85~95，纯附和 <30，建议 70~75） |
| `soft_judge_context_before` | 0 | 判定窗口往前多带几条 |
| `soft_judge_timeout_ms` | 4000 | **单次 provider 请求超时**，不是放弃判定 |
| `soft_judge_max_restarts` | 3 | 判定重判上限（超限保留当前判定） |
| `soft_judge_max_tokens` | 256 | 思考模型给 64 会把预算全花在 reasoning 上、正文为空 |
| `soft_judge_thinking_level` | `off` | off/minimal/low（面板滑动条） |
| `soft_judge_fail_mode` | `regen` | 判定不可用时重答 / 保留 |
| `soft_judge_provider_id` | `""` | 面板下拉，空=跟随当前会话模型 |
| `segment_delay_mode` | `log` | `log` / `linear` |
| `segment_log_base` | 2.6 | `log_底数(字数)` 秒 |
| `segment_linear_start_ms` / `segment_linear_rate` | 800 / 10 | `初始值 + 字数/增长率` 秒 |
| `segment_separator` | `$$$` | 分句标识（≤16 字符） |
| `hold_default_wait_seconds` | 10 | 与提示词模板一致 |

### 6.3 实测结论（千帆 tokenplan）

| 模型 | 图片输入 | 价格/1K（入/出） | 一次 ~500 token 回复 | 结论 |
|---|---|---|---|---|
| `deepseek-v4.1-flash-1017` | ✅ | 0.002/0.008 | 5.7s | **推荐主模型**（唯一支持图片、最便宜） |
| `deepseek-v4-flash-0731` | ❌ | 0.003/0.009 | — | 旧的纯文本 |
| `deepseek-v4-pro-0813` | ❌ | 0.009/0.027 | 10.6s | 重推理才用 |
| `glm-5.3-flash` | ✅ | 0.0008/0.0028 | 1.2s（判定任务） | **推荐判定模型**，思考=关 |
| `ernie-5.5-flash-preview` | ✅ | 0.001/0.004 | 2s | 备选，思考关不掉 |

- `reasoning_effort` 对该端点**有效但按模型而异**：glm 的 `none` 几乎完全关思考（21s→1.2s）；deepseek-v4-flash-0731 用 `none` 反而退化（9s、512 token 全花在思考、正文为空），要用 `minimal`。
- judge/provider 的坑：`baiduqianfan/glm-5.3-flash` 是 pi 的模型键格式，**不是** AstrBot provider id；判定必须用面板下拉里列出的 provider id。

---

## 7. 面板与接口

- 面板目录 `views/gate/index.html`（AstrBot 优先扫 `views/`，`pages/` 是别名）；`metadata.yaml:views` 声明为 `gate`。
- 分组（可折叠）：**插件总开关** / **沉默和等待** / **消息打断** / **分句发送** / **消息缓冲**；系统提示词块在「沉默和等待」，判定提示词在「消息打断」，「分句发送」收着分句开关/标识/间隔算法与参数。
- 「消息打断」组顺序：打断强度 → **超时（强/弱都生效）** → 判定设置（标「仅弱打断」）→ 判定模型（下拉，自动保存）→ 判定提示词。
- 「唤醒窗口」开关已从面板移除（`group_window_enabled` 配置键与后端快照保留，留给 M2），面板不再出现「（待实现）」类占位开关；面板文字不带里程碑标注。
- 统计卡（5 张）：**今日判定**（小字：已作废/已打断/中途作废/不重发）、**沉默/丢弃**、**HOLD 暂存**、**回收草稿**、**异常**。
- 路由（前缀 `/astrbot_plugin_ReplyGate`，宿主暴露为 `/api/v1/plugins/extensions/astrbot_plugin_ReplyGate/...`）：
  `console/logs`、`console/log`、`console/stats`、`console/window`、`console/health`、`console/markers`、`console/prompt`、`console/config`、`console/models`、`console/window/end`、`console/logs/clear`。
- `console/config` 支持 bool / int / float / 字符串枚举 / 自由字符串（判定模型 id、分句标识）。
- 面板**唯一**滑动条是判定思考等级；其余数值全是输入框。

---

## 8. 数据库与流水

- 表 `events`：`ts kind umo gid sender_id sender_name reason act confidence wait_seconds followup ms model result detail user_input raw_output`；老库启动时 `ALTER TABLE ADD COLUMN` 补 `user_input/raw_output`。
- `kind`（现在只产这五种）：`judge`（判定，**包含打断**）/ `silence` / `hold` / `recycle` / `error`。
  **分句事件与面板按钮/输入框改动都不进流水**；窗口外的消息也不记。老库里可能还有 `soft_judge/interrupt/regen/segment/config/...`，面板保留旧标签仅供回看。
- `result`（判定行）：`judging`（判定中）→ `discarded`（已作废）/ `interrupted`（已打断）/ `partial_discarded`（中途作废）/ `keep`（不重发）。
  判定行**一行到底**：判定任务先写 `judging`，结算时就地改状态并补齐 `reason` + `confidence`（`update_log_status` 支持补 `reason/confidence/raw_output/detail`）。
- 判定 `reason`：模型只能在 `user_request`（用户要求）/ `requirement_change`（需求取消/改变）/ `info_supplement`（补充信息）里选；不调模型的场合由插件写 `hard_interrupt`（强打断，置信度 100）/ `user_request`（@ 点名，置信度 100）/ `judge_unavailable`（判定不可用）/ `unspecified`（模型没给原因）。
- 其余 `result`：`pending/sent/held/cancelled/dry_run/passed/switch_off/refuse_passthrough/failed`；颜色：`judging/pending/held` 黄，`discarded/interrupted/partial_discarded/cancelled/failed` 红，`sent/keep` 绿，`dry_run` 蓝，其余灰。
- 详情页两块：**用户输入**（`user_input`）、**模型原始输出**（`raw_output`，判定行含 `[思考]…[输出]…`）。

---

## 9. 已知问题 / 待验证

1. **分句偶发"整条没分段"**：已加公式保护；但**分句不再写流水**，复测时只能从"回答未分段"本身看出，需要时临时加 logger（不要重新加回 `kind=segment`）。
2. **@ 无正文不触发模型**：AstrBot `internal.py` 的 `has_input` 只认文本/图片等，纯 `At` 事件不调模型。修法见 §3.3-5。
3. **AstrWechat 未打 mentioned 透传补丁**：`all` 模式下 bridge 不带 `At` 段，AstrBot 看不到 @；补丁见 §3.3-1。
4. **M2 未实现**：`group_*` 配置项全是占位；判定层/窗口状态机没有代码。
5. **判定草稿不可行**：非流式下拿不到生成中的正文/思考；只能"完成后补判"，但那要二次调用，已被否决。
6. **`recycle` 回收池只写不读**：`take_recycled()` 没有被调用（回收注入未做，见 §3.3-4）。
7. **群聊 hard 打断按 umo 停**：`request_agent_stop_all` 会连带停该会话其它在途任务。

---

## 10. 本机临时文件（可清理，仅调试产物）

| 路径 | 用途 | 可否删除 |
|---|---|---|
| `D:/jingle/astrbot_plugin_ReplyGate/` | 源码（本文件所在） | 否 |
| `D:/jingle/astrbot_plugin_ReplyGate.zip` | 当前发布包 | 每次重打包会覆盖 |
| `D:/jingle/astrbot_plugin_LivingGate.zip` | 旧名发布包 | 可删 |
| `%TEMP%/qf2.json` | 千帆 `/v2/models` 目录快照（31 模型） | 可删 |
| `%TEMP%/judge_probe*.json`、`main_probe.json`、`img_probe.json` | 模型延迟/打分/图片探测请求体 | 可删 |
| `%TEMP%/resp*.json`、`r.json`、`r2.json`、`body.json` | 上述探测的响应 | 可删 |
| `%TEMP%/pagebak/index.html` | 面板某个中间版本的备份 | 可删（保留也无害） |
| `%TEMP%/repro/`、`repro2/`、`verify_pkg/`、`verify2/` | 用 `data/plugins` 目录结构复现"包加载"导入问题 | 可删 |
| `%TEMP%/rg_page.js` | 面板脚本抽出来跑 `node --check` | 可删 |
| `%TEMP%/probe.py`、`schema_peek.txt`、`peek.json` | 杂项调试 | 可删 |
| `/tmp/ab/AstrBot-master` | AstrBot master 源码（只读参考） | 可删（需要时重新 clone） |
| `D:/jingle/Astrwechat/Astrwechat/1.1.0 … 1.5.1` | 桥的多版本源码；**实际运行 1.5.1** | 不要删 |
| `D:/jingle/Astrwechat/{ANALYSIS.md,PLAN-KVM-EASY.md,PLAN-V2.md,Windows10-final.xml,tools}` | 桥/虚拟化方案文档 | 不要删 |

> 注意：AstrBot 的运行日志与安装目录**不在本机**（本机无 `C:/Users/Jingle/data`、无 uv 工具），排查线上问题要让用户贴日志。

---

## 11. 变更时间线（由近到远，只记行为变化）

1. **2026-11-18（1.0.0 发布）**：定版本 1.0.0；面板目录 `pages/` → `views/`；补 `LICENSE`（AGPL-3.0）、`.gitignore`、`CHANGELOG.md`、GitHub Actions CI；`metadata.yaml` 补 `repo` / `support_platforms` / `tags` / `views`；README 收敛为「功能 + 配置 + 部署」，架构与路线图并入本文；删除 `DESIGN.md`（并入附录 A）；新增"版本号与 metadata 一致"测试。
2. **2026-11-18**：流水重构——**判定与打断合并成一种类型 `判定`**（一条判定一行：判定中 → 已作废/已打断/中途作废/不重发），补齐**原因**（用户要求/需求取消-改变/补充信息，模型固定集合里选；强打断与 @ 点名写固定值）与**置信度**；分句事件与面板按钮/输入框改动**不再进流水**；判定提示词要求输出 `reason`。
3. **2026-11-18**：面板去掉「唤醒窗口（待实现）」开关；分句相关设置（开关/标识/间隔算法/参数）从「消息打断」拆成独立分组 **「分句发送」**；面板与代码注释里不再出现 M1~M5 里程碑标注。
4. **2026-11-18**：修 `on_outbound` 里 `session.deadline` 引发的 `AttributeError`（导致"判定 90 但不拦截 + 两次判定"）；删除"补草稿重判"（判定只一次）；出站等判定结果再发；新增可配置分句标识 `segment_separator`；新增 `kind=segment` 诊断流水（**后在 1.0.0 删除**）；分句加"`$` 相邻不切"保护。
5. **2026-11-18**：判定设置面板重排（超时置顶、弱打断标记）；分句延迟两算法（对数/一次函数）；分段发送中被打断 = 生成中被打断；`interrupt_window_ms` 明确为强/弱共用。
6. **2026-11-17**：全量改名 LivingGate → ReplyGate；面板改走宿主桥；开关/数值/枚举接口；提示词块可编辑/还原；流水两列（用户输入/原始输出）+ 就地改状态；HOLD 一行到底。
7. **2026-11-16**：M5 缓冲（只影响主模型）+ M4 打断（hard/soft）+ 判定模型；`user_request` reason；命令旁路；DB 迁移。
8. **更早**：M1 出站闸门、SQLite、看板、仅观察；原生 `No module named 'core'` 导入事故的根因定位（包加载 + 兜底掩盖）。

---

## 12. 安全与敏感信息

- **不要**把 API Key / token 写进项目文件、README、日志截图、GitHub issue。
- 密钥位置（仅供定位，勿外传）：`~/.pi/agent/models.json`（千帆 `apiKey`）、AstrBot 面板里的 provider 配置、`~/.pi-web/server-config.json`（pi web-ui 的固定口令）。
- 插件自身不保存任何第三方密钥；判定/主模型都通过 AstrBot provider 调用。
- 公开仓库里的 `events.db`、`data/config/*_config.json` 可能含会话 id 与聊天内容：**不要提交**（`.gitignore` 已排除 `*.db`）。

---

## 13. 交接清单（下次会话开始前确认）

- [ ] 运行端 AstrBot 版本仍是 4.29.4（内部 API `active_event_registry`、`provider_config` 结构依赖它）；
- [ ] 用户当前主模型 = `deepseek-v4.1-flash-1017`，判定模型 = `glm-5.3-flash`（思考=关）；
- [ ] AstrWechat 仍是 `group_reply_mode=all`、`buffer_seconds=0`；
- [ ] 是否已打 §3.3-1 的桥补丁（决定 M2 能否看到 @）；
- [x] GitHub 仓库地址已确认为 `https://github.com/JingleCJZ/astrbot_plugin_ReplyGate`（已写进 `metadata.yaml:repo`，上线前确认仓库已建好且公开）；
- [ ] 最近一次复测的分句结论（分句不写流水，只能看实际发送段数）。

---

## 附录 A：详细设计规格（原 `DESIGN.md` v5）

> 以下是项目最初的设计规格原文，保留作契约与历史参考。**与当前实现冲突时，以正文 §1–§13 为准**（主要差异：流水类型已合并为 `judge`、分句与面板改动不再写流水、面板目录改为 `views/`、版本号 1.0.0）。

### ReplyGate 插件设计文档 v5（审计版）

> 状态：M1 已实现，M2–M4 待实现。本文件描述行为与契约。
> 变更记录：v5 把「催促语」统一到 HOLD（SILENCE 变成纯丢弃，无 wait）；
> 取消范围按原因拆分（等解释=仅目标用户，催促=任何人）；M1 落地。
> 项目目录：`D:/jingle/astrbot_plugin_ReplyGate/`
> 目标插件名：`astrbot_plugin_ReplyGate`
> 运行环境：AstrBot >= 4.29.1，平台 AstrWechat（微信个人号 / aiocqhttp）

---

#### 0. 部署前提（检查表）

| # | 项 | 要求 | 原因 |
|---|---|---|---|
| 1 | `provider_settings.streaming_response` | **必须关闭** | 流式下 `on_decorating_result` 不保证生效（AstrBot 源码明确 warn） |
| 2 | 「主动回复概率」`provider_ltm_settings.active_reply` | **必须关闭** | 它绕过 pipeline 独立发送 → 双发 |
| 3 | `astrbot_plugin_LivingHello` | 停用 | 两套拦截互掐 |
| 4 | `platform_settings.segmented_reply` | 维持现有 `$$$` 配置 | 分段与对数延迟由 AstrBot 负责，插件不碰文本 |
| 5 | 相邻消息合并 | **由 ReplyGate 负责**（M5） | AstrWechat `buffer_seconds` 必须设 0；插件 `buffer_quiet_ms` 默认 1500 |

---

#### 1. 三层职责切分

| 层 | 触发 | 只负责 | 产出 | 影响终止/集合 |
|---|---|---|---|---|
| **判定层** | 群聊窗口内每条消息 | 这条消息**是不是对模型说的**（**不看图片内容**） | `directed` / `not_for_me`（带 confidence） | 只有高置信 `not_for_me` 影响 |
| **主模型** | 被唤醒后 | 正常回复 或 沉默/延迟标记 | `waiting_content` / `waiting_explanation` / `goodbye` / `consent_trap` / `no_value` / `already_answered` / `user_busy` | **完全不影响** |
| **出站闸门** | 每轮发送前 | 解析标记 → 丢弃 / 暂存后发 / 定时兜底 / 被打断 | — | 不影响 |

私聊不过判定层，每条都唤醒主模型；沉默与延迟只由主模型标记 + 出站闸门处理。

---

#### 2. 功能清单（审计用）

| ID | 功能 | 默认 | 开关 |
|---|---|---|---|
| F1 | 群聊唤醒窗口：@ 唤醒后，窗口内每条消息过判定层 | 关 | `group_window_enabled` |
| F2 | 终止：`not_for_me` 连续 N 条 / 有效用户集合空 / 窗口超时 | — | `group_streak_limit` / `group_window_ttl_seconds` |
| F3 | 判定置信度阈值（低置信不丢弃） | 70 | `group_not_for_me_threshold` |
| F4 | 主模型沉默标记：丢弃类 | 开 | `silence_enabled` + 每个 reason 开关 |
| F5 | 主模型等待解释标记：暂存正文后发 | 开 | `silence_allow_waiting_explanation` |
| F6 | 等待兜底定时器（催促语）+ 新消息取消 | 开 | `silence_allow_waiting_content` / `silence_cancel_scope` |
| F7 | 同用户新消息打断在途生成并重新生成（**M4 已实现**） | 关 | `interrupt_private_enabled` / `interrupt_group_enabled` / `interrupt_mode` / `interrupt_window_ms` / `interrupt_cooldown_count` |
| F15 | 插件内消息缓冲（**只影响主模型交付**；M5 已实现） | 开 | `buffer_quiet_ms`（单次，0 = 关）/ `buffer_max_wait_ms`（总上限） |
| F8 | 出站重生成闸门（丢弃旧回答 / 小模型判断） | 关 | `regen_private_enabled` / `regen_group_enabled` / `regen_mode` |
| F9 | 被丢弃的**完整回答**作为参考注入下一轮 | 开 | `recycle_discarded_answer` |
| F10 | 判定层不看图片（只看"有 N 张图"+上下文） | 是 | `judge_ignore_images`（关掉才走旧行为） |
| F11 | 仅观察 / 实际执行（面板开关反向：关闭=仅观察；仅观察只剥标记、正文照发、记一条流水） | 仅观察 | `gate_dry_run` |
| F11c | 提示词块面板内编辑 / 还原（存 `prompt_block_text`） | 默认文本 | `prompt_block_text` |
| F11b | 命令消息旁路（`/` 开头不进判定层、不撤销 HOLD） | 开 | `command_bypass` |
| F12 | 网页日志看板（SQLite） | 开 | `log_enabled` |
| F12b | 提示词块自动注入（`on_llm_request` 追加 `req.system_prompt`，幂等） | 开 | `prompt_block_inject` |
| F13 | 判定层每小时限额 | 60 | `group_judge_hourly_limit` |
| F14 | 全部参数网页面板可调 | — | 全部走 `_conf_schema.json` |

---

#### 3. 群聊状态机

```
[群消息] handler（priority = maxsize - 1，最后执行；不 stop、不置位，除非决定唤醒）
  │
  ├─ 插件未启用 / 该会话未启用 → return（完全不干预）
  │
  ├─ 是唤醒消息（is_at_or_wake_command == True）
  │     → open_window(waker = sender)；valid_users = {waker}；streak = 0
  │
  ├─ 存在 in-flight 回答 且 sender == 该回答的目标用户（F7 开启）
  │     → 打断在途生成（见 §6），并置 is_at_or_wake_command = True（重新生成）
  │
  ├─ 窗口未激活 / 已终止 → return
  │
  └─ 窗口内 → 判定层（1 次小模型调用，受 F13 限额，**不看图片像素**）
        ├─ directed（任意置信度）        → streak = 0；用户入集合；置位唤醒主模型
        ├─ not_for_me 且 conf ≥ 阈值     → streak + 1；用户出集合；不唤醒；查终止
        └─ not_for_me 但 conf < 阈值     → 保守 = 按 directed 处理（记 low_confidence_passthrough）

[终止] streak ≥ group_streak_limit / valid_users 为空 / 超过 TTL(300s)
  → active = False；记 window_close(trigger=streak|empty_set|ttl)；直到下次 @ 唤醒
```

**安全点**：插件从不把 `is_at_or_wake_command` 置 false，只在不干预时保持原值。

---

#### 4. 判定层契约（不看图片）

##### 4.1 输入

1. 二元判定规则（含图片消息专项规则，见下）
2. 当前消息：**文本、图片数量（N 张）、发送者昵称/ID、是否 @、是否引用机器人**
3. 最近 N 条群流水（默认 8，含机器人发言）
4. 窗口元信息：唤醒者、窗口已持续秒数、当前有效用户昵称列表

**绝不传入图片 URL / base64 / 摘要**（F10）。

##### 4.2 图片消息专项规则（写进 prompt）

- 上下文显示用户在跟机器人对话（上一轮是机器人→该用户，或该用户刚才 @ 过 / 提到机器人）→ **directed**
- 上下文显示用户预告过要给机器人看东西（"一会儿给你看张图"），随后到达的图片 → **directed**
- 上下文显示该用户正在跟别人聊话题、图片明显是发给别人的（紧接别人发言、点名别人）→ `not_for_me`
- 无法判断时 → **directed**（交给主模型，主模型有像素，可自行沉默/等待/回复）

##### 4.3 输出

```json
{"act":"directed|not_for_me","confidence":88,"why":"≤50字，仅进日志"}
```

##### 4.4 失败策略

| 场景 | 处理 |
|---|---|
| 超时 / 网络错误 | `group_judge_fail_mode`（默认 `not_for_me`：省钱不刷群；可切 `directed`：不漏话） |
| JSON 解析失败 | 1 次格式纠正重试（共享同一超时预算）→ 仍失败按 fail_mode |
| 超额 / 无 provider | 视为 fail_mode，记 `error(quota)` / `error(no_provider)` |

---

#### 5. 主模型标记契约（三种，均在最后一行）

##### 5.1 正常回复

```
<正文>
$$$ <第二段正文>
```

##### 5.2 `%%SILENCE%%`：丢弃（真沉默，永久）

```
%%SILENCE%% {"reason":"goodbye"}
```

| reason | 语义 |
|---|---|
| `goodbye` | 双方已互相告别 |
| `consent_trap` | 抗议："回复就算同意"而你不愿同意（可配置为回一句拒绝） |
| `no_value` | 纯表情 / 刷屏 |
| `already_answered` | 已答过同一件事 |
| `user_busy` | 对方在忙 / 要安静（私聊为主） |

SILENCE **没有 wait、没有后续发送** —— 永久不出声，直到下一条消息。

##### 5.3 `%%HOLD%%`：暂存正文，延后发送（F5）

```
<正文>
%%HOLD%% {"reason":"waiting_explanation","wait":5}
```

HOLD **保留正文**，先暂存 `wait` 秒，期间被打断则丢弃，否则按时发送（仍走 AstrBot 原生分段与对数延迟）。
两种用途共用同一机制：

| reason | 正文是 | 被打断范围（默认） | 打断后 |
|---|---|---|---|
| `waiting_explanation` | 对图片/内容的回答 | `target`（仅发图的人补话） | 丢弃正文 → 新消息重新回答 |
| `waiting_content` | 催促语（如"哎，你不是要发吗"） | `any`（任何人开口） | 撤销催促，不再发 |

> 催促语走 HOLD 而不是 SILENCE：它本来就要"延后发一句"，而不是"永久不说"。
> 这样绝大多数场景是 HOLD，SILENCE 只处理真正的"不该出声"。

##### 5.4 沉默机制提示词块（插件自动注入）

源文本在 `prompts.PROMPT_BLOCK`，由 `on_llm_request` 钩子追加到 `req.system_prompt` 末尾：

- 开关：`prompt_block_inject`（默认开）
- 幂等：`req.system_prompt` 里已有 `# 沉默机制` 或旧的 `【沉默与等待协议】` 就跳过，避免重复叠字
- 失败兜底：注入异常只记 `error` 流水，不影响正常回复
- 人格设定不需要再粘贴这段文本；面板「系统提示词块」展示同一内容，便于核对/复制

不选 AstrBot 的 Skills（`SKILL.md`）或 FunctionTool 的原因：Skills 只在 agent runtime 的 tool-loop 路径以 `<skills_instructions>` 注入且依附人格；FunctionTool 要求模型改用工具调用表达沉默，与行首标记协议冲突。

```
# 沉默机制
## 想完全不出声时（永久沉默），只输出一行、不要带正文：
%%SILENCE%% {"reason":"<原因>"}
可用原因：
- goodbye：双方已经互相告别过（你说了拜拜，对方也回了拜拜）
- consent_trap：对方用「你回复这条就算你同意」之类的话逼你表态，而你不愿同意
- no_value：纯表情、刷屏、没有信息量
- already_answered：同一件事你已经答过
- user_busy：对方明显在忙，或要求安静

## 想先等一等再决定（延迟发送），正常写正文，然后在最后另起一行：
%%HOLD%% {"reason":"<原因>","wait":<秒>}
可用原因：
- waiting_explanation —— 对方发了图片/内容但没说想让你做什么：
   你先按内容正常回答，标记等待 5 秒左右；对方补充解释就丢掉这条重新回答，
   没解释就按原回答发出。
- waiting_content —— 对方说了要发东西却一直没发：
   此时正文就是催促语（简短一句，例如「哎，你不是说要发吗」），等待 10 秒左右；
   对方在等待期间发来内容，这条催促会被自动撤销。

## 不想沉默就正常说话，不要输出任何标记。
```

##### 5.5 解析规则

- 标记必须是**最后一行**，且 `%%SILENCE%%` / `%%HOLD%%` 后接合法 JSON
- 解析失败 → 剥离疑似标记行、**正文照常发送**（HOLD 视为普通回复）
- 正文可省略（纯 SILENCE）；HOLD 必须有正文

---

#### 6. 打断与重新生成（F7）

##### 6.1 触发条件（全部满足）

1. 该 umo 存在 in-flight 回答（`on_llm_request` 时打点 `answer_started_at`）
2. 新消息发送者 == 该回答的目标用户（私聊即对方；群聊即当初触发回复的人）
3. 时间在 `interrupt_window_ms` 内（默认 8000）
4. 对应开关开启（`interrupt_private_enabled` / `interrupt_group_enabled`）

##### 6.2 打断实现（两种，配置选择）

| 模式 | 做法 | 代价 |
|---|---|---|
| `soft`（默认） | 开**判定模型**读"生成点→最新"的消息窗口打分；分数 > `soft_judge_threshold` → `gate.suppress()` 丢弃旧回答 + 停掉在途 + 新消息重答；否则保留旧回答、新消息不单独回答 | 多一次小模型调用（~0.5-2s）；判定期间回答被压住 |
| `hard` | 不判定，直接 `active_event_registry.request_agent_stop_all(umo, exclude=新事件)`；残答同样标记作废 | 真正停止等待；但**按 umo 生效**，会连带停止该会话里其它活跃事件（群聊慎用）；依赖内部 API |

**弱打断的时序契约**（`core/judge.py`）：

1. 新消息到达 → `SoftJudge.start()`：取消上一次未完成的判定（`verdict=restarted`），用新窗口重开一个 asyncio 任务；
2. 两个角色等同一个结果：新事件（`on_inbound`，决定是否置位唤醒）、旧事件（`on_decorating_result`，决定发送还是作废）；
3. 判定期间主模型已生成完毕 → `on_decorating_result` **await 判定**，不立即发送；
4. `soft_judge_timeout_ms` 只约束**单次 provider 请求**（超时/异常才走失败策略）；`soft_judge_max_restarts` 用尽后**保留当前判定**、不再重判（不抛弃结果）；
5. 激活窗口 `interrupt_window_ms` 从**第一条打断消息**起算（强弱共用）：窗口内才接受新的判定触发；窗口关闭后既不强打断也不开判定，回答/剩余分段照常发送；已在跑的判定仍然决定这条回答发不发；回答解决后窗口归零；
5b. 分句发送（`segment_control_enabled`）：`log` = log_`segment_log_base`(字数) 秒；`linear` = `segment_linear_start_ms` + 字数 / `segment_linear_rate`；每段之间结算判定，行为与"生成中被打断"等同；
5c. 判定只调用一次：回答先完成时由 `on_decorating_result` **压住**等判定结果；不为了补草稿而重判；分句标识 `segment_separator`（默认 `$$$`）可配；
6. 加速：浅拷贝 provider，把 `reasoning_effort`（面板滑动条：off/minimal/low，默认 off）与 `max_tokens`（默认 256）写进 `custom_extra_body`，不动主模型；服务商拒绝时退回普通请求；
6b. 判定模型由面板下拉选择（`console/models` 列出所有已配置对话模型），存 `soft_judge_provider_id`；留空 = 跟随当前会话模型；
7. 每次启用判定模型都写一条 `soft_judge` 流水（`confidence`=分数，`result`=regen/keep/restarted，`raw_output` 含思考内容，`user_input`=判定窗口）。

**为什么"不重生成"不能只 stop_event**：`ProcessStage` 调模型的条件是 `(event.get_result() and not event.is_stopped()) or not event.get_result()`，空结果时即使 stop 了也会调模型；所以必须同时把 `is_at_or_wake_command` 置为 False。

##### 6.3 群聊防"被打断风暴"

- 只有**同一发送者**能触发打断；群内其他人发消息不影响
- hard 连续触发打断时，全局计数 `interrupt_cooldown_count`（默认 3 次/10 分钟）后降级为先判定（soft）
- 群聊默认 `soft`，`hard` 需显式开启

---

#### 7. 被丢弃回答的回收（F9）

- 产生回收素材的场景：`HOLD` 暂存正文被新解释取代、`regen` 丢弃的旧答案、`soft` 打断后被丢弃的答案
- 存储：按 umo 存 **一次**（覆盖旧的），`recycle_discarded_answer=true` 时生效
- 注入：下一次 `on_llm_request` 里一次性消费，追加为不可信参考：
  ```
  <discarded_draft untrusted="true" stale="true">
  （上一条未采用的草稿，可能已过期，仅作参考，不要照抄）
  <草稿前 600 字>
  </discarded_draft>
  ```
- 开关：`recycle_discarded_answer`（默认开）、`recycle_max_chars`（默认 600）
- 明确限制：只在**已生成完成**的文本上生效；生成中的打断没有可回收内容

---

#### 8. 配置项全表（`_conf_schema.json`）

##### 8.1 总控

| key | 类型 | 默认 | 范围 |
|---|---|---|---|
| `gate_enabled` | bool | false | |
| `gate_dry_run` | bool | **true** | 仅观察：只剥标记，不做任何拦截 |
| `command_bypass` | bool | **true** | `/` 开头（取自 `wake_prefix`）的消息旁路判定层 |
| `buffer_quiet_ms` | int | 1500 | 单次缓冲时间（防抖，0 = 关闭） |
| `buffer_max_wait_ms` | int | 8000 | 最大总缓冲时间（从第一条起算，必须 ≥ 单次） |

| `buffer_max_wait_ms` | int | 8000 | 一批最多等待 |
| `segment_control_enabled` | bool | **false** | 分句由插件接管（可被新消息打断） |
| `segment_separator` | string | `$$$` | 分句标识（可配，≤16 字符） |
| `segment_delay_mode` | string | `log` | `log` = 对数；`linear` = 一次函数 |
| `segment_log_base` | float | 2.6 | 对数底数 |
| `segment_linear_start_ms` | int | 800 | 一次函数初始延迟 |
| `segment_linear_rate` | int | 10 | 一次函数增长率（字/秒） |
| `interrupt_mode` | string | `soft` | `soft` = 旧回答作废丢弃（不取消生成）；`hard` = 取消在途执行 + 残答丢弃（面板「强/弱打断」按钮切换） |
| `interrupt_window_ms` | int | 8000 | 打断窗口：在途回答开始后的这段时间内才算打断（面板输入框按秒改） |
| `prompt_block_text` | string | `""` | 面板编辑后的自定义提示词块；空 = 内置默认 |
| `prompt_block_inject` | bool | **true** | 插件自动注入「沉默机制」块；关闭后自行放进人格 |
| `log_enabled` | bool | true | |
| `log_retention_days` | int | 14 | 1–365 |
| `log_max_rows` | int | 50000 | 1000–500000 |

##### 8.2 群聊窗口与判定层

| key | 类型 | 默认 | 范围 | 说明 |
|---|---|---|---|---|
| `group_window_enabled` | bool | false | | F1 |
| `group_streak_limit` | int | 8 | 1–50 | `not_for_me` 连续条数 |
| `group_window_ttl_seconds` | int | **300** | 0–7200 | 0=不限 |
| `group_context_messages` | int | 8 | 0–30 | 判定流水条数 |
| `group_judge_provider_id` | string | "" | | 留空=会话主模型 |
| `group_judge_timeout_seconds` | float | 8 | 1–30 | |
| `group_judge_hourly_limit` | int | 60 | 0–600 | 0=不限 |
| `group_judge_fail_mode` | string | `not_for_me` | `not_for_me`/`directed` | |
| `group_not_for_me_threshold` | int | **70** | 0–100 | 低于此不丢弃 |
| `group_seed_is_waker` | bool | true | | 开窗 seed |
| `group_channels` | list | [] | | 生效会话白名单，空=全部群 |
| `judge_ignore_images` | bool | **true** | | F10；关掉则回退到"带图片摘要/索图"旧逻辑 |

##### 8.3 沉默 / 等待 / 兜底

| key | 类型 | 默认 | 说明 |
|---|---|---|---|
| `silence_enabled` | bool | true | 解析标记 |
| `silence_marker` | string | `%%SILENCE%%` | |
| `hold_marker` | string | `%%HOLD%%` | |
| `silence_private_enabled` | bool | true | 私聊允许沉默 |
| `silence_allow_waiting_content` | bool | true | |
| `silence_allow_waiting_explanation` | bool | true | F5 |
| `silence_allow_goodbye` | bool | true | |
| `silence_allow_consent_trap` | bool | true | |
| `silence_allow_no_value` | bool | true | |
| `silence_allow_already_answered` | bool | true | |
| `silence_allow_user_busy` | bool | true | |
| `consent_trap_refuse_reply` | bool | false | true=让主模型回一句明确拒绝 |
| `hold_max_wait_seconds` | int | 30 | `%%HOLD%%` 等待上限（1–180） |
| `hold_default_wait_seconds` | int | 8 | 模型没给 wait 时的默认值 |
| `hold_cancel_scope_waiting_explanation` | string | `target` | 等解释：仅发图者补话才算打断 |
| `hold_cancel_scope_waiting_content` | string | `any` | 催促：任何人开口即撤销 |
| `silence_followup_max_chars` | int | 200 | |

##### 8.4 打断与重生成

| key | 类型 | 默认 | 说明 |
|---|---|---|---|
| `interrupt_private_enabled` | bool | false | F7 |
| `interrupt_group_enabled` | bool | false | |
| `segment_control_enabled` | bool | **false** | 分句由插件接管（可被新消息打断） |
| `segment_separator` | string | `$$$` | 分句标识（可配，≤16 字符） |
| `segment_delay_mode` | string | `log` | `log` = 对数；`linear` = 一次函数 |
| `segment_log_base` | float | 2.6 | 对数底数 |
| `segment_linear_start_ms` | int | 800 | 一次函数初始延迟 |
| `segment_linear_rate` | int | 10 | 一次函数增长率（字/秒） |
| `interrupt_mode` | string | `soft` | `soft`（不取消）/ `hard`（真取消，内部 API） |
| `interrupt_window_ms` | int | 8000 | 生成开始后多久内可被打断 |
| `interrupt_cooldown_count` | int | 3 | 每分钟同一会话最多 hard 打断次数 |
| `regen_private_enabled` | bool | false | F8 |
| `regen_group_enabled` | bool | false | |
| `regen_mode` | string | `discard` | `discard` / `ask_model` |
| `regen_ask_provider_id` | string | "" | |
| `regen_window_ms` | int | 3000 | |

##### 8.5 回收

| key | 类型 | 默认 | 说明 |
|---|---|---|---|
| `recycle_discarded_answer` | bool | true | F9 |
| `recycle_max_chars` | int | 600 | 注入草稿的字数上限 |

##### 8.6 缓冲（暂不实现）

| key | 类型 | 默认 |
|---|---|---|
| `buffer_quiet_ms` | int | 1500 |

---

#### 9. 存储与看板

##### 9.1 SQLite

```sql
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,
  umo TEXT, gid TEXT,
  sender_id TEXT, sender_name TEXT,
  reason TEXT, act TEXT,
  confidence INTEGER,
  wait_seconds INTEGER,
  followup TEXT,
  ms INTEGER, model TEXT,
  result TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_umo ON events(umo, ts DESC);
```

`kind`（当前只产这五种）：`judge`（判定，**包含打断**）`silence` `hold` `recycle` `error`
`result`（判定行，面板显示为「状态」，带颜色）：`judging`（判定中）→ `discarded`（已作废）/ `interrupted`（已打断）/ `partial_discarded`（中途作废）/ `keep`（不重发）
其余 `result`：`pending` `sent` `held` `cancelled` `dry_run` `passed` `switch_off` `refuse_passthrough` `failed`
判定 `reason`：模型只能在 `user_request` / `requirement_change` / `info_supplement` 里选；插件固定写 `hard_interrupt`（置信度 100）/ `user_request`（@ 点名，置信度 100）/ `judge_unavailable`。
颜色映射：`judging`/`pending`/`held`=黄，`discarded`/`interrupted`/`partial_discarded`/`cancelled`/`failed`=红，`sent`/`keep`=绿，`dry_run`=蓝，其余=灰。
**分句事件与面板按钮/输入框改动不写流水**。

一条判定只写一行：`insert_one` 拿到 id（状态 `judging`）→ 结算时 `update_status(row_id, result, reason, confidence, raw_output, detail)` 就地改状态；HOLD 同理。
流水表另有 `user_input`（触发该回答的用户输入）与 `raw_output`（含标记的模型原文）两列，老库在 `init` 时用 `ALTER TABLE ADD COLUMN` 自动补齐。

##### 9.2 网页接口（注册前缀 `/astrbot_plugin_ReplyGate`）

宿主实际暴露路径：`/api/v1/plugins/extensions/astrbot_plugin_ReplyGate/<endpoint>`（旧版还兼容 `/api/plug/...`）。页面在沙箱 iframe 中运行，必须经 `window.AstrBotPluginView.apiGet/apiPost('<endpoint>')` 调用，不能直接 fetch。

| 端点 | 方法 | 说明 |
|---|---|---|
| `console/logs` | GET | 分页查询（kind/umo/sender/reason/result/since/until/q） |
| `console/log` | GET | 详情（含模型原文） |
| `console/window` | GET | 窗口快照（active/valid_users/streak/终止原因/in-flight） |
| `console/stats` | GET | 今日统计：判定数、directed/not_for_me 比、平均置信度、沉默/暂存/兜底/打断/重生成次数、回收次数 |
| `console/health` | GET | 运行开关 + 数值项（`options`）、判定 provider、DB 行数、启动自检告警 |
| `console/markers` | GET | 标记格式、生效提示词块（含 `block_custom` / `default_template`）与注入状态 |
| `console/prompt` | POST | 保存自定义提示词块 `{"text": "..."}`；`{"reset": true}` 还原默认 |
| `console/config` | POST | 切换单个运行开关（白名单 `gate_enabled` / `gate_dry_run` / `silence_enabled` / `command_bypass` / `prompt_block_inject` / `group_window_enabled`），写回插件配置 |
| `console/window/end` | POST | 手动结束窗口 |
| `console/logs/clear` | POST | 清空流水 |

页面：运行开关（`role="switch"`，点一下即生效） + 统计卡 + 过滤条 + 表格 + 行展开看原文 + 5s 自动刷新（可关）。确认类操作走页内 `<dialog>`，不用 `alert/confirm/prompt`（沙箱里被禁）。

---

#### 10. 失败与安全（fail-safe）

| 场景 | 行为 |
|---|---|
| handler 抛异常 | 不置位、不 stop（等价不干预），记 `error` |
| 判定 provider 缺失/超时 | `group_judge_fail_mode`，记 `error` |
| 判定 JSON 坏 | 1 次纠正重试 → fail_mode，记 `error(parse_failed)` |
| 标记 JSON 坏 | 剥离疑似标记行，正文照发，记 `error(marker_parse_failed)` |
| `%%HOLD%%` 等待中被新解释打断 | 丢弃暂存正文，注入回收池，记 `hold(cancelled)` |
| 打断 API 不可用（版本变更） | 自动降级为 `soft`，启动时自检并写警告 |
| 兜底发送失败 | 记 `followup(failed)`，不重试 |
| 仅观察模式 | 只剥掉标记行，正文原样发送；**不** 丢弃、**不** 暂存、**不** sleep、**不** 写回收池；只记一条 `result=dry_run` 流水（detail 里写明"实际执行会丢弃/暂存 N 秒"）。唯一边界：标记外没有正文时跳过发送（模型只回了一行标记，剥完就是空的） |

**关键安全点**：`hard` 打断依赖 AstrBot 内部 API（`active_event_registry.request_agent_stop_all`），必须 try/except + 版本自检，失败一律降级 `soft`。

---

#### 11. 并发与生命周期

| 项 | 方案 |
|---|---|
| 并发 | 每 umo 一把 `asyncio.Lock`；判定/开窗/终止/HOLD 状态都在锁内 |
| 时钟 | 逻辑用 `time.monotonic()`，日志用 `time.time()` |
| HOLD 等待 | 在 `on_decorating_result` 内 `await asyncio.sleep(wait)`（仅阻塞该事件自己的发送链，不影响其它事件） |
| 兜底定时器 | `context.register_task` 托管，重载时取消 |
| 打断状态 | in-flight 记录（umo → answer_started_at/target_sender），回答发送完成即清除 |
| 重启 | 窗口/HOLD/pending 状态不落盘（等价未唤醒）；DB 日志保留 |
| 清理 | 每日按 `log_retention_days` / `log_max_rows` 清理 |

---

#### 12. 里程碑

| 阶段 | 内容 | 验收 |
|---|---|---|
| **M1 ✅ 已实现** | 出站闸门（SILENCE/HOLD 解析）+ SQLite + 看板 + 仅观察 | 私聊造三条假回复（普通/沉默/暂存），日志与行为均正确 |
| **M2** | 群聊窗口 + 二元判定（不看图）+ 置信度阈值 + 终止条件 + TTL | 8 条 not_for_me 关窗 / 集合空关窗 / 300s 关窗；图片消息按上下文判为 directed |
| **M3** | waiting_content 兜底 + 取消 | wait=10 到点秒发；任何新消息 → cancelled |
| **M4** | 打断（soft → hard）+ 重生成 + HOLD 取消与回收 | 生成中同用户补话 → 旧答案作废、新答案生成、草稿注入可见 |
| **M5（可选）** | 缓冲 | AstrWechat 合并不够时启用 |

---

#### 13. 验收测试（18 条）

| # | 场景 | 期望 |
|---|---|---|
| T1 | 群内 @ 机器人 | window_open，后续消息进判定 |
| T2 | 连续 8 条高置信 not_for_me | 第 8 条后 close(streak)，第 9 条不再判定 |
| T3 | not_for_me 但 conf=40（阈值 70） | 按 directed 处理，唤醒，streak 归零 |
| T4 | directed 后主模型沉默 | 正文不发，streak 已归零 |
| T5 | 唤醒者说无关话 → 集合空 | close(empty_set) |
| T6 | 300 秒无消息 | close(ttl) |
| T7 | 图片消息 + 上下文在跟机器人聊 | judged directed，主模型拿到原图 |
| T8 | 图片消息 + 明显在跟别人聊 | judged not_for_me（高置信），不唤醒 |
| T9 | 主模型 `waiting_content` wait=10 | 10s 后秒发催促；期间有人说话（scope=any）→ cancelled |
| T10 | 主模型 `%%HOLD%%` wait=8 | 8s 内同用户解释 → 暂存正文丢弃 + 新回答；无解释 → 8s 后原回答发送（走分段） |
| T11 | HOLD 丢弃后下一轮请求 | 请求里出现 `discarded_draft` 参考块（仅一次） |
| T12 | 私聊生成中同用户补话（soft） | 旧答案丢弃，新消息产生新回答，日志 interrupt(soft) |
| T13 | 私聊生成中同用户补话（hard） | 旧生成任务被取消（耗时显著下降），日志 interrupt(hard) |
| T14 | 群聊其它用户发消息 | **不触发**打断与重生成 |
| T15 | `consent_trap` 默认 | 静默；`consent_trap_refuse_reply=true` 时改为主模型回一句拒绝 |
| T16 | `goodbye` | 静默，日志 reason=goodbye |
| T17 | 标记 JSON 坏 | 正文照发 + error(marker_parse_failed) |
| T18 | 仅观察模式 | 只剥标记、正文照发、无延时、无回收，result=dry_run |

---

#### 14. 已确认决策（20 条）

| # | 决策 |
|---|---|
| D1 | 判定层二元，只判"是不是对我说"，带 confidence |
| D2 | 只有判定层 `not_for_me` 计数；主模型丢弃不计数不影响集合 |
| D3 | 有效用户集合：directed 加入 / not_for_me 移出；初始 = {唤醒者} |
| D4 | 必须同一 reason（not_for_me）连续 8 条；其它打断连续性 |
| D5 | directed 但主模型沉默 → 仍归零 streak |
| D6 | 窗口最长存活 300 秒 |
| D7 | 判定置信度阈值 70，低于阈值保守唤醒 |
| D8 | 等待（催促）被任何新消息打断并取消 |
| D9 | `consent_trap` 默认纯静默，可切"回一句拒绝" |
| D10 | 兜底催促秒发单条，不走分段 |
| D11 | goodbye 状态由主模型维护，插件不存 |
| D12 | `$$$` 分段由 AstrBot 处理 |
| D13 | 相邻消息合并由 AstrWechat 负责 |
| D14 | 私聊无判定层；沉默/延迟由主模型标记决定 |
| D15 | SQLite + 网页看板 |
| D16 | 停用 LivingHello |
| D17 | 判定层**不看图片像素**，图片消息按上下文判定 |
| D18 | 新增 `waiting_explanation`（`%%HOLD%%`）：暂存正文，等解释则丢弃重答，否则短暂后发送 |
| D19 | 同用户新消息可打断在途生成并重新生成；群聊仅同发送者、默认 soft |
| D20 | 回收仅对**完整生成**的正文生效；生成中打断无法回收 |
| D21 | 催促语也走 `%%HOLD%%`：SILENCE = 纯丢弃，HOLD = 延后发送；大部分场景是 HOLD |
| D22 | 打断范围按原因拆：等解释仅目标用户（`target`），催促任何人（`any`） |
| D23 | 看板加运维按钮：手动结束窗口 / 清空流水（已实现） |

---

#### 15. 待确认（实现前）

1. `interrupt_mode` 默认 `soft`；`hard` 需要显式开启（依赖内部 API）。是否接受？
2. `%%HOLD%%` 超时后发送暂存正文，走 AstrBot 原生分段（可能比 `silence` 兜底的"秒发"慢）—— 是否接受？
3. 看板是否要运维按钮：**手动结束窗口** / **清空日志**？

---

#### 16. 风险清单

| 风险 | 影响 | 缓解 |
|---|---|---|
| 判定模型每条群消息一次调用 | 成本/延迟 | TTL 300s + streak 8 + 限额 + 群白名单 |
| 群聊 `hard` 打断连带停其它事件 | 误伤 | 默认 `soft`；`hard` 仅私聊默认可用 |
| `active_event_registry` 属内部 API | 版本升级可能失效 | 启动自检 + try/except + 降级 soft |
| HOLD 阻塞发送链最长 30s | 用户等待感 | `hold_max_wait_seconds` 钳制 + 只阻塞本事件 |
| 提示词块未生效（被模型忽略） | 沉默/暂存不生效 | 仅观察模式核对流水 + 标记可改 + 解析失败照发 |
| 流式被打开 | 出站闸门失效 | 启动自检 + 警告日志 |
| 与其它插件优先级冲突 | 上下文记录缺失 | handler 用 `maxsize - 1` 最后执行 |
