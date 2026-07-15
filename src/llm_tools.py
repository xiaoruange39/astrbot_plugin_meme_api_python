import json
import random
import re
import time

from .image_resolver import raw_event_dict_from_event

from .commands import (
    _fill_default_avatar_images,
    _fill_sender_avatar_images,
    _format_range,
    _image_component,
    _params_type,
    _resolve_generate_args,
    _select_render_images,
    _try_send_small_image_aiocqhttp,
)

CANDIDATE_REQUEST_FLAG = "_meme_llm_candidate_batch_requested"
GENERATION_COMPLETE_FLAG = "_meme_llm_generation_complete"
GENERATION_LOCKS_ATTR = "_meme_llm_generation_locks"
GENERATION_LOCK_TTL_SECONDS = 300


MEME_SENT_RESULT = (
    "<meme_result status='sent' final='true'>"
    "The meme image has already been sent directly to the user. "
    "A meme has already been generated for this user message. Do not call "
    "meme_get_random_candidates, meme_search_candidates, or "
    "meme_generate_from_candidate again in this "
    "tool loop. You may still decide whether "
    "to send a normal follow-up reply, or send no message, based on the conversation. "
    "If you reply, do not repeat the image or mention internal tool details."
    "</meme_result>"
)
MEME_SKIPPED_RESULT = (
    "<meme_result status='skipped' final='true'>"
    "No suitable meme was sent. Do not call meme tools again for this turn. "
    "Continue the conversation naturally if a text reply is appropriate."
    "</meme_result>"
)


def finish_without_meme(event) -> str:
    return MEME_SKIPPED_RESULT


def _event_message_id(event) -> str:
    raw_event = raw_event_dict_from_event(event)
    message_obj = getattr(event, "message_obj", None)
    candidates = [
        raw_event.get("message_id") if isinstance(raw_event, dict) else None,
        raw_event.get("msg_id") if isinstance(raw_event, dict) else None,
    ]
    if isinstance(raw_event, dict):
        data = raw_event.get("data")
        if isinstance(data, dict):
            candidates.extend([data.get("message_id"), data.get("msg_id")])
    for source in (message_obj, event):
        candidates.extend(
            [
                getattr(source, "message_id", None),
                getattr(source, "msg_id", None),
                getattr(source, "id", None),
            ]
        )
    for value in candidates:
        message_id = str(value or "").strip()
        if message_id:
            return message_id

    timestamp = ""
    if isinstance(raw_event, dict):
        timestamp = str(
            raw_event.get("time") or raw_event.get("timestamp") or ""
        ).strip()
    if not timestamp:
        timestamp = str(getattr(message_obj, "timestamp", "") or "").strip()
    if timestamp:
        return f"time:{timestamp}"
    return f"event:{id(event)}"


def _generation_lock_key(updater, event) -> str:
    try:
        group_id = updater._group_id(event)
    except Exception:
        group_id = ""
    try:
        sender_id = updater._sender_id(event)
    except Exception:
        sender_id = ""
    return ":".join(
        [
            str(group_id or "private"),
            str(sender_id or "unknown"),
            _event_message_id(event),
        ]
    )


def _generation_locks(updater) -> dict[str, float]:
    locks = getattr(updater, GENERATION_LOCKS_ATTR, None)
    if not isinstance(locks, dict):
        locks = {}
        setattr(updater, GENERATION_LOCKS_ATTR, locks)
    now = time.monotonic()
    expired = [
        key
        for key, created_at in locks.items()
        if now - float(created_at or 0) > GENERATION_LOCK_TTL_SECONDS
    ]
    for key in expired:
        locks.pop(key, None)
    return locks


def _is_generation_locked(updater, event) -> bool:
    return _generation_lock_key(updater, event) in _generation_locks(updater)


def _mark_generation_locked(updater, event) -> None:
    _generation_locks(updater)[_generation_lock_key(updater, event)] = (
        time.monotonic()
    )


def _as_strings(value, name: str, limit: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array.")
    if len(value) > limit:
        raise ValueError(f"{name} accepts at most {limit} items.")
    return [str(item).strip() for item in value if str(item).strip()]


def _brief(value, limit: int = 8) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value[:limit] if str(item).strip()]


