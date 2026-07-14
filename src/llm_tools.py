import json
import random

from .commands import (
    _fill_default_avatar_images,
    _fill_sender_avatar_images,
    _format_range,
    _image_component,
    _params_type,
    _resolve_generate_args,
    _select_render_images,
)

CANDIDATE_COUNT = 50
CANDIDATE_REQUEST_FLAG = "_meme_llm_candidate_batch_requested"
GENERATION_COMPLETE_FLAG = "_meme_llm_generation_complete"


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


def pick_random_meme_infos(meme_infos: dict[str, dict]) -> list[dict]:
    infos = list(meme_infos.values())
    return random.sample(infos, min(CANDIDATE_COUNT, len(infos)))


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
                "Choose the candidate that best fits the full conversation, then call "
                "meme_generate_from_candidate. If none fits, do not call either meme "
                "tool or answer again in this turn; end silently without a meme."
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
    if getattr(event, CANDIDATE_REQUEST_FLAG, False):
        return None
    await updater._refresh_meme_infos()
    setattr(event, CANDIDATE_REQUEST_FLAG, True)
    infos = pick_random_meme_infos(updater._visible_meme_infos(event))
    if not infos:
        return None
    return format_candidate_batch(scene, infos)


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
    if getattr(event, GENERATION_COMPLETE_FLAG, False):
        return None

    texts = _as_strings(texts, "texts", 20)
    image_urls = _as_strings(image_urls, "image_urls", 10)
    user_ids = _as_strings(user_ids, "user_ids", 10)
    if any(not user_id.isdigit() for user_id in user_ids):
        raise ValueError("user_ids may only contain numeric user IDs.")

    await updater._refresh_meme_infos()
    visible_infos = updater._visible_meme_infos(event)
    if not visible_infos:
        return None
    info = updater._find_meme(
        str(meme_name or "").strip(), visible_infos
    )
    if not info:
        return None

    params = _params_type(info)
    tokens = [*image_urls, *(f"@{user_id}" for user_id in user_ids), *texts]
    images, texts, user_infos = await _resolve_generate_args(updater, event, tokens)
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
        return None
    if not (params["min_texts"] <= len(texts) <= params["max_texts"]):
        return None

    image, content_type = await updater.meme_client.render_meme(
        str(info.get("key")), images, texts, user_infos, {}
    )
    await updater.usage_stats.record(event, info)
    setattr(event, GENERATION_COMPLETE_FLAG, True)
    result = event.chain_result([_image_component(updater, image, content_type)])
    event.stop_event()
    return result
