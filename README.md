<h1 align="center">ReplyGate</h1>

<p align="center">
  <img alt="version" src="https://img.shields.io/badge/version-1.0.0-4cc2ff">
  <img alt="AstrBot" src="https://img.shields.io/badge/AstrBot-%3E%3D4.29.1-1f6feb">
  <img alt="license" src="https://img.shields.io/badge/license-AGPL--3.0-3fb950">
  <img alt="platform" src="https://img.shields.io/badge/platform-aiocqhttp%20%7C%20qq__official-8957e5">
</p>

<p align="center">让模型自己对回复时机下判断，插件只负责执行与记录。</p>

---

## 功能

### 1. 沉默与暂存（模型可控的回复时机）

模型在回答**最后一行**追加控制标记，插件在执行层负责丢弃或延迟发送：

```
那拜拜，随时找我。
%%SILENCE%% {"reason":"goodbye"}
```

```
这张图我先按外观答一下，如果你想问别的告诉我。
%%HOLD%% {"reason":"waiting_explanation","wait":10}
```

| 标记 | 作用 | 可用原因 |
|---|---|---|
| `%%SILENCE%%` | 丢弃正文，完全不出声 | `goodbye` 再见已完成 · `consent_trap` 抗议（回复即视为同意）· `no_value` 无价值 · `already_answered` 已经答过 · `user_busy` 用户想安静 · `user_request` 用户要求别回 |
| `%%HOLD%%` | 暂存正文 `wait` 秒，被新消息撤销、否则按时发送 | `waiting_explanation` 等待解释 · `waiting_content` 等待内容（正文是催促语）· `user_request` 用户要求稍后再回 |

- 每个原因都可以在配置里单独开关；`consent_trap` 可改成"回一句明确拒绝"。
- 暂存的撤销范围可配：`any` = 任何人开口就撤销，`target` = 只有当事人开口才撤销。
- 一次暂存**只占一行流水**：先记「等待中」，撤销或发出后原地改状态。

「沉默机制」说明由插件注入系统提示词（`on_llm_request`），**人格设定不需要改动**；人格里已有等价说明时自动跳过，不会重复叠加。面板里可以编辑、还原这段文本，也可以关掉注入自己维护。

### 2. 打断与重答（判定模型）

机器人在生成回答时，同一用户（可选：群内其他成员）又发了消息，插件会按配置处理：

| 模式 | 行为 |
|---|---|
| **弱打断 `soft`（默认）** | 起一个小模型给"是否需要重新回答"打分（0-100）与原因；分数 > 阈值 → 旧回答作废、重新回答；否则 → 保留旧回答，这条新消息不单独回答 |
| **强打断 `hard`** | 不判定，直接停掉在途执行并重新回答；受次数预算限制，超限自动降级走判定 |

- **@ 机器人 / 带唤醒前缀的消息**：两种模式下都直接重新回答（跳过判定）。
- **激活窗口**（默认 8 秒）：从第一条打断消息起算，窗口外的消息不会打断正在准备或正在分段发送的回答；回答成功发出或开始重答后计时归零。
- **判定只调用一次**：判定在消息到达时启动；若主模型先生成完，插件会**压住回答等判定结果**再决定发送或丢弃，不会二次判定。判定期间又有新消息会立刻用最新窗口重判。
- **原因由判定模型在固定集合里选**：`user_request` 用户要求、`requirement_change` 需求取消/改变、`info_supplement` 补充信息；不经过模型的场合由插件写 `hard_interrupt`（强打断，置信度 100）、`user_request`（@ 点名，置信度 100）、`judge_unavailable`（判定不可用）。
- **判定模型的加速**：可指定单独的判定模型、限制输出长度、压低或关闭思考等级（不影响主模型）；服务商拒绝这些参数时自动退回普通请求。
- **回答生成期间到达、最终没被覆盖的消息**会进"未回复队列"，下一轮以 `【尚未回复的消息…】` 的形式交给模型一并回应，不会石沉大海。

