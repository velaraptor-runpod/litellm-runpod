# Custom callbacks for the LiteLLM proxy. Loaded via
# `litellm_settings: callbacks: custom_callbacks.proxy_handler_instance` in
# /app/config.yaml (the proxy adds the config file's directory to sys.path,
# which is /app in this image).
#
# What it does: some OpenAI-compatible backends occasionally return tool-call
# arguments with stray whitespace at value boundaries, or leak raw template
# markup into content. Response/JSON consumers (agent harnesses) then reject
# or mangle otherwise-valid calls. This callback:
#   - chat-completions, non-streaming: repairs function.arguments by stripping
#     leading/trailing whitespace from string values (env CALLBACK_MUTATE=1,
#     default on; set 0 for audit-only), and logs every hit;
#   - responses API (/v1/responses), non-streaming: same repair for
#     function_call.arguments inside response.output[] items, plus stripping
#     leaked Kimi K3 channel markup from message/reasoning text;
#   - chat-completions, streaming: chunks pass through unmodified (mid-stream
#     JSON edits are unsafe); reassembled arguments are validated at stream end
#     and logged as a hit/miss counter;
#   - responses API, streaming: events are sanitized in place — channel-markup
#     deltas are scrubbed from output_text/reasoning_text deltas (with a
#     hold-back buffer for markers split across chunks), and the *_done /
#     response.completed events that carry full text / full arguments are
#     repaired. These edits touch only whole text fields, never JSON
#     structure mid-stream, so they are safe to perform inline.
# K3 markup forms seen upstream: bare <|open|>/<|close|>/<|sep|>/<|end_of_msg|>
# plus a channel word (<|open|> response, <|close|> think), with or without a
# separating space depending on spaces_between_special_tokens. Tracked upstream
# at vllm-project/vllm #51152 (padding on /v1/responses), #51399/#51400
# (streaming marker leak), #52889 (reserved-marker grammar leak) — silence in
# these logs after a vLLM upgrade = fixed upstream.
import json
import logging
import os
import re

from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger("arg_sanitizer")
MUTATE = os.getenv("CALLBACK_MUTATE", "1") == "1"
# Template markup tokens that should never appear in client-visible text.
MARKER_RE = re.compile(r"<\|(?:open|close|sep)\|>")

# K3 channel words that follow <|open|>/<|close|> markers (as plain text when
# the backend decodes with spaces_between_special_tokens).
_CHANNEL_WORDS = r"(?:think|response|message|tools|sep)"
# A channel marker plus its optional trailing channel word. Marker-only matches
# don't eat following whitespace, so sanitizing " <|open|> response hi " yields
# "  hi " — same shape the clean non-streamed path returns.
CHANNEL_MARKER_RE = re.compile(
    r"<\|(?:open|close|sep|end_of_msg)\|>(?:\s*" + _CHANNEL_WORDS + r"(?![A-Za-z]))?"
)
# A marker with no channel word attached (used to detect "marker at end of this
# delta, channel word still to come in the next one").
_WORDLESS_TAIL_RE = re.compile(r"<\|(?:open|close|sep|end_of_msg)\|>\s*")
# A bare channel word at the start of a delta (the second half of the split
# case above): stripped once when pending.
_WORD_PREFIX_RE = re.compile(r"\s*" + _CHANNEL_WORDS + r"(?![A-Za-z])")
# Full marker heads, for holding back a marker that is split across deltas.
_MARKER_HEADS = ("<|open|>", "<|close|>", "<|sep|>", "<|end_of_msg|>")
_MAX_HEAD_LEN = max(len(h) for h in _MARKER_HEADS)

_RESP_TEXT_DELTA = ("response.output_text.delta", "response.reasoning_text.delta")
_RESP_TEXT_DONE = ("response.output_text.done", "response.reasoning_text.done")
_RESP_FINAL = ("response.completed", "response.incomplete", "response.failed")


