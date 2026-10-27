"""插件入口，注册绘图、会话配置、预设和提示词历史命令"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .core.clients import NaiWebClient
from .core.config import (
    build_help_text,
    can_change_generation_config,
    can_use_generation,
    get_config_value,
    is_plugin_admin,
    is_prompt_show_enabled,
    normalize_size_alias,
    resolve_model_config,
    size_display_name,
)
from .core.message_utils import extract_first_image_input, send_text_message
from .core.models import SessionContext
from .core.presets import PUBLIC_OWNER_ID, PresetManager, normalize_preset_kind
from .core.prompt_editing import apply_history_edits
from .core.prompt_history import PromptHistoryStore
from .core.services import ImageService, LLMService, PromptGeneratorService
from .core.session_state import SessionStateStore


@register("astrbot-nai-drawing", "sharmanOwO", "NovelAI-Drawing", "1.0.0")
class Main(Star):
    """AstrBot Star，负责把命令参数交给相应服务"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context, config)
        self.context = context
        self.config: dict[str, Any] = dict(config or {})
        self.states = SessionStateStore()
        self.nai_client = NaiWebClient()
        base_dir = Path(__file__).resolve().parent
        self.presets = PresetManager(base_dir)
        self.history = PromptHistoryStore(base_dir)
        # 面板中的画师列表作为公共画师预设导入，之后统一由 presets.json 管理
        self.presets.sync_panel_artists(get_config_value(self.config, "model.artist_presets", []))
        self.llm_service = LLMService(self.context, self.config)
        self.prompt_service = PromptGeneratorService(self.config, self.states, self.llm_service)
        self.image_service = ImageService(
            self.config, self.states, self.nai_client, self.presets, self.history
        )

    async def terminate(self) -> None:
        """插件卸载时关闭官方 API 使用的 HTTP 连接池"""
        await self.nai_client.close()

    # ── nai 命令组 ────────────────────────────────────────────

    @filter.command_group("nai")
    def nai_group(self):
        """NovelAI 图片生成命令组"""
        pass

    @nai_group.command("draw")
    async def nai_draw(self, event: AstrMessageEvent) -> None:
        """自然语言描述文生图"""
        # 命令由插件直接处理；禁止 AstrBot 随后再次触发默认会话 LLM
        # _handle_draw 内部的提示词生成仍会主动调用插件配置的 LLM provider
        event.should_call_llm(True)
        session = SessionContext.from_event(event)
        request_text = self._subcommand_argument(event.message_str)
        if not request_text:
            await send_text_message(event, "用法：/nai draw <描述>")
            return
        await self._handle_draw(event, session, request_text)

    @nai_group.command("tag")
    async def nai_tag(self, event: AstrMessageEvent) -> None:
        """直接使用英文标签文生图"""
        # 这是直接执行的命令，不应在插件处理后再触发默认会话 LLM
        event.should_call_llm(True)
        session = SessionContext.from_event(event)
        tags = self._subcommand_argument(event.message_str)
        if not tags:
            await send_text_message(event, "用法：/nai tag 1girl, Hatsune Miku, smile")
            return
        await self._handle_tag(event, session, tags)

    @nai_group.command("i2i")
    async def nai_i2i(self, event: AstrMessageEvent) -> None:
        """用自然语言描述图生图"""
        # 自然语言转标签由插件内部显式调用 LLM；禁止同一事件再走默认 LLM
        event.should_call_llm(True)
        session = SessionContext.from_event(event)
        request_text = self._subcommand_argument(event.message_str)
        if not request_text:
            await send_text_message(event, "用法：/nai i2i <描述>（请同时发送或引用一张图片）")
            return
        await self._handle_i2i(event, session, request_text, natural_language=True)

    @nai_group.command("help")
    async def nai_help(self, event: AstrMessageEvent) -> None:
        """发送可用命令清单"""
        event.should_call_llm(True)
        await send_text_message(event, build_help_text(self.config))

    @nai_group.command("size")
    async def nai_size(self, event: AstrMessageEvent) -> None:
        """查看或切换当前会话的宽高组合"""
        event.should_call_llm(True)
        await self._handle_size_switch(
            event, SessionContext.from_event(event), self._subcommand_argument(event.message_str)
        )

    @nai_group.command("nsfw")
    async def nai_nsfw(self, event: AstrMessageEvent) -> None:
        """查看或切换 NSFW 负面过滤"""
        event.should_call_llm(True)
        await self._handle_nsfw(
            event, SessionContext.from_event(event), self._subcommand_argument(event.message_str)
        )

    # ── 独立绘图命令 ─────────────────────────────────────────

    @filter.command("nai0")
    async def nai0(self, event: AstrMessageEvent) -> None:
        """直接使用英文标签，兼容 /nai tag、/nai0 i2i 和 positiondraw"""
        # /nai0 是直接执行英文标签的命令，不应触发默认会话 LLM
        event.should_call_llm(True)
        session = SessionContext.from_event(event)
        remainder = self._command_remainder(event.message_str)
        if not remainder:
            await send_text_message(
                event,
                "用法：/nai0 <英文标签>、/nai0 i2i <英文标签> 或 /nai0 positiondraw <英文标签>",
            )
            return
        command_parts = remainder.split(maxsplit=1)
        command = command_parts[0].casefold()
        value = command_parts[1].strip() if len(command_parts) > 1 else ""
        if command == "i2i":
            if not value:
                await send_text_message(event, "用法：/nai0 i2i <英文标签>（请同时发送或引用一张图片）")
                return
            await self._handle_i2i(event, session, value, natural_language=False)
            return
        if command == "positiondraw":
            if not value:
                await send_text_message(event, "用法：/nai0 positiondraw <英文标签>")
                return
            await self._handle_tag(event, session, value, use_coords=True)
            return
        if command == "draw" and not value:
            # 显式 draw 子命令必须带标签，避免把 draw 本身误当成提示词
            await send_text_message(event, "用法：/nai0 draw <英文标签>")
            return
        # 显式 draw 与旧式 /nai0 <tags> 都是直接标签文生图
        await self._handle_tag(event, session, value if command == "draw" and value else remainder)

    # ── 会话参数命令 ─────────────────────────────────────────

    @filter.command("naiconfig")
    async def naiconfig(self, event: AstrMessageEvent) -> None:
        """展示当前群聊或私聊实际会采用的生图配置"""
        event.should_call_llm(True)
        session = SessionContext.from_event(event)
        config = resolve_model_config(self.config, session, self.states)
        state = self.states.get(session)
        # 未生成过的随机种子没有“上一次”可展示，按约定明确显示 None
        displayed_seed = state.fixed_seed if state.seed_mode == "fixed" else state.last_seed
        fields = (
            "width", "height", "steps", "scale", "sampler", "noise_schedule",
            "cfg_rescale", "sm", "sm_dyn", "n_samples", "i2i_strength", "i2i_noise",
            "dynamic_thresholding", "controlnet_strength", "legacy", "add_original_image",
            "legacy_v3_extend", "skip_cfg_above_sigma", "use_order",
        )
        lines = ["当前会话生图配置：", "model: nai-diffusion-5-full"]
        lines.extend(f"{field}: {config.get(field)!r}" for field in fields)
        lines.append(f"seed: {displayed_seed if displayed_seed is not None else 'None'}")
        lines.append(f"seed_mode: {state.seed_mode or 'panel'}")
        await send_text_message(event, "\n".join(lines))

    @filter.command("naiset")
    async def naiset(self, event: AstrMessageEvent) -> None:
        """修改或清除本会话的单个生图参数覆盖"""
        event.should_call_llm(True)
        session = SessionContext.from_event(event)
        if not can_change_generation_config(self.config, session):
            await send_text_message(event, "❌ 你不在 /naiset 用户白名单中")
            return
        remainder = self._command_remainder(event.message_str)
        field, separator, raw_value = remainder.partition(" ")
        field = self._normalise_setting_name(field)
        if not field:
            await send_text_message(event, "用法：/naiset <参数> [值]")
            return
        if field == "seed":
            await self._set_seed(event, session, raw_value if separator else "")
            return
        if field not in self._SETTING_TYPES:
            await send_text_message(event, "❌ 不支持该参数，可用参数请查看 /naiconfig")
            return
        if not separator or not raw_value.strip():
            self.states.clear_generation_override(session, field)
            await send_text_message(event, f"✅ 已清除 {field} 的会话设置，后续使用面板默认值")
            return
        try:
            value = self._parse_setting(field, raw_value)
        except ValueError as exc:
            await send_text_message(event, f"❌ {exc}")
            return
        self.states.set_generation_override(session, field, value)
        await send_text_message(event, f"✅ 已设置 {field} = {value!r}")

    # ── 预设命令 ─────────────────────────────────────────────

    @filter.command("preset")
    async def preset(self, event: AstrMessageEvent) -> None:
        """查看或保存当前用户自己的角色、画师预设"""
        event.should_call_llm(True)
        await self._handle_preset_command(
            event, SessionContext.from_event(event), self._command_remainder(event.message_str), owner_id=None
        )

    @filter.command("publicpreset")
    async def publicpreset(self, event: AstrMessageEvent) -> None:
        """查看或保存所有用户都能引用的公共预设"""
        event.should_call_llm(True)
        session = SessionContext.from_event(event)
        if not can_change_generation_config(self.config, session):
            await send_text_message(event, "❌ 你不在 /publicpreset 用户白名单中")
            return
        await self._handle_preset_command(
            event, session, self._command_remainder(event.message_str), owner_id=PUBLIC_OWNER_ID
        )

    # ── 提示词历史命令 ───────────────────────────────────────

    @filter.command("naiprompt")
    async def naiprompt(self, event: AstrMessageEvent) -> None:
        """查看、永久保存、修改并复用当前会话中的提示词历史"""
        event.should_call_llm(True)
        session = SessionContext.from_event(event)
        remainder = self._command_remainder(event.message_str)
        first, separator, rest = remainder.partition(" ")
        first = first.casefold()
        rest = rest.strip()
        if not first or first in {"l", "last"}:
            ordinal = 1
            if rest:
                if not rest.isdigit() or int(rest) < 1:
                    await send_text_message(event, "用法：/naiprompt last [数字]")
                    return
                ordinal = int(rest)
            record = self.history.get_by_ordinal(session, ordinal)
            if record is None:
                await send_text_message(event, "❌ 没有找到对应的提示词历史")
                return
            await send_text_message(event, self.history.render(record))
            return
        if first == "save":
            if not rest.isdigit() or int(rest) < 1:
                await send_text_message(event, "用法：/naiprompt save <数字>")
                return
            record = self.history.save(session, int(rest))
            if record is None:
                await send_text_message(event, "❌ 没有找到对应的提示词历史")
                return
            await send_text_message(event, f"✅ 已将 prompt{record['request_timestamp']} 保存为 permanent")
            return
        # 省略序号直接写修改路径时，按约定复用最新一条历史
        if first.startswith("prompt.") or first.startswith("\\prompt."):
            record = self.history.get_by_ordinal(session, 1)
            rest = remainder.lstrip("\\").strip()
            separator = " "
        else:
            record = self.history.get(session, first)
        if record is None:
            await send_text_message(event, "❌ 没有找到对应的提示词历史")
            return
        if not can_use_generation(self.config, session, self.states):
            await send_text_message(event, "❌ 当前会话启用了管理员模式，只有管理员可以使用生图命令")
            return
        if separator and rest:
            try:
                record = apply_history_edits(
                    record,
                    rest,
                    lambda value: self.presets.expand_inline(session.user_id, value),
                )
            except ValueError as exc:
                await send_text_message(event, f"❌ 修改提示词失败：{exc}")
                return
        success, result = await self.image_service.regenerate_and_send(
            event, session, record
        )
        if not success:
            await send_text_message(
                event,
                f"⚠️ {result}"
                if result.startswith("图片已生成")
                else f"❌ 生成失败：{result}",
            )

    # ── 绘图内部处理 ─────────────────────────────────────────

    async def _handle_draw(
        self, event: AstrMessageEvent, session: SessionContext, request_text: str
    ) -> None:
        """把自然语言转为标签后再调用统一的文生图流程"""
        if not can_use_generation(self.config, session, self.states):
            await send_text_message(
                event, "❌ 当前会话启用了管理员模式，只有管理员可以使用生图命令"
            )
            return
        prompt_result = await self.prompt_service.generate_prompt(
            event, session, request_text
        )
        # _handle_draw内部调用 LLM 的体现，request_text是用户自己
        if not prompt_result:
            await send_text_message(
                event, "❌ 提示词生成失败，请确认当前会话有可用的聊天模型"
            )
            return
        if is_prompt_show_enabled(self.config, session, self.states):
            await send_text_message(
                event, f"📝 提示词：\n{prompt_result.display_prompt}"
            )
        success, result = await self.image_service.generate_and_send(
            event, session, prompt_result.prompt
        )
        if not success:
            await send_text_message(
                event,
                f"⚠️ {result}"
                if result.startswith("图片已生成")
                else f"❌ 生成失败：{result}",
            )
        elif get_config_value(self.config, "components.enable_debug_info", False):
            await send_text_message(event, "✅ 图片生成完成")

    async def _handle_tag(
        self,
        event: AstrMessageEvent,
        session: SessionContext,
        tags: str,
        *,
        use_coords: bool = False,
    ) -> None:
        """执行无需 LLM 的英文标签文生图，并可启用精确定位模式"""
        if not can_use_generation(self.config, session, self.states):
            await send_text_message(
                event, "❌ 当前会话启用了管理员模式，只有管理员可以使用生图命令"
            )
            return
        if is_prompt_show_enabled(self.config, session, self.states):
            await send_text_message(event, f"📝 提示词：\n{tags}")
        success, result = await self.image_service.generate_and_send(
            event, session, tags, use_coords=use_coords
        )
        if not success:
            await send_text_message(
                event,
                f"⚠️ {result}"
                if result.startswith("图片已生成")
                else f"❌ 生成失败：{result}",
            )

    async def _handle_i2i(
        self,
        event: AstrMessageEvent,
        session: SessionContext,
        request_text: str,
        *,
        natural_language: bool,
    ) -> None:
        """统一处理自然语言和英文标签两种图生图命令"""
        if not can_use_generation(self.config, session, self.states):
            await send_text_message(
                event, "❌ 当前会话启用了管理员模式，只有管理员可以使用生图命令"
            )
            return
        # 取图辅助函数先检查当前消息，再检查 Reply 和 OneBot 原消息，只会返回第一张图
        image_input = await extract_first_image_input(event)
        if not image_input:
            await send_text_message(event, "❌ 当前消息或引用消息中未包含图片")
            return
        prompt = request_text.strip()
        display_prompt = prompt
        if natural_language:
            prompt_result = await self.prompt_service.generate_prompt(
                event, session, prompt
            )
            if not prompt_result:
                await send_text_message(
                    event, "❌ 提示词生成失败，请确认当前会话有可用的聊天模型"
                )
                return
            prompt, display_prompt = prompt_result.prompt, prompt_result.display_prompt
        if is_prompt_show_enabled(self.config, session, self.states):
            await send_text_message(event, f"📝 提示词：\n{display_prompt}")
        success, result = await self.image_service.generate_and_send(
            event, session, prompt, image_input=image_input
        )
        if not success:
            await send_text_message(
                event,
                f"⚠️ {result}"
                if result.startswith("图片已生成")
                else f"❌ 图片生成失败：{result}",
            )

    # ── 设置、预设处理 ───────────────────────────────────────

    _SETTING_TYPES: dict[str, type] = {
        "width": int,
        "height": int,
        "steps": int,
        "scale": float,
        "sampler": str,
        "noise_schedule": str,
        "cfg_rescale": float,
        "sm": bool,
        "sm_dyn": bool,
        "n_samples": int,
        "i2i_strength": float,
        "i2i_noise": float,
        "dynamic_thresholding": bool,
        "controlnet_strength": float,
        "legacy": bool,
        "add_original_image": bool,
        "legacy_v3_extend": bool,
        "skip_cfg_above_sigma": float,
        "use_order": bool,
    }

    @classmethod
    def _normalise_setting_name(cls, raw_value: str) -> str:
        """兼容 cfg 等常用简写，其他字段保持 API 和面板同名"""
        name = str(raw_value or "").strip().casefold()
        return {
            "cfg": "cfg_rescale",
            "noise": "i2i_noise",
            "strength": "i2i_strength",
        }.get(name, name)

    @classmethod
    def _parse_setting(cls, field: str, raw_value: str) -> Any:
        """按字段类型解析 /naiset 的文本参数，拒绝模糊布尔值和非法数字"""
        value = str(raw_value).strip()
        target_type = cls._SETTING_TYPES[field]
        if target_type is str:
            return value
        if target_type is bool:
            normalized = value.casefold()
            if normalized in {"true", "1", "on"}:
                return True
            if normalized in {"false", "0", "off"}:
                return False
            raise ValueError(f"{field} 只能是 True 或 False")
        if field == "skip_cfg_above_sigma" and value.casefold() in {"none", "null"}:
            return None
        try:
            return target_type(value)
        except ValueError as exc:
            raise ValueError(f"{field} 的值格式不正确") from exc

    async def _set_seed(self, event: AstrMessageEvent, session: SessionContext, raw_value: str) -> None:
        """处理固定种子、随机种子和恢复面板种子的三种会话状态"""
        value = str(raw_value or "").strip()
        if not value:
            self.states.clear_seed_override(session)
            await send_text_message(event, "✅ 已清除 seed 的会话设置，后续使用面板默认值")
            return
        if value.casefold() == "random":
            self.states.set_random_seed(session)
            await send_text_message(event, "✅ 后续每次生图都会使用新的随机种子")
            return
        try:
            seed = int(value)
        except ValueError:
            await send_text_message(event, "❌ seed 必须是整数或 random")
            return
        self.states.set_fixed_seed(session, seed)
        await send_text_message(event, f"✅ 已固定 seed = {seed}")

    async def _handle_size_switch(
        self, event: AstrMessageEvent, session: SessionContext, argument: str
    ) -> None:
        """将旧的尺寸快捷命令映射为本会话 width 和 height 覆盖"""
        if argument:
            normalized = normalize_size_alias(argument)
            match = re.fullmatch(r"(\d+)x(\d+)", str(normalized or "").casefold())
            if match is None:
                await send_text_message(event, "❌ 尺寸应为 832x1216、竖、横或方")
                return
            self.states.set_generation_override(session, "width", int(match.group(1)))
            self.states.set_generation_override(session, "height", int(match.group(2)))
            await send_text_message(event, f"✅ 已切换尺寸为：{size_display_name(normalized)}")
            return
        model_config = resolve_model_config(self.config, session, self.states)
        current_size = f"{model_config.get('width', 832)}x{model_config.get('height', 1216)}"
        await send_text_message(event, "\n".join([
            f"当前尺寸：{size_display_name(current_size)}", "可用尺寸：",
            "竖 / v -> 832x1216", "横 / h -> 1216x832", "方 / s -> 1024x1024",
        ]))

    async def _handle_nsfw(
        self, event: AstrMessageEvent, session: SessionContext, argument: str
    ) -> None:
        """保留原有的 NSFW 过滤开关和插件管理员限制"""
        argument = argument.casefold()
        if argument not in {"", "on", "off"}:
            await send_text_message(event, "用法：/nai nsfw [on|off]")
            return
        if not argument:
            enabled = self.states.get(session).nsfw_filter_enabled
            if enabled is None:
                enabled = bool(get_config_value(self.config, "nsfw_filter.enabled", False))
            await send_text_message(event, f"当前 NSFW 过滤：{'开启' if enabled else '关闭'}")
            return
        if not is_plugin_admin(self.config, session):
            await send_text_message(event, "❌ 只有插件管理员可以使用 NSFW 过滤开关")
            return
        self.states.get(session).nsfw_filter_enabled = argument == "on"
        await send_text_message(event, "✅ 已开启 NSFW 过滤" if argument == "on" else "✅ 已关闭 NSFW 过滤")

    async def _handle_preset_command(
        self, event: AstrMessageEvent, session: SessionContext, remainder: str, *, owner_id: str | None
    ) -> None:
        """解析 character/c、artist/a 的列表与 <名称>=<内容> 保存语法"""
        kind_text, _, assignment = str(remainder or "").strip().partition(" ")
        kind = normalize_preset_kind(kind_text)
        if kind is None:
            await send_text_message(event, "用法：/preset character|artist [预设名=预设内容]")
            return
        target_owner = owner_id or session.user_id
        if not assignment.strip():
            entries = self.presets.list(target_owner, kind)
            scope = "公共" if target_owner == PUBLIC_OWNER_ID else "个人"
            if not entries:
                await send_text_message(event, f"当前没有{scope}{'角色' if kind == 'character' else '画师'}预设")
                return
            lines = [f"{scope}{'角色' if kind == 'character' else '画师'}预设："]
            lines.extend(f"{entry.title} = {entry.content}" for entry in entries)
            await send_text_message(event, "\n".join(lines))
            return
        title, equal, content = assignment.partition("=")
        if not equal:
            # 文档中的公共预设示例也允许 "名称 内容" 这一写法
            title, separator, content = assignment.strip().partition(" ")
            if not separator or not content.strip():
                await send_text_message(event, "用法：/preset character <预设名>=<预设内容>")
                return
        try:
            preset = self.presets.add(target_owner, kind, title, content)
        except ValueError as exc:
            await send_text_message(event, f"❌ 保存预设失败：{exc}")
            return
        await send_text_message(event, f"✅ 已保存{preset.type} 预设：{preset.title}")

    # ── 文本解析辅助 ─────────────────────────────────────────

    @staticmethod
    def _subcommand_argument(message: str) -> str:
        """提取命令组子命令后的自由文本参数"""
        parts = (message or "").strip().split(maxsplit=2)
        return parts[2].strip() if len(parts) > 2 else ""

    @staticmethod
    def _command_remainder(message: str) -> str:
        """提取顶级命令后的全部文本，用于 /nai0 等独立命令"""
        parts = (message or "").strip().split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""
