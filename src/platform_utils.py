from urllib.parse import quote

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .image_resolver import extract_segments_from_event, raw_event_dict_from_event

QQ_AVATAR_URL_TEMPLATE = "https://q4.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=640"


def avatar_url(user_id: str) -> str:
    """Returns the avatar URL for a given QQ user ID.

    Args:
        user_id: The QQ user ID.

    Returns:
        The formatted avatar URL.
    """
    if not user_id:
        return ""
    return QQ_AVATAR_URL_TEMPLATE.format(user_id=quote(str(user_id), safe=""))


def sender_id(event: AstrMessageEvent) -> str:
    """Extracts the sender user ID from the message event.

    Args:
        event: The AstrMessageEvent.

    Returns:
        The sender ID string, or empty string.
    """
    message_obj = getattr(event, "message_obj", None)
    raw_event = raw_event_dict_from_event(event)
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


def sender_avatar_url(event: AstrMessageEvent) -> str:
    """Extracts the sender avatar URL.

    Args:
        event: The AstrMessageEvent.

    Returns:
        The avatar URL string.
    """
    return avatar_url(sender_id(event))


def bot_id(event: AstrMessageEvent) -> str:
    """Extracts the bot's user ID from the event.

    Args:
        event: The AstrMessageEvent.

    Returns:
        The bot ID string.
    """
    message_obj = getattr(event, "message_obj", None)
    raw_event = raw_event_dict_from_event(event)
    bot_id_val = str(raw_event.get("self_id") or raw_event.get("bot_id") or "").strip()
    if bot_id_val:
        return bot_id_val
    return str(
        getattr(event, "self_id", "") or getattr(message_obj, "self_id", "") or ""
    ).strip()


def bot_avatar_url(event: AstrMessageEvent) -> str:
    """Extracts the bot's avatar URL.

    Args:
        event: The AstrMessageEvent.

    Returns:
        The avatar URL string.
    """
    return avatar_url(bot_id(event))


def group_id(event: AstrMessageEvent) -> str:
    """Extracts the group/chat ID from the event.

    Args:
        event: The AstrMessageEvent.

    Returns:
        The group ID string, or empty string.
    """
    try:
        group_id_val = str(event.get_group_id() or "").strip()
        if group_id_val:
            return group_id_val
    except Exception:
        pass
    message_obj = getattr(event, "message_obj", None)
    raw_message = raw_event_dict_from_event(event)
    if isinstance(raw_message, dict):
        group_id_val = str(
            raw_message.get("group_id") or raw_message.get("group") or ""
        ).strip()
        if group_id_val:
            return group_id_val
    for source in (message_obj, event):
        group_id_val = str(
            getattr(source, "group_id", "") or getattr(source, "group", "") or ""
        ).strip()
        if group_id_val:
            return group_id_val
    return ""


def group_name_from_event(event: AstrMessageEvent, group_id_val: str) -> str:
    """Extracts the group name from various attributes in the event.

    Args:
        event: The AstrMessageEvent.
        group_id_val: The group ID.

    Returns:
        The group name string, or empty string.
    """
    message_obj = getattr(event, "message_obj", None)
    raw_message = raw_event_dict_from_event(event)
    names = []
    if isinstance(raw_message, dict):
        for key in (
            "group_name",
            "group_card",
            "group_title",
            "name",
            "title",
            "chat_name",
        ):
            value = str(raw_message.get(key) or "").strip()
            if value:
                names.append(value)
    for source in (message_obj, event):
        for attr in (
            "group_name",
            "group_card",
            "group_title",
            "name",
            "title",
            "chat_name",
        ):
            value = str(getattr(source, attr, "") or "").strip()
            if value:
                names.append(value)
    return next((name for name in names if name and name != group_id_val), "")


def name_from_group_info(info: object, group_id_val: str) -> str:
    """Resolves a group name from a group info payload.

    Args:
        info: The group info object/dict.
        group_id_val: The group ID.

    Returns:
        The group name string, or empty string.
    """
    if isinstance(info, dict):
        data = info.get("data")
        if isinstance(data, dict):
            name = name_from_group_info(data, group_id_val)
            if name:
                return name
        for key in (
            "group_name",
            "group_card",
            "name",
            "group_title",
            "title",
            "chat_name",
            "nickname",
        ):
            value = str(info.get(key) or "").strip()
            if value and value != group_id_val:
                return value
        return ""
    for attr in (
        "group_name",
        "group_card",
        "name",
        "group_title",
        "title",
        "chat_name",
        "nickname",
    ):
        value = str(getattr(info, attr, "") or "").strip()
        if value and value != group_id_val:
            return value
    return ""


