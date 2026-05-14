import os
import re
import posixpath
from urllib.parse import urlparse

SCREEN_SESSION = "meme-generator"

DEFAULT_REPO_SPECS = [
    {
        "name": "meme_emoji",
        "url": "https://github.com/anyliew/meme_emoji",
        "data_subdir": "meme_emoji/emoji",
    },
    {
        "name": "meme-generator-jj",
        "url": "https://github.com/jinjiao007/meme-generator-jj",
        "data_subdir": "meme-generator-jj/memes",
    },
    {
        "name": "meme_emoji_nsfw",
        "url": "https://github.com/anyliew/meme_emoji_nsfw",
        "data_subdir": "meme_emoji_nsfw/emoji",
    },
    {
        "name": "tudou-meme",
        "url": "https://github.com/LRZ9712/tudou-meme",
        "data_subdir": "tudou-meme/meme",
    },
    {
        "name": "xiaoruan-meme",
        "url": "https://github.com/xiaoruange39/xiaoruan-meme",
        "data_subdir": "xiaoruan-meme/emoji",
    },
    {
        "name": "meme-generator-contrib",
        "url": "https://github.com/MemeCrafters/meme-generator-contrib",
        "data_subdir": "meme-generator-contrib/memes",
    },
]


def has_shell_control_chars(value: str) -> bool:
    return any(ord(char) < 32 or char in ";&|`$<>\n\r" for char in value)


