import re
from dataclasses import dataclass


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
        return any(str(keyword).strip() in disabled_names for keyword in info.get("keywords", []))

    def find_meme_in_infos(self, query: str, meme_infos: dict[str, dict]) -> dict | None:
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
                if str(value).lower() == lowered:
                    return info
        return None

    def meme_display_name(self, info: dict) -> str:
        for value in info.get("keywords", []):
            keyword = str(value).strip()
            if keyword and self.remove_emoji(keyword):
                return keyword
        keywords = [str(value).strip() for value in info.get("keywords", []) if str(value).strip()]
        return keywords[0] if keywords else str(info.get("key", ""))

    def remove_emoji(self, text: str) -> str:
        emoji_pattern = re.compile(
            "["
            "\U0001F1E6-\U0001F1FF"
            "\U0001F300-\U0001F5FF"
            "\U0001F600-\U0001F64F"
            "\U0001F680-\U0001F6FF"
            "\U0001F700-\U0001F77F"
            "\U0001F780-\U0001F7FF"
            "\U0001F800-\U0001F8FF"
            "\U0001F900-\U0001F9FF"
            "\U0001FA00-\U0001FAFF"
            "]+",
            flags=re.UNICODE,
        )
        return emoji_pattern.sub("", text).replace("️", "").replace("‍", "").strip()

    def disable(self, name: str, meme_infos: dict[str, dict]) -> DisabledMemeResult:
        info = self.find_meme_in_infos(name, meme_infos)
        if not info:
            return DisabledMemeResult("not_found")
        key = str(info.get("key", name))
        display_name = self.meme_display_name(info)
        current = list(self.plugin_config.disabled_meme_names())
        if self.is_meme_disabled(key, info, set(current)):
            return DisabledMemeResult("already_disabled", display_name, len(current))
        current.append(display_name)
        self.config["meme_disabled_keys"] = current
        return DisabledMemeResult("disabled", display_name, len(current))

    def enable(self, name: str, all_meme_infos: dict[str, dict]) -> DisabledMemeResult:
        info = self.find_meme_in_infos(name, all_meme_infos)
        if not info:
            return DisabledMemeResult("not_found")
        key = str(info.get("key", name))
        display_name = self.meme_display_name(info)
        candidates = [key, *[str(value).strip() for value in info.get("keywords", []) if str(value).strip()]]
        current = list(self.plugin_config.disabled_meme_names())
        removed = next((value for value in candidates if value in current), None)
        if not removed:
            return DisabledMemeResult("not_disabled", display_name, len(current))
        current.remove(removed)
        self.config["meme_disabled_keys"] = current
        return DisabledMemeResult("enabled", display_name, len(current))

    def disabled_display_names(self, all_meme_infos: dict[str, dict]) -> list[str]:
        names = []
        for disabled_key in sorted(self.plugin_config.disabled_meme_names()):
            info = all_meme_infos.get(disabled_key)
            if not info:
                for key, candidate in all_meme_infos.items():
                    if self.is_meme_disabled(key, candidate, {disabled_key}):
                        info = candidate
                        break
            names.append(self.meme_display_name(info) if info else disabled_key)
        return names
