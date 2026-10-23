
from __future__ import annotations

import hashlib
import random
import re
import threading
import time
from typing import Iterator

from .base import Backend, BackendError, Cancelled, GenParams

TOKEN_RE = re.compile(r"\S+\s*")
MAX_WORDS_RE = re.compile(r"Maximum\s+(\d+)\s+words", re.IGNORECASE)
SECTION_RE = re.compile(r"^[ \t]{0,8}#{1,4}[ \t]*([A-Z][A-Z /&-]{2,40})[ \t]*$", re.MULTILINE)
NEW_TEXT_RE = re.compile(r"=== NEW[A-Z ]*TEXT ===\r?\n(.*?)\r?\n=== END", re.DOTALL)
NAME_RE = re.compile(r"(?<![.!?]\s)(?<!^)\b([A-ZÀ-ÖØ-Þ][^\W\d_]{2,})\b", re.MULTILINE)

_FAILED_ONCE: set[str] = set()

VOICES = {
    "cydonia-24b": {
        "tone": "close and concrete",
        "openers": ["The cold got in first.", "She did not answer straight away.",
                    "It started with the sound of the door.", "He counted three before moving."],
        "nouns": ["salt", "rope", "lamp oil", "the tide line", "wet stone", "a torn sleeve",
                  "the hinge", "the doorframe", "the gutter", "a cracked cup"],
        "verbs": ["set", "dragged", "held", "broke", "wound", "pressed", "let go of", "carried"],
        "connectives": ["and then", "so", "but", "which is when"],
        "avg_sentence": 13,
    },
    "dolphin-venice-24b": {
        "tone": "blunt and forward-leaning",
        "openers": ["Here is what happened.", "Nobody stopped them.", "The plan was simple.",
                    "They went in through the back."],
        "nouns": ["the ledger", "a hard drive", "the manifest", "two hundred euro",
                  "the fire door", "a burner phone", "the loading bay", "his own name"],
        "verbs": ["took", "burned", "traded", "opened", "walked out with", "signed", "denied"],
        "connectives": ["so", "then", "after that", "because of that"],
        "avg_sentence": 10,
    },
    "mistral-small-32": {
        "tone": "measured and orderly",
        "openers": ["By the time the light changed, the room had emptied.",
                    "There were three things left to settle.",
                    "The arrangement had always been provisional.",
                    "It was not the first time the question had been asked."],
        "nouns": ["the arrangement", "an old debt", "the north window", "the appointment",
                  "a folded page", "the corridor", "her account of it", "the interval"],
        "verbs": ["considered", "arranged", "postponed", "recorded", "declined", "returned"],
        "connectives": ["and yet", "which meant that", "in the end", "even so"],
        "avg_sentence": 19,
    },
    "qwen3-32b": {
        "tone": "brisk and structural",
        "openers": ["Three people knew.", "The sequence mattered more than the reason.",
                    "It resolved faster than expected.", "The first attempt failed."],
        "nouns": ["the timing", "a second key", "the west gate", "the record",
                  "an unpaid account", "the signal", "the interval between"],
        "verbs": ["moved", "confirmed", "reversed", "held", "cut", "restored"],
        "connectives": ["therefore", "then", "which left", "meanwhile"],
        "avg_sentence": 12,
    },
}

DEFAULT_VOICE = {
    "tone": "plain",
    "openers": ["It began quietly.", "Nothing had changed by morning."],
    "nouns": ["the room", "a closed door", "the far wall", "the hour"],
    "verbs": ["moved", "waited", "turned", "left"],
    "connectives": ["and", "so", "but"],
    "avg_sentence": 14,
}


