from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent
sys.path.insert(0, str(HERE))

import find_workspace

RUNNER_CONFIG = APP / "runner" / "config.toml"

LOST = "lost"
AT_RISK = "at_risk"
UNKNOWN = "unknown"
REBUILDABLE = "rebuildable"
NOTE = "note"

SEVERITY_ORDER = {LOST: 0, AT_RISK: 1, UNKNOWN: 2, REBUILDABLE: 3, NOTE: 4}

INTACT = "intact"
VERDICT_ORDER = {LOST: 0, UNKNOWN: 1, AT_RISK: 2, INTACT: 3}

PROSE_CHECKS = {"S1", "S2", "S3", "S4", "S5", "S7"}
PLOT_CHECKS = {"S1", "S6", "S8"}

MAX_WALK_FILES = 200_000

LOOSE_WITH_NO_PACK = 1
LOOSE_EVEN_IF_PACKED = 5000


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


def _norm(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "the file is not there"
    except OSError as e:
        return None, f"could not be opened ({e.strerror or e})"
    except ValueError as e:
        return None, f"is not valid JSON ({str(e)[:90]})"


def finding(fid: str, severity: str, what: str, where, fix=None,
            project: str = None, story: str = None, target=None, **detail) -> dict:
    if fix and target is None:
        target = where
    return {"id": fid, "severity": severity, "what": what,
            "where": str(where) if where is not None else None,
            "fix": fix, "target": (str(target) if fix else None),
            "project": project, "story": story,
            "detail": detail}



DEMO_TEXTS = frozenset()


def seed_chapters() -> list:
    def part(pid, ordinal, text):
        return {"id": pid, "ord": ordinal, "title": "", "summary": None,
                "plan": None,
                "versions": ([{"text": text}] if text else []),
                "current": (0 if text else None)}
    return []


LEGACY_OPS = frozenset({
    "sel", "set_field", "move_part", "move_beat", "add_part", "add_beat",
    "del", "clone", "pick", "pick_direction", "add_take", "accept_take",
    "del_take", "set_suggestions"})

INERT_OPS = frozenset({
    "sel2", "set_story", "set_style", "set_instructions", "set_ideas",
    "mark_add", "mark_edit", "mark_remove", "set_feedback_windows",
    "ledger_add", "ledger_edit", "ledger_remove", "ledger_cat_add",
    "ledger_cat_edit", "quote_spend", "quote_restore", "vote", "vote2",
    "set_ch_suggestions", "set_part_paragraphs", "beat_edit", "beat_add",
    "beat_remove", "beat_move"})


def _fnv1a(text: str) -> str:
    h = 0x811c9dc5
    for ch in text:
        cp = ord(ch)
        units = ((cp,) if cp < 0x10000 else
                 (0xD800 + ((cp - 0x10000) >> 10), 0xDC00 + ((cp - 0x10000) & 0x3FF)))
        for u in units:
            h = ((h ^ u) * 0x01000193) & 0xFFFFFFFF
    return f"{h:08x}"


def _para_key(text: str) -> str:
    return "p" + _fnv1a(re.sub(r"\s+", " ", str(text or "")).strip().lower())


def _paras(text: str) -> list:
    return [x for x in re.split(r"\n{2,}", text or "") if x.strip()]


def _part_text(part: dict) -> str:
    i = part.get("current")
    vs = part.get("versions") or []
    if i is None or not (0 <= i < len(vs)):
        return ""
    return vs[i].get("text") or ""


def _unique(candidate: str, taken) -> str:
    out, n = candidate, 2
    while taken(out):
        out = f"{candidate}~{n}"
        n += 1
    return out


def _next_ord(items: list) -> int:
    used = {x.get("ord") for x in (items or []) if isinstance(x.get("ord"), int)}
    n = 1
    while n in used:
        n += 1
    return n


def fold_prose(ops: list, ptr: int) -> dict:
    chapters = seed_chapters()
    unknown: dict = {}
    demo_loaded = False

    next_id = [0]

    def bump(value):
        m = re.fullmatch(r"n(\d+)", str(value or ""))
        if m:
            next_id[0] = max(next_id[0], int(m.group(1)))

    def nid():
        next_id[0] += 1
        return f"n{next_id[0]}"

    def find_ch(cid):
        return next((c for c in chapters if c.get("id") == cid), None)

    def find_part(cid, pid):
        c = find_ch(cid)
        return next((p for p in (c or {}).get("parts") or [] if p.get("id") == pid), None)

    def ch_taken(cid):
        return any(c.get("id") == cid for c in chapters)

    def part_taken(pid):
        return any(p.get("id") == pid for c in chapters for p in c.get("parts") or [])

    def push(part, text, **extra):
        part.setdefault("versions", []).append({"text": text, **extra})
        part["current"] = len(part["versions"]) - 1

    for op in (ops or [])[:max(0, int(ptr or 0))]:
        if not isinstance(op, dict):
            unknown["(not an object)"] = unknown.get("(not an object)", 0) + 1
            continue
        t = op.get("t")
        bump(op.get("id"))
        for x in (op.get("ids") or []):
            bump(x)

        if t in LEGACY_OPS or t in INERT_OPS:
            continue
        elif t == "reset":
            chapters = seed_chapters()
        elif t == "reset_empty":
            chapters = []
        elif t == "load_demo":
            chapters = seed_chapters()
            demo_loaded = True
        elif t == "add_chapter":
            cid = _unique(op.get("id") or nid(), ch_taken)
            chapters.append({"id": cid, "ord": _next_ord(chapters),
                             "title": "New chapter", "summary": "", "facets": {},
                             "plan": None, "plotdraft": None, "parts": []})
        elif t == "chapter_remove":
            chapters = [c for c in chapters if c.get("id") != op.get("ch")]
        elif t == "add_part2":
            c = find_ch(op.get("ch"))
            if c is not None:
                c.setdefault("parts", []).append(
                    {"id": _unique(op.get("id") or nid(), part_taken),
                     "ord": _next_ord(c.get("parts")), "title": "",
                     "summary": None, "plan": None, "versions": [], "current": None})
        elif t == "part_remove":
            c = find_ch(op.get("ch"))
            if c is not None:
                c["parts"] = [p for p in c.get("parts") or []
                              if p.get("id") != op.get("part")]
        elif t == "add_version":
            p = find_part(op.get("ch"), op.get("part"))
            if p is not None:
                push(p, op.get("text") or "")
        elif t == "set_current_version":
            p = find_part(op.get("ch"), op.get("part"))
            idx = op.get("idx")
            if p is not None and isinstance(idx, int) and 0 <= idx < len(p.get("versions") or []):
                p["current"] = idx
        elif t == "replace_para":
            p = find_part(op.get("ch"), op.get("part"))
            if p is not None and p.get("current") is not None:
                paras = _paras(_part_text(p))
                at = op.get("para")
                if isinstance(at, int) and 0 <= at < len(paras):
                    paras[at] = op.get("text") or ""
                    push(p, "\n\n".join(paras), replaced_para=at)
        elif t == "para_remove":
            p = find_part(op.get("ch"), op.get("part"))
            if p is not None and p.get("current") is not None:
                paras = _paras(_part_text(p))
                at = op.get("para")
                at = at if isinstance(at, int) else -1
                if op.get("key"):
                    hits = [i for i, x in enumerate(paras) if _para_key(x) == op["key"]]
                    if not (0 <= at < len(paras) and _para_key(paras[at]) == op["key"]):
                        at = hits[0] if len(hits) == 1 else -1
                if 0 <= at < len(paras):
                    paras.pop(at)
                    push(p, "\n\n".join(paras), removed_para=at)
        elif t == "set_ch":
            c = find_ch(op.get("ch"))
            if c is not None and op.get("field"):
                c[op["field"]] = op.get("v")
        elif t == "set_ch_facets":
            c = find_ch(op.get("ch"))
            if c is not None:
                c["facets"] = {**(c.get("facets") or {}), **(op.get("v") or {})}
        elif t in ("set_ch_plan", "set_ch_plan_stage"):
            c = find_ch(op.get("ch"))
            if c is not None:
                plan = dict(c.get("plan") or {})
                key = op.get("field") if t == "set_ch_plan" else op.get("stage")
                if key:
                    plan[key] = op.get("v") if t == "set_ch_plan" else (op.get("text") or "")
                c["plan"] = plan
        elif t == "set_ch_plotdraft":
            c = find_ch(op.get("ch"))
            if c is not None:
                c["plotdraft"] = op.get("text") or None
        elif t == "apply_suggestion":
            c = find_ch(op.get("ch"))
            if c is not None:
                if op.get("title"):
                    c["title"] = op["title"]
                if op.get("summary"):
                    c["summary"] = op["summary"]
                c["facets"] = {**(c.get("facets") or {}), **(op.get("facets") or {})}
        elif t == "plan_apply_blueprint":
            c = find_ch(op.get("ch"))
            if c is not None:
                for i, scene in enumerate(op.get("scenes") or []):
                    parts = c.setdefault("parts", [])
                    if i < len(parts):
                        p = parts[i]
                        if p.get("current") is not None:
                            continue
                    else:
                        ids = op.get("ids") or []
                        p = {"id": _unique(ids[i] if i < len(ids) else nid(), part_taken),
                             "ord": _next_ord(parts), "title": "", "summary": None,
                             "plan": None, "versions": [], "current": None}
                        parts.append(p)
                    p["plan"] = {"scene": scene.get("scene") or "",
                                 "beats": list(scene.get("beats") or [])}
                    if not str(p.get("title") or "").strip():
                        p["title"] = scene.get("scene") or ""
        elif t in ("set_part_plan", "set_part_plan_text"):
            p = find_part(op.get("ch"), op.get("part"))
            if p is not None:
                p["plan"] = (dict(op["plan"]) if t == "set_part_plan" and op.get("plan")
                             else ({"raw": op.get("text") or ""} if op.get("text") else None))
        elif t == "set_part_summary":
            p = find_part(op.get("ch"), op.get("part"))
            if p is not None:
                p["summary"] = op.get("s") or None
        elif t == "set_part_title":
            p = find_part(op.get("ch"), op.get("part"))
            if p is not None:
                p["title"] = op.get("v") or ""
        elif t == "set_part_notes":
            p = find_part(op.get("ch"), op.get("part"))
            if p is not None:
                p["notes"] = op.get("v") or ""
        else:
            unknown[str(t)] = unknown.get(str(t), 0) + 1

    for i, c in enumerate(chapters, start=1):
        if not isinstance(c.get("ord"), int):
            c["ord"] = i
        for j, p in enumerate(c.get("parts") or [], start=1):
            if not isinstance(p.get("ord"), int):
                p["ord"] = j
    return {"chapters": chapters, "demo_loaded": demo_loaded, "unknown_ops": unknown}


def prose_skeleton(chapters: list) -> list:
    return [{"id": c.get("id"), "ord": c.get("ord"),
             "parts": [{"id": p.get("id"), "ord": p.get("ord"),
                        "current": p.get("current"),
                        "versions": [_norm(v.get("text")) for v in p.get("versions") or []]}
                       for p in c.get("parts") or []]}
            for c in chapters or []]



def check_story(pid: str, sid: str, sdir: Path) -> dict:
    out = {"id": sid, "project": pid, "title": sid, "path": str(sdir),
           "prose": INTACT, "plot": INTACT, "chapters": 0, "parts": 0,
           "written": 0, "words": 0, "summaries": 0, "manuscript_files": 0,
           "findings": []}
    add = out["findings"].append

    meta, _ = _read_json(sdir / "story.json")
    if isinstance(meta, dict) and str(meta.get("title") or "").strip():
        out["title"] = meta["title"].strip()

    jpath, spath = sdir / "project.json", sdir / "state.json"
    ms = sdir / "manuscript"
    ms_files = sorted(ms.rglob("part-*.md")) if ms.is_dir() else []
    out["manuscript_files"] = len(ms_files)

    journal, jerr = _read_json(jpath)
    if jerr == "the file is not there" and not ms_files and not spath.exists():
        add(finding("S1", NOTE, "nothing has been written in this story yet.",
                    jpath, None, pid, sid))
        return out
    if jerr == "the file is not there":
        if ms_files:
            add(finding("S1", LOST,
                        "the op journal - this story's whole history, including "
                        "its plot - is missing. The manuscript files below are "
                        "all that is left, and nothing can undo or replay any of it.",
                        jpath, None, pid, sid, manuscript_files=len(ms_files)))
        else:
            add(finding("S1", NOTE,
                        "nothing has been written in this story yet.",
                        jpath, None, pid, sid))
        journal = None
    elif jerr:
        add(finding("S1", LOST,
                    f"the op journal {jerr}. Its history and its plot cannot be "
                    "read. Any prose that reached the manuscript files is still "
                    "on disk and is listed below.",
                    jpath, None, pid, sid, reason=jerr,
                    manuscript_files=len(ms_files)))
        journal = None
    elif not isinstance(journal, dict):
        add(finding("S1", LOST,
                    "the op journal is valid JSON but not a journal - it holds "
                    f"a {type(journal).__name__}, not the expected object.",
                    jpath, None, pid, sid))
        journal = None

    ops = (journal or {}).get("ops")
    ops = ops if isinstance(ops, list) else []
    ptr = (journal or {}).get("ptr")
    if journal is not None:
        if not isinstance(ptr, int) or not (0 <= ptr <= len(ops)):
            add(finding("S1", LOST,
                        f"the journal's pointer says {ptr!r} of {len(ops)} ops. "
                        "It names a place that does not exist, so there is no "
                        "telling which of these ops were undone.",
                        jpath, None, pid, sid, ptr=ptr, ops=len(ops)))
            ptr = None
    if not isinstance(ptr, int) or not (0 <= ptr <= len(ops)):
        ptr = None

    folded = fold_prose(ops, ptr) if (journal is not None and ptr is not None) else None
    chapters = (folded or {}).get("chapters") or []
    out["chapters"] = len(chapters)
    for c in chapters:
        for p in c.get("parts") or []:
            out["parts"] += 1
            if p.get("summary"):
                out["summaries"] += 1
            text = _norm(_part_text(p))
            if text:
                out["written"] += 1
                out["words"] += len(text.split())

    state, serr = _read_json(spath)
    live = ((state or {}).get("state") or {}) if isinstance(state, dict) else {}

    _s2(add, pid, sid, spath, state, serr, live, folded)
    _s3_s4_s5(add, pid, sid, sdir, ms, chapters, folded)
    _s6(add, pid, sid, spath, jpath, state, serr, journal, folded)
    _s7(add, pid, sid, sdir, ops, ptr, folded, ms_files)
    _s8(add, pid, sid, jpath, chapters, folded)

    out["prose"] = _verdict(out["findings"], PROSE_CHECKS)
    out["plot"] = _verdict(out["findings"], PLOT_CHECKS)
    return out


def _s2(add, pid, sid, spath, state, serr, live, folded):
    if serr or not isinstance(state, dict):
        return
    if folded is None:
        add(finding("S2", UNKNOWN,
                    "cannot tell whether the saved prose is in the journal: the "
                    "journal could not be read, so there is nothing to check it "
                    "against.", spath, None, pid, sid,
                    reason="the journal is unreadable (see S1)"))
        return
    unknown_ops = folded.get("unknown_ops") or {}
    known = {_norm(v.get("text"))
             for c in folded["chapters"] for p in c.get("parts") or []
             for v in p.get("versions") or []}
    known.discard("")
    orphans = []
    for c in live.get("chapters") or []:
        for p in c.get("parts") or []:
            i, vs = p.get("current"), p.get("versions") or []
            if not isinstance(i, int) or not (0 <= i < len(vs)):
                continue
            text = _norm((vs[i] or {}).get("text"))
            if not text or text in known or text in DEMO_TEXTS:
                continue
            orphans.append({"chapter": c.get("id"), "part": p.get("id"),
                            "words": len(text.split()), "chars": len(text)})
    if orphans and unknown_ops:
        names = ", ".join(sorted(unknown_ops))
        add(finding("S2", UNKNOWN,
                    f"{len(orphans)} part(s) hold prose this check could not "
                    f"find in the journal - but the journal also uses op(s) it "
                    f"cannot replay ({names}), so the prose may well be there. "
                    "Saying `lost` on a partial replay would be a guess.",
                    spath, None, pid, sid, parts=orphans,
                    unknown_ops=unknown_ops,
                    reason=f"unreplayable ops: {names}"))
        return
    if orphans:
        words = sum(o["words"] for o in orphans)
        add(finding("S2", LOST,
                    f"{len(orphans)} part(s) - about {words:,} words - hold "
                    "prose that is in the saved state and NOT in the op "
                    "journal. If state.json is ever rebuilt from the journal, "
                    "these words are what would not come back.",
                    spath, None, pid, sid, parts=orphans, words=words))


def _s3_s4_s5(add, pid, sid, sdir, ms, chapters, folded):
    if folded is None:
        add(finding("S3", UNKNOWN,
                    "cannot tell whether this story's manuscript files are all "
                    "there: the journal could not be read, so there is no list "
                    "of what should exist.", sdir, None, pid, sid,
                    reason="the journal is unreadable (see S1)"))
        return
    for c in chapters:
        for p in c.get("parts") or []:
            text = _norm(_part_text(p))
            if not text:
                continue
            cdir = ms / f"chapter-{int(c['ord']):02d}"
            stem = f"part-{int(p['ord']):02d}"
            versions = sorted(cdir.glob(stem + ".v*.md")) if cdir.is_dir() else []
            where = f"{c.get('title') or c.get('id')} - part {p['ord']}"
            if not versions:
                add(finding("S3", AT_RISK,
                            f"{where} has {len(text.split()):,} words in the "
                            "journal and no manuscript file. The words are "
                            "safe, but they are down to ONE copy.",
                            cdir / (stem + ".v1.md"), None, pid, sid,
                            chapter=c.get("id"), part=p.get("id"),
                            route="POST /loom/files/backfill",
                            words=len(text.split())))
                continue

            latest = max(versions, key=lambda f: _version_of(f))
            try:
                body = latest.read_text(encoding="utf-8")
            except OSError as e:
                add(finding("S4", UNKNOWN,
                            f"{where}: the manuscript file is there but could "
                            f"not be read ({e.strerror or e}).",
                            latest, None, pid, sid, reason=str(e)))
                continue

            side, side_err = _read_json(latest.with_suffix(".json"))
            if side_err == "the file is not there":
                add(finding("S4", AT_RISK,
                            f"{where}: the manuscript file has no sidecar, so "
                            "nothing records what wrote it or what its bytes "
                            "should be. A sidecar cannot be rebuilt - its "
                            "provenance is gone.",
                            latest.with_suffix(".json"), None, pid, sid))
            elif side_err or not isinstance(side, dict):
                add(finding("S4", AT_RISK,
                            f"{where}: the sidecar {side_err or 'is not an object'}, "
                            "so these bytes cannot be checked against anything.",
                            latest.with_suffix(".json"), None, pid, sid,
                            reason=side_err))
            elif not side.get("text_sha256"):
                add(finding("S4", AT_RISK,
                            f"{where}: the sidecar records no text_sha256 - it "
                            "was written before the hash existed - so these "
                            "bytes cannot be checked.",
                            latest.with_suffix(".json"), None, pid, sid))
            elif _sha(body) != side["text_sha256"]:
                add(finding("S4", AT_RISK,
                            f"{where}: the file's bytes do not match the hash "
                            "recorded beside them. Either it was edited by hand "
                            "outside the app, or it is damaged. Only you can "
                            "tell which - so nothing here will touch it.",
                            latest, None, pid, sid,
                            recorded=side["text_sha256"], actual=_sha(body)))

            file_text = _norm(body)
            if file_text != text:
                longer = ("the manuscript file" if len(file_text) > len(text)
                          else "the journal" if len(text) > len(file_text)
                          else "neither - they are the same length")
                add(finding("S5", AT_RISK,
                            f"{where}: the journal and the manuscript file hold "
                            f"DIFFERENT text for the current version. {longer.capitalize()} "
                            f"is longer ({len(text):,} characters in the journal, "
                            f"{len(file_text):,} on disk). Both are on disk; "
                            "which one is the book is yours to say.",
                            latest, None, pid, sid,
                            journal_chars=len(text), file_chars=len(file_text),
                            longer=longer, chapter=c.get("id"), part=p.get("id")))


def _version_of(path: Path) -> int:
    try:
        return int(path.name.split(".v")[-1].split(".")[0])
    except (ValueError, IndexError):
        return 0


def _s6(add, pid, sid, spath, jpath, state, serr, journal, folded):
    if journal is None:
        add(finding("S6", UNKNOWN,
                    "cannot tell whether the saved state matches the journal: "
                    "the journal could not be read.", spath, None, pid, sid,
                    reason="the journal is unreadable (see S1)"))
        return
    missing = (serr == "the file is not there")
    if missing:
        state, serr = {"state": None}, None
    elif serr or not isinstance(state, dict):
        add(finding("S6", REBUILDABLE,
                    f"the saved state {serr or 'is not an object'}. It is only a "
                    "cache of the journal, so it can be rebuilt from it.",
                    spath, "refold", pid, sid, target=spath.parent, reason=serr))
        return

    if folded is None:
        add(finding("S6", UNKNOWN,
                    "cannot compare the saved state with the journal: the "
                    "journal's pointer is out of range.", spath, None, pid, sid,
                    reason="the journal pointer is out of range (see S1)"))
        return

    jrev, srev = journal.get("rev"), state.get("rev")
    if not missing and jrev != srev:
        add(finding("S6", REBUILDABLE,
                    f"the saved state is at revision {srev!r} and the journal at "
                    f"{jrev!r}. They are written together, so one of the two "
                    "writes did not finish. The journal is the record; folding "
                    "it again makes the cache agree.",
                    spath, "refold", pid, sid, target=spath.parent,
                    state_rev=srev, journal_rev=jrev))
        return

    unknown_ops = folded.get("unknown_ops") or {}
    if unknown_ops:
        names = ", ".join(sorted(unknown_ops))
        add(finding("S6", UNKNOWN,
                    "cannot certify that the saved state is the fold of the "
                    f"journal: the journal uses op(s) this check cannot replay "
                    f"({names}). Everything else about this story was checked.",
                    spath, None, pid, sid, unknown_ops=unknown_ops,
                    reason=f"unreplayable ops: {names}"))
        return

    want = prose_skeleton(folded["chapters"])
    got = prose_skeleton(((state.get("state") or {}).get("chapters")) or [])
    if want != got:
        add(finding("S6", REBUILDABLE,
                    ("the saved state is missing or empty while the journal "
                     "holds a book. Folding it again rewrites the cache and "
                     "loses nothing."
                     if not got else
                     "the saved state does not match what the journal folds to - "
                     "its chapters, parts or version texts differ. The journal is "
                     "the record; folding it again rewrites the cache from it."),
                    spath, "refold", pid, sid, target=spath.parent,
                    journal_chapters=len(want), state_chapters=len(got),
                    compared="chapter and part ids, ordinals, every version's "
                             "text, and which version is current",
                    not_compared="marks, ledgers, quotes, feedback, styles and "
                                 "instructions - this module does not replay "
                                 "those ops, so it does not judge them"))


def _s7(add, pid, sid, sdir, ops, ptr, folded, ms_files):
    if folded is None:
        return
    journal_real = [o for o in (ops or [])[:ptr or 0]
                    if isinstance(o, dict) and o.get("t") == "add_version"
                    and _norm(o.get("text")) and _norm(o.get("text")) not in DEMO_TEXTS]
    if not ms_files and not journal_real:
        return

    seeded = [{"chapter": c.get("id"), "part": p.get("id")}
              for c in folded["chapters"] for p in c.get("parts") or []
              if _norm(_part_text(p)) in DEMO_TEXTS]
    if not (folded.get("demo_loaded") or seeded):
        return

    how = ("a `load_demo` op" if folded.get("demo_loaded")
           else "the app's built-in seed")
    add(finding("S7", AT_RISK,
                f"this story is showing the shipped demo ({how}), and it is not "
                f"the demo: {len(journal_real)} generated part(s) in the journal "
                f"and {len(ms_files)} manuscript file(s) belong to it. Your words are "
                "still on disk in both places - the story simply no longer opens "
                "on them. Nothing here will put them back for you.",
                sdir, None, pid, sid,
                demo_parts=seeded, load_demo=bool(folded.get("demo_loaded")),
                journal_parts=len(journal_real), manuscript_files=len(ms_files),
                snapshot=str(sdir / "manuscript")))


def _s8(add, pid, sid, jpath, chapters, folded):
    if folded is None:
        add(finding("S8", UNKNOWN,
                    "cannot tell which chapters have a plot: the journal could "
                    "not be read.", jpath, None, pid, sid,
                    reason="the journal is unreadable (see S1)"))
        return
    bare = []
    for c in chapters:
        if not (c.get("parts") or []):
            continue
        plan = c.get("plan") or {}
        if (str(c.get("summary") or "").strip()
                or str(c.get("plotdraft") or "").strip()
                or any(str(v or "").strip() for v in plan.values())):
            continue
        bare.append({"chapter": c.get("id"), "ord": c.get("ord"),
                     "title": c.get("title") or "", "parts": len(c.get("parts") or [])})
    if bare:
        add(finding("S8", NOTE,
                    f"{len(bare)} chapter(s) with parts in them have no plot - "
                    "no summary, no plan and no plot draft. The prose is "
                    "unaffected; this is only the chapter's own record of what "
                    "it was for.",
                    jpath, None, pid, sid, chapters=bare))


def _verdict(findings: list, ids: set) -> str:
    worst = INTACT
    for f in findings:
        if f["id"] not in ids or f["severity"] not in (LOST, AT_RISK, UNKNOWN):
            continue
        if VERDICT_ORDER[f["severity"]] < VERDICT_ORDER[worst]:
            worst = f["severity"]
    return worst



def check_project(pid: str, pdir: Path) -> dict:
    out = {"id": pid, "title": pid, "path": str(pdir), "story_count": 0,
           "words": 0, "findings": [], "stories": []}
    add = out["findings"].append

    meta, merr = _read_json(pdir / "project.json")

    if merr == "the file is not there":
        add(finding("P1", NOTE,
                    "this project has no project.json. Its title and notes fall "
                    "back to the folder name; nothing else depends on it.",
                    pdir / "project.json", None, pid))
    elif merr or not isinstance(meta, dict):
        add(finding("P1", LOST,
                    f"the project record {merr or 'is not an object'}. Its title "
                    "and notes are gone. The stories inside it are separate "
                    "files and are checked on their own.",
                    pdir / "project.json", None, pid, reason=merr))
    elif str(meta.get("title") or "").strip():
        out["title"] = meta["title"].strip()

    sdir = pdir / "stories"
    story_dirs = sorted([d for d in sdir.iterdir() if d.is_dir()]) if sdir.is_dir() else []

    if _is_empty_tree(pdir):
        add(finding("P2", REBUILDABLE,
                    "this project folder is completely empty - no stories, no "
                    "record, no files at all. It is what a stale browser tab "
                    "leaves behind when it polls a project that was never made. "
                    "Forgetting it deletes an empty folder and nothing else.",
                    pdir, "forget_empty", pid))

    for d in story_dirs:
        if not (d / "story.json").exists():
            has_work = (d / "project.json").exists() or (d / "manuscript").is_dir()
            add(finding("P3", AT_RISK,
                        "this story folder has no story.json, so the app does "
                        "not know its title or its settings - it is an orphan "
                        "in the tree. " + ("Its journal and manuscript are "
                                           "checked below."
                                           if has_work else
                                           "It holds no journal and no "
                                           "manuscript either."),
                        d / "story.json", None, pid, d.name, has_work=has_work))
        out["stories"].append(check_story(pid, d.name, d))

    out["story_count"] = len(out["stories"])
    out["words"] = sum(s["words"] for s in out["stories"])
    return out


def _is_empty_tree(d: Path) -> bool:
    try:
        for _, _, files in os.walk(d):
            if files:
                return False
    except OSError:
        return False
    return True


def _p4(projects: list, loom: Path) -> list:
    by_title: dict = {}
    for p in projects:
        for s in p["stories"]:
            by_title.setdefault(str(s["title"]).strip().lower(), []).append(
                {"project": p["id"], "story": s["id"], "title": s["title"],
                 "path": s["path"], "words": s["words"]})
    out = []
    for title, rows in sorted(by_title.items()):
        if len({r["project"] for r in rows}) < 2:
            continue
        out.append(finding(
            "P4", NOTE,
            f"{len(rows)} stories in {len({r['project'] for r in rows})} different "
            f"projects are all called \"{rows[0]['title']}\". Nothing is wrong - "
            "but they are different books, and the name will not tell them apart.",
            loom / "projects", None, None, None, stories=rows))
    return out



def _w1(root: Path, loom: Path) -> list:
    out = []
    try:
        near = find_workspace.find(root.parent) if root.parent != root else {"status": "here"}
    except OSError as e:
        near = {"status": "unknown", "why": str(e), "others": [], "path": ""}

    others = [o for o in (near.get("others") or [])
              if str(Path(o["path"])).lower() != str(root).lower()]
    out.append(finding(
        "W1", NOTE,
        f"this reading is of {root}" +
        (f"; {len(others)} other folder(s) beside this one also look like "
         "workspaces." if others else "."),
        root, None,
        reading=str(root), loom=str(loom),
        neighbours=others, scan_status=near.get("status")))
    return out


def _w2(root: Path) -> list:
    git = root / ".git"
    if not git.exists():
        return [finding("W2", NOTE,
                        "this workspace is not under git, so there are no "
                        "launch snapshots to fall back on.", git, None)]

    objects = git / "objects"
    loose, packs = 0, 0
    try:
        for d in objects.iterdir() if objects.is_dir() else []:
            if d.is_dir() and len(d.name) == 2 and re.fullmatch(r"[0-9a-f]{2}", d.name):
                loose += sum(1 for f in d.iterdir() if f.is_file())
        packs = len(list((objects / "pack").glob("*.pack"))) if (objects / "pack").is_dir() else 0
    except OSError as e:
        return [finding("W2", UNKNOWN,
                        f"could not count the snapshot repo's objects ({e.strerror or e}).",
                        objects, None, reason=str(e))]

    commits, commits_reason = _git_commits(root)
    where = f"{loose:,} loose object(s), {packs} pack(s)"
    head = (f"{commits:,} snapshot(s)" if commits is not None
            else f"an unknown number of snapshots ({commits_reason})")

    if packs == 0 and loose >= LOOSE_WITH_NO_PACK:
        return [finding("W2", REBUILDABLE,
                        f"the snapshot repo holds {head} as {where} - it has "
                        "never been packed. Packing it is `git gc`: it changes "
                        "no snapshot and usually collapses thousands of files "
                        "into one.",
                        git, "pack", target=root, loose=loose, packs=packs,
                        commits=commits, commits_reason=commits_reason)]
    if loose >= LOOSE_EVEN_IF_PACKED:
        return [finding("W2", REBUILDABLE,
                        f"the snapshot repo holds {head} as {where}. It has been "
                        f"packed before, but {loose:,} loose objects have piled "
                        f"up since (the threshold here is {LOOSE_EVEN_IF_PACKED:,}). "
                        "Packing changes no snapshot.",
                        git, "pack", target=root, loose=loose, packs=packs,
                        commits=commits, commits_reason=commits_reason)]
    return [finding("W2", NOTE,
                    f"the snapshot repo holds {head} as {where}. Nothing to do.",
                    git, None, loose=loose, packs=packs, commits=commits,
                    commits_reason=commits_reason)]


def _git_commits(root: Path):
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-list", "--count", "--all"],
                           capture_output=True, text=True, timeout=20)
    except FileNotFoundError:
        return None, "git is not on this machine's PATH"
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"git could not be run ({e})"
    if r.returncode != 0:
        return None, "git could not read it: " + ((r.stderr or "").strip().splitlines() or [""])[0][:90]
    try:
        return int((r.stdout or "").strip()), None
    except ValueError:
        return None, "git returned something that is not a number"


