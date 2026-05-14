import os
import re
import base64
import random
import socket
import asyncio
import tempfile
import io
import time
import ipaddress
import mimetypes
import platform
import threading
from datetime import datetime
from urllib.parse import quote, urlparse

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.core.config import AstrBotConfig as CoreAstrBotConfig
from astrbot.core.star.filter.custom_filter import CustomFilter
import astrbot.api.message_components as Comp
from .usage_stats import MemeUsageStats
from .meme_client import MemeApiClient
from .plugin_config import MemePluginConfig
from .repo_manager import MemeRepoManager
try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:
    Image = ImageDraw = ImageFont = None

TEMP_IMAGE_TTL_SECONDS = 3600
TEMP_IMAGE_CLEANUP_INTERVAL_SECONDS = 300
MAX_IMAGE_DOWNLOAD_CONCURRENCY = 4
SHORTCUT_INITIALIZE_WAIT_SECONDS = 10
MEME_API_RESTART_REFRESH_ATTEMPTS = 6
MEME_API_RESTART_REFRESH_INTERVAL_SECONDS = 5
MIRAGETANK_KEY = "miragetank"
PLUGIN_NAME = "meme_updater"

QUOTE_PAIRS = {
    '"': '"',
    "'": "'",
    "`": "`",
    "“": "”",
    "‘": "’",
}


def _format_range(min_value: int, max_value: int) -> str:
    return str(min_value) if min_value == max_value else f"{min_value} ~ {max_value}"


def _format_keywords(keywords: list[str]) -> str:
    return "、".join(f"“{keyword}”" for keyword in keywords)

class ArgSyntaxError(SyntaxError):
    pass


def split_arg_string(arg_string: str) -> list[str]:
    args = []
    current = []
    in_quote = None
    out_quote = None
    escape_next = False

    for char in arg_string:
        if escape_next:
            current.append(char)
            escape_next = False
            continue
        if char == "\\":
            escape_next = True
            continue
        if in_quote:
            if char == out_quote:
                in_quote = None
                out_quote = None
            else:
                current.append(char)
            continue
        if char in QUOTE_PAIRS:
            in_quote = char
            out_quote = QUOTE_PAIRS[char]
        elif char.isspace():
            if current:
                args.append("".join(current))
                current.clear()
        else:
            current.append(char)

    if escape_next:
        raise ArgSyntaxError("参数转义符不能位于末尾")
    if current:
        args.append("".join(current))
    if in_quote:
        raise ArgSyntaxError(f"参数引号未闭合：{in_quote}")
    return args


class PokeToBotFilter(CustomFilter):
    def filter(self, event: AstrMessageEvent, cfg: CoreAstrBotConfig) -> bool:
        return MemeUpdater._is_poke_to_bot_event(event)


