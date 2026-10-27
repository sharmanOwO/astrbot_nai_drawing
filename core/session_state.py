"""存储临时会话状态

状态仅用于当前 AstrBot 进程，也就是会话中临时设置的生图参数、NSFW 和自动撤回的情况；不会写入磁盘
"""

from __future__ import annotations

import time
from collections import deque
from typing import Iterable

from .models import RecentImageRecord, SessionContext, SessionRuntimeState


class SessionStateStore:
    """记录会话状态"""

    def __init__(self) -> None:
        self._states: dict[str, SessionRuntimeState] = {}

    def get(self, session: SessionContext) -> SessionRuntimeState:
        """获取会话状态；首次访问时自动创建"""
        return self._states.setdefault(session.session_key, SessionRuntimeState())

    def set_generation_override(
        self,
        session: SessionContext,
        field: str,
        value: object,
    ) -> None:
        """保存某个会话参数，生成时会覆盖配置面板里的同名默认值"""
        self.get(session).generation_overrides[field] = value

    def clear_generation_override(self, session: SessionContext, field: str) -> None:
        """移除会话覆盖，让该参数重新从插件面板读取"""
        self.get(session).generation_overrides.pop(field, None)

    def set_fixed_seed(self, session: SessionContext, seed: int) -> None:
        """设置固定种子，后续请求不会重新随机生成"""
        state = self.get(session)
        state.seed_mode = "fixed"
        state.fixed_seed = seed

    def set_random_seed(self, session: SessionContext) -> None:
        """切换为每次请求重新生成随机种子"""
        state = self.get(session)
        state.seed_mode = "random"
        state.fixed_seed = None

    def clear_seed_override(self, session: SessionContext) -> None:
        """放弃会话种子设置，重新遵从配置面板的 seed 默认值"""
        state = self.get(session)
        state.seed_mode = None
        state.fixed_seed = None

    def track_image(
        self,
        session: SessionContext,
        message_id: str,
        prompt: str,
    ) -> None:
        """记录最近发送的图片，最多保留 20 条"""
        state = self.get(session)
        state.recent_images.appendleft(
            RecentImageRecord(
                message_id=str(message_id),
                prompt=prompt,
                created_at=time.time(),
            )
        )

    def recent_images(self, session: SessionContext) -> Iterable[RecentImageRecord]:
        """返回当前会话中最近的图片"""
        return tuple(self.get(session).recent_images)

    def find_recent_image(
        self,
        session: SessionContext,
        message_id: str,
    ) -> RecentImageRecord | None:
        """按消息 ID 查找生成的已记录的图片"""
        for item in self.get(session).recent_images:
            if item.message_id == str(message_id):
                return item
        return None

    def latest_image(self, session: SessionContext) -> RecentImageRecord | None:
        """返回最近一张生成图片，没有记录时返回 ``None``"""
        state = self.get(session)
        return state.recent_images[0] if state.recent_images else None

    def prune_expired_images(
        self,
        session: SessionContext,
        max_age_seconds: float,
    ) -> None:
        """删除超过指定缓存时间限制的图片记录，避免内存爆炸"""
        state = self.get(session)
        now = time.time()
        valid = [
            item
            for item in state.recent_images
            if (now - item.created_at) <= max_age_seconds
        ]
        state.recent_images = deque(valid, maxlen=20)
