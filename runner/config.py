
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .toml_min import load as load_toml


class NoDataRoot(RuntimeError):
    pass


DEFAULTS: dict[str, Any] = {
    "paths": {"models_dir": "models"},
    "runner": {
        "backend": "mock",
        "llama_server": "llama-server",
        "port": 8901,
        "startup_timeout_s": 600,
        "health_poll_s": 1.0,
        "shutdown_timeout_s": 60,
        "idle_unload_s": 600,
        "warm_keep": True,
        "request_timeout_s": 1800,
        "flash_attn": "on",
    },
    "mock": {
        "tokens_per_second": 45.0,
        "load_seconds": 1.2,
        "unload_seconds": 0.4,
        "fail_models": [],
        "fail_first_for": "",
        "seed": 7,
    },
    "write": {"divider": "\n\n---\n\n", "max_tokens_default": 1600, "samples_default": 1},
}

MODEL_DEFAULTS: dict[str, Any] = {
    "ctx": 16384,
    "n_gpu_layers": 99,
    "cache_type_k": "f16",
    "cache_type_v": "f16",
    "temperature": 0.85,
    "min_p": 0.05,
    "top_p": 1.0,
    "repeat_penalty": 1.05,
}


def _merge(base: dict, over: dict) -> dict:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


@dataclass
class ModelSpec:
    key: str
    label: str
    repo: str = ""
    file: str = ""
    quant: str = ""
    size_gb: float = 0.0
    base_repo: str = ""
    note: str = ""
    ctx: int = 16384
    n_gpu_layers: int = 99
    cache_type_k: str = "f16"
    cache_type_v: str = "f16"
    temperature: float = 0.85
    min_p: float = 0.05
    top_p: float = 1.0
    repeat_penalty: float = 1.05
    disable_thinking: bool = False
    extra_args: list[str] = field(default_factory=list)

    def gguf_path(self, models_dir: Path) -> Path:
        return Path(models_dir) / self.file

    def is_present(self, models_dir: Path) -> bool:
        return bool(self.file) and self.gguf_path(models_dir).exists()

    def as_public(self, models_dir: Path) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "quant": self.quant,
            "size_gb": self.size_gb,
            "ctx": self.ctx,
            "present": self.is_present(models_dir),
            "disable_thinking": self.disable_thinking,
            "note": self.note,
        }


@dataclass
class Config:
    data: dict
    root: Path
    models: "dict[str, ModelSpec]"
    config_path: Path | None = None
    models_path: Path | None = None

    @property
    def runner(self) -> dict:
        r = self.data["runner"]
        llama = os.environ.get("LOOM_LLAMA_SERVER", "").strip()
        return {**r, "llama_server": llama} if llama else r

    @property
    def mock(self) -> dict:
        return self.data["mock"]

    @property
    def write(self) -> dict:
        return self.data["write"]

    def _path(self, value: str) -> Path:
        p = Path(str(value)).expanduser()
        return p if p.is_absolute() else (self.root / p)

    @property
    def data_dir(self) -> Path:
        try:
            raw = self.data["paths"]["data_dir"]
        except KeyError:
            raise NoDataRoot(
                "the runner has no data root, because nothing has bound one. "
                "Its config deliberately ships none (R4, 2026-09-22): start "
                "loom-server with --data <workspace>, or call "
                "runner.local.use_data_dir(<folder>) before asking the runner "
                "for anything. Do not put a path back into runner/config.toml "
                "- a root written there cannot follow a workspace, which is "
                "exactly what it failed to do for 27 days."
            ) from None
        return self._path(raw)

    @property
    def models_dir(self) -> Path:
        override = os.environ.get("LOOM_MODELS_DIR", "").strip()
        return self._path(override or self.data["paths"]["models_dir"])

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def events_path(self) -> Path:
        return self.data_dir / "runner_events.jsonl"

    def model(self, key: str) -> ModelSpec:
        if key not in self.models:
            raise KeyError(f"unknown model {key!r}; known: {', '.join(self.models)}")
        return self.models[key]

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.runs_dir):
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def load(cls, config_path: Path | None = None, models_path: Path | None = None,
             force_fallback: bool = False) -> "Config":
        if config_path is not None:
            config_path = Path(config_path).expanduser()
            if not config_path.exists():
                raise SystemExit(f"config file not found: {config_path}")
            config_path = config_path.resolve()
        else:
            config_path = find_config("config.toml")
        root = config_path.parent if config_path else Path.cwd()
        data = _merge(DEFAULTS, load_toml(config_path, force_fallback) if config_path else {})

        if models_path is not None:
            models_path = Path(models_path).expanduser()
            if not models_path.exists():
                raise SystemExit(f"models file not found: {models_path}")
            models_path = models_path.resolve()
        else:
            models_path = find_config("models.toml", near=root)
        models: dict[str, ModelSpec] = {}
        if models_path and models_path.exists():
            raw = load_toml(models_path, force_fallback)
            defaults = _merge(MODEL_DEFAULTS, raw.get("defaults", {}))
            for key, entry in (raw.get("models") or {}).items():
                merged = _merge(defaults, entry)
                known = {f for f in ModelSpec.__dataclass_fields__ if f != "key"}
                unknown = set(merged) - known
                if unknown:
                    raise ValueError(
                        f"models.toml [models.{key}] has unknown setting(s): "
                        f"{', '.join(sorted(unknown))}"
                    )
                models[key] = ModelSpec(key=key, **{k: v for k, v in merged.items() if k in known})
        return cls(data=data, root=root, models=models,
                   config_path=config_path, models_path=models_path)


def find_config(name: str, near: Path | None = None) -> Path | None:
    if near:
        c = Path(near) / name
        if c.exists():
            return c.resolve()
    beside = Path(__file__).resolve().parent / name
    return beside.resolve() if beside.exists() else None
