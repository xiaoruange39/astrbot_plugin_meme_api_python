import re

QUOTE_PAIRS = {
    '"': '"',
    "'": "'",
    "`": "`",
    "“": "”",
    "‘": "’",
}


class ArgSyntaxError(Exception):
    pass


def split_arg_string(arg_string: str) -> list[str]:
    """Splits a string into a list of arguments, handling quotes and escapes.

    Args:
        arg_string: The raw argument string to split.

    Returns:
        A list of split argument strings.

    Raises:
        ArgSyntaxError: If quotes are unclosed or escapes are malformed.
    """
    args = []
    current = []
    in_quote = None
    out_quote = None
    escape_next = False

    for char in arg_string:
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

    if escape_next:
        raise ArgSyntaxError("参数转义符不能位于末尾")
    if current:
        args.append("".join(current))
    if in_quote:
        raise ArgSyntaxError(f"参数引号未闭合：{in_quote}")
    return args


def set_direction_option(options: dict[str, object], direction: str) -> None:
    """Sets a direction option, raising an error if it conflicts with an existing one.

    Args:
        options: The options dictionary to modify.
        direction: The direction string to set (e.g. 'left', 'right').

    Raises:
        ArgSyntaxError: If a different direction was already set.
    """
    existing = options.get("__direction")
    if existing and existing != direction:
        raise ArgSyntaxError(f"方向参数冲突：{existing} 与 {direction}")
    options["__direction"] = direction


def direction_options_for_key(direction: str) -> dict[str, object]:
    """Returns the options dictionary format for a given direction.

    Args:
        direction: The target direction.

    Returns:
        A dictionary containing the resolved direction option.
    """
    return {"direction": direction}


def materialize_direction_options(options: dict[str, object]) -> dict[str, object]:
    """Materializes direction shorthand keys into unified direction attributes.

    Args:
        options: The parsed options dictionary.

    Returns:
        The updated options dictionary with unified direction attributes.
    """
    direction = options.get("__direction")
    if not direction:
        return options
    resolved = {
        name: value
        for name, value in options.items()
        if name not in {"__direction", "left", "right", "top", "bottom", "direction"}
    }
    resolved.update(direction_options_for_key(str(direction)))
    return resolved


def normalize_meme_options(tokens: list[str]) -> tuple[list[str], dict[str, object]]:
    """Normalizes raw tokens into options and positional arguments.

    Args:
        tokens: The list of raw argument tokens.

    Returns:
        A tuple of (remaining_positional_tokens, options_dict).

    Raises:
        ArgSyntaxError: If direction options conflict.
    """
    options: dict[str, object] = {}
    remaining = []
    for token in tokens:
        if token in {"右", "#右"}:
            set_direction_option(options, "right")
            continue
        if token in {"左", "#左"}:
            set_direction_option(options, "left")
            continue
        if token in {"上", "#上"}:
            set_direction_option(options, "top")
            continue
        if token in {"下", "#下"}:
            set_direction_option(options, "bottom")
            continue
        if token.startswith("#") and len(token) > 1:
            if re.fullmatch(r"#[A-Za-z_][\w-]*=.+", token):
                name, value = token[1:].split("=", 1)
                options[name.replace("-", "_")] = value
            else:
                options[token[1:]] = True
            continue
        if re.fullmatch(r"[A-Za-z_][\w-]*=.+", token):
            name, value = token.split("=", 1)
            options[name.replace("-", "_")] = value
            continue
        remaining.append(token)
    return remaining, options


def direction_options_from_text(key: str, text: str) -> dict[str, object]:
    """Extracts direction options from text if the key matches a directional property.

    Args:
        key: The configuration parameter key (e.g. 'symmetry').
        text: The text to parse.

    Returns:
        A dictionary containing the parsed direction option, or empty dictionary.
    """
    compact = text.replace(" ", "").replace("#", "")
    if key == "symmetry":
        if compact.startswith("对称右"):
            return direction_options_for_key("right")
        if compact.startswith("对称左"):
            return direction_options_for_key("left")
        if compact.startswith("对称上"):
            return direction_options_for_key("top")
        if compact.startswith("对称下"):
            return direction_options_for_key("bottom")
    return {}
