# AGENTS.md（给自动化 agent 的入口）

本项目是 AstrBot 插件 **ReplyGate**（仓库根目录即插件目录），当前版本 **1.0.0**。许可证 AGPL-3.0。

**先读 [`HANDOFF.md`](HANDOFF.md)**：架构、模块地图、契约与坑、里程碑与路线图、测试与发布流程、附录（原始设计规格）都在那一份里。
`README.md` 只面向用户，含现有功能、配置项、部署方法；`_conf_schema.json` 是全部配置项的权威定义。

快速自检：

```bash
python -m py_compile main.py prompts.py core/*.py store/*.py webui/*.py
for t in tests/*.py; do python "$t"; done
node tests/test_page_smoke.mjs
```

硬性约定（详见 HANDOFF §5）：

1. 插件按包加载：只用 `if __package__:` + 相对导入，**禁止**顶层 `import prompts` 等兄弟模块；
2. 面板在沙箱 iframe 内，必须走 `window.AstrBotPluginView` 桥，不能用 fetch/alert/confirm/localStorage；
3. 缓冲只影响主模型交付；撤销 HOLD、打断、写流水都必须立即；
4. 弱打断只判定一次；主模型先完成要**压住**等判定；超时 = 激活窗口，不抛弃结果；
5. 判定与打断共用一种流水（`kind=judge`，一条判定一行：判定中 → 已作废/已打断/中途作废/不重发），原因只能取固定集合；
6. 分句事件与面板开关改动**不写流水**；
7. 改完必须跑全部测试 + `node tests/test_page_smoke.mjs`，再用 HANDOFF §4.2 的脚本重打包 zip；
8. 改版本号时同步 `metadata.yaml`、`main.py:VERSION`、`CHANGELOG.md`（有测试断言三者前两者一致）。
