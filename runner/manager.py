
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

from .config import Config
from .base import Backend, BackendError
from .llamacpp import LlamaCppBackend, probe_server
from .mock import MockBackend


class ConcurrencyViolation(RuntimeError):
    pass


LOOM_WORKER = "loom-worker"


class LeaseHeld(RuntimeError):
    pass


class GpuLease:

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._holder: str | None = None
        self._since: float | None = None
        self._model_key: str | None = None

    def check(self, holder: str) -> str:
        who = str(holder or "").strip()
        if not who:
            raise ValueError(
                "a lease needs a holder: name who is taking the card")
        with self._lock:
            if self._holder is not None and self._holder != who:
                raise LeaseHeld(
                    f"the card is held by {self._holder!r}, taken "
                    f"{self._held_for():.0f}s ago for {self._model_key!r}; "
                    f"{who!r} cannot take it")
        return who

    def grant(self, holder: str, model_key: str | None = None) -> None:
        with self._lock:
            who = self.check(holder)
            if self._holder is None:
                self._since = time.time()
            self._holder = who
            self._model_key = model_key

    def release(self) -> bool:
        with self._lock:
            held = self._holder is not None
            self._holder = None
            self._since = None
            self._model_key = None
            return held

    def public(self, now: float | None = None) -> dict:
        with self._lock:
            if self._holder is None:
                return {"holder": None, "since": None, "held_seconds": None,
                        "model_key": None}
            t = time.time() if now is None else now
            return {"holder": self._holder, "since": self._since,
                    "held_seconds": round(max(0.0, t - (self._since or t)), 1),
                    "model_key": self._model_key}

    def _held_for(self) -> float:
        return 0.0 if self._since is None else max(0.0, time.time() - self._since)


class ModelManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.backend_kind = str(cfg.runner.get("backend", "mock")).lower()
        self._lock = threading.RLock()
        self._backend: Backend | None = None
        self._key: str | None = None
        self._loaded_at: float | None = None
        self._last_used: float | None = None
        self._load_seconds: float = 0.0
        self._loads = 0
        self._live = 0
        self.events_path = cfg.events_path
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self.loads_path = loads_path_for(self.events_path)
        self._loaded_keys: set[str] = set()
        self._load_cold: bool | None = None
        self._load_baseline: float | None = None
        self._load_slow = False
        self._lease = GpuLease()
        self._event("session_start", pid=os.getpid())

    def rebind_paths(self) -> None:
        with self._lock:
            self.events_path = self.cfg.events_path
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            self.loads_path = loads_path_for(self.events_path)

    def _event(self, event: str, **fields) -> None:
        record = {"ts": time.time(), "event": event, "backend": self.backend_kind,
                  "live_after": self._live, **fields}
        with open(self.events_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def status(self) -> dict:
        return {
            "backend": self.backend_kind,
            "loaded": self._key,
            "loaded_label": self._backend.describe() if self._backend else None,
            "loaded_at": self._loaded_at,
            "last_used": self._last_used,
            "load_seconds": round(self._load_seconds, 2),
            "loads_this_session": self._loads,
            "pid": getattr(self._backend, "pid", None),
            "idle_unload_s": float(self.cfg.runner.get("idle_unload_s", 600)),
            "load_cold": self._load_cold,
            "load_baseline_seconds": (round(self._load_baseline, 2)
                                      if self._load_baseline is not None else None),
            "load_slow": self._load_slow,
            "lease": self._lease.public(),
        }

    VRAM_SLACK_GB = 1.5

    @staticmethod
    def free_vram_mib() -> int | None:
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10)
            if out.returncode != 0:
                return None
            return int(out.stdout.strip().splitlines()[0].strip())
        except Exception:
            return None

    def _check_vram(self, model_key: str) -> None:
        if self.backend_kind != "llamacpp":
            return
        free = self.free_vram_mib()
        if free is None:
            self._event("vram_unknown", model_key=model_key)
            return
        spec = self.cfg.model(model_key)
        gguf = spec.gguf_path(self.cfg.models_dir)
        try:
            weights = gguf.stat().st_size / 1024 ** 3
        except OSError:
            return
        kv = 0.0
        try:
            from .gguf import kv_cache_bytes, read_header
            per = 1.0 if str(spec.cache_type_k).startswith("q8") else 2.0
            n = kv_cache_bytes(read_header(gguf), int(spec.ctx), per)
            kv = (n or 0) / 1024 ** 3
        except Exception:
            kv = 0.0
        need = weights + kv + self.VRAM_SLACK_GB
        have = free / 1024
        self._event("vram_check", model_key=model_key,
                    need_gb=round(need, 2), free_gb=round(have, 2))
        if have < need:
            raise BackendError(
                f"not enough free VRAM for {model_key}: needs about "
                f"{need:.1f} GB (weights {weights:.1f} + KV {kv:.1f} + "
                f"{self.VRAM_SLACK_GB} slack) and the card has {have:.1f} GB "
                f"free. Something else is holding it - another loom-server, "
                f"another agent's run, or an orphaned llama-server. Free it, or "
                f"pick a smaller model."
            )

    def startup_check(self, reclaim: bool = True) -> dict:
        if self.backend_kind != "llamacpp":
            return {"orphan": None, "reclaimed": 0}
        port = int(self.cfg.runner.get("port", 8901))
        info = probe_server("127.0.0.1", port)
        if not info:
            return {"orphan": None, "reclaimed": 0}
        self._event("orphan_detected", detail=info)
        if not reclaim:
            return {"orphan": info, "reclaimed": 0}
        killed = self._kill_on_port(port)
        self._event("orphan_reclaimed", port=port, pids=killed,
                    models=info.get("models"))
        return {"orphan": info if not killed else None,
                "reclaimed": len(killed), "reclaimed_detail": info}

    @staticmethod
    def _kill_on_port(port: int) -> list:
        pids = []
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Get-NetTCPConnection -LocalPort {port} -State Listen "
                 f"-ErrorAction SilentlyContinue | "
                 f"Select-Object -ExpandProperty OwningProcess"],
                capture_output=True, text=True, timeout=15)
            pids = [int(x) for x in out.stdout.split() if x.strip().isdigit()]
        except Exception:
            return []
        done = []
        for pid in pids:
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=15)
                done.append(pid)
            except Exception:
                pass
        if done:
            for _ in range(20):
                if not probe_server("127.0.0.1", port):
                    break
                time.sleep(0.5)
        return done

    def _make(self, model_key: str) -> Backend:
        spec = self.cfg.model(model_key)
        if self.backend_kind == "mock":
            return MockBackend(model_key, spec.label, self.cfg.mock)
        if self.backend_kind == "llamacpp":
            return LlamaCppBackend(spec, self.cfg.models_dir, self.cfg.runner,
                                   self.cfg.data_dir / "logs")
        raise BackendError(f"unknown runner backend {self.backend_kind!r} (expected mock|llamacpp)")

    def acquire(self, model_key: str, holder: str) -> Backend:
        with self._lock:
            self._lease.check(holder)
            warm = bool(self.cfg.runner.get("warm_keep", True))
            if self._backend is not None and self._key == model_key and warm:
                if self._backend.is_alive():
                    self._last_used = time.time()
                    self._lease.grant(holder, model_key)
                    self._event("reuse", model_key=model_key)
                    return self._backend
                self._event("stale_backend", model_key=model_key,
                            detail={"was_loaded_at": self._loaded_at,
                                    "reason": "process gone under a warm "
                                              "backend; reloading"})
                self.unload()
            if self._backend is not None:
                self.unload()
            if self._live != 0:
                raise ConcurrencyViolation(
                    f"refusing to load {model_key}: {self._live} backend(s) still live"
                )
            backend = self._make(model_key)
            self._lease.grant(holder, model_key)
            try:
                self._check_vram(model_key)
                self._event("load_start", model_key=model_key)
                started = time.time()
                try:
                    backend.start()
                except Exception as exc:
                    self._event("load_failed", model_key=model_key, detail=str(exc)[:500])
                    raise
            except Exception:
                self._lease.release()
                raise
            self._live += 1
            self._backend = backend
            self._key = model_key
            self._loaded_at = time.time()
            self._last_used = self._loaded_at
            self._load_seconds = time.time() - started
            self._loads += 1
            cold = model_key not in self._loaded_keys
            self._loaded_keys.add(model_key)
            self._event("load_ok", model_key=model_key, pid=backend.pid,
                        cold=cold,
                        load_seconds=round(self._load_seconds, 3))
            self._note_load(model_key, cold, self._load_seconds)
            return backend

    def _note_load(self, model_key: str, cold: bool, seconds: float) -> None:
        self._load_cold = cold
        try:
            note = note_load(self.loads_path, model_key, cold, seconds)
        except Exception as exc:
            self._event("load_record_failed", model_key=model_key,
                        detail=str(exc)[:500])
            self._load_baseline, self._load_slow = None, False
            return
        self._load_baseline = note["baseline"]
        self._load_slow = bool(note["slow"])
        if not self._load_slow:
            return
        self._event("load_slow", model_key=model_key,
                    cold=cold, load_seconds=round(seconds, 3),
                    baseline_seconds=round(note["baseline"], 3),
                    samples=note["n"])
        print(f"[runner] {SLOW_LOAD_WARNING} - {model_key} took "
              f"{seconds:.0f}s and normally takes about "
              f"{note['baseline']:.0f}s here", flush=True)

    def touch(self) -> None:
        self._last_used = time.time()

    def unload(self) -> None:
        with self._lock:
            if self._backend is None:
                return
            key, pid = self._key, getattr(self._backend, "pid", None)
            self._event("unload_start", model_key=key, pid=pid)
            try:
                self._backend.stop()
            finally:
                self._live = max(0, self._live - 1)
                self._backend = None
                self._key = None
                self._loaded_at = None
                self._load_seconds = 0.0
                self._load_cold = None
                self._load_baseline = None
                self._load_slow = False
                self._lease.release()
            self._event("unload_ok", model_key=key, pid=pid)

    def maybe_idle_unload(self) -> bool:
        with self._lock:
            if self._backend is None or self._last_used is None:
                return False
            idle_for = time.time() - self._last_used
            limit = float(self.cfg.runner.get("idle_unload_s", 600))
            if limit <= 0 or idle_for < limit:
                return False
            self._event("idle_unload", model_key=self._key, idle_seconds=round(idle_for, 1))
            self.unload()
            return True

    def shutdown(self) -> None:
        try:
            self.unload()
        except Exception as exc:
            self._event("shutdown_error", detail=str(exc)[:500])


