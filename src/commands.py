import asyncio
import base64
import mimetypes
import os
import random
import re
import tempfile
import time
from datetime import datetime

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

try:
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
        AiocqhttpMessageEvent,
    )
except Exception:  # pragma: no cover - optional platform dependency
    AiocqhttpMessageEvent = None

from .arg_parser import (
    ArgSyntaxError,
    direction_options_from_text,
    materialize_direction_options,
    normalize_meme_options,
    split_arg_string,
)
from .image_resolver import (
    MAX_IMAGE_DOWNLOAD_CONCURRENCY,
    download_image,
    extract_image_urls_from_segments,
    extract_message_image_urls,
    extract_message_segments,
    get_replied_message_segments,
)
from .platform_utils import (
    avatar_url,
    bot_avatar_url,
    bot_user_info,
    group_id,
    lookup_sender_name,
    sender_avatar_url,
    sender_id,
    sender_user_info,
    try_send_forward_message,
)

TEMP_IMAGE_TTL_SECONDS = 3600
TEMP_IMAGE_CLEANUP_INTERVAL_SECONDS = 300
MEME_API_RESTART_REFRESH_INTERVAL_SECONDS = 5
LOUVRE_KEY = "louvre"
LOUVRE_MODE_MAPPING = {
    "随机": 0,
    "精细": 1,
    "一般": 2,
    "稍粗": 3,
    "超粗": 4,
    "极粗": 5,
    "浮雕": 6,
    "线稿": 7,
    "彩色线稿": 7,
    "线稿彩色": 7,
}
MIRAGETANK_KEY = "miragetank"


def _format_range(min_value: int, max_value: int) -> str:
    return str(min_value) if min_value == max_value else f"{min_value} ~ {max_value}"


def _format_keywords(keywords: list[str]) -> str:
    return "、".join(f"“{keyword}”" for keyword in keywords)


def _sorted_meme_infos(
    updater, meme_infos: dict[str, dict] | None = None
) -> list[dict]:
    sort_by = updater.plugin_config.meme_list_sort_by()
    reverse = updater.plugin_config.meme_list_sort_reverse()
    infos = list((meme_infos or updater.meme_infos).values())
    if sort_by == "名称":
        return sorted(
            infos,
            key=lambda info: str(info.get("key", "")).lower(),
            reverse=reverse,
        )
    if sort_by == "关键词":
        return sorted(
            infos, key=lambda info: _meme_keyword_sort_value(info), reverse=reverse
        )
    if sort_by == "更新时间":
        return sorted(
            infos,
            key=lambda info: _parse_meme_time(info.get("date_modified")),
            reverse=reverse,
        )
    return sorted(
        infos,
        key=lambda info: _parse_meme_time(info.get("date_created")),
        reverse=reverse,
    )


def _parse_meme_time(value: object) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _meme_keyword_sort_value(info: dict) -> str:
    keywords = info.get("keywords")
    if isinstance(keywords, list) and keywords:
        return str(keywords[0]).lower()
    return ""


def _image_component(updater, data: bytes, content_type: str) -> Comp.Image:
    ext = mimetypes.guess_extension(content_type) or ".png"
    try:
        temp_dir = os.path.join(updater._meme_data_dir, "temp")
        os.makedirs(temp_dir, exist_ok=True)
        now = time.time()
        cleanup_task = updater._temp_cleanup_task
        background_alive = cleanup_task is not None and not cleanup_task.done()
        if (
            not background_alive
            and now - updater._last_temp_cleanup >= TEMP_IMAGE_CLEANUP_INTERVAL_SECONDS
        ):
            _cleanup_temp_images(temp_dir, now)
            updater._last_temp_cleanup = now
        with tempfile.NamedTemporaryFile(suffix=ext, dir=temp_dir, delete=False) as tf:
            tf.write(data)
            temp_path = tf.name
        temp_path = os.path.abspath(temp_path).replace("\\", "/")
        return Comp.Image(file=f"file:///{temp_path}")
    except Exception as e:
        logger.warning(f"无法创建临时文件发送图片，降级到 base64: {e}")
        b64 = base64.b64encode(data).decode("ascii")
        return Comp.Image(file=f"base64://{b64}")


async def _try_send_small_image_aiocqhttp(
    updater,
    event: AstrMessageEvent,
    data: bytes,
    *,
    text: str = "",
    summary: str = "表情包",
) -> bool:
    """Send an image as a OneBot small sticker-like image on aiocqhttp.

    AstrBot's generic Image component is portable, but aiocqhttp/NapCat/Lagrange
    support a QQ small-image/sticker-like mode through the OneBot image segment's
    subType fields. Keep this path optional and fall back to the normal
    event.chain_result image everywhere else.
    """
    if not updater.plugin_config.meme_send_small_image_enabled():
        return False
    if AiocqhttpMessageEvent is None:
        return False
    if event.get_platform_name() != "aiocqhttp" or not isinstance(
        event, AiocqhttpMessageEvent
    ):
        return False

    try:
        message = []
        if text.strip():
            message.append({"type": "text", "data": {"text": text}})
        b64 = base64.b64encode(data).decode("ascii")
        message.append(
            {
                "type": "image",
                "data": {
                    "file": f"base64://{b64}",
                    # Different OneBot implementations look for different
                    # spellings, so keep all three like Giftia does.
                    "subType": 1,
                    "sub_type": 1,
                    "subtype": 1,
                    "summary": summary,
                },
            }
        )

        group_id_value = event.get_group_id()
        if group_id_value:
            await event.bot.send_group_msg(
                group_id=int(group_id_value), message=message
            )
        else:
            await event.bot.send_private_msg(
                user_id=int(event.get_sender_id()), message=message
            )
        return True
    except Exception as e:
        logger.warning(f"发送小图表情包失败，降级为普通图片: {e}")
        return False


