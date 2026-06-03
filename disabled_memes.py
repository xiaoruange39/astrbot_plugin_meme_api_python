import re
from dataclasses import dataclass

from astrbot.api import logger


_EMOJI_PATTERN = re.compile(
    "["
    "\U0001f1e6-\U0001f1ff"
    "\U0001f300-\U0001f5ff"
    "\U0001f600-\U0001f64f"
    "\U0001f680-\U0001f6ff"
    "\U0001f700-\U0001f77f"
    "\U0001f780-\U0001f7ff"
    "\U0001f800-\U0001f8ff"
    "\U0001f900-\U0001f9ff"
    "\U0001fa00-\U0001faff"
    "]+",
    flags=re.UNICODE,
)


@dataclass
class DisabledMemeResult:
    status: str
    display_name: str = ""
    count: int = 0


class DisabledMemeManager:
    def __init__(self, config, plugin_config):
        self.config = config
        self.plugin_config = plugin_config

    def is_meme_disabled(self, key: str, info: dict, disabled_names: set[str]) -> bool:
        if not disabled_names:
            return False
        if key in disabled_names:
            return True
        return any(
            str(keyword).strip() in disabled_names
            for keyword in info.get("keywords", [])
        )

    def find_meme_in_infos(
        self, query: str, meme_infos: dict[str, dict]
    ) -> dict | None:
        query = query.strip()
        if query in meme_infos:
            return meme_infos[query]
        lowered = query.lower()
        for info in meme_infos.values():
            for field in ("keywords", "tags"):
                if any(str(value).lower() == lowered for value in info.get(field, [])):
                    return info
            for shortcut in info.get("shortcuts", []):
                if not isinstance(shortcut, dict):
                    continue
                value = shortcut.get("humanized") or shortcut.get("key")
                if value is not None and str(value).lower() == lowered:
                    return info
        return None

    def meme_display_name(self, info: dict) -> str:
        for value in info.get("keywords", []):
            keyword = str(value).strip()
            if keyword and self.remove_emoji(keyword):
                return keyword
        keywords = [
            str(value).strip()
            for value in info.get("keywords", [])
            if str(value).strip()
        ]
        return keywords[0] if keywords else str(info.get("key", ""))

    def remove_emoji(self, text: str) -> str:
        return _EMOJI_PATTERN.sub("", text).replace("️", "").replace("‍", "").strip()

    def _save_config(self) -> None:
        save_config = getattr(self.config, "save_config", None)
        if not callable(save_config):
            logger.warning(
                "当前 AstrBot 配置对象不支持 save_config，屏蔽表情列表可能无法持久化"
            )
            return
        try:
            save_config()
        except Exception as e:
            logger.error(f"保存屏蔽表情配置失败: {e}")

    def _disabled_group_entries(self) -> list[dict]:
        entries = self.config.get("meme_disabled_groups", [])
        return entries if isinstance(entries, list) else []

    def _group_keywords(self, group_id: str) -> list[str]:
        group_id = str(group_id or "").strip()
        for item in self._disabled_group_entries():
            if (
                isinstance(item, dict)
                and str(item.get("group_id", "")).strip() == group_id
            ):
                keywords = item.get("keywords", [])
                if isinstance(keywords, str):
                    return [value for value in re.split(r"[\s,，]+", keywords) if value]
                if isinstance(keywords, list):
                    return [
                        str(value).strip() for value in keywords if str(value).strip()
                    ]
                return []
        return []

    def _set_group_keywords(self, group_id: str, keywords: list[str]) -> None:
        group_id = str(group_id or "").strip()
        entries = [
            item for item in self._disabled_group_entries() if isinstance(item, dict)
        ]
        for item in entries:
            if str(item.get("group_id", "")).strip() == group_id:
                item["group_id"] = group_id
                item["keywords"] = keywords
                self.config["meme_disabled_groups"] = entries
                self._save_config()
                return
        entries.append(
            {
                "__template_key": "disabled_group_item",
                "group_id": group_id,
                "keywords": keywords,
            }
        )
        self.config["meme_disabled_groups"] = entries
        self._save_config()

    def _global_keywords(self) -> list[str]:
        return sorted(self.plugin_config.disabled_meme_names())

    def _set_global_keywords(self, keywords: list[str]) -> None:
        self.config["meme_disabled_keys"] = keywords
        self._save_config()

    def _scope_keywords(self, group_id: str) -> list[str]:
        return self._group_keywords(group_id) if group_id else self._global_keywords()

    def _set_scope_keywords(self, group_id: str, keywords: list[str]) -> None:
        if group_id:
            self._set_group_keywords(group_id, keywords)
        else:
            self._set_global_keywords(keywords)

    def disable(
        self, group_id: str, name: str, meme_infos: dict[str, dict]
    ) -> DisabledMemeResult:
        info = self.find_meme_in_infos(name, meme_infos)
        if not info:
            return DisabledMemeResult("not_found")
        key = str(info.get("key", name))
        display_name = self.meme_display_name(info)
        current = self._scope_keywords(group_id)
        if self.is_meme_disabled(key, info, set(current)):
            return DisabledMemeResult("already_disabled", display_name, len(current))
        current.append(display_name)
        self._set_scope_keywords(group_id, current)
        return DisabledMemeResult("disabled", display_name, len(current))

    def enable(
        self, group_id: str, name: str, all_meme_infos: dict[str, dict]
    ) -> DisabledMemeResult:
        info = self.find_meme_in_infos(name, all_meme_infos)
        if not info:
            return DisabledMemeResult("not_found")
        key = str(info.get("key", name))
        display_name = self.meme_display_name(info)
        candidates = [
            key,
            *[
                str(value).strip()
                for value in info.get("keywords", [])
                if str(value).strip()
            ],
        ]
        current = self._scope_keywords(group_id)
        removed = next((value for value in candidates if value in current), None)
        if not removed:
            return DisabledMemeResult("not_disabled", display_name, len(current))
        current.remove(removed)
        self._set_scope_keywords(group_id, current)
        return DisabledMemeResult("enabled", display_name, len(current))

    def disabled_display_names(
        self, all_meme_infos: dict[str, dict], disabled_names: set[str]
    ) -> list[str]:
        names = []
        for disabled_key in sorted(disabled_names):
            info = all_meme_infos.get(disabled_key)
            if not info:
                for key, candidate in all_meme_infos.items():
                    if self.is_meme_disabled(key, candidate, {disabled_key}):
                        info = candidate
                        break
            names.append(self.meme_display_name(info) if info else disabled_key)
        return names
