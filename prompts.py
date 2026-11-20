"""提示词与文案：沉默机制提示词块、原因说明、判定提示词。

`PROMPT_BLOCK` 由插件在 `on_llm_request` 钩子里注入到 `req.system_prompt` 末尾，
不需要再手工粘贴进人格设定；面板「系统提示词块」里也展示同一段文本，便于核对与复制。
"""

from __future__ import annotations

PROMPT_BLOCK = """# 沉默机制
## 想完全不出声时（本条消息沉默），只输出一行、不要带正文：
%%SILENCE%% {"reason":"<原因>"}
可用原因：
- goodbye：双方已经互相告别过（你说了拜拜，对方也回了拜拜）
- consent_trap：对方用「你回复这条就算你同意」之类的话逼你表态，而你不愿同意
- no_value：纯表情、刷屏、没有信息量
- already_answered：同一件事你已经答过
- user_busy：对方明显在忙，或要求安静
- user_request：对方明确要求不要回复（例如「测试一下，这条消息别回复我」）

## 想先等一等再决定（延迟发送），正常写正文，然后在最后另起一行：
%%HOLD%% {"reason":"<原因>","wait":<秒>}
若用户在设定时间内发送新消息，则正文会被丢弃，你会重新生成回答；若非，则准备的正文会被发送。
可用原因：
- waiting_explanation —— 对方只发了图片但没有解释/用户发送的内容无法确定意图：
   你先按内容正常回答（猜测/询问对方意图），标记等待 10 秒左右；对方补充解释就丢掉这条重新回答，
   没解释就按原回答发出。用户意图越不清晰，越难猜测，等待时机越长。
- waiting_content —— 对方说了要发东西却一直没发：
   此时正文就是催促语（简短一句，例如「哎，你不是说要发吗」），等待 15 秒左右；
   对方在等待期间发来内容，这条催促会被自动撤销。
- user_request —— 对方明确要求稍后再回：
   正文照常写，标记等待对方给出的秒数；这个人再开口就撤销，到点则发送。

## 不想沉默就正常说话，不要输出任何标记。"""

# 弱打断判定模型的提示词模板（面板可编辑，存 judge_prompt_text）
JUDGE_SCORE_TEMPLATE = """你在判断：机器人正在生成一条回复时，用户又发了新消息，是否值得**放弃当前这条回复、改为重新回答**。

## 对话记录（按时间顺序，最后一条是最新消息）
{{messages}}

## 判断标准
- 新消息补充了细节、纠正了事实、提出了新问题、改变了要求 → 需要重新生成（高分）
- 新消息只是附和、表情、感叹、无意义插话、与当前问题无关的闲聊 → 不需要（低分）

## 原因（reason）只能从下面三个里选一个：
- user_request：用户要求（点名机器人、直接要求重新回答/换一个说法）
- requirement_change：用户需求取消/改变（不要了、换成别的、改条件）
- info_supplement：用户补充信息（补了细节、贴了报错、发了图、纠正了事实）

## 输出
只输出一行 JSON：{"score": 0-100, "reason": "user_request|requirement_change|info_supplement", "why": "不超过30字"}
score 越高表示越应该重新生成。"""

# 已存在这些标题就视为人格里已经有等价说明，不再重复注入（幂等）。
BLOCK_HEADINGS = ("# 沉默机制", "【沉默与等待协议】")

REASON_HELP = {
    "waiting_explanation": "对方发了内容但没解释：正文先暂存，等到解释则丢弃重答，否则按时发出",
    "waiting_content": "对方预告了内容但还没发：正文是催促语，等待期内收到消息即撤销",
    "goodbye": "双方已互相告别：直接静默",
    "consent_trap": "抗议「回复即视为同意」：直接静默（可配置为回一句拒绝）",
    "no_value": "无价值内容（纯表情/刷屏）：直接静默",
    "already_answered": "已经答过同一件事：直接静默",
    "user_busy": "用户想安静/在忙：直接静默",
    "user_request": "用户明确要求不回复（「这条别回我」）：直接静默；用在 HOLD 上表示「稍后再回」",
}