def _get(obj, key, default=None):
    """Read a field from a plain dict or an attribute-carrying model."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _set(obj, key, value):
    if isinstance(obj, dict):
        obj[key] = value
    elif hasattr(obj, key):
        setattr(obj, key, value)


def _strip_boundary_ws(value):
    if isinstance(value, str):
        s = value.strip()
        return s, s != value
    if isinstance(value, list):
        out, changed = [], False
        for item in value:
            nv, c = _strip_boundary_ws(item)
            out.append(nv)
            changed |= c
        return out, changed
    if isinstance(value, dict):
        out, changed = {}, False
        for k, v in value.items():
            nv, c = _strip_boundary_ws(v)
            out[k] = nv
            changed |= c
        return out, changed
    return value, False


def _inspect_arguments(raw):
    """Return (possibly-repaired str, details dict). Never raises."""
    details = {
        "padded": False,
        "markers": bool(MARKER_RE.search(raw)),
        "unparseable": False,
    }
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        details["unparseable"] = True
        return raw, details
    parsed, details["padded"] = _strip_boundary_ws(parsed)
    repaired = json.dumps(parsed, ensure_ascii=False)
    return (repaired if repaired != raw else raw), details


def _strip_channel_markers(text):
    """Remove K3 channel markup from a text blob. Returns (cleaned, hit)."""
    cleaned = CHANNEL_MARKER_RE.sub("", text)
    return cleaned, cleaned != text


def _split_trailing_partial_marker(text):
    """Split off a trailing prefix of a marker head, e.g. "...foo <|cl".

    Returns (emit_now, hold_for_next_delta). Holding back guarantees a marker
    fractured across streamed chunks can't slip through the regex above.
    """
    if not text:
        return text, ""
    if any(text.endswith(head) for head in _MARKER_HEADS):
        return text, ""  # complete marker at the end; stripped by the regex
    limit = min(len(text), _MAX_HEAD_LEN - 1)
    for n in range(limit, 0, -1):
        tail = text[-n:]
        if any(head.startswith(tail) for head in _MARKER_HEADS):
            return text[:-n], tail
    return text, ""


def _wordless_marker_at_end(text):
    """True if text ends with a marker whose channel word hasn't arrived yet."""
    match = None
    for match in CHANNEL_MARKER_RE.finditer(text):
        pass
    return bool(
        match is not None
        and match.end() == len(text)
        and _WORDLESS_TAIL_RE.fullmatch(match.group(0))
    )


def _sanitize_text_fields(obj, hits, mutate):
    """Strip channel markers from obj's own `text` field and any
    content[]/summary[] parts (message and reasoning items)."""
    if obj is None:
        return
    text = _get(obj, "text")
    if isinstance(text, str):
        cleaned, hit = _strip_channel_markers(text)
        if hit:
            hits["markers"] += 1
            if mutate and cleaned != text:
                _set(obj, "text", cleaned)
    for key in ("content", "summary"):
        parts = _get(obj, key)
        if isinstance(parts, list):
            for part in parts:
                _sanitize_text_fields(part, hits, mutate)


def _sanitize_output_items(items, hits, mutate):
    """Repair function_call arguments and text fields across an output[] list."""
    for item in items or []:
        if _get(item, "type") == "function_call":
            raw = _get(item, "arguments")
            if isinstance(raw, str):
                repaired, d = _inspect_arguments(raw)
                hits["padded"] += int(d["padded"])
                hits["arg_markers"] += int(d["markers"])
                hits["unparseable"] += int(d["unparseable"])
                if mutate and d["padded"]:
                    _set(item, "arguments", repaired)
        _sanitize_text_fields(item, hits, mutate)


def _new_hits():
    return {"padded": 0, "markers": 0, "arg_markers": 0, "unparseable": 0}


def _any_hits(hits):
    return any(hits.values())


