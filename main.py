import os
import re
import io
import json
import shlex
import base64
import random
import socket
import asyncio
import tempfile
import time
import ipaddress
import mimetypes
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

# 基础配置常量
SCREEN_SESSION = "meme-generator"
TEMP_IMAGE_TTL_SECONDS = 3600

DEFAULT_REPO_SPECS = [
    {
        "name": "meme_emoji",
        "url": "https://github.com/anyliew/meme_emoji",
        "data_subdir": os.path.join("meme_emoji", "emoji"),
    },
    {
        "name": "meme-generator-jj",
        "url": "https://github.com/jinjiao007/meme-generator-jj",
        "data_subdir": os.path.join("meme-generator-jj", "memes"),
    },
    {
        "name": "meme_emoji_nsfw",
        "url": "https://github.com/anyliew/meme_emoji_nsfw",
        "data_subdir": os.path.join("meme_emoji_nsfw", "emoji"),
    },
    {
        "name": "tudou-meme",
        "url": "https://github.com/LRZ9712/tudou-meme",
        "data_subdir": os.path.join("tudou-meme", "meme"),
    },
    {
        "name": "xiaoruan-meme",
        "url": "https://github.com/xiaoruange39/xiaoruan-meme",
        "data_subdir": os.path.join("xiaoruan-meme", "emoji"),
    },
    {
        "name": "meme-generator-contrib",
        "url": "https://github.com/MemeCrafters/meme-generator-contrib",
        "data_subdir": os.path.join("meme-generator-contrib", "memes"),
    },
]

QUOTE_PAIRS = {
    '"': '"',
    "'": "'",
    "`": "`",
    "“": "”",
    "‘": "’",
}


def _normalize_remote_path(path: str) -> str:
    return path.strip().replace("\\", "/")


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

    for index, char in enumerate(arg_string):
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
            elif char == in_quote:
                raise ArgSyntaxError(f"参数中第 {index} 个字符的引号不匹配：{char}")
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

    if current:
        args.append("".join(current))
    if in_quote:
        raise ArgSyntaxError(f"参数引号未闭合：{in_quote}")
    return args


class PokeToBotFilter(CustomFilter):
    def filter(self, event: AstrMessageEvent, cfg: CoreAstrBotConfig) -> bool:
        return MemeUpdater._is_poke_to_bot_event(event)


