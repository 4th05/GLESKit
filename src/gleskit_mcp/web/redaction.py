"""Credential filtering before any provider bytes reach the engine."""
from __future__ import annotations

import base64
from copy import deepcopy
from urllib.parse import quote, quote_plus


class SecretFilter:
    def __init__(self, secrets=()):
        variants = set()
        for secret in filter(None, secrets):
            variants.update((secret, quote(secret, safe=""), quote_plus(secret),
                             base64.b64encode(secret.encode()).decode(),
                             base64.b64encode(("Bearer " + secret).encode()).decode()))
        self.variants = tuple(sorted(variants, key=len, reverse=True))

    def text(self, value):
        for secret in self.variants:
            value = value.replace(secret, "[redacted]")
        return value

    def value(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.text(key): self.value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.value(item) for item in value]
        return value


class StreamFilter:
    """Keep possible credential prefixes across text/tool-argument SSE deltas.

    Network chunks and JSON events need not align with a credential. Holding only
    a possible prefix preserves incremental output without exposing split keys.
    """
    def __init__(self, redact):
        self.redact = redact
        self.pending, self.frames = {}, {}

    def feed(self, channel, text, frame):
        value = self.redact.text(self.pending.pop(channel, "") + text)
        keep = 0
        for secret in self.redact.variants:
            for size in range(min(len(value), len(secret) - 1), keep, -1):
                if value.endswith(secret[:size]):
                    keep = size
                    break
        self.frames[channel] = frame
        if keep:
            self.pending[channel] = value[-keep:]
            return value[:-keep]
        return value

    def flush(self):
        result = []
        for channel, text in self.pending.items():
            # Do not change innocent endings such as a single letter matching
            # a key prefix. Longer, distinctive incomplete prefixes are masked.
            result.append(self.frames[channel]("[redacted]" if len(text) >= 8 else text))
        self.pending.clear()
        return result

    def clean(self, payload):
        value = deepcopy(payload)
        for choice in value.get("choices", []):
            delta = choice.get("delta", {})
            index = choice.get("index", 0)
            def chat_frame(text, key, index=index):
                return {"id": value.get("id", "gleskit"), "object": "chat.completion.chunk",
                        "choices": [{"index": index, "delta": {key: text}, "finish_reason": None}]}
            for key in ("content", "reasoning_content", "reasoning"):
                if isinstance(delta.get(key), str):
                    delta[key] = self.feed(("chat", index, key), delta[key], lambda text, key=key, frame=chat_frame: frame(text, key))
            for tool in delta.get("tool_calls", []):
                function, tool_index = tool.get("function", {}), tool.get("index", 0)
                if isinstance(function.get("arguments"), str):
                    def tool_frame(text, index=index, tool_index=tool_index):
                        return {"id": value.get("id", "gleskit"), "object": "chat.completion.chunk", "choices": [
                            {"index": index, "delta": {"tool_calls": [{"index": tool_index, "function": {"arguments": text}}]}, "finish_reason": None}]}
                    function["arguments"] = self.feed(("tool", index, tool_index), function["arguments"], tool_frame)
        kind = value.get("type", "")
        if kind == "content_block_delta":
            delta = value.get("delta", {})
            for key in ("text", "partial_json", "thinking"):
                if isinstance(delta.get(key), str):
                    def anthropic_frame(text, key=key, kind=delta.get("type"), index=value.get("index", 0)):
                        return {"type": "content_block_delta", "index": index, "delta": {"type": kind, key: text}}
                    delta[key] = self.feed(("anthropic", value.get("index", 0), key), delta[key], anthropic_frame)
        elif kind.startswith("response.") and kind.endswith(".delta") and isinstance(value.get("delta"), str):
            template = {key: item for key, item in value.items() if key != "delta"}
            value["delta"] = self.feed((kind, value.get("item_id"), value.get("output_index"), value.get("content_index")),
                                       value["delta"], lambda text: {**template, "delta": text})
        return self.redact.value(value)
