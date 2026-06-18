import asyncio
import os
import re
import time
from datetime import datetime

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType, PermissionType
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.config import AstrBotConfig as CoreAstrBotConfig
from astrbot.core.star.filter.custom_filter import CustomFilter

from .src.disabled_memes import DisabledMemeManager
from .src.image_renderer import MemeImageRenderer
from .src.meme_client import MemeApiClient
from .src.plugin_config import MemePluginConfig
from .src.repo_manager import MemeRepoManager
from .src.usage_stats import MemeUsageStats

TEMP_IMAGE_TTL_SECONDS = 3600
TEMP_IMAGE_CLEANUP_INTERVAL_SECONDS = 300
MEME_API_RESTART_REFRESH_ATTEMPTS = 6
MEME_API_RESTART_REFRESH_INTERVAL_SECONDS = 5
MIRAGETANK_KEY = "miragetank"
LOUVRE_KEY = "louvre"
QQ_AVATAR_URL_TEMPLATE = "https://q4.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=640"
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
PLUGIN_NAME = "meme_updater"

QUOTE_PAIRS = {
    '"': '"',
    "'": "'",
    "`": "`",
    "“": "”",
    "‘": "’",
}


class PokeToBotFilter(CustomFilter):
    """Filter that matches events where the user double-taps/pokes the bot."""

    def filter(self, event: AstrMessageEvent, cfg: CoreAstrBotConfig) -> bool:
        from .src.platform_utils import is_poke_to_bot_event

        return is_poke_to_bot_event(event)


