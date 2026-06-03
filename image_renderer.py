import io
import os
import platform
import threading
from collections.abc import Callable

from astrbot.api import logger

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:
    Image = ImageDraw = ImageFont = None


class MemeImageRenderer:
    def __init__(self, usage_stats, remove_emoji: Callable[[str], str]):
        self.usage_stats = usage_stats
        self.remove_emoji = remove_emoji
        self._usage_font_candidates: list[tuple[int, str]] | None = None
        self._usage_font_cache = {}
        self._usage_font_lock = threading.Lock()
        self._usage_font_warned = False

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
        if any(
            value in lower
            for value in ("serif", "song", "simsun", "ming", "kaiti", "fangsong")
        ):
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
                windir = (
                    os.environ.get("WINDIR")
                    or os.environ.get("SystemRoot")
                    or "C:/Windows"
                )
                font_dirs.append(os.path.join(windir, "Fonts"))
            else:
                font_dirs.extend(
                    [
                        "/usr/share/fonts",
                        "/usr/local/share/fonts",
                        os.path.expanduser("~/.local/share/fonts"),
                        "/System/Library/Fonts",
                        "/Library/Fonts",
                    ]
                )
            candidates = []
            for font_dir in font_dirs:
                if not os.path.isdir(font_dir):
                    continue
                for root, _, files in os.walk(font_dir):
                    for name in files:
                        lower = name.lower()
                        if lower.endswith((".ttf", ".ttc", ".otf")):
                            candidates.append(
                                (
                                    self._usage_font_priority(name),
                                    os.path.join(root, name),
                                )
                            )
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
        if not self._usage_font_warned:
            self._usage_font_warned = True
            logger.warning(
                "未找到可用的中文字体，统计/列表图片将回退到默认字体，"
                "中文可能显示为方块或缺字，请在部署环境安装中文字体"
            )
        with self._usage_font_lock:
            self._usage_font_cache[size] = font
        return font

    def _draw_usage_text(
        self,
        draw,
        xy: tuple[int, int],
        text: str,
        font,
        fill: str,
        max_width: int | None = None,
    ) -> None:
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

    def _draw_centered_text(
        self, draw, box: tuple[int, int, int, int], text: str, font, fill: str
    ) -> None:
        text_box = draw.textbbox((0, 0), text, font=font)
        text_w = text_box[2] - text_box[0]
        text_h = text_box[3] - text_box[1]
        x1, y1, x2, y2 = box
        x = x1 + (x2 - x1 - text_w) / 2 - text_box[0]
        y = y1 + (y2 - y1 - text_h) / 2 - text_box[1]
        draw.text((x, y), text, font=font, fill=fill)

    def _vertical_gradient(
        self,
        width: int,
        height: int,
        top: tuple[int, int, int],
        bottom: tuple[int, int, int],
    ):
        if height <= 1:
            return Image.new("RGB", (width, height), top)
        rows = [
            tuple(
                int(top[i] * (1 - y / (height - 1)) + bottom[i] * (y / (height - 1)))
                for i in range(3)
            )
            for y in range(height)
        ]
        image = Image.new("RGB", (1, height))
        image.putdata(rows)
        return image.resize((width, height))

    def _draw_soft_circle(
        self,
        image,
        center: tuple[int, int],
        radius: int,
        color: tuple[int, int, int, int],
    ) -> None:
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        cx, cy = center
        for step in range(radius, 0, -8):
            alpha = int(color[3] * (1 - step / radius) ** 2)
            draw.ellipse(
                (cx - step, cy - step, cx + step, cy + step), fill=(*color[:3], alpha)
            )
        image.alpha_composite(overlay)

    def _draw_card_shadows(
        self,
        image,
        count: int,
        columns: int,
        margin_x: int,
        top_h: int,
        card_w: int,
        card_h: int,
        gap_x: int,
        gap_y: int,
        scale: int,
    ) -> None:
        """在单张 overlay 上批量绘制所有卡片阴影，最后只合成一次。"""
        shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
        shadow_draw = ImageDraw.Draw(shadow)
        for index in range(count):
            row, col = divmod(index, columns)
            x = margin_x + col * (card_w + gap_x)
            y = top_h + row * (card_h + gap_y)
            shadow_draw.rounded_rectangle(
                (
                    x + 3 * scale,
                    y + 5 * scale,
                    x + card_w + 3 * scale,
                    y + card_h + 5 * scale,
                ),
                radius=20 * scale,
                fill=(48, 72, 102, 22),
            )
        image.alpha_composite(shadow)

    def render_meme_usage_stats(
        self,
        rows: list[tuple[str, int]],
        scope: str = "global",
        group_id: str = "",
        title_override: str | None = None,
    ) -> tuple[bytes, str]:
        if Image is None or ImageDraw is None:
            raise RuntimeError("Pillow 不可用")
        scale = 2
        columns = 4
        card_w, card_h = 250 * scale, 82 * scale
        gap_x, gap_y = 22 * scale, 20 * scale
        margin_x, top_h, bottom = 58 * scale, 178 * scale, 58 * scale
        shown_rows = rows[: self.usage_stats.limit()]
        row_count = max(1, (len(shown_rows) + columns - 1) // columns)
        width = margin_x * 2 + columns * card_w + (columns - 1) * gap_x
        height = top_h + row_count * card_h + (row_count - 1) * gap_y + bottom
        image = self._vertical_gradient(
            width, height, (248, 251, 255), (239, 245, 252)
        ).convert("RGBA")
        self._draw_soft_circle(
            image, (130 * scale, 80 * scale), 220 * scale, (145, 190, 255, 70)
        )
        self._draw_soft_circle(
            image, (width - 120 * scale, 130 * scale), 260 * scale, (255, 176, 211, 62)
        )
        self._draw_soft_circle(
            image, (width // 2, height + 20 * scale), 340 * scale, (176, 224, 210, 52)
        )
        draw = ImageDraw.Draw(image)
        title_font = self._load_usage_font(42 * scale)
        subtitle_font = self._load_usage_font(20 * scale)
        name_font = self._load_usage_font(21 * scale)
        rank_font = self._load_usage_font(14 * scale)
        count_font = self._load_usage_font(17 * scale)
        title = title_override or self.usage_stats.title()
        title_box = draw.textbbox((0, 0), title, font=title_font)
        draw.text(
            ((width - (title_box[2] - title_box[0])) // 2, 42 * scale),
            title,
            font=title_font,
            fill="#14213d",
        )
        total = sum(count for _, count in self.usage_stats.rows(10**9, scope, group_id))
        subtitle = f"表情调用总次数 · {total}"
        subtitle_w, _ = self._text_size(draw, subtitle, subtitle_font)
        pill_box = (
            (width - subtitle_w - 52 * scale) // 2,
            103 * scale,
            (width + subtitle_w + 52 * scale) // 2,
            143 * scale,
        )
        draw.rounded_rectangle(
            pill_box,
            radius=20 * scale,
            fill=(255, 255, 255, 178),
            outline=(255, 255, 255, 230),
            width=scale,
        )
        self._draw_centered_text(draw, pill_box, subtitle, subtitle_font, "#52677d")
        max_count = max((count for _, count in shown_rows), default=1)
        stats_data = self.usage_stats.load()
        self._draw_card_shadows(
            image,
            len(shown_rows),
            columns,
            margin_x,
            top_h,
            card_w,
            card_h,
            gap_x,
            gap_y,
            scale,
        )
        for index, (key, count) in enumerate(shown_rows):
            row, col = divmod(index, columns)
            x = margin_x + col * (card_w + gap_x)
            y = top_h + row * (card_h + gap_y)
            draw.rounded_rectangle(
                (x, y, x + card_w, y + card_h),
                radius=20 * scale,
                fill=(255, 255, 255, 218),
                outline=(255, 255, 255, 245),
                width=scale,
            )
            accent_h = max(18 * scale, int((card_h - 26 * scale) * count / max_count))
            draw.rounded_rectangle(
                (
                    x + 14 * scale,
                    y + card_h - 13 * scale - accent_h,
                    x + 19 * scale,
                    y + card_h - 13 * scale,
                ),
                radius=3 * scale,
                fill="#5b8def",
            )
            rank = f"#{index + 1}"
            draw.text(
                (x + 30 * scale, y + 16 * scale), rank, font=rank_font, fill="#9aa9b8"
            )
            self._draw_usage_text(
                draw,
                (x + 30 * scale, y + 40 * scale),
                self.usage_stats.display_name(key, scope, group_id, data=stats_data),
                name_font,
                "#1f2d3d",
                card_w - 112 * scale,
            )
            count_text = f"{count} 次"
            count_box = draw.textbbox((0, 0), count_text, font=count_font)
            count_w = count_box[2] - count_box[0]
            badge_x = x + card_w - count_w - 34 * scale
            badge_box = (
                badge_x,
                y + 26 * scale,
                x + card_w - 18 * scale,
                y + 58 * scale,
            )
            draw.rounded_rectangle(badge_box, radius=16 * scale, fill="#eef5ff")
            self._draw_centered_text(draw, badge_box, count_text, count_font, "#3f78c8")
        output = io.BytesIO()
        image.convert("RGB").save(output, format="PNG")
        return output.getvalue(), "image/png"

    def render_disabled_memes(
        self, names: list[str], title: str = "屏蔽表情列表"
    ) -> tuple[bytes, str]:
        if Image is None or ImageDraw is None:
            raise RuntimeError("Pillow 不可用")
        scale = 2
        columns = 4
        card_w, card_h = 250 * scale, 70 * scale
        gap_x, gap_y = 22 * scale, 20 * scale
        margin_x, top_h, bottom = 58 * scale, 178 * scale, 58 * scale
        row_count = max(1, (len(names) + columns - 1) // columns) if names else 1
        width = margin_x * 2 + columns * card_w + (columns - 1) * gap_x
        height = top_h + row_count * card_h + (row_count - 1) * gap_y + bottom
        image = self._vertical_gradient(
            width, height, (248, 251, 255), (239, 245, 252)
        ).convert("RGBA")
        self._draw_soft_circle(
            image, (130 * scale, 80 * scale), 220 * scale, (145, 190, 255, 70)
        )
        self._draw_soft_circle(
            image, (width - 120 * scale, 130 * scale), 260 * scale, (255, 176, 211, 62)
        )
        self._draw_soft_circle(
            image, (width // 2, height + 20 * scale), 340 * scale, (176, 224, 210, 52)
        )
        draw = ImageDraw.Draw(image)
        title_font = self._load_usage_font(42 * scale)
        subtitle_font = self._load_usage_font(20 * scale)
        name_font = self._load_usage_font(21 * scale)
        rank_font = self._load_usage_font(14 * scale)
        title_box = draw.textbbox((0, 0), title, font=title_font)
        draw.text(
            ((width - (title_box[2] - title_box[0])) // 2, 42 * scale),
            title,
            font=title_font,
            fill="#14213d",
        )
        subtitle = f"已屏蔽 {len(names)} 个表情"
        subtitle_w, _ = self._text_size(draw, subtitle, subtitle_font)
        pill_box = (
            (width - subtitle_w - 52 * scale) // 2,
            103 * scale,
            (width + subtitle_w + 52 * scale) // 2,
            143 * scale,
        )
        draw.rounded_rectangle(
            pill_box,
            radius=20 * scale,
            fill=(255, 255, 255, 178),
            outline=(255, 255, 255, 230),
            width=scale,
        )
        self._draw_centered_text(draw, pill_box, subtitle, subtitle_font, "#52677d")
        self._draw_card_shadows(
            image,
            len(names),
            columns,
            margin_x,
            top_h,
            card_w,
            card_h,
            gap_x,
            gap_y,
            scale,
        )
        for index, name in enumerate(names):
            row, col = divmod(index, columns)
            x = margin_x + col * (card_w + gap_x)
            y = top_h + row * (card_h + gap_y)
            draw.rounded_rectangle(
                (x, y, x + card_w, y + card_h),
                radius=20 * scale,
                fill=(255, 255, 255, 218),
                outline=(255, 255, 255, 245),
                width=scale,
            )
            draw.rounded_rectangle(
                (
                    x + 14 * scale,
                    y + 16 * scale,
                    x + 19 * scale,
                    y + card_h - 16 * scale,
                ),
                radius=3 * scale,
                fill="#ef4444",
            )
            rank = f"#{index + 1}"
            draw.text(
                (x + 30 * scale, y + 14 * scale), rank, font=rank_font, fill="#9aa9b8"
            )
            clean_name = self.remove_emoji(name)
            self._draw_usage_text(
                draw,
                (x + 30 * scale, y + 34 * scale),
                clean_name,
                name_font,
                "#1f2d3d",
                card_w - 50 * scale,
            )
        output = io.BytesIO()
        image.convert("RGB").save(output, format="PNG")
        return output.getvalue(), "image/png"
