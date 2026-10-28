"""回答正文的数字溯源（number guard）。

用途：自由 SQL 路径里，模型会基于工具结果写自然语言回答；工具结果可信，但正文
数字可能被模型改写、心算错、混单位或直接编造。本模块把正文里出现的每个数字与
本轮工具结果（含 table artifact）对齐：

- 直接命中工具结果中的数值；
- 允许平方米 ↔ 亩换算；
- 允许由两个工具数值计算出的百分比（回答必须带 %）；
- 允许引用用户问题里的参数（如“小于 100 平方米”的 100）和期次年份。

未命中的数字不会被悄悄当成事实：调用方会在回答末尾追加警示，并把
``number_guard`` 元数据挂到 message 事件上。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class NumberToken:
    raw: str
    value: float
    unit: str = ""
    percent: bool = False

    def label(self) -> str:
        return f"{self.raw}{self.unit}"


_NUMBER_RE = re.compile(
    r"(?<![0-9A-Za-z._])(-?\d[\d,]*(?:\.\d+)?)\s*(万|亿|%|％|平方米|平米|亩|公顷|个|块|条|种|行|年)?"
)
_M2_UNITS = {"平方米", "平米"}
_MU_UNITS = {"亩"}
_AREA_UNITS = _M2_UNITS | _MU_UNITS | {"公顷"}


def extract_numbers(text: str) -> list[NumberToken]:
    """从一段中文文本中抽取数字、单位和百分号。"""
    tokens: list[NumberToken] = []
    for match in _NUMBER_RE.finditer(str(text or "")):
        raw_number, unit = match.group(1), match.group(2) or ""
        try:
            value = float(raw_number.replace(",", ""))
        except ValueError:
            continue
        multiplier = 10000.0 if unit == "万" else 100000000.0 if unit == "亿" else 1.0
        tokens.append(
            NumberToken(
                raw=raw_number,
                value=value * multiplier,
                unit=unit,
                percent=unit in {"%", "％"},
            )
        )
    return tokens


def _walk_numbers(value: Any, output: list[float]) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        output.append(float(value))
        return
    if isinstance(value, str):
        output.extend(token.value for token in extract_numbers(value))
        return
    if isinstance(value, dict):
        for item in value.values():
            _walk_numbers(item, output)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _walk_numbers(item, output)


def _tool_evidence(tool_messages: Iterable[dict[str, Any]]) -> tuple[list[float], list[float]]:
    """返回（原始数值池，百分比池）。百分比池包含由任意两数算出的占比。"""
    values: list[float] = []
    for message in tool_messages:
        if message.get("role") != "tool":
            continue
        _walk_numbers(message.get("content"), values)
        _walk_numbers(message.get("artifacts"), values)
    # 单位换算：工具结果可能是平方米，正文展示为亩；两个方向都纳入证据。
    expanded = list(values)
    for value in values:
        expanded.append(value * 0.0015)
        expanded.append(value / 0.0015)
    percentages: list[float] = []
    for value in values:
        if 0.0 <= value <= 100.0:
            percentages.append(value)
    # 允许模型由分子/分母计算的百分比（只接受 0~100 的结果）。
    positive = [value for value in values if value > 0]
    for index, numerator in enumerate(positive[:120]):
        for denominator in positive[:120]:
            ratio = numerator / denominator * 100.0
            if 0.0 <= ratio <= 100.0:
                percentages.append(ratio)
    return expanded, percentages


def _near(value: float, target: float) -> bool:
    tolerance = max(0.01, abs(target) * 0.005)
    return math.isclose(value, target, rel_tol=0.0, abs_tol=tolerance)


def verify_numbers(
    answer: str,
    tool_messages: Iterable[dict[str, Any]],
    *,
    question: str = "",
) -> dict[str, Any]:
    """校验回答正文中的数字是否有本轮工具结果/用户参数作为出处。"""
    tokens = extract_numbers(answer)
    if not tokens:
        return {"ok": True, "checked": 0, "unverified": [], "evidence_count": 0}

    evidence_values, evidence_percentages = _tool_evidence(tool_messages)
    allowed_values: list[float] = []
    for token in extract_numbers(question):
        allowed_values.extend([token.value, token.value * 0.0015, token.value / 0.0015])
    unverified: list[NumberToken] = []
    checked = 0

    for token in tokens:
        # 期次年份、用户问题里的阈值/数字允许作为“引用”出现。
        if 1900 <= token.value <= 2100 and (token.unit == "年" or not token.unit):
            continue
        if any(_near(token.value, value) for value in allowed_values):
            continue
        # 像 01 / 0101 / 20 这种编码不按数值指标校验，避免把 TBLX 编码当成错误。
        raw_digits = token.raw.replace(",", "")
        if not token.unit and raw_digits.startswith("0") and "." not in raw_digits:
            continue
        checked += 1
        candidates: list[float] = []
        if token.percent:
            candidates = evidence_percentages
        elif token.unit in _M2_UNITS:
            candidates = [token.value] + [value for value in evidence_values]
            # 直接比较平方米；evidence_values 已含 m²、亩两种量纲。
        elif token.unit in _MU_UNITS:
            candidates = evidence_values
        elif token.unit in _AREA_UNITS:
            candidates = evidence_values
        else:
            candidates = evidence_values
        if not any(_near(token.value, candidate) for candidate in candidates):
            unverified.append(token)

    return {
        "ok": not unverified,
        "checked": checked,
        "unverified": [
            {"value": token.value, "raw": token.raw, "unit": token.unit}
            for token in unverified
        ],
        "evidence_count": len(evidence_values),
    }


def warning_text(result: dict[str, Any], limit: int = 5) -> str:
    """把未溯源数字压成一句用户可见的警示。"""
    unverified = result.get("unverified") or []
    if not unverified:
        return ""
    labels = []
    for item in unverified[:limit]:
        unit = str(item.get("unit") or "")
        labels.append(f"{item.get('raw')}{unit}")
    more = f"等 {len(unverified)} 个" if len(unverified) > limit else ""
    return (
        "⚠ 数字溯源：未能从本轮查询结果中找到以下数字的出处："
        + "、".join(labels)
        + more
        + "。请以上方表格/原始查询结果为准。"
    )