@register(
    "astrbot_plugin_meme_api_python",
    "表情包数据更新与生成插件",
    "xiaoruange39",
    "0.2.7",
)
class MemeUpdater(Star):
    """The main plugin controller for managing and rendering meme packages."""

    def __init__(self, context: Context, config: AstrBotConfig):
        """Initializes the MemeUpdater plugin instance.

        Args:
            context: The AstrBot Context instance.
            config: The plugin AstrBotConfig instance.
        """
        super().__init__(context)
        self.config = config
        self._meme_infos: dict[str, dict] = {}
        self._meme_shortcuts: list[dict] = []
        self._shortcut_index: dict[str, list[dict]] = {}
        self._shortcut_wildcards: list[dict] = []
        self._meme_info_lock = asyncio.Lock()
        self._meme_info_refresh_task: asyncio.Task | None = None
        self._meme_data_dir = str(StarTools.get_data_dir() / "memeapi")
        self._last_temp_cleanup = 0.0
        self._temp_cleanup_task: asyncio.Task | None = None
        self._download_session: aiohttp.ClientSession | None = None
        self.plugin_config = MemePluginConfig(config, self._meme_data_dir)
        self.repo_manager = MemeRepoManager(self.plugin_config, self._meme_data_dir)
        self.meme_client = MemeApiClient(
            self.plugin_config.meme_api_base_url,
            self.plugin_config.meme_request_timeout,
            self.plugin_config.max_image_bytes,
            self.plugin_config.meme_info_concurrency,
            self.plugin_config.meme_refresh_verbose_log,
        )
        self.disabled_memes = DisabledMemeManager(config, self.plugin_config)
        self.usage_stats = MemeUsageStats(
            config,
            str(StarTools.get_data_dir() / "meme_usage.json"),
            lambda: self.meme_infos,
            self.disabled_memes.meme_display_name,
            self._safe_int,
            self._group_id,
            self._group_name_from_event,
            self._lookup_group_name,
        )
        self.image_renderer = MemeImageRenderer(
            self.usage_stats, self.disabled_memes.remove_emoji
        )
        self.usage_stats.register_web_apis(context, "astrbot_plugin_meme_api_python")
        context.register_web_api(
            "/astrbot_plugin_meme_api_python/disabled-memes",
            self.web_get_disabled_memes,
            ["GET"],
            "获取屏蔽表情列表",
        )
        context.register_web_api(
            "/astrbot_plugin_meme_api_python/disabled-memes/add",
            self.web_add_disabled_meme,
            ["POST"],
            "添加屏蔽表情",
        )
        context.register_web_api(
            "/astrbot_plugin_meme_api_python/disabled-memes/delete",
            self.web_delete_disabled_meme,
            ["POST", "DELETE"],
            "删除屏蔽表情",
        )

    # Property Getters / Setters

    @property
    def meme_infos(self) -> dict[str, dict]:
        return self._meme_infos

    @meme_infos.setter
    def meme_infos(self, value: dict[str, dict]):
        self._meme_infos = value

    @property
    def meme_shortcuts(self) -> list[dict]:
        return self._meme_shortcuts

    @meme_shortcuts.setter
    def meme_shortcuts(self, value: list[dict]):
        self._meme_shortcuts = value

    # Lifecycle Methods

    async def initialize(self):
        """Asynchronously initializes the plugin resources on load."""
        self._ensure_meme_info_refresh_task()
        if self._temp_cleanup_task is None or self._temp_cleanup_task.done():
            self._temp_cleanup_task = asyncio.create_task(self._temp_cleanup_loop())
            self._temp_cleanup_task.add_done_callback(self._log_background_task_result)

    async def terminate(self):
        """Asynchronously cleans up the plugin resources on unload."""
        if self._temp_cleanup_task:
            self._temp_cleanup_task.cancel()
            try:
                await self._temp_cleanup_task
            except asyncio.CancelledError:
                pass
            self._temp_cleanup_task = None
        if self._meme_info_refresh_task:
            self._meme_info_refresh_task.cancel()
            try:
                await self._meme_info_refresh_task
            except asyncio.CancelledError:
                pass
            self._meme_info_refresh_task = None
        if self._download_session and not self._download_session.closed:
            try:
                await self._download_session.close()
            except Exception as e:
                logger.debug(f"关闭图片下载 session 失败：{e}")
        self._download_session = None
        if self.meme_client:
            try:
                await self.meme_client.close()
            except Exception as e:
                logger.debug(f"关闭 meme_client session 失败：{e}")
        parent_terminate = getattr(super(), "terminate", None)
        if parent_terminate:
            result = parent_terminate()
            if asyncio.iscoroutine(result):
                await result

    # State Cache refreshing

    async def _refresh_meme_infos(self, force: bool = False) -> int:
        """Refreshes the internal meme metadata info cache from the memeapi backend.

        Args:
            force: If True, forces refresh even if cached data already exists.

        Returns:
            The count of cached memes.
        """
        if not force and self._meme_infos:
            return len(self._meme_infos)
        async with self._meme_info_lock:
            if not force and self._meme_infos:
                return len(self._meme_infos)
            try:
                infos = await self.meme_client.fetch_meme_infos()
                self.meme_infos = infos
                self._refresh_meme_shortcuts()
                return len(infos)
            except Exception as e:
                logger.error(f"加载 meme API 表情信息失败: {e}")
                raise

    async def _refresh_meme_infos_after_restart(self, lines: list[str]) -> None:
        """Refreshes meme info after a restart with retries.

        Args:
            lines: Log summary line builder array.
        """
        attempts = MEME_API_RESTART_REFRESH_ATTEMPTS
        for attempt in range(attempts):
            try:
                count = await self._refresh_meme_infos(force=True)
                lines.append(f"✅ 表情信息刷新完成，共载入 {count} 个表情。")
                return
            except Exception as e:
                if attempt == attempts - 1:
                    lines.append(f"❌ 刷新表情信息失败，已达最大尝试次数：{e}")
                else:
                    lines.append(
                        f"⏳ 刷新表情信息失败，等待重试 ({attempt + 1}/{attempts})..."
                    )
                    await asyncio.sleep(MEME_API_RESTART_REFRESH_INTERVAL_SECONDS)

    def _ensure_meme_info_refresh_task(self) -> asyncio.Task | None:
        """Ensures the background refresh task is running if info is not yet loaded.

        Returns:
            The refresh Task instance, or None.
        """
        if self._meme_infos:
            return None
        if self._meme_info_refresh_task and not self._meme_info_refresh_task.done():
            return self._meme_info_refresh_task
        task = asyncio.create_task(self._refresh_meme_infos())
        task.add_done_callback(self._log_background_task_result)
        self._meme_info_refresh_task = task
        return task

    def _log_background_task_result(self, task: asyncio.Task) -> None:
        """Logs exceptions if the background refresh task fails.

        Args:
            task: The completed task.
        """
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"后台任务执行失败: {e}")

    async def _temp_cleanup_loop(self):
        """Background loop cleaning up expired generated images periodically."""
        temp_dir = os.path.join(self._meme_data_dir, "temp")
        os.makedirs(temp_dir, exist_ok=True)
        while True:
            try:
                now = time.time()
                from .src.commands import _cleanup_temp_images

                _cleanup_temp_images(temp_dir, now)
                self._last_temp_cleanup = now
            except Exception as e:
                logger.error(f"清理临时图片后台任务异常: {e}")

            try:
                await asyncio.sleep(TEMP_IMAGE_CLEANUP_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                break

    def _ensure_download_session(self) -> aiohttp.ClientSession:
        if self._download_session is None or self._download_session.closed:
            self._download_session = aiohttp.ClientSession()
        return self._download_session

    # Web APIs

    async def web_get_disabled_memes(self):
        from .src.web_api import web_get_disabled_memes

        return await web_get_disabled_memes(self)

    async def web_add_disabled_meme(self):
        from .src.web_api import web_add_disabled_meme

        return await web_add_disabled_meme(self)

    async def web_delete_disabled_meme(self):
        from .src.web_api import web_delete_disabled_meme

        return await web_delete_disabled_meme(self)

    # Command implementation stubs

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("重启memeapi")
    async def restart_memeapi(self, event: AstrMessageEvent):
        from .src.commands import restart_memeapi

        async for res in restart_memeapi(self, event):
            yield res

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("更新表情包")
    async def update_memes(self, event: AstrMessageEvent):
        from .src.commands import update_memes

        async for res in update_memes(self, event):
            yield res

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("表情包状态")
    async def meme_status(self, event: AstrMessageEvent):
        from .src.commands import meme_status

        async for res in meme_status(self, event):
            yield res

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("刷新表情信息")
    async def refresh_meme_infos(self, event: AstrMessageEvent):
        from .src.commands import refresh_meme_infos

        async for res in refresh_meme_infos(self, event):
            yield res

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("屏蔽表情")
    async def disable_meme(self, event: AstrMessageEvent):
        from .src.commands import set_meme_disabled

        async for res in set_meme_disabled(self, event, "屏蔽表情"):
            yield res

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("取消屏蔽表情")
    async def enable_meme(self, event: AstrMessageEvent):
        from .src.commands import unset_meme_disabled

        async for res in unset_meme_disabled(self, event, "取消屏蔽表情"):
            yield res

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("全局屏蔽表情")
    async def disable_meme_globally(self, event: AstrMessageEvent):
        from .src.commands import set_meme_disabled

        async for res in set_meme_disabled(
            self, event, "全局屏蔽表情", force_global=True
        ):
            yield res

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("取消全局屏蔽表情")
    async def enable_meme_globally(self, event: AstrMessageEvent):
        from .src.commands import unset_meme_disabled

        async for res in unset_meme_disabled(
            self, event, "取消全局屏蔽表情", force_global=True
        ):
            yield res

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("屏蔽表情列表")
    async def list_disabled_memes(self, event: AstrMessageEvent):
        from .src.commands import list_disabled_memes

        args = self._get_message_args(event, "屏蔽表情列表")
        group_id = self._block_list_scope_from_args(event, args)
        async for res in list_disabled_memes(self, event, group_id):
            yield res

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("全局屏蔽表情列表")
    async def list_global_disabled_memes(self, event: AstrMessageEvent):
        from .src.commands import list_disabled_memes

        async for res in list_disabled_memes(self, event, ""):
            yield res

    @filter.command("表情统计")
    async def meme_usage_stats(self, event: AstrMessageEvent):
        from .src.commands import meme_usage_stats

        async for res in meme_usage_stats(self, event):
            yield res

    @filter.command("总表情统计")
    async def meme_global_usage_stats(self, event: AstrMessageEvent):
        from .src.commands import meme_global_usage_stats

        async for res in meme_global_usage_stats(self, event):
            yield res

    @filter.command("meme搜索")
    async def meme_search(self, event: AstrMessageEvent):
        from .src.commands import meme_search

        async for res in meme_search(self, event):
            yield res

    @filter.command("表情列表")
    async def meme_list(self, event: AstrMessageEvent):
        from .src.commands import meme_list

        async for res in meme_list(self, event):
            yield res

    @filter.command("表情详情")
    async def meme_info(self, event: AstrMessageEvent):
        from .src.commands import meme_info

        async for res in meme_info(self, event):
            yield res

    @filter.command("制作表情")
    async def meme_generate(self, event: AstrMessageEvent):
        from .src.commands import meme_generate

        async for res in meme_generate(self, event):
            yield res

    @filter.custom_filter(PokeToBotFilter)
    async def meme_poke_random_listener(self, event: AstrMessageEvent):
        from .src.commands import meme_poke_random_listener

        async for res in meme_poke_random_listener(self, event):
            yield res

    @filter.event_message_type(EventMessageType.ALL)
    async def meme_shortcut_listener(self, event: AstrMessageEvent):
        from .src.commands import meme_shortcut_listener

        async for res in meme_shortcut_listener(self, event):
            yield res

    # Metadata & Config Helpers

    def _is_allowed_group(self, event: AstrMessageEvent) -> bool:
        whitelist = self.plugin_config.meme_group_whitelist()
        if not whitelist:
            return True
        group_id = self._group_id(event)
        if group_id and group_id not in whitelist:
            return False
        return True

    def _format_time(self, dt: datetime) -> str:
        return f"{dt.year}/{dt.month}/{dt.day} {dt:%H:%M:%S}"

    def _regex_first_literal(self, regex: str) -> str | None:
        if not regex:
            return None
        if regex[0] == "\\":
            return regex[1] if len(regex) > 1 else None
        if regex[0] in ".^$*+?()[]{}|":
            return None
        return regex[0]

    def _is_dangerous_regex(self, regex: str) -> bool:
        return bool(
            re.search(r"\([^()]*[+*][^()]*\)\s*[+*]", regex)
            or re.search(r"\([^()]*[+*][^()]*\)\{", regex)
        )

    def _shortcut_entry(
        self, key: str, regex: str, args: list, options: dict
    ) -> dict | None:
        if len(regex) > 120:
            logger.debug(f"快捷正则过长，已跳过: {key}")
            return None
        if self._is_dangerous_regex(regex):
            logger.debug(f"快捷正则存在回溯风险，已跳过: {key} - {regex}")
            return None
        try:
            return {
                "key": key,
                "regex": regex,
                "compiled_regex": re.compile(f"^{regex}"),
                "args": args,
                "options": options,
                "first_char": self._regex_first_literal(regex),
            }
        except re.error as e:
            logger.debug(f"快捷正则编译跳过: {key} - {e}")
            return None

    def _refresh_meme_shortcuts(self):
        shortcuts = []
        for key, info in self.meme_infos.items():
            for keyword in info.get("keywords", []):
                entry = self._shortcut_entry(key, re.escape(str(keyword)), [], {})
                if entry:
                    shortcuts.append(entry)
            for shortcut in info.get("shortcuts", []):
                if not isinstance(shortcut, dict):
                    continue
                shortcut_key = str(
                    shortcut.get("humanized") or shortcut.get("key") or ""
                ).strip()
                if not shortcut_key:
                    continue
                regex = re.sub(r"\(\?P<(\w+)>", r"(?P<\1>", shortcut_key.strip("^$"))
                args = shortcut.get("args") or []
                options = {}
                compact_key = shortcut_key.replace(" ", "").replace("#", "")
                if key == "symmetry":
                    options.update(self._direction_options_from_text(key, compact_key))
                entry = self._shortcut_entry(key, regex, args, options)
                if entry:
                    shortcuts.append(entry)
                if "#" in shortcut_key:
                    entry = self._shortcut_entry(
                        key, regex.replace("#", ""), args, options
                    )
                    if entry:
                        shortcuts.append(entry)
        shortcuts.sort(key=lambda item: len(item["regex"]), reverse=True)
        self.meme_shortcuts = shortcuts
        index: dict[str, list[dict]] = {}
        wildcards: list[dict] = []
        for entry in shortcuts:
            first_char = entry.get("first_char")
            if first_char:
                index.setdefault(first_char, []).append(entry)
            else:
                wildcards.append(entry)
        self._shortcut_index = index
        self._shortcut_wildcards = wildcards

    def _disabled_names_for_event(self, event: AstrMessageEvent) -> set[str]:
        return self.plugin_config.disabled_meme_names_for_group(self._group_id(event))

    def _visible_meme_infos(self, event: AstrMessageEvent) -> dict[str, dict]:
        disabled_names = self._disabled_names_for_event(event)
        if not disabled_names:
            return self.meme_infos
        return {
            key: info
            for key, info in self.meme_infos.items()
            if not self.disabled_memes.is_meme_disabled(key, info, disabled_names)
        }

    def _find_meme(
        self, query: str, meme_infos: dict[str, dict] | None = None
    ) -> dict | None:
        return self.disabled_memes.find_meme_in_infos(
            query, meme_infos or self.meme_infos
        )

    def _meme_search_text(self, info: dict) -> str:
        values = [str(info.get("key", ""))]
        for field in ("keywords", "tags"):
            values.extend(str(value) for value in info.get(field, []))
        for shortcut in info.get("shortcuts", []):
            if isinstance(shortcut, dict):
                values.append(
                    str(shortcut.get("humanized") or shortcut.get("key") or "")
                )
        return " ".join(values).lower()

    def _search_memes(
        self,
        query: str,
        meme_infos: dict[str, dict] | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        if limit is None:
            limit = self.plugin_config.meme_search_limit()
        lowered = query.strip().lower()
        if not lowered:
            return []
        exact_matches = []
        fuzzy_matches = []
        for info in self._sorted_meme_infos(meme_infos):
            key = str(info.get("key", ""))
            keywords = [str(value) for value in info.get("keywords", [])]
            candidates = [key, *keywords]
            if any(value.lower() == lowered for value in candidates):
                exact_matches.append(info)
            elif lowered in self._meme_search_text(info):
                fuzzy_matches.append(info)
        return [*exact_matches, *fuzzy_matches][:limit]

    def _format_meme_search_result(self, index: int, info: dict) -> str:
        return f"{index}. {self.disabled_memes.meme_display_name(info)}"

    def _block_scope_from_args(
        self, event: AstrMessageEvent, args: str
    ) -> tuple[str, str]:
        from .src.arg_parser import split_arg_string

        parts = split_arg_string(args)
        if len(parts) >= 2 and re.fullmatch(r"\d{5,20}", parts[0]):
            return parts[0], " ".join(parts[1:])
        return self._group_id(event), args

    def _block_list_scope_from_args(self, event: AstrMessageEvent, args: str) -> str:
        args = args.strip()
        if args and re.fullmatch(r"\d{5,20}", args):
            return args
        return self._group_id(event)

    async def _block_scope_title(
        self, event: AstrMessageEvent, group_id: str
    ) -> tuple[str, str]:
        if not group_id:
            return "全局屏蔽表情列表", "全局"
        event_group_id = self._group_id(event)
        group_name = (
            self._group_name_from_event(event, group_id)
            if group_id == event_group_id
            else ""
        )
        group_name = group_name or await self._lookup_group_name(event, group_id)
        title = (
            f"屏蔽表情列表 - {group_name}（{group_id}）"
            if group_name
            else f"屏蔽表情列表 - 群 {group_id}"
        )
        return title, "当前群" if group_id == event_group_id else f"群 {group_id}"

    async def _block_scope_name(self, event: AstrMessageEvent, group_id: str) -> str:
        if not group_id:
            return "全局"
        event_group_id = self._group_id(event)
        if group_id == event_group_id:
            return "本群"
        group_name = await self._lookup_group_name(event, group_id)
        return f"群 {group_name}（{group_id}）" if group_name else f"群 {group_id}"

    def _direction_options_from_text(self, key: str, text: str) -> dict[str, object]:
        from .src.arg_parser import direction_options_from_text

        return direction_options_from_text(key, text)

    def _sorted_meme_infos(
        self, meme_infos: dict[str, dict] | None = None
    ) -> list[dict]:
        from .src.commands import _sorted_meme_infos

        return _sorted_meme_infos(self, meme_infos)

    def _safe_int(self, value: object, default: int = 0) -> int:
        from .src.commands import _safe_int

        return _safe_int(value, default)

    # Delegated Platform & Event Utilities

    def _group_id(self, event: AstrMessageEvent) -> str:
        from .src.platform_utils import group_id

        return group_id(event)

    def _sender_id(self, event: AstrMessageEvent) -> str:
        from .src.platform_utils import sender_id

        return sender_id(event)

    def _bot_id(self, event: AstrMessageEvent) -> str:
        from .src.platform_utils import bot_id

        return bot_id(event)

    def _avatar_url(self, user_id: str) -> str:
        from .src.platform_utils import avatar_url

        return avatar_url(user_id)

    def _sender_avatar_url(self, event: AstrMessageEvent) -> str:
        from .src.platform_utils import sender_avatar_url

        return sender_avatar_url(event)

    def _bot_avatar_url(self, event: AstrMessageEvent) -> str:
        from .src.platform_utils import bot_avatar_url

        return bot_avatar_url(event)

    def _group_name_from_event(self, event: AstrMessageEvent, group_id_val: str) -> str:
        from .src.platform_utils import group_name_from_event

        return group_name_from_event(event, group_id_val)

    async def _lookup_group_name(
        self, event: AstrMessageEvent | None, group_id_val: str
    ) -> str:
        from .src.platform_utils import lookup_group_name

        return await lookup_group_name(event, group_id_val)

    async def _lookup_sender_name(self, event: AstrMessageEvent, user_id: str) -> str:
        from .src.platform_utils import lookup_sender_name

        return await lookup_sender_name(event, user_id)

    def _extract_message_text(self, event: AstrMessageEvent) -> str:
        from .src.platform_utils import extract_message_text

        return extract_message_text(self, event)

    def _stop_event(self, event: AstrMessageEvent) -> None:
        from .src.platform_utils import stop_event

        stop_event(event)

    def _get_message_args(self, event: AstrMessageEvent, command_name: str) -> str:
        from .src.platform_utils import get_message_args

        return get_message_args(self, event, command_name)
