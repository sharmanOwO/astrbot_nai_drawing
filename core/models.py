"""插件使用的数据结构

这些 dataclass 用于描述最近生成图片、提示词结果
和会话运行时状态，便于命令处理与服务层之间传递结构化数据。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(slots=True)
class RecentImageRecord:
    """记录已发送图片的消息 ID 和提示词，用于撤回和追踪会话消息"""
    message_id: str
    prompt: str
    created_at: float


@dataclass(slots=True)
class PromptBuildResult:
    """LLM 生成的最终提示词及用于展示的提示词"""
    prompt: str
    display_prompt: str


@dataclass(slots=True)
class SessionContext:
    """从 AstrBot 事件提取用户在会话中的身份与用户的权限信息"""
    platform: str
    platform_id: str
    chat_id: str
    user_id: str
    group_id: str | None
    is_group: bool
    is_admin: bool

    @property
    def session_key(self) -> str:
        """返回适配器实例和会话组成的稳定键值对"""
        return f"{self.platform_id}:{self.chat_id}"

    @classmethod
    def from_event(cls, event: Any) -> "SessionContext":
        """从 AstrBot 事件提取群聊/私聊、用户和管理员信息"""
        group_id = str(event.get_group_id() or "")
        user_id = str(event.get_sender_id() or "")
        platform = str(event.get_platform_name() or "")
        platform_id = str(getattr(event, "get_platform_id", lambda: "")() or platform)
        private_key = str(getattr(event, "unified_msg_origin", "") or user_id)
        return cls(
            platform=platform,
            platform_id=platform_id,
            chat_id=group_id or private_key,
            user_id=user_id,
            group_id=group_id or None,
            is_group=bool(group_id),
            is_admin=bool(getattr(event, "is_admin", lambda: False)()),
        )


@dataclass(slots=True)
class SessionRuntimeState:
    """只在内存中存在的会话级临时选择和生图参数覆盖"""
    admin_mode: bool | None = None
    recall_enabled: bool | None = None
    nsfw_filter_enabled: bool | None = None
    prompt_show_enabled: bool | None = None
    generation_overrides: dict[str, Any] = field(default_factory=dict)
    seed_mode: Literal["fixed", "random"] | None = None
    fixed_seed: int | None = None
    last_seed: int | None = None
    recent_images: deque[RecentImageRecord] = field(
        default_factory=lambda: deque(maxlen=20)
    )