### 3. 分句发送

- 插件按可配置的标识（默认 `$$$`）把回答拆成多段、逐段发送，段间等待支持两种算法：
  **对数** `log_底数(本段字数)` 秒 / **一次函数** `初始值 + 本段字数 ÷ 增长率` 秒。
- 切段会跳过夹在公式里的分隔符（前后紧邻 `$` 时不切），避免切坏 `$$a$$$$b$$` 这类相邻公式。
- 每段发送前后都会检查是否被打断：被判定为重新回答时丢弃剩余分段（流水记「中途作废」并写明已发几段），判定为保留时继续发完。
- 分句事件本身不写流水；开启前**必须关闭 AstrBot 的「分段回复」**（平台设置 → 扩展功能），否则会重复分段。

### 4. 消息缓冲

- **单次缓冲**（默认 1.5 秒，0 = 关闭）：每来一条新消息重新计时，安静满这么久才把这批消息一起交给主模型。
- **最大总缓冲**（默认 8 秒）：从第一条被缓冲的消息开始计时，到点必须放行，避免对方一直发导致无限缓冲。
- 缓冲**只影响主模型什么时候收到消息**：撤销暂存、触发判定/打断、写流水都在消息到达时立即发生。

### 5. 流水与看板

- 所有判定、沉默、暂存、回收、异常写入 SQLite：类型、原因、置信度、结果、模型原始输出（含思考）、触发消息一应俱全，可按类型/原因/状态/会话/关键词/时间筛选。
- **一条判定只占一行**：先记「判定中」，结算后就地改成 `已作废` / `已打断` / `中途作废` / `不重发`，同时补齐原因与置信度。
- 面板分组：**插件总开关 / 沉默和等待 / 消息打断 / 分句发送 / 消息缓冲**，开关点一下即生效并写回配置；系统提示词块与判定提示词都可编辑、还原。
- **仅观察模式**：只记录判定、剥掉标记、正文照发，适合先观察再启用。

### 6. 命令消息旁路

以唤醒前缀（默认 `/`）开头的消息按命令处理：不进判定、不进打断、不撤销暂存。

---

## 部署

### 前提

| 检查项 | 要求 |
|---|---|
| AstrBot | `>= 4.29.1`（插件市场的版本检查会自动校验） |
| 流式输出 | **必须关闭**（`provider_settings.streaming_response = false`） |
| 主动回复概率 | **必须关闭**（`provider_ltm_settings.active_reply`） |
| 其它拦截类插件 | **关闭**，否则互相抢消息 |
| 分句发送 | 启用插件分句前，关闭 AstrBot 自带的「分段回复」 |

插件启动时会检查前两项与冲突插件，没通过会在面板顶部红字列出。

### 安装

1. **插件市场**：搜索 `ReplyGate` → 安装 → 重载插件。
2. **上传压缩包**：AstrBot 面板「插件 → 安装/更新」上传 zip。
   - 上传前先删掉 `data/plugins/astrbot_plugin_ReplyGate`，避免残留目录导致加载失败。
3. **手动放置**：把仓库目录放到 `data/plugins/astrbot_plugin_ReplyGate`，重启或重载插件。

安装完成后：面板「插件 → ReplyGate」里会出现 `gate` 页面，进去后按 **Ctrl+F5** 强制刷新一次。

### 首次使用建议

1. 保持「仅观察」开启（默认）、「提示词注入」开启。
2. 正常使用，观察判定与沉默记录，确认原因分类与阈值合适。
3. 再关闭「仅观察」转为实际执行，按需开启「消息缓冲」「群聊打断」「分句发送」。

---

## 配置项

全部配置项都可在面板里改，也同步到 `data/config/astrbot_plugin_ReplyGate_config.json`。

### 总控

