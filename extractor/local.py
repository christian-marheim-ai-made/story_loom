from __future__ import annotations

import threading
from urllib.parse import parse_qs, urlparse

from . import core


class HTTPish(Exception):

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def use_store(load, save, lock, wake, positions=None) -> None:
    core._STORE = {"load": load, "save": save, "positions": positions}
    core._JLOCK = lock
    core._WAKE = wake


_worker_started = False
_worker_lock = threading.Lock()


def configure(data_dir, loom_port: int = 8888) -> None:
    core.DATA = core.Path(str(data_dir)).resolve()
    (core.DATA / "stories").mkdir(parents=True, exist_ok=True)
    (core.DATA / "shared" / "examples").mkdir(parents=True, exist_ok=True)
    core.STUDIO = f"http://127.0.0.1:{loom_port}"
    for m in core.migrate_globals():
        print(f"  migrated      {m}")


def start_worker() -> None:
    global _worker_started
    with _worker_lock:
        _worker_started = True


def _q(path: str) -> dict:
    return parse_qs(urlparse(path).query)


def _needs_root(path: str) -> None:
    if core.DATA is None and not path.startswith("/extract/health"):
        raise HTTPish(503, "the extract lane has no data root. Start the "
                           "server with --extractor-data <folder>; the "
                           "hardcoded default was removed (R1) so that a "
                           "launch can no longer read a scan tree nobody "
                           "pointed it at.")


def get(path: str):
    _needs_root(path)
    try:
        if path.startswith("/extract/list"):
            return core.list_tree()
        if path.startswith("/extract/read"):
            return core.read_text((_q(path).get("path") or [""])[0])
        if path.startswith("/extract/jobs"):
            return {"jobs": [core.job_public(j)
                             for j in core.extract_rows(core.load_jobs())]}
        if path.startswith("/extract/stories"):
            return core.list_stories()
        if path.startswith("/extract/toplist"):
            return core.toplist_view((_q(path).get("story") or [None])[0])
        if path.startswith("/extract/review"):
            q = _q(path)
            st = (q.get("story") or [""])[0]
            ch = (q.get("chapter") or [""])[0]
            return {"review": core.review_load(st, ch),
                    "suggest": core.suggest_categories(
                        st, ch, (q.get("dim") or [""])[0])}
        if path.startswith("/extract/dims"):
            return {"dims": list(core.DIM_TASKS) + ["custom_input"],
                    "tasks": core.DIM_TASKS}
        if path.startswith("/extract/health"):
            return {"ok": True,
                    "data_root": None if core.DATA is None else str(core.DATA),
                    "in_process": True, "loom": core.STUDIO}
    except ValueError as e:
        raise HTTPish(400, str(e)) from e
    raise HTTPish(404, f"no such extract route: {path}")


def post(path: str, body: dict):
    _needs_root(path)
    body = body or {}
    try:
        if path.startswith("/extract/save"):
            rel = body.get("path")
            if not rel:
                rel = "/".join([
                    "stories", core.safe_name(body.get("story"), "story"),
                    core.safe_name(body.get("chapter"), "Chapter 1"),
                    core.safe_name(body.get("file"), "out.txt")])
            return core.write_text(rel, body.get("content", ""))
        if path.startswith("/extract/jobs"):
            parts = path.strip("/").split("/")
            if len(parts) == 2:
                return core.jobs_submit(body)
            if parts[-1] == "cancel":
                return core.jobs_cancel(parts[2] if len(parts) == 4 else None)
            raise HTTPish(404, "bad jobs route")
        if path.startswith("/extract/stories"):
            name = (body.get("story") or body.get("title") or "").strip()
            if not name:
                raise ValueError("story name is required")
            exists = (core.story_meta_path(name).exists()
                      or core.story_dir(name).exists())
            meta = core.story_meta_save(name, body)
            return {"ok": True, "name": core.safe_name(name, "story"),
                    "existed": exists, "meta": meta,
                    "next_chapter_index": core.next_chapter_index(name)}
        if path.startswith("/extract/review"):
            return core.review_set(body)
        if path.startswith("/extract/toplist/add"):
            return core.toplist_add(body)
        if path.startswith("/extract/toplist/update"):
            return core.toplist_update(body)
        if path.startswith("/extract/toplist/remove"):
            return core.toplist_remove(body)
        if path.startswith("/extract/examples/append"):
            return core.examples_append(body)
        if path.startswith("/extract/compile"):
            return core.compile_prompts(
                body.get("dim", ""), body.get("text", ""),
                custom=body.get("custom", ""),
                max_tokens=int(body.get("max_tokens") or 900),
                fix=body.get("fix", ""), only=body.get("only"),
                story=body.get("story"))
    except HTTPish:
        raise
    except ValueError as e:
        raise HTTPish(400, str(e)) from e
    raise HTTPish(404, f"no such extract route: {path}")