def _cleanup_temp_images(temp_dir: str, now: float | None = None) -> None:
    expires_before = (now or time.time()) - TEMP_IMAGE_TTL_SECONDS
    for name in os.listdir(temp_dir):
        path = os.path.join(temp_dir, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < expires_before:
                os.remove(path)
        except OSError:
            pass


def _shortcut_args(template_args: list, match: re.Match) -> str:
    resolved = []
    for value in template_args:
        text = str(value)

        def repl(m: re.Match) -> str:
            name = m.group(1)
            if name.isdigit():
                try:
                    return match.group(int(name)) or name
                except IndexError:
                    return name
            return match.groupdict().get(name) or name

        resolved.append(re.sub(r"\{(.+?)\}", repl, text))
    return " ".join(resolved)


def _strip_random_direction_options(
    key: str, options: dict[str, object]
) -> dict[str, object]:
    if key != "turn":
        return options
    return {
        name: value
        for name, value in options.items()
        if name not in {"__direction", "direction", "left", "right", "top", "bottom"}
    }


def _louvre_options_from_args(
    key: str, tokens: list[str]
) -> tuple[list[str], dict[str, object]]:
    if key != LOUVRE_KEY:
        return tokens, {}
    remaining = []
    options: dict[str, object] = {}
    for token in tokens:
        if "number" not in options:
            stripped = token.lstrip("#").strip()
            if stripped in LOUVRE_MODE_MAPPING:
                options["number"] = LOUVRE_MODE_MAPPING[stripped]
                continue
            if stripped.isdigit():
                n = int(stripped)
                if 0 <= n <= 7:
                    options["number"] = n
                    continue
        remaining.append(token)
    return remaining, options


def _select_render_images(
    images: list[tuple[bytes, str, str]],
    user_infos: list[dict],
    max_images: int,
) -> tuple[list[tuple[bytes, str, str]], list[dict]]:
    if max_images <= 0 or len(images) <= max_images:
        return images, user_infos
    selected_images = images[:max_images]
    selected_user_infos = user_infos[:max_images]
    return selected_images, selected_user_infos


def _safe_int(value: object, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _params_type(info: dict) -> dict:
    params = info.get("params_type") or {}
    return {
        "min_images": _safe_int(params.get("min_images")),
        "max_images": _safe_int(params.get("max_images")),
        "min_texts": _safe_int(params.get("min_texts")),
        "max_texts": _safe_int(params.get("max_texts")),
        "default_texts": list(params.get("default_texts") or []),
    }


def _format_meme_list_text(updater, meme_infos: dict[str, dict] | None = None) -> str:
    template = updater.plugin_config.meme_list_text_template()
    lines = []
    for index, info in enumerate(_sorted_meme_infos(updater, meme_infos), 1):
        keywords = "、".join(
            str(value) for value in info.get("keywords", []) if str(value).strip()
        )
        line = template.format(
            index=index,
            key=info.get("key", ""),
            keywords=keywords or updater.disabled_memes.meme_display_name(info),
        )
        lines.append(line)
    return "\n".join(lines)


async def _render_list(
    updater, meme_infos: dict[str, dict] | None = None
) -> tuple[bytes, str]:
    meme_list = []
    now = datetime.now().timestamp()
    for index, info in enumerate(_sorted_meme_infos(updater, meme_infos), 1):
        labels = []
        try:
            created = _parse_meme_time(info.get("date_created"))
            if now - created <= 30 * 24 * 3600:
                labels.append("new")
        except Exception:
            pass
        meme_list.append(
            {
                "meme_key": info.get("key"),
                "disabled": False,
                "labels": labels,
                "index": index,
            }
        )
    return await updater.meme_client.render_list(
        meme_list,
        updater.plugin_config.meme_list_text_template(),
        timeout_total=updater.plugin_config.meme_list_render_timeout(),
    )


async def restart_memeapi(updater, event: AstrMessageEvent):
    """Restarts the memeapi server backend.

    Args:
        updater: The MemeUpdater instance.
        event: The AstrMessageEvent.
    """
    try:
        yield event.plain_result("正在重启 memeapi 服务，请稍候...")
        result = await updater.repo_manager.restart_memeapi()
        yield event.plain_result("\n".join(result["lines"]))
    finally:
        updater._stop_event(event)


async def update_memes(updater, event: AstrMessageEvent):
    """Syncs the meme repository and restarts the API server.

    Args:
        updater: The MemeUpdater instance.
        event: The AstrMessageEvent.
    """
    try:
        if not updater.plugin_config.repo_update_enabled():
            yield event.plain_result(
                "更新表情包功能未启用，请先在配置项 repo_update_enabled 中开启。"
            )
            return

        yield event.plain_result("正在更新表情包数据，请稍候...")

        os.makedirs(updater._meme_data_dir, exist_ok=True)

        started_at = datetime.now()
        repos = updater.repo_manager.repos()
        total = len(repos)
        semaphore = asyncio.Semaphore(updater.plugin_config.repo_update_concurrency())

        async def sync_limited(repo: dict, index: int):
            async with semaphore:
                return await updater.repo_manager.sync_repo(repo, index, total)

        results = await asyncio.gather(
            *[sync_limited(repo, i + 1) for i, repo in enumerate(repos)],
            return_exceptions=True,
        )
        normalized_results = []
        for i, result in enumerate(results, 1):
            if isinstance(result, Exception):
                normalized_results.append(
                    {
                        "status": "failed",
                        "updated": False,
                        "lines": [f"❌ [{i}/{total}] 更新异常", f"    {result}"],
                    }
                )
            else:
                normalized_results.append(result)
        results = normalized_results
        success = sum(1 for r in results if r["status"] == "success")
        success_updates = [
            r for r in results if r["status"] == "success" and r["updated"]
        ]
        failed = sum(1 for r in results if r["status"] == "failed")
        updated = sum(1 for r in results if r["updated"])

        restart_result = None
        if updated > 0:
            restart_result = await updater.repo_manager.restart_memeapi()
            if restart_result["success"]:
                restart_result["lines"].append("⏳ 等待 meme API 启动后刷新表情信息...")
                await asyncio.sleep(MEME_API_RESTART_REFRESH_INTERVAL_SECONDS)
                await updater._refresh_meme_infos_after_restart(restart_result["lines"])

        finished_at = datetime.now()
        summary_lines = [
            "========================",
            "📋 更新任务执行完成",
            f"⏰ {updater._format_time(started_at)} → {updater._format_time(finished_at)} ({(finished_at - started_at).total_seconds():.2f}秒)",
            f"📊 成功:{success} 失败:{failed} 更新:{updated}",
            "========================",
            "🔌 开始执行 meme 仓库更新任务",
            f"⏰ 开始时间: {updater._format_time(started_at)}",
            "========================",
        ]

        for result in results:
            summary_lines.extend(result["lines"])

        if success_updates:
            summary_lines.extend(["========================", "✅ 成功更新的仓库:"])
            for result in success_updates:
                success_line = next(
                    (
                        line.strip()
                        for line in result["lines"]
                        if "完成" in line and "📦" not in line
                    ),
                    result["lines"][-1],
                )
                summary_lines.append(f"  - {success_line}")

        if restart_result:
            summary_lines.append("准备重启 memeapi...")
            summary_lines.extend(restart_result["lines"])
            summary_lines.append("========================")
            summary_lines.append(
                f"📌 重启状态: {'成功' if restart_result['success'] else '失败'}"
            )
        else:
            summary_lines.extend(
                [
                    "仓库无更新，已跳过 memeapi 重启。",
                    "========================",
                ]
            )

        yield event.plain_result("\n".join(summary_lines))
    except Exception as e:
        logger.exception("更新表情包失败")
        yield event.plain_result(f"更新表情包失败：{e}")
    finally:
        updater._stop_event(event)


async def meme_status(updater, event: AstrMessageEvent):
    """Gets the status of the memeapi server and loaded repos.

    Args:
        updater: The MemeUpdater instance.
        event: The AstrMessageEvent.
    """
    updater._stop_event(event)
    lines = ["表情包仓库状态:"]

    repos = updater.repo_manager.repos()

    for repo in repos:
        lines.append(await updater.repo_manager.repo_status(repo))

    try:
        count = await updater._refresh_meme_infos()
        lines.append(
            f"meme API: 已加载 {count} 个表情 | {updater.plugin_config.meme_api_base_url()}"
        )
    except Exception as e:
        lines.append(
            f"meme API: 无法连接或加载失败 | {updater.plugin_config.meme_api_base_url()} | {e}"
        )

    yield event.plain_result("\n".join(lines))


async def refresh_meme_infos(updater, event: AstrMessageEvent):
    """Refreshes the meme info data from the backend server.

    Args:
        updater: The MemeUpdater instance.
        event: The AstrMessageEvent.
    """
    updater._stop_event(event)
    task = updater._meme_info_refresh_task
    if task and not task.done():
        yield event.plain_result("meme API 表情信息仍在后台加载中，请稍后再试。")
        return
    if task and task.done():
        try:
            task.result()
        except Exception:
            pass
    task = asyncio.create_task(updater._refresh_meme_infos(force=True))
    updater._meme_info_refresh_task = task
    try:
        count = await task
        yield event.plain_result(f"表情信息刷新完成，共载入 {count} 个表情。")
    except Exception as e:
        yield event.plain_result(f"刷新表情信息失败：{e}")
    finally:
        if updater._meme_info_refresh_task is task:
            updater._meme_info_refresh_task = None


async def set_meme_disabled(
    updater, event: AstrMessageEvent, command_name: str, force_global: bool = False
):
    """Internal helper to disable a meme either locally or globally.

    Args:
        updater: The MemeUpdater instance.
        event: The AstrMessageEvent.
        command_name: The calling command name.
        force_global: If True, blocks it globally.
    """
    updater._stop_event(event)
    name = updater._get_message_args(event, command_name)
    if not name:
        yield event.plain_result(
            f"用法：{command_name} {'[群号] ' if not force_global else ''}<表情名/关键词/key>"
        )
        return
    if force_global:
        group_id_val = ""
    else:
        group_id_val, name = updater._block_scope_from_args(event, name)
    if not name:
        yield event.plain_result(
            f"用法：{command_name} {'[群号] ' if not force_global else ''}<表情名/关键词/key>"
        )
        return
    scope_name = (
        "全局" if force_global else await updater._block_scope_name(event, group_id_val)
    )
    await updater._refresh_meme_infos()
    all_meme_infos = updater.meme_infos
    result = updater.disabled_memes.disable(group_id_val, name, all_meme_infos)
    if result.status == "not_found":
        yield event.plain_result(f"未找到表情 “{name}”，请确认名称 or 关键词是否正确。")
        return
    if result.status == "already_disabled":
        yield event.plain_result(f"“{name}” 已在{scope_name}屏蔽列表中。")
        return
    yield event.plain_result(
        f"已在{scope_name}屏蔽表情 “{result.display_name}”，当前{scope_name}共屏蔽 {result.count} 个。"
    )


async def unset_meme_disabled(
    updater, event: AstrMessageEvent, command_name: str, force_global: bool = False
):
    """Internal helper to enable a previously disabled meme either locally or globally.

    Args:
        updater: The MemeUpdater instance.
        event: The AstrMessageEvent.
        command_name: The calling command name.
        force_global: If True, unblocks it globally.
    """
    updater._stop_event(event)
    name = updater._get_message_args(event, command_name)
    if not name:
        yield event.plain_result(
            f"用法：{command_name} {'[群号] ' if not force_global else ''}<表情名/关键词/key>"
        )
        return
    if force_global:
        group_id_val = ""
    else:
        group_id_val, name = updater._block_scope_from_args(event, name)
    if not name:
        yield event.plain_result(
            f"用法：{command_name} {'[群号] ' if not force_global else ''}<表情名/关键词/key>"
        )
        return
    scope_name = (
        "全局" if force_global else await updater._block_scope_name(event, group_id_val)
    )
    await updater._refresh_meme_infos()
    all_meme_infos = updater.meme_infos
    result = updater.disabled_memes.enable(group_id_val, name, all_meme_infos)
    if result.status == "not_found":
        yield event.plain_result(f"未找到表情 “{name}”，请确认名称 or 关键词是否正确。")
        return
    if result.status == "not_disabled":
        yield event.plain_result(
            f"表情 “{result.display_name}” 不在{scope_name}屏蔽列表中。"
        )
        return
    yield event.plain_result(
        f"已在{scope_name}取消屏蔽 “{result.display_name}”，当前{scope_name}共屏蔽 {result.count} 个。"
    )


async def list_disabled_memes(updater, event: AstrMessageEvent, group_id_val: str):
    """Lists disabled memes for a given group or globally.

    Args:
        updater: The MemeUpdater instance.
        event: The AstrMessageEvent.
        group_id_val: The group ID.
    """
    title, scope_name = await updater._block_scope_title(event, group_id_val)
    keys = sorted(
        updater.plugin_config.disabled_meme_names()
        if not group_id_val
        else updater.plugin_config.disabled_meme_names_for_group(group_id_val)
    )
    if not keys:
        yield event.plain_result(f"{scope_name}没有屏蔽任何表情。")
        return
    await updater._refresh_meme_infos()
    all_meme_infos = updater.meme_infos
    display_names = updater.disabled_memes.disabled_display_names(
        all_meme_infos, set(keys)
    )
    try:
        image, content_type = await asyncio.to_thread(
            updater.image_renderer.render_disabled_memes, display_names, title
        )
        yield event.chain_result([_image_component(updater, image, content_type)])
    except Exception as e:
        logger.warning(f"渲染屏蔽表情列表图片失败: {e}")
        lines = [f"{title}：共屏蔽 {len(display_names)} 个表情"]
        lines.extend(f"  {i}. {n}" for i, n in enumerate(display_names, 1))
        yield event.plain_result("\n".join(lines))


async def meme_usage_stats(updater, event: AstrMessageEvent):
    """Lists usage statistics of memes inside this chat.

    Args:
        updater: The MemeUpdater instance.
        event: The AstrMessageEvent.
    """
    if not updater._is_allowed_group(event):
        return
    updater._stop_event(event)
    group_id_val = group_id(event)
    scope = "group" if group_id_val else "global"
    rows = updater.usage_stats.rows(scope=scope, group_id=group_id_val)
    if not rows:
        yield event.plain_result(
            "当前群组暂无表情调用统计。" if group_id_val else "暂无表情调用统计。"
        )
        return
    try:
        await updater._refresh_meme_infos()
        title_override = None if group_id_val else "总表情统计"
        image, content_type = await asyncio.to_thread(
            updater.image_renderer.render_meme_usage_stats,
            rows,
            scope=scope,
            group_id=group_id_val,
            title_override=title_override,
        )
        yield event.chain_result([_image_component(updater, image, content_type)])
    except Exception as e:
        logger.warning(f"生成表情调用统计图失败: {e}")
        yield event.plain_result(
            updater.usage_stats.format_text(rows, scope=scope, group_id=group_id_val)
        )


async def meme_global_usage_stats(updater, event: AstrMessageEvent):
    """Lists global usage statistics of memes across all chats.

    Args:
        updater: The MemeUpdater instance.
        event: The AstrMessageEvent.
    """
    if not updater._is_allowed_group(event):
        return
    updater._stop_event(event)
    rows = updater.usage_stats.rows(scope="global")
    if not rows:
        yield event.plain_result("暂无表情调用统计。")
        return
    try:
        await updater._refresh_meme_infos()
        image, content_type = await asyncio.to_thread(
            updater.image_renderer.render_meme_usage_stats,
            rows,
            scope="global",
            title_override="总表情统计",
        )
        yield event.chain_result([_image_component(updater, image, content_type)])
    except Exception as e:
        logger.warning(f"生成总表情调用统计图失败: {e}")
        yield event.plain_result(updater.usage_stats.format_text(rows))


async def meme_search(updater, event: AstrMessageEvent):
    """Searches for memes matching a keyword.

    Args:
        updater: The MemeUpdater instance.
        event: The AstrMessageEvent.
    """
    if not updater._is_allowed_group(event):
        return
    updater._stop_event(event)
    query = updater._get_message_args(event, "meme搜索")
    if not query:
        yield event.plain_result("用法：meme搜索 <关键词>")
        return
    try:
        await updater._refresh_meme_infos()
        visible_infos = updater._visible_meme_infos(event)
        matches = updater._search_memes(query, visible_infos)
        if not matches:
            yield event.plain_result(f"未找到相关表情：{query}")
            return
        title = f"搜索结果（查看 {len(matches)} 条搜索结果）"
        result_text = "\n".join(
            [
                title,
                *[
                    updater._format_meme_search_result(index, info)
                    for index, info in enumerate(matches, 1)
                ],
            ]
        )
        if (
            updater.plugin_config.meme_search_forward_enabled()
            and await try_send_forward_message(event, title, result_text, len(matches))
        ):
            return
        yield event.plain_result(result_text)
    except Exception as e:
        yield event.plain_result(f"搜索表情失败：{e}")


async def meme_list(updater, event: AstrMessageEvent):
    """Retrieves list of all available memes.

    Args:
        updater: The MemeUpdater instance.
        event: The AstrMessageEvent.
    """
    if not updater._is_allowed_group(event):
        return
    try:
        await updater._refresh_meme_infos()
        visible_infos = updater._visible_meme_infos(event)
        try:
            image, content_type = await _render_list(updater, visible_infos)
            yield event.chain_result([_image_component(updater, image, content_type)])
            return
        except Exception as e:
            logger.warning(f"meme API 渲染表情列表失败，降级为文本列表: {e}")
        result_text = _format_meme_list_text(updater, visible_infos)
        if (
            updater.plugin_config.meme_search_forward_enabled()
            and await try_send_forward_message(
                event, "表情列表", result_text, len(visible_infos)
            )
        ):
            return
        yield event.plain_result(result_text)
    except Exception as e:
        yield event.plain_result(f"获取表情列表失败：{e}")
    finally:
        updater._stop_event(event)


async def meme_info(updater, event: AstrMessageEvent):
    """Retrieves detail info and template of a specific meme.

    Args:
        updater: The MemeUpdater instance.
        event: The AstrMessageEvent.
    """
    if not updater._is_allowed_group(event):
        return
    updater._stop_event(event)
    query = updater._get_message_args(event, "表情详情")
    if not query:
        yield event.plain_result("用法：表情详情 <表情名/关键词>")
        return
    try:
        await updater._refresh_meme_infos()
        info = updater._find_meme(query, updater._visible_meme_infos(event))
        if not info:
            yield event.plain_result(f"未找到表情：{query}")
            return
        params = _params_type(info)
        lines = [
            f"表情：{info.get('key')}",
            f"关键词：{_format_keywords([str(v) for v in info.get('keywords', [])])}",
        ]
        shortcuts = [
            str(v.get("humanized") or v.get("key"))
            for v in info.get("shortcuts", [])
            if isinstance(v, dict)
        ]
        if shortcuts:
            lines.append(f"快捷指令：{_format_keywords(shortcuts)}")
        if params["max_images"]:
            lines.append(
                f"图片数量：{_format_range(params['min_images'], params['max_images'])}"
            )
        if params["max_texts"]:
            lines.append(
                f"文字数量：{_format_range(params['min_texts'], params['max_texts'])}"
            )
            if params["default_texts"]:
                lines.append(
                    f"默认文字：{_format_keywords([str(v) for v in params['default_texts']])}"
                )
        components = [Comp.Plain("\n".join(lines))]
        try:
            image, content_type = await updater.meme_client.get_preview(
                str(info.get("key"))
            )
            components.extend(
                [Comp.Plain("\n"), _image_component(updater, image, content_type)]
            )
        except Exception as e:
            logger.warning(f"获取表情预览失败 {info.get('key')}: {e}")
        yield event.chain_result(components)
    except Exception as e:
        yield event.plain_result(f"获取表情详情失败：{e}")


async def _yield_and_stop(updater, event: AstrMessageEvent, result):
    updater._stop_event(event)
    yield result


async def meme_generate(updater, event: AstrMessageEvent):
    """Processes user input to render a meme.

    Args:
        updater: The MemeUpdater instance.
        event: The AstrMessageEvent.
    """
    if not updater._is_allowed_group(event):
        return
    updater._stop_event(event)
    raw_args = updater._get_message_args(event, "制作表情")
    if not raw_args:
        yield event.plain_result(
            "用法：制作表情 <表情名/关键词> [文字/@自己/@QQ号/图片URL...]"
        )
        return
    try:
        parts = split_arg_string(raw_args)
        if not parts:
            yield event.plain_result(
                "用法：制作表情 <表情名/关键词> [文字/@自己/@QQ号/图片URL...]"
            )
            return
        query, rest = parts[0], parts[1:]
        await updater._refresh_meme_infos()
        info = updater._find_meme(query, updater._visible_meme_infos(event))
        if not info:
            yield event.plain_result(f"未找到表情：{query}")
            return
        params = _params_type(info)
        key = str(info.get("key"))
        rest, options = normalize_meme_options(rest)
        options = _strip_random_direction_options(key, options)
        options = materialize_direction_options(options)
        rest, louvre_options = _louvre_options_from_args(key, rest)
        options.update(louvre_options)
        images, texts, user_infos = await _resolve_generate_args(updater, event, rest)
        if (
            updater.plugin_config.meme_auto_sender_avatar()
            and len(images) < params["min_images"]
        ):
            if images:
                await _fill_sender_avatar_images(
                    updater, event, images, user_infos, params["min_images"]
                )
            else:
                await _fill_default_avatar_images(
                    updater, event, images, user_infos, params["min_images"]
                )
        if updater.plugin_config.meme_auto_default_texts() and not texts:
            texts.extend(str(v) for v in params["default_texts"])
        if not (params["min_images"] <= len(images) <= params["max_images"]):
            yield event.plain_result(
                f"图片数量不符，需要 {_format_range(params['min_images'], params['max_images'])} 张，当前 {len(images)} 张。"
            )
            return
        if not (params["min_texts"] <= len(texts) <= params["max_texts"]):
            yield event.plain_result(
                f"文字数量不符，需要 {_format_range(params['min_texts'], params['max_texts'])} 段，当前 {len(texts)} 段。"
            )
            return
        image, content_type = await updater.meme_client.render_meme(
            key, images, texts, user_infos, options
        )
        await updater.usage_stats.record(event, info)
        if await _try_send_small_image_aiocqhttp(updater, event, image):
            updater._stop_event(event)
            return
        async for result in _yield_and_stop(
            updater,
            event,
            event.chain_result([_image_component(updater, image, content_type)]),
        ):
            yield result
    except ArgSyntaxError as e:
        yield event.plain_result(str(e))
    except Exception as e:
        yield event.plain_result(f"制作表情失败：{e}")


async def _resolve_generate_args(
    updater, event: AstrMessageEvent, tokens: list[str]
) -> tuple[list[tuple[bytes, str, str]], list[str], list[dict]]:
    replied_segments = await get_replied_message_segments(updater, event)
    image_urls = extract_image_urls_from_segments(updater, replied_segments)
    image_urls.extend(extract_message_image_urls(updater, event))
    user_infos = [{} for _ in image_urls]
    avatar_urls = []
    avatar_user_infos = []
    texts = []
    mention_patterns = (
        r"^\[CQ:at,qq=\d+\]$",
        r"^\[At:\d+\]$",
        r"^@[^\s@/\(]+\(\d{5,}\)$",
        r"^@[^\s@/]+/\d{5,}$",
    )
    for arg in tokens:
        if any(re.fullmatch(pattern, arg) for pattern in mention_patterns):
            continue
        if arg in {"自己", "@自己"}:
            avatar = sender_avatar_url(event)
            if avatar:
                avatar_urls.append(avatar)
                avatar_user_infos.append(await sender_user_info(event))
            continue
        if re.fullmatch(r"https?://\S+", arg):
            image_urls.append(arg)
            user_infos.append({})
            continue
        if arg.startswith("@") and arg[1:].isdigit():
            user_id = arg[1:]
            avatar_urls.append(avatar_url(user_id))
            avatar_user_infos.append(
                {
                    "name": await lookup_sender_name(event, user_id) or user_id,
                    "gender": "unknown",
                }
            )
            continue
        texts.append(arg)
    at_ids = _extract_message_at_ids(updater, event)
    for user_id in at_ids:
        avatar_urls.append(avatar_url(user_id))
        avatar_user_infos.append(
            {
                "name": await lookup_sender_name(event, user_id) or user_id,
                "gender": "unknown",
            }
        )
    explicit_image_count = len(image_urls)
    image_urls.extend(avatar_urls)
    user_infos.extend(avatar_user_infos)
    if image_urls:
        semaphore = asyncio.Semaphore(MAX_IMAGE_DOWNLOAD_CONCURRENCY)

        async def download(url: str):
            async with semaphore:
                return await download_image(updater, url)

        download_results = await asyncio.gather(
            *(download(url) for url in image_urls), return_exceptions=True
        )
        explicit_failures = [
            result
            for result in download_results[:explicit_image_count]
            if isinstance(result, Exception)
        ]
        if explicit_failures:
            failure = explicit_failures[0]
            message = str(failure) or type(failure).__name__
            raise RuntimeError(f"引用/输入图片下载失败：{message}")
        images = [
            result for result in download_results if not isinstance(result, Exception)
        ]
        user_infos = [
            info
            for info, result in zip(user_infos, download_results)
            if not isinstance(result, Exception)
        ]
    else:
        images = []
    return list(images), texts, user_infos


def _extract_message_at_ids(updater, event: AstrMessageEvent) -> list[str]:
    from .platform_utils import extract_message_text

    user_ids = _extract_at_ids_from_segments(extract_message_segments(updater, event))
    text = extract_message_text(updater, event)
    for pattern in (
        r"\[CQ:at,qq=(\d+)\]",
        r"\[At:(\d+)\]",
        r"@[^\s@/\(]+\((\d{5,})\)",
        r"@[^\s@/]+/(\d{5,})",
        r"@(?:CQ:at,qq=)?(\d{5,})",
    ):
        for qq in re.findall(pattern, text):
            if qq not in user_ids:
                user_ids.append(qq)
    sender_id_val = sender_id(event)
    if sender_id_val and sender_id_val not in user_ids and text.startswith(("@", "＠")):
        user_ids.insert(0, sender_id_val)
    return user_ids


def _extract_at_ids_from_segments(segments: list[object]) -> list[str]:
    user_ids = []
    for segment in segments:
        if isinstance(segment, dict):
            seg_type = segment.get("type")
            data = segment.get("data") or {}
        else:
            seg_type = getattr(segment, "type", None)
            if seg_type == "reply" or hasattr(segment, "chain"):
                continue
            qq_attr = getattr(segment, "qq", None)
            if qq_attr is not None:
                seg_type = "at"
                data = {"qq": qq_attr}
            else:
                data = getattr(segment, "data", None)
        if seg_type not in {"at", "mention"} or not isinstance(data, dict):
            continue
        qq = str(
            data.get("qq")
            or data.get("user_id")
            or data.get("uid")
            or data.get("id")
            or data.get("target")
            or ""
        ).strip()
        if qq and qq != "all" and qq not in user_ids:
            user_ids.append(qq)
    return user_ids


async def _fill_sender_avatar_images(
    updater,
    event: AstrMessageEvent,
    images: list[tuple[bytes, str, str]],
    user_infos: list[dict],
    target_count: int,
):
    if (
        not updater.plugin_config.meme_auto_sender_avatar()
        or len(images) >= target_count
    ):
        return
    avatar = sender_avatar_url(event)
    if not avatar:
        return
    data, content_type, filename = await download_image(updater, avatar)
    user_info = await sender_user_info(event)
    while len(images) < target_count:
        images.insert(0, (data, content_type, filename))
        user_infos.insert(0, user_info)


async def _fill_default_avatar_images(
    updater,
    event: AstrMessageEvent,
    images: list[tuple[bytes, str, str]],
    user_infos: list[dict],
    target_count: int,
):
    if not updater.plugin_config.meme_auto_sender_avatar() or images:
        return
    sender_avatar = sender_avatar_url(event)
    bot_avatar = bot_avatar_url(event)
    fill_items = []
    if target_count >= 2 and bot_avatar:
        fill_items.append((bot_avatar, bot_user_info(event)))
    if sender_avatar:
        fill_items.append((sender_avatar, await sender_user_info(event)))
    if not fill_items:
        return
    avatar_cache: dict[str, tuple[bytes, str, str]] = {}
    fill_index = 0
    while len(images) < target_count:
        url, user_info = fill_items[fill_index % len(fill_items)]
        if url not in avatar_cache:
            avatar_cache[url] = await download_image(updater, url)
        data, content_type, filename = avatar_cache[url]
        images.append((data, content_type, filename))
        user_infos.append(user_info)
        fill_index += 1


async def _random_meme_results(
    updater, event: AstrMessageEvent, raw_args: str, resolve_args: bool = True
):
    try:
        await updater._refresh_meme_infos()
        tokens, options = normalize_meme_options(split_arg_string(raw_args))
        options = materialize_direction_options(options)
        if resolve_args:
            images, texts, user_infos = await _resolve_generate_args(
                updater, event, tokens
            )
        else:
            images, texts, user_infos = [], [], []
        auto_use = not images and not texts
        suitable = []
        for info in updater._visible_meme_infos(event).values():
            params = _params_type(info)
            image_count = params["min_images"] if auto_use else len(images)
            if params["min_images"] <= image_count <= params["max_images"] and (
                auto_use or params["min_texts"] <= len(texts) <= params["max_texts"]
            ):
                suitable.append((info, params))
        random.shuffle(suitable)
        for info, params in suitable:
            try:
                render_images = list(images)
                render_user_infos = list(user_infos)
                if auto_use:
                    await _fill_default_avatar_images(
                        updater,
                        event,
                        render_images,
                        render_user_infos,
                        params["min_images"],
                    )
                render_texts = (
                    [str(v) for v in params["default_texts"]] if auto_use else texts
                )
                image, content_type = await updater.meme_client.render_meme(
                    str(info.get("key")),
                    render_images,
                    render_texts,
                    render_user_infos,
                    _strip_random_direction_options(str(info.get("key")), options),
                )
                await updater.usage_stats.record(event, info)
                keywords = _format_keywords([str(v) for v in info.get("keywords", [])])
                if await _try_send_small_image_aiocqhttp(
                    updater, event, image, text=f"关键词：{keywords}\n"
                ):
                    updater._stop_event(event)
                    return
                async for result in _yield_and_stop(
                    updater,
                    event,
                    event.chain_result(
                        [
                            Comp.Plain(f"关键词：{keywords}\n"),
                            _image_component(updater, image, content_type),
                        ]
                    ),
                ):
                    yield result
                return
            except Exception as e:
                logger.debug(f"随机表情渲染跳过 {info.get('key')}: {e}")
                continue
        yield event.plain_result("没有找到适合当前参数的表情。")
    except Exception as e:
        yield event.plain_result(f"随机表情失败：{e}")


async def meme_poke_random_listener(updater, event: AstrMessageEvent):
    """Quart / event listener for poke random meme.

    Args:
        updater: The MemeUpdater instance.
        event: The AstrMessageEvent.
    """
    if (
        not updater.plugin_config.meme_poke_random_enabled()
        or not updater._is_allowed_group(event)
    ):
        return
    async for result in _random_meme_results(updater, event, "", resolve_args=False):
        yield result


async def meme_shortcut_listener(updater, event: AstrMessageEvent):
    """Processes shortcut triggers matching specific patterns to render a meme.

    Args:
        updater: The MemeUpdater instance.
        event: The AstrMessageEvent.
    """
    from .platform_utils import extract_message_text, stop_event

    if not updater._is_allowed_group(event):
        return
    content = extract_message_text(updater, event)
    if content in {"随机表情", "随机meme", "随机 meme", "来个表情", "来张表情"}:
        async for result in _random_meme_results(updater, event, ""):
            yield result
        stop_event(event)
        return

    if not updater.plugin_config.meme_shortcut_enabled():
        return

    if not content or content.startswith(("/", "#", "%", "％")):
        return
    if not updater.meme_infos:
        updater._ensure_meme_info_refresh_task()
        return
    if not updater.meme_shortcuts:
        updater._refresh_meme_shortcuts()
    visible_infos = updater._visible_meme_infos(event)
    candidates = updater._shortcut_index.get(content[0], [])
    if updater._shortcut_wildcards:
        candidates = sorted(
            [*candidates, *updater._shortcut_wildcards],
            key=lambda item: len(item["regex"]),
            reverse=True,
        )
    try:
        for shortcut in candidates:
            match = shortcut["compiled_regex"].match(content)
            if not match:
                continue
            tail = content[match.end() :].strip()
            resolved_args = " ".join(
                value
                for value in [_shortcut_args(shortcut["args"], match), tail]
                if value
            ).strip()
            info = visible_infos.get(shortcut["key"])
            if not info:
                continue
            params = _params_type(info)
            key = str(info.get("key"))
            resolved_tokens, options = normalize_meme_options(
                split_arg_string(resolved_args)
            )
            options = {**shortcut.get("options", {}), **options}
            content_options = direction_options_from_text(key, content)
            if content_options:
                for d in [
                    "left",
                    "right",
                    "top",
                    "bottom",
                    "direction",
                    "__direction",
                ]:
                    options.pop(d, None)
                options.update(content_options)
            options = _strip_random_direction_options(key, options)
            options = materialize_direction_options(options)
            resolved_tokens, louvre_options = _louvre_options_from_args(
                key, resolved_tokens
            )
            options.update(louvre_options)
            images, texts, user_infos = await _resolve_generate_args(
                updater, event, resolved_tokens
            )

            if (
                params["max_images"] == 2
                and str(info.get("key")) != MIRAGETANK_KEY
                and len(images) >= 3
            ):
                images = [images[0], images[-1]]
                user_infos = [user_infos[0], user_infos[-1]]
            elif len(images) > params["max_images"]:
                images, user_infos = _select_render_images(
                    images, user_infos, params["max_images"]
                )

            if (
                updater.plugin_config.meme_auto_sender_avatar()
                and len(images) < params["min_images"]
            ):
                if images:
                    await _fill_sender_avatar_images(
                        updater, event, images, user_infos, params["min_images"]
                    )
                else:
                    await _fill_default_avatar_images(
                        updater, event, images, user_infos, params["min_images"]
                    )

            if updater.plugin_config.meme_auto_default_texts() and not texts:
                texts.extend(str(v) for v in params["default_texts"])

            if not (params["min_images"] <= len(images) <= params["max_images"]):
                continue
            if not (params["min_texts"] <= len(texts) <= params["max_texts"]):
                continue

            try:
                image, content_type = await updater.meme_client.render_meme(
                    key, images, texts, user_infos, options
                )
            except Exception as e:
                logger.debug(f"快捷指令渲染跳过 {key}: {e}")
                continue
            await updater.usage_stats.record(event, info)
            if await _try_send_small_image_aiocqhttp(updater, event, image):
                stop_event(event)
                return
            yield event.chain_result([_image_component(updater, image, content_type)])
            stop_event(event)
            return
    except Exception as e:
        logger.warning(f"处理表情快捷指令异常: {e}")
        return