| 键 | 默认 | 说明 |
|---|---|---|
| `gate_enabled` | `false` | 总开关，关闭后插件完全不介入（仍可查看历史流水） |
| `gate_dry_run` | `true` | 仅观察：只剥标记、正文照发。面板上的「实际执行」开关与此项相反 |
| `log_enabled` | `true` | 是否写入流水数据库 |
| `log_retention_days` | `14` | 流水保留天数 |
| `log_max_rows` | `50000` | 流水最大行数 |
| `prompt_block_inject` | `true` | 是否把「沉默机制」块注入系统提示词 |
| `prompt_block_text` | 空 | 自定义提示词块，留空用内置默认（面板「编辑/还原」维护） |
| `silence_marker` | `%%SILENCE%%` | 沉默标记文本 |
| `hold_marker` | `%%HOLD%%` | 暂存标记文本 |
| `command_bypass` | `true` | `/` 开头的命令消息旁路 |

### 沉默与暂存

| 键 | 默认 | 说明 |
|---|---|---|
| `silence_enabled` | `true` | 是否解析 SILENCE / HOLD 标记 |
| `silence_private_enabled` | `true` | 私聊是否允许沉默 |
| `silence_allow_goodbye` | `true` | 允许原因「再见已完成」 |
| `silence_allow_consent_trap` | `true` | 允许原因「抗议」 |
| `silence_allow_no_value` | `true` | 允许原因「无价值」 |
| `silence_allow_already_answered` | `true` | 允许原因「已经答过」 |
| `silence_allow_user_busy` | `true` | 允许原因「用户想安静」 |
| `silence_allow_user_request` | `true` | 允许原因「用户要求」（沉默与暂存共用） |
| `silence_allow_waiting_explanation` | `true` | 允许 HOLD 原因「等待解释」 |
| `silence_allow_waiting_content` | `true` | 允许 HOLD 原因「等待内容」 |
| `consent_trap_refuse_reply` | `false` | 抗议时不静默，改为回一句明确拒绝 |
| `hold_max_wait_seconds` | `30` | 暂存等待上限（秒） |
| `hold_default_wait_seconds` | `10` | 模型没写 `wait` 时的默认等待秒数 |
| `silence_followup_max_chars` | `200` | 催促语最大长度 |
| `hold_cancel_scope_waiting_explanation` | `target` | 「等待解释」的撤销范围：`target` 只有发图的人补话才算解释 |
| `hold_cancel_scope_waiting_content` | `any` | 「催促语」的撤销范围：`any` 任何人开口都撤销 |
| `hold_cancel_scope_user_request` | `target` | 「用户要求稍后再回」的撤销范围 |

### 判定与打断

| 键 | 默认 | 说明 |
|---|---|---|
| `interrupt_group_enabled` | `false` | 群聊：同用户新消息打断在途回答 |
| `interrupt_private_enabled` | `false` | 私聊：同上 |
| `interrupt_other_users` | `false` | 群内其他成员的消息也参与打断 |
| `interrupt_mode` | `soft` | `soft` 弱打断（判定打分）/ `hard` 强打断（直接停掉） |
| `interrupt_window_ms` | `8000` | 打断激活窗口（毫秒，强/弱共用） |
| `interrupt_cooldown_count` | `3` | 强打断次数预算（10 分钟），超限降级为弱打断 |
| `soft_judge_provider_id` | 空 | 判定模型（面板下拉选择），留空跟随当前会话模型 |
| `soft_judge_threshold` | `60` | 判定阈值：分数 > 阈值才重新回答 |
| `soft_judge_context_before` | `0` | 判定窗口在生成点之前再多带几条消息 |
| `soft_judge_timeout_ms` | `4000` | 判定模型**单次请求**超时（不是放弃判定） |
| `soft_judge_max_restarts` | `3` | 判定被新消息重判的次数上限 |
| `soft_judge_fail_mode` | `regen` | 判定不可用时：`regen` 重新回答 / `keep` 保留旧回答 |
| `soft_judge_max_tokens` | `256` | 判定模型输出上限（思考模型别设太小） |
| `soft_judge_thinking_level` | `off` | 判定模型思考等级：`off` / `minimal` / `low` |
| `judge_prompt_text` | 空 | 自定义判定提示词，留空用内置默认；`{{messages}}` 会被替换成对话窗口 |
| `recycle_discarded_answer` | `true` | 回收被丢弃的完整回答，作为下一轮参考草稿 |
| `recycle_max_chars` | `600` | 参考草稿字数上限 |

