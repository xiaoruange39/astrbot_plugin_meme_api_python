import asyncio
import base64
import ipaddress
import mimetypes
import os
import re
import socket
import urllib.parse
from urllib.parse import urlparse
from urllib.request import url2pathname

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

MAX_IMAGE_DOWNLOAD_CONCURRENCY = 4


def is_forbidden_ip(address: str) -> bool:
    """Checks if the given IP address is a private, loopback, or reserved address.

    Args:
        address: The IP address string.

    Returns:
        True if the IP address is forbidden, False otherwise.
    """
    try:
        ip = ipaddress.ip_address(address)
        return any(
            (
                ip.is_loopback,
                ip.is_private,
                ip.is_link_local,
                ip.is_multicast,
                ip.is_reserved,
                ip.is_unspecified,
            )
        )
    except ValueError:
        return False


async def validate_external_image_url(url: str) -> tuple[str, set[str]]:
    """Validates if the image URL points to a public, external HTTP/HTTPS host.

    Args:
        url: The external URL string.

    Returns:
        A tuple of (lowered_hostname, resolved_ip_set).

    Raises:
        RuntimeError: If validation fails or host resolves to a forbidden IP.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise RuntimeError("图片 URL 只支持 http/https")
    hostname = parsed.hostname
    if not hostname:
        raise RuntimeError("图片 URL 缺少主机名")
    lowered = hostname.rstrip(".").lower()
    if lowered == "localhost" or lowered.endswith(".localhost"):
        raise RuntimeError("不允许访问本机地址")

    if is_forbidden_ip(lowered):
        raise RuntimeError("不允许访问内网或本机地址")
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, hostname, parsed.port, type=socket.SOCK_STREAM
        )
    except socket.gaierror as e:
        raise RuntimeError(f"图片 URL 域名解析失败：{e}") from e
    resolved_ips = set()
    for info in infos:
        address = info[4][0]
        resolved_ips.add(address)
        if is_forbidden_ip(address):
            raise RuntimeError("不允许访问解析到内网或本机的地址")
    return lowered, resolved_ips


def response_peer_ip(resp: aiohttp.ClientResponse) -> str:
    """Retrieves the peer IP address from a response transport.

    Args:
        resp: The ClientResponse object.

    Returns:
        The peer IP address string, or empty string.
    """
    connection = getattr(resp, "connection", None)
    transport = getattr(connection, "transport", None)
    if transport is None:
        return ""
    peername = transport.get_extra_info("peername")
    if isinstance(peername, tuple) and peername:
        return str(peername[0] or "")
    return ""


def validate_image_bytes(data: bytes, content_type: str) -> None:
    """Validates the image file signatures match the expected content type.

    Args:
        data: The raw image bytes.
        content_type: The MIME content type.

    Raises:
        RuntimeError: If signatures do not match the expected type.
    """
    signatures = {
        "image/png": (b"\x89PNG\r\n\x1a\n",),
        "image/jpeg": (b"\xff\xd8\xff",),
        "image/gif": (b"GIF87a", b"GIF89a"),
        "image/webp": (b"RIFF",),
    }
    prefixes = signatures.get(content_type)
    if not prefixes:
        return
    if not any(data.startswith(prefix) for prefix in prefixes):
        raise RuntimeError("下载内容与图片类型不匹配")
    if content_type == "image/webp" and data[8:12] != b"WEBP":
        raise RuntimeError("下载内容与图片类型不匹配")


async def read_limited_response(updater, resp: aiohttp.ClientResponse) -> bytes:
    """Reads response content chunks up to the configured limit.

    Args:
        updater: The MemeUpdater instance.
        resp: The ClientResponse object.

    Returns:
        The complete downloaded bytes.

    Raises:
        RuntimeError: If the downloaded size exceeds the configured max limit.
    """
    max_bytes = updater.plugin_config.max_image_bytes()
    chunks = []
    total = 0
    async for chunk in resp.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise RuntimeError(f"响应内容超过大小限制：{max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


async def request_external_image(
    updater,
    session: aiohttp.ClientSession,
    url: str,
    timeout: aiohttp.ClientTimeout,
) -> tuple[bytes, str]:
    """Issues an HTTP request to download an external image.

    Args:
        updater: The MemeUpdater instance.
        session: The ClientSession instance.
        url: The image URL.
        timeout: The request timeout config.

    Returns:
        A tuple of (image_bytes, content_type).

    Raises:
        RuntimeError: If server responds with failure or invalid content.
    """
    current_url = url
    for _ in range(5):
        _, resolved_ips = await validate_external_image_url(current_url)
        async with session.get(
            current_url, allow_redirects=False, timeout=timeout
        ) as resp:
            peer_ip = response_peer_ip(resp)
            if peer_ip and is_forbidden_ip(peer_ip):
                raise RuntimeError("实际连接到了内网或本机地址")
            if peer_ip and resolved_ips and peer_ip not in resolved_ips:
                raise RuntimeError("图片下载连接地址与校验结果不一致")
            if not peer_ip:
                logger.debug(
                    f"无法确认图片下载的实际连接地址，已使用 DNS 校验结果继续: {current_url}"
                )
            if 300 <= resp.status < 400:
                location = resp.headers.get("Location")
                if not location:
                    raise RuntimeError("图片下载重定向缺少 Location")
                current_url = str(resp.url.join(location))
                continue
            data = await read_limited_response(updater, resp)
            if resp.status >= 400:
                raise RuntimeError(f"下载图片失败：HTTP {resp.status}")
            content_type = resp.headers.get("Content-Type", "image/png").split(";", 1)[
                0
            ]
            if not content_type.startswith("image/"):
                raise RuntimeError(f"下载内容不是图片：{content_type}")
            validate_image_bytes(data, content_type)
            return data, content_type
    raise RuntimeError("图片下载重定向次数过多")


async def download_image(updater, url: str) -> tuple[bytes, str, str]:
    """Downloads or resolves an image from a URL, local path, or base64 data.

    Args:
        updater: The MemeUpdater instance.
        url: The image source, which can be an HTTP/HTTPS URL, file:// URI,
            base64:// URI, data URI, or absolute local file path.

    Returns:
        A tuple of (data_bytes, content_type, filename).

    Raises:
        RuntimeError: If downloading or resolving the image fails.
    """

    def has_image_signature(data_bytes: bytes) -> bool:
        return (
            data_bytes.startswith(b"\x89PNG\r\n\x1a\n")
            or data_bytes.startswith(b"\xff\xd8\xff")
            or data_bytes.startswith(b"GIF87a")
            or data_bytes.startswith(b"GIF89a")
            or (data_bytes.startswith(b"RIFF") and data_bytes[8:12] == b"WEBP")
            or data_bytes.startswith(b"BM")
        )

    def detect_mime(data_bytes: bytes) -> str:
        if data_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data_bytes.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if data_bytes.startswith(b"GIF87a") or data_bytes.startswith(b"GIF89a"):
            return "image/gif"
        if data_bytes.startswith(b"RIFF") and data_bytes[8:12] == b"WEBP":
            return "image/webp"
        if data_bytes.startswith(b"BM"):
            return "image/bmp"
        return "image/png"

    # Try utilizing MediaResolver if available
    try:
        from astrbot.core.utils.media_utils import MediaResolver
    except ImportError:
        MediaResolver = None

    # 仅在 MediaResolver 可用且能返回有效图片时使用它；旧版本框架（如 2.25）
    # 没有该工具或其行为不同时，统一回退到下方的内置解析逻辑，保持兼容。
    if MediaResolver is not None:
        try:
            resolver = MediaResolver(url, media_type="image")
            async with resolver.as_path() as resolved:
                data = resolved.read_bytes()
                if not data or not has_image_signature(data):
                    raise RuntimeError("MediaResolver 返回的数据不是有效图片")
                content_type = getattr(resolved, "mime_type", None) or detect_mime(data)
                ext = mimetypes.guess_extension(content_type) or ".png"
                return data, content_type, f"image{ext}"
        except Exception as e:
            logger.warning(f"MediaResolver 无法解析图片 ({url})，将尝试 fallback: {e}")

    url_clean = url.strip()
    if url_clean.startswith("base64://"):
        try:
            b64_data = url_clean[len("base64://") :]
            b64_data = "".join(b64_data.split())
            missing_padding = len(b64_data) % 4
            if missing_padding:
                b64_data += "=" * (4 - missing_padding)
            data = base64.b64decode(b64_data)
            if not data or not has_image_signature(data):
                raise RuntimeError("base64 内容不是有效图片")
            content_type = detect_mime(data)
            ext = mimetypes.guess_extension(content_type) or ".png"
            return data, content_type, f"image{ext}"
        except Exception as e:
            raise RuntimeError(f"解析 base64 图片失败: {e}")

    if url_clean.startswith("data:image/"):
        try:
            header, b64_data = url_clean.split(",", 1)
            content_type = header.split(";")[0].split(":")[1]
            b64_data = "".join(b64_data.split())
            missing_padding = len(b64_data) % 4
            if missing_padding:
                b64_data += "=" * (4 - missing_padding)
            data = base64.b64decode(b64_data)
            if not data or not has_image_signature(data):
                raise RuntimeError("data URI 内容不是有效图片")
            ext = mimetypes.guess_extension(content_type) or ".png"
            return data, content_type, f"image{ext}"
        except Exception as e:
            raise RuntimeError(f"解析 data URI 图片失败: {e}")

    if url_clean.startswith("file://"):
        try:
            parsed = urlparse(url_clean)
            local_path = url2pathname(parsed.path)
            if os.name == "nt" and local_path.startswith("\\"):
                local_path = local_path.lstrip("\\")
            if not os.path.exists(local_path):
                if url_clean.startswith("file:///"):
                    local_path = url_clean[8:]
                else:
                    local_path = url_clean[7:]
                local_path = urllib.parse.unquote(local_path)
            with open(local_path, "rb") as f:
                data = f.read()
            if not data or not has_image_signature(data):
                raise RuntimeError("本地文件内容不是有效图片")
            content_type = detect_mime(data)
            ext = mimetypes.guess_extension(content_type) or ".png"
            return data, content_type, f"image{ext}"
        except Exception as e:
            raise RuntimeError(f"读取 file:// 本地图片失败: {e}")

    # Check if local file path
    is_local_file = False
    try:
        is_local_file = os.path.isabs(url_clean) or os.path.exists(url_clean)
    except Exception:
        pass
    if is_local_file:
        try:
            with open(url_clean, "rb") as f:
                data = f.read()
            if not data or not has_image_signature(data):
                raise RuntimeError("本地文件内容不是有效图片")
            content_type = detect_mime(data)
            ext = mimetypes.guess_extension(content_type) or ".png"
            return data, content_type, f"image{ext}"
        except Exception as e:
            raise RuntimeError(f"读取本地文件图片失败: {e}")

    # Check if bare base64 —— 仅当能解码出带有效图片头的数据时才采用，
    # 否则（例如 QQ 引用图片的 file 字段是一长串文件名/ID）继续走 HTTP 下载，
    # 避免把非图片字符串误当 base64 解出乱码字节送给 meme API。
    try:
        compact = "".join(url_clean.split())
        if len(compact) > 64 and re.fullmatch(r"[A-Za-z0-9+/=]+", compact):
            data = base64.b64decode(compact)
            if data and has_image_signature(data):
                content_type = detect_mime(data)
                ext = mimetypes.guess_extension(content_type) or ".png"
                return data, content_type, f"image{ext}"
    except Exception:
        pass

    # Fallback to HTTP/HTTPS download
    timeout = aiohttp.ClientTimeout(total=updater.plugin_config.meme_request_timeout())
    session = updater._ensure_download_session()
    data, content_type = await request_external_image(
        updater, session, url_clean, timeout
    )
    ext = mimetypes.guess_extension(content_type) or ".png"
    return data, content_type, f"image{ext}"


def extract_message_segments(updater, event: AstrMessageEvent) -> list[object]:
    """Extracts raw message segments from the message event.

    Args:
        updater: The MemeUpdater instance.
        event: The AstrMessageEvent.

    Returns:
        A list of segments.
    """
    return extract_segments_from_event(event)


async def get_replied_message_segments(
    updater, event: AstrMessageEvent
) -> list[object]:
    """Extracts segments of a replied message referenced in the event.

    Args:
        updater: The MemeUpdater instance.
        event: The AstrMessageEvent.

    Returns:
        A list of segments from the replied message, or empty list.
    """
    for segment in extract_message_segments(updater, event):
        if isinstance(segment, dict):
            if segment.get("type") != "reply":
                continue
            data = segment.get("data") or {}
            message_id = str(
                data.get("id") or data.get("message_id") or data.get("msg_id") or ""
            ).strip()
        else:
            if hasattr(segment, "chain"):
                # If segment has its own chain (e.g. Comp.Reply), return its content directly
                chain = getattr(segment, "chain", [])
                return list(chain) if hasattr(chain, "__iter__") else [chain]
            message_id = str(
                getattr(segment, "id", "")
                or getattr(segment, "message_id", "")
                or getattr(segment, "msg_id", "")
                or getattr(segment, "reply_id", "")
                or getattr(segment, "target", "")
                or ""
            ).strip()
            if not message_id:
                data = getattr(segment, "data", None)
                if isinstance(data, dict):
                    message_id = str(
                        data.get("id")
                        or data.get("message_id")
                        or data.get("msg_id")
                        or ""
                    ).strip()
            if not message_id and not hasattr(segment, "chain"):
                continue
        if not message_id:
            logger.warning(
                f"获取引用消息失败：未找到引用消息 ID，segment={type(segment).__name__}"
            )
            return []
        try:
            msg = await event.bot.get_msg(message_id=int(message_id))
        except Exception as e:
            logger.warning(f"获取引用消息失败：message_id={message_id}，{e}")
            return []
        segments = msg.get("message", []) if isinstance(msg, dict) else []
        return segments if isinstance(segments, list) else []
    return []


def extract_image_urls_from_segments(updater, segments: list[object]) -> list[str]:
    """Retrieves all image URLs/references from the given message segments.

    Args:
        updater: The MemeUpdater instance.
        segments: The list of segments.

    Returns:
        A list of image source strings.
    """
    urls = []
    for segment in segments:
        if isinstance(segment, dict):
            if segment.get("type") not in {"image", "mface"}:
                continue
            data = segment.get("data", {})
            candidates = [data.get("url"), data.get("file"), data.get("path")]
        else:
            candidates = [
                getattr(segment, "url", None),
                getattr(segment, "file", None),
                getattr(segment, "path", None),
            ]
        for candidate in candidates:
            value = str(candidate or "").strip()
            if not value:
                continue
            is_valid = False
            if (
                value.startswith("http://")
                or value.startswith("https://")
                or value.startswith("file://")
                or value.startswith("base64://")
                or value.startswith("data:image/")
            ):
                is_valid = True
            else:
                try:
                    if os.path.isabs(value) or os.path.exists(value):
                        is_valid = True
                    else:
                        # check if it is bare base64 with valid image headers
                        compact = "".join(value.split())
                        if len(compact) > 64 and re.fullmatch(
                            r"[A-Za-z0-9+/=]+", compact
                        ):
                            decoded = base64.b64decode(compact)
                            if (
                                decoded.startswith(b"\x89PNG\r\n\x1a\n")
                                or decoded.startswith(b"\xff\xd8\xff")
                                or decoded.startswith(b"GIF87a")
                                or decoded.startswith(b"GIF89a")
                                or (
                                    decoded.startswith(b"RIFF")
                                    and decoded[8:12] == b"WEBP"
                                )
                            ):
                                is_valid = True
                except Exception:
                    pass
            if is_valid:
                urls.append(value)
                break
    return urls


def extract_message_image_urls(updater, event: AstrMessageEvent) -> list[str]:
    """Extracts all image source locations from the message event.

    Args:
        updater: The MemeUpdater instance.
        event: The AstrMessageEvent.

    Returns:
        A list of image source strings.
    """
    segments = [
        segment
        for segment in extract_message_segments(updater, event)
        if isinstance(segment, dict) or not hasattr(segment, "chain")
    ]
    return extract_image_urls_from_segments(updater, segments)


def extract_segments_from_event(event: AstrMessageEvent) -> list[object]:
    """Helper to retrieve message segments from various event structure candidates.

    Args:
        event: The AstrMessageEvent.

    Returns:
        A list of segments.
    """
    get_messages = getattr(event, "get_messages", None)
    if callable(get_messages):
        try:
            messages = get_messages()
            if isinstance(messages, list):
                return messages
            if messages is not None and not isinstance(messages, (str, bytes, dict)):
                try:
                    return list(messages)
                except Exception:
                    pass
        except Exception:
            pass
    message_obj = getattr(event, "message_obj", None)
    for value in (
        getattr(message_obj, "message", None),
        getattr(message_obj, "raw_message", None),
        getattr(event, "message", None),
    ):
        if isinstance(value, list):
            return value
        if isinstance(value, dict) and isinstance(value.get("message"), list):
            return value["message"]
    return []


def raw_event_dict_from_event(event: AstrMessageEvent) -> dict:
    """Helper to locate the raw platform payload dictionary in the event.

    Args:
        event: The AstrMessageEvent.

    Returns:
        The raw event dictionary, or empty dictionary.
    """
    message_obj = getattr(event, "message_obj", None)
    for value in (
        getattr(message_obj, "raw_message", None),
        getattr(message_obj, "raw_event", None),
        getattr(message_obj, "raw", None),
        getattr(event, "raw_message", None),
        getattr(event, "raw_event", None),
        getattr(event, "raw", None),
    ):
        if isinstance(value, dict):
            return value
    return {}