def _runs_dir_from_config():
    cfg = RUNNER_CONFIG
    try:
        body = cfg.read_text(encoding="utf-8")
    except OSError as e:
        return None, f"the runner's config could not be read ({e.strerror or e})"
    m = re.search(r'^\s*data_dir\s*=\s*"([^"]+)"', body, re.M)
    if not m:
        return None, None
    p = Path(m.group(1).replace("\\\\", "\\")).expanduser()
    if not p.is_absolute():
        p = (cfg.parent / p)
    return p / "runs", None


def _walk(d: Path):
    n, size, lo, hi, truncated = 0, 0, None, None, False
    for base, _, names in os.walk(d):
        for name in names:
            if n >= MAX_WALK_FILES:
                return n, size, lo, hi, True
            try:
                st = os.stat(os.path.join(base, name))
            except OSError:
                continue
            n += 1
            size += st.st_size
            lo = st.st_mtime if lo is None or st.st_mtime < lo else lo
            hi = st.st_mtime if hi is None or st.st_mtime > hi else hi
    return n, size, lo, hi, truncated


def _w3(root: Path) -> list:
    runs, reason = _runs_dir_from_config()
    if runs is None and reason is None:
        return [finding("W3", NOTE,
                        "no run history can be living outside this workspace: "
                        "the runner's config names no data root, so it writes "
                        "wherever --data bound it or refuses to start.",
                        RUNNER_CONFIG, None, runs_dir=None, inside=True)]
    if runs is None:
        return [finding("W3", UNKNOWN,
                        f"cannot tell whether any prose is living outside this "
                        f"workspace: {reason}.", RUNNER_CONFIG,
                        None, reason=reason)]
    try:
        inside = str(runs.resolve()).lower().startswith(str(root.resolve()).lower())
    except OSError:
        inside = False
    if inside or not runs.is_dir():
        return [finding("W3", NOTE,
                        "no run history is living outside this workspace." if not runs.is_dir()
                        else "the run history is inside this workspace, where it belongs.",
                        runs, None, runs_dir=str(runs), inside=inside)]

    n, size, lo, hi, truncated = _walk(runs)
    if truncated:
        return [finding("W3", UNKNOWN,
                        f"there is a run history outside this workspace at {runs}, "
                        f"and it holds more than {MAX_WALK_FILES:,} files - too many "
                        "to count without making you wait. Its size is unreported "
                        "rather than guessed.",
                        runs, "adopt_runs", workspace=str(root),
                        runs_dir=str(runs),
                        reason=f"more than {MAX_WALK_FILES:,} files")]
    if n == 0:
        return [finding("W3", NOTE, f"the run folder at {runs} is empty.",
                        runs, None, runs_dir=str(runs), files=0)]
    span = (f"{time.strftime('%Y-%m-%d', time.localtime(lo))} to "
            f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(hi))}")
    return [finding("W3", NOTE,
                    f"{n:,} files ({size / 1e6:,.0f} MB, {span}) of run history "
                    f"sit OUTSIDE this workspace, at {runs}. These are a second "
                    "copy of the prompts and the prose that were generated - a "
                    "record, not a duplicate to throw away. Adopting it MOVES it "
                    "in; nothing is deleted.",
                    runs, "adopt_runs",
                    workspace=str(root), runs_dir=str(runs), files=n, bytes=size,
                    oldest=time.strftime("%Y-%m-%d %H:%M", time.localtime(lo)),
                    newest=time.strftime("%Y-%m-%d %H:%M", time.localtime(hi)))]


