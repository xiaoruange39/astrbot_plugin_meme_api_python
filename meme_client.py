import asyncio
import json
from collections.abc import Callable
from urllib.parse import quote

import aiohttp
from astrbot.api import logger

MAX_JSON_RESPONSE_BYTES = 2 * 1024 * 1024


class MemeApiClient:
    def __init__(
        self,
        base_url_getter: Callable[[], str],
        timeout_getter: Callable[[], int],
        max_image_bytes_getter: Callable[[], int],
        info_concurrency_getter: Callable[[], int],
        verbose_log_getter: Callable[[], bool],
    ):
        self._base_url_getter = base_url_getter
        self._timeout_getter = timeout_getter
        self._max_image_bytes_getter = max_image_bytes_getter
        self._info_concurrency_getter = info_concurrency_getter
        self._verbose_log_getter = verbose_log_getter

    async def _read_limited_response(self, resp: aiohttp.ClientResponse, limit: int | None = None) -> bytes:
        max_bytes = limit or self._max_image_bytes_getter()
        chunks = []
        total = 0
        async for chunk in resp.content.iter_chunked(64 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise RuntimeError(f"响应内容超过大小限制：{max_bytes} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    async def _get_json(self, paths: list[str], session: aiohttp.ClientSession | None = None, attempts: int = 3):
        errors = []
        timeout = aiohttp.ClientTimeout(total=self._timeout_getter(), sock_connect=5, sock_read=5)

        async def request_once(active_session: aiohttp.ClientSession, path: str):
            async with active_session.get(f"{self._base_url_getter()}{path}", timeout=timeout) as resp:
                data = await self._read_limited_response(resp, MAX_JSON_RESPONSE_BYTES)
                text = data.decode("utf-8", errors="replace")
                if resp.status >= 400:
                    raise RuntimeError(f"HTTP {resp.status}: {text[:200]}")
                content_type = resp.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if content_type and content_type not in {"application/json", "text/json"} and not content_type.endswith("+json"):
                    raise RuntimeError(f"meme API 返回非 JSON 内容：{content_type}")
                try:
                    return json.loads(text) if text else None
                except json.JSONDecodeError as e:
                    raise RuntimeError(f"meme API JSON 解析失败：{e.msg}，响应片段：{text[:120]}") from e

        for attempt in range(1, attempts + 1):
            if session:
                for path in paths:
                    try:
                        return await request_once(session, path)
                    except Exception as e:
                        errors.append(f"{path}: {e}")
            else:
                async with aiohttp.ClientSession(timeout=timeout) as new_session:
                    for path in paths:
                        try:
                            return await request_once(new_session, path)
                        except Exception as e:
                            errors.append(f"{path}: {e}")
            if attempt < attempts:
                logger.warning(f"meme API 请求失败，准备重试 {attempt}/{attempts}：{errors[-1] if errors else '未知错误'}")
                await asyncio.sleep(attempt * 2)
        raise RuntimeError("; ".join(errors[-6:]) or "meme API 请求失败")

    async def _post_image(self, paths: list[str], *, json_body: dict | None = None, form_factory=None) -> tuple[bytes, str]:
        errors = []
        timeout = aiohttp.ClientTimeout(total=self._timeout_getter(), sock_connect=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for path in paths:
                try:
                    kwargs = {}
                    if form_factory is not None:
                        kwargs["data"] = form_factory()
                    else:
                        kwargs["json"] = json_body or {}
                    async with session.post(f"{self._base_url_getter()}{path}", **kwargs) as resp:
                        data = await self._read_limited_response(resp)
                        content_type = resp.headers.get("Content-Type", "").split(";", 1)[0].lower()
                        if resp.status < 400:
                            if not content_type.startswith("image/"):
                                text = data.decode("utf-8", errors="replace")[:300]
                                raise RuntimeError(f"meme API 返回非图片内容：{content_type or '未知类型'}，响应片段：{text}")
                            return data, content_type
                        errors.append(f"{path}: HTTP {resp.status}: {data.decode('utf-8', errors='replace')[:300]}")
                except Exception as e:
                    errors.append(f"{path}: {e}")
        raise RuntimeError("; ".join(errors) or "meme API 渲染失败")

    async def _get_image(self, paths: list[str]) -> tuple[bytes, str]:
        last_error = ""
        timeout = aiohttp.ClientTimeout(total=self._timeout_getter(), sock_connect=5, sock_read=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for path in paths:
                try:
                    async with session.get(f"{self._base_url_getter()}{path}", timeout=timeout) as resp:
                        data = await self._read_limited_response(resp)
                        content_type = resp.headers.get("Content-Type", "").split(";", 1)[0].lower()
                        if resp.status < 400:
                            if not content_type.startswith("image/"):
                                text = data.decode("utf-8", errors="replace")[:300]
                                raise RuntimeError(f"meme API 返回非图片内容：{content_type or '未知类型'}，响应片段：{text}")
                            return data, content_type
                        last_error = data.decode("utf-8", errors="replace")[:300]
                except Exception as e:
                    last_error = str(e)
        raise RuntimeError(last_error or "meme API 图片请求失败")

    async def fetch_meme_infos(self) -> dict[str, dict]:
        logger.info(f"刷新 meme API 表情信息: {self._base_url_getter()}")
        payload = await self._get_json(["/memes", "/memes/", "/memes/keys", "/keys"])
        if isinstance(payload, dict):
            payload = payload.get("memes") or payload.get("data") or payload.get("keys")

        if not isinstance(payload, list):
            raise RuntimeError("meme API 返回的表情列表格式不正确")

        if payload and all(isinstance(item, dict) for item in payload):
            entries = []
            for item in payload:
                key = str(item.get("key") or item.get("meme_key") or "").strip()
                if not key:
                    logger.warning(f"表情信息缺少 key，跳过: {item}")
                    continue
                item.setdefault("key", key)
                item.setdefault("keywords", [key])
                item.setdefault("shortcuts", [])
                item.setdefault("tags", [])
                entries.append((key, item))
            logger.info(f"meme API 表情信息刷新完成，共获取 {len(entries)} 个详情")
            return dict(entries)

        keys = payload

        total = len(keys)
        verbose_log = self._verbose_log_getter()
        logger.info(f"meme API 返回 {total} 个表情，开始加载详情")
        semaphore = asyncio.Semaphore(self._info_concurrency_getter())

        async def load_info(session: aiohttp.ClientSession, index: int, key: str) -> tuple[str, dict] | None:
            async with semaphore:
                if verbose_log:
                    logger.info(f"[{index}/{total}] 获取表情信息: {key}")
                try:
                    quoted_key = quote(key, safe="")
                    info = await self._get_json([
                        f"/memes/{quoted_key}/info",
                        f"/memes/{quoted_key}/",
                        f"/memes/{quoted_key}",
                    ], session=session, attempts=1)
                    if not isinstance(info, dict):
                        logger.warning(f"{key} 的 info 格式不正确，跳过")
                        return None
                    info.setdefault("key", key)
                    info.setdefault("keywords", [key])
                    info.setdefault("shortcuts", [])
                    info.setdefault("tags", [])
                    return key, info
                except Exception as e:
                    logger.warning(f"获取表情信息失败 {key}: {e}")
                    return None

        timeout = aiohttp.ClientTimeout(total=self._timeout_getter(), sock_connect=5, sock_read=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            results = await asyncio.gather(*(load_info(session, i + 1, str(key)) for i, key in enumerate(keys)), return_exceptions=True)

        entries = [r for r in results if r is not None and not isinstance(r, Exception)]
        return dict(entries)

    async def get_preview(self, key: str) -> tuple[bytes, str]:
        quoted_key = quote(str(key), safe="")
        return await self._get_image([f"/memes/{quoted_key}/preview", f"/memes/{quoted_key}/preview/"])

    async def render_meme(
        self,
        key: str,
        images: list[tuple[bytes, str, str]],
        texts: list[str],
        user_infos: list[dict],
        options: dict[str, object] | None = None,
    ) -> tuple[bytes, str]:
        def make_form() -> aiohttp.FormData:
            form = aiohttp.FormData()
            for data, content_type, filename in images:
                form.add_field("images", data, filename=filename, content_type=content_type)
            for text in texts:
                form.add_field("texts", str(text))
            render_args = {"user_infos": user_infos}
            if options:
                render_args.update(options)
            form.add_field("args", json.dumps(render_args, ensure_ascii=False))
            return form

        quoted_key = quote(str(key), safe="")
        return await self._post_image([
            f"/memes/{quoted_key}/render",
            f"/memes/{quoted_key}/",
            f"/memes/{quoted_key}",
        ], form_factory=make_form)

    async def render_list(self, meme_list: list[dict], text_template: str) -> tuple[bytes, str]:
        return await self._post_image([
            "/memes/render_list",
            "/memes/list",
            "/render_list",
        ], json_body={
            "meme_list": meme_list,
            "text_template": text_template,
            "add_category_icon": True,
        })