class MockBackend(Backend):
    kind = "mock"

    def __init__(self, model_key: str, label: str, cfg_mock: dict):
        super().__init__(model_key, label)
        self.cfg = cfg_mock
        self.tps = float(cfg_mock.get("tokens_per_second", 45.0)) or 45.0
        self.load_seconds = float(cfg_mock.get("load_seconds", 1.2))
        self.unload_seconds = float(cfg_mock.get("unload_seconds", 0.4))
        self.fail_models = set(cfg_mock.get("fail_models") or [])
        self.base_seed = int(cfg_mock.get("seed", 7))
        self.voice = VOICES.get(model_key, DEFAULT_VOICE)

    def describe(self) -> str:
        return f"{self.label} (mock)"

    def start(self) -> None:
        time.sleep(self.load_seconds)
        self.pid = None

    def stop(self) -> None:
        time.sleep(self.unload_seconds)

    def _rng(self, user_message: str, params: GenParams) -> random.Random:
        digest = hashlib.sha256(
            f"{self.base_seed}|{self.model_key}|{params.seed}|{user_message}".encode("utf-8")
        ).hexdigest()
        return random.Random(int(digest[:16], 16))

    def _names_from(self, user_message: str, rng: random.Random) -> list[str]:
        source = NEW_TEXT_RE.search(user_message)
        text = source.group(1) if source else user_message
        counts: dict[str, int] = {}
        for m in NAME_RE.finditer(text):
            word = m.group(1)
            if word.isupper():
                continue
            counts[word] = counts.get(word, 0) + 1
        ranked = [w for w, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
        return ranked[:6] or ["the narrator"]

    def _sentence(self, rng: random.Random, names: list[str]) -> str:
        v = self.voice
        subject = rng.choice(names) if rng.random() < 0.55 else rng.choice(
            ["She", "He", "They", "The other one"]
        )
        body = f"{subject} {rng.choice(v['verbs'])} {rng.choice(v['nouns'])}"
        if rng.random() < 0.5:
            body += f", {rng.choice(v['connectives'])} {rng.choice(names).lower()} " \
                    f"{rng.choice(v['verbs'])} {rng.choice(v['nouns'])}"
        if rng.random() < 0.25:
            body += f" — {rng.choice(v['nouns'])} again"
        return body[0].upper() + body[1:] + rng.choice([".", ".", ".", "?", "."])

    def _paragraph(self, rng: random.Random, names: list[str], sentences: int) -> str:
        return " ".join(self._sentence(rng, names) for _ in range(sentences))

    def _compose(self, user_message: str, params: GenParams) -> tuple[str, int]:
        rng = self._rng(user_message, params)
        names = self._names_from(user_message, rng)
        max_words_match = MAX_WORDS_RE.search(user_message)
        sections = [s.strip() for s in SECTION_RE.findall(user_message)]
        sections = [s for s in sections
                    if s not in ("SUMMARY SO FAR", "NEW TEXT", "END OF NEW TEXT", "INSTRUCTION")]

        if max_words_match:
            budget = int(max_words_match.group(1))
            head = (f"*(mock backend — {self.voice['tone']} voice for {self.label}; "
                    f"no model was involved)*\n\n")
            if sections:
                per = max(2, budget // max(len(sections), 1) // 12)
                body = "\n\n".join(
                    f"## {name}\n" + "\n".join(
                        f"- {self._sentence(rng, names)}" for _ in range(per)
                    )
                    for name in sections[:8]
                )
            else:
                paras = max(2, min(5, budget // 90))
                body = "\n\n".join(
                    self._paragraph(rng, names, rng.randint(3, 6)) for _ in range(paras)
                )
            return head + body, budget

        paras = rng.randint(3, 6)
        body = "\n\n".join(
            self._paragraph(rng, names, max(2, round(rng.gauss(5, 1.5)))) for _ in range(paras)
        )
        head = (f"*(mock backend — {self.voice['tone']} voice for {self.label}; "
                f"no model was involved)*\n\n")
        return head + rng.choice(self.voice["openers"]) + " " + body, 0

    def stream(self, user_message: str, params: GenParams,
               cancel: threading.Event | None = None) -> Iterator[dict]:
        always_fail = set(self.cfg.get("fail_models") or []) | self.fail_models
        once = str(self.cfg.get("fail_first_for") or "")
        fail_now = self.model_key in always_fail or "[[MOCK_FAIL]]" in user_message
        if not fail_now and once and once == self.model_key and once not in _FAILED_ONCE:
            _FAILED_ONCE.add(once)
            fail_now = True
        if fail_now:
            time.sleep(min(0.3, self.load_seconds))
            raise BackendError(
                f"mock failure injected for {self.model_key}: "
                f"CUDA error: out of memory (simulated)"
            )

        text, _ = self._compose(user_message, params)
        tokens = TOKEN_RE.findall(text)
        limit = max(1, int(params.max_tokens))
        finish = "stop"
        if len(tokens) > limit:
            tokens = tokens[:limit]
            finish = "length"

        delay = 1.0 / self.tps if self.tps > 0 else 0.0
        started = time.time()
        for token in tokens:
            if cancel is not None and cancel.is_set():
                raise Cancelled("cancelled during generation")
            if delay:
                time.sleep(delay)
            yield {"type": "token", "text": token}
        elapsed = max(time.time() - started, 1e-6)
        yield {
            "type": "done",
            "finish_reason": finish,
            "usage": {
                "prompt_tokens": len(TOKEN_RE.findall(user_message)),
                "completion_tokens": len(tokens),
                "total_tokens": len(TOKEN_RE.findall(user_message)) + len(tokens),
            },
            "timings": {"predicted_n": len(tokens), "predicted_per_second": len(tokens) / elapsed},
        }