def _w4(loom: Path, projects: list) -> list:
    inv = {"summaries": 0, "texts": 0, "text_indexes": 0, "texts_missing_index": 0,
           "studies": 0, "artifacts": 0, "requests": 0, "manuscript_files": 0}
    pdir = loom / "projects"
    no_index = []
    for p in projects:
        root = Path(p["path"])
        texts = root / "texts"
        if texts.is_dir():
            inv["texts"] += sum(1 for f in texts.glob("*.md"))
            if (texts / "index.json").is_file():
                inv["text_indexes"] += 1
            elif any(texts.glob("*.md")):
                inv["texts_missing_index"] += 1
                no_index.append(p)
        studies = root / "studies"
        if studies.is_dir():
            inv["studies"] += sum(1 for f in studies.iterdir())
        artifacts = root / "artifacts"
        if artifacts.is_dir():
            inv["artifacts"] += sum(1 for f in artifacts.rglob("*") if f.is_file())
        requests = root / "requests"
        if requests.is_dir():
            inv["requests"] += sum(1 for month in requests.iterdir() if month.is_dir()
                                   for corr in month.iterdir() if corr.is_dir())
        for s in p["stories"]:
            inv["manuscript_files"] += s["manuscript_files"]
            inv["summaries"] += s["summaries"]

    out = [finding("W4", NOTE,
                   f"rebuildable things on disk: {inv['summaries']:,} part "
                   f"summar{'y' if inv['summaries'] == 1 else 'ies'}, "
                   f"{inv['texts']:,} frozen text "
                   f"versions in {inv['text_indexes']} store(s), "
                   f"{inv['requests']:,} request record(s), {inv['studies']:,} "
                   f"study file(s), {inv['artifacts']:,} artifact file(s), "
                   f"{inv['manuscript_files']:,} manuscript file(s). The "
                   "manuscript files are prose and are checked per story; the "
                   "rest is provenance, which is the feature, not a leak.",
                   pdir, None, **inv)]
    for p in no_index:
        out.append(finding("W4", REBUILDABLE,
                           f"\"{p['title']}\" has frozen text versions on disk "
                           "and no index.json beside them. The index is a "
                           "convenience and the files are the record, so it "
                           "rebuilds from them.",
                           Path(p["path"]) / "texts" / "index.json", "reindex",
                           p["id"], None, target=p["path"]))
    return out



