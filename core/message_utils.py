"""用于提取消息和发送消息的辅助函数

图片输入统一保留为 URL、``file://`` 或 ``base64://`` 字符串，具体如何转换
成 SDK 的 bytes/path 由客户端层负责。本模块同时兼容 Reply 消息链和 aiocqhttp
通过 ``get_msg`` 查询引用消息的两种情况
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image, Plain, Reply

from .utils import strip_data_url


def _resolve_call_action(event: Any):
    """从事件对象中取得 OneBot ``call_action``，平台不支持时返回 ``None``。"""
    bot = getattr(event, "bot", None)
    api = getattr(bot, "api", None)
    call_action = getattr(api, "call_action", None)
    if callable(call_action):
        return call_action
    return None


def _unwrap_action_response(result: Any) -> dict[str, Any]:
    """兼容 OneBot 将实际数据包裹在 ``data`` 字段中的返回格式。"""
    if not isinstance(result, dict):
        return {}
    data = result.get("data")
    if isinstance(data, dict):
        return data
    return result


async def send_text_message(event: Any, text: str) -> None:
    """通过 AstrBot MessageChain 向当前会话发送纯文本。"""
    await event.send(MessageChain([Plain(text)]))


def extract_reply_component(event: Any) -> Reply | None:
    """这个函数判断当前消息是否是引用的消息，"""
    # 以QQ为例，使用图生图指令时如果引用消息中有图片，
    # Astrbot 中的 event.get_messages() 中就会有形如
    # [
    #     Reply(
    #         id="456",
    #         chain=[Image(...)],
    #         message_str=""
    #     ),
    #     Plain("/nai i2i cyberpunk style")
    # ]
    # 的消息链
    # id 是QQ平台在用户发送这条消息时赋予的
    for component in event.get_messages():
        if isinstance(component, Reply):
            return component
    return None


def extract_reply_message_id(event: Any) -> str | None:
    """这个函数会优先从 Reply 中读取引用消息的 ID，如果没有再递归检查原始消息"""
    # 这是为了兼容不同消息平台的消息结构

    # 有了消息 ID ，例如 Reply(id="456")
    # 就可以用QQ OneBot 的 API 查询原消息：
    # await call_action("get_msg", message_id="456")
    # 得到原消息内容：
    # "message": [
    #     {
    #         "type": "image",
    #         "data": {"url": "https://..."}
    #     }
    # ]
    # 然后插件就能从中取出图片

    reply = extract_reply_component(event)
    if reply and getattr(reply, "id", None):
        return str(reply.id)

    raw_message = getattr(event.message_obj, "raw_message", None)
    return _deep_find_reply_id(raw_message)


def _clean_message_id(value: Any) -> str | None:
    """将可能为数字或空值的消息 ID 规范化为非空字符串"""
    if value in (None, ""):
        return None
    return str(value).strip() or None


def _deep_find_reply_id(payload: Any) -> str | None:
    """通过遍历一系列消息名称来兼容不同平台的引用消息的命名"""
    if isinstance(payload, dict):
        if str(payload.get("type") or "").strip().lower() == "reply":
            data = payload.get("data")
            if isinstance(data, dict):
                for key in ("id", "message_id", "reply_to", "reply_id"):
                    message_id = _clean_message_id(data.get(key))
                    if message_id:
                        return message_id
            else:
                message_id = _clean_message_id(data)
                if message_id:
                    return message_id

        for key in (
            "reply_to",
            "reply_to_message_id",
            "reply_message_id",
            "quote_message_id",
            "reply_id",
        ):
            message_id = _clean_message_id(payload.get(key))
            if message_id:
                return message_id
        for value in payload.values():
            hit = _deep_find_reply_id(value)
            if hit:
                return hit
        return None
    if isinstance(payload, list):
        for item in payload:
            hit = _deep_find_reply_id(item)
            if hit:
                return hit
    return None


def _normalize_image_input(value: str) -> str | None:
    """规范化 URL、文件 URI、base64 URI 等格式"""
    stripped = (value or "").strip()
    if not stripped:
        return None
    if stripped.startswith(("http://", "https://", "file:///", "base64://")):
        return stripped
    if stripped.startswith("data:image"):
        b64, _ = strip_data_url(stripped)
        return f"base64://{b64}" if b64 else None
    return stripped


async def extract_first_reply_image_input(event: Any) -> str | None:
    """从引用消息中提取第一张图片，供图生图等引用图片场景使用"""
    reply = extract_reply_component(event)
    if reply and getattr(reply, "chain", None):
        for component in reply.chain or []:
            image = await _extract_image_from_component(component)
            if image:
                return image

    reply_id = extract_reply_message_id(event)
    if not reply_id:
        return None
    return await extract_image_input_from_onebot(event, reply_id)


async def _extract_image_from_component(component: Any) -> str | None:
    """获取 AstrBot Image 对象，并统一转成 URL/base64/path 字符串"""
    if not isinstance(component, Image):
        return None
    image_value = getattr(component, "url", None) or getattr(component, "file", None)
    normalized = _normalize_image_input(str(image_value or ""))
    if normalized:
        return normalized
    try:
        base64_value = await component.convert_to_base64()
    except Exception:
        return None
    return f"base64://{base64_value}" if base64_value else None


async def extract_first_image_input(event: Any) -> str | None:
    """提取当前消息或引用消息中的第一张图片。

    按 AstrBot 消息链顺序优先使用当前消息中的图片；当前消息没有图片时，
    再检查引用的消息。这样既支持“文字+图片”也支持只
    引用图片后发送命令的场景
    """
    for component in event.get_messages() or []:
        image = await _extract_image_from_component(component)
        if image:
            return image
    return await extract_first_reply_image_input(event)


async def extract_image_input_from_onebot(event: Any, message_id: str) -> str | None:
    """调用 aiocqhttp ``get_msg``，从引用消息中找出第一张图片。"""
    call_action = _resolve_call_action(event)
    if event.get_platform_name() != "aiocqhttp" or call_action is None:
        return None

    params_list = [{"message_id": message_id}, {"id": message_id}]
    if str(message_id).isdigit():
        params_list.extend(
            [{"message_id": int(message_id)}, {"id": int(message_id)}]
        )

    for params in params_list:
        try:
            result = await call_action("get_msg", **params)
        except Exception:
            continue
        payload = _unwrap_action_response(result)
        messages = payload.get("message")
        if not isinstance(messages, list):
            continue
        for item in messages:
            if not isinstance(item, dict) or item.get("type") != "image":
                continue
            data = item.get("data") or {}
            if not isinstance(data, dict):
                continue
            for key in ("file", "url"):
                normalized = _normalize_image_input(str(data.get(key) or ""))
                if normalized:
                    return normalized
    return None


class ImageDeliveryUnconfirmed(RuntimeError):
    """The image was generated, but its QQ delivery could not be confirmed."""


async def send_image_message(
    event: Any,
    image_input: str,
) -> str | None:
    """Send an image without resending an uncertain OneBot delivery.

    Args:
        event: The incoming event and its platform client.
        image_input: A normalized image URL, file URI, or base64 payload.

    Returns:
        The confirmed OneBot message ID, or None for other-platform sends.

    Raises:
        ImageDeliveryUnconfirmed: QQ did not provide a confirmed send result.
    """
    normalized = _normalize_image_input(image_input)
    if not normalized:
        return None

    call_action = _resolve_call_action(event)
    if event.get_platform_name() == "aiocqhttp" and call_action is not None:
        # 发送消息动作，携带上 payload 中的图片
        action = "send_group_msg" if event.get_group_id() else "send_private_msg"
        payload: dict[str, Any] = {
            "message": [{"type": "image", "data": {"file": normalized}}],
        }
        if event.get_group_id():
            payload["group_id"] = int(event.get_group_id())
        else:
            payload["user_id"] = int(event.get_sender_id())

        started_at = time.time()
        try:
            result = await call_action(action, **payload)
        except Exception as exc:
            detail = str(exc)
            uncertain = (
                isinstance(exc, (TimeoutError, asyncio.TimeoutError))
                or (
                    "Timeout: NTEvent" in detail
                    and "NodeIKernelMsgService/sendMsg" in detail
                )
                or "WebSocket API call timeout" in detail
            )
            if uncertain:
                # A timeout does not cancel QQ's send. Never resend it blindly.
                history_action = (
                    "get_group_msg_history"
                    if event.get_group_id()
                    else "get_friend_msg_history"
                )
                history_params = {
                    "count": 20,
                    "disable_get_url": True,
                    "parse_mult_msg": False,
                    "quick_reply": True,
                }
                target_key = "group_id" if event.get_group_id() else "user_id"
                history_params[target_key] = payload[target_key]
                recent_images = 0
                try:
                    history = await asyncio.wait_for(
                        call_action(history_action, **history_params), timeout=8
                    )
                    self_id = str(getattr(event.message_obj, "self_id", ""))
                    for message in _unwrap_action_response(history).get("messages", []):
                        if str(message.get("user_id", "")) != self_id:
                            continue
                        if float(message.get("time", 0)) < started_at - 1:
                            continue
                        parts = message.get("message", [])
                        if isinstance(parts, list) and any(
                            part.get("type") == "image"
                            for part in parts
                            if isinstance(part, dict)
                        ):
                            recent_images += 1
                except Exception as history_exc:
                    logger.warning(
                        "[nai_drawing] Delivery history check failed (%s); no resend.",
                        type(history_exc).__name__,
                    )
                # Nearby images cannot identify this request reliably, especially
                # with concurrent requests. Do not invent a confirmed message ID.
                logger.warning(
                    "[nai_drawing] Image delivery unconfirmed; recent self images=%s; "
                    "automatic resend suppressed.",
                    recent_images,
                )
                raise ImageDeliveryUnconfirmed(
                    "图片已生成，但 QQ 的发送结果尚未确认。为避免重复出图，"
                    "本次不会自动重发；请先查看聊天记录，再决定是否重试。"
                ) from None
            logger.warning(
                "[nai_drawing] OneBot image send failed (%s); trying event.send.",
                type(exc).__name__,
            )
        else:
            response = _unwrap_action_response(result)
            message_id = response.get("message_id")
            if message_id not in (None, ""):
                return str(message_id)
            # A successful call without an ID must not send the image a second time.
            raise ImageDeliveryUnconfirmed(
                "图片已生成，QQ 未返回可确认的消息编号。为避免重复出图，"
                "本次不会自动重发；请先查看聊天记录。"
            )

    if normalized.startswith(("http://", "https://")):
        await event.send(MessageChain([Image.fromURL(normalized)]))
    elif normalized.startswith("base64://"):
        await event.send(MessageChain([Image.fromBase64(normalized[9:])]))
    elif normalized.startswith("file:///"):
        await event.send(MessageChain([Image(file=normalized)]))
    else:
        await event.send(MessageChain([Image(file=normalized)]))
    return None


async def delete_onebot_message(event: Any, message_id: str) -> bool:
    """调用 OneBot ``delete_msg`` 撤回指定消息"""
    call_action = _resolve_call_action(event)
    if event.get_platform_name() != "aiocqhttp" or call_action is None:
        return False

    params_list = [{"message_id": message_id}, {"id": message_id}]
    if str(message_id).isdigit():
        params_list.extend([{"message_id": int(message_id)}, {"id": int(message_id)}])

    for params in params_list:
        try:
            await call_action("delete_msg", **params)
        except Exception:
            continue
        return True
    return False


async def sleep_and_delete(event: Any, message_id: str, delay_seconds: float) -> bool:
    """等待指定秒数后撤回消息，用于自动撤回后台任务。"""
    await asyncio.sleep(delay_seconds)
    return await delete_onebot_message(event, message_id)
