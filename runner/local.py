from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from pathlib import Path

from .config import Config, NoDataRoot
from .manager import LOOM_WORKER, ModelManager
from .base import BackendError, Cancelled, GenParams, strip_thinking

KEEP_JOBS = 64


class SubmitError(Exception):
    pass


def _word_count(text: str) -> int:
    return len(text.split())


def _payload_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class LocalRunner:

    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or Config.load()
        self.manager = ModelManager(self.cfg)
        self._jobs: "OrderedDict[str, dict]" = OrderedDict()
        self._lock = threading.Lock()
        self._queue: list = []
        self._wake = threading.Event()
        self._cancels: dict = {}
        self._seq = 0
        self._stop = False
        _chk = self.manager.startup_check()
        self._orphan = _chk.get("orphan")
        self._reclaimed = int(_chk.get("reclaimed") or 0)
        self._worker = threading.Thread(target=self._run_forever, daemon=True,
                                        name="local-runner")
        self._worker.start()

    def _mint(self) -> str:
        self._seq += 1
        return f"{time.strftime('%Y%m%d-%H%M%S')}-wri-{self._seq:04d}"

    def status(self) -> dict:
        return {
            "version": "local",
            "backend": self.manager.backend_kind,
            "runner": self.manager.status(),
            "orphan": self._orphan,
            "reclaimed_on_startup": self._reclaimed,
            "models": [m.as_public(self.cfg.models_dir)
                       for m in self.cfg.models.values()],
            "models_dir": str(self.cfg.models_dir),
            "queued": sum(1 for j in self._jobs.values() if j["status"] == "queued"),
            "write_defaults": {
                "max_tokens": self.cfg.write["max_tokens_default"],
                "samples": self.cfg.write["samples_default"],
                "divider": self.cfg.write["divider"],
            },
        }

    def submit_write(self, body: dict) -> str:
        prompt = str(body.get("prompt") or "")
        if not prompt.strip():
            raise SubmitError("the prompt is empty")
        keys = list(body.get("model_keys") or [])
        if not keys:
            raise SubmitError("select at least one model")
        unknown = [k for k in keys if k not in self.cfg.models]
        if unknown:
            raise SubmitError(f"unknown model(s): {', '.join(unknown)}")
        samples = int(body.get("samples") or 1)
        max_tokens = int(body.get("max_tokens") or self.cfg.write["max_tokens_default"])
        seed = body.get("seed")
        seed = int(seed) if str(seed or "").strip() else None
        temperature = (float(body["temperature"])
                       if str(body.get("temperature", "")).strip() not in ("", "None")
                       else None)

        jid = self._mint()
        variants = []
        for key in keys:
            for s in range(1, samples + 1):
                variants.append({
                    "label": f"{key}#{s:02d}" if samples > 1 or len(keys) > 1 else key,
                    "status": "queued",
                    "model_key": key,
                    "error": None,
                    "progress": {},
                    "meta": {
                        "sample": s, "seed": seed, "max_tokens": max_tokens,
                        "temperature": temperature,
                        "dry_multiplier": body.get("dry_multiplier"),
                        "dry_base": body.get("dry_base"),
                        "dry_allowed_length": body.get("dry_allowed_length"),
                    },
                    "_text": "",
                })
        job = {
            "id": jid, "kind": "write", "status": "queued",
            "title": prompt.strip().splitlines()[0][:80] if prompt.strip() else "",
            "created_at": time.time(), "finished_at": None, "error": None,
            "blind": False, "revealed": True,
            "request": {"summary_source": body.get("summary_source", ""),
                        "payload_sha256": _payload_hash(prompt),
                        "payload_chars": len(prompt)},
            "payload": prompt, "rankings": None,
            "variants": variants, "has_report": False,
        }
        with self._lock:
            self._jobs[jid] = job
            self._queue.append(jid)
            while len(self._jobs) > KEEP_JOBS:
                old, j = next(iter(self._jobs.items()))
                if j["status"] in ("queued", "running"):
                    break
                self._jobs.popitem(last=False)
        self._wake.set()
        return jid

    def job(self, job_id: str) -> dict | None:
        with self._lock:
            j = self._jobs.get(job_id)
            if j is None:
                return None
            return {
                **{k: v for k, v in j.items() if k != "variants"},
                "variants": [{k: v for k, v in var.items() if k != "_text"}
                             for var in j["variants"]],
            }

    def output(self, job_id: str, label: str) -> str:
        with self._lock:
            j = self._jobs.get(job_id)
            if not j:
                return ""
            for v in j["variants"]:
                if v["label"] == label:
                    return v.get("_text") or ""
        return ""

    def cancel(self, job_id: str) -> dict:
        with self._lock:
            j = self._jobs.get(job_id)
            if j is None:
                raise KeyError(job_id)
            self._cancels.setdefault(job_id, threading.Event()).set()
            n = 0
            for v in j["variants"]:
                if v["status"] in ("queued", "running"):
                    v["status"] = "cancelled"
                    n += 1
            if j["status"] in ("queued", "running"):
                j["status"] = "cancelled"
                j["finished_at"] = time.time()
            queued = sum(1 for x in self._jobs.values() if x["status"] == "queued")
        self._wake.set()
        return {"cancelled": n, "queued_remaining": queued}

    def _run_forever(self) -> None:
        while not self._stop:
            jid = None
            with self._lock:
                while self._queue:
                    candidate = self._queue.pop(0)
                    j = self._jobs.get(candidate)
                    if j and j["status"] == "queued":
                        jid = candidate
                        break
            if jid is None:
                self.manager.maybe_idle_unload()
                self._wake.wait(0.25)
                self._wake.clear()
                continue
            self._run_job(jid)

    def _run_job(self, jid: str) -> None:
        with self._lock:
            job = self._jobs.get(jid)
            if job is None or job["status"] != "queued":
                return
            job["status"] = "running"
            payload = job["payload"]
            variants = list(job["variants"])
        failed = False
        for v in variants:
            if self._cancels.get(jid, threading.Event()).is_set():
                break
            try:
                self._run_variant(jid, payload, v)
            except Cancelled:
                v["status"] = "cancelled"
            except (BackendError, Exception) as exc:
                v["status"] = "failed"
                v["error"] = str(exc)
                failed = True
        with self._lock:
            job = self._jobs.get(jid)
            if job is None:
                return
            if job["status"] == "cancelled":
                pass
            elif failed:
                job["status"] = "failed"
                job["error"] = next((v["error"] for v in variants if v.get("error")), None)
            else:
                job["status"] = "done"
            job["finished_at"] = time.time()
        self._cancels.pop(jid, None)

    def _run_variant(self, jid: str, payload: str, v: dict) -> None:
        spec = self.cfg.model(v["model_key"])
        m_in = v["meta"]
        cancel = self._cancels.setdefault(jid, threading.Event())
        params = GenParams(
            temperature=float(m_in["temperature"]
                              if m_in.get("temperature") is not None
                              else spec.temperature),
            top_p=spec.top_p, min_p=spec.min_p, repeat_penalty=spec.repeat_penalty,
            dry_multiplier=m_in.get("dry_multiplier"),
            dry_base=m_in.get("dry_base"),
            dry_allowed_length=m_in.get("dry_allowed_length"),
            seed=m_in.get("seed"), max_tokens=int(m_in["max_tokens"]),
        )
        v["status"] = "running"

        loads_before = self.manager.status()["loads_this_session"]
        load_started = time.time()
        backend = self.manager.acquire(v["model_key"], LOOM_WORKER)
        warm = self.manager.status()["loads_this_session"] == loads_before
        load_seconds = 0.0 if warm else round(time.time() - load_started, 3)

        out: list = []
        reasoning: list = []
        tokens = 0
        finish_reason = "stop"
        usage: dict = {}
        timings: dict = {}
        started = time.time()
        for event in backend.stream(payload, params, cancel):
            kind = event["type"]
            if kind in ("token", "reasoning"):
                (out if kind == "token" else reasoning).append(event["text"])
                tokens += 1
                if tokens % 8 == 0:
                    elapsed = max(time.time() - started, 1e-6)
                    v["progress"] = {
                        "stage": "generating" if kind == "token" else "thinking",
                        "tokens": tokens, "tok_s": round(tokens / elapsed, 1),
                        "elapsed": round(elapsed, 1), "model": spec.label,
                        "sample": m_in.get("sample"),
                    }
            elif kind == "done":
                finish_reason = event.get("finish_reason") or "stop"
                usage = event.get("usage") or {}
                timings = event.get("timings") or {}

        prose = "".join(out)
        if spec.disable_thinking:
            cleaned = strip_thinking(prose)
            stripped = cleaned != prose
            text = cleaned
        else:
            stripped = False
            cleaned = prose
            text = ("<think>" + "".join(reasoning) + "</think>" + prose
                    if reasoning else prose)

        gen_seconds = max(time.time() - started, 1e-6)
        self.manager.touch()
        completion = int(usage.get("completion_tokens") or tokens)
        v["_text"] = text
        v["status"] = "done"
        v["meta"] = {
            **m_in,
            "label": v["label"], "kind": "write",
            "model_key": v["model_key"], "model_label": spec.label,
            "temperature": params.temperature,
            "top_p": params.top_p, "min_p": params.min_p,
            "repeat_penalty": params.repeat_penalty,
            "finish_reason": finish_reason,
            "hit_length_cap": finish_reason == "length",
            "word_count": _word_count(cleaned),
            "token_count": completion,
            "prompt_tokens": usage.get("prompt_tokens"),
            "load_seconds": load_seconds, "warm_start": warm,
            "gen_seconds": round(gen_seconds, 3),
            "tokens_per_second": round(completion / gen_seconds, 2),
            "payload_sha256": _payload_hash(payload),
            "payload_chars": len(payload),
            "thinking_stripped": stripped,
            "backend": self.manager.backend_kind,
            "server_timings": timings,
            "finished_at": time.time(),
        }
        v["progress"] = {"stage": "done", "tokens": completion,
                         "tok_s": v["meta"]["tokens_per_second"],
                         "elapsed": round(gen_seconds, 1), "model": spec.label}

    def shutdown(self) -> None:
        self._stop = True
        self._wake.set()
        self.manager.shutdown()



