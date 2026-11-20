"""配置读取：统一类型转换、范围钳制与开关判断。

AstrBot 的 config 是普通 dict（可能来自 JSON），数值可能是字符串，
所有读取都经过这里，避免散落各处的 int()/bool() 转换出错。
"""

from __future__ import annotations

from typing import Any

_TRUE = {"1", "true", "yes", "on", "y"}


class Config:
    def __init__(self, raw: dict[str, Any] | None = None):
        self.raw = raw if isinstance(raw, dict) else {}

    def refresh(self, raw: dict[str, Any] | None) -> None:
        self.raw = raw if isinstance(raw, dict) else {}

    def get(self, key: str, default: Any = None) -> Any:
        value = self.raw.get(key, default)
        return default if value is None else value

    def bool(self, key: str, default: bool = False) -> bool:
        value = self.raw.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value or "").strip().lower() in _TRUE

    def int(self, key: str, default: int = 0, low: int | None = None,
            high: int | None = None) -> int:
        try:
            number = int(float(self.raw.get(key, default)))
        except (TypeError, ValueError):
            number = int(default)
        if low is not None:
            number = max(low, number)
        if high is not None and high > 0:
            number = min(high, number)
        return number

    def float(self, key: str, default: float = 0.0, low: float | None = None,
              high: float | None = None) -> float:
        try:
            number = float(self.raw.get(key, default))
        except (TypeError, ValueError):
            number = float(default)
        if low is not None:
            number = max(low, number)
        if high is not None and high > 0:
            number = min(high, number)
        return number

    def str(self, key: str, default: str = "") -> str:
        value = self.raw.get(key, default)
        if value is None:
            return default
        return str(value).strip()

    def str_list(self, key: str) -> list[str]:
        value = self.raw.get(key)
        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        text = str(value or "")
        return [item.strip() for item in text.replace("，", ",").split(",") if item.strip()]

    def option(self, key: str, default: str, allowed: tuple[str, ...]) -> str:
        value = self.str(key, default)
        return value if value in allowed else default

    # ---- 语义化读取 ----

    @property
    def enabled(self) -> bool:
        return self.bool("gate_enabled", False)

    @property
    def dry_run(self) -> bool:
        return self.bool("gate_dry_run", True)

    @property
    def silence_enabled(self) -> bool:
        return self.bool("silence_enabled", True)

    def reason_allowed(self, switch_key: str) -> bool:
        return self.bool(switch_key, True)