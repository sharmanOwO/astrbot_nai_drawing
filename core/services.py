"""定义服务类

LLMService 负责调用 AstrBot 当前聊天模型，PromptGeneratorService 负责把
自然语言翻译成 NovelAI 标签，ImageService 负责调用官方 API 并发送图片。
"""

from __future__ import annotations

import asyncio
import copy
import random
import re
import time
from typing import Any

from astrbot.api import logger

from .clients import NaiWebClient
from .config import (
    get_config_value,
    get_session_provider_id,
    is_recall_enabled,
    recall_is_allowed_in_session,
    resolve_model_config,
)
from .message_utils import (
    ImageDeliveryUnconfirmed,
    delete_onebot_message,
    send_image_message,
    sleep_and_delete,
)
from .models import PromptBuildResult, SessionContext
from .presets import PresetManager
from .prompt_history import PromptHistoryStore
from .session_state import SessionStateStore
from .templates import (
    PROMPT_GENERATOR_JSON_TEMPLATE,
    PROMPT_GENERATOR_TEMPLATE,
    SFW_PROMPT_GENERATOR_JSON_TEMPLATE,
    SFW_PROMPT_GENERATOR_TEMPLATE,
)
from .utils import (
    normalize_prompt_order,
    parse_prompt_from_structured_output,
)


class LLMService:
    """对 AstrBot LLM Provider 的最小异步封装。"""
    def __init__(self, context: Any, config: dict[str, Any]) -> None:
        self.context = context
        self.config = config

    async def _provider_id(
        self,
        event: Any,
        primary_path: str,
        *fallback_paths: str,
    ) -> str | None:
        """按配置路径查找 Provider；未配置时回退到当前会话 Provider。"""
        provider_id = get_session_provider_id(self.config, primary_path, *fallback_paths)
        if provider_id:
            return provider_id
        try:
            provider_id = await self.context.get_current_chat_provider_id(
                umo=event.unified_msg_origin
            )
        except Exception as exc:
            logger.error(f"[nai_drawing] 获取当前会话 provider 失败: {exc}")
            return None
        return provider_id or None

    async def generate(
        self,
        event: Any,
        *,
        prompt: str,
        provider_path: str,
        fallback_paths: tuple[str, ...] = (),
        temperature: float = 0.2,
        max_tokens: int = 300,
    ) -> str | None:
        """调用 AstrBot LLM，并返回纯文本完成结果。"""
        provider_id = await self._provider_id(event, provider_path, *fallback_paths)
        if not provider_id:
            return None

        try:
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            logger.error(f"[nai_drawing] LLM 调用失败: {exc}")
            return None

        text = str(getattr(response, "completion_text", "") or "").strip()
        return text or None


