// 面板前端冒烟测试：用最小 DOM 桩 + 假桥跑一遍真实脚本，抓 "xxx is not defined" 这类错误。
// 运行：node tests/test_page_smoke.mjs
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const HERE = dirname(fileURLToPath(import.meta.url));
const HTML = readFileSync(join(HERE, "..", "views", "gate", "index.html"), "utf8");
const SCRIPT = HTML.match(/<script>([\s\S]*)<\/script>/)[1];

/** 元素桩：记录 textContent / innerHTML / value，其余属性随便写。 */
function makeElement(id = "") {
  const el = {
    id,
    tagName: "DIV",
    textContent: "",
    innerHTML: "",
    value: "",
    className: "",
    disabled: false,
    hidden: false,
    style: {},
    dataset: {},
    selectedOptions: [{ textContent: "（跟随当前会话模型）", value: "" }],
    classList: {
      _set: new Set(),
      add(name) { this._set.add(name); },
      remove(name) { this._set.delete(name); },
      toggle(name, force) { (force ?? !this._set.has(name)) ? this._set.add(name) : this._set.delete(name); },
      contains(name) { return this._set.has(name); },
    },
    children: [],
    appendChild(child) { this.children.push(child); return child; },
    removeChild(child) { this.children = this.children.filter((item) => item !== child); },
    remove() {},
    setAttribute(name, value) { this[name] = value; },
    removeAttribute(name) { delete this[name]; },
    hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this, name); },
    addEventListener() {},
    removeEventListener() {},
    focus() {},
    select() {},
    click() {},
    close() {},
    showModal() {},
    querySelectorAll() { return []; },
    querySelector() { return null; },
  };
  return el;
}

const elements = new Map();
const getElement = (id) => {
  if (!elements.has(id)) elements.set(id, makeElement(id));
  return elements.get(id);
};

const failures = [];
const document = {
  getElementById: getElement,
  createElement: () => makeElement(),
  querySelectorAll: () => [],
  querySelector: () => null,
  activeElement: null,
  hidden: false,
  body: { appendChild() {}, },
  documentElement: { setAttribute() {} },
};
const sandbox = {
  document,
  window: { addEventListener() {}, getSelection: () => ({ removeAllRanges() {}, addRange() {} }) },
  navigator: {},
  location: { origin: "http://127.0.0.1" },
  console,
  setTimeout,
  clearTimeout,
  setInterval: () => 0,
  clearInterval: () => {},
  URL,
  FormData: class {},
};

const responses = {
  "console/health": {
    version: "1.0.0", db_ready: true, rows: 1, db_bytes: 1024, holds: 0, recycled: 0,
    startup_warnings: [],
    switches: {
      gate_enabled: true, gate_dry_run: true, silence_enabled: true,
      prompt_block_inject: true, command_bypass: true, group_window_enabled: false,
      interrupt_group_enabled: true, interrupt_private_enabled: false,
      interrupt_other_users: false,
    },
    options: {
      buffer_quiet_ms: 1500, buffer_max_wait_ms: 8000, interrupt_window_ms: 8000,
      soft_judge_threshold: 60, soft_judge_context_before: 0,
      soft_judge_timeout_ms: 4000, soft_judge_max_restarts: 3,
      soft_judge_max_tokens: 256, interrupt_mode: "soft",
      soft_judge_fail_mode: "regen", soft_judge_thinking_level: "off",
      soft_judge_provider_id: "p1",
    },
  },
  "console/stats": { today: { by_kind: {}, by_act: {}, by_result: {} } },
  "console/logs": { total: 0, rows: [] },
  "console/window": { windows: [], holds: [] },
  "console/markers": {
    persona_template: "# 沉默机制", default_template: "# 沉默机制", block_custom: false,
    judge_template: "判定模板", judge_custom: false, judge_default_template: "判定模板",
    inject_enabled: true, inject_hook: "on_llm_request",
  },
  "console/models": {
    providers: [{ id: "p1", model: "glm-5.3-flash", type: "openai_chat_completion" }],
    current: "p1",
  },
};

sandbox.window.AstrBotPluginView = {
  async apiGet(endpoint) { return responses[endpoint] ?? {}; },
  async apiPost() { return { options: responses["console/health"].options, switches: responses["console/health"].switches, persisted: true }; },
};

const context = vm.createContext(sandbox);
const unhandled = [];
process.on("unhandledRejection", (reason) => { unhandled.push(reason); });

try {
  await vm.runInContext(`(async () => { ${SCRIPT} })()`, context, { timeout: 5000 });
} catch (error) {
  failures.push(`脚本执行异常：${error && error.stack ? error.stack.split("\n")[0] : error}`);
}

// 等异步渲染跑完
await new Promise((resolve) => setTimeout(resolve, 200));
if (unhandled.length) {
  failures.push(`未处理的 promise 拒绝：${unhandled.map((item) => String(item && item.message ? item.message : item)).join("; ")}`);
}

const title = getElement("appTitle").textContent;
if (!String(title).includes("ReplyGate")) failures.push(`标题未渲染：${JSON.stringify(title)}`);
const judgeMeta = getElement("judgeMeta").textContent;
if (!String(judgeMeta).includes("判定模型")) failures.push(`judgeMeta 未渲染：${JSON.stringify(judgeMeta)}`);
const switchHost = getElement("switchList-interrupt").innerHTML;
if (!switchHost.includes("其它用户参与打断")) failures.push("打断分组未渲染");
if (switchHost.includes("接管分句发送")) failures.push("分句开关仍留在打断分组");
if (switchHost.includes("唤醒窗口")) failures.push("唤醒窗口开关未移除");
const segmentHost = getElement("switchList-segment").innerHTML;
if (!segmentHost.includes("接管分句发送")) failures.push("分句发送分组未渲染");
if (!getElement("segmentState").textContent) failures.push("分句状态未渲染");
const bufferState = getElement("bufferState").textContent;
if (!bufferState) failures.push("缓冲状态未渲染");
const personaState = getElement("personaState").textContent;
if (!personaState) failures.push("系统提示词块状态未渲染");
if (String(personaState).includes("失败")) failures.push(`系统提示词块渲染失败：${personaState}`);
const judgeState = getElement("judgeState").textContent;
if (String(judgeState).includes("失败") || !judgeState) failures.push(`判定提示词状态异常：${judgeState}`);
const banner = getElement("banner").innerHTML;
if (String(banner).includes("面板请求失败")) failures.push(`顶部出现失败横幅：${String(banner).slice(0, 120)}`);

if (failures.length) {
  console.error("✗ 面板冒烟测试失败：\n- " + failures.join("\n- "));
  process.exit(1);
}
console.log("✓ 面板冒烟测试通过（无未定义变量 / 各分组已渲染）");