def _valid_image_locator(value: str) -> bool:
    value = str(value or "").strip()
    if not value:
        return False
    lower = value.lower()
    if lower.startswith(("http://", "https://", "base64://", "data:image/")):
        return True
    # Allow explicit local paths for advanced/self-hosted deployments. Bare media
    # hashes from chat history are intentionally ignored here: they are not URLs
    # and would otherwise be misinterpreted as text inputs for meme templates.
    return len(value) >= 3 and (value[1:3] in {":\\", ":/"} or value.startswith("/"))


def pick_random_meme_infos(meme_infos: dict[str, dict], count: int) -> list[dict]:
    infos = list(meme_infos.values())
    return random.sample(infos, min(max(1, count), len(infos)))


def search_meme_infos(updater, event, query: str, limit: int) -> list[dict]:
    visible_infos = updater._visible_meme_infos(event)
    query = str(query or "").strip()
    if not query:
        return []

    matches = updater._search_memes(query, visible_infos, limit=limit)
    if matches:
        return matches

    seen = set()
    fallback_matches = []
    tokens = [
        token.strip().lower()
        for token in re.split(r"[\s,，。；;、/|]+", query)
        if token.strip()
    ]
    for token in tokens:
        if len(token) < 2:
            continue
        for info in updater._search_memes(token, visible_infos, limit=limit):
            key = str(info.get("key", ""))
            if key in seen:
                continue
            seen.add(key)
            fallback_matches.append(info)
            if len(fallback_matches) >= limit:
                return fallback_matches
    if fallback_matches:
        return fallback_matches

    compact_query = "".join(tokens) if tokens else query.lower()
    grams = {
        compact_query[index : index + 2]
        for index in range(max(0, len(compact_query) - 1))
        if len(compact_query[index : index + 2]) == 2
    }
    if not grams:
        return []

    min_score = 2 if len(grams) >= 3 else 1
    scored_matches = []
    for info in updater._sorted_meme_infos(visible_infos):
        search_text = updater._meme_search_text(info)
        score = sum(1 for gram in grams if gram in search_text)
        if score < min_score:
            continue
        scored_matches.append((score, info))
    scored_matches.sort(key=lambda item: item[0], reverse=True)
    return [info for _, info in scored_matches[:limit]]