### 分句发送

| 键 | 默认 | 说明 |
|---|---|---|
| `segment_control_enabled` | `false` | 分句由插件接管（可被新消息打断） |
| `segment_separator` | `$$$` | 分句标识（≤16 字符，模型提示词里要用同一个） |
| `segment_delay_mode` | `log` | 段间等待算法：`log` 对数 / `linear` 一次函数 |
| `segment_log_base` | `2.6` | 对数算法底数（1.0–10.0，越大等待越短） |
| `segment_linear_start_ms` | `800` | 一次函数：初始延迟（毫秒） |
| `segment_linear_rate` | `10` | 一次函数：增长率（字/秒） |

### 消息缓冲

| 键 | 默认 | 说明 |
|---|---|---|
| `buffer_quiet_ms` | `1500` | 单次缓冲时间（毫秒，0 = 关闭缓冲） |
| `buffer_max_wait_ms` | `8000` | 最大总缓冲时间（毫秒，必须 ≥ 单次） |

### 预留配置项

以下键已在 `_conf_schema.json` 中预留，**1.0.0 尚未生效**，计划随后续版本实现（详见开发文档的路线图）：
`group_window_enabled`、`group_streak_limit`、`group_window_ttl_seconds`、`group_context_messages`、`group_judge_provider_id`、`group_judge_timeout_seconds`、`group_judge_hourly_limit`、`group_judge_fail_mode`、`group_not_for_me_threshold`、`group_seed_is_waker`、`group_channels`、`judge_ignore_images`、`regen_private_enabled`、`regen_group_enabled`、`regen_mode`、`regen_ask_provider_id`、`regen_window_ms`。

---

## 排错

| 现象 | 处理 |
|---|---|
| 安装后报 `No module named 'core'` / `No module named 'prompts'` | 插件按包加载，兄弟模块必须用相对导入；如果你改过代码，请保持 `if __package__:` 写法。另外先删掉 `data/plugins/astrbot_plugin_ReplyGate` 再重传，避免半新半旧目录 |
| 面板提示「无法连接 AstrBot 仪表盘桥」 | 不要用浏览器直接打开 html；请在插件页里打开视图 |
| 沉默/暂存完全不生效 | 确认「仅观察」已关闭、`silence_enabled` 与对应原因开关已开；模型输出里必须带标记行 |
| 判定永远不触发 | 需要先有"在途回答"：确认打断开关已开、新消息落在 `interrupt_window_ms` 内、发言人是触发回答的人（或开启「其它用户参与打断」） |
| 判定很慢 | 换更小的判定模型、把「思考」设为关、降低 `soft_judge_max_tokens`；不要设成 64（思考模型会把预算全花在思考上） |
| 分句重复发送 | 关闭 AstrBot 自带的「分段回复」（平台设置 → 扩展功能） |
| 回复出现两次或被打断风暴 | 关闭其它同类插件（如 LivingHello），并检查微信桥的 `buffer_seconds` 是否为 0 |

---

## 开发

- 路线图：见 [`HANDOFF.md`](HANDOFF.md)。
- 自动化测试：

```bash
python -m py_compile main.py prompts.py core/*.py store/*.py webui/*.py
for t in tests/*.py; do python "$t"; done
node tests/test_page_smoke.mjs
```

## 许可证

[AGPL-3.0](LICENSE) © jingle