def read_events(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def max_concurrency(events: list[dict]) -> int:
    live = 0
    peak = 0
    for ev in events:
        event = ev.get("event")
        if event == "session_start":
            live = 0
        elif event == "load_ok":
            live += 1
            peak = max(peak, live)
        elif event == "unload_ok":
            live = max(0, live - 1)
    return peak


def orphaned_loads(events: list[dict]) -> list[dict]:
    open_loads: list[dict] = []
    orphans: list[dict] = []
    for ev in events:
        event = ev.get("event")
        if event == "session_start":
            orphans.extend(open_loads)
            open_loads = []
        elif event == "load_ok":
            open_loads.append(ev)
        elif event == "unload_ok" and open_loads:
            open_loads.pop()
    return orphans



LOAD_SAMPLES_KEPT = 50

LOAD_BASELINE_MIN = 3

LOAD_SLOW_FACTOR = 2.0
LOAD_SLOW_FLOOR_S = 10.0

SLOW_LOAD_WARNING = "Model loading is slow, check computer"


def loads_path_for(events_path) -> Path:
    return Path(events_path).parent / "model_loads.json"


def load_bucket(model_key: str, cold: bool) -> str:
    return f"{model_key}::{'cold' if cold else 'warm'}"


def load_baseline(samples) -> "float | None":
    values = sorted(float(s) for s in (samples or []))
    if len(values) < LOAD_BASELINE_MIN:
        return None
    mid = len(values) // 2
    return (values[mid] if len(values) % 2
            else (values[mid - 1] + values[mid]) / 2.0)


def load_is_slow(seconds, baseline) -> bool:
    if baseline is None:
        return False
    seconds, baseline = float(seconds), float(baseline)
    return (seconds >= baseline * LOAD_SLOW_FACTOR
            and seconds - baseline >= LOAD_SLOW_FLOOR_S)


def read_loads(path) -> dict:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    buckets = raw.get("buckets") if isinstance(raw, dict) else None
    return buckets if isinstance(buckets, dict) else {}


def note_load(path, model_key: str, cold: bool, seconds: float,
              at: "float | None" = None) -> dict:
    path = Path(path)
    buckets = read_loads(path)
    key = load_bucket(model_key, cold)
    entry = buckets.get(key)
    if not isinstance(entry, dict):
        entry = {}
    samples = [s for s in (entry.get("samples") or []) if isinstance(s, (int, float))]
    baseline = load_baseline(samples)
    slow = load_is_slow(seconds, baseline)
    over = len(samples)

    samples = (samples + [round(float(seconds), 3)])[-LOAD_SAMPLES_KEPT:]
    buckets[key] = {
        "model": model_key,
        "cold": bool(cold),
        "samples": samples,
        "n": int(entry.get("n") or 0) + 1,
        "last": round(float(seconds), 3),
        "last_at": float(at if at is not None else time.time()),
    }
    _write_loads(path, buckets)
    return {"bucket": key, "cold": bool(cold), "seconds": float(seconds),
            "baseline": baseline, "slow": slow, "n": over}


def _write_loads(path: Path, buckets: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps({"version": 1, "buckets": buckets},
                              ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, path)
