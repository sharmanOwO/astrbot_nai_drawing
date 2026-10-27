"""模型配置与会话配置的解析

本模块集中处理配置路径读取、画师预设、管理员权限和自动撤回开关
"""

from __future__ import annotations

from typing import Any

from .constants import (
    DEFAULT_NEGATIVE_FILTER_TAG,
    SIZE_ALIASES,
    SIZE_DISPLAY_NAMES,
)
from .models import SessionContext
from .session_state import SessionStateStore


def get_config_value(config: dict[str, Any], path: str, default: Any = None) -> Any:
    """按点号路径读取配置，这里会嵌套读取每一层字典，任何中间节点类型不正确时返回默认值。"""
    current: Any = config
    for key in path.split("."):
        if not isinstance(current, dict):
            return default
        if key not in current:
            return default
        current = current[key]
    return current


def normalize_size_alias(raw_value: str) -> str | None:
    """把竖图、横图、方图等快捷名称转换为具体的尺寸
     比如  "竖": "832x1216"
        "landscape": "1216x832"
     """
    value = (raw_value or "").strip().lower()
    if not value:
        return None
    return SIZE_ALIASES.get(value, raw_value.strip())


def size_display_name(size: str) -> str:
    """返回面向用户展示的图片尺寸名称。"""
    return SIZE_DISPLAY_NAMES.get(size, size)


def get_session_provider_id(
    config: dict[str, Any],
    primary_path: str,
    *fallback_paths: str,
) -> str:
    """从配置主路径和兼容回退路径读取 Provider ID。"""
    for path in (primary_path, *fallback_paths):
        value = str(get_config_value(config, path, "") or "").strip()
        if value:
            return value
    return ""


def is_plugin_admin(
    config: dict[str, Any],
    session: SessionContext,
) -> bool:
    """判断用户是否在插件管理员列表或 AstrBot 全局管理员中。"""
    admin_users = get_config_value(config, "admin.admin_users", [])
    if isinstance(admin_users, list) and str(session.user_id) in {
        str(item) for item in admin_users
    }:
        return True
    return session.is_admin


def is_admin_mode_enabled(
    config: dict[str, Any],
    session: SessionContext,
    states: SessionStateStore,
) -> bool:
    """实际上在插件的其他地方没有发现 state.admin_mode 的相关修改
    所以这个函数只取决于有没有开启默认管理员模式"""
    state = states.get(session)
    if state.admin_mode is not None:
        return state.admin_mode
    return bool(get_config_value(config, "admin.default_admin_mode", False))


def can_use_generation(
    config: dict[str, Any],
    session: SessionContext,
    states: SessionStateStore,
) -> bool:
    """根据管理员模式判断当前用户有无生图权限"""
    if not is_admin_mode_enabled(config, session, states):
        return True
    return is_plugin_admin(config, session)


def is_prompt_show_enabled(
    config: dict[str, Any],
    session: SessionContext,
    states: SessionStateStore,
) -> bool:
    """判断是否在生成前展示最终提示词"""
    state = states.get(session)
    if state.prompt_show_enabled is not None:
        return state.prompt_show_enabled
    return bool(get_config_value(config, "prompt_show.enabled", False))


def is_recall_enabled(
    config: dict[str, Any],
    session: SessionContext,
    states: SessionStateStore,
) -> bool:
    """判断是否启用自动撤回"""
    state = states.get(session)
    if state.recall_enabled is not None:
        return state.recall_enabled
    return bool(get_config_value(config, "auto_recall.enabled", False))


def is_nsfw_filter_enabled(
    config: dict[str, Any],
    session: SessionContext,
    states: SessionStateStore,
) -> bool:
    """判断当前会话是否开启 NSFW 过滤"""
    state = states.get(session)
    if state.nsfw_filter_enabled is not None:
        return state.nsfw_filter_enabled   # 优先使用会话中配置的 True/False
    return bool(get_config_value(config, "nsfw_filter.enabled", False))  # None 时再读全局配置


def recall_is_allowed_in_session(config: dict[str, Any], session: SessionContext) -> bool:
    """判断当前会话是否处于自动撤回白名单"""
    allowed_groups = get_config_value(config, "auto_recall.allowed_groups", [])
    if not isinstance(allowed_groups, list) or not allowed_groups:
        return True
    return session.session_key in {str(item) for item in allowed_groups}


def resolve_model_config(
    config: dict[str, Any],
    session: SessionContext,
    states: SessionStateStore,
) -> dict[str, Any]:
    """生成当前会话最终使用的配置

    当前只有 NovelAI V5 Full，配置中只需要保留公共参数和会话临时选项
    """
    base = get_config_value(config, "model", {})
    merged = dict(base) if isinstance(base, dict) else {}
    state = states.get(session)
    # 每个会话只保存与面板不同的字段，合并后才是一次请求真正使用的值
    merged.update(state.generation_overrides)
    merged["model"] = "nai-diffusion-5-full"

    # NSFW 过滤标签单独保存，不能改写面板的全局负面提示词
    # 否则重绘历史记录时会把后来修改过的面板内容混入旧提示词
    if is_nsfw_filter_enabled(config, session, states):
        filter_tags = str(
            get_config_value(
                config,
                "nsfw_filter.filter_tags",
                DEFAULT_NEGATIVE_FILTER_TAG,
            )
            or DEFAULT_NEGATIVE_FILTER_TAG
        ).strip()
        if filter_tags:
            merged["_nsfw_filter_tags"] = filter_tags

    return merged


def can_change_generation_config(config: dict[str, Any], session: SessionContext) -> bool:
    """判断用户是否在可修改会话生图参数的用户白名单中"""
    users = get_config_value(config, "permissions.user_whitelist", [])
    return isinstance(users, list) and str(session.user_id) in {
        str(item) for item in users
    }


def build_help_text(config: dict[str, Any]) -> str:
    """ /nai help 使用的帮助文本"""
    return "\n".join(
        [
            "📖 NAI 生图插件命令",
            "/nai draw <描述>：自然语言文生图",
            "/nai tag <英文标签>：直接使用英文标签生图（兼容 /nai0）",
            "/nai i2i <描述>：自然语言图生图",
            "/nai0 i2i <英文标签>：英文标签图生图",
            "/nai0 positiondraw <英文标签>：使用角色坐标的英文标签文生图",
            "/nai help：查看帮助",
            "/nai size [尺寸]：查看或切换尺寸",
            "/nai nsfw [on|off]： NSFW 过滤开关",
            "/naiconfig：查看当前会话生图参数",
            "/naiset <参数> [值]：修改或还原当前会话参数",
            "/preset、/publicpreset：管理个人和公共预设",
            "/naiprompt：查看、保存、修改并复用提示词历史",
            "当前模型：NAI V5 Full（nai-diffusion-5-full）",
        ]
    )