def format_candidate_batch(scene: str, infos: list[dict]) -> str:
    candidates = []
    for info in infos:
        params = _params_type(info)
        candidates.append(
            {
                "key": str(info.get("key", "")),
                "keywords": _brief(info.get("keywords")),
                "tags": _brief(info.get("tags")),
                "images": _format_range(
                    params["min_images"], params["max_images"]
                ),
                "texts": _format_range(params["min_texts"], params["max_texts"]),
                "default_texts": _brief(params["default_texts"], 4),
            }
        )
    return json.dumps(
        {
            "scene": str(scene or "").strip(),
            "candidate_count": len(candidates),
            "instruction": (
                "If a meme would improve the conversation, choose the best candidate "
                "and call meme_generate_from_candidate using the real XML tool-call "
                "format: <tool_call name=\"meme_generate_from_candidate\">{...}</tool_call>. "
                "Never write <tool_code> or Python-like calls; those are plain text and "
                "will not execute. Use image_urls only for real image URLs/base64/local "
                "paths, never media hashes; use user_ids for QQ avatars. Messages in the "
                "same completion may be sent before the tool executes, so wait for the "
                "tool result if you want a reply after the meme. If tool_results already "
                "contain <meme_result final='true'>, do not call meme tools again. If no "
                "candidate fits, continue naturally with text or no message as appropriate."
            ),
            "candidates": candidates,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


async def get_random_candidate_batch(updater, event, scene: str) -> str | None:
    if not updater.plugin_config.meme_llm_tool_enabled():
        return None
    if not updater._is_allowed_group(event):
        return None
    if getattr(event, GENERATION_COMPLETE_FLAG, False) or _is_generation_locked(
        updater, event
    ):
        return MEME_SENT_RESULT
    if getattr(event, CANDIDATE_REQUEST_FLAG, False):
        return finish_without_meme(event)
    await updater._refresh_meme_infos()
    setattr(event, CANDIDATE_REQUEST_FLAG, True)
    infos = pick_random_meme_infos(
        updater._visible_meme_infos(event), updater.plugin_config.meme_llm_candidate_count()
    )
    if not infos:
        return finish_without_meme(event)
    return format_candidate_batch(scene, infos)


async def search_candidate_batch(
    updater, event, query: str, scene: str = ""
) -> str | None:
    if not updater.plugin_config.meme_llm_tool_enabled():
        return None
    if not updater._is_allowed_group(event):
        return None
    if getattr(event, GENERATION_COMPLETE_FLAG, False) or _is_generation_locked(
        updater, event
    ):
        return MEME_SENT_RESULT
    if getattr(event, CANDIDATE_REQUEST_FLAG, False):
        return finish_without_meme(event)

    await updater._refresh_meme_infos()
    setattr(event, CANDIDATE_REQUEST_FLAG, True)
    limit = updater.plugin_config.meme_llm_candidate_count()
    infos = search_meme_infos(updater, event, query, limit)
    if not infos:
        return finish_without_meme(event)

    scene_text = str(scene or "").strip()
    query_text = str(query or "").strip()
    if scene_text:
        scene_text = f"{scene_text} | search_query={query_text}"
    else:
        scene_text = f"search_query={query_text}"
    return format_candidate_batch(scene_text, infos)


async def generate_meme_from_candidate(
    updater,
    event,
    meme_name: str,
    texts=None,
    image_urls=None,
    user_ids=None,
    use_sender_avatar: bool = True,
):
    if not updater.plugin_config.meme_llm_tool_enabled():
        return None
    if not updater._is_allowed_group(event):
        return None
    if getattr(event, GENERATION_COMPLETE_FLAG, False) or _is_generation_locked(
        updater, event
    ):
        return MEME_SENT_RESULT

    texts = _as_strings(texts, "texts", 20)
    image_urls = [
        value
        for value in _as_strings(image_urls, "image_urls", 10)
        if _valid_image_locator(value)
    ]
    user_ids = _as_strings(user_ids, "user_ids", 10)
    if any(not user_id.isdigit() for user_id in user_ids):
        raise ValueError("user_ids may only contain numeric user IDs.")

    await updater._refresh_meme_infos()
    visible_infos = updater._visible_meme_infos(event)
    if not visible_infos:
        return finish_without_meme(event)
    info = updater._find_meme(
        str(meme_name or "").strip(), visible_infos
    )
    if not info:
        return finish_without_meme(event)

    params = _params_type(info)
    tokens = [*image_urls, *(f"@{user_id}" for user_id in user_ids), *texts]
    images, texts, user_infos = await _resolve_generate_args(
        updater, event, tokens, strict_explicit_images=False
    )
    if len(images) > params["max_images"]:
        if params["max_images"] <= 0:
            images, user_infos = [], []
        else:
            images, user_infos = _select_render_images(
                images, user_infos, params["max_images"]
            )
    if use_sender_avatar and len(images) < params["min_images"]:
        if images:
            await _fill_sender_avatar_images(
                updater, event, images, user_infos, params["min_images"]
            )
        else:
            await _fill_default_avatar_images(
                updater, event, images, user_infos, params["min_images"]
            )
    if len(texts) > params["max_texts"]:
        if params["max_texts"] <= 0:
            texts = []
        else:
            texts = texts[: params["max_texts"]]
    if updater.plugin_config.meme_auto_default_texts() and not texts:
        texts.extend(str(value) for value in params["default_texts"])

    if not (params["min_images"] <= len(images) <= params["max_images"]):
        return finish_without_meme(event)
    if not (params["min_texts"] <= len(texts) <= params["max_texts"]):
        return finish_without_meme(event)

    image, content_type = await updater.meme_client.render_meme(
        str(info.get("key")), images, texts, user_infos, {}
    )
    if not await _try_send_small_image_aiocqhttp(updater, event, image):
        await event.send(
            event.chain_result([_image_component(updater, image, content_type)])
        )
    await updater.usage_stats.record(event, info)
    setattr(event, GENERATION_COMPLETE_FLAG, True)
    _mark_generation_locked(updater, event)
    return MEME_SENT_RESULT
