from __future__ import annotations

import json
import re
from pathlib import Path

FIELDS = ("seq", "t", "ms", "cmd", "cs", "args", "ops", "corr", "ok", "err")

MAX_ROWS = 5000

def trace_dir(story_dir) -> Path:
    return Path(story_dir) / "trace"


def _scalar(v):
    return v is None or isinstance(v, (str, int, float, bool))


def clean_row(row: dict) -> dict:
    if not isinstance(row, dict):
        raise ValueError("a trace row must be an object")
    args = row.get("args") or {}
    if not isinstance(args, dict):
        raise ValueError("args must be an object")
    for k, v in args.items():
        if not _scalar(v):
            raise ValueError(
                f"args.{k} is a {type(v).__name__}, not a scalar - the client "
                f"is meant to have typed and flattened it (R2)")
    ops = row.get("ops") or []
    if not isinstance(ops, list):
        raise ValueError("ops must be a list")
    out = {k: row[k] for k in FIELDS if k in row}
    out["args"] = dict(args)
    out["ops"] = [str(o) for o in ops]
    return out


def trace_append(story_dir, rows) -> dict:
    if not str(story_dir or "").strip():
        return {"error": "a trace belongs to a story: no story directory given"}
    if not isinstance(rows, list) or not rows:
        return {"error": "nothing to append"}
    try:
        clean = [clean_row(r) for r in rows]
    except ValueError as e:
        return {"error": str(e)}

    d = trace_dir(story_dir)
    d.mkdir(parents=True, exist_ok=True)
    live = d / "trace.jsonl"

    n_before = 0
    if live.exists():
        with live.open("r", encoding="utf-8") as f:
            n_before = sum(1 for _ in f)
    if n_before >= MAX_ROWS:
        k = 1
        while (d / f"trace-{k}.jsonl").exists():
            k += 1
        live.rename(d / f"trace-{k}.jsonl")
        n_before = 0

    with live.open("a", encoding="utf-8", newline="\n") as f:
        for r in clean:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return {"ok": True, "path": str(live), "written": len(clean),
            "rows": n_before + len(clean)}


def texts_append(story_dir, texts) -> dict:
    if not isinstance(texts, list) or not texts:
        return {"texts": 0}
    rows = []
    for t in texts:
        if not isinstance(t, dict):
            continue
        body = t.get("texts")
        if not isinstance(body, dict) or not body:
            continue
        rows.append({"seq": t.get("seq"), "cmd": t.get("cmd"),
                     "texts": {str(k): str(v) for k, v in body.items()}})
    if not rows:
        return {"texts": 0}
    d = trace_dir(story_dir)
    d.mkdir(parents=True, exist_ok=True)
    with (d / "trace-text.jsonl").open("a", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return {"texts": len(rows)}


def trace_read(story_dir, limit: int = 500) -> dict:
    d = trace_dir(story_dir)
    if not d.exists():
        return {"rows": [], "files": 0}
    files = sorted(d.glob("trace-*.jsonl")) + [d / "trace.jsonl"]
    files = [f for f in files if f.exists()]
    out = []
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return {"rows": out[-max(1, int(limit)):], "files": len(files),
            "total": len(out)}