def report(root) -> dict:
    given = Path(str(root).replace("\\", "/")).expanduser()
    out = {"root": str(given), "workspace": None, "loom": None,
           "generated": _now(), "ok": False, "headline": "",
           "prose": UNKNOWN, "plot": UNKNOWN,
           "totals": {"projects": 0, "stories": 0, "words": 0, "parts": 0,
                      "written": 0, "manuscript_files": 0},
           "counts": {LOST: 0, AT_RISK: 0, UNKNOWN: 0, REBUILDABLE: 0, NOTE: 0,
                      "findings": 0},
           "projects": [], "workspace_findings": [], "findings": [],
           "error": None}

    if not given.is_dir():
        out["error"] = f"there is no folder at {given}"
        out["headline"] = out["error"]
        return out

    if (given / "loom" / "projects").is_dir() or (given / "loom").is_dir():
        ws, loom = given, given / "loom"
    elif (given / "projects").is_dir():
        ws, loom = given.parent, given
    else:
        out["error"] = (f"{given} does not hold a workspace - there is no "
                        "loom/projects in it")
        out["headline"] = out["error"]
        return out
    out["workspace"], out["loom"] = str(ws), str(loom)

    pdir = loom / "projects"
    if pdir.is_dir():
        for d in sorted(x for x in pdir.iterdir() if x.is_dir()):
            out["projects"].append(check_project(d.name, d))

    out["workspace_findings"] = (_w1(ws, loom) + _w2(ws) + _w3(ws)
                                 + _w4(loom, out["projects"])
                                 + _p4(out["projects"], loom))

    flat = list(out["workspace_findings"])
    for p in out["projects"]:
        flat.extend(p["findings"])
        for s in p["stories"]:
            flat.extend(s["findings"])
    flat.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), f["id"],
                             f.get("project") or "", f.get("story") or ""))
    out["findings"] = flat

    for f in flat:
        out["counts"][f["severity"]] = out["counts"].get(f["severity"], 0) + 1
    out["counts"]["findings"] = len(flat)

    stories = [s for p in out["projects"] for s in p["stories"]]
    out["totals"] = {
        "projects": len(out["projects"]), "stories": len(stories),
        "words": sum(s["words"] for s in stories),
        "parts": sum(s["parts"] for s in stories),
        "written": sum(s["written"] for s in stories),
        "manuscript_files": sum(s["manuscript_files"] for s in stories)}

    out["prose"] = _worst([s["prose"] for s in stories])
    out["plot"] = _worst([s["plot"] for s in stories])
    out["ok"] = not (out["counts"][LOST] or out["counts"][AT_RISK]
                     or out["counts"][UNKNOWN])
    out["headline"] = _headline(out)
    return out


