"""复用提示词历史时的点号路径修改工具"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Callable


_OPERATION_RE = re.compile(
    r"^\s*(?P<path>prompt\.(?:artist|base_caption|char_?captions\[\d+\](?:\.(?:char_caption|centers))?))\s*"
    # 匹配如 prompt.char_captions[0] 等
    # (?P<path>...)：这里的 ?P<path> 是 Python 专属语法，表示定义一个命名捕获组。
    # 
    #(?:...)（出现两次）：这里的 ?: 表示非捕获组。意思是括号内的内容作为一个整体进行匹配，但不会单独保存为子组（节省内存，提高性能）
    #
    # char_?：这里的 ? 修饰前面的下划线 _，表示 下划线可有可无。所以它既能匹配 char_captions，也能匹配 charcaptions（不带下划线）
    # 
    # (?:\.(?:char_caption|centers))?：末尾括号外的 ? 修饰整个组，表示 后缀部分可有可无
    r"(?P<operator>[=+-])\s*(?P<value>.*?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_CAPTION_RE = re.compile(r"^prompt\.char_?captions\[(\d+)\](?:\.(char_caption|centers))?$", re.IGNORECASE)


def apply_history_edits(
    record: dict[str, Any],
    edit_text: str,
    expand_presets: Callable[[str], tuple[str, str]],
) -> dict[str, Any]:
    """对历史记录应用一个或多个以反斜杠分隔的修改语句"""
    result = copy.deepcopy(record)
    text = str(edit_text or "").strip()
    if not text:
        return result

    operations = [part.strip() for part in text.split("\\") if part.strip()]
    if not operations:
        raise ValueError("没有找到可执行的提示词修改语句")
    for operation in operations:
        _apply_one(result, operation, expand_presets)
    return result


def _apply_one(
    record: dict[str, Any],
    statement: str,
    expand_presets: Callable[[str], tuple[str, str]],
) -> None:
    """解析单条路径、运算符和值，并原地修改记录"""
    match = _OPERATION_RE.match(statement)
    if match is None:
        raise ValueError(f"无法识别修改语句: {statement}")

    path = match.group("path").replace("charcaptions", "char_captions")
    operator = match.group("operator")
    raw_value = match.group("value").strip()
    prompt = record.setdefault("prompt", {})
    if not isinstance(prompt, dict):
        raise ValueError("历史记录缺少 prompt 对象")

    if path.casefold() == "prompt.artist":
        _set_text(prompt, "artist", operator, raw_value, expand_presets)
        return
    if path.casefold() == "prompt.base_caption":
        _set_text(prompt, "base_caption", operator, raw_value, expand_presets)
        return

    caption_match = _CAPTION_RE.match(path)
    if caption_match is None:
        raise ValueError(f"不支持的路径: {path}")
    index = int(caption_match.group(1))
    member = caption_match.group(2) or "char_caption"
    captions = prompt.setdefault("char_captions", [])
    if not isinstance(captions, list) or index >= len(captions):
        raise ValueError(f"角色序号 {index} 不存在")
    caption = captions[index]
    if not isinstance(caption, dict):
        raise ValueError(f"角色序号 {index} 格式不正确")
    if member.casefold() == "centers":
        if operator != "=":
            raise ValueError("centers 仅支持 = 覆写")
        caption["centers"] = _parse_centers(raw_value)
        return
    _set_text(caption, "char_caption", operator, raw_value, expand_presets)


def _set_text(
    container: dict[str, Any],
    key: str,
    operator: str,
    raw_value: str,
    expand_presets: Callable[[str], tuple[str, str]],
) -> None:
    """按 =、+、- 覆写、追加或删除逗号分隔标签"""
    expanded, artist = expand_presets(raw_value)
    value = expanded.strip().strip(",")
    if key == "artist" and artist:
        value = ", ".join(part for part in (value, artist) if part)
    existing = str(container.get(key) or "").strip()
    if operator == "=":
        container[key] = value
    elif operator == "+":
        container[key] = ", ".join(part for part in (existing, value) if part)
    else:
        remove_keys = {_tag_key(part) for part in value.split(",") if _tag_key(part)}
        kept = [part.strip() for part in existing.split(",") if _tag_key(part) not in remove_keys]
        container[key] = ", ".join(part for part in kept if part)


def _tag_key(value: str) -> str:
    """忽略英文大小写、空格和换行比较一项标签"""
    # 删除操作按约定只比较 ASCII 英文和数字，空格及其他分隔符不参与匹配
    return "".join(
        char
        for char in str(value).casefold()
        if ("a" <= char <= "z") or ("0" <= char <= "9")
    )


def _parse_centers(value: str) -> list[dict[str, float]]:
    """接受 JSON、(x,y) 和 (x=...,y=...) 三种坐标写法"""
    text = str(value).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        normalized = _normalize_centers(parsed)
        if normalized:
            return normalized

    match = re.fullmatch(
        r"\(\s*(?:x\s*=\s*)?(?P<x>-?\d+(?:\.\d+)?)\s*,\s*(?:y\s*=\s*)?(?P<y>-?\d+(?:\.\d+)?)\s*\)",
        text,
        flags=re.IGNORECASE,
    )
    if match is None:
        raise ValueError("centers 应为 [{\"x\":0.5,\"y\":0.5}]、(0.5,0.5) 或 (x=0.5,y=0.5)")
    return [{"x": _coordinate(match.group("x")), "y": _coordinate(match.group("y"))}]


def _normalize_centers(value: list[Any]) -> list[dict[str, float]]:
    """校验 JSON 数组中的坐标对象"""
    result: list[dict[str, float]] = []
    for item in value:
        if not isinstance(item, dict) or "x" not in item or "y" not in item:
            continue
        result.append({"x": _coordinate(item["x"]), "y": _coordinate(item["y"])})
    return result


def _coordinate(value: Any) -> float:
    """把坐标限制在 NovelAI 使用的 0 到 1 范围内"""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("坐标必须是数字") from exc
    return max(0.0, min(1.0, number))
