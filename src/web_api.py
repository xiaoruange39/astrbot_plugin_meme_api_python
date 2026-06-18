from quart import jsonify, request

from astrbot.api import logger


async def all_meme_infos_for_disabled_web(updater) -> dict[str, dict]:
    """Refreshes and returns all meme infos for the disabled memes Web UI.

    Args:
        updater: The MemeUpdater instance.

    Returns:
        A dictionary containing all meme infos.
    """
    await updater._refresh_meme_infos()
    return updater.meme_infos


async def disabled_meme_params(updater) -> dict:
    """Extracts payload params from JSON, query args, or form payload.

    Args:
        updater: The MemeUpdater instance.

    Returns:
        A dictionary containing combined request parameters.
    """
    payload = {}
    try:
        if request.is_json:
            data = await request.get_json(silent=True)
            if isinstance(data, dict):
                payload.update(data)
    except Exception:
        pass
    payload.update(request.args)
    try:
        form = await request.form
        payload.update(form)
    except Exception:
        pass
    return payload


def disabled_meme_scope(updater, params: dict) -> str:
    """Extracts target group ID scope from request parameters.

    Args:
        updater: The MemeUpdater instance.
        params: The request parameter dictionary.

    Returns:
        The target group ID string, or empty string.
    """
    scope = str(params.get("scope", "global")).strip()
    group_id = str(params.get("group_id", "")).strip()
    return group_id if scope == "group" and group_id else ""


async def web_get_disabled_memes(updater):
    """Quart handler for retrieving disabled memes.

    Args:
        updater: The MemeUpdater instance.

    Returns:
        A JSON response containing global and group-specific disabled memes.
    """
    try:
        all_meme_infos = await all_meme_infos_for_disabled_web(updater)
        global_names = updater.plugin_config.disabled_meme_names()
        groups = {}
        for group_id, names in updater.plugin_config.disabled_meme_groups().items():
            display_names = updater.disabled_memes.disabled_display_names(
                all_meme_infos, names
            )
            groups[group_id] = {"count": len(display_names), "items": display_names}
        global_display_names = updater.disabled_memes.disabled_display_names(
            all_meme_infos, global_names
        )
        return jsonify(
            {
                "global": {
                    "count": len(global_display_names),
                    "items": global_display_names,
                },
                "groups": groups,
                "group_names": updater.usage_stats.normalize(
                    updater.usage_stats.load()
                ).get("group_names", {}),
            }
        )
    except Exception as e:
        logger.error(f"Plugin Page 获取屏蔽表情列表失败: {e}")
        return jsonify(
            {
                "global": {"count": 0, "items": []},
                "groups": {},
                "group_names": {},
                "error": str(e),
            }
        )


async def web_add_disabled_meme(updater):
    """Quart handler for adding a meme to the disabled list.

    Args:
        updater: The MemeUpdater instance.

    Returns:
        A JSON response indicating success or failure.
    """
    try:
        params = await disabled_meme_params(updater)
        name = str(params.get("name", "")).strip()
        if not name:
            return jsonify({"success": False, "message": "未提供表情名或关键词"}), 400
        group_id = disabled_meme_scope(updater, params)
        all_meme_infos = await all_meme_infos_for_disabled_web(updater)
        result = updater.disabled_memes.disable(group_id, name, all_meme_infos)
        if result.status == "not_found":
            return jsonify({"success": False, "message": f"未找到表情：{name}"}), 404
        if result.status == "already_disabled":
            return jsonify(
                {
                    "success": True,
                    "message": "已在屏蔽列表中",
                    "status": result.status,
                }
            )
        return jsonify(
            {
                "success": True,
                "message": f"已屏蔽 {result.display_name}",
                "status": result.status,
            }
        )
    except Exception as e:
        logger.error(f"Plugin Page 添加屏蔽表情失败: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


async def web_delete_disabled_meme(updater):
    """Quart handler for removing a meme from the disabled list.

    Args:
        updater: The MemeUpdater instance.

    Returns:
        A JSON response indicating success or failure.
    """
    try:
        params = await disabled_meme_params(updater)
        name = str(params.get("name", "")).strip()
        if not name:
            return jsonify({"success": False, "message": "未提供表情名或关键词"}), 400
        group_id = disabled_meme_scope(updater, params)
        all_meme_infos = await all_meme_infos_for_disabled_web(updater)
        result = updater.disabled_memes.enable(group_id, name, all_meme_infos)
        if result.status == "not_found":
            return jsonify({"success": False, "message": f"未找到表情：{name}"}), 404
        if result.status == "not_disabled":
            return jsonify(
                {
                    "success": True,
                    "message": "不在屏蔽列表中",
                    "status": result.status,
                }
            )
        return jsonify(
            {
                "success": True,
                "message": f"已取消屏蔽 {result.display_name}",
                "status": result.status,
            }
        )
    except Exception as e:
        logger.error(f"Plugin Page 删除屏蔽表情失败: {e}")
        return jsonify({"success": False, "message": str(e)}), 500
