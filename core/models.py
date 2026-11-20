"""ReplyGate 数据契约：三层唯一的输入输出形状。

markers.py 负责从模型输出里解析出这里的指令；outbound.py 负责执行；
db.py 只负责把这些结构落成一行行日志。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# 沉默（丢弃正文）允许的原因
SILENCE_REASONS = (
    "goodbye",
    "consent_trap",
    "no_value",
    "already_answered",
    "user_busy",
    "user_request",          # 用户明确要求不要回复（"这条别回我"）
)

# 暂存（保留正文，延后发送）允许的原因
HOLD_REASONS = (
    "waiting_explanation",   # 发了图没解释：先暂存回答，等解释或超时再发
    "waiting_content",       # 预告了内容没发：正文是催促语，收到消息就作废
    "user_request",          # 用户明确要求稍后再回：暂存指定秒数
)

ALL_REASONS = SILENCE_REASONS + HOLD_REASONS

# ------------------------------------------------------------------ 判定（judge）

# 判定模型必须在下面这三个里选一个（面板/流水共用同一套 label）
JUDGE_REASONS = (
    "user_request",         # 用户要求（点名/明说要改）
    "requirement_change",   # 用户需求取消/改变
    "info_supplement",      # 用户补充信息
)

# 不经过判定模型时由插件固定写入的原因
JUDGE_FIXED_REASONS = (
    "hard_interrupt",       # 强打断（置信度按 100 记）
    "judge_unavailable",    # 判定模型不可用（按失败策略收尾）
    "unspecified",          # 模型没返回原因（如自定义判定提示词没要求 reason）
)

JUDGE_REASON_LABEL = {
    "user_request": "用户要求",
    "requirement_change": "需求取消/改变",
    "info_supplement": "补充信息",
    "hard_interrupt": "强打断",
    "judge_unavailable": "判定不可用",
    "unspecified": "未标注原因",
}

# 判定结果：一条判定流水从「判定中」开始，最终落到下面四种之一
JUDGE_RESULT_JUDGING = "judging"            # 判定中
JUDGE_RESULT_DISCARDED = "discarded"        # 已作废（旧回答已生成完，只丢弃不发）
JUDGE_RESULT_INTERRUPTED = "interrupted"    # 已打断（真把在途生成停掉了）
JUDGE_RESULT_PARTIAL = "partial_discarded"  # 中途作废（分句已发出一部分后被打断）
JUDGE_RESULT_KEEP = "keep"                  # 不重发（保留旧回答，新消息不单独回）

JUDGE_RESULT_LABEL = {
    JUDGE_RESULT_JUDGING: "判定中",
    JUDGE_RESULT_DISCARDED: "已作废",
    JUDGE_RESULT_INTERRUPTED: "已打断",
    JUDGE_RESULT_PARTIAL: "中途作废",
    JUDGE_RESULT_KEEP: "不重发",
}

# 每个原因对应的开关键
REASON_SWITCH = {
    "goodbye": "silence_allow_goodbye",
    "consent_trap": "silence_allow_consent_trap",
    "no_value": "silence_allow_no_value",
    "already_answered": "silence_allow_already_answered",
    "user_busy": "silence_allow_user_busy",
    "waiting_explanation": "silence_allow_waiting_explanation",
    "waiting_content": "silence_allow_waiting_content",
    # 用户要求：沉默与暂存共用同一个开关
    "user_request": "silence_allow_user_request",
}

REASON_LABEL = {
    "goodbye": "再见已完成",
    "consent_trap": "抗议（回复即视为同意）",
    "no_value": "无价值内容",
    "already_answered": "已经答过",
    "user_busy": "用户想安静",
    "waiting_explanation": "等待解释",
    "waiting_content": "等待内容（催促）",
    "user_request": "用户要求",
}

# HOLD 被打断时的取消范围：配置键 + 运行时默认值（与 _conf_schema.json 保持一致）
HOLD_SCOPE_KEY = {
    "waiting_explanation": "hold_cancel_scope_waiting_explanation",
    "waiting_content": "hold_cancel_scope_waiting_content",
    "user_request": "hold_cancel_scope_user_request",
}
HOLD_SCOPE_DEFAULT = {
    # 等解释：只有发图的人补话才算解释
    "waiting_explanation": "target",
    # 催促：有人开口就撤销催促，避免群聊话题早翻篇了还在催
    "waiting_content": "any",
    # 用户要求稍后再回：只有这个人再开口才算撤销
    "user_request": "target",
}


@dataclass
class Marker:
    """模型输出尾部解析出的控制指令。

    SILENCE = 丢弃正文（永久沉默，没有后续发送）
    HOLD    = 保留正文，等待 wait 秒；期间被打断则丢弃，否则按时发送
    """

    kind: str                  # "silence" | "hold"
    reason: str
    wait: int = 0              # 仅 hold 有效
    memo: str = ""
    raw_json: str = ""

    @property
    def is_hold(self) -> bool:
        return self.kind == "hold"


@dataclass
class ParsedOutput:
    """一次出站文本的解析结果。"""

    text: str                  # 去掉标记行后的正文
    marker: Marker | None = None
    parse_error: str = ""      # 标记疑似存在但不合法时的原因码


@dataclass
class LogRow:
    """一条流水。db.insert 接受 dict，这里只做字段收敛。"""

    kind: str
    umo: str = ""
    gid: str = ""
    sender_id: str = ""
    sender_name: str = ""
    reason: str = ""
    act: str = ""
    confidence: int | None = None
    wait_seconds: int | None = None
    followup: str = ""
    ms: int | None = None
    model: str = ""
    result: str = ""
    detail: str = ""
    user_input: str = ""
    raw_output: str = ""
    ts: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "kind": self.kind,
            "umo": self.umo,
            "gid": self.gid,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "reason": self.reason,
            "act": self.act,
            "confidence": self.confidence,
            "wait_seconds": self.wait_seconds,
            "followup": self.followup,
            "ms": self.ms,
            "model": self.model,
            "result": self.result,
            "detail": self.detail,
            "user_input": self.user_input,
            "raw_output": self.raw_output,
        }