def name_from_user_info(info: object, user_id: str) -> str:
    """Resolves a user name from a user info payload.

    Args:
        info: The user info object/dict.
        user_id: The user ID.

    Returns:
        The user name string, or empty string.
    """
    if not isinstance(info, dict):
        return ""
    for key in ("card", "nickname", "user_name", "name", "remark"):
        value = str(info.get(key) or "").strip()
        if value and value != user_id:
            return value
    data = info.get("data")
    if isinstance(data, dict):
        return name_from_user_info(data, user_id)
    return ""


async def lookup_group_name(event: AstrMessageEvent | None, group_id_val: str) -> str:
    """Looks up group name using API call on event's bot.

    Args:
        event: The AstrMessageEvent.
        group_id_val: The group ID.

    Returns:
        The group name string.
    """
    if event is None or not group_id_val:
        return ""
    try:
        group_data = await event.get_group(group_id_val)
        return name_from_group_info(group_data, group_id_val) or ""
    except Exception as e:
        logger.debug(f"event.get_group 获取群名失败: {e}")
        return ""


async def call_bot_action_candidates(
    bot: object, action: str, params: dict
) -> list[object]:
    """Tries executing a bot action using candidate method formats.

    Args:
        bot: The bot instance.
        action: The action name.
        params: The action parameters.

    Returns:
        A list of successful API results.
    """
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


async def lookup_sender_name(event: AstrMessageEvent, user_id: str) -> str:
    """Looks up a user's display name inside a group or globally.

    Args:
        event: The AstrMessageEvent.
        user_id: The target user's ID.

    Returns:
        The resolved display name string.
    """
    bot = getattr(event, "bot", None)
    if not bot or not user_id:
        return ""
    group_id_val = group_id(event)
    query_user_id = int(user_id) if user_id.isdigit() else user_id
    query_group_id = int(group_id_val) if group_id_val.isdigit() else group_id_val
    calls = []
    if group_id_val:
        calls.extend(
            (
                (
                    "get_group_member_info",
                    {
                        "group_id": query_group_id,
                        "user_id": query_user_id,
                        "no_cache": False,
                    },
                ),
                (
                    "get_group_member",
                    {"group_id": query_group_id, "user_id": query_user_id},
                ),
            )
        )
    calls.extend(
        (
            ("get_stranger_info", {"user_id": query_user_id, "no_cache": False}),
            ("get_friend_info", {"user_id": query_user_id}),
        )
    )

    for action, params in calls:
        for info in await call_bot_action_candidates(bot, action, params):
            name = name_from_user_info(info, user_id)
            if name:
                return name
    return ""


async def sender_user_info(event: AstrMessageEvent) -> dict:
    """Resolves sender user info details.

    Args:
        event: The AstrMessageEvent.

    Returns:
        A dictionary containing "name" and "gender".
    """
    names = []
    message_obj = getattr(event, "message_obj", None)
    sender = getattr(message_obj, "sender", None)
    sender_id_val = sender_id(event)

    for source in (sender, message_obj, event):
        if not source:
            continue
        for attr in ("card", "nickname", "user_name", "name", "sender_name"):
            value = str(getattr(source, attr, "") or "").strip()
            if value:
                names.append(value)

    raw_message = raw_event_dict_from_event(event)
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

    name = next((value for value in names if value and value != sender_id_val), "")
    if not name:
        name = await lookup_sender_name(event, sender_id_val)
    return {"name": name or sender_id_val, "gender": "unknown"}


def bot_user_info(event: AstrMessageEvent) -> dict:
    """Resolves bot user info details.

    Args:
        event: The AstrMessageEvent.

    Returns:
        A dictionary containing "name" and "gender".
    """
    bot_id_val = bot_id(event)
    return {"name": bot_id_val or "机器人", "gender": "unknown"}


async def try_send_forward_message(
    event: AstrMessageEvent, title: str, content: str, count: int
) -> bool:
    """Tries delivering a forward message list to group or private chat.

    Args:
        event: The AstrMessageEvent.
        title: The forward node title.
        content: The message content block.
        count: The count of messages inside.

    Returns:
        True if successfully sent, False otherwise.
    """
    bot = getattr(event, "bot", None)
    if not bot or not content:
        return False
    group_id_val = group_id(event)
    user_id = sender_id(event)
    bot_id_val = bot_id(event) or "0"
    nodes = [
        {"type": "node", "data": {"name": title, "uin": bot_id_val, "content": content}}
    ]
    metadata = {
        "prompt": "表情搜索结果",
        "summary": f"查看 {count} 条搜索结果",
        "source": "meme搜索",
    }
    if group_id_val:
        target = int(group_id_val) if group_id_val.isdigit() else group_id_val
        calls = [
            (
                "send_group_forward_msg",
                {"group_id": target, "messages": nodes, **metadata},
            )
        ]
    elif user_id:
        target = int(user_id) if user_id.isdigit() else user_id
        calls = [
            (
                "send_private_forward_msg",
                {"user_id": target, "messages": nodes, **metadata},
            )
        ]
    else:
        return False
    for action, params in calls:
        call_action = getattr(bot, "call_action", None)
        if callable(call_action):
            try:
                await call_action(action, **params)
                return True
            except Exception as e:
                logger.debug(f"调用 {action} 失败，尝试直调降级: {e}")
        method = getattr(bot, action, None)
        if callable(method):
            try:
                await method(**params)
                return True
            except Exception as e:
                logger.debug(f"调用 {action} 失败: {e}")
    return False


