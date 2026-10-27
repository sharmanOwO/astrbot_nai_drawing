"""角色和画师预设的本地持久化管理"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


PUBLIC_OWNER_ID = "public"
_PRESET_KINDS = frozenset({"character", "artist"})
_KIND_ALIASES = {
    "character": "character",
    "c": "character",
    "artist": "artist",
    "a": "artist",
}
_INLINE_PRESET_PATTERN = re.compile(
    # 名称允许跨行，实际比较时会统一去掉所有空白字符
    r"(?<![A-Za-z0-9_])(?P<kind>character|artist|c|a)\s*=\s*(?P<name>[^,|]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Preset:
    """写入 presets.json 的一条角色或画师预设"""

    title: str
    content: str
    owner_id: str
    type: str


def normalize_preset_name(value: str) -> str:
    """生成预设名称的比较键，忽略大小写和所有空白字符"""
    return "".join(str(value).casefold().split())


def normalize_preset_kind(value: str) -> str | None:
    """把 character、artist 及其简写统一成存储类型"""
    return _KIND_ALIASES.get(str(value).strip().casefold())


class PresetManager:
    """管理插件目录内的 presets.json 与角色保持预留目录"""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)
        self.data_file = self.base_dir / "presets.json"
        self.character_keep_dir = self.base_dir / "cs"
        self._data: dict[str, dict[str, dict[str, str]]] | None = None

    def add(self, owner_id: str, kind: str, title: str, content: str) -> Preset:
        """新增或覆盖同一拥有者、类型和名称的预设"""
        preset_kind = self._require_kind(kind)
        clean_title = str(title).strip()
        clean_content = str(content).strip()
        if not clean_title:
            raise ValueError("预设名称不能为空")
        if not clean_content:
            raise ValueError("预设内容不能为空")

        preset = Preset(
            title=clean_title,
            content=clean_content,
            owner_id=str(owner_id).strip() or PUBLIC_OWNER_ID,
            type=preset_kind,
        )
        self._load()["presets"][self._key_for(preset.owner_id, preset.type, preset.title)] = asdict(preset)
        self._save()
        return preset

    def list(self, owner_id: str, kind: str) -> list[Preset]:
        """列出一个拥有者指定类型的预设，按名称排序"""
        preset_kind = self._require_kind(kind)
        target_owner = str(owner_id).strip() or PUBLIC_OWNER_ID
        matches = [
            self._preset_from_dict(item)
            for item in self._load()["presets"].values()
            if item.get("owner_id") == target_owner and item.get("type") == preset_kind
        ]
        return sorted(matches, key=lambda item: normalize_preset_name(item.title))

    def resolve(self, owner_id: str, kind: str, name: str) -> Preset | None:
        """按个人优先、公共回退的顺序查找预设"""
        preset_kind = self._require_kind(kind)
        normalized_name = normalize_preset_name(name)
        if not normalized_name:
            return None

        owners = (str(owner_id).strip(), PUBLIC_OWNER_ID)
        for candidate_owner in owners:
            if not candidate_owner:
                continue
            item = self._load()["presets"].get(
                self._key_for(candidate_owner, preset_kind, normalized_name)
            )
            if item is not None:
                return self._preset_from_dict(item)
        return None

    def sync_panel_artists(self, raw_list: Any) -> list[Preset]:
        """把配置面板中的画师预设写入公共 artist 预设"""
        if not isinstance(raw_list, list):
            return []

        synced: list[Preset] = []
        for index, item in enumerate(raw_list, start=1):
            if isinstance(item, str):
                title = f"画师预设 {index}"
                content = item
            elif isinstance(item, dict):
                title = str(item.get("name") or item.get("title") or f"画师预设 {index}")
                content = str(item.get("prompt") or item.get("content") or "")
            else:
                continue

            if content.strip():
                synced.append(self.add(PUBLIC_OWNER_ID, "artist", title, content))
        return synced

    def expand_inline(self, owner_id: str, prompt: str) -> tuple[str, str]:
        """展开提示词中的 character/c 和 artist/a 预设引用

        角色预设直接替换原片段，画师预设从用户提示词移出并作为第二个返回值
        未找到的预设会保留原文，方便用户发现名称写错
        """
        artist_parts: list[str] = []

        def replace(match: re.Match[str]) -> str:
            kind = normalize_preset_kind(match.group("kind"))
            preset = self.resolve(owner_id, kind or "", match.group("name")) if kind else None
            if preset is None:
                return match.group(0)

            content = self._content_for_prompt(preset.content)
            if kind == "artist":
                artist_parts.append(content)
                return ""
            return content

        expanded = _INLINE_PRESET_PATTERN.sub(replace, str(prompt))
        # 删除画师引用留下的空逗号，不改变角色提示词中的正常逗号
        expanded = re.sub(r"(?:\s*,\s*){2,}", ", ", expanded)
        expanded = re.sub(r"\s*,\s*(?=\|)", " ", expanded)
        expanded = re.sub(r"^\s*,\s*|\s*,\s*$", "", expanded)
        expanded = re.sub(r"^\s*\|\s*|\s*\|\s*(?=\||$)", " ", expanded)
        return expanded.strip(), ", ".join(part for part in artist_parts if part).strip()

    def character_keep_path(self, owner_id: str, name: str) -> Path:
        """返回角色保持预留文本的安全路径，并创建所属用户目录"""
        safe_owner = self._safe_file_part(owner_id, "user")
        safe_name = self._safe_file_part(name, "character")
        directory = self.character_keep_dir / safe_owner
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{safe_name}.txt"

    def write_character_keep(self, owner_id: str, name: str, content: str) -> Path:
        """写入角色保持预留文本，当前不参与任何生图请求"""
        path = self.character_keep_path(owner_id, name)
        path.write_text(str(content), encoding="utf-8")
        return path

    def _load(self) -> dict[str, dict[str, dict[str, str]]]:
        """首次访问时读取预设文件，不存在时建立空数据结构"""
        if self._data is not None:
            return self._data

        self.base_dir.mkdir(parents=True, exist_ok=True)
        if not self.data_file.exists():
            self._data = {"presets": {}}
            return self._data

        try:
            raw = json.loads(self.data_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"无法读取预设文件 {self.data_file}") from exc

        entries = raw.get("presets") if isinstance(raw, dict) else None
        if not isinstance(entries, dict):
            raise ValueError("presets.json 的 presets 字段必须是对象")

        normalized: dict[str, dict[str, str]] = {}
        for value in entries.values():
            if not isinstance(value, dict):
                continue
            try:
                preset = self._preset_from_dict(value)
            except ValueError:
                continue
            normalized[self._key_for(preset.owner_id, preset.type, preset.title)] = asdict(preset)
        self._data = {"presets": normalized}
        return self._data

    def _save(self) -> None:
        """将内存中的预设整体写回插件目录"""
        data = self._load()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.data_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _content_for_prompt(content: str) -> str:
        """兼容旧数据中的 tag= 前缀，传给 NovelAI 时只保留标签正文"""
        return re.sub(r"^\s*tag\s*=\s*", "", str(content), count=1, flags=re.IGNORECASE).strip()

    @staticmethod
    def _safe_file_part(value: str, fallback: str) -> str:
        """替换 Windows 文件名不能使用的字符"""
        clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value).strip())
        clean = clean.strip(". ")
        return clean or fallback

    @staticmethod
    def _key_for(owner_id: str, kind: str, title_or_normalized_name: str) -> str:
        """组成稳定复合键，允许个人与公共预设使用同名标题"""
        return f"{str(owner_id).strip()}::{kind}::{normalize_preset_name(title_or_normalized_name)}"

    @staticmethod
    def _preset_from_dict(value: dict[str, Any]) -> Preset:
        """校验磁盘记录并转换成模块内使用的预设对象"""
        kind = normalize_preset_kind(str(value.get("type") or ""))
        title = str(value.get("title") or "").strip()
        content = str(value.get("content") or "").strip()
        owner_id = str(value.get("owner_id") or "").strip()
        if kind not in _PRESET_KINDS or not title or not content or not owner_id:
            raise ValueError("预设记录缺少 title、content、owner_id 或 type")
        return Preset(title=title, content=content, owner_id=owner_id, type=kind)

    @staticmethod
    def _require_kind(kind: str) -> str:
        """验证调用方传入的是角色或画师预设类型"""
        normalized = normalize_preset_kind(kind)
        if normalized not in _PRESET_KINDS:
            raise ValueError("预设类型只能是 character、artist、c 或 a")
        return normalized