def _worst(verdicts: list) -> str:
    if not verdicts:
        return INTACT
    return min(verdicts, key=lambda v: VERDICT_ORDER.get(v, 9))


def _headline(r: dict) -> str:
    t = r["totals"]
    base = (f"{t['stories']} stor{'y' if t['stories'] == 1 else 'ies'} in "
            f"{t['projects']} project{'' if t['projects'] == 1 else 's'} · "
            f"{t['words']:,} words of prose")
    bad = [s for p in r["projects"] for s in p["stories"]
           if s["prose"] != INTACT or s["plot"] != INTACT]
    if not bad and not r["counts"][LOST] and not r["counts"][AT_RISK] and not r["counts"][UNKNOWN]:
        return base + " · all present"
    if not bad:
        return base + " · every story is whole; the workspace itself has notes below"
    if len(bad) == 1:
        s = bad[0]
        return base + f" · \"{s['title']}\" needs attention"
    return base + f" · {len(bad)} stories need attention"



_BADGE = {INTACT: "OK     ", AT_RISK: "AT RISK", LOST: "LOST   ",
          UNKNOWN: "UNKNOWN"}


def _print(r: dict) -> None:
    print()
    print(f"  {r['headline']}")
    print(f"  {r['root']}   (read {r['generated']})")
    if r["error"]:
        return
    print(f"  PROSE {_BADGE.get(r['prose'], r['prose'])}      "
          f"PLOT {_BADGE.get(r['plot'], r['plot'])}")
    c = r["counts"]
    print(f"  {c['findings']} finding(s): {c[LOST]} lost, {c[AT_RISK]} at risk, "
          f"{c[UNKNOWN]} unknown, {c[REBUILDABLE]} rebuildable, {c[NOTE]} note")

    for p in r["projects"]:
        print()
        print(f"  {p['title']}   ({p['story_count']} stor"
              f"{'y' if p['story_count'] == 1 else 'ies'}, {p['words']:,} words)")
        for f in p["findings"]:
            _print_finding(f, "    ")
        for s in p["stories"]:
            print(f"    PROSE {_BADGE.get(s['prose'])}  PLOT {_BADGE.get(s['plot'])}  "
                  f"{s['title']}  -  {s['written']}/{s['parts']} parts, "
                  f"{s['words']:,} words, {s['manuscript_files']} file(s)")
            for f in s["findings"]:
                _print_finding(f, "        ")

    print()
    print("  THE WORKSPACE ITSELF")
    for f in r["workspace_findings"]:
        _print_finding(f, "    ")
    print()


def _print_finding(f: dict, pad: str) -> None:
    tail = f"   [{f['fix']}]" if f["fix"] else ""
    print(f"{pad}{f['severity'].upper():<12}{f['id']}  {f['what']}{tail}")
    if f["where"]:
        print(f"{pad}            {f['where']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Read a Story Loom workspace and say whether the prose "
                    "survived. Writes nothing.")
    ap.add_argument("--root", required=True,
                    help="the workspace folder (the one holding loom/). "
                         "REQUIRED: there is no default and nothing is read "
                         "from disk to guess one.")
    ap.add_argument("--json", action="store_true", help="print the report dict")
    a = ap.parse_args(argv)

    if not str(a.root).strip():
        print("--root was empty. Name the workspace folder: an empty path is "
              "the current directory, not a workspace.", file=sys.stderr)
        return 2

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    r = report(a.root)
    if a.json:
        print(json.dumps(r, indent=1, ensure_ascii=False))
    else:
        _print(r)
    if r["error"]:
        return 2
    return 1 if r["counts"][LOST] else 0


if __name__ == "__main__":
    sys.exit(main())