class PromptGeneratorService:
    """根据配置模板调用 LLM，并清理、规范化其提示词输出。"""
    def __init__(
        self,
        config: dict[str, Any],
        states: SessionStateStore,
        llm_service: LLMService,
    ) -> None:
        self.config = config
        self.states = states
        self.llm = llm_service

    async def generate_prompt(
        self,
        event: Any,
        session: SessionContext,
        request_text: str,
    ) -> PromptBuildResult | None:
        """将用户自然语言转换成 NovelAI 标签，并应用可选的标签排序。"""
        output_format = str(
            get_config_value(self.config, "prompt_generator.output_format", "text") or "text"
        ).strip().lower()
        # 判断输出格式：默认text，或者json
        nsfw_filter_enabled = bool(
            get_config_value(self.config, "nsfw_filter.enabled", False)
        )
        # 先判断是否 None，若不是则覆盖全局配置
        if self.states.get(session).nsfw_filter_enabled is not None:
            nsfw_filter_enabled = bool(self.states.get(session).nsfw_filter_enabled)

        if output_format == "json":
            template = (
                SFW_PROMPT_GENERATOR_JSON_TEMPLATE
                if nsfw_filter_enabled
                else PROMPT_GENERATOR_JSON_TEMPLATE
            )
        else:
            template = (
                SFW_PROMPT_GENERATOR_TEMPLATE
                if nsfw_filter_enabled
                else PROMPT_GENERATOR_TEMPLATE
            )

        custom_template = str(
            get_config_value(self.config, "prompt_generator.prompt_template", "") or ""
        ).strip()
        if custom_template:
            template = custom_template

        custom_system_prompt = str(
            get_config_value(self.config, "custom_prompt.system_prompt", "") or ""
        ).strip()
        prompt = template.replace(
            "<<CUSTOM_SYSTEM_PROMPT>>",
            f"{custom_system_prompt}\n\n" if custom_system_prompt else "",
        )
        prompt = prompt.replace("<<USER_REQUEST>>", request_text.strip() or "N/A")

        response = await self.llm.generate(
            event,
            prompt=prompt,
            provider_path="prompt_generator.provider_id",
            fallback_paths=("prompt_generator.chat_provider_id",),
            temperature=float(
                get_config_value(self.config, "prompt_generator.temperature", 0.2) or 0.2
            ),
            max_tokens=int(
                get_config_value(self.config, "prompt_generator.max_tokens", 220) or 220
            ),
        )
        if not response:
            return None

        cleaned = self._cleanup_response(response)
        if not cleaned:
            return None

        display_prompt = cleaned
        final_prompt = cleaned

        if get_config_value(self.config, "prompt_generator.enforce_tag_order", False):
            display_prompt = normalize_prompt_order(display_prompt)
            final_prompt = normalize_prompt_order(final_prompt)
        return PromptBuildResult(
            prompt=final_prompt,
            display_prompt=display_prompt,
        )

    def _cleanup_response(self, response: str) -> str:
        """移除形如```等前缀、解释性前缀和多余行，只保留提示词正文"""
        parsed = parse_prompt_from_structured_output(response)
        if parsed:
            return parsed

        cleaned = response.strip()
        if cleaned.startswith("```") and cleaned.endswith("```"):
            cleaned = cleaned[3:-3].strip()
            if "\n" in cleaned:
                first_line, rest = cleaned.split("\n", 1)
                if first_line.strip().isalpha() and len(first_line.strip()) < 15:
                    cleaned = rest.strip()

        if cleaned.startswith("`") and cleaned.endswith("`") and cleaned.count("`") == 2:
            cleaned = cleaned[1:-1].strip()
        if cleaned.startswith(("'", '"')) and cleaned.endswith(("'", '"')) and len(cleaned) >= 2:
            cleaned = cleaned[1:-1].strip()

        cleaned = re.sub(
            r"^(?:output|result|prompt|here(?:'s| is)(?: the)?(?: prompt)?)\s*[:：]\s*",
            "",
            cleaned,
            flags=re.I,
        ).strip()

        if "\n" in cleaned:
            lines = [line.strip() for line in cleaned.split("\n") if line.strip()]
            has_multi = any(line.startswith("|") for line in lines)
            valid_lines = [
                line
                for line in lines
                if not re.match(r"^(note|explanation|this|i |the above|here)", line, re.I)
            ]
            if valid_lines:
                cleaned = "\n".join(valid_lines) if has_multi else valid_lines[0]

        return cleaned


