"""NovelAI 提示词历史的本地持久化管理"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


TEMPORARY_LIMIT = 20
_BEIJING_TIMEZONE = timezone(timedelta(hours=8))


class PromptHistoryStore:
    """管理插件目录内 prompt.json 的会话级提示词历史"""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)
        self.data_file = self.base_dir / "prompt.json"
        self._records: list[dict[str, Any]] | None = None

    def add(
        self,
        session: Any,
        user_id: str,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        """保存一条历史记录，并裁剪当前会话过多的 temporary 记录"""
        normalized = self._normalize_record(session, user_id, record)
        records = self._load()
        records.append(normalized)
        self._trim_temporary_records(session, records)
        self._save()
        return copy.deepcopy(normalized)

    def list_for_session(self, session: Any) -> list[dict[str, Any]]:
        """按从新到旧的顺序返回当前群聊或私聊的全部历史"""
        matched = [item for item in self._load() if self._matches_session(item, session)]
        return [copy.deepcopy(item) for item in reversed(matched)]

    def get_by_ordinal(self, session: Any, ordinal: int) -> dict[str, Any] | None:
        """获取当前会话倒数第 ordinal 条记录，ordinal 从 1 开始"""
        if ordinal < 1:
            return None
        records = self.list_for_session(session)
        if ordinal > len(records):
            return None
        return records[ordinal - 1]

    def get_by_timestamp(self, session: Any, request_timestamp: str) -> dict[str, Any] | None:
        """按请求时间戳获取当前会话的历史记录"""
        target = str(request_timestamp).strip()
        for item in self.list_for_session(session):
            if str(item.get("request_timestamp") or "") == target:
                return item
        return None

    def get(self, session: Any, ordinal_or_timestamp: int | str) -> dict[str, Any] | None:
        """同时支持 1 这类倒数序号和 request_timestamp 查询"""
        if isinstance(ordinal_or_timestamp, int):
            return self.get_by_ordinal(session, ordinal_or_timestamp)

        value = str(ordinal_or_timestamp).strip()
        if value.isdigit() and len(value) <= 2:
            return self.get_by_ordinal(session, int(value))
        return self.get_by_timestamp(session, value)

    def save(self, session: Any, ordinal_or_timestamp: int | str) -> dict[str, Any] | None:
        """把指定记录改为 permanent，使其不受 temporary 数量限制"""
        target = self.get(session, ordinal_or_timestamp)
        if target is None:
            return None

        timestamp = str(target["request_timestamp"])
        for item in reversed(self._load()):
            if self._matches_session(item, session) and str(item.get("request_timestamp")) == timestamp:
                item["type"] = "permanent"
                self._save()
                return copy.deepcopy(item)
        return None

    def render(self, record: dict[str, Any]) -> str:
        """按命令回复需要的字段展示一条历史提示词"""
        prompt = record.get("prompt") if isinstance(record.get("prompt"), dict) else {}
        negative = (
            record.get("negative_prompt")
            if isinstance(record.get("negative_prompt"), dict)
            else {}
        )
        return "\n".join(
            [
                f"prompt{record.get('request_timestamp', '')}:",
                "prompt:",
                f'global_prompt:{self._quoted(prompt.get("global_prompt", ""))}',
                f'artist:{self._quoted(prompt.get("artist", ""))}',
                f'base_caption:{self._quoted(prompt.get("base_caption", ""))}',
                f"char_captions:{self._caption_text(prompt.get('char_captions', []))}",
                "negative_prompt:",
                f'global_negative_prompt:{self._quoted(negative.get("global_negative_prompt", ""))}',
                f'base_caption:{self._quoted(negative.get("base_caption", ""))}',
                f"char_captions:{self._caption_text(negative.get('char_captions', []))}",
            ]
        )

    @staticmethod
    def new_timestamp() -> str:
        """生成北京时间月日时分秒毫秒格式的请求时间戳"""
        return datetime.now(_BEIJING_TIMEZONE).strftime("%m%d%H%M%S%f")[:-3]

    def _load(self) -> list[dict[str, Any]]:
        """首次访问时读入 prompt.json，不存在时返回空列表"""
        if self._records is not None:
            return self._records

        self.base_dir.mkdir(parents=True, exist_ok=True)
        if not self.data_file.exists():
            self._records = []
            return self._records

        try:
            raw = json.loads(self.data_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"无法读取提示词历史文件 {self.data_file}") from exc

        if isinstance(raw, dict):
            raw = raw.get("prompts", raw.get("records"))
        if not isinstance(raw, list):
            raise ValueError("prompt.json 必须是记录数组")
        self._records = [item for item in raw if isinstance(item, dict)]
        return self._records

    def _save(self) -> None:
        """将全部历史以数组形式写回 prompt.json"""
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.data_file.write_text(
            json.dumps(self._load(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _normalize_record(
        self,
        session: Any,
        user_id: str,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        """补齐历史所需字段，保留调用方准备好的结构化提示词内容"""
        source = copy.deepcopy(record)
        prompt = self._normalize_caption_section(source.get("prompt"), "global_prompt", True)
        negative = self._normalize_caption_section(
            source.get("negative_prompt"), "global_negative_prompt", False
        )
        record_type = str(source.get("type") or "temporary").strip().lower()
        if record_type not in {"temporary", "permanent"}:
            record_type = "temporary"

        normalized = {
            "user_id": str(user_id),
            "group_id": self._group_id(session),
            "request_timestamp": str(source.get("request_timestamp") or self.new_timestamp()),
            "response_time": source.get("response_time"),
            "type": record_type,
            "prompt": prompt,
            "negative_prompt": negative,
        }
        generation = source.get("generation")
        if isinstance(generation, dict):
            normalized["generation"] = copy.deepcopy(generation)
        return normalized

    @staticmethod
    def _normalize_caption_section(
        value: Any,
        global_key: str,
        include_artist: bool,
    ) -> dict[str, Any]:
        """统一正面或负面提示词段落，避免历史文件缺少必要字段"""
        source = value if isinstance(value, dict) else {}
        result: dict[str, Any] = {
            global_key: str(source.get(global_key) or ""),
            "base_caption": str(source.get("base_caption") or ""),
            "char_captions": copy.deepcopy(
                source.get("char_captions") if isinstance(source.get("char_captions"), list) else []
            ),
        }
        if include_artist:
            result["artist"] = str(source.get("artist") or "")
        return result

    @staticmethod
    def _group_id(session: Any) -> str | None:
        """群聊使用群号，私聊在文件中按要求记录为 null"""
        explicit_group_id = getattr(session, "group_id", None)
        if explicit_group_id not in (None, ""):
            return str(explicit_group_id)
        if bool(getattr(session, "is_group", False)):
            chat_id = getattr(session, "chat_id", None)
            return str(chat_id) if chat_id not in (None, "") else None
        return None

    @classmethod
    def _matches_session(cls, record: dict[str, Any], session: Any) -> bool:
        """群聊按群号共享记录，私聊再按用户号隔离不同用户"""
        group_id = cls._group_id(session)
        if group_id is not None:
            return str(record.get("group_id") or "") == group_id
        return record.get("group_id") is None and str(record.get("user_id") or "") == str(
            getattr(session, "user_id", "")
        )

    @classmethod
    def _trim_temporary_records(cls, session: Any, records: list[dict[str, Any]]) -> None:
        """只删除当前会话最早的 temporary 记录，永久记录一直保留"""
        temporary_indexes = [
            index
            for index, item in enumerate(records)
            if cls._matches_session(item, session) and item.get("type") == "temporary"
        ]
        excess = len(temporary_indexes) - TEMPORARY_LIMIT
        if excess <= 0:
            return
        for index in reversed(temporary_indexes[:excess]):
            del records[index]

    @staticmethod
    def _quoted(value: Any) -> str:
        """使用 JSON 字符串格式保留换行和引号，便于用户复制修改"""
        return json.dumps(str(value or ""), ensure_ascii=False)

    @staticmethod
    def _caption_text(value: Any) -> str:
        """把角色提示词与坐标完整展示成可读的 JSON 数组"""
        captions = value if isinstance(value, list) else []
        return json.dumps(captions, ensure_ascii=False)


PromptHistoryManager = PromptHistoryStore