def is_poke_to_bot_event(event: AstrMessageEvent) -> bool:
    """Checks if the event is a double-tap/poke gesture targeting the bot.

    Args:
        event: The AstrMessageEvent.

    Returns:
        True if it targets the bot, False otherwise.
    """
    message_obj = getattr(event, "message_obj", None)
    bot_id_val = str(
        getattr(event, "self_id", "") or getattr(message_obj, "self_id", "")
    ).strip()
    for segment in extract_segments_from_event(event):
        if isinstance(segment, dict):
            seg_type = str(segment.get("type") or "").lower()
            data = segment.get("data") or {}
            target_id = (
                str(
                    data.get("id") or data.get("qq") or data.get("target_id") or ""
                ).strip()
                if isinstance(data, dict)
                else ""
            )
        else:
            seg_type = str(
                getattr(segment, "type", "") or getattr(segment, "_type", "") or ""
            ).lower()
            target_id = ""
            target_method = getattr(segment, "target_id", None)
            if callable(target_method):
                target_id = str(target_method() or "").strip()
            if not target_id:
                target_id = str(
                    getattr(segment, "id", "") or getattr(segment, "qq", "") or ""
                ).strip()
        if (
            seg_type.endswith("poke")
            and target_id
            and bot_id_val
            and target_id == bot_id_val
        ):
            return True
    return False


def segment_type_name(segment_type: object) -> str:
    """Helper to convert segment type into a normalized lowercase string.

    Args:
        segment_type: The segment type object.

    Returns:
        The normalized type name.
    """
    value = getattr(segment_type, "value", None)
    if value is not None:
        return str(value).lower()
    name = getattr(segment_type, "name", None)
    if name is not None:
        return str(name).lower()
    return str(segment_type or "").lower()


def segment_text(segment: object) -> str:
    """Extracts raw text content from a message segment.

    Args:
        segment: The message segment object.

    Returns:
        The text content string.
    """
    if isinstance(segment, dict):
        seg_type = segment_type_name(segment.get("type"))
        data = segment.get("data") or {}
        if seg_type in {"text", "plain"} and isinstance(data, dict):
            return str(data.get("text") or "")
        return ""
    seg_type = segment_type_name(
        getattr(segment, "type", "") or getattr(segment, "_type", "")
    )
    if seg_type not in {"plain", "text"}:
        return ""
    return str(getattr(segment, "text", "") or "")


def extract_message_text(updater, event: AstrMessageEvent) -> str:
    """Extracts the full text string from the event message segments.

    Args:
        updater: The MemeUpdater instance.
        event: The AstrMessageEvent.

    Returns:
        The extracted message text.
    """
    text = "".join(
        segment_text(segment) for segment in extract_segments_from_event(event)
    ).strip()
    if text:
        return text
    message_obj = getattr(event, "message_obj", None)
    for value in (
        getattr(message_obj, "message", None),
        getattr(event, "message", None),
    ):
        if isinstance(value, str):
            return value.strip()
    return ""


def stop_event(event: AstrMessageEvent) -> None:
    """Helper to stop propagation of the message event.

    Args:
        event: The AstrMessageEvent.
    """
    stop_event_method = getattr(event, "stop_event", None)
    if callable(stop_event_method):
        stop_event_method()


def get_message_args(updater, event: AstrMessageEvent, command_name: str) -> str:
    """Extracts arguments for a command by stripping the command prefix from the message.

    Args:
        updater: The MemeUpdater instance.
        event: The AstrMessageEvent.
        command_name: The command name.

    Returns:
        The argument string trailing the command.
    """
    message = extract_message_text(updater, event)
    if not message:
        return ""

    parts = message.split(maxsplit=1)
    if parts:
        p0 = parts[0].lstrip("#/％%")
        if p0 == command_name or p0.endswith(command_name):
            return parts[1] if len(parts) > 1 else ""

    idx = message.find(command_name)
    if idx != -1:
        return message[idx + len(command_name) :].strip()

    return ""