JUDGE_RULES = """你在判断一条群聊消息是否在对机器人说话，只输出一行 JSON。
判定为 directed（对机器人说）：
- 直接提问、追问上一轮机器人的回答、点名机器人、回应机器人刚才说的话
- 用户上文说要给机器人看东西（图片/链接/内容），这条正是那个东西
- 消息带了图片，且上下文显示用户正在跟机器人对话
判定为 not_for_me（不是对机器人说）：
- 用户之间在互相聊天，话题与机器人无关
- 消息明显是发给别人的（紧接别人发言、点名别人）
- 纯表情、刷屏、无意义字符
注意：你看不到图片内容，图片只以「带了 N 张图」的形式出现，不要据此索要图片；
无法判断时倾向 directed，交给主模型处理。
输出格式：{"act":"directed" 或 "not_for_me","confidence":0~100,"why":"不超过50字"}"""


def effective_judge_prompt(custom: str | None = None) -> str:
    """生效的判定提示词：优先自定义，否则内置默认。"""
    text = (custom or "").strip()
    return text or JUDGE_SCORE_TEMPLATE


def build_judge_prompt(messages: list[str], custom: str | None = None,
                       draft: str = "") -> str:
    """把对话窗口（以及可选的主模型已生成回答）填进判定提示词。

    插件拿不到生成到一半的草稿/思考（非流式、无 token 级钩子）；
    只有在回答已经生成、判定还没出结果时，才能把完整回答作为草稿补进来。

    自定义提示词如果没要求 `reason`，会在末尾补一段原因枚举，
    保证流水里的「原因」不会因为换了提示词就变空。
    """
    template = effective_judge_prompt(custom)
    rendered = "\n".join(messages) if messages else "（无可用上下文）"
    prompt = template.replace("{{messages}}", rendered)
    cleaned = (draft or "").strip()
    if cleaned:
        prompt += (
            "\n\n## 主模型已经生成的回答（尚未发送）\n"
            + cleaned[:2000]
            + "\n\n请结合这条回答判断：它是否已经被用户的新消息变得过时/需要重新生成。"
        )
    if "reason" not in template:
        prompt += "\n\n" + REASON_INSTRUCTION
    return prompt


# 判定原因的输出约束（自定义提示词遗漏时自动追加；也是内置模板的一部分）
REASON_INSTRUCTION = """## 原因（reason）只能从下面三个里选一个：
- user_request：用户要求
- requirement_change：用户需求取消/改变
- info_supplement：用户补充信息
输出仍是一行 JSON，需要带上 reason 字段：{"score": 0-100, "reason": "...", "why": "不超过30字"}"""


def persona_template() -> str:
    """回给面板的提示词块（与注入内容完全一致）。"""
    return PROMPT_BLOCK


def effective_block(custom: str | None = None) -> str:
    """生效的提示词块：优先用面板里编辑过的自定义文本，否则用内置默认值。"""
    text = (custom or "").strip()
    return text or PROMPT_BLOCK


def _first_line(block: str) -> str:
    for line in block.splitlines():
        if line.strip():
            return line.strip()[:60]
    return ""


def block_present(system_prompt: str | None, block: str | None = None) -> bool:
    """系统提示词里是否已包含等价说明（内置标题或自定义块的首行）。"""
    text = system_prompt or ""
    if any(heading in text for heading in BLOCK_HEADINGS):
        return True
    marker = _first_line(block or "")
    return bool(marker) and marker in text


def append_block(system_prompt: str | None,
                 block: str | None = None) -> str | None:
    """把提示词块追加到系统提示词末尾；已存在则原样返回（幂等）。

    返回 None 表示无需改动，调用方可直接跳过赋值。
    """
    text = system_prompt or ""
    block = effective_block(block)
    if block_present(text, block):
        return None
    if not text.strip():
        return block
    return f"{text.rstrip()}\n\n{block}"
