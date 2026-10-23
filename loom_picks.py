from __future__ import annotations

import hashlib
import json
from pathlib import Path

VERIFIED = "verified"
MISMATCH = "mismatch"
MISSING = "missing"
UNVERIFIABLE = "unverifiable"
NEVER_PICKED = "never_picked"


def sample_key(sample) -> str:
    try:
        n = int(sample)
    except (TypeError, ValueError):
        return None
    return f"{n:02d}" if n > 0 else None


def _answer(status: str, **kv) -> dict:
    out = {
        "status": status,
        "verified": status == VERIFIED,
        "sample": None,
        "sha256": None,
        "sha256_now": None,
        "output": None,
        "backfilled": False,
        "recorded_at": None,
        "job": None,
        "version": None,
        "picks": 0,
        "detail": "",
    }
    out.update(kv)
    return out


def _say(*parts) -> str:
    return ", ".join(p for p in parts if p)


def _events(f: Path):
    rows, bad = [], 0
    try:
        text = f.read_text(encoding="utf-8")
    except OSError:
        return rows, bad
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(obj, dict):
            rows.append(obj)
        else:
            bad += 1
    return rows, bad


def _sha_of(p: Path):
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def read_pick(request_dir) -> dict:
    d = Path(request_dir)
    f = d / "events.jsonl"
    rows, bad = _events(f)
    if not f.exists():
        return _answer(NEVER_PICKED, detail=(
            f"{d.name}: no events.jsonl" if d.is_dir()
            else f"{d.name}: no such request folder"))

    picks = [r for r in rows if r.get("event") == "picked"]
    torn = f"{bad} unparsable line(s)" if bad else ""
    if not picks:
        return _answer(NEVER_PICKED, detail=_say(
            f"{len(rows)} event(s), none of them a pick", torn))

    row = picks[-1]
    key = sample_key(row.get("sample"))
    if key is None:
        return _answer(NEVER_PICKED, picks=len(picks), detail=_say(
            f"the last of {len(picks)} picked row(s) names no sample "
            f"({row.get('sample')!r})", torn))

    out = d / "samples" / key / "output.md"
    recorded, now = row.get("sha256"), _sha_of(out)
    if recorded is None:
        status, detail = UNVERIFIABLE, (
            f"sample {key} was adopted and the row recorded no sha256 - its "
            f"output.md was already missing when the pick was written")
    elif now is None:
        status, detail = MISSING, (
            f"samples/{key}/output.md is not readable"
            if out.exists() else f"samples/{key}/output.md is gone")
    elif now == recorded:
        status, detail = VERIFIED, ""
    else:
        status, detail = MISMATCH, (
            f"samples/{key}/output.md has changed since he adopted it")

    return _answer(status, sample=key, sha256=recorded, sha256_now=now,
                   output=out, backfilled=bool(row.get("backfilled")),
                   recorded_at=row.get("t"), job=row.get("job"),
                   version=row.get("version"), picks=len(picks),
                   detail=_say(detail, torn))
