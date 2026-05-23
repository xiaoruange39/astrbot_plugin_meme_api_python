import asyncio
import os
import platform
import posixpath
import shlex
import tempfile
from urllib.parse import urlparse

from .plugin_config import (
    MemePluginConfig,
    is_safe_ssh_arg,
    normalize_remote_path,
    posix_join,
)

COMMAND_TIMEOUT_SECONDS = 120


class MemeRepoManager:
    def __init__(self, plugin_config: MemePluginConfig, meme_data_dir: str):
        self.config = plugin_config
        self.meme_data_dir = meme_data_dir

    def repos(self) -> list[dict]:
        return self.config.repos()

    def _remote_repo_paths(self, repo: dict) -> tuple[str, str]:
        remote_workdir = normalize_remote_path(self.config.remote_workdir())
        data_subdir = str(repo.get("data_subdir") or "").strip()
        data_leaf = str(repo.get("data_leaf") or "").strip()
        remote_clone_dir = posix_join(remote_workdir, data_subdir)
        remote_data_dir = (
            posix_join(remote_clone_dir, data_leaf) if data_leaf else remote_clone_dir
        )
        return remote_clone_dir, remote_data_dir

    def _remote_mode_warning(self) -> str:
        return "⚠️ 远程服务器模式（实验性）"

    def _build_remote_cmd(self, cmd: str) -> tuple[str, dict[str, str]]:
        if self.config.remote_auth_mode() != "密码登录":
            return cmd, {}

        password = self.config.remote_password()
        askpass = tempfile.NamedTemporaryFile(
            "w", delete=False, prefix="meme_updater_askpass_"
        )
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

    def _ssh_base_args(self) -> list[str]:
        host = self.config.remote_host()
        user = self.config.remote_user()
        port = self.config.remote_port()
        mode = self.config.remote_auth_mode()
        key_path = self.config.remote_key_path()
        if not is_safe_ssh_arg(host):
            raise ValueError("远程主机配置不合法")
        if user and not is_safe_ssh_arg(user):
            raise ValueError("远程用户配置不合法")
        if key_path and not is_safe_ssh_arg(key_path):
            raise ValueError("远程私钥路径配置不合法")
        destination = f"{user}@{host}" if user else host
        args = ["ssh", "-p", str(port)]
        if mode == "私钥登录" and key_path:
            args.extend(["-i", key_path])
        if mode == "密码登录":
            args.extend(
                [
                    "-o",
                    "PreferredAuthentications=password",
                    "-o",
                    "PubkeyAuthentication=no",
                ]
            )
        args.extend(["--", destination])
        return args

    def _shell_join(self, args: list[str]) -> str:
        return " ".join(shlex.quote(str(arg)) for arg in args)

    async def _run_remote_cmd(
        self, cmd: str, timeout: int = COMMAND_TIMEOUT_SECONDS
    ) -> tuple[int, str]:
        if (
            self.config.remote_auth_mode() == "密码登录"
            and platform.system() == "Windows"
        ):
            return -1, "Windows 环境不支持 SSH_ASKPASS 密码登录，请改用私钥登录"
        remote_cmd, env = self._build_remote_cmd(cmd)
        try:
            return await self._run_cmd(
                [*self._ssh_base_args(), remote_cmd], env=env, timeout=timeout
            )
        finally:
            if "SSH_ASKPASS" in env:
                try:
                    os.unlink(env["SSH_ASKPASS"])
                except OSError:
                    pass

    def _get_repo_cmd_timeout(self, cmd: list[str] | str) -> int:
        if isinstance(cmd, str):
            normalized = cmd.lower()
        else:
            normalized = " ".join(str(part).lower() for part in cmd)
        if "git clone" in normalized or "git fetch" in normalized:
            return max(COMMAND_TIMEOUT_SECONDS, 600)
        return COMMAND_TIMEOUT_SECONDS

    async def _run_repo_cmd(
        self, cmd: list[str] | str, cwd: str | None = None, remote: bool = False
    ) -> tuple[int, str]:
        timeout = self._get_repo_cmd_timeout(cmd)
        if remote:
            remote_cmd = self._shell_join(cmd) if isinstance(cmd, list) else cmd
            if cwd:
                remote_cmd = f"cd {shlex.quote(cwd)} && {remote_cmd}"
            return await self._run_remote_cmd(remote_cmd, timeout=timeout)
        if isinstance(cmd, list):
            return await self._run_cmd(cmd, cwd=cwd, timeout=timeout)
        return await self._run_shell_cmd(cmd, cwd=cwd, timeout=timeout)

    async def _run_cmd(
        self,
        cmd: list[str],
        cwd: str | None = None,
        env: dict | None = None,
        timeout: int = COMMAND_TIMEOUT_SECONDS,
    ) -> tuple[int, str]:
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
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return -1, f"命令执行超时（{timeout}秒）"
        return int(proc.returncode or 0), stdout.decode(
            "utf-8", errors="replace"
        ).strip()

    async def _run_shell_cmd(
        self,
        cmd: str,
        cwd: str | None = None,
        env: dict | None = None,
        timeout: int = COMMAND_TIMEOUT_SECONDS,
    ) -> tuple[int, str]:
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
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return -1, f"命令执行超时（{timeout}秒）"
        return int(proc.returncode or 0), stdout.decode(
            "utf-8", errors="replace"
        ).strip()

    def _format_commit_short(self, commit_info: str) -> str:
        return (commit_info or "").split()[0][:8] if commit_info else "unknown"

    def _meaningful_status_lines(self, status_output: str) -> list[str]:
        lines = []
        for line in status_output.splitlines():
            path = (
                line[3:].strip().strip('"')
                if len(line) > 3
                else line.strip().strip('"')
            )
            if (
                path.endswith(".pyc")
                or "/__pycache__/" in path
                or path.endswith("/__pycache__/")
            ):
                continue
            lines.append(line)
        return lines

    def _get_owner_repo(self, url: str) -> str:
        path = urlparse(url).path.strip("/")
        path = path.removesuffix(".git")
        parts = path.split("/")
        return (
            "/".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else url)
        )

    async def sync_repo(self, repo: dict, index: int, total: int) -> dict:
        clone_path = repo["clone_dir"]
        data_path = repo["data_dir"]
        before_count = 0
        owner_repo = repo.get("owner_repo") or self._get_owner_repo(repo["url"])
        remote_mode = self.config.remote_enabled()
        workdir = self.config.remote_workdir() if remote_mode else None

        lines = [f"📦 [{index}/{total}] 正在更新 {owner_repo}..."]

        if remote_mode:
            remote_clone, remote_data = self._remote_repo_paths(repo)
            remote_clone_parent = posixpath.dirname(remote_clone)
            mkdir_cmd = f"mkdir -p {shlex.quote(remote_clone_parent)}"
            ret, output = await self._run_remote_cmd(mkdir_cmd)
            if ret != 0:
                lines.extend(
                    [
                        f"❌ [{index}/{total}] {owner_repo} 无法创建远程仓库目录",
                        f"    {output[:300]}",
                    ]
                )
                return {"status": "failed", "updated": False, "lines": lines}
            exists_cmd = f"test -d {shlex.quote(remote_clone + '/.git')}"
            ret, _ = await self._run_repo_cmd(
                exists_cmd, remote=True, cwd=remote_clone_parent
            )
            repo_clone_path = remote_clone
            repo_data_path = remote_data
            before_count = await self._count_remote_data_items(repo_data_path)
        else:
            ret = 0 if os.path.isdir(os.path.join(clone_path, ".git")) else 1
            repo_clone_path = clone_path
            repo_data_path = data_path
            before_count = self._count_data_items(repo_data_path)

        if ret == 0:
            branch_cmd = ["git", "rev-parse", "--abbrev-ref", "HEAD"]
            ret, branch = await self._run_repo_cmd(
                branch_cmd, cwd=repo_clone_path, remote=remote_mode
            )
            if ret != 0:
                lines.extend(
                    [
                        f"❌ [{index}/{total}] {owner_repo} 无法读取当前分支",
                        f"    {branch[:300]}",
                    ]
                )
                return {"status": "failed", "updated": False, "lines": lines}

            fetch_cmd = ["git", "fetch", "--depth", "1", "origin", branch]
            ret, output = await self._run_repo_cmd(
                fetch_cmd, cwd=repo_clone_path, remote=remote_mode
            )
            if ret != 0:
                lines.extend(
                    [
                        f"❌ [{index}/{total}] {owner_repo} git fetch 失败",
                        f"    {output[:300]}",
                    ]
                )
                return {"status": "failed", "updated": False, "lines": lines}

            ret, local_commit = await self._run_repo_cmd(
                ["git", "rev-parse", "HEAD"], cwd=repo_clone_path, remote=remote_mode
            )
            if ret != 0:
                lines.extend(
                    [
                        f"❌ [{index}/{total}] {owner_repo} 无法读取本地版本",
                        f"    {local_commit[:300]}",
                    ]
                )
                return {"status": "failed", "updated": False, "lines": lines}

            ret, remote_commit = await self._run_repo_cmd(
                ["git", "rev-parse", f"origin/{branch}"],
                cwd=repo_clone_path,
                remote=remote_mode,
            )
            if ret != 0:
                lines.extend(
                    [
                        f"❌ [{index}/{total}] {owner_repo} 无法读取远端版本",
                        f"    {remote_commit[:300]}",
                    ]
                )
                return {"status": "failed", "updated": False, "lines": lines}

            local_short = self._format_commit_short(local_commit)
            remote_short = self._format_commit_short(remote_commit)

            status_cmd = ["git", "status", "--porcelain"]
            ret, status_output = await self._run_repo_cmd(
                status_cmd, cwd=repo_clone_path, remote=remote_mode
            )
            if ret != 0:
                lines.extend(
                    [
                        f"❌ [{index}/{total}] {owner_repo} 无法检查本地修改",
                        f"    {status_output[:300]}",
                    ]
                )
                return {"status": "failed", "updated": False, "lines": lines}

            meaningful_status_lines = self._meaningful_status_lines(status_output)
            local_dirty = bool(meaningful_status_lines)
            if local_dirty:
                status_preview = "\n".join(meaningful_status_lines)[:300]
                lines.extend(
                    [
                        f"⚠️ [{index}/{total}] {owner_repo} 检测到本地文件变更，准备恢复到远端版本",
                        f"    {status_preview}",
                    ]
                )

            if local_commit == remote_commit and not local_dirty:
                lines.append(
                    f"✅ [{index}/{total}] {owner_repo} 无更新 ({local_short})"
                )
                return {"status": "success", "updated": False, "lines": lines}

            reset_cmd = ["git", "reset", "--hard", f"origin/{branch}"]
            ret, output = await self._run_repo_cmd(
                reset_cmd, cwd=repo_clone_path, remote=remote_mode
            )
            if ret == 0:
                after_count = (
                    await self._count_remote_data_items(repo_data_path)
                    if remote_mode
                    else self._count_data_items(repo_data_path)
                )
                added = max(after_count - before_count, 0)
                action = "本地恢复完成" if local_commit == remote_commit else "更新完成"
                lines.extend(
                    [
                        f"✅ [{index}/{total}] {owner_repo} {action} ({local_short} → {remote_short})",
                        f"    📁 新增 {added} 个 | {repo_data_path}",
                    ]
                )
                return {"status": "success", "updated": True, "lines": lines}

            lines.extend(
                [
                    f"❌ [{index}/{total}] {owner_repo} git reset 失败",
                    f"    {output[:300]}",
                ]
            )
            return {"status": "failed", "updated": False, "lines": lines}

        clone_cmd = ["git", "clone", "--depth", "1", repo["url"], repo_clone_path]
        ret, output = await self._run_repo_cmd(
            clone_cmd,
            cwd=workdir if not remote_mode else posixpath.dirname(repo_clone_path),
            remote=remote_mode,
        )
        if ret == 0:
            after_count = (
                await self._count_remote_data_items(repo_data_path)
                if remote_mode
                else self._count_data_items(repo_data_path)
            )
            lines.extend(
                [
                    f"✅ [{index}/{total}] {owner_repo} 克隆完成",
                    f"    📁 新增 {after_count} 个 | {repo_data_path}",
                ]
            )
            return {"status": "success", "updated": True, "lines": lines}

        lines.extend(
            [f"❌ [{index}/{total}] {owner_repo} git clone 失败", f"    {output[:300]}"]
        )
        if not remote_mode and not os.path.isdir(os.path.join(clone_path, ".git")):
            try:
                os.rmdir(clone_path)
            except OSError:
                pass
        return {"status": "failed", "updated": False, "lines": lines}

    async def restart_memeapi(self) -> dict:
        container = self.config.docker_container()
        remote_mode = self.config.remote_enabled()
        lines = (
            [f"{self._remote_mode_warning()}", f"准备重启容器 {container}..."]
            if remote_mode
            else [f"准备重启容器 {container}..."]
        )

        if remote_mode:
            workdir = normalize_remote_path(self.config.remote_workdir())
            ret, output = await self._run_repo_cmd(
                ["docker", "inspect", container],
                cwd=workdir,
                remote=True,
            )
            if ret != 0:
                lines.extend(
                    [
                        f"⚠️ 远程容器未找到：{container}",
                        "    请先确认远端服务器已经部署了这个容器，并检查容器名是否填写正确。",
                        f"    {output[:300]}",
                    ]
                )
                return {"success": False, "lines": lines}

            ret, output = await self._run_repo_cmd(
                ["docker", "restart", container], cwd=workdir, remote=True
            )
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
            lines.extend(
                [
                    f"⚠️ 未找到本机容器 {container}",
                    "    请确认 Docker 上确实存在这个容器，并检查容器名是否填写正确。",
                    f"    {output[:300]}",
                ]
            )
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

    async def repo_status(self, repo: dict) -> str:
        if self.config.remote_enabled():
            clone_path, data_path = self._remote_repo_paths(repo)
            ret, _ = await self._run_remote_cmd(f"test -d {shlex.quote(clone_path)}")
            if ret != 0:
                return f"[未克隆] {repo['name']} | {data_path}"

            ret, commit_info = await self._run_repo_cmd(
                ["git", "log", "-1", "--format=%h %s (%cr)"],
                cwd=clone_path,
                remote=True,
            )
            if ret == 0:
                count = await self._count_remote_data_items(data_path)
                return f"[已安装] {repo['name']} | {count} 项 | {data_path} | {commit_info}"
            return f"[异常] {repo['name']}: 无法读取 git 信息 | {data_path}"

        clone_path = repo["clone_dir"]
        data_path = repo["data_dir"]

        if not os.path.isdir(clone_path):
            return f"[未克隆] {repo['name']} | {data_path}"

        ret, commit_info = await self._run_cmd(
            ["git", "log", "-1", "--format=%h %s (%cr)"], cwd=clone_path
        )
        if ret == 0:
            count = self._count_data_items(data_path)
            return f"[已安装] {repo['name']} | {count} 项 | {data_path} | {commit_info}"
        return f"[异常] {repo['name']}: 无法读取 git 信息 | {data_path}"

    def _count_data_items(self, path: str) -> int:
        if not os.path.isdir(path):
            return 0
        return len([item for item in os.listdir(path) if not item.startswith(".")])

    async def _count_remote_data_items(self, path: str) -> int:
        ret, output = await self._run_remote_cmd(
            f"if [ -d {shlex.quote(path)} ]; then find {shlex.quote(path)} -mindepth 1 -maxdepth 1 ! -name '.*' | wc -l; else echo 0; fi"
        )
        if ret != 0:
            return 0
        try:
            return int(output.strip().splitlines()[-1])
        except (IndexError, ValueError):
            return 0
