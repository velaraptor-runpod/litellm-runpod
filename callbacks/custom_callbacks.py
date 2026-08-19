# Custom callbacks for the LiteLLM proxy. Loaded via
# `litellm_settings: callbacks: custom_callbacks.proxy_handler_instance` in
# /app/config.yaml (the proxy adds the config file's directory to sys.path,
# which is /app in this image).
#
# What it does: some OpenAI-compatible backends occasionally return tool-call
# arguments with stray whitespace at value boundaries, or leak raw template
# markup into streamed content. Response.JSON consumers (agent harnesses) then
# reject otherwise-valid calls. This callback:
#   - non-streaming responses: repairs function.arguments by stripping
#     leading/trailing whitespace from string values (env CALLBACK_MUTATE=1,
#     default on; set 0 for audit-only), and logs every hit;
#   - streaming responses: chunks pass through unmodified (mid-stream edits
#     are unsafe); reassembled arguments are validated when the stream ends
#     and logged as a hit/miss counter.
# The log lines are the tripwire for measuring how often upstream responses
# needed sanitation; silence over time = fixed upstream.
import json
import logging
import os
import re

from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger("arg_sanitizer")
MUTATE = os.getenv("CALLBACK_MUTATE", "1") == "1"
# Template markup tokens that should never appear in client-visible text.
MARKER_RE = re.compile(r"<\|(?:open|close|sep)\|>")


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


class ResponseSanitizer(CustomLogger):
    def __init__(self):
        logger.addHandler(logging.StreamHandler())
        logger.setLevel(logging.INFO)

    async def async_post_call_success_callback(self, data, user_api_key_dict, response):
        try:
            for choice in getattr(response, "choices", []) or []:
                tool_calls = getattr(getattr(choice, "message", None), "tool_calls", None)
                for tc in tool_calls or []:
                    fn = getattr(tc, "function", None)
                    if fn is None or not isinstance(fn.arguments, str):
                        continue
                    repaired, d = _inspect_arguments(fn.arguments)
                    if d["padded"] or d["markers"] or d["unparseable"]:
                        logger.warning(
                            "[arg-sanitizer] non-stream hit model=%s tool=%s %s",
                            data.get("model"), fn.name, json.dumps(d),
                        )
                        if MUTATE and d["padded"]:
                            fn.arguments = repaired
        except Exception:
            logger.exception("[arg-sanitizer] failed; returning response untouched")
        return response

    async def async_post_call_streaming_iterator_hook(self, user_api_key_dict, response, request_data):
        bufs = {}  # (choice_index, tool_call_index) -> {"name": str, "args": str}
        marker_text = False
        async for chunk in response:
            try:
                for choice in getattr(chunk, "choices", []) or []:
                    d = getattr(choice, "delta", None)
                    if d is None:
                        continue
                    if isinstance(d.content, str) and MARKER_RE.search(d.content):
                        marker_text = True
                    for tc in getattr(d, "tool_calls", None) or []:
                        fn = tc.function
                        if fn is None:
                            continue
                        key = (choice.index, tc.index)
                        ent = bufs.setdefault(key, {"name": None, "args": ""})
                        if fn.name:
                            ent["name"] = fn.name
                        if isinstance(fn.arguments, str):
                            ent["args"] += fn.arguments
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


proxy_handler_instance = ResponseSanitizer()