@register("astrbot_plugin_meme_api_python", "表情包数据更新与生成插件", "xiaoruange39", "0.1.8")
class MemeUpdater(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._meme_infos: dict[str, dict] = {}
        self._meme_shortcuts: list[dict] = []
        self._meme_info_lock = asyncio.Lock()
        self._meme_info_refresh_task: asyncio.Task | None = None
        self._usage_font_candidates: list[tuple[int, str]] | None = None
        self._usage_font_cache = {}
        self._usage_font_lock = threading.Lock()
        self._meme_data_dir = str(StarTools.get_data_dir() / "memeapi")
        self._last_temp_cleanup = 0.0
        self.plugin_config = MemePluginConfig(config, self._meme_data_dir)
        self.repo_manager = MemeRepoManager(self.plugin_config, self._meme_data_dir)
        self.meme_client = MemeApiClient(
            self.plugin_config.meme_api_base_url,
            self.plugin_config.meme_request_timeout,
            self.plugin_config.max_image_bytes,
            self.plugin_config.meme_info_concurrency,
            self.plugin_config.meme_refresh_verbose_log,
        )
        self.usage_stats = MemeUsageStats(
            config,
            str(StarTools.get_data_dir() / "meme_usage.json"),
            lambda: self.meme_infos,
            self._meme_display_name,
            self._safe_int,
            self._group_id,
            self._group_name_from_event,
            self._lookup_group_name,
        )
        self.usage_stats.register_web_apis(context, "astrbot_plugin_meme_api_python")

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

    def _is_meme_disabled(self, key: str, info: dict, disabled_names: set[str]) -> bool:
        if not disabled_names:
            return False
        if key in disabled_names:
            return True
        return any(str(keyword).strip() in disabled_names for keyword in info.get("keywords", []))

    def _remote_mode_warning(self) -> str:
        return "⚠️ 远程服务器模式（实验性）"

    def _format_time(self, dt: datetime) -> str:
        return f"{dt.year}/{dt.month}/{dt.day} {dt:%H:%M:%S}"

    async def _refresh_meme_infos(self, force: bool = False) -> int:
        async with self._meme_info_lock:
            if self.meme_infos and not force:
                return len(self.meme_infos)
            meme_infos = await self.meme_client.fetch_meme_infos()
            entries = list(meme_infos.items())
            disabled_names = self.plugin_config.disabled_meme_names()
            if disabled_names:
                entries = [(key, info) for key, info in entries if not self._is_meme_disabled(key, info, disabled_names)]
            self.meme_infos = dict(entries)
            self._refresh_meme_shortcuts()
            logger.info(f"meme API 表情信息刷新完成，共载入 {len(self.meme_infos)} 个表情")
            return len(self.meme_infos)

    async def _refresh_meme_infos_after_restart(self, lines: list[str]) -> None:
        for attempt in range(1, MEME_API_RESTART_REFRESH_ATTEMPTS + 1):
            if attempt > 1:
                await asyncio.sleep(MEME_API_RESTART_REFRESH_INTERVAL_SECONDS)
            try:
                count = await self._refresh_meme_infos(force=True)
                lines.append(f"✅ 已刷新表情信息，共载入 {count} 个表情")
                return
            except Exception as e:
                if attempt == MEME_API_RESTART_REFRESH_ATTEMPTS:
                    lines.append(f"⚠️ 刷新表情信息失败：{e}")
                else:
                    retry_line = f"⏳ meme API 尚未就绪，{MEME_API_RESTART_REFRESH_INTERVAL_SECONDS} 秒后重试刷新表情信息（外层第 {attempt}/{MEME_API_RESTART_REFRESH_ATTEMPTS} 次）"
                    logger.warning(f"重启后刷新表情信息失败，准备重试外层第 {attempt + 1}/{MEME_API_RESTART_REFRESH_ATTEMPTS} 次：{e}")
                    lines.append(retry_line)

    def _ensure_meme_info_refresh_task(self) -> asyncio.Task | None:
        if self.meme_infos:
            return None
        task = self._meme_info_refresh_task
        if task and not task.done():
            return task
        if task and task.done():
            try:
                task.result()
            except Exception as e:
                logger.warning(f"后台刷新 meme API 表情信息失败: {e}")
        self._meme_info_refresh_task = asyncio.create_task(self._refresh_meme_infos())
        return self._meme_info_refresh_task

    async def _wait_meme_info_refresh_for_shortcut(self) -> bool:
        if self.meme_infos:
            return True
        task = self._ensure_meme_info_refresh_task()
        if not task:
            return bool(self.meme_infos)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=SHORTCUT_INITIALIZE_WAIT_SECONDS)
        except asyncio.TimeoutError:
            return False
        except Exception as e:
            logger.warning(f"快捷指令等待 meme API 表情信息初始化失败: {e}")
            return False
        return bool(self.meme_infos)

    async def terminate(self):
        task = self._meme_info_refresh_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._meme_info_refresh_task = None
        parent_terminate = getattr(super(), "terminate", None)
        if parent_terminate:
            result = parent_terminate()
            if asyncio.iscoroutine(result):
                await result

    def _shortcut_entry(self, key: str, regex: str, args: list, options: dict) -> dict | None:
        if len(regex) > 120:
            logger.debug(f"快捷正则过长，已跳过: {key}")
            return None
        try:
            return {"key": key, "regex": regex, "compiled_regex": re.compile(f"^{regex}"), "args": args, "options": options}
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
                shortcut_key = str(shortcut.get("humanized") or shortcut.get("key") or "").strip()
                if not shortcut_key:
                    continue
                regex = re.sub(r"\(\?P<(\w+)>", r"(?P<\1>", shortcut_key.strip("^$"))
                args = shortcut.get("args") or []
                options = {}
                compact_key = shortcut_key.replace(" ", "").replace("#", "")
                if key in {"turn", "symmetry"}:
                    options.update(self._direction_options_from_text(key, compact_key))
                entry = self._shortcut_entry(key, regex, args, options)
                if entry:
                    shortcuts.append(entry)
                if "#" in shortcut_key:
                    entry = self._shortcut_entry(key, regex.replace("#", ""), args, options)
                    if entry:
                        shortcuts.append(entry)
        shortcuts.sort(key=lambda item: len(item["regex"]), reverse=True)
        self.meme_shortcuts = shortcuts

    def _find_meme(self, query: str) -> dict | None:
        query = query.strip()
        if query in self.meme_infos:
            return self.meme_infos[query]
        lowered = query.lower()
        for info in self.meme_infos.values():
            for field in ("keywords", "tags"):
                if any(str(value).lower() == lowered for value in info.get(field, [])):
                    return info
            for shortcut in info.get("shortcuts", []):
                if not isinstance(shortcut, dict):
                    continue
                value = shortcut.get("humanized") or shortcut.get("key")
                if str(value).lower() == lowered:
                    return info
        return None

    def _meme_search_text(self, info: dict) -> str:
        values = [str(info.get("key", ""))]
        for field in ("keywords", "tags"):
            values.extend(str(value) for value in info.get(field, []))
        for shortcut in info.get("shortcuts", []):
            if isinstance(shortcut, dict):
                values.append(str(shortcut.get("humanized") or shortcut.get("key") or ""))
        return " ".join(values).lower()

    def _search_memes(self, query: str, limit: int | None = None) -> list[dict]:
        limit = limit or self.plugin_config.meme_search_limit()
        lowered = query.strip().lower()
        if not lowered:
            return []
        exact_matches = []
        fuzzy_matches = []
        for info in self._sorted_meme_infos():
            key = str(info.get("key", ""))
            keywords = [str(value) for value in info.get("keywords", [])]
            candidates = [key, *keywords]
            if any(value.lower() == lowered for value in candidates):
                exact_matches.append(info)
            elif lowered in self._meme_search_text(info):
                fuzzy_matches.append(info)
        return [*exact_matches, *fuzzy_matches][:limit]

    def _meme_display_name(self, info: dict) -> str:
        keywords = [str(value) for value in info.get("keywords", []) if str(value).strip()]
        return keywords[0] if keywords else str(info.get("key", ""))

    def _format_meme_search_result(self, index: int, info: dict) -> str:
        return f"{index}. {self._meme_display_name(info)}"

    def _font_supports_usage_text(self, font) -> bool:
        try:
            for char in "表情调用统计次数摸春日燕归来骑马":
                mask = font.getmask(char)
                if not mask.getbbox() or mask.size[0] <= 6:
                    return False
            return True
        except Exception:
            return False

    def _usage_font_priority(self, filename: str) -> int:
        lower = filename.lower()
        if any(value in lower for value in ("serif", "song", "simsun", "ming", "kaiti", "fangsong")):
            return 100
        groups = [
            ("yahei", "msyh"),
            ("deng",),
            ("simhei", "heiti", "hei"),
            ("noto", "sans"),
            ("sourcehan", "sans"),
            ("wqy",),
            ("pingfang",),
            ("hiragino", "sans"),
            ("gothic",),
            ("sans",),
        ]
        for index, group in enumerate(groups):
            if all(value in lower for value in group):
                return index
        return 50

    def _usage_font_candidates_sorted(self) -> list[tuple[int, str]]:
        with self._usage_font_lock:
            if self._usage_font_candidates is not None:
                return self._usage_font_candidates
            font_dirs = []
            if platform.system() == "Windows":
                windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or "C:/Windows"
                font_dirs.append(os.path.join(windir, "Fonts"))
            else:
                font_dirs.extend([
                    "/usr/share/fonts",
                    "/usr/local/share/fonts",
                    os.path.expanduser("~/.local/share/fonts"),
                    "/System/Library/Fonts",
                    "/Library/Fonts",
                ])
            candidates = []
            for font_dir in font_dirs:
                if not os.path.isdir(font_dir):
                    continue
                for root, _, files in os.walk(font_dir):
                    for name in files:
                        lower = name.lower()
                        if lower.endswith((".ttf", ".ttc", ".otf")):
                            candidates.append((self._usage_font_priority(name), os.path.join(root, name)))
            self._usage_font_candidates = sorted(candidates, key=lambda item: item[0])
            return self._usage_font_candidates

    def _load_usage_font(self, size: int):
        if ImageFont is None:
            return None
        with self._usage_font_lock:
            cached_font = self._usage_font_cache.get(size)
            if cached_font is not None:
                return cached_font
        candidates = self._usage_font_candidates_sorted()
        for priority, path in candidates:
            if priority >= 100:
                continue
            try:
                font = ImageFont.truetype(path, size)
                if self._font_supports_usage_text(font):
                    with self._usage_font_lock:
                        self._usage_font_cache[size] = font
                    return font
            except Exception:
                continue
        for _, path in candidates:
            try:
                font = ImageFont.truetype(path, size)
                if self._font_supports_usage_text(font):
                    with self._usage_font_lock:
                        self._usage_font_cache[size] = font
                    return font
            except Exception:
                continue
        font = ImageFont.load_default()
        with self._usage_font_lock:
            self._usage_font_cache[size] = font
        return font

    def _draw_usage_text(self, draw, xy: tuple[int, int], text: str, font, fill: str, max_width: int | None = None) -> None:
        if not max_width:
            draw.text(xy, text, font=font, fill=fill)
            return
        value = text
        while value and draw.textbbox(xy, value, font=font)[2] - xy[0] > max_width:
            value = value[:-1]
        draw.text(xy, f"{value}…" if value != text else value, font=font, fill=fill)

    def _text_size(self, draw, text: str, font) -> tuple[int, int]:
        box = draw.textbbox((0, 0), text, font=font)
        return box[2] - box[0], box[3] - box[1]

    def _draw_centered_text(self, draw, box: tuple[int, int, int, int], text: str, font, fill: str) -> None:
        text_box = draw.textbbox((0, 0), text, font=font)
        text_w = text_box[2] - text_box[0]
        text_h = text_box[3] - text_box[1]
        x1, y1, x2, y2 = box
        x = x1 + (x2 - x1 - text_w) / 2 - text_box[0]
        y = y1 + (y2 - y1 - text_h) / 2 - text_box[1]
        draw.text((x, y), text, font=font, fill=fill)

    def _vertical_gradient(self, width: int, height: int, top: tuple[int, int, int], bottom: tuple[int, int, int]):
        if height <= 1:
            return Image.new("RGB", (width, height), top)
        rows = [tuple(int(top[i] * (1 - y / (height - 1)) + bottom[i] * (y / (height - 1))) for i in range(3)) for y in range(height)]
        image = Image.new("RGB", (1, height))
        image.putdata(rows)
        return image.resize((width, height))

    def _draw_soft_circle(self, image, center: tuple[int, int], radius: int, color: tuple[int, int, int, int]) -> None:
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        cx, cy = center
        for step in range(radius, 0, -8):
            alpha = int(color[3] * (1 - step / radius) ** 2)
            draw.ellipse((cx - step, cy - step, cx + step, cy + step), fill=(*color[:3], alpha))
        image.alpha_composite(overlay)

    def _render_meme_usage_stats(self, rows: list[tuple[str, int]], scope: str = "global", group_id: str = "", title_override: str = None) -> tuple[bytes, str]:
        if Image is None or ImageDraw is None:
            raise RuntimeError("Pillow 不可用")
        scale = 2
        columns = 4
        card_w, card_h = 250 * scale, 82 * scale
        gap_x, gap_y = 22 * scale, 20 * scale
        margin_x, top_h, bottom = 58 * scale, 178 * scale, 58 * scale
        shown_rows = rows[:self.usage_stats.limit()]
        row_count = max(1, (len(shown_rows) + columns - 1) // columns)
        width = margin_x * 2 + columns * card_w + (columns - 1) * gap_x
        height = top_h + row_count * card_h + (row_count - 1) * gap_y + bottom
        image = self._vertical_gradient(width, height, (248, 251, 255), (239, 245, 252)).convert("RGBA")
        self._draw_soft_circle(image, (130 * scale, 80 * scale), 220 * scale, (145, 190, 255, 70))
        self._draw_soft_circle(image, (width - 120 * scale, 130 * scale), 260 * scale, (255, 176, 211, 62))
        self._draw_soft_circle(image, (width // 2, height + 20 * scale), 340 * scale, (176, 224, 210, 52))
        draw = ImageDraw.Draw(image)
        title_font = self._load_usage_font(42 * scale)
        subtitle_font = self._load_usage_font(20 * scale)
        name_font = self._load_usage_font(21 * scale)
        rank_font = self._load_usage_font(14 * scale)
        count_font = self._load_usage_font(17 * scale)
        title = title_override or self.usage_stats.title()
        title_box = draw.textbbox((0, 0), title, font=title_font)
        draw.text(((width - (title_box[2] - title_box[0])) // 2, 42 * scale), title, font=title_font, fill="#14213d")
        total = sum(count for _, count in self.usage_stats.rows(10**9, scope, group_id))
        subtitle = f"表情调用总次数 · {total}"
        subtitle_w, _ = self._text_size(draw, subtitle, subtitle_font)
        pill_box = ((width - subtitle_w - 52 * scale) // 2, 103 * scale, (width + subtitle_w + 52 * scale) // 2, 143 * scale)
        draw.rounded_rectangle(pill_box, radius=20 * scale, fill=(255, 255, 255, 178), outline=(255, 255, 255, 230), width=scale)
        self._draw_centered_text(draw, pill_box, subtitle, subtitle_font, "#52677d")
        max_count = max((count for _, count in shown_rows), default=1)
        for index, (key, count) in enumerate(shown_rows):
            row, col = divmod(index, columns)
            x = margin_x + col * (card_w + gap_x)
            y = top_h + row * (card_h + gap_y)
            shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
            shadow_draw = ImageDraw.Draw(shadow)
            shadow_draw.rounded_rectangle((x + 3 * scale, y + 5 * scale, x + card_w + 3 * scale, y + card_h + 5 * scale), radius=20 * scale, fill=(48, 72, 102, 22))
            image.alpha_composite(shadow)
            draw.rounded_rectangle((x, y, x + card_w, y + card_h), radius=20 * scale, fill=(255, 255, 255, 218), outline=(255, 255, 255, 245), width=scale)
            accent_h = max(18 * scale, int((card_h - 26 * scale) * count / max_count))
            draw.rounded_rectangle((x + 14 * scale, y + card_h - 13 * scale - accent_h, x + 19 * scale, y + card_h - 13 * scale), radius=3 * scale, fill="#5b8def")
            rank = f"#{index + 1}"
            draw.text((x + 30 * scale, y + 16 * scale), rank, font=rank_font, fill="#9aa9b8")
            self._draw_usage_text(draw, (x + 30 * scale, y + 40 * scale), self.usage_stats.display_name(key, scope, group_id), name_font, "#1f2d3d", card_w - 112 * scale)
            count_text = f"{count} 次"
            count_box = draw.textbbox((0, 0), count_text, font=count_font)
            count_w = count_box[2] - count_box[0]
            badge_x = x + card_w - count_w - 34 * scale
            badge_box = (badge_x, y + 26 * scale, x + card_w - 18 * scale, y + 58 * scale)
            draw.rounded_rectangle(badge_box, radius=16 * scale, fill="#eef5ff")
            self._draw_centered_text(draw, badge_box, count_text, count_font, "#3f78c8")
        output = io.BytesIO()
        image.convert("RGB").save(output, format="PNG")
        return output.getvalue(), "image/png"

    def _get_message_args(self, event: AstrMessageEvent, command_name: str) -> str:
        message = self._extract_message_text(event)
        if not message:
            return ""

        # 优先处理带有空格的情况，支持 "小祥 meme搜索 举牌" 或 "catmeme搜索 举牌"
        parts = message.split(maxsplit=1)
        if parts:
            p0 = parts[0].lstrip("#/％%")
            if p0 == command_name or p0.endswith(command_name):
                return parts[1] if len(parts) > 1 else ""

        # 处理没有空格的情况，或者上述匹配失败的情况，如 "catmeme搜索举牌"
        idx = message.find(command_name)
        if idx != -1:
            # 找到指令位置，截取之后的部分作为参数
            return message[idx + len(command_name):].strip()

        return ""

    def _set_direction_option(self, options: dict[str, object], direction: str) -> None:
        existing = options.get("__direction")
        if existing and existing != direction:
            raise ArgSyntaxError(f"方向参数冲突：{existing} 与 {direction}")
        options["__direction"] = direction

    def _direction_options_for_key(self, direction: str) -> dict[str, object]:
        return {"direction": direction}

    def _materialize_direction_options(self, options: dict[str, object]) -> dict[str, object]:
        direction = options.get("__direction")
        if not direction:
            return options
        resolved = {name: value for name, value in options.items() if name not in {"__direction", "left", "right", "top", "bottom", "direction"}}
        resolved.update(self._direction_options_for_key(str(direction)))
        return resolved

    def _normalize_meme_options(self, raw_args: str) -> tuple[str, dict[str, object]]:
        options: dict[str, object] = {}
        tokens = []
        for token in split_arg_string(raw_args):
            if token in {"右", "#右"}:
                self._set_direction_option(options, "right")
                continue
            if token in {"左", "#左"}:
                self._set_direction_option(options, "left")
                continue
            if token in {"上", "#上"}:
                self._set_direction_option(options, "top")
                continue
            if token in {"下", "#下"}:
                self._set_direction_option(options, "bottom")
                continue
            if token.startswith("#") and len(token) > 1:
                if re.fullmatch(r"#[A-Za-z_][\w-]*=.+", token):
                    name, value = token[1:].split("=", 1)
                    options[name.replace("-", "_")] = value
                else:
                    options[token[1:]] = True
                continue
            if re.fullmatch(r"[A-Za-z_][\w-]*=.+", token):
                name, value = token.split("=", 1)
                options[name.replace("-", "_")] = value
                continue
            tokens.append(token)
        return " ".join(tokens), options

    def _direction_options_from_text(self, key: str, text: str) -> dict[str, object]:
        compact = text.replace(" ", "").replace("#", "")
        if key == "turn":
            if compact.startswith("转右"):
                return self._direction_options_for_key("right")
            if compact.startswith("转左"):
                return self._direction_options_for_key("left")
        if key == "symmetry":
            if compact.startswith("对称右"):
                return self._direction_options_for_key("right")
            if compact.startswith("对称左"):
                return self._direction_options_for_key("left")
            if compact.startswith("对称上"):
                return self._direction_options_for_key("top")
            if compact.startswith("对称下"):
                return self._direction_options_for_key("bottom")
        return {}

    def _avatar_url(self, user_id: str) -> str:
        if not user_id:
            return ""
        return f"https://q4.qlogo.cn/headimg_dl?dst_uin={quote(str(user_id), safe='')}&spec=640"

    def _sender_id(self, event: AstrMessageEvent) -> str:
        message_obj = getattr(event, "message_obj", None)
        raw_event = self._raw_event_dict_from_event(event)
        user_id = str(raw_event.get("user_id") or "").strip()
        if user_id:
            return user_id
        for source in (message_obj, event):
            for name in ("user_id", "sender_id"):
                user_id = str(getattr(source, name, "") or "").strip()
                if user_id:
                    return user_id
        try:
            return str(event.get_sender_id() or "").strip()
        except Exception:
            return ""

    def _sender_avatar_url(self, event: AstrMessageEvent) -> str:
        return self._avatar_url(self._sender_id(event))

    def _bot_id(self, event: AstrMessageEvent) -> str:
        message_obj = getattr(event, "message_obj", None)
        raw_event = self._raw_event_dict_from_event(event)
        bot_id = str(raw_event.get("self_id") or raw_event.get("bot_id") or "").strip()
        if bot_id:
            return bot_id
        return str(getattr(event, "self_id", "") or getattr(message_obj, "self_id", "") or "").strip()

    def _bot_avatar_url(self, event: AstrMessageEvent) -> str:
        return self._avatar_url(self._bot_id(event))

    def _group_id(self, event: AstrMessageEvent) -> str:
        try:
            group_id = str(event.get_group_id() or "").strip()
            if group_id:
                return group_id
        except Exception:
            pass
        message_obj = getattr(event, "message_obj", None)
        raw_message = self._raw_event_dict_from_event(event)
        if isinstance(raw_message, dict):
            group_id = str(raw_message.get("group_id") or raw_message.get("group") or "").strip()
            if group_id:
                return group_id
        for source in (message_obj, event):
            group_id = str(getattr(source, "group_id", "") or getattr(source, "group", "") or "").strip()
            if group_id:
                return group_id
        return ""

    def _group_name_from_event(self, event: AstrMessageEvent, group_id: str) -> str:
        message_obj = getattr(event, "message_obj", None)
        raw_message = self._raw_event_dict_from_event(event)
        names = []
        if isinstance(raw_message, dict):
            for key in ("group_name", "group_card", "group_title", "name", "title", "chat_name"):
                value = str(raw_message.get(key) or "").strip()
                if value:
                    names.append(value)
        for source in (message_obj, event):
            for attr in ("group_name", "group_card", "group_title", "name", "title", "chat_name"):
                value = str(getattr(source, attr, "") or "").strip()
                if value:
                    names.append(value)
        return next((name for name in names if name and name != group_id), "")

    async def _lookup_group_name(self, event: AstrMessageEvent | None, group_id: str) -> str:
        if event is None or not group_id:
            return ""
        try:
            group_data = await event.get_group(group_id)
            return self._name_from_group_info(group_data, group_id) or ""
        except Exception as e:
            logger.debug(f"event.get_group 获取群名失败: {e}")
            return ""

    async def _lookup_sender_name(self, event: AstrMessageEvent, user_id: str) -> str:
        bot = getattr(event, "bot", None)
        if not bot or not user_id:
            return ""
        group_id = self._group_id(event)
        query_user_id = int(user_id) if user_id.isdigit() else user_id
        query_group_id = int(group_id) if group_id.isdigit() else group_id
        calls = []
        if group_id:
            calls.extend((
                ("get_group_member_info", {"group_id": query_group_id, "user_id": query_user_id, "no_cache": False}),
                ("get_group_member", {"group_id": query_group_id, "user_id": query_user_id}),
            ))
        calls.extend((
            ("get_stranger_info", {"user_id": query_user_id, "no_cache": False}),
            ("get_friend_info", {"user_id": query_user_id}),
        ))

        for action, params in calls:
            for info in await self._call_bot_action_candidates(bot, action, params):
                name = self._name_from_user_info(info, user_id)
                if name:
                    return name
        return ""

    async def _call_bot_action_candidates(self, bot: object, action: str, params: dict) -> list[object]:
        results = []
        method = getattr(bot, action, None)
        if callable(method):
            try:
                results.append(await method(**params))
            except Exception as e:
                logger.debug(f"调用 {action} 失败: {e}")
        api = getattr(bot, "api", None)
        api_call_action = getattr(api, "call_action", None)
        if callable(api_call_action):
            try:
                results.append(await api_call_action(action, **params))
            except Exception as e:
                logger.debug(f"调用 api.{action} 失败: {e}")
        call_action = getattr(bot, "call_action", None)
        if callable(call_action):
            try:
                results.append(await call_action(action, **params))
            except Exception as e:
                logger.debug(f"调用 {action} 失败: {e}")
        return results

    async def _try_send_forward_message(self, event: AstrMessageEvent, title: str, content: str, count: int) -> bool:
        bot = getattr(event, "bot", None)
        if not bot or not content:
            return False
        group_id = self._group_id(event)
        user_id = self._sender_id(event)
        bot_id = self._bot_id(event) or "0"
        nodes = [{"type": "node", "data": {"name": title, "uin": bot_id, "content": content}}]
        metadata = {"prompt": "表情搜索结果", "summary": f"查看 {count} 条搜索结果", "source": "meme搜索"}
        if group_id:
            target = int(group_id) if group_id.isdigit() else group_id
            calls = [("send_group_forward_msg", {"group_id": target, "messages": nodes, **metadata})]
        elif user_id:
            target = int(user_id) if user_id.isdigit() else user_id
            calls = [("send_private_forward_msg", {"user_id": target, "messages": nodes, **metadata})]
        else:
            return False
        for action, params in calls:
            call_action = getattr(bot, "call_action", None)
            if callable(call_action):
                try:
                    await call_action(action, **params)
                    return True
                except Exception as e:
                    logger.debug(f"调用 {action} 失败: {e}")
                    continue
            method = getattr(bot, action, None)
            if callable(method):
                try:
                    await method(**params)
                    return True
                except Exception as e:
                    logger.debug(f"调用 {action} 失败: {e}")
        return False

    def _name_from_group_info(self, info: object, group_id: str) -> str:
        if isinstance(info, dict):
            data = info.get("data")
            if isinstance(data, dict):
                name = self._name_from_group_info(data, group_id)
                if name:
                    return name
            for key in ("group_name", "group_card", "name", "group_title", "title", "chat_name", "nickname"):
                value = str(info.get(key) or "").strip()
                if value and value != group_id:
                    return value
            return ""
        for attr in ("group_name", "group_card", "name", "group_title", "title", "chat_name", "nickname"):
            value = str(getattr(info, attr, "") or "").strip()
            if value and value != group_id:
                return value
        return ""

    def _name_from_user_info(self, info: object, user_id: str) -> str:
        if not isinstance(info, dict):
            return ""
        for key in ("card", "nickname", "user_name", "name", "remark"):
            value = str(info.get(key) or "").strip()
            if value and value != user_id:
                return value
        data = info.get("data")
        if isinstance(data, dict):
            return self._name_from_user_info(data, user_id)
        return ""

    async def _sender_user_info(self, event: AstrMessageEvent) -> dict:
        names = []
        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None)
        sender_id = self._sender_id(event)

        for source in (sender, message_obj, event):
            if not source:
                continue
            for attr in ("card", "nickname", "user_name", "name", "sender_name"):
                value = str(getattr(source, attr, "") or "").strip()
                if value:
                    names.append(value)

        raw_message = self._raw_event_dict_from_event(event)
        if isinstance(raw_message, dict):
            raw_sender = raw_message.get("sender")
            if isinstance(raw_sender, dict):
                for key in ("card", "nickname", "user_name", "name"):
                    value = str(raw_sender.get(key) or "").strip()
                    if value:
                        names.append(value)

        try:
            value = str(event.get_sender_name() or "").strip()
            if value:
                names.append(value)
        except Exception:
            pass

        name = next((value for value in names if value and value != sender_id), "")
        if not name:
            name = await self._lookup_sender_name(event, sender_id)
        return {"name": name or sender_id, "gender": "unknown"}

    def _bot_user_info(self, event: AstrMessageEvent) -> dict:
        bot_id = self._bot_id(event)
        return {"name": bot_id or "机器人", "gender": "unknown"}

    async def _read_limited_response(self, resp: aiohttp.ClientResponse, limit: int | None = None) -> bytes:
        max_bytes = limit or self.plugin_config.max_image_bytes()
        chunks = []
        total = 0
        async for chunk in resp.content.iter_chunked(64 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise RuntimeError(f"响应内容超过大小限制：{max_bytes} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    def _is_forbidden_ip(self, address: str) -> bool:
        try:
            ip = ipaddress.ip_address(address)
            return any((
                ip.is_loopback,
                ip.is_private,
                ip.is_link_local,
                ip.is_multicast,
                ip.is_reserved,
                ip.is_unspecified,
            ))
        except ValueError:
            return False

    async def _validate_external_image_url(self, url: str) -> tuple[str, set[str]]:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise RuntimeError("图片 URL 只支持 http/https")
        hostname = parsed.hostname
        if not hostname:
            raise RuntimeError("图片 URL 缺少主机名")
        lowered = hostname.rstrip(".").lower()
        if lowered == "localhost" or lowered.endswith(".localhost"):
            raise RuntimeError("不允许访问本机地址")

        if self._is_forbidden_ip(lowered):
            raise RuntimeError("不允许访问内网或本机地址")
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, hostname, parsed.port, type=socket.SOCK_STREAM)
        except socket.gaierror as e:
            raise RuntimeError(f"图片 URL 域名解析失败：{e}") from e
        resolved_ips = set()
        for info in infos:
            address = info[4][0]
            resolved_ips.add(address)
            if self._is_forbidden_ip(address):
                raise RuntimeError("不允许访问解析到内网或本机的地址")
        return lowered, resolved_ips

    def _response_peer_ip(self, resp: aiohttp.ClientResponse) -> str:
        connection = getattr(resp, "connection", None)
        transport = getattr(connection, "transport", None)
        if transport is None:
            return ""
        peername = transport.get_extra_info("peername")
        if isinstance(peername, tuple) and peername:
            return str(peername[0] or "")
        return ""

    def _validate_image_bytes(self, data: bytes, content_type: str) -> None:
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

    async def _request_external_image(self, session: aiohttp.ClientSession, url: str) -> tuple[bytes, str]:
        current_url = url
        for _ in range(5):
            _, resolved_ips = await self._validate_external_image_url(current_url)
            async with session.get(current_url, allow_redirects=False) as resp:
                peer_ip = self._response_peer_ip(resp)
                if not peer_ip:
                    raise RuntimeError("无法确认图片下载的实际连接地址")
                if self._is_forbidden_ip(peer_ip):
                    raise RuntimeError("实际连接到了内网或本机地址")
                if resolved_ips and peer_ip not in resolved_ips:
                    raise RuntimeError("图片下载连接地址与校验结果不一致")
                if 300 <= resp.status < 400:
                    location = resp.headers.get("Location")
                    if not location:
                        raise RuntimeError("图片下载重定向缺少 Location")
                    current_url = str(resp.url.join(location))
                    continue
                data = await self._read_limited_response(resp)
                if resp.status >= 400:
                    raise RuntimeError(f"下载图片失败：HTTP {resp.status}")
                content_type = resp.headers.get("Content-Type", "image/png").split(";", 1)[0]
                if not content_type.startswith("image/"):
                    raise RuntimeError(f"下载内容不是图片：{content_type}")
                self._validate_image_bytes(data, content_type)
                return data, content_type
        raise RuntimeError("图片下载重定向次数过多")

    async def _download_image(self, url: str) -> tuple[bytes, str, str]:
        timeout = aiohttp.ClientTimeout(total=self.plugin_config.meme_request_timeout())
        async with aiohttp.ClientSession(timeout=timeout) as session:
            data, content_type = await self._request_external_image(session, url)
        ext = mimetypes.guess_extension(content_type) or ".png"
        return data, content_type, f"image{ext}"

    def _extract_message_segments(self, event: AstrMessageEvent) -> list[object]:
        return self._extract_segments_from_event(event)

    async def _get_replied_message_segments(self, event: AstrMessageEvent) -> list[object]:
        for segment in self._extract_message_segments(event):
            if isinstance(segment, dict):
                if segment.get("type") != "reply":
                    continue
                data = segment.get("data") or {}
                message_id = str(data.get("id") or data.get("message_id") or data.get("msg_id") or "").strip()
            else:
                chain = getattr(segment, "chain", None)
                if chain is not None:
                    return list(chain) if isinstance(chain, list) else []
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
                        message_id = str(data.get("id") or data.get("message_id") or data.get("msg_id") or "").strip()
                if not message_id and not hasattr(segment, "chain"):
                    continue
            if not message_id:
                logger.warning(f"获取引用消息失败：未找到引用消息 ID，segment={type(segment).__name__}")
                return []
            try:
                msg = await event.bot.get_msg(message_id=int(message_id))
            except Exception as e:
                logger.warning(f"获取引用消息失败：message_id={message_id}，{e}")
                return []
            segments = msg.get("message", []) if isinstance(msg, dict) else []
            return segments if isinstance(segments, list) else []
        return []

    def _extract_image_urls_from_segments(self, segments: list[object]) -> list[str]:
        urls = []
        for segment in segments:
            if isinstance(segment, dict):
                if segment.get("type") not in {"image", "mface"}:
                    continue
                data = segment.get("data", {})
                candidates = [data.get("url"), data.get("file"), data.get("path")]
            else:
                candidates = [getattr(segment, "url", None), getattr(segment, "file", None), getattr(segment, "path", None)]
            for candidate in candidates:
                value = str(candidate or "").strip()
                if value.startswith("http://") or value.startswith("https://"):
                    urls.append(value)
                    break
        return urls

    def _extract_message_image_urls(self, event: AstrMessageEvent) -> list[str]:
        segments = [segment for segment in self._extract_message_segments(event) if isinstance(segment, dict) or not hasattr(segment, "chain")]
        return self._extract_image_urls_from_segments(segments)

    @staticmethod
    def _raw_event_dict_from_event(event: AstrMessageEvent) -> dict:
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

    @staticmethod
    def _extract_segments_from_event(event: AstrMessageEvent) -> list[object]:
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

    @staticmethod
    def _is_poke_to_bot_event(event: AstrMessageEvent) -> bool:
        message_obj = getattr(event, "message_obj", None)
        bot_id = str(getattr(event, "self_id", "") or getattr(message_obj, "self_id", "")).strip()
        for segment in MemeUpdater._extract_segments_from_event(event):
            if isinstance(segment, dict):
                seg_type = str(segment.get("type") or "").lower()
                data = segment.get("data") or {}
                target_id = str(data.get("id") or data.get("qq") or data.get("target_id") or "").strip() if isinstance(data, dict) else ""
            else:
                seg_type = str(getattr(segment, "type", "") or getattr(segment, "_type", "") or "").lower()
                target_id = ""
                target_method = getattr(segment, "target_id", None)
                if callable(target_method):
                    target_id = str(target_method() or "").strip()
                if not target_id:
                    target_id = str(getattr(segment, "id", "") or getattr(segment, "qq", "") or "").strip()
            if seg_type.endswith("poke") and target_id and bot_id and target_id == bot_id:
                return True

        raw = MemeUpdater._raw_event_dict_from_event(event)
        if not raw:
            return False
        post_type = str(raw.get("post_type") or raw.get("type") or "").lower()
        sub_type = str(raw.get("sub_type") or raw.get("notice_type") or raw.get("event") or raw.get("detail_type") or "").lower()
        if post_type and post_type not in {"notice", "notify"}:
            return False
        if sub_type not in {"poke", "戳一戳"}:
            return False
        target_id = str(raw.get("target_id") or raw.get("target") or "").strip()
        bot_id = bot_id or str(raw.get("self_id") or "").strip()
        return bool(target_id and bot_id and target_id == bot_id)

    @staticmethod
    def _segment_type_name(segment_type: object) -> str:
        value = getattr(segment_type, "value", None)
        if value is not None:
            return str(value).lower()
        name = getattr(segment_type, "name", None)
        if name is not None:
            return str(name).lower()
        return str(segment_type or "").lower()

    @staticmethod
    def _segment_text(segment: object) -> str:
        if isinstance(segment, dict):
            seg_type = MemeUpdater._segment_type_name(segment.get("type"))
            data = segment.get("data") or {}
            if seg_type in {"text", "plain"} and isinstance(data, dict):
                return str(data.get("text") or "")
            return ""
        seg_type = MemeUpdater._segment_type_name(getattr(segment, "type", "") or getattr(segment, "_type", ""))
        if seg_type not in {"plain", "text"}:
            return ""
        return str(getattr(segment, "text", "") or "")

    def _extract_message_text(self, event: AstrMessageEvent) -> str:
        text = "".join(self._segment_text(segment) for segment in self._extract_message_segments(event)).strip()
        if text:
            return text
        message_obj = getattr(event, "message_obj", None)
        for value in (getattr(message_obj, "message", None), getattr(event, "message", None)):
            if isinstance(value, str):
                return value.strip()
        return ""

    def _raw_event_dict(self, event: AstrMessageEvent) -> dict:
        return self._raw_event_dict_from_event(event)

    def _stop_event(self, event: AstrMessageEvent):
        stop_event = getattr(event, "stop_event", None)
        if callable(stop_event):
            stop_event()

    async def _yield_and_stop(self, event: AstrMessageEvent, result):
        self._stop_event(event)
        yield result

    def _is_poke_to_bot(self, event: AstrMessageEvent) -> bool:
        return self._is_poke_to_bot_event(event)

    def _extract_at_ids_from_segments(self, segments: list[object]) -> list[str]:
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
            qq = str(data.get("qq") or data.get("user_id") or data.get("uid") or data.get("id") or data.get("target") or "").strip()
            if qq and qq != "all" and qq not in user_ids:
                user_ids.append(qq)
        return user_ids

    def _extract_message_at_ids(self, event: AstrMessageEvent) -> list[str]:
        user_ids = self._extract_at_ids_from_segments(self._extract_message_segments(event))
        text = self._extract_message_text(event)
        for pattern in (r"\[CQ:at,qq=(\d+)\]", r"\[At:(\d+)\]", r"@[^\s@/\(]+\((\d{5,})\)", r"@[^\s@/]+/(\d{5,})", r"@(?:CQ:at,qq=)?(\d{5,})"):
            for qq in re.findall(pattern, text):
                if qq not in user_ids:
                    user_ids.append(qq)
        sender_id = self._sender_id(event)
        if sender_id and sender_id not in user_ids and text.startswith(("@", "＠")):
            user_ids.insert(0, sender_id)
        return user_ids

    async def _resolve_generate_args(self, event: AstrMessageEvent, raw_args: str) -> tuple[list[tuple[bytes, str, str]], list[str], list[dict]]:
        replied_segments = await self._get_replied_message_segments(event)
        image_urls = self._extract_image_urls_from_segments(replied_segments)
        image_urls.extend(self._extract_message_image_urls(event))
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
        for arg in split_arg_string(raw_args):
            if any(re.fullmatch(pattern, arg) for pattern in mention_patterns):
                continue
            if arg in {"自己", "@自己"}:
                avatar = self._sender_avatar_url(event)
                if avatar:
                    avatar_urls.append(avatar)
                    avatar_user_infos.append(await self._sender_user_info(event))
                continue
            if re.fullmatch(r"https?://\S+", arg):
                image_urls.append(arg)
                user_infos.append({})
                continue
            if arg.startswith("@") and arg[1:].isdigit():
                user_id = arg[1:]
                avatar_urls.append(self._avatar_url(user_id))
                avatar_user_infos.append({"name": await self._lookup_sender_name(event, user_id) or user_id, "gender": "unknown"})
                continue
            texts.append(arg)
        at_ids = self._extract_message_at_ids(event)
        for user_id in at_ids:
            avatar_urls.append(self._avatar_url(user_id))
            avatar_user_infos.append({"name": await self._lookup_sender_name(event, user_id) or user_id, "gender": "unknown"})
        explicit_image_count = len(image_urls)
        image_urls.extend(avatar_urls)
        user_infos.extend(avatar_user_infos)
        if image_urls:
            hosts = []
            for url in image_urls:
                host = urlparse(url).hostname
                hosts.append(host or "unknown")
            logger.info(f"meme 参数图片数量：{len(image_urls)}，来源域名：{hosts}，当前发送者={self._sender_id(event)}")
        if image_urls:
            semaphore = asyncio.Semaphore(MAX_IMAGE_DOWNLOAD_CONCURRENCY)

            async def download(url: str):
                async with semaphore:
                    return await self._download_image(url)

            download_results = await asyncio.gather(*(download(url) for url in image_urls), return_exceptions=True)
            explicit_failures = [result for result in download_results[:explicit_image_count] if isinstance(result, Exception)]
            if explicit_failures:
                failure = explicit_failures[0]
                message = str(failure) or type(failure).__name__
                raise RuntimeError(f"引用/输入图片下载失败：{message}")
            images = [result for result in download_results if not isinstance(result, Exception)]
            user_infos = [info for info, result in zip(user_infos, download_results) if not isinstance(result, Exception)]
        else:
            images = []
        return list(images), texts, user_infos

    async def _fill_sender_avatar_images(self, event: AstrMessageEvent, images: list[tuple[bytes, str, str]], user_infos: list[dict], target_count: int):
        if not self.plugin_config.meme_auto_sender_avatar() or len(images) >= target_count:
            return
        avatar = self._sender_avatar_url(event)
        if not avatar:
            return
        data, content_type, filename = await self._download_image(avatar)
        user_info = await self._sender_user_info(event)
        while len(images) < target_count:
            images.insert(0, (data, content_type, filename))
            user_infos.insert(0, user_info)

    async def _fill_default_avatar_images(self, event: AstrMessageEvent, images: list[tuple[bytes, str, str]], user_infos: list[dict], target_count: int):
        if not self.plugin_config.meme_auto_sender_avatar() or images:
            return
        sender_avatar = self._sender_avatar_url(event)
        bot_avatar = self._bot_avatar_url(event)
        fill_items = []
        if target_count >= 2 and bot_avatar:
            fill_items.append((bot_avatar, self._bot_user_info(event)))
        if sender_avatar:
            fill_items.append((sender_avatar, await self._sender_user_info(event)))
        if not fill_items:
            return
        avatar_cache: dict[str, tuple[bytes, str, str]] = {}
        fill_index = 0
        while len(images) < target_count:
            url, user_info = fill_items[fill_index % len(fill_items)]
            if url not in avatar_cache:
                avatar_cache[url] = await self._download_image(url)
            data, content_type, filename = avatar_cache[url]
            images.append((data, content_type, filename))
            user_infos.append(user_info)
            fill_index += 1

    def _select_render_images(self, images: list[tuple[bytes, str, str]], user_infos: list[dict], max_images: int) -> tuple[list[tuple[bytes, str, str]], list[dict]]:
        if max_images <= 0 or len(images) <= max_images:
            return images, user_infos
        selected_images = images[:max_images]
        selected_user_infos = user_infos[:max_images]
        return selected_images, selected_user_infos

    def _safe_int(self, value: object, default: int = 0) -> int:
        try:
            if value is None or value == "":
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    def _params_type(self, info: dict) -> dict:
        params = info.get("params_type") or {}
        return {
            "min_images": self._safe_int(params.get("min_images")),
            "max_images": self._safe_int(params.get("max_images")),
            "min_texts": self._safe_int(params.get("min_texts")),
            "max_texts": self._safe_int(params.get("max_texts")),
            "default_texts": list(params.get("default_texts") or []),
        }

    def _format_meme_list_text(self) -> str:
        template = self.plugin_config.meme_list_text_template()
        lines = []
        for index, info in enumerate(self._sorted_meme_infos(), 1):
            keywords = "、".join(str(value) for value in info.get("keywords", []) if str(value).strip())
            line = template.format(
                index=index,
                key=info.get("key", ""),
                keywords=keywords or self._meme_display_name(info),
            )
            lines.append(line)
        return "\n".join(lines)

    async def _render_list(self) -> tuple[bytes, str]:
        meme_list = []
        now = datetime.now().timestamp()
        for index, info in enumerate(self._sorted_meme_infos(), 1):
            labels = []
            try:
                created = self._parse_meme_time(info.get("date_created"))
                if now - created <= 30 * 24 * 3600:
                    labels.append("new")
            except Exception:
                pass
            meme_list.append({"meme_key": info.get("key"), "disabled": False, "labels": labels, "index": index})
        return await self.meme_client.render_list(meme_list, self.plugin_config.meme_list_text_template())

    def _parse_meme_time(self, value: object) -> float:
        if not value:
            return 0
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0

    def _meme_keyword_sort_value(self, info: dict) -> str:
        keywords = info.get("keywords")
        if isinstance(keywords, list) and keywords:
            return str(keywords[0]).lower()
        return ""

    def _sorted_meme_infos(self) -> list[dict]:
        sort_by = self.plugin_config.meme_list_sort_by()
        reverse = self.plugin_config.meme_list_sort_reverse()
        infos = list(self.meme_infos.values())
        if sort_by == "名称":
            return sorted(infos, key=lambda info: str(info.get("key", "")).lower(), reverse=reverse)
        if sort_by == "关键词":
            return sorted(infos, key=self._meme_keyword_sort_value, reverse=reverse)
        if sort_by == "更新时间":
            return sorted(infos, key=lambda info: self._parse_meme_time(info.get("date_modified")), reverse=reverse)
        return sorted(infos, key=lambda info: self._parse_meme_time(info.get("date_created")), reverse=reverse)

    def _image_component(self, data: bytes, content_type: str) -> Comp.Image:
        ext = mimetypes.guess_extension(content_type) or ".png"
        try:
            temp_dir = os.path.join(self._meme_data_dir, "temp")
            os.makedirs(temp_dir, exist_ok=True)
            now = time.time()
            if now - self._last_temp_cleanup >= TEMP_IMAGE_CLEANUP_INTERVAL_SECONDS:
                self._cleanup_temp_images(temp_dir, now)
                self._last_temp_cleanup = now
            with tempfile.NamedTemporaryFile(suffix=ext, dir=temp_dir, delete=False) as tf:
                tf.write(data)
                temp_path = tf.name
            temp_path = os.path.abspath(temp_path).replace("\\", "/")
            return Comp.Image(file=f"file:///{temp_path}")
        except Exception as e:
            logger.warning(f"无法创建临时文件发送图片，降级到 base64: {e}")
            b64 = base64.b64encode(data).decode("ascii")
            return Comp.Image(file=f"base64://{b64}")

    def _cleanup_temp_images(self, temp_dir: str, now: float | None = None) -> None:
        expires_before = (now or time.time()) - TEMP_IMAGE_TTL_SECONDS
        for name in os.listdir(temp_dir):
            path = os.path.join(temp_dir, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < expires_before:
                    os.remove(path)
            except OSError:
                pass

    def _shortcut_args(self, template_args: list, match: re.Match) -> str:
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

    def _normalize_turn_options(self, key: str, raw_args: str) -> tuple[str, dict[str, object]]:
        if key != "turn":
            return raw_args, {}
        compact = raw_args.replace(" ", "")
        if compact in {"右", "#右"}:
            return "", self._direction_options_for_key("right")
        if compact in {"左", "#左"}:
            return "", self._direction_options_for_key("left")
        return raw_args, {}

    @filter.command("重启memeapi")
    async def restart_memeapi(self, event: AstrMessageEvent):
        try:
            yield event.plain_result("正在重启 memeapi 服务，请稍候...")
            result = await self.repo_manager.restart_memeapi()
            yield event.plain_result("\n".join(result["lines"]))
        finally:
            self._stop_event(event)

    @filter.command("更新表情包")
    async def update_memes(self, event: AstrMessageEvent):
        try:
            if not self.plugin_config.repo_update_enabled():
                yield event.plain_result("更新表情包功能未启用，请先在配置项 repo_update_enabled 中开启。")
                return

            yield event.plain_result("正在更新表情包数据，请稍候...")

            os.makedirs(self._meme_data_dir, exist_ok=True)

            started_at = datetime.now()
            repos = self.repo_manager.repos()
            total = len(repos)
            semaphore = asyncio.Semaphore(self.plugin_config.repo_update_concurrency())

            async def sync_limited(repo: dict, index: int):
                async with semaphore:
                    return await self.repo_manager.sync_repo(repo, index, total)

            results = await asyncio.gather(*[sync_limited(repo, i + 1) for i, repo in enumerate(repos)], return_exceptions=True)
            normalized_results = []
            for i, result in enumerate(results, 1):
                if isinstance(result, Exception):
                    normalized_results.append({"status": "failed", "updated": False, "lines": [f"❌ [{i}/{total}] 更新异常", f"    {result}"]})
                else:
                    normalized_results.append(result)
            results = normalized_results
            success = sum(1 for r in results if r["status"] == "success")
            success_updates = [r for r in results if r["status"] == "success" and r["updated"]]
            failed = sum(1 for r in results if r["status"] == "failed")
            updated = sum(1 for r in results if r["updated"])

            restart_result = None
            if updated > 0:
                restart_result = await self.repo_manager.restart_memeapi()
                if restart_result["success"]:
                    restart_result["lines"].append("⏳ 等待 meme API 启动后刷新表情信息...")
                    await asyncio.sleep(MEME_API_RESTART_REFRESH_INTERVAL_SECONDS)
                    await self._refresh_meme_infos_after_restart(restart_result["lines"])

            finished_at = datetime.now()
            summary_lines = [
                "========================",
                "📋 更新任务执行完成",
                f"⏰ {self._format_time(started_at)} → {self._format_time(finished_at)} ({(finished_at - started_at).total_seconds():.2f}秒)",
                f"📊 成功:{success} 失败:{failed} 更新:{updated}",
                "========================",
                "🔌 开始执行 meme 仓库更新任务",
                f"⏰ 开始时间: {self._format_time(started_at)}",
                "========================",
            ]

            for result in results:
                summary_lines.extend(result["lines"])

            if success_updates:
                summary_lines.extend(["========================", "✅ 成功更新的仓库:"])
                for result in success_updates:
                    success_line = next(
                        (line.strip() for line in result["lines"] if "完成" in line and "📦" not in line),
                        result["lines"][-1],
                    )
                    summary_lines.append(f"  - {success_line}")

            if restart_result:
                summary_lines.append("准备重启 memeapi...")
                summary_lines.extend(restart_result["lines"])
                summary_lines.append("========================")
                summary_lines.append(f"📌 重启状态: {'成功' if restart_result['success'] else '失败'}")
            else:
                summary_lines.extend([
                    "仓库无更新，已跳过 memeapi 重启。",
                    "========================",
                ])

            yield event.plain_result("\n".join(summary_lines))
        except Exception as e:
            logger.exception("更新表情包失败")
            yield event.plain_result(f"更新表情包失败：{e}")
        finally:
            self._stop_event(event)

    @filter.command("表情包状态")
    async def meme_status(self, event: AstrMessageEvent):
        self._stop_event(event)
        lines = ["表情包仓库状态:"]

        repos = self.repo_manager.repos()

        for repo in repos:
            lines.append(await self.repo_manager.repo_status(repo))

        try:
            count = await self._refresh_meme_infos()
            lines.append(f"meme API: 已加载 {count} 个表情 | {self.plugin_config.meme_api_base_url()}")
        except Exception as e:
            lines.append(f"meme API: 无法连接或加载失败 | {self.plugin_config.meme_api_base_url()} | {e}")

        yield event.plain_result("\n".join(lines))

    @filter.command("刷新表情信息")
    async def refresh_meme_infos(self, event: AstrMessageEvent):
        self._stop_event(event)
        task = self._meme_info_refresh_task
        if task and not task.done():
            yield event.plain_result("meme API 表情信息仍在后台加载中，请稍后再试。")
            return
        if task and task.done():
            try:
                task.result()
            except Exception:
                pass
        task = asyncio.create_task(self._refresh_meme_infos(force=True))
        self._meme_info_refresh_task = task
        try:
            count = await task
            yield event.plain_result(f"表情信息刷新完成，共载入 {count} 个表情。")
        except Exception as e:
            yield event.plain_result(f"刷新表情信息失败：{e}")
        finally:
            if self._meme_info_refresh_task is task:
                self._meme_info_refresh_task = None

    @filter.command("表情统计")
    async def meme_usage_stats(self, event: AstrMessageEvent):
        self._stop_event(event)
        group_id = self._group_id(event)
        scope = "group" if group_id else "global"
        rows = self.usage_stats.rows(scope=scope, group_id=group_id)
        if not rows:
            yield event.plain_result("当前群组暂无表情调用统计。" if group_id else "暂无表情调用统计。")
            return
        try:
            await self._refresh_meme_infos()
            title_override = None if group_id else "总表情统计"
            image, content_type = await asyncio.to_thread(self._render_meme_usage_stats, rows, scope=scope, group_id=group_id, title_override=title_override)
            yield event.chain_result([self._image_component(image, content_type)])
        except Exception as e:
            logger.warning(f"生成表情调用统计图失败: {e}")
            yield event.plain_result(self.usage_stats.format_text(rows, scope=scope, group_id=group_id))

    @filter.command("总表情统计")
    async def meme_global_usage_stats(self, event: AstrMessageEvent):
        self._stop_event(event)
        rows = self.usage_stats.rows(scope="global")
        if not rows:
            yield event.plain_result("暂无表情调用统计。")
            return
        try:
            await self._refresh_meme_infos()
            image, content_type = await asyncio.to_thread(self._render_meme_usage_stats, rows, scope="global", title_override="总表情统计")
            yield event.chain_result([self._image_component(image, content_type)])
        except Exception as e:
            logger.warning(f"生成总表情调用统计图失败: {e}")
            yield event.plain_result(self.usage_stats.format_text(rows))

    @filter.command("meme搜索")
    async def meme_search(self, event: AstrMessageEvent):
        self._stop_event(event)
        query = self._get_message_args(event, "meme搜索")
        if not query:
            yield event.plain_result("用法：meme搜索 <关键词>")
            return
        try:
            await self._refresh_meme_infos()
            matches = self._search_memes(query)
            if not matches:
                yield event.plain_result(f"未找到相关表情：{query}")
                return
            title = f"搜索结果（查看 {len(matches)} 条搜索结果）"
            result_text = "\n".join([title, *[self._format_meme_search_result(index, info) for index, info in enumerate(matches, 1)]])
            if self.plugin_config.meme_search_forward_enabled() and await self._try_send_forward_message(event, title, result_text, len(matches)):
                return
            yield event.plain_result(result_text)
        except Exception as e:
            yield event.plain_result(f"搜索表情失败：{e}")

    @filter.command("表情列表")
    async def meme_list(self, event: AstrMessageEvent):
        try:
            await self._refresh_meme_infos()
            try:
                image, content_type = await self._render_list()
                yield event.chain_result([self._image_component(image, content_type)])
                return
            except Exception as e:
                logger.warning(f"meme API 渲染表情列表失败，降级为文本列表: {e}")
            result_text = self._format_meme_list_text()
            if self.plugin_config.meme_search_forward_enabled() and await self._try_send_forward_message(event, "表情列表", result_text, len(self.meme_infos)):
                return
            yield event.plain_result(result_text)
        except Exception as e:
            yield event.plain_result(f"获取表情列表失败：{e}")
        finally:
            self._stop_event(event)

    @filter.command("表情详情")
    async def meme_info(self, event: AstrMessageEvent):
        self._stop_event(event)
        query = self._get_message_args(event, "表情详情")
        if not query:
            yield event.plain_result("用法：表情详情 <表情名/关键词>")
            return
        try:
            await self._refresh_meme_infos()
            info = self._find_meme(query)
            if not info:
                yield event.plain_result(f"未找到表情：{query}")
                return
            params = self._params_type(info)
            lines = [
                f"表情：{info.get('key')}",
                f"关键词：{_format_keywords([str(v) for v in info.get('keywords', [])])}",
            ]
            shortcuts = [str(v.get("humanized") or v.get("key")) for v in info.get("shortcuts", []) if isinstance(v, dict)]
            if shortcuts:
                lines.append(f"快捷指令：{_format_keywords(shortcuts)}")
            if params["max_images"]:
                lines.append(f"图片数量：{_format_range(params['min_images'], params['max_images'])}")
            if params["max_texts"]:
                lines.append(f"文字数量：{_format_range(params['min_texts'], params['max_texts'])}")
                if params["default_texts"]:
                    lines.append(f"默认文字：{_format_keywords([str(v) for v in params['default_texts']])}")
            components = [Comp.Plain("\n".join(lines))]
            try:
                image, content_type = await self.meme_client.get_preview(str(info.get("key")))
                components.extend([Comp.Plain("\n"), self._image_component(image, content_type)])
            except Exception as e:
                logger.warning(f"获取表情预览失败 {info.get('key')}: {e}")
            yield event.chain_result(components)
        except Exception as e:
            yield event.plain_result(f"获取表情详情失败：{e}")

    @filter.command("制作表情")
    async def meme_generate(self, event: AstrMessageEvent):
        self._stop_event(event)
        raw_args = self._get_message_args(event, "制作表情")
        if not raw_args:
            yield event.plain_result("用法：制作表情 <表情名/关键词> [文字/@自己/@QQ号/图片URL...]")
            return
        try:
            parts = split_arg_string(raw_args)
            if not parts:
                yield event.plain_result("用法：制作表情 <表情名/关键词> [文字/@自己/@QQ号/图片URL...]")
                return
            query, rest = parts[0], " ".join(parts[1:])
            await self._refresh_meme_infos()
            info = self._find_meme(query)
            if not info:
                yield event.plain_result(f"未找到表情：{query}")
                return
            params = self._params_type(info)
            rest, options = self._normalize_turn_options(str(info.get("key")), rest)
            images, texts, user_infos = await self._resolve_generate_args(event, rest)
            if self.plugin_config.meme_auto_sender_avatar() and len(images) < params["min_images"]:
                if images:
                    await self._fill_sender_avatar_images(event, images, user_infos, params["min_images"])
                else:
                    await self._fill_default_avatar_images(event, images, user_infos, params["min_images"])
            if self.plugin_config.meme_auto_default_texts() and not texts:
                texts.extend(str(v) for v in params["default_texts"])
            if not (params["min_images"] <= len(images) <= params["max_images"]):
                yield event.plain_result(f"图片数量不符，需要 {_format_range(params['min_images'], params['max_images'])} 张，当前 {len(images)} 张。")
                return
            if not (params["min_texts"] <= len(texts) <= params["max_texts"]):
                yield event.plain_result(f"文字数量不符，需要 {_format_range(params['min_texts'], params['max_texts'])} 段，当前 {len(texts)} 段。")
                return
            image, content_type = await self.meme_client.render_meme(str(info.get("key")), images, texts, user_infos, options)
            await self.usage_stats.record(event, info)
            async for result in self._yield_and_stop(event, event.chain_result([self._image_component(image, content_type)])):
                yield result
        except ArgSyntaxError as e:
            yield event.plain_result(str(e))
        except Exception as e:
            yield event.plain_result(f"制作表情失败：{e}")

    async def _random_meme_results(self, event: AstrMessageEvent, raw_args: str, resolve_args: bool = True):
        try:
            await self._refresh_meme_infos()
            raw_args, options = self._normalize_meme_options(raw_args)
            options = self._materialize_direction_options(options)
            if resolve_args:
                images, texts, user_infos = await self._resolve_generate_args(event, raw_args)
            else:
                images, texts, user_infos = [], [], []
            auto_use = not images and not texts
            suitable = []
            for info in self.meme_infos.values():
                params = self._params_type(info)
                image_count = params["min_images"] if auto_use else len(images)
                if params["min_images"] <= image_count <= params["max_images"] and (auto_use or params["min_texts"] <= len(texts) <= params["max_texts"]):
                    suitable.append((info, params))
            random.shuffle(suitable)
            for info, params in suitable:
                try:
                    render_images = list(images)
                    render_user_infos = list(user_infos)
                    if auto_use:
                        await self._fill_default_avatar_images(event, render_images, render_user_infos, params["min_images"])
                    render_texts = [str(v) for v in params["default_texts"]] if auto_use else texts
                    image, content_type = await self.meme_client.render_meme(str(info.get("key")), render_images, render_texts, render_user_infos, options)
                    await self.usage_stats.record(event, info)
                    keywords = _format_keywords([str(v) for v in info.get("keywords", [])])
                    async for result in self._yield_and_stop(event, event.chain_result([
                        Comp.Plain(f"关键词：{keywords}\n"),
                        self._image_component(image, content_type),
                    ])):
                        yield result
                    return
                except Exception as e:
                    logger.debug(f"随机表情渲染跳过 {info.get('key')}: {e}")
                    continue
            yield event.plain_result("没有找到适合当前参数的表情。")
        except Exception as e:
            yield event.plain_result(f"随机表情失败：{e}")

    @filter.custom_filter(PokeToBotFilter)
    async def meme_poke_random_listener(self, event: AstrMessageEvent):
        if not self.plugin_config.meme_poke_random_enabled():
            return
        async for result in self._random_meme_results(event, "", resolve_args=False):
            yield result

    @filter.event_message_type(EventMessageType.ALL)
    async def meme_shortcut_listener(self, event: AstrMessageEvent):
        content = self._extract_message_text(event)
        if content in {"随机表情", "随机meme", "随机 meme", "来个表情", "来张表情"}:
            async for result in self._random_meme_results(event, ""):
                yield result
            self._stop_event(event)
            return

        if not self.plugin_config.meme_shortcut_enabled():
            return

        if not content or content.startswith(("/", "#", "%", "％")):
            return
        if not self.meme_infos:
            if not await self._wait_meme_info_refresh_for_shortcut():
                logger.info("表情信息正在初始化，请稍后再试一次。")
                self._stop_event(event)
                return
        if not self.meme_shortcuts:
            self._refresh_meme_shortcuts()
        try:
            for shortcut in self.meme_shortcuts:
                match = shortcut["compiled_regex"].match(content)
                if not match:
                    continue
                tail = content[match.end():].strip()
                resolved_args = " ".join(value for value in [self._shortcut_args(shortcut["args"], match), tail] if value).strip()
                info = self.meme_infos.get(shortcut["key"])
                if not info:
                    continue
                params = self._params_type(info)
                key = str(info.get("key"))
                resolved_args, options = self._normalize_meme_options(resolved_args)
                options = {**shortcut.get("options", {}), **options}
                content_options = self._direction_options_from_text(key, content)
                if content_options:
                    for d in ["left", "right", "top", "bottom", "direction", "__direction"]:
                        options.pop(d, None)
                    options.update(content_options)
                options = self._materialize_direction_options(options)
                images, texts, user_infos = await self._resolve_generate_args(event, resolved_args)

                if params["max_images"] == 2 and str(info.get("key")) != MIRAGETANK_KEY and len(images) >= 3:
                    images = [images[0], images[1]]
                    user_infos = [user_infos[0], user_infos[1]]
                elif len(images) > params["max_images"]:
                    images, user_infos = self._select_render_images(images, user_infos, params["max_images"])

                if self.plugin_config.meme_auto_sender_avatar() and len(images) < params["min_images"]:
                    if images:
                        await self._fill_sender_avatar_images(event, images, user_infos, params["min_images"])
                    else:
                        await self._fill_default_avatar_images(event, images, user_infos, params["min_images"])

                if self.plugin_config.meme_auto_default_texts() and not texts:
                    texts.extend(str(v) for v in params["default_texts"])

                if not (params["min_images"] <= len(images) <= params["max_images"]):
                    continue
                if not (params["min_texts"] <= len(texts) <= params["max_texts"]):
                    continue

                image, content_type = await self.meme_client.render_meme(key, images, texts, user_infos, options)
                await self.usage_stats.record(event, info)
                yield event.chain_result([self._image_component(image, content_type)])
                self._stop_event(event)
                return
        except Exception as e:
            logger.warning(f"处理表情快捷指令异常: {e}")
            return