_RUNNER: LocalRunner | None = None
_RUNNER_LOCK = threading.Lock()


_DATA_DIR: str | None = None


def use_data_dir(path) -> None:
    global _DATA_DIR, _RUNNER
    root = Path(str(path))
    root.mkdir(parents=True, exist_ok=True)
    with _RUNNER_LOCK:
        _DATA_DIR = str(root)
        if _RUNNER is not None:
            _RUNNER.cfg.data["paths"]["data_dir"] = _DATA_DIR
            _RUNNER.cfg.ensure_dirs()
            _RUNNER.manager.rebind_paths()


def runner() -> LocalRunner:
    global _RUNNER
    with _RUNNER_LOCK:
        if _RUNNER is None:
            if not _DATA_DIR:
                raise NoDataRoot(
                    "no workspace is bound, so the runner has nowhere to put "
                    "its own files - its runs, its queue state, its model-load "
                    "audit and llama-server's logs. loom-server binds one from "
                    "--data before it serves anything; a caller standing the "
                    "runner up by hand calls use_data_dir(<folder>) first.")
            cfg = Config.load()
            cfg.data["paths"]["data_dir"] = _DATA_DIR
            _RUNNER = LocalRunner(cfg)
        return _RUNNER


class HTTPish(Exception):

    def __init__(self, code: int, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


def post(path: str, body: dict):
    r = runner()
    if path == "/api/jobs/write":
        try:
            return {"job_id": r.submit_write(body)}
        except SubmitError as exc:
            raise HTTPish(400, str(exc)) from None
        except (ValueError, TypeError) as exc:
            raise HTTPish(400, str(exc)) from None
    if path.startswith("/api/jobs/") and path.endswith("/cancel"):
        jid = path[len("/api/jobs/"):-len("/cancel")]
        try:
            return r.cancel(jid)
        except KeyError:
            raise HTTPish(404, "no such job") from None
    raise HTTPish(404, f"no such route {path}")


def get(path: str):
    r = runner()
    if path == "/api/status":
        return r.status()
    if path.startswith("/api/jobs/"):
        rest = path[len("/api/jobs/"):]
        if "/variants/" in rest and rest.endswith("/output"):
            jid, tail = rest.split("/variants/", 1)
            return r.output(jid, tail[:-len("/output")])
        j = r.job(rest)
        if j is None:
            raise HTTPish(404, "no such job")
        return j
    raise HTTPish(404, f"no such route {path}")
