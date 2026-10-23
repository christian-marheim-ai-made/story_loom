
from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator

from .config import ModelSpec
from .gguf import GGUFError, check_against_spec, read_header
from .jobobject import JobObjectError, KillOnCloseJob
from .base import Backend, BackendError, Cancelled, GenParams

OPTIONAL_FLAG_NAMES = ("--reasoning", "-fa", "-ctk", "-ctv", "-a")


def port_in_use(host: str, port: int, timeout: float = 0.35) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, int(port))) == 0


def probe_server(host: str, port: int, timeout: float = 1.5) -> dict | None:
    if not port_in_use(host, port):
        return None
    info: dict = {"host": host, "port": port, "reachable": True, "models": [], "health": None}
    for path, key in (("/health", "health"), ("/v1/models", "models")):
        try:
            with urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=timeout) as resp:
                doc = json.loads(resp.read().decode("utf-8", "replace"))
                if key == "models":
                    info["models"] = [m.get("id") for m in doc.get("data", []) if isinstance(m, dict)]
                else:
                    info["health"] = doc
        except urllib.error.HTTPError as exc:
            if key == "health":
                info["health"] = {"http_status": exc.code}
        except Exception:
            pass
    return info


class LlamaCppBackend(Backend):
    kind = "llamacpp"

    def __init__(self, spec: ModelSpec, models_dir: Path, runner_cfg: dict, log_dir: Path):
        super().__init__(spec.key, spec.label)
        self.spec = spec
        self.models_dir = Path(models_dir)
        self.cfg = runner_cfg
        self.host = "127.0.0.1"
        self.port = int(runner_cfg.get("port", 8901))
        self.proc: subprocess.Popen | None = None
        self.log_dir = Path(log_dir)
        self.log_path = self.log_dir / f"llama-server-{spec.key}.log"
        self._log_fh = None
        self.load_seconds = 0.0
        self.command: list[str] = []
        self.dropped_flags: list[str] = []
        self._job: KillOnCloseJob | None = None
        self.job_kill_on_close = False

    def build_command(self, drop_optional: bool = False) -> list[str]:
        gguf = self.spec.gguf_path(self.models_dir)
        cmd = [
            str(self.cfg.get("llama_server", "llama-server")),
            "-m", str(gguf),
            "--host", self.host,
            "--port", str(self.port),
            "-ngl", str(self.spec.n_gpu_layers),
            "-c", str(self.spec.ctx),
        ]
        if drop_optional:
            return cmd
        cmd += ["-a", self.spec.key]
        cmd += ["-ctk", self.spec.cache_type_k, "-ctv", self.spec.cache_type_v]
        cmd += ["-fa", str(self.cfg.get("flash_attn", "on"))]
        if self.spec.disable_thinking:
            cmd += ["--reasoning", "off"]
        cmd += [str(a) for a in (self.spec.extra_args or [])]
        return cmd

    def start(self) -> None:
        gguf = self.spec.gguf_path(self.models_dir)
        if not gguf.exists():
            raise BackendError(
                f"model file not found: {gguf}\n"
                f"Run `python -m story_studio download --only {self.spec.key}`, "
                f"or point models.toml at a file you already have."
            )
        try:
            info = read_header(gguf, read_tensors=False)
        except GGUFError as exc:
            raise BackendError(
                f"{gguf.name} is not a usable GGUF: {exc}\n"
                f"The download is probably truncated — delete the file and fetch it again."
            ) from None
        except OSError as exc:
            raise BackendError(f"cannot read {gguf}: {exc}") from None
        for problem in check_against_spec(info, self.spec):
            self._note(f"{gguf.name}: {problem}")
        if port_in_use(self.host, self.port):
            raise BackendError(
                f"port {self.port} is already in use. Something else — probably an "
                f"orphaned llama-server — is holding it. Close it and try again."
            )
        self.log_dir.mkdir(parents=True, exist_ok=True)
        started = time.time()
        for attempt, drop in enumerate((False, True)):
            self.command = self.build_command(drop_optional=drop)
            if drop:
                self.dropped_flags = [f for f in OPTIONAL_FLAG_NAMES
                                      if f in self.build_command(False)]
            self._log_fh = open(self.log_path, "a", encoding="utf-8", errors="replace")
            self._log_fh.write(
                f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} launch attempt {attempt + 1} =====\n"
                f"{' '.join(self.command)}\n"
            )
            self._log_fh.flush()
            kwargs: dict = {"stdout": self._log_fh, "stderr": subprocess.STDOUT}
            if sys.platform == "win32":
                kwargs["creationflags"] = (
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
            else:
                kwargs["start_new_session"] = True
            try:
                self.proc = subprocess.Popen(self.command, **kwargs)
            except FileNotFoundError:
                raise BackendError(
                    f"could not run {self.command[0]!r}. Put llama-server on PATH or set "
                    f"[runner] llama_server in config.toml to its full path."
                ) from None
            self.pid = self.proc.pid
            self._hold_in_kill_on_close_job()
            try:
                self._await_health()
            except BackendError as exc:
                tail = self._log_tail()
                self._hard_stop()
                looks_like_bad_flag = any(
                    s in tail.lower()
                    for s in ("invalid argument", "unknown value", "unrecognized",
                              "error while handling argument")
                )
                if attempt == 0 and looks_like_bad_flag:
                    self._note(
                        "llama-server rejected an argument; retrying without the optional "
                        f"flags ({', '.join(OPTIONAL_FLAG_NAMES)}). Your llama.cpp build may "
                        "predate them. Log: " + str(self.log_path)
                    )
                    continue
                raise BackendError(f"{exc}\n--- llama-server log tail ---\n{tail}") from None
            self.load_seconds = time.time() - started
            return

    def _hold_in_kill_on_close_job(self) -> None:
        if self.proc is None:
            return
        if not sys.platform == "win32":
            return
        try:
            job = KillOnCloseJob()
            job.add(self.proc.pid)
        except JobObjectError as exc:
            self.job_kill_on_close = False
            self._note(
                f"could NOT put llama-server (pid {self.proc.pid}) in a "
                f"kill-on-close job: {exc}. The model will load, but if this "
                f"process is force-killed or crashes, llama-server will keep "
                f"the card - see runner/jobobject.py. `./start.sh --stop` still "
                f"kills it by name.")
            return
        self._job = job
        self.job_kill_on_close = True

    def _release_job(self) -> None:
        if self._job is not None:
            self._job.close()
            self._job = None
        self.job_kill_on_close = False

    def _note(self, message: str) -> None:
        print(f"[runner] {message}", flush=True)
        if self._log_fh:
            self._log_fh.write(f"[story-studio] {message}\n")
            self._log_fh.flush()

    def _await_health(self) -> None:
        deadline = time.time() + float(self.cfg.get("startup_timeout_s", 600))
        poll = float(self.cfg.get("health_poll_s", 1.0))
        url = f"http://{self.host}:{self.port}/health"
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise BackendError(
                    f"llama-server exited with code {self.proc.returncode} before becoming healthy"
                )
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    if resp.status == 200:
                        return
            except urllib.error.HTTPError as exc:
                if exc.code != 503:
                    raise BackendError(f"/health returned HTTP {exc.code}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                pass
            time.sleep(poll)
        raise BackendError(
            f"llama-server did not become healthy within "
            f"{self.cfg.get('startup_timeout_s')}s"
        )

    def _log_tail(self, lines: int = 25) -> str:
        try:
            return "\n".join(self.log_path.read_text(encoding="utf-8", errors="replace")
                             .splitlines()[-lines:])
        except OSError:
            return "(no log)"

    def _hard_stop(self) -> None:
        if self.proc is not None:
            try:
                self.proc.kill()
                self.proc.wait(timeout=15)
            except Exception:
                pass
        self._release_job()
        if self._log_fh:
            try:
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None
        self.proc = None

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.proc is None:
            return
        timeout = float(self.cfg.get("shutdown_timeout_s", 60))
        proc = self.proc
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._note(f"llama-server (pid {proc.pid}) ignored terminate; killing")
            try:
                proc.kill()
                proc.wait(timeout=timeout)
            except Exception as exc:
                raise BackendError(f"could not stop llama-server pid {proc.pid}: {exc}") from None
        self.proc = None
        self.pid = None
        self._release_job()
        if self._log_fh:
            try:
                self._log_fh.close()
            finally:
                self._log_fh = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not port_in_use(self.host, self.port):
                return
            time.sleep(0.25)
        raise BackendError(
            f"llama-server exited but port {self.port} is still held. Refusing to load "
            f"another model on top of it."
        )

    def stream(self, user_message: str, params: GenParams,
               cancel: threading.Event | None = None) -> Iterator[dict]:
        body = {
            "model": self.spec.key,
            "messages": [{"role": "user", "content": user_message}],
            "stream": True,
            "stream_options": {"include_usage": True},
            **params.as_body(),
        }
        if self.spec.disable_thinking:
            kwargs = dict(body.get("chat_template_kwargs") or {})
            kwargs["enable_thinking"] = False
            body["chat_template_kwargs"] = kwargs
        req = urllib.request.Request(
            f"http://{self.host}:{self.port}/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
            method="POST",
        )
        finish_reason = None
        usage: dict = {}
        timings: dict = {}
        try:
            resp = urllib.request.urlopen(req, timeout=float(self.cfg.get("request_timeout_s", 1800)))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:600]
            raise BackendError(f"HTTP {exc.code} from llama-server: {detail}") from None
        except urllib.error.URLError as exc:
            raise BackendError(f"cannot reach llama-server: {exc.reason}") from None
        try:
            for raw in resp:
                if cancel is not None and cancel.is_set():
                    resp.close()
                    raise Cancelled("cancelled during generation")
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if obj.get("usage"):
                    usage = obj["usage"]
                if obj.get("timings"):
                    timings = obj["timings"]
                for choice in obj.get("choices") or []:
                    delta = choice.get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        yield {"type": "token", "text": piece}
                    rpiece = delta.get("reasoning_content")
                    if rpiece:
                        yield {"type": "reasoning", "text": rpiece}
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
        finally:
            try:
                resp.close()
            except Exception:
                pass
        yield {
            "type": "done",
            "finish_reason": finish_reason or "stop",
            "usage": usage,
            "timings": timings,
        }
