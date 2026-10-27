"""提示词解析、标签后处理和图片字符串工具"""

from __future__ import annotations

import json
import re
from typing import Any


_COUNT_RE = re.compile(
    r"^(?:solo|\d+girls|\d+boys|\d+people|1girl|1boy)$",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"^year\s+\d{4}$", re.IGNORECASE)
_DATA_URL_RE = re.compile(
    r"^data:image/(?P<fmt>[a-zA-Z0-9+.-]+);base64,(?P<b64>[A-Za-z0-9+/=]+)$"
)
_CAMERA_TAGS = {
    "pov",
    "female pov",
    "looking at viewer",
    "from above",
    "from below",
    "wide angle",
    "close-up",
    "close up",
    "full body",
    "upper body",
    "lower body",
    "holding phone",
}


def _strip_code_fence(text: str) -> str:
    """去掉 Markdown 的```及可选语言。"""
    stripped = (text or "").strip()
    if not (stripped.startswith("```") and stripped.endswith("```")):
        return stripped

    inner = stripped[3:-3].strip()
    if "\n" not in inner:
        return inner.strip()

    first_line, rest = inner.split("\n", 1)
    if first_line.strip().isalpha() and len(first_line.strip()) < 15:
        return rest.strip()
    return inner.strip()


def _join_tags(tags: Any) -> str:
    """将字符串标签列表清洗并用逗号连接"""
    if not isinstance(tags, list):
        return ""
    return ", ".join(
        value.strip() for value in tags if isinstance(value, str) and value.strip()
    ).strip()


def parse_prompt_from_structured_output(text: str) -> str | None:
    """解析文本或 JSON 两种提示词输出格式，统一返回可直接生图的 prompt"""
    cleaned = _strip_code_fence(text).strip()
    if not cleaned:
        return None

    candidates = [cleaned]
    if '"prompt"' in cleaned:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidates.append(cleaned[start : end + 1])

    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except Exception:
            continue

        if not isinstance(payload, dict):
            continue

        version = payload.get("version")
        if version == 2 or (isinstance(version, int) and version >= 2):
            rendered = _render_from_v2(payload)
            if rendered:
                return rendered

        prompt = payload.get("prompt")
        #旧版提示词格式
        if isinstance(prompt, str) and prompt.strip():
            normalized = prompt.strip()
            if "\\n|" in normalized:
                normalized = normalized.replace("\\n", "\n")
            return normalized

    return None


def _render_from_v2(payload: dict[str, Any]) -> str | None:
    """新版结构化多人提示词；每个人物保留一行 ``|`` 分隔格式"""
    # 例如
    # {
    #     "version": 2,
    #     "format": "multi",
    #     "global": ["2girls","outdoors","sunset"],
    #     "people": [
    #         ["1girl", "red hair", "school uniform"],
    #         ["1girl", "blue hair", "casual clothes"]
    #     ]
    # }
    # 函数会将其转成 Novel AI 格式的多人提示词：
    # 2 girls, outdoors, sunset
    # | 1girl, red hair, school uniform
    # | 1girl, blue hair, casual clothes
    global_tags = payload.get("global")
    if not isinstance(global_tags, list):
        return None
    first_line = _join_tags(global_tags)
    if not first_line:
        return None

    people = payload.get("people")
    people = people if isinstance(people, list) else []
    format_value = str(payload.get("format") or "").strip().lower()

    valid_people: list[list[str]] = []
    for item in people:
        if isinstance(item, list):
            parts = [value.strip() for value in item if isinstance(value, str) and value.strip()]
            if parts:
                valid_people.append(parts)

    if format_value != "multi" or len(valid_people) <= 1:
        if valid_people:
            return _join_tags(global_tags + valid_people[0]) or first_line
        return first_line

    lines = [first_line]
    for item in valid_people:
        lines.append(f"| {_join_tags(item)}")
    return "\n".join(lines).strip()


def _strip_wrappers(tag: str) -> str:
    """去掉标签外层括号和简单的权重语法包装"""
    stripped = tag.strip()
    stripped = stripped.lstrip("{[(").rstrip("}])").strip()
    stripped = re.sub(r"^[+-]?\d+(?:\.\d+)?::", "", stripped).strip()
    stripped = re.sub(r"::\s*$", "", stripped).strip()
    return stripped


def normalize_prompt_order(prompt: str) -> str:
    """将视角、人数标签和年份标签整理到稳定位置，让提示词更有序"""
    if not prompt or not prompt.strip():
        return prompt

    def reorder(tags: list[str]) -> list[str]:
        counts: list[str] = []
        cameras: list[str] = []
        years: list[str] = []
        rest: list[str] = []
        # 内层函数用于匹配和整理不同类型的标签

        for tag in tags:
            core = re.sub(r"\s+", " ", _strip_wrappers(tag)).strip().lower()
            if _YEAR_RE.match(core):
                years.append(tag)
            elif _COUNT_RE.match(core):
                counts.append(tag)
            elif core in _CAMERA_TAGS:
                cameras.append(tag)
            else:
                rest.append(tag)

        return cameras + counts + rest + years

    return _process_prompt_lines(prompt, reorder)


def _process_prompt_lines(prompt: str, transform) -> str:
    """逐行拆分提示词、应用标签转换器并保留多人行前缀"""
    # 这里的transform在上一个函数中调用时，
    # 传入的实际上是reorder()函数
    lines = prompt.split("\n")
    out_lines: list[str] = []
    for line in lines:
        raw = line.strip()
        if not raw:
            continue
        prefix = ""
        if raw.startswith("|"):
            prefix = "|"
            raw = raw[1:].strip()
        tags = [tag.strip() for tag in raw.split(",") if tag.strip()]
        transformed = transform(tags)
        # 等价于reorder(tags)，后面再用逗号拼接
        joined = ", ".join(transformed).strip()
        if prefix:
            out_lines.append(f"{prefix} {joined}".strip())
        else:
            out_lines.append(joined)
    return "\n".join(out_lines).strip()


def strip_data_url(image_data: str) -> tuple[str, str | None]:
    """拆分 data URL，返回不带头部的 base64 和图片格式"""
    if not isinstance(image_data, str):
        return "", None
    stripped = image_data.strip()
    match = _DATA_URL_RE.match(stripped)
    if not match:
        return stripped, None
    fmt = (match.group("fmt") or "").lower()
    if fmt == "jpg":
        fmt = "jpeg"
    return match.group("b64") or "", (fmt or None)
