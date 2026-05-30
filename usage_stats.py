import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from quart import jsonify, request

from astrbot.api import logger

JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8"}


class MemeUsageStats:
    def __init__(
        self,
        config,
        path: str,
        meme_infos_getter: Callable[[], dict[str, dict]],
        meme_display_name: Callable[[dict], str],
        safe_int: Callable[[object], int],
        group_id: Callable[[Any], str],
        group_name_from_event: Callable[[Any, str], str],
        lookup_group_name: Callable[[Any, str], Awaitable[str]],
    ):
        self.config = config
        self._meme_infos_getter = meme_infos_getter
        self._meme_display_name = meme_display_name
        self._safe_int = safe_int
        self._group_id = group_id
        self._group_name_from_event = group_name_from_event
        self._lookup_group_name = lookup_group_name
        self.path = path
        self.lock = asyncio.Lock()

    def register_web_apis(self, context, plugin_id: str) -> None:
        context.register_web_api(
            f"/{plugin_id}/stats",
            self.web_get_stats,
            ["GET"],
            "获取表情统计数据",
        )
        context.register_web_api(
            f"/{plugin_id}/reset",
            self.web_reset_stats,
            ["POST"],
            "清空表情统计数据",
        )
        context.register_web_api(
            f"/{plugin_id}/delete",
            self.web_delete_stats,
            ["POST", "DELETE"],
            "删除特定表情统计记录",
        )
        context.register_web_api(
            f"/{plugin_id}/group-name",
            self.web_get_group_name,
            ["GET"],
            "获取缓存群组名称",
        )

    def limit(self) -> int:
        try:
            return max(1, int(self.config.get("meme_usage_stats_limit", 100)))
        except Exception:
            return 100

    def title(self) -> str:
        return (
            str(self.config.get("meme_usage_stats_title", "表情调用统计")).strip()
            or "表情调用统计"
        )

    def load(self) -> dict[str, dict]:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.warning(f"读取表情调用统计失败: {e}")
            return {}

    def normalize(self, data: dict) -> dict:
        if "global" in data or "groups" in data:
            global_usage = (
                data.get("global") if isinstance(data.get("global"), dict) else {}
            )
            groups = data.get("groups") if isinstance(data.get("groups"), dict) else {}
            group_names = (
                data.get("group_names")
                if isinstance(data.get("group_names"), dict)
                else {}
            )
            return {
                "global": global_usage,
                "groups": groups,
                "group_names": group_names,
            }
        return {"global": data, "groups": {}, "group_names": {}}

    def bucket(self, data: dict, scope: str, group_id: str = "") -> dict:
        normalized = self.normalize(data)
        if scope == "group" and group_id:
            groups = normalized.setdefault("groups", {})
            bucket = groups.get(group_id)
            if not isinstance(bucket, dict):
                bucket = {}
                groups[group_id] = bucket
            return bucket
        bucket = normalized.setdefault("global", {})
        return bucket if isinstance(bucket, dict) else {}

    def save(self, data: dict[str, dict]) -> None:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.debug(f"表情统计数据已保存至: {self.path}")
        except Exception as e:
            logger.error(f"保存表情统计数据失败: {e}")

    async def web_get_stats(self):
        try:
            data = self.load()
            normalized = self.normalize(data)
            logger.info(
                f"Plugin Page 请求统计数据，当前记录数: {len(normalized.get('global', {}))}"
            )
            return json.dumps(normalized, ensure_ascii=False), 200, JSON_HEADERS
        except Exception as e:
            logger.error(f"Plugin Page 获取统计失败: {e}")
            return jsonify({"global": {}, "groups": {}, "error": str(e)})

    async def _mutation_params(self) -> dict:
        payload = {}
        if request.is_json:
            data = await request.get_json(silent=True)
            if isinstance(data, dict):
                payload.update(data)
        payload.update(request.args)
        try:
            form = await request.form
            payload.update(form)
        except Exception:
            pass
        return payload

    def _confirmed(self, params: dict) -> bool:
        return str(params.get("confirm", "")).lower() in {"1", "true", "yes"}

    async def web_reset_stats(self):
        params = await self._mutation_params()
        if not self._confirmed(params):
            return (
                json.dumps(
                    {
                        "success": False,
                        "message": "请提供 confirm=true 确认清空统计数据",
                    },
                    ensure_ascii=False,
                ),
                400,
                JSON_HEADERS,
            )
        async with self.lock:
            self.save({"global": {}, "groups": {}, "group_names": {}})
        return (
            json.dumps(
                {"success": True, "message": "统计数据已清空"}, ensure_ascii=False
            ),
            200,
            JSON_HEADERS,
        )

    async def web_get_group_name(self):
        try:
            group_id = str(request.args.get("group_id", "")).strip()
            if not group_id:
                return (
                    json.dumps({"success": False, "message": "未提供 group_id"}),
                    400,
                    JSON_HEADERS,
                )
            data = self.normalize(self.load())
            group_name = str(data.get("group_names", {}).get(group_id) or "").strip()
            return (
                json.dumps(
                    {
                        "success": True,
                        "group_id": group_id,
                        "group_name": group_name,
                    },
                    ensure_ascii=False,
                ),
                200,
                JSON_HEADERS,
            )
        except Exception as e:
            return json.dumps({"success": False, "message": str(e)}), 500, JSON_HEADERS

    async def web_delete_stats(self):
        try:
            args = await self._mutation_params()
            if not self._confirmed(args):
                return (
                    json.dumps(
                        {
                            "success": False,
                            "message": "请提供 confirm=true 确认删除统计数据",
                        },
                        ensure_ascii=False,
                    ),
                    400,
                    JSON_HEADERS,
                )
            key = args.get("key", "")
            scope = args.get("scope", "global")
            group_id = args.get("group_id", "")
            delete_all = args.get("all", "") == "1"

            async with self.lock:
                data = self.normalize(self.load())
                if delete_all:
                    if scope == "global":
                        data["global"] = {}
                    elif scope == "group" and group_id in data["groups"]:
                        del data["groups"][group_id]
                    self.save(data)
                    return (
                        json.dumps({"success": True, "message": "统计数据已删除"}),
                        200,
                        JSON_HEADERS,
                    )

                if not key:
                    return (
                        json.dumps({"success": False, "message": "未提供 meme key"}),
                        400,
                        JSON_HEADERS,
                    )

                if scope == "global":
                    if key in data["global"]:
                        del data["global"][key]
                elif scope == "group" and group_id:
                    if group_id in data["groups"] and key in data["groups"][group_id]:
                        del data["groups"][group_id][key]

                self.save(data)

            return (
                json.dumps({"success": True, "message": f"记录 {key} 已删除"}),
                200,
                JSON_HEADERS,
            )
        except Exception as e:
            return json.dumps({"success": False, "message": str(e)}), 500, JSON_HEADERS

    def increment_item(self, bucket: dict, key: str, info: dict, now: int) -> None:
        item = bucket.get(key)
        if not isinstance(item, dict):
            item = {}
        item["count"] = self._safe_int(item.get("count")) + 1
        item["name"] = self._meme_display_name(info)
        item["last_used"] = now
        bucket[key] = item

    async def record(self, event, info: dict) -> None:
        key = str(info.get("key") or "").strip()
        if not key:
            return
        group_id = self._group_id(event)
        group_name = ""
        if group_id:
            group_name = self._group_name_from_event(
                event, group_id
            ) or await self._lookup_group_name(event, group_id)
        async with self.lock:
            data = self.normalize(self.load())
            now = int(time.time())
            self.increment_item(self.bucket(data, "global"), key, info, now)
            if group_id:
                self.increment_item(
                    self.bucket(data, "group", group_id), key, info, now
                )
                if group_name:
                    data.setdefault("group_names", {})[group_id] = group_name
            self.save(data)

    def display_name(self, key: str, scope: str = "global", group_id: str = "") -> str:
        info = self._meme_infos_getter().get(key) or {}
        if info:
            name = self._meme_display_name(info)
            if name and name != key:
                return name
        item = self.bucket(self.load(), scope, group_id).get(key)
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            if name:
                return name
        return key

    def rows(
        self, limit: int | None = None, scope: str = "global", group_id: str = ""
    ) -> list[tuple[str, int]]:
        data = self.load()
        bucket = self.bucket(data, scope, group_id)
        rows = []
        for key, item in bucket.items():
            if isinstance(item, dict):
                count = self._safe_int(item.get("count"))
            else:
                count = self._safe_int(item)
            if count > 0:
                rows.append((str(key), count))
        rows.sort(key=lambda row: row[1], reverse=True)
        return rows[: limit or self.limit()]

    def format_text(
        self, rows: list[tuple[str, int]], scope: str = "global", group_id: str = ""
    ) -> str:
        total = sum(count for _, count in self.rows(10**9, scope, group_id))
        lines = [self.title(), f"表情调用总次数：{total}"]
        lines.extend(
            f"{index}. {self.display_name(key, scope, group_id)}：{count} 次"
            for index, (key, count) in enumerate(rows, 1)
        )
        return "\n".join(lines)