class ResponseSanitizer(CustomLogger):
    def __init__(self):
        logger.addHandler(logging.StreamHandler())
        logger.setLevel(logging.INFO)

    async def async_post_call_success_callback(self, data, user_api_key_dict, response):
        try:
            choices = _get(response, "choices")
            if choices:
                # Chat completions shape.
                for choice in choices:
                    tool_calls = _get(_get(choice, "message"), "tool_calls")
                    for tc in tool_calls or []:
                        fn = _get(tc, "function")
                        if fn is None or not isinstance(_get(fn, "arguments"), str):
                            continue
                        raw = _get(fn, "arguments")
                        repaired, d = _inspect_arguments(raw)
                        if d["padded"] or d["markers"] or d["unparseable"]:
                            logger.warning(
                                "[arg-sanitizer] non-stream hit model=%s tool=%s %s",
                                data.get("model"), _get(fn, "name"), json.dumps(d),
                            )
                            if MUTATE and d["padded"]:
                                _set(fn, "arguments", repaired)
                return response
            output = _get(response, "output")
            if isinstance(output, list):
                # Responses API shape.
                hits = _new_hits()
                _sanitize_output_items(output, hits, MUTATE)
                if _any_hits(hits):
                    logger.warning(
                        "[arg-sanitizer] responses non-stream hit model=%s %s",
                        data.get("model"), json.dumps(hits),
                    )
        except Exception:
            logger.exception("[arg-sanitizer] failed; returning response untouched")
        return response

    async def async_post_call_streaming_iterator_hook(self, user_api_key_dict, response, request_data):
        bufs = {}  # chat: (choice_index, tool_call_index) -> {"name", "args"}
        marker_text = False
        text_state = {}  # responses: item_id -> {"hold": str, "pending_word": bool}
        hits = _new_hits()
        async for chunk in response:
            try:
                if self._sanitize_responses_chunk(chunk, text_state, hits):
                    pass  # responses-API event, handled in place
                else:
                    for choice in _get(chunk, "choices") or []:
                        d = _get(choice, "delta")
                        if d is None:
                            continue
                        content = _get(d, "content")
                        if isinstance(content, str) and MARKER_RE.search(content):
                            marker_text = True
                        for tc in _get(d, "tool_calls") or []:
                            fn = _get(tc, "function")
                            if fn is None:
                                continue
                            key = (_get(choice, "index"), _get(tc, "index"))
                            ent = bufs.setdefault(key, {"name": None, "args": ""})
                            if _get(fn, "name"):
                                ent["name"] = _get(fn, "name")
                            args = _get(fn, "arguments")
                            if isinstance(args, str):
                                ent["args"] += args
            except Exception:
                pass
            yield chunk
        for (ci, ti), ent in bufs.items():
            _, d = _inspect_arguments(ent["args"])
            if d["padded"] or d["markers"] or d["unparseable"]:
                logger.warning(
                    "[arg-sanitizer] stream hit model=%s tool=%s %s",
                    request_data.get("model"), ent["name"], json.dumps(d),
                )
        if marker_text:
            logger.warning(
                "[arg-sanitizer] template markup leaked into streamed content model=%s",
                request_data.get("model"),
            )
        if _any_hits(hits):
            logger.warning(
                "[arg-sanitizer] responses stream hit model=%s %s",
                request_data.get("model"), json.dumps(hits),
            )

    def _sanitize_responses_chunk(self, chunk, text_state, hits):
        """In-place sanitation of one Responses-API SSE event. Returns True if
        the chunk was a responses-API event (handled), False otherwise."""
        typ = _get(chunk, "type")
        if not isinstance(typ, str) or not typ.startswith("response."):
            return False
        if typ in _RESP_TEXT_DELTA:
            delta = _get(chunk, "delta")
            if not isinstance(delta, str) or not delta:
                return True
            st = text_state.setdefault(
                _get(chunk, "item_id") or "", {"hold": "", "pending_word": False}
            )
            buf = st["hold"] + delta
            emit, st["hold"] = _split_trailing_partial_marker(buf)
            cleaned, hit = _strip_channel_markers(emit)
            if st["pending_word"]:
                m = _WORD_PREFIX_RE.match(cleaned)
                if m:
                    cleaned = cleaned[m.end():]
                    hit = True
                st["pending_word"] = False
            if _wordless_marker_at_end(emit):
                st["pending_word"] = True
            if hit:
                hits["markers"] += 1
            # Mutate whenever the outgoing delta differs from the incoming one,
            # including hold-back-only chunks (the tail is deferred to the next
            # delta) — not just on complete-marker matches.
            if MUTATE and cleaned != delta:
                _set(chunk, "delta", cleaned)
        elif typ in _RESP_TEXT_DONE:
            text = _get(chunk, "text")
            if isinstance(text, str):
                cleaned, hit = _strip_channel_markers(text)
                if hit:
                    hits["markers"] += 1
                    if MUTATE and cleaned != text:
                        _set(chunk, "text", cleaned)
        elif typ == "response.function_call_arguments.done":
            raw = _get(chunk, "arguments")
            if isinstance(raw, str):
                repaired, d = _inspect_arguments(raw)
                hits["padded"] += int(d["padded"])
                hits["arg_markers"] += int(d["markers"])
                hits["unparseable"] += int(d["unparseable"])
                if MUTATE and d["padded"]:
                    _set(chunk, "arguments", repaired)
        elif typ == "response.output_item.done":
            item = _get(chunk, "item")
            if item is not None:
                _sanitize_output_items([item], hits, MUTATE)
        elif typ in _RESP_FINAL:
            output = _get(_get(chunk, "response"), "output")
            if isinstance(output, list):
                _sanitize_output_items(output, hits, MUTATE)
        return True


proxy_handler_instance = ResponseSanitizer()