@register("meme_updater", "表情包数据更新与生成插件", "lantao", "1.1.1")
class MemeUpdater(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._meme_infos: dict[str, dict] = {}
        self._meme_shortcuts: list[dict] = []
        self._meme_info_lock = asyncio.Lock()
        self._meme_data_dir = os.path.join(StarTools.get_data_dir(), "memeapi")

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

    def _default_repos(self) -> list[dict]:
        repos = []
        for spec in DEFAULT_REPO_SPECS:
            data_dir = os.path.join(self._meme_data_dir, spec["data_subdir"])
            repos.append({
                "name": spec["name"],
                "url": spec["url"],
                "clone_dir": os.path.dirname(data_dir),
                "data_dir": data_dir,
            })
        return repos

    def _get_repos(self) -> list[dict]:
        default_repos = self._default_repos()
        repos = self.config.get("repo_list", default_repos)
        if not isinstance(repos, list):
            return default_repos

        result = []
        for repo in repos:
            if not isinstance(repo, dict):
                continue
            url = str(repo.get("url", "")).strip()
            data_dir = str(repo.get("data_dir", "")).strip()
            if not url or not data_dir:
                continue
            clone_dir = os.path.dirname(data_dir.rstrip("/"))
            name = os.path.basename(clone_dir) or url.rstrip("/").split("/")[-1].removesuffix(".git")
            result.append({
                "name": name,
                "url": url,
                "clone_dir": clone_dir,
                "data_dir": data_dir,
            })
        return result

    def _get_remote_enabled(self) -> bool:
        return bool(self.config.get("remote_enabled", False))

    def _get_remote_auth_mode(self) -> str:
        mode = str(self.config.get("remote_auth_mode", "私钥登录")).strip()
        return mode if mode in {"私钥登录", "密码登录"} else "私钥登录"

    def _get_remote_host(self) -> str:
        return str(self.config.get("remote_host", "")).strip()

    def _get_remote_user(self) -> str:
        return str(self.config.get("remote_user", "")).strip()

    def _get_remote_port(self) -> int:
        try:
            return int(self.config.get("remote_port", 22))
        except (TypeError, ValueError):
            return 22

    def _get_remote_password(self) -> str:
        return str(self.config.get("remote_password", "")).strip()

    def _get_remote_key_path(self) -> str:
        return str(self.config.get("remote_key_path", "")).strip()

    def _get_remote_workdir(self) -> str:
        return str(self.config.get("remote_workdir", self._meme_data_dir)).strip() or self._meme_data_dir

    def _get_docker_container(self) -> str:
        return str(self.config.get("docker_container", SCREEN_SESSION)).strip() or SCREEN_SESSION

    def _get_meme_api_base_url(self) -> str:
        return str(self.config.get("meme_api_base_url", "http://127.0.0.1:2233")).strip().rstrip("/")

    def _get_meme_request_timeout(self) -> int:
        try:
            return max(1, int(self.config.get("meme_request_timeout", 30)))
        except (TypeError, ValueError):
            return 30

    def _get_max_image_bytes(self) -> int:
        try:
            mb = int(self.config.get("meme_max_image_mb", 10))
            return max(1, mb) * 1024 * 1024
        except (TypeError, ValueError):
            return 10 * 1024 * 1024

    def _get_meme_info_concurrency(self) -> int:
        try:
            return max(1, int(self.config.get("meme_info_concurrency", 8)))
        except (TypeError, ValueError):
            return 8

    def _get_meme_shortcut_enabled(self) -> bool:
        return bool(self.config.get("meme_shortcut_enabled", True))

    def _get_meme_poke_random_enabled(self) -> bool:
        return bool(self.config.get("meme_poke_random_enabled", False))

    def _get_meme_auto_default_texts(self) -> bool:
        return bool(self.config.get("meme_auto_default_texts", True))

    def _get_meme_auto_sender_avatar(self) -> bool:
        return bool(self.config.get("meme_auto_sender_avatar", True))

    def _get_meme_refresh_verbose_log(self) -> bool:
        return bool(self.config.get("meme_refresh_verbose_log", False))

    def _get_meme_list_text_template(self) -> str:
        return str(self.config.get("meme_list_text_template", "{index}. {keywords}")).strip() or "{index}. {keywords}"

    def _get_meme_list_sort_by(self) -> str:
        sort_by = str(self.config.get("meme_list_sort_by", "创建时间")).strip()
        return sort_by if sort_by in {"名称", "关键词", "创建时间", "更新时间"} else "创建时间"

    def _get_meme_list_sort_reverse(self) -> bool:
        return bool(self.config.get("meme_list_sort_reverse", True))

    def _remote_mode_warning(self) -> str:
        return "⚠️ 远程服务器模式（实验性）"

    def _build_remote_cmd(self, cmd: str) -> tuple[str, dict[str, str]]:
        if self._get_remote_auth_mode() != "密码登录":
            return cmd, {}

        password = self._get_remote_password()
        askpass = tempfile.NamedTemporaryFile("w", delete=False, prefix="meme_updater_askpass_")
        askpass.write("#!/bin/sh\nexec printf '%s\\n' \"$SSHPASS\"\n")
        askpass.close()
        os.chmod(askpass.name, 0o700)
        env = {
            "DISPLAY": ":0",
            "SSH_ASKPASS": askpass.name,
            "SSH_ASKPASS_REQUIRE": "force",
            "SSHPASS": password,
        }
        return cmd, env

    def _ssh_base_cmd(self) -> str:
        host = self._get_remote_host()
        user = self._get_remote_user()
        port = self._get_remote_port()
        mode = self._get_remote_auth_mode()
        key_path = self._get_remote_key_path()
        destination = f"{user}@{host}" if user else host
        parts = ["ssh", "-p", str(port)]
        if mode == "私钥登录" and key_path:
            parts.extend(["-i", key_path])
        if mode == "密码登录":
            parts.extend(["-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no"])
        parts.append(destination)
        return " ".join(shlex.quote(part) for part in parts)

    def _shell_join(self, args: list[str]) -> str:
        return " ".join(shlex.quote(str(arg)) for arg in args)

    async def _run_remote_cmd(self, cmd: str) -> tuple[int, str]:
        remote_cmd, env = self._build_remote_cmd(cmd)
        ssh_cmd = f"{self._ssh_base_cmd()} {shlex.quote(remote_cmd)}"
        try:
            return await self._run_shell_cmd(ssh_cmd, env=env)
        finally:
            if "SSH_ASKPASS" in env:
                try:
                    os.unlink(env["SSH_ASKPASS"])
                except OSError:
                    pass

    async def _run_repo_cmd(self, cmd: list[str] | str, cwd: str = None, remote: bool = False) -> tuple[int, str]:
        if remote:
            remote_cmd = self._shell_join(cmd) if isinstance(cmd, list) else cmd
            if cwd:
                remote_cmd = f"cd {shlex.quote(cwd)} && {remote_cmd}"
            return await self._run_remote_cmd(remote_cmd)
        if isinstance(cmd, list):
            return await self._run_cmd(cmd, cwd=cwd)
        return await self._run_shell_cmd(cmd, cwd=cwd)

    async def _run_cmd(self, cmd: list[str], cwd: str = None, env: dict | None = None) -> tuple[int, str]:
        run_env = os.environ.copy()
        if env:
            run_env.update(env)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            env=run_env,
        )
        stdout, _ = await proc.communicate()
        return proc.returncode, stdout.decode("utf-8", errors="replace").strip()

    async def _run_shell_cmd(self, cmd: str, cwd: str = None, env: dict | None = None) -> tuple[int, str]:
        run_env = os.environ.copy()
        if env:
            run_env.update(env)
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            env=run_env,
        )
        stdout, _ = await proc.communicate()
        return proc.returncode, stdout.decode("utf-8", errors="replace").strip()

    def _format_time(self, dt: datetime) -> str:
        return f"{dt.year}/{dt.month}/{dt.day} {dt:%H:%M:%S}"

    def _format_commit_short(self, commit_info: str) -> str:
        return (commit_info or "").split()[0][:8] if commit_info else "unknown"

    def _get_owner_repo(self, url: str) -> str:
        repo = url.rstrip("/").removesuffix(".git").split("github.com/")[-1]
        return repo if "/" in repo else repo.split("/")[-1]

    async def _sync_repo(self, repo: dict, index: int, total: int) -> dict:
        clone_path = repo["clone_dir"]
        before_count = self._count_data_items(repo["data_dir"])
        owner_repo = repo.get("owner_repo") or self._get_owner_repo(repo["url"])
        remote_mode = self._get_remote_enabled()
        workdir = self._get_remote_workdir() if remote_mode else None

        lines = [f"📦 [{index}/{total}] 正在更新 {owner_repo}..."]

        if remote_mode:
            remote_base = _normalize_remote_path(workdir)
            remote_clone = _normalize_remote_path(clone_path)
            mkdir_cmd = f"mkdir -p {shlex.quote(remote_base)}"
            ret, output = await self._run_remote_cmd(mkdir_cmd)
            if ret != 0:
                lines.extend([f"❌ [{index}/{total}] {owner_repo} 无法创建远程工作目录", f"    {output[:300]}"])
                return {"status": "failed", "updated": False, "lines": lines}
            exists_cmd = f"test -d {shlex.quote(remote_clone + '/.git')}"
            ret, _ = await self._run_repo_cmd(exists_cmd, remote=True, cwd=remote_base)
        else:
            ret = 0 if os.path.isdir(os.path.join(clone_path, ".git")) else 1

        if ret == 0:
            branch_cmd = ["git", "rev-parse", "--abbrev-ref", "HEAD"]
            ret, branch = await self._run_repo_cmd(branch_cmd, cwd=clone_path, remote=remote_mode)
            if ret != 0:
                lines.extend([f"❌ [{index}/{total}] {owner_repo} 无法读取当前分支", f"    {branch[:300]}"])
                return {"status": "failed", "updated": False, "lines": lines}

            fetch_cmd = ["git", "fetch", "origin", branch]
            ret, output = await self._run_repo_cmd(fetch_cmd, cwd=clone_path, remote=remote_mode)
            if ret != 0:
                lines.extend([f"❌ [{index}/{total}] {owner_repo} git fetch 失败", f"    {output[:300]}"])
                return {"status": "failed", "updated": False, "lines": lines}

            ret, local_commit = await self._run_repo_cmd(["git", "rev-parse", "HEAD"], cwd=clone_path, remote=remote_mode)
            if ret != 0:
                lines.extend([f"❌ [{index}/{total}] {owner_repo} 无法读取本地版本", f"    {local_commit[:300]}"])
                return {"status": "failed", "updated": False, "lines": lines}

            ret, remote_commit = await self._run_repo_cmd(["git", "rev-parse", f"origin/{branch}"], cwd=clone_path, remote=remote_mode)
            if ret != 0:
                lines.extend([f"❌ [{index}/{total}] {owner_repo} 无法读取远端版本", f"    {remote_commit[:300]}"])
                return {"status": "failed", "updated": False, "lines": lines}

            local_short = self._format_commit_short(local_commit)
            remote_short = self._format_commit_short(remote_commit)

            if local_commit == remote_commit:
                lines.append(f"✅ [{index}/{total}] {owner_repo} 无更新 ({local_short})")
                return {"status": "success", "updated": False, "lines": lines}

            reset_cmd = ["git", "reset", "--hard", f"origin/{branch}"]
            ret, output = await self._run_repo_cmd(reset_cmd, cwd=clone_path, remote=remote_mode)
            if ret == 0:
                after_count = self._count_data_items(repo["data_dir"])
                added = max(after_count - before_count, 0)
                lines.extend([
                    f"✅ [{index}/{total}] {owner_repo} 更新完成 ({local_short} → {remote_short})",
                    f"    📁 新增 {added} 个 | {repo['data_dir']}",
                ])
                return {"status": "success", "updated": True, "lines": lines}

            lines.extend([f"❌ [{index}/{total}] {owner_repo} git reset 失败", f"    {output[:300]}"])
            return {"status": "failed", "updated": False, "lines": lines}

        clone_cmd = ["git", "clone", "--depth", "1", repo["url"], clone_path]
        ret, output = await self._run_repo_cmd(clone_cmd, cwd=workdir, remote=remote_mode)
        if ret == 0:
            after_count = self._count_data_items(repo["data_dir"])
            lines.extend([
                f"✅ [{index}/{total}] {owner_repo} 克隆完成",
                f"    📁 新增 {after_count} 个 | {repo['data_dir']}",
            ])
            return {"status": "success", "updated": True, "lines": lines}

        lines.extend([f"❌ [{index}/{total}] {owner_repo} git clone 失败", f"    {output[:300]}"])
        return {"status": "failed", "updated": False, "lines": lines}

    async def _restart_memeapi(self) -> dict:
        container = self._get_docker_container()
        remote_mode = self._get_remote_enabled()
        lines = [f"{self._remote_mode_warning()}", f"准备重启容器 {container}..."] if remote_mode else [f"准备重启容器 {container}..."]

        if remote_mode:
            workdir = _normalize_remote_path(self._get_remote_workdir())
            ret, output = await self._run_repo_cmd(
                ["docker", "inspect", container],
                cwd=workdir,
                remote=True,
            )
            if ret != 0:
                lines.extend([
                    f"⚠️ 远程容器未找到：{container}",
                    "    请先确认远端服务器已经部署了这个容器，并检查容器名是否填写正确。",
                    f"    {output[:300]}",
                ])
                return {"success": False, "lines": lines}

            ret, output = await self._run_repo_cmd(["docker", "restart", container], cwd=workdir, remote=True)
            if ret != 0:
                lines.extend(["❌ 重启容器失败", f"    {output[:300]}"])
                return {"success": False, "lines": lines}

            lines.append("⏳ 等待容器状态稳定...")
            await asyncio.sleep(2)
            ret, output = await self._run_repo_cmd(
                ["docker", "inspect", "-f", "{{.State.Running}}", container],
                cwd=workdir,
                remote=True,
            )
            if ret == 0 and output.strip().lower() == "true":
                lines.extend(["✅ 容器重启成功", f"🎉 {container} 已运行"])
                return {"success": True, "lines": lines}

            lines.extend(["❌ 容器未处于运行状态", f"    {output[:300]}"])
            return {"success": False, "lines": lines}

        ret, output = await self._run_cmd(["docker", "inspect", container])
        if ret != 0:
            lines.extend([
                f"⚠️ 未找到本机容器 {container}",
                "    请确认 Docker 上确实存在这个容器，并检查容器名是否填写正确。",
                f"    {output[:300]}",
            ])
            return {"success": False, "lines": lines}

        ret, output = await self._run_cmd(["docker", "restart", container])
        if ret != 0:
            lines.extend(["❌ 重启容器失败", f"    {output[:300]}"])
            return {"success": False, "lines": lines}

        lines.append("⏳ 等待容器状态稳定...")
        await asyncio.sleep(2)

        ret, output = await self._run_cmd(
            ["docker", "inspect", "-f", "{{.State.Running}}", container]
        )
        if ret == 0 and output.strip().lower() == "true":
            lines.extend(["✅ 容器重启成功", f"🎉 {container} 已运行"])
            return {"success": True, "lines": lines}

        lines.extend(["❌ 容器未处于运行状态", f"    {output[:300]}"])
        return {"success": False, "lines": lines}

    def _count_data_items(self, path: str) -> int:
        if not os.path.isdir(path):
            return 0
        return len([item for item in os.listdir(path) if not item.startswith(".")])

    async def _meme_get_json(self, paths: list[str], session: aiohttp.ClientSession | None = None):
        last_error = ""
        timeout = aiohttp.ClientTimeout(total=self._get_meme_request_timeout())
        for attempt in range(1, 4):
            if session:
                for path in paths:
                    try:
                        async with session.get(f"{self._get_meme_api_base_url()}{path}") as resp:
                            text = await resp.text()
                            if resp.status < 400:
                                return json.loads(text) if text else None
                            last_error = f"HTTP {resp.status}: {text[:200]}"
                    except Exception as e:
                        last_error = str(e)
            else:
                async with aiohttp.ClientSession(timeout=timeout) as new_session:
                    for path in paths:
                        try:
                            async with new_session.get(f"{self._get_meme_api_base_url()}{path}") as resp:
                                text = await resp.text()
                                if resp.status < 400:
                                    return json.loads(text) if text else None
                                last_error = f"HTTP {resp.status}: {text[:200]}"
                        except Exception as e:
                            last_error = str(e)
            if attempt < 3:
                logger.warning(f"meme API 请求失败，准备重试 {attempt}/3：{last_error}")
                await asyncio.sleep(attempt * 2)
        raise RuntimeError(last_error or "meme API 请求失败")

    async def _meme_post_image(self, paths: list[str], *, json_body: dict | None = None, form_body: aiohttp.FormData | None = None) -> tuple[bytes, str]:
        last_error = ""
        timeout = aiohttp.ClientTimeout(total=self._get_meme_request_timeout())
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for path in paths:
                try:
                    kwargs = {}
                    if form_body is not None:
                        kwargs["data"] = form_body
                    else:
                        kwargs["json"] = json_body or {}
                    async with session.post(f"{self._get_meme_api_base_url()}{path}", **kwargs) as resp:
                        data = await self._read_limited_response(resp)
                        content_type = resp.headers.get("Content-Type", "image/png").split(";", 1)[0]
                        if resp.status < 400:
                            return data, content_type
                        last_error = data.decode("utf-8", errors="replace")[:300]
                except Exception as e:
                    last_error = str(e)
        raise RuntimeError(last_error or "meme API 渲染失败")

    async def _meme_get_image(self, paths: list[str]) -> tuple[bytes, str]:
        last_error = ""
        timeout = aiohttp.ClientTimeout(total=self._get_meme_request_timeout())
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for path in paths:
                try:
                    async with session.get(f"{self._get_meme_api_base_url()}{path}") as resp:
                        data = await self._read_limited_response(resp)
                        content_type = resp.headers.get("Content-Type", "image/png").split(";", 1)[0]
                        if resp.status < 400:
                            return data, content_type
                        last_error = data.decode("utf-8", errors="replace")[:300]
                except Exception as e:
                    last_error = str(e)
        raise RuntimeError(last_error or "meme API 图片请求失败")

    async def _refresh_meme_infos(self, force: bool = False) -> int:
        async with self._meme_info_lock:
            if self.meme_infos and not force:
                return len(self.meme_infos)
            logger.info(f"刷新 meme API 表情信息: {self._get_meme_api_base_url()}")
            keys = await self._meme_get_json(["/memes/keys", "/keys"])
            if not isinstance(keys, list):
                raise RuntimeError("meme API 返回的 keys 格式不正确")

            total = len(keys)
            verbose_log = self._get_meme_refresh_verbose_log()
            logger.info(f"meme API 返回 {total} 个表情，开始加载详情")
            semaphore = asyncio.Semaphore(self._get_meme_info_concurrency())

            async def load_info(session: aiohttp.ClientSession, index: int, key: str) -> tuple[str, dict] | None:
                async with semaphore:
                    if verbose_log:
                        logger.info(f"[{index}/{total}] 获取表情信息: {key}")
                    try:
                        info = await self._meme_get_json([
                            f"/memes/{quote(key, safe='')}/info",
                            f"/memes/{quote(key, safe='')}/",
                            f"/memes/{quote(key, safe='')}",
                        ], session=session)
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

            timeout = aiohttp.ClientTimeout(total=self._get_meme_request_timeout())
            async with aiohttp.ClientSession(timeout=timeout) as session:
                results = await asyncio.gather(*(load_info(session, i + 1, str(key)) for i, key in enumerate(keys)))

            entries = [r for r in results if r is not None]
            self.meme_infos = dict(entries)
            self._refresh_meme_shortcuts()
            logger.info(f"meme API 表情信息刷新完成，共载入 {len(self.meme_infos)} 个表情")
            return len(self.meme_infos)

    def _refresh_meme_shortcuts(self):
        shortcuts = []
        for key, info in self.meme_infos.items():
            for keyword in info.get("keywords", []):
                shortcuts.append({"key": key, "regex": re.escape(str(keyword)), "args": [], "options": {}})
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
                if key == "turn":
                    options.update(self._direction_options_from_text(key, compact_key))
                elif key == "symmetry":
                    options.update(self._direction_options_from_text(key, compact_key))
                shortcuts.append({"key": key, "regex": regex, "args": args, "options": options})
                if "#" in shortcut_key:
                    shortcuts.append({"key": key, "regex": regex.replace("#", ""), "args": args, "options": options})
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

    def _get_message_args(self, event: AstrMessageEvent, command_name: str) -> str:
        message = getattr(event, "message_str", "") or ""
        message = message.strip()
        if not message:
            return ""
        parts = message.split(maxsplit=1)
        if parts and parts[0].lstrip("#/％%") == command_name:
            return parts[1] if len(parts) > 1 else ""
        return ""

    def _normalize_meme_options(self, raw_args: str) -> tuple[str, dict[str, object]]:
        options: dict[str, object] = {}
        tokens = []
        for token in split_arg_string(raw_args):
            if token in {"右", "#右"}:
                options["right"] = True
                options["direction"] = "right"
                continue
            if token in {"左", "#左"}:
                options["left"] = True
                options["direction"] = "left"
                continue
            if token in {"上", "#上"}:
                options["top"] = True
                options["direction"] = "top"
                continue
            if token in {"下", "#下"}:
                options["bottom"] = True
                options["direction"] = "bottom"
                continue
            if token.startswith("#") and len(token) > 1:
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
                return {"right": True}
            if compact.startswith("转左"):
                return {"left": True}
        if key == "symmetry":
            if compact.startswith("对称右"):
                return {"direction": "right"}
            if compact.startswith("对称左"):
                return {"direction": "left"}
            if compact.startswith("对称上"):
                return {"direction": "top"}
            if compact.startswith("对称下"):
                return {"direction": "bottom"}
        return {}

    def _avatar_url(self, user_id: str) -> str:
        return f"https://q4.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=640" if user_id else ""

    def _sender_id(self, event: AstrMessageEvent) -> str:
        message_obj = getattr(event, "message_obj", None)
        raw_message = getattr(message_obj, "raw_message", None)
        if isinstance(raw_message, dict):
            user_id = str(raw_message.get("user_id") or "").strip()
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

    def _bot_avatar_url(self, event: AstrMessageEvent) -> str:
        message_obj = getattr(event, "message_obj", None)
        raw_message = getattr(message_obj, "raw_message", None)
        bot_id = str(getattr(event, "self_id", "") or getattr(message_obj, "self_id", "") or "").strip()
        if not bot_id and isinstance(raw_message, dict):
            bot_id = str(raw_message.get("self_id", "")).strip()
        return self._avatar_url(bot_id)

    def _group_id(self, event: AstrMessageEvent) -> str:
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
            except Exception:
                pass
        call_action = getattr(bot, "call_action", None)
        if callable(call_action):
            try:
                results.append(await call_action(action, **params))
            except Exception:
                pass
        return results

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
        message_obj = getattr(event, "message_obj", None)
        raw_message = getattr(message_obj, "raw_message", None)
        bot_id = str(getattr(event, "self_id", "") or getattr(message_obj, "self_id", "") or "").strip()
        if not bot_id and isinstance(raw_message, dict):
            bot_id = str(raw_message.get("self_id", "")).strip()
        return {"name": bot_id or "机器人", "gender": "unknown"}

    async def _read_limited_response(self, resp: aiohttp.ClientResponse, limit: int | None = None) -> bytes:
        max_bytes = limit or self._get_max_image_bytes()
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

    async def _validate_external_image_url(self, url: str) -> None:
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
        for info in infos:
            address = info[4][0]
            if self._is_forbidden_ip(address):
                raise RuntimeError("不允许访问解析到内网或本机的地址")

    async def _request_external_image(self, session: aiohttp.ClientSession, url: str) -> tuple[bytes, str]:
        current_url = url
        for _ in range(5):
            await self._validate_external_image_url(current_url)
            async with session.get(current_url, allow_redirects=False) as resp:
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
                return data, content_type
        raise RuntimeError("图片下载重定向次数过多")

    async def _download_image(self, url: str) -> tuple[bytes, str, str]:
        timeout = aiohttp.ClientTimeout(total=self._get_meme_request_timeout())
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
                if hasattr(segment, "chain"):
                    continue
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

    def _extract_message_text(self, event: AstrMessageEvent) -> str:
        message_str = str(getattr(event, "message_str", "") or "").strip()
        if message_str:
            return message_str
        message_obj = getattr(event, "message_obj", None)
        for value in (
            getattr(message_obj, "raw_message", None),
            getattr(message_obj, "message", None),
            getattr(event, "message", None),
        ):
            if isinstance(value, str):
                return value.strip()
        return ""

    def _raw_event_dict(self, event: AstrMessageEvent) -> dict:
        return self._raw_event_dict_from_event(event)

    def _stop_event(self, event: AstrMessageEvent):
        stop_event = getattr(event, "stop_event", None)
        if callable(stop_event):
            stop_event()

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
        if sender_id and sender_id not in user_ids and text.startswith("@"):
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
                avatar_urls.append(self._avatar_url(arg[1:]))
                avatar_user_infos.append({"name": arg[1:], "gender": "unknown"})
                continue
            texts.append(arg)
        at_ids = self._extract_message_at_ids(event)
        for user_id in at_ids:
            avatar_urls.append(self._avatar_url(user_id))
            avatar_user_infos.append({"name": user_id, "gender": "unknown"})
        image_urls.extend(avatar_urls)
        user_infos.extend(avatar_user_infos)
        if image_urls:
            logger.info(f"meme 参数图片来源：{image_urls}，当前发送者={self._sender_id(event)}")
        images = await asyncio.gather(*(self._download_image(url) for url in image_urls)) if image_urls else []
        return list(images), texts, user_infos

    async def _fill_sender_avatar_images(self, event: AstrMessageEvent, images: list[tuple[bytes, str, str]], user_infos: list[dict], target_count: int):
        if not self._get_meme_auto_sender_avatar() or len(images) >= target_count:
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
        if not self._get_meme_auto_sender_avatar() or images:
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
        fill_index = 0
        while len(images) < target_count:
            url, user_info = fill_items[fill_index % len(fill_items)]
            data, content_type, filename = await self._download_image(url)
            images.append((data, content_type, filename))
            user_infos.append(user_info)
            fill_index += 1

    def _select_render_images(self, images: list[tuple[bytes, str, str]], user_infos: list[dict], max_images: int) -> tuple[list[tuple[bytes, str, str]], list[dict]]:
        if max_images <= 0 or len(images) <= max_images:
            return images, user_infos
        selected_images = images[-max_images:]
        selected_user_infos = user_infos[-max_images:]
        return selected_images, selected_user_infos

    def _params_type(self, info: dict) -> dict:
        params = info.get("params_type") or {}
        return {
            "min_images": int(params.get("min_images", 0)),
            "max_images": int(params.get("max_images", 0)),
            "min_texts": int(params.get("min_texts", 0)),
            "max_texts": int(params.get("max_texts", 0)),
            "default_texts": list(params.get("default_texts") or []),
        }

    async def _render_meme(self, key: str, images: list[tuple[bytes, str, str]], texts: list[str], user_infos: list[dict], options: dict[str, object] | None = None) -> tuple[bytes, str]:
        form = aiohttp.FormData()
        for data, content_type, filename in images:
            form.add_field("images", data, filename=filename, content_type=content_type)
        for text in texts:
            # 确保 text 为字符串
            form.add_field("texts", str(text))
        render_args = {"user_infos": user_infos}
        if options:
            render_args.update(options)
        form.add_field("args", json.dumps(render_args, ensure_ascii=False))
        quoted_key = quote(str(key), safe="")
        return await self._meme_post_image([
            f"/memes/{quoted_key}/render",
            f"/memes/{quoted_key}/",
            f"/memes/{quoted_key}",
        ], form_body=form)

    def _parse_meme_time(self, value: object) -> float:
        if not value:
            return 0
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0

    def _sorted_meme_infos(self) -> list[dict]:
        sort_by = self._get_meme_list_sort_by()
        reverse = self._get_meme_list_sort_reverse()
        infos = list(self.meme_infos.values())
        if sort_by == "名称":
            return sorted(infos, key=lambda info: str(info.get("key", "")).lower(), reverse=reverse)
        if sort_by == "关键词":
            return sorted(infos, key=lambda info: str((info.get("keywords") or [""])[0]).lower(), reverse=reverse)
        if sort_by == "更新时间":
            return sorted(infos, key=lambda info: self._parse_meme_time(info.get("date_modified")), reverse=reverse)
        return sorted(infos, key=lambda info: self._parse_meme_time(info.get("date_created")), reverse=reverse)

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
        return await self._meme_post_image([
            "/memes/render_list",
            "/memes/list",
            "/render_list",
        ], json_body={
            "meme_list": meme_list,
            "text_template": self._get_meme_list_text_template(),
            "add_category_icon": True,
        })

    def _image_component(self, data: bytes, content_type: str) -> Comp.Image:
        ext = mimetypes.guess_extension(content_type) or ".png"
        try:
            temp_dir = os.path.join(self._meme_data_dir, "temp")
            os.makedirs(temp_dir, exist_ok=True)
            self._cleanup_temp_images(temp_dir)
            with tempfile.NamedTemporaryFile(suffix=ext, dir=temp_dir, delete=False) as tf:
                tf.write(data)
                temp_path = tf.name
            return Comp.Image(file=f"file:///{os.path.abspath(temp_path)}")
        except Exception as e:
            logger.warning(f"无法创建临时文件发送图片，降级到 base64: {e}")
            b64 = base64.b64encode(data).decode("ascii")
            return Comp.Image(file=f"base64://{b64}")

    def _cleanup_temp_images(self, temp_dir: str) -> None:
        expires_before = time.time() - TEMP_IMAGE_TTL_SECONDS
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
            return "", {"right": True}
        if compact in {"左", "#左"}:
            return "", {"left": True}
        return raw_args, {}

    @filter.command("重启memeapi")
    async def restart_memeapi(self, event: AstrMessageEvent):
        self._stop_event(event)
        yield event.plain_result("正在重启 memeapi 服务，请稍候...")
        result = await self._restart_memeapi()
        yield event.plain_result("\n".join(result["lines"]))

    @filter.command("更新表情包")
    async def update_memes(self, event: AstrMessageEvent):
        self._stop_event(event)
        yield event.plain_result("正在更新表情包数据，请稍候...")

        os.makedirs(self._meme_data_dir, exist_ok=True)

        started_at = datetime.now()
        repos = self._get_repos()
        total = len(repos)
        results = await asyncio.gather(*[self._sync_repo(repo, i + 1, total) for i, repo in enumerate(repos)])
        success = sum(1 for r in results if r["status"] == "success")
        success_updates = [r for r in results if r["status"] == "success" and r["updated"]]
        failed = sum(1 for r in results if r["status"] == "failed")
        updated = sum(1 for r in results if r["updated"])

        restart_result = await self._restart_memeapi() if failed == 0 else {"success": False, "lines": ["准备重启 memeapi...", "⚠️ 有仓库更新失败，已跳过重启"]}

        if restart_result["success"]:
            restart_result["lines"].append("⏳ 等待 meme API 启动后刷新表情信息...")
            await asyncio.sleep(5)
            try:
                count = await self._refresh_meme_infos(force=True)
                restart_result["lines"].append(f"✅ 已刷新表情信息，共载入 {count} 个表情")
            except Exception as e:
                restart_result["lines"].append(f"⚠️ 刷新表情信息失败：{e}")

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

        summary_lines.extend([
            f"📊 仓库更新统计: 成功 {success} | 失败 {failed} | 有更新 {updated}",
            "准备重启 memeapi...",
        ])

        summary_lines.extend(restart_result["lines"])
        summary_lines.append("========================")
        summary_lines.append(f"📌 重启状态: {'成功' if restart_result['success'] else '失败'}")

        yield event.plain_result("\n".join(summary_lines))

    @filter.command("表情包状态")
    async def meme_status(self, event: AstrMessageEvent):
        self._stop_event(event)
        lines = ["表情包仓库状态:"]

        repos = self._get_repos()

        for repo in repos:
            clone_path = repo["clone_dir"]
            data_path = repo["data_dir"]

            if not os.path.isdir(clone_path):
                lines.append(f"[未克隆] {repo['name']} | {data_path}")
                continue

            ret, commit_info = await self._run_cmd(
                ["git", "log", "-1", "--format=%h %s (%cr)"], cwd=clone_path
            )
            if ret == 0:
                count = self._count_data_items(data_path)
                lines.append(f"[已安装] {repo['name']} | {count} 项 | {data_path} | {commit_info}")
            else:
                lines.append(f"[异常] {repo['name']}: 无法读取 git 信息 | {data_path}")

        try:
            count = await self._refresh_meme_infos()
            lines.append(f"meme API: 已加载 {count} 个表情 | {self._get_meme_api_base_url()}")
        except Exception as e:
            lines.append(f"meme API: 无法连接或加载失败 | {self._get_meme_api_base_url()} | {e}")

        yield event.plain_result("\n".join(lines))

    @filter.command("刷新表情信息")
    async def refresh_meme_infos(self, event: AstrMessageEvent):
        self._stop_event(event)
        yield event.plain_result("正在刷新 meme API 表情信息...")
        try:
            count = await self._refresh_meme_infos(force=True)
            yield event.plain_result(f"刷新完成，共载入 {count} 个表情。")
        except Exception as e:
            yield event.plain_result(f"刷新失败：{e}")

    @filter.command("表情列表")
    async def meme_list(self, event: AstrMessageEvent):
        self._stop_event(event)
        try:
            await self._refresh_meme_infos()
            image, content_type = await self._render_list()
            yield event.chain_result([self._image_component(image, content_type)])
        except Exception as e:
            yield event.plain_result(f"获取表情列表失败：{e}")

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
            yield event.plain_result("\n".join(lines))
            try:
                key = quote(str(info.get("key")), safe="")
                image, content_type = await self._meme_get_image([f"/memes/{key}/preview", f"/memes/{key}/preview/"])
                yield event.chain_result([self._image_component(image, content_type)])
            except Exception:
                pass
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
            if self._get_meme_auto_sender_avatar() and len(images) < params["min_images"]:
                if images:
                    await self._fill_sender_avatar_images(event, images, user_infos, params["min_images"])
                else:
                    await self._fill_default_avatar_images(event, images, user_infos, params["min_images"])
            if self._get_meme_auto_default_texts() and not texts:
                texts.extend(str(v) for v in params["default_texts"])
            if not (params["min_images"] <= len(images) <= params["max_images"]):
                yield event.plain_result(f"图片数量不符，需要 {_format_range(params['min_images'], params['max_images'])} 张，当前 {len(images)} 张。")
                return
            if not (params["min_texts"] <= len(texts) <= params["max_texts"]):
                yield event.plain_result(f"文字数量不符，需要 {_format_range(params['min_texts'], params['max_texts'])} 段，当前 {len(texts)} 段。")
                return
            image, content_type = await self._render_meme(str(info.get("key")), images, texts, user_infos, options)
            yield event.chain_result([self._image_component(image, content_type)])
        except ArgSyntaxError as e:
            yield event.plain_result(str(e))
        except Exception as e:
            yield event.plain_result(f"制作表情失败：{e}")

    async def _random_meme_results(self, event: AstrMessageEvent, raw_args: str, resolve_args: bool = True):
        try:
            await self._refresh_meme_infos()
            raw_args, options = self._normalize_meme_options(raw_args)
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
                    image, content_type = await self._render_meme(str(info.get("key")), render_images, render_texts, render_user_infos, options)
                    keywords = _format_keywords([str(v) for v in info.get("keywords", [])])
                    yield event.chain_result([
                        Comp.Plain(f"关键词：{keywords}\n"),
                        self._image_component(image, content_type),
                    ])
                    return
                except Exception:
                    continue
            yield event.plain_result("没有找到适合当前参数的表情。")
        except Exception as e:
            yield event.plain_result(f"随机表情失败：{e}")

    @filter.custom_filter(PokeToBotFilter)
    async def meme_poke_random_listener(self, event: AstrMessageEvent):
        if not self._get_meme_poke_random_enabled():
            return
        async for result in self._random_meme_results(event, "", resolve_args=False):
            yield result

    @filter.event_message_type(EventMessageType.ALL)
    async def meme_shortcut_listener(self, event: AstrMessageEvent):
        content = self._extract_message_text(event)
        # 显式的随机指令即使关闭快捷匹配也应生效
        if content in {"随机表情", "随机meme", "随机 meme", "来个表情", "来张表情"}:
            self._stop_event(event)
            async for result in self._random_meme_results(event, ""):
                yield result
            return

        if not self._get_meme_shortcut_enabled():
            return

        if not content or content.startswith(("/", "#", "%", "％")):
            return
        try:
            await self._refresh_meme_infos()
            for shortcut in self.meme_shortcuts:
                try:
                    # 预检查正则语法，防止编译错误
                    regex = re.compile(f"^{shortcut['regex']}")
                    match = regex.match(content)
                except Exception as e:
                    logger.debug(f"快捷正则匹配跳过: {shortcut.get('key')} - {e}")
                    continue
                if not match:
                    continue
                tail = content[match.end():].strip()
                resolved_args = " ".join(value for value in [self._shortcut_args(shortcut["args"], match), tail] if value).strip()
                info = self.meme_infos.get(shortcut["key"])
                if not info:
                    continue
                params = self._params_type(info)
                resolved_args, options = self._normalize_meme_options(resolved_args)
                options = {**shortcut.get("options", {}), **options}
                content_options = self._direction_options_from_text(str(info.get("key")), content)
                if content_options:
                    # 如果内容中自带方向，覆盖快捷指令中的方向
                    for d in ["left", "right", "top", "bottom", "direction"]:
                        options.pop(d, None)
                    options.update(content_options)
                images, texts, user_infos = await self._resolve_generate_args(event, resolved_args)

                # 针对特定表情的参数微调
                if params["max_images"] == 2 and str(info.get("key")) != "miragetank" and len(images) >= 3:
                    images = [images[0], images[-1]]
                    user_infos = [user_infos[0], user_infos[-1]]
                elif len(images) > params["max_images"]:
                    images, user_infos = self._select_render_images(images, user_infos, params["max_images"])

                if self._get_meme_auto_sender_avatar() and len(images) < params["min_images"]:
                    if images:
                        await self._fill_sender_avatar_images(event, images, user_infos, params["min_images"])
                    else:
                        await self._fill_default_avatar_images(event, images, user_infos, params["min_images"])

                if self._get_meme_auto_default_texts() and not texts:
                    texts.extend(str(v) for v in params["default_texts"])

                if not (params["min_images"] <= len(images) <= params["max_images"]):
                    continue
                if not (params["min_texts"] <= len(texts) <= params["max_texts"]):
                    continue

                image, content_type = await self._render_meme(str(info.get("key")), images, texts, user_infos, options)
                self._stop_event(event)
                yield event.chain_result([self._image_component(image, content_type)])
                return
        except Exception as e:
            logger.warning(f"处理表情快捷指令异常: {e}")
            return
