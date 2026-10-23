
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Iterator

THINK_RE = re.compile(r"<think\b[^>]*>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
OPEN_THINK_RE = re.compile(r"<think\b[^>]*>.*\Z", re.DOTALL | re.IGNORECASE)


class BackendError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


@dataclass
class GenParams:
    temperature: float = 0.85
    top_p: float | None = None
    min_p: float | None = None
    repeat_penalty: float | None = None
    dry_multiplier: float | None = None
    dry_base: float | None = None
    dry_allowed_length: int | None = None
    seed: int | None = None
    max_tokens: int = 1024
    chat_template_kwargs: dict = field(default_factory=dict)

    def as_body(self) -> dict:
        body: dict = {"temperature": self.temperature, "max_tokens": self.max_tokens}
        if self.top_p is not None:
            body["top_p"] = self.top_p
        if self.min_p is not None:
            body["min_p"] = self.min_p
        if self.repeat_penalty is not None:
            body["repeat_penalty"] = self.repeat_penalty
        if self.dry_multiplier is not None:
            body["dry_multiplier"] = self.dry_multiplier
        if self.dry_base is not None:
            body["dry_base"] = self.dry_base
        if self.dry_allowed_length is not None:
            body["dry_allowed_length"] = self.dry_allowed_length
        if self.seed is not None:
            body["seed"] = self.seed
        if self.chat_template_kwargs:
            body["chat_template_kwargs"] = dict(self.chat_template_kwargs)
        return body


def strip_thinking(text: str) -> str:
    out = THINK_RE.sub("", text)
    out = OPEN_THINK_RE.sub("", out)
    return out.lstrip()


class Backend:

    kind = "base"

    def __init__(self, model_key: str, label: str = ""):
        self.model_key = model_key
        self.label = label or model_key
        self.pid: int | None = None

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def stream(self, user_message: str, params: GenParams,
               cancel: threading.Event | None = None) -> Iterator[dict]:
        raise NotImplementedError

    def is_alive(self) -> bool:
        return True

    def describe(self) -> str:
        return self.label