class ImageService:
    """协调结构化提示词、NovelAI 请求、消息发送和提示词历史"""

    def __init__(
        self,
        config: dict[str, Any],
        states: SessionStateStore,
        nai_client: NaiWebClient,
        presets: PresetManager,
        history: PromptHistoryStore,
    ) -> None:
        self.config = config
        self.states = states
        self.client = nai_client
        self.presets = presets
        self.history = history

    async def generate_and_send(
        self,
        event: Any,
        session: SessionContext,
        prompt: str,
        *,
        image_input: str | None = None,
        use_coords: bool = False,
    ) -> tuple[bool, str]:
        """从用户标签构造记录并完成一次文生图或图生图"""
        record = self.build_prompt_record(session, prompt, use_coords=use_coords)
        return await self._generate_record_and_send(
            event,
            session,
            record,
            image_input=image_input,
        )

    async def regenerate_and_send(
        self,
        event: Any,
        session: SessionContext,
        record: dict[str, Any],
    ) -> tuple[bool, str]:
        """用历史中的结构化提示词重绘，不再调用提示词 LLM"""
        return await self._generate_record_and_send(event, session, record)

    def build_prompt_record(
        self,
        session: SessionContext,
        user_prompt: str,
        *,
        use_coords: bool = False,
    ) -> dict[str, Any]:
        """拆分管道提示词，并保留全局、画师和用户三段的原始来源"""
        # character/c 在原位置替换为标签，artist/a 被提取出来并始终排在用户标签前
        expanded_prompt, artist_prompt = self.presets.expand_inline(session.user_id, user_prompt)
        parts = [part.strip().strip(",") for part in expanded_prompt.split("|")]
        base_caption = parts[0] if parts else ""
        char_captions = [
            {
                "char_caption": part,
                # 即使不使用精确定位，官方接口也要求角色段包含 centers
                "centers": [
                    {
                        "x": round(random.uniform(0.1, 0.9), 3),
                        "y": round(random.uniform(0.1, 0.9), 3),
                    }
                ],
            }
            for part in parts[1:]
            if part
        ]
        return {
            "request_timestamp": self.history.new_timestamp(),
            "type": "temporary",
            "prompt": {
                "global_prompt": str(
                    get_config_value(self.config, "model.custom_prompt_add", "") or ""
                ).strip(),
                "artist": artist_prompt,
                "base_caption": base_caption,
                "char_captions": char_captions,
            },
            "negative_prompt": {
                # 历史展示面板原值，NSFW 过滤标签只在实际请求时临时追加
                "global_negative_prompt": str(
                    get_config_value(self.config, "model.negative_prompt_add", "") or ""
                ).strip(),
                "base_caption": "",
                "char_captions": [],
            },
            "generation": {"use_coords": bool(use_coords)},
        }

    async def _generate_record_and_send(
        self,
        event: Any,
        session: SessionContext,
        record: dict[str, Any],
        *,
        image_input: str | None = None,
    ) -> tuple[bool, str]:
        """把历史记录转换为请求体，成功后发送图片并保存新的历史记录"""
        # 历史重绘也代表一条新的请求，不能复用旧记录的请求时间戳
        record = copy.deepcopy(record)
        record["request_timestamp"] = self.history.new_timestamp()
        model_config = resolve_model_config(self.config, session, self.states)
        actual_seed = self._select_seed(session, model_config)
        model_config["seed"] = actual_seed

        v4_prompt, v4_negative_prompt = self._build_api_prompts(record, model_config)
        model_config["_v4_prompt"] = v4_prompt
        model_config["_v4_negative_prompt"] = v4_negative_prompt

        started_at = time.perf_counter()
        if image_input:
            success, result = await self.client.generate_i2i_image(
                prompt=v4_prompt,
                model_config=model_config,
                image_input=image_input,
            )
        else:
            success, result = await self.client.generate_image(
                prompt=v4_prompt,
                model_config=model_config,
            )
        response_time = round(time.perf_counter() - started_at, 3)
        if not success:
            return False, result

        output = self._normalize_image_output(result)
        if not output:
            return False, "无法识别官方接口返回的图片格式"

        # 请求成功才更新“上一次种子”，失败请求不会污染 /naiconfig 的显示
        self.states.get(session).last_seed = actual_seed
        saved_record = self._history_record(record, response_time, v4_prompt)
        self.history.add(session, session.user_id, saved_record)

        try:
            message_id = await send_image_message(event, output)
        except ImageDeliveryUnconfirmed as exc:
            return False, str(exc)
        if message_id:
            display_prompt = str(v4_prompt["caption"]["base_caption"] or "")
            self.states.track_image(session, message_id, display_prompt)
            await self._schedule_auto_recall(event, session, message_id)
        return True, str(v4_prompt["caption"]["base_caption"] or "")

    def _select_seed(
        self, session: SessionContext, model_config: dict[str, Any]
    ) -> int:
        """按会话优先级解析固定种子、随机种子和面板默认种子"""
        state = self.states.get(session)
        if state.seed_mode == "fixed" and state.fixed_seed is not None:
            return state.fixed_seed
        if state.seed_mode == "random":
            return random.randrange(0, 2**32)

        configured_seed = model_config.get("seed")
        if configured_seed not in (None, ""):
            try:
                return int(configured_seed)
            except (TypeError, ValueError):
                # 非法面板值不能发给 API，按留空处理并随机生成
                pass
        return random.randrange(0, 2**32)

    @staticmethod
    def _join_tags(*parts: object) -> str:
        """按固定顺序合并非空逗号标签，避免形成重复的分隔符"""
        return ", ".join(
            str(part).strip().strip(",")
            for part in parts
            if str(part or "").strip().strip(",")
        )

    @classmethod
    def _normalise_char_captions(cls, value: Any) -> list[dict[str, Any]]:
        """清理历史中的角色段，并为旧记录补上必填 centers"""
        if not isinstance(value, list):
            return []
        result: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict):
                text = str(item.get("char_caption") or "").strip().strip(",")
                centers = item.get("centers")
            else:
                text = str(item or "").strip().strip(",")
                centers = None
            if not text:
                continue
            valid_centers: list[dict[str, float]] = []
            if isinstance(centers, list):
                for center in centers:
                    if not isinstance(center, dict):
                        continue
                    try:
                        x = float(center["x"])
                        y = float(center["y"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    valid_centers.append(
                        {"x": max(0.0, min(1.0, x)), "y": max(0.0, min(1.0, y))}
                    )
            if not valid_centers:
                valid_centers = [
                    {
                        "x": round(random.uniform(0.1, 0.9), 3),
                        "y": round(random.uniform(0.1, 0.9), 3),
                    }
                ]
            result.append({"char_caption": text, "centers": valid_centers})
        return result

    def _build_api_prompts(
        self,
        record: dict[str, Any],
        model_config: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """将展示用历史段落组合为 V5 仍要求的 v4_prompt 两个对象"""
        prompt = record.get("prompt") if isinstance(record.get("prompt"), dict) else {}
        negative = (
            record.get("negative_prompt")
            if isinstance(record.get("negative_prompt"), dict)
            else {}
        )
        generation = record.get("generation") if isinstance(record.get("generation"), dict) else {}

        # 请求中正向提示词顺序固定为全局 -> 画师 -> 用户基础提示词
        positive_base = self._join_tags(
            prompt.get("global_prompt"),
            prompt.get("artist"),
            prompt.get("base_caption"),
        )
        # 全局负面提示词来自历史记录，重绘时不会被后来修改的面板覆盖
        # 当前会话开启的 NSFW 过滤仍在发送请求前临时追加
        actual_negative = self._join_tags(
            model_config.get("_nsfw_filter_tags"),
            negative.get("global_negative_prompt"),
            negative.get("base_caption"),
        )
        return (
            {
                "caption": {
                    "base_caption": positive_base,
                    "char_captions": self._normalise_char_captions(
                        prompt.get("char_captions")
                    ),
                },
                "use_coords": bool(generation.get("use_coords", False)),
                "use_order": bool(
                    generation.get("use_order", model_config.get("use_order", False))
                ),
            },
            {
                "caption": {
                    "base_caption": actual_negative,
                    "char_captions": self._normalise_char_captions(
                        negative.get("char_captions")
                    ),
                }
            },
        )

    @staticmethod
    def _history_record(
        record: dict[str, Any],
        response_time: float,
        v4_prompt: dict[str, Any],
    ) -> dict[str, Any]:
        """复制本次请求的展示数据，并把实际生图耗时和开关写入历史"""
        prompt = record.get("prompt") if isinstance(record.get("prompt"), dict) else {}
        negative = (
            record.get("negative_prompt")
            if isinstance(record.get("negative_prompt"), dict)
            else {}
        )
        generation = record.get("generation") if isinstance(record.get("generation"), dict) else {}
        return {
            "request_timestamp": str(record.get("request_timestamp") or ""),
            "response_time": response_time,
            "type": "temporary",
            "prompt": prompt,
            "negative_prompt": negative,
            "generation": {
                "use_coords": bool(v4_prompt.get("use_coords", False)),
                "use_order": bool(v4_prompt.get("use_order", False)),
                **{
                    key: value
                    for key, value in generation.items()
                    if key not in {"use_coords", "use_order"}
                },
            },
        }

    def _normalize_image_output(self, result: str) -> str | None:
        """把官方 API 返回的 Base64 统一转换成 AstrBot 可发送的图片"""
        cleaned = (result or "").strip()
        if not cleaned:
            return None
        if cleaned.startswith(("http://", "https://", "file:///", "base64://")):
            return cleaned
        if cleaned.startswith("data:image"):
            prefix, _ = cleaned.split(",", 1)
            return f"base64://{cleaned[len(prefix) + 1:]}"
        return f"base64://{cleaned}"

    async def _schedule_auto_recall(
        self,
        event: Any,
        session: SessionContext,
        message_id: str,
    ) -> None:
        """按照配置创建延迟撤回任务。"""
        if not is_recall_enabled(self.config, session, self.states):
            return
        if not recall_is_allowed_in_session(self.config, session):
            return

        delay_seconds = float(
            get_config_value(self.config, "auto_recall.delay_seconds", 100) or 100
        )
        asyncio.create_task(sleep_and_delete(event, message_id, delay_seconds))

    async def delete_message(self, event: Any, message_id: str) -> bool:
        """删除指定消息；当前仅 aiocqhttp OneBot 平台支持。"""
        return await delete_onebot_message(event, message_id)