def is_safe_relative_path(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    return bool(path) and not has_shell_control_chars(path) and not os.path.isabs(path) and ".." not in parts


def is_safe_ssh_arg(value: str) -> bool:
    return bool(value) and not value.startswith("-") and not has_shell_control_chars(value)


def normalize_remote_path(path: str) -> str:
    return path.strip().replace("\\", "/")


def posix_join(base: str, *parts: str) -> str:
    path = base
    for part in parts:
        normalized = str(part).replace("\\", "/").strip("/")
        if normalized:
            path = posixpath.join(path, normalized)
    return path


def is_allowed_repo_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


class MemePluginConfig:
    def __init__(self, config, meme_data_dir: str):
        self.config = config
        self.meme_data_dir = meme_data_dir

    def _default_repos(self) -> list[dict]:
        repos = []
        for spec in DEFAULT_REPO_SPECS:
            data_subdir = spec["data_subdir"]
            clone_subdir = os.path.dirname(data_subdir)
            data_leaf = os.path.basename(data_subdir)
            clone_dir = os.path.join(self.meme_data_dir, clone_subdir)
            repos.append({
                "name": spec["name"],
                "url": spec["url"],
                "data_subdir": clone_subdir,
                "data_leaf": data_leaf,
                "clone_dir": clone_dir,
                "data_dir": os.path.join(clone_dir, data_leaf),
            })
        return repos

    def repos(self) -> list[dict]:
        default_repos = self._default_repos()
        repos = self.config.get("repo_list", default_repos)
        if not isinstance(repos, list):
            return default_repos

        result = []
        for repo in repos:
            if not isinstance(repo, dict):
                continue
            url = str(repo.get("url", "")).strip()
            data_subdir = str(repo.get("data_subdir") or repo.get("name") or "").strip()
            if not url or has_shell_control_chars(url) or not is_allowed_repo_url(url) or not is_safe_relative_path(data_subdir):
                continue
            clone_dir = os.path.join(self.meme_data_dir, data_subdir)
            data_leaf = str(repo.get("data_leaf", "")).strip()
            if data_leaf and not is_safe_relative_path(data_leaf):
                continue
            data_dir = os.path.join(clone_dir, data_leaf) if data_leaf else clone_dir
            name = os.path.basename(clone_dir) or url.rstrip("/").split("/")[-1].removesuffix(".git")
            result.append({
                "name": name,
                "url": url,
                "data_subdir": data_subdir,
                "data_leaf": data_leaf,
                "clone_dir": clone_dir,
                "data_dir": data_dir,
            })
        return result or default_repos

    def remote_enabled(self) -> bool:
        return bool(self.config.get("remote_enabled", False))

    def remote_auth_mode(self) -> str:
        mode = str(self.config.get("remote_auth_mode", "私钥登录")).strip()
        return mode if mode in {"私钥登录", "密码登录"} else "私钥登录"

    def remote_host(self) -> str:
        return str(self.config.get("remote_host", "")).strip()

    def remote_user(self) -> str:
        return str(self.config.get("remote_user", "")).strip()

    def remote_port(self) -> int:
        try:
            port = int(self.config.get("remote_port", 22))
            return port if 1 <= port <= 65535 else 22
        except (TypeError, ValueError):
            return 22

    def remote_password(self) -> str:
        return str(self.config.get("remote_password", "")).strip()

    def remote_key_path(self) -> str:
        return str(self.config.get("remote_key_path", "")).strip()

    def remote_workdir(self) -> str:
        workdir = str(self.config.get("remote_workdir", self.meme_data_dir)).strip() or self.meme_data_dir
        return self.meme_data_dir if has_shell_control_chars(workdir) else workdir

    def docker_container(self) -> str:
        container = str(self.config.get("docker_container", SCREEN_SESSION)).strip() or SCREEN_SESSION
        return SCREEN_SESSION if has_shell_control_chars(container) else container

    def repo_update_enabled(self) -> bool:
        return bool(self.config.get("repo_update_enabled", False))

    def meme_api_base_url(self) -> str:
        value = str(self.config.get("meme_api_base_url", "http://127.0.0.1:2233")).strip().rstrip("/")
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return "http://127.0.0.1:2233"
        return value

    def meme_request_timeout(self) -> int:
        try:
            return max(1, int(self.config.get("meme_request_timeout", 30)))
        except (TypeError, ValueError):
            return 30

    def max_image_bytes(self) -> int:
        try:
            mb = int(self.config.get("meme_max_image_mb", 10))
            return max(1, mb) * 1024 * 1024
        except (TypeError, ValueError):
            return 10 * 1024 * 1024

    def meme_info_concurrency(self) -> int:
        try:
            return min(4, max(1, int(self.config.get("meme_info_concurrency", 4))))
        except (TypeError, ValueError):
            return 4

    def repo_update_concurrency(self) -> int:
        try:
            return max(1, int(self.config.get("repo_update_concurrency", 2)))
        except (TypeError, ValueError):
            return 2

    def meme_shortcut_enabled(self) -> bool:
        return bool(self.config.get("meme_shortcut_enabled", True))

    def meme_poke_random_enabled(self) -> bool:
        return bool(self.config.get("meme_poke_random_enabled", False))

    def meme_auto_default_texts(self) -> bool:
        return bool(self.config.get("meme_auto_default_texts", True))

    def meme_auto_sender_avatar(self) -> bool:
        return bool(self.config.get("meme_auto_sender_avatar", True))

    def meme_refresh_verbose_log(self) -> bool:
        return bool(self.config.get("meme_refresh_verbose_log", False))

    def disabled_meme_names(self) -> set[str]:
        value = self.config.get("meme_disabled_keys", [])
        if isinstance(value, str):
            items = re.split(r"[\s,，]+", value)
        elif isinstance(value, list):
            items = value
        else:
            items = []
        return {str(item).strip() for item in items if str(item).strip()}

    def meme_search_limit(self) -> int:
        try:
            return max(1, int(self.config.get("meme_search_limit", 30)))
        except (TypeError, ValueError):
            return 30

    def meme_search_forward_enabled(self) -> bool:
        return bool(self.config.get("meme_search_forward_enabled", True))

    def meme_list_text_template(self) -> str:
        return str(self.config.get("meme_list_text_template", "{index}. {keywords}")).strip() or "{index}. {keywords}"

    def meme_list_sort_by(self) -> str:
        sort_by = str(self.config.get("meme_list_sort_by", "创建时间")).strip()
        return sort_by if sort_by in {"名称", "关键词", "创建时间", "更新时间"} else "创建时间"

    def meme_list_sort_reverse(self) -> bool:
        return bool(self.config.get("meme_list_sort_reverse", True))
