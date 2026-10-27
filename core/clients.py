"""NovelAI 官方图片 API 客户端。"""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Mapping, Sequence
from io import BytesIO
from pathlib import Path
import random
import re
from typing import Any
from urllib.parse import unquote, urlparse
import zipfile

import httpx

from astrbot.api import logger

from .constants import DEFAULT_TIMEOUT_SECONDS


GENERATE_IMAGE_URL = "https://image.novelai.net/ai/generate-image"
V5_FULL_MODEL = "nai-diffusion-5-full"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)


class NaiWebClient:
    """通过 NovelAI 官方 HTTP API 生成文生图和图生图。"""

    def __init__(self) -> None:
        headers = {
            "Accept": "application/zip",
            "Content-Type": "application/json",
            "Origin": "https://novelai.net",
            "Referer": "https://novelai.net",
            "User-Agent": USER_AGENT,
        }
        # 生图和下载引用图共用连接池，避免每张图都重新建立 TLS 连接
        self._http_client = httpx.AsyncClient(
            headers=headers,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            trust_env=True,
            follow_redirects=True,
        )

    async def close(self) -> None:
        """关闭 HTTP 连接池。"""
        await self._http_client.aclose()

    @staticmethod
    def _as_bool(value: Any, default: bool = False) -> bool:
        """解析配置面板可能传入的布尔字符串。"""
        if value is None or value == "":
            return default
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "on"}:
                return True
            if normalized in {"false", "0", "no", "off"}:
                return False
        return bool(value)

    @staticmethod
    def _as_float(value: Any, default: float) -> float:
        """把配置值转换为浮点数，非法值回退默认值。"""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_int(value: Any, default: int) -> int:
        """把配置值转换为整数，非法值回退默认值。"""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @classmethod
    def _dimensions(
        cls,
        model_config: Mapping[str, Any],
        size: str | None,
    ) -> tuple[int, int]:
        """读取当前请求的宽高，缺失时使用默认竖图尺寸。"""
        width_value = model_config.get("width")
        height_value = model_config.get("height")
        if width_value not in (None, "") and height_value not in (None, ""):
            return cls._as_int(width_value, 832), cls._as_int(height_value, 1216)

        raw_size = str(size or "").strip().lower()
        match = re.fullmatch(r"(\d+)x(\d+)", raw_size)
        if match:
            return int(match.group(1)), int(match.group(2))
        return 832, 1216

    @staticmethod
    def _random_center() -> list[dict[str, float]]:
        """为没有指定位置的角色补齐 API 必填的中心点。"""
        return [
            {
                "x": round(random.uniform(0.1, 0.9), 3),
                "y": round(random.uniform(0.1, 0.9), 3),
            }
        ]

    @classmethod
    def _normalize_centers(cls, raw_centers: Any) -> list[dict[str, float]]:
        """清洗历史记录或调用方传入的位置，缺失时生成一个随机位置。"""
        if not isinstance(raw_centers, list):
            return cls._random_center()

        centers: list[dict[str, float]] = []
        for raw_center in raw_centers:
            if not isinstance(raw_center, Mapping):
                continue
            try:
                x = float(raw_center["x"])
                y = float(raw_center["y"])
            except (KeyError, TypeError, ValueError):
                continue
            # API 坐标采用 0 到 1 的归一化小数，边界值也允许精确传入
            centers.append({"x": max(0.0, min(1.0, x)), "y": max(0.0, min(1.0, y))})
        return centers or cls._random_center()

    @classmethod
    def _normalize_char_captions(cls, raw_captions: Any) -> list[dict[str, Any]]:
        """保证每个角色提示词都有 char_caption 和 centers 两个字段。"""
        if not isinstance(raw_captions, Sequence) or isinstance(raw_captions, (str, bytes)):
            return []

        captions: list[dict[str, Any]] = []
        for raw_caption in raw_captions:
            if isinstance(raw_caption, Mapping):
                text = str(raw_caption.get("char_caption") or "").strip()
                centers = cls._normalize_centers(raw_caption.get("centers"))
            else:
                text = str(raw_caption or "").strip()
                centers = cls._random_center()
            if text:
                captions.append({"char_caption": text, "centers": centers})
        return captions

    @classmethod
    def _build_prompt_from_text(cls, prompt: str, model_config: Mapping[str, Any]) -> dict[str, Any]:
        """把直接调用客户端时传入的文本转换成 V4 提示词结构。"""
        parts = [part.strip() for part in str(prompt or "").split("|")]
        return {
            "caption": {
                "base_caption": parts[0] if parts else "",
                "char_captions": cls._normalize_char_captions(parts[1:]),
            },
            "use_coords": cls._as_bool(model_config.get("use_coords"), False),
            "use_order": cls._as_bool(model_config.get("use_order"), False),
        }

    @classmethod
    def _normalize_v4_prompt(
        cls,
        raw_prompt: Mapping[str, Any],
        model_config: Mapping[str, Any],
    ) -> dict[str, Any]:
        """接收服务层传来的结构化提示词并补齐 API 所需字段。"""
        caption = raw_prompt.get("caption")
        caption = caption if isinstance(caption, Mapping) else raw_prompt
        return {
            "caption": {
                "base_caption": str(caption.get("base_caption") or "").strip(),
                "char_captions": cls._normalize_char_captions(caption.get("char_captions")),
            },
            "use_coords": cls._as_bool(
                raw_prompt.get("use_coords", model_config.get("use_coords")),
                False,
            ),
            "use_order": cls._as_bool(
                raw_prompt.get("use_order", model_config.get("use_order")),
                False,
            ),
        }

    @classmethod
    def _build_v4_prompt(
        cls,
        prompt: str | Mapping[str, Any],
        model_config: Mapping[str, Any],
    ) -> dict[str, Any]:
        """优先使用服务层已构建的结构化正面提示词。"""
        if isinstance(prompt, Mapping):
            return cls._normalize_v4_prompt(prompt, model_config)
        for key in ("_v4_prompt", "v4_prompt"):
            raw_prompt = model_config.get(key)
            if isinstance(raw_prompt, Mapping):
                return cls._normalize_v4_prompt(raw_prompt, model_config)
        return cls._build_prompt_from_text(str(prompt or ""), model_config)

    @classmethod
    def _build_v4_negative_prompt(cls, model_config: Mapping[str, Any]) -> dict[str, Any]:
        """生成 V5 仍要求的 V4 结构化负面提示词。"""
        for key in ("_v4_negative_prompt", "v4_negative_prompt"):
            raw_prompt = model_config.get(key)
            if isinstance(raw_prompt, Mapping):
                caption = raw_prompt.get("caption")
                caption = caption if isinstance(caption, Mapping) else raw_prompt
                return {
                    "caption": {
                        "base_caption": str(caption.get("base_caption") or "").strip(),
                        "char_captions": cls._normalize_char_captions(caption.get("char_captions")),
                    }
                }

        return {
            "caption": {
                "base_caption": str(model_config.get("negative_prompt_add") or "").strip(),
                "char_captions": [],
            }
        }

    @classmethod
    def _build_request_body(
        cls,
        *,
        prompt: str | Mapping[str, Any],
        model_config: Mapping[str, Any],
        size: str | None = None,
        image_base64: str | None = None,
    ) -> dict[str, Any]:
        """构建官方 API 的完整请求体，不保留已废弃字段。"""
        width, height = cls._dimensions(model_config, size)
        seed_value = model_config.get("seed")
        seed = cls._as_int(seed_value, random.randrange(0, 2**32))
        skip_value = model_config.get("skip_cfg_above_sigma")
        if skip_value in (None, "", "None", "none", "null"):
            skip_cfg_above_sigma: float | None = None
        else:
            skip_cfg_above_sigma = cls._as_float(skip_value, 0.0)
        parameters: dict[str, Any] = {
            "params_version": 4,
            "width": width,
            "height": height,
            "steps": cls._as_int(model_config.get("steps"), 23),
            "scale": cls._as_float(model_config.get("scale"), 5.0),
            "seed": seed,
            "sampler": str(model_config.get("sampler") or "k_euler_ancestral"),
            "noise_schedule": str(model_config.get("noise_schedule") or "karras"),
            "cfg_rescale": cls._as_float(model_config.get("cfg_rescale"), 0.0),
            "sm": cls._as_bool(model_config.get("sm"), False),
            "sm_dyn": cls._as_bool(model_config.get("sm_dyn"), False),
            "n_samples": max(1, cls._as_int(model_config.get("n_samples"), 1)),
            "dynamic_thresholding": cls._as_bool(
                model_config.get("dynamic_thresholding"), False
            ),
            "controlnet_strength": cls._as_float(
                model_config.get("controlnet_strength"), 1.0
            ),
            "legacy": cls._as_bool(model_config.get("legacy"), False),
            "add_original_image": cls._as_bool(
                model_config.get("add_original_image"), True
            ),
            "legacy_v3_extend": cls._as_bool(model_config.get("legacy_v3_extend"), False),
            "skip_cfg_above_sigma": skip_cfg_above_sigma,
            "v4_prompt": cls._build_v4_prompt(prompt, model_config),
            "v4_negative_prompt": cls._build_v4_negative_prompt(model_config),
        }
        action = "generate"
        if image_base64:
            action = "img2img"
            parameters.update(
                {
                    "image": image_base64,
                    "strength": cls._as_float(model_config.get("i2i_strength"), 0.6),
                    "noise": cls._as_float(model_config.get("i2i_noise"), 0.0),
                }
            )
        return {"model": V5_FULL_MODEL, "action": action, "parameters": parameters}

    async def _image_to_base64(self, image_input: str) -> str:
        """读取 AstrBot 图片引用并转换为官方 API 需要的裸 Base64。"""
        value = str(image_input or "").strip()
        if not value:
            raise ValueError("未提供图生图参考图片")

        if value.startswith("base64://"):
            return self._normalize_base64(value[len("base64://") :])
        if value.startswith("data:image/"):
            try:
                _, encoded = value.split(",", 1)
            except ValueError as exc:
                raise ValueError("图片 data URL 不完整") from exc
            return self._normalize_base64(encoded)
        if value.startswith(("http://", "https://")):
            response = await self._http_client.get(value)
            response.raise_for_status()
            return base64.b64encode(response.content).decode("ascii")

        path = self._local_image_path(value)
        if path is not None:
            try:
                payload = await asyncio.to_thread(path.read_bytes)
            except OSError as exc:
                raise ValueError(f"无法读取图片文件: {path}") from exc
            return base64.b64encode(payload).decode("ascii")

        # 部分适配器直接给出无前缀 Base64，验证后重新编码可去除换行和残留前缀
        return self._normalize_base64(value)

    @staticmethod
    def _local_image_path(value: str) -> Path | None:
        """把 file URI 或普通文件路径转换为本地路径。"""
        if value.startswith("file://"):
            parsed = urlparse(value)
            path_text = unquote(parsed.path)
            if re.match(r"^/[A-Za-z]:[\\/]", path_text):
                path_text = path_text[1:]
            if parsed.netloc and parsed.netloc.lower() != "localhost":
                path_text = f"//{parsed.netloc}{path_text}"
            return Path(path_text)
        path = Path(value)
        return path if path.is_file() else None

    @staticmethod
    def _normalize_base64(value: str) -> str:
        """验证并规范化 Base64，避免把损坏图片内容发给官方 API。"""
        compact = "".join(str(value or "").split())
        try:
            payload = base64.b64decode(compact, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("图片不是有效的 Base64 数据") from exc
        if not payload:
            raise ValueError("图片数据为空")
        return base64.b64encode(payload).decode("ascii")

    @staticmethod
    def _first_image_from_zip(payload: bytes) -> bytes:
        """提取官方 ZIP 响应中的第一张非目录文件。"""
        try:
            with zipfile.ZipFile(BytesIO(payload)) as archive:
                member = next((item for item in archive.infolist() if not item.is_dir()), None)
                if member is None:
                    raise ValueError("NovelAI 返回的 ZIP 中没有图片")
                return archive.read(member)
        except zipfile.BadZipFile as exc:
            raise ValueError("NovelAI 返回的内容不是有效的 ZIP 图片包") from exc

    async def _generate(
        self,
        *,
        prompt: str | Mapping[str, Any],
        model_config: Mapping[str, Any],
        size: str | None,
        image_input: str | None,
    ) -> tuple[bool, str]:
        """发送一次官方 API 请求，并把首张图片编码为 AstrBot 可发送的 Base64。"""
        token = str(model_config.get("api_key") or "").strip()
        if token.lower().startswith("bearer "):
            token = token.split(" ", 1)[1].strip()
        if not token:
            return False, "未配置 NovelAI Persistent API Token"

        try:
            image_base64 = await self._image_to_base64(image_input) if image_input else None
            request_body = self._build_request_body(
                prompt=prompt,
                model_config=model_config,
                size=size,
                image_base64=image_base64,
            )
            response = await self._http_client.post(
                GENERATE_IMAGE_URL,
                json=request_body,
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code not in {200, 201}:
                detail = response.text.strip()[:500]
                logger.error(
                    f"[nai_drawing] NovelAI 官方 API 请求失败: "
                    f"status={response.status_code}, body={detail}"
                )
                return False, f"NovelAI 生成失败: HTTP {response.status_code}: {detail or '未知错误'}"
            image = self._first_image_from_zip(response.content)
            return True, base64.b64encode(image).decode("ascii")
        except (httpx.HTTPError, OSError, ValueError) as exc:
            logger.error(f"[nai_drawing] NovelAI 图片生成失败: {exc}")
            return False, f"NovelAI 生成失败: {exc}"

    async def generate_image(
        self,
        *,
        prompt: str | Mapping[str, Any],
        model_config: dict[str, Any],
        size: str | None = None,
        image_input: str | None = None,
    ) -> tuple[bool, str]:
        """生成文生图，兼容旧调用方传入 image_input 的方式。"""
        return await self._generate(
            prompt=prompt,
            model_config=model_config,
            size=size,
            image_input=image_input,
        )

    async def generate_i2i_image(
        self,
        *,
        prompt: str | Mapping[str, Any],
        model_config: dict[str, Any],
        image_input: str,
        size: str | None = None,
    ) -> tuple[bool, str]:
        """生成图生图，参考图片只取调用方已经选择的第一张。"""
        if not str(image_input or "").strip():
            return False, "消息或引用消息中未包含图片"
        return await self._generate(
            prompt=prompt,
            model_config=model_config,
            size=size,
            image_input=image_input,
        )
