from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import threading
import time
import re
import random
import hashlib
import urllib.error
import urllib.parse
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    import loom_engine as E
except Exception as _e:
    E = None
    print(f"  WARNING: loom_engine not importable ({_e}); structured jobs will fail")

from runner import local as _local_runner
from runner.local import HTTPish as LocalHTTPish

import loom_trace

import loom_routing

import loom_picks

try:
    import loom_playbooks
except Exception as _e:
    loom_playbooks = None
    print(f"  WARNING: loom_playbooks not importable ({_e}); playbook-sealed runs will be refused")

try:
    import loom_axes as AX
except Exception as _e:
    AX = None
    print(f"  WARNING: loom_axes not importable ({_e}); the pre-send review "
          f"cannot check for contradictory steering")

sys.path.append(str(HERE / "tools"))
try:
    import loom_doctor
except Exception as _e:
    loom_doctor = None
    print(f"  WARNING: loom_doctor not importable ({_e}); the Loading tab "
          f"cannot read the workspace")
try:
    import loom_repair
except Exception as _e:
    loom_repair = None
    print(f"  WARNING: loom_repair not importable ({_e}); the Loading tab's "
          f"rebuild buttons will be refused")

_SELF = sys.modules.get(__name__)
if loom_repair is not None and _SELF is not None \
        and getattr(loom_repair, "_SRV", None) is None:
    loom_repair._SRV = _SELF

PROJECTS = None
TRACE = os.environ.get("LOOM_TRACE") == "1"
TRACE_DIR = None


def _trace(tag: str, **kv) -> None:
    if not TRACE:
        return
    bits = " ".join(f"{k}={v}" for k, v in kv.items() if v is not None)
    print(f"  TRACE {time.strftime('%H:%M:%S')} {tag:<18} {bits}", flush=True)


WARN_EVERY_S = 30.0
_WARN_LAST: dict = {}
_WARN_LOCK = threading.Lock()


def _warn_reset() -> None:
    with _WARN_LOCK:
        _WARN_LAST.clear()


def _warn_ok(key: str) -> tuple:
    now = time.time()
    with _WARN_LOCK:
        row = _WARN_LAST.get(key)
        if row is not None and now - row[0] < WARN_EVERY_S:
            row[1] += 1
            return False, 0
        folded = row[1] if row else 0
        _WARN_LAST[key] = [now, 0]
        return True, folded


def _warn_more(folded: int) -> str:
    return f" [+{folded} more since the last line]" if folded else ""


def _warn(tag: str, **kv) -> bool:
    speak, folded = _warn_ok(f"{tag}|{kv.get('kind', '')}")
    if not speak:
        return False
    bits = " ".join(f"{k}={v}" for k, v in kv.items() if v is not None)
    print(f"[loom] WARN {time.strftime('%H:%M:%S')} {tag:<18} {bits}"
          f"{_warn_more(folded)}", flush=True)
    return True


def _trace_prompt(job: dict, prompts: list, fit: dict, used: list) -> None:
    if not TRACE:
        return
    try:
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        jid = job.get("id") or "preview"
        p = TRACE_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{jid}-{job.get('kind')}.txt"
        head = [
            f"job       {jid}  kind={job.get('kind')}",
            f"project   {job.get('project')}",
            f"story     {job.get('story')}",
            f"model     {(job.get('gen') or {}).get('model')}",
            f"max_tok   {(job.get('gen') or {}).get('max_tokens')}",
            f"pools     {', '.join(used) if used else '(none)'}",
            f"fit       ~{fit.get('approx_tokens')} tok of {fit.get('ctx')} "
            f"(headroom {fit.get('headroom')}) fits={fit.get('fits')}",
        ]
        if fit.get("warning"):
            head.append(f"warning   {fit['warning']}")
        body = []
        for i, pr in enumerate(prompts):
            body.append(f"\n{'=' * 70}\nPROMPT {i + 1} of {len(prompts)}  "
                        f"({len(pr):,} chars)\n{'=' * 70}\n{pr}")
        p.write_text("\n".join(head) + "\n" + "".join(body),
                     encoding="utf-8", newline="\n")
        _trace("prompt.saved", file=p.name, chars=sum(len(x) for x in prompts))
    except OSError as e:
        _trace("prompt.save_failed", err=str(e))


DEFAULT_STORY = "Untitled Story"
DEFAULT_PROJECT = "Untitled Project"
JOBS = None


class NoProjectLoaded(RuntimeError):
    pass


def projects_root() -> Path:
    if PROJECTS is None:
        raise NoProjectLoaded(
            "no project is loaded: this server has no workspace bound, so "
            "there is no projects folder to read or write. Choose a folder "
            "to work in first.")
    return PROJECTS


def bound_workspace():
    try:
        return str(projects_root().parent)
    except NoProjectLoaded:
        return None


def workspace_binding() -> dict:
    ex = _ex_root()
    extract = {"loaded": ex is not None, "root": str(ex) if ex else None}
    was = bound_workspace()
    if was is None:
        return {"loaded": False, "workspace": None, "projects": None,
                "extract": extract}
    return {"loaded": True, "workspace": was, "projects": str(projects_root()),
            "extract": extract}


def _story_id(raw) -> str:
    name = str(raw or "").strip() or DEFAULT_STORY
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if name.upper() in {"CON", "PRN", "AUX", "NUL"} or not name:
        name = DEFAULT_STORY
    return name[:64]


def _project_id(raw) -> str:
    return _story_id(str(raw or "").strip() or DEFAULT_PROJECT)




def _project_dir(pid: str, create: bool = False) -> Path:
    d = projects_root() / _project_id(pid)
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def _project_paths(pid: str, create: bool = False) -> dict:
    d = _project_dir(pid, create)
    return {"dir": d, "meta": d / "project.json",
            "pools": d / "pools", "stories": d / "stories"}


def _project_meta(pid: str) -> dict:
    p = _project_paths(pid)["meta"]
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    meta.setdefault("title", pid)
    meta.setdefault("created", None)
    meta.setdefault("notes", "")
    meta["id"] = pid
    return meta


def _write_project_meta(pid: str, **fields) -> dict:
    meta = _project_meta(pid)
    meta.update({k: v for k, v in fields.items() if v is not None})
    if not meta.get("created"):
        meta["created"] = time.strftime("%Y-%m-%d %H:%M:%S")
    meta["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    meta["id"] = pid
    _atomic_write(_project_paths(pid, True)["meta"], meta)
    return meta


def projects_list() -> list:
    projects = projects_root()
    projects.mkdir(parents=True, exist_ok=True)
    out = []
    for d in sorted(projects.iterdir()):
        if not d.is_dir():
            continue
        pid = d.name
        stories = stories_list(pid)
        pools = pools_list(pid)
        out.append({**_project_meta(pid),
                    "stories": stories,
                    "story_count": len(stories),
                    "pool_count": len(pools),
                    "always_pools": [p["id"] for p in pools
                                     if p.get("include") == "always"
                                     and not p.get("archived")],
                    "words": sum(x.get("words", 0) for x in stories)})
    return out


POOL_KINDS = [
    "plot", "events", "adjectives_subjects", "matches_against_toplist",
    "story_context", "writingstyle", "topics", "person_descriptions",
    "person_behaviour", "interactions", "custom_input",
    "characters", "context", "style", "examples", "toplist-import", "free",
]
POOL_INCLUDE = ("always", "optional")
POOL_ROLES = ("context", "under", "secret")
POOL_SCOPES = ("plot", "prose", "both")
POOL_META_KEYS = ("title", "kind", "include", "role", "scope")
_POOL_FILE = re.compile(r"(.+)\.v(\d+)\.md$")


def _pool_id(raw) -> str:
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", str(raw or "").strip()).strip("-_")
    return (name or "pool")[:48]


def _pools_dir(pid: str, create: bool = False) -> Path:
    d = _project_paths(pid, create)["pools"]
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def _pool_versions(pid: str, poolid: str) -> list:
    out = []
    for f in _pools_dir(pid).glob(f"{poolid}.v*.md"):
        m = _POOL_FILE.match(f.name)
        if m and m.group(1) == poolid:
            out.append(int(m.group(2)))
    return sorted(out)


def _pool_text(pid: str, poolid: str, v) -> str:
    try:
        return (_pools_dir(pid) / f"{poolid}.v{int(v)}.md").read_text(encoding="utf-8")
    except (OSError, TypeError, ValueError):
        return ""


def _pool_index(pid: str) -> dict:
    try:
        doc = json.loads((_pools_dir(pid) / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    if not isinstance(doc, dict) or not isinstance(doc.get("pools"), list):
        doc = {"pools": []}
    return doc


def _pool_fields(fields: dict) -> dict:
    out = {}
    if fields.get("title"):
        out["title"] = str(fields["title"])[:120]
    if fields.get("kind"):
        k = str(fields["kind"])
        out["kind"] = k if k in POOL_KINDS else "free"
    if fields.get("include"):
        inc = str(fields["include"])
        out["include"] = inc if inc in POOL_INCLUDE else "optional"
    if fields.get("role"):
        role = str(fields["role"])
        out["role"] = role if role in POOL_ROLES else "context"
    if fields.get("scope"):
        sc = str(fields["scope"])
        out["scope"] = sc if sc in POOL_SCOPES else "both"
    if isinstance(fields.get("tags"), list):
        out["tags"] = [str(t)[:40] for t in fields["tags"]][:12]
    if isinstance(fields.get("source"), dict):
        out["source"] = fields["source"]
    if "archived" in fields:
        out["archived"] = bool(fields["archived"])
    if fields.get("notes") is not None:
        out["notes"] = str(fields["notes"])[:2000]
    return out


POOL_DEFAULTS = {"kind": "free", "include": "optional", "role": "context",
                 "tags": [], "source": {}, "archived": False}


def _pool_full(e: dict) -> dict:
    return {**POOL_DEFAULTS, "title": e.get("id"), **e}


def pools_list(pid: str) -> list:
    if not _pools_dir(pid).is_dir():
        return []
    doc = _pool_index(pid)
    seen = {e.get("id") for e in doc["pools"]}
    for f in sorted(_pools_dir(pid).glob("*.v*.md")):
        m = _POOL_FILE.match(f.name)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            doc["pools"].append({"id": m.group(1), "kind": "free",
                                 "title": m.group(1), "include": "optional",
                                 "tags": [], "source": {"adopted": True}})
    out = []
    for e in doc["pools"]:
        vs = _pool_versions(pid, e.get("id", ""))
        cur = e.get("current") if e.get("current") in vs else (vs[-1] if vs else None)
        text = _pool_text(pid, e["id"], cur) if cur else ""
        out.append({**_pool_full(e), "current": cur, "versions": vs,
                    "words": len(text.split()), "chars": len(text)})
    return out


def pool_save(pid: str, poolid: str, text: str, **fields) -> dict:
    poolid = _pool_id(poolid)
    text = (text or "").replace("\r\n", "\n")
    with _LOCK:
        doc = _pool_index(pid)
        e = next((x for x in doc["pools"] if x.get("id") == poolid), None)
        if e is None:
            e = {"id": poolid, "kind": "free", "title": poolid,
                 "include": "optional", "tags": [], "source": {},
                 "created": time.strftime("%Y-%m-%d %H:%M:%S")}
            doc["pools"].append(e)
        vs = _pool_versions(pid, poolid)
        cur = e.get("current") if e.get("current") in vs else (vs[-1] if vs else None)
        if cur is not None and _pool_text(pid, poolid, cur) == text:
            v, wrote = cur, False
        else:
            v, wrote = ((vs[-1] + 1) if vs else 1), True
            (_pools_dir(pid, True) / f"{poolid}.v{v}.md").write_text(text, encoding="utf-8",
                                                               newline="\n")
        e.update(_pool_fields(fields))
        e["current"] = v
        e["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _atomic_write(_pools_dir(pid, True) / "index.json", doc)
    return {**_pool_full(e), "versions": _pool_versions(pid, poolid),
            "wrote": wrote, "words": len(text.split())}


def pool_meta(pid: str, poolid: str, **fields) -> dict:
    poolid = _pool_id(poolid)
    with _LOCK:
        doc = _pool_index(pid)
        e = next((x for x in doc["pools"] if x.get("id") == poolid), None)
        if e is None:
            return {}
        e.update(_pool_fields(fields))
        vs = _pool_versions(pid, poolid)
        try:
            if int(fields.get("current")) in vs:
                e["current"] = int(fields["current"])
        except (TypeError, ValueError):
            pass
        e["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _atomic_write(_pools_dir(pid, True) / "index.json", doc)
    return {**_pool_full(e), "versions": _pool_versions(pid, poolid)}



DOCTRINE_DIRNAME = "doctrines"


def _doctrine_seed_dir() -> Path:
    return Path(__file__).resolve().parent / DOCTRINE_DIRNAME


def _doctrine_read(d: Path) -> dict:
    out = {}
    for which in (E.DOCTRINE_KINDS if E else ("plot", "prose")):
        f = d / f"{which}.md"
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        if text.strip():
            out[which] = {"text": text, "version": E.doctrine_version(text)}
    return out


def doctrine_for(pid: str, story: str = None) -> tuple:
    if E is None:
        return {}, "none"
    try:
        proj = _project_dir(pid)
    except Exception:
        proj = None
    if proj and story:
        d = _doctrine_read(proj / "stories" / story / DOCTRINE_DIRNAME)
        if d:
            return d, f"story:{story}"
    if proj:
        d = _doctrine_read(proj / DOCTRINE_DIRNAME)
        if d:
            return d, "project"
    d = _doctrine_read(_doctrine_seed_dir())
    return (d, "shipped-default") if d else ({}, "none")



ASKS_DIRNAME = "asks"


def _asks_dir(pid: str, story: str = None) -> Path:
    proj = _project_dir(pid)
    if story:
        return proj / "stories" / _story_id(story) / ASKS_DIRNAME
    return proj / ASKS_DIRNAME


def _asks_read(d: Path, source: str) -> dict:
    out, seeds = {}, (E.ask_seeds() if E else {})
    try:
        files = sorted(p for p in d.iterdir() if p.is_file()
                       and p.suffix == ".md")
    except OSError:
        return out
    for f in files:
        name = f.stem
        if name not in seeds:
            _trace("ask.unknown_name", source=source, name=str(name)[:40])
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except OSError as e:
            _trace("ask.unreadable", source=source, name=name,
                   err=str(e)[:120])
            continue
        if not text.strip():
            _trace("ask.empty", source=source, name=name)
            continue
        out[name] = {"text": text, "version": E.ask_version(text),
                     "source": source}
    return out


def asks_for(pid: str, story: str = None) -> dict:
    if E is None:
        return {}
    try:
        out = _asks_read(_asks_dir(pid), "project")
        if story:
            out.update(_asks_read(_asks_dir(pid, story), "story"))
    except Exception:
        return {}
    return out



ASK_HISTORY_SUFFIX = ".history.jsonl"


def _ask_history_path(pid: str, name: str, story: str = None) -> Path:
    return _asks_dir(pid, story) / (str(name) + ASK_HISTORY_SUFFIX)


def _ask_history_read(f: Path) -> tuple:
    rows, bad = [], 0
    try:
        raw = f.read_text(encoding="utf-8")
    except OSError:
        return rows, bad
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            r = None
        if isinstance(r, dict) and r.get("version"):
            rows.append(r)
        else:
            bad += 1
            _trace("ask.history.badline", file=f.name, line=line[:80])
    return rows, bad


def ask_save(pid: str, name: str, text: str, story: str = None) -> dict:
    if E is None:
        raise ValueError("the engine is not loaded, so nothing can be "
                         "versioned - refusing to write an untraceable ask")
    name = str(name or "").strip()
    _ask_must_be_shipped(name)
    body = str(text or "")
    if not body.strip():
        raise ValueError(
            f"an ask has to ask something - {name} was saved empty. An empty "
            "file traces `ask.empty` and the shipped wording rides in its "
            "place, which is the silent revert P-D4 refuses outright")
    rung = "story" if story else "project"
    with _LOCK:
        in_force = E.ask_in_force(asks_for(pid, story), name)
        row = E.ask_edit_row(name, body, rung, in_force["version"],
                             time.strftime("%Y-%m-%dT%H:%M:%S"))
        if row["version"] == in_force["version"]:
            _trace("ask.unchanged", project=pid, story=story, name=name,
                   version=row["version"], source=in_force["source"])
            return {"change": "none", "name": name, "wrote": False,
                    "version": row["version"], "parent": in_force["version"],
                    "at": "", "source": in_force["source"], "text": body}
        d = _asks_dir(pid, story)
        d.mkdir(parents=True, exist_ok=True)
        _atomic_text(d / f"{name}.md", body)
        with _ask_history_path(pid, name, story).open(
                "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    _trace("ask.saved", project=pid, story=story, name=name,
           version=row["version"], parent=row["parent"])
    return {"change": "recorded", "name": name, "wrote": True, **row}


def ask_history(pid: str, name: str, story: str = None) -> dict:
    out = {"name": str(name), "rows": [], "bad": 0,
           "current": {}, "origin": "none"}
    if E is None:
        return out
    rows, bad = [], 0
    for rung in ((None, story) if story else (None,)):
        got, n = _ask_history_read(_ask_history_path(pid, name, rung))
        rows += got
        bad += n
    cur = E.ask_in_force(asks_for(pid, story), name)
    if cur["source"] in ("shipped", "none"):
        origin = cur["source"]
    elif any(r.get("version") == cur["version"]
             and r.get("source") == cur["source"] for r in rows):
        origin = "recorded"
    else:
        origin = "hand-edited"
    return {**out, "rows": rows, "bad": bad, "current": cur, "origin": origin}


def ask_at_version(pid: str, name: str, version: str,
                   story: str = None) -> dict:
    want = str(version or "")
    if not want or E is None:
        return {}
    h = ask_history(pid, name, story)
    for r in reversed(h["rows"]):
        if r.get("version") == want:
            return {**{k: r.get(k, "") for k in
                       ("version", "parent", "at", "source", "text")},
                    "origin": "recorded"}
    seed = E.ask_seeds().get(name) or ""
    if seed and E.ask_version(seed) == want:
        return {"version": want, "parent": "", "at": "", "source": "shipped",
                "text": seed, "origin": "shipped"}
    cur = h.get("current") or {}
    if cur.get("version") == want:
        return {"version": want, "parent": "", "at": "",
                "source": cur.get("source", ""), "text": cur.get("text", ""),
                "origin": "hand-edited"}
    return {}




def asks_overview(pid: str, story: str = None) -> dict:
    if E is None:
        return {"asks": [], "doctrines": [], "doctrine_source": "none"}
    ladder = asks_for(pid, story)
    out = []
    for name in sorted(E.ask_seeds()):
        cur = E.ask_in_force(ladder, name)
        row = ask_at_version(pid, name, cur["version"], story)
        if not row:
            out.append({"name": name, "text": cur.get("text", ""),
                        "version": cur.get("version", ""),
                        "source": cur.get("source", "none"),
                        "parent": "", "at": "", "origin": "none",
                        "sample_why": _ask_sample_refusal(name)})
            continue
        out.append({"name": name, **{k: row.get(k, "") for k in
                                     ("text", "version", "source", "parent",
                                      "at", "origin")},
                    "sample_why": _ask_sample_refusal(name)})
    doc, dsrc = doctrine_for(pid, story)
    return {"asks": out,
            "doctrines": [{"which": w, "text": d.get("text", ""),
                           "version": d.get("version", "")}
                          for w, d in sorted(doc.items())],
            "doctrine_source": dsrc,
            "sample": {"max_tokens": ASK_SAMPLE_MAX_TOKENS,
                       "subject_cap": ASK_SAMPLE_SUBJECT_CAP}}



ASK_LEVELS = ("project", "story")

ASK_SAMPLE_MAX_TOKENS = 220
ASK_SAMPLE_SUBJECT_CAP = 2000


def _ask_must_be_shipped(name: str) -> dict:
    seeds = E.ask_seeds() if E else {}
    if name not in seeds:
        raise ValueError(
            f"no such ask: {name!r}. The app ships {len(seeds)} of them, and "
            "the datamine question is minted per request rather than edited "
            "here")
    return seeds


def _ask_draft_entry(draft) -> tuple:
    if not isinstance(draft, dict):
        return None, None
    name = str(draft.get("name") or "").strip()
    text = str(draft.get("text") or "")
    if not name or not text.strip():
        return None, None
    return name, {"text": text, "source": "request"}


def _ask_sample_refusal(name: str) -> str:
    family, _, leaf = str(name or "").partition(".")
    if family != "study":
        return (f"{name} cannot be quick-sampled: a sample reads a small "
                f"subject and a {family or 'nameless'} ask has no subject to "
                f"read - it is asked of the plot and of the stages before it. "
                f"Edit it and bring it back; what it produces is seen by "
                f"running the staged build")
    if E is None or leaf not in E.STUDY_ASPECTS \
            or not E.STUDY_ASPECTS[leaf].get("task"):
        return (f"{name} has no wording of its own to sample - its question "
                f"is written per request rather than edited here")
    return ""


def ask_save_at(pid: str, story: str, name: str, text: str,
                level: str) -> dict:
    lvl = str(level or "").strip().lower()
    if lvl == "shipped":
        raise ValueError(
            "the shipped wording cannot be edited - it is the app's own "
            "source, and what you change rides above it as this project's or "
            "this story's. Pick one of those and the shipped one stays as the "
            "thing your edit is recorded against")
    if lvl not in ASK_LEVELS:
        raise ValueError(
            f"save where? {level!r} is not a level - it is "
            + " or ".join(ASK_LEVELS))
    if lvl == "story" and not str(story or "").strip():
        raise ValueError(
            "no story is bound, so there is no story rung to save on - bind "
            "one, or save this at the project level")
    return ask_save(pid, name, text, story if lvl == "story" else None)


def ask_sample(pid: str, story: str, body: dict) -> dict:
    if E is None:
        raise ValueError("the engine is not loaded, so nothing can be "
                         "versioned - refusing to send an untraceable sample")
    b = body or {}
    name = str(b.get("name") or "").strip()
    _ask_must_be_shipped(name)
    dname, entry = _ask_draft_entry({"name": name, "text": b.get("text")})
    if not dname:
        raise ValueError(
            "there is no wording to sample - the editor is empty. A sample "
            "sends what you are looking at, never what is on disk behind it")
    if (no := _ask_sample_refusal(name)):
        raise ValueError(no)
    leaf = name.partition(".")[2]
    subject = str(b.get("subject") or "")
    if not subject.strip():
        raise ValueError(
            "a sample needs something to read - pick an excerpt, or a subject "
            "on the Study tab, and try again. A sample over nothing would "
            "tell you how the model fills a silence, not what your wording does")
    if len(subject) > ASK_SAMPLE_SUBJECT_CAP:
        raise ValueError(
            f"that subject is {len(subject):,} characters and a quick sample "
            f"takes at most {ASK_SAMPLE_SUBJECT_CAP:,}. A real study splits a "
            f"long subject across chunks and merges them; this one would have "
            f"to cut it, and a sample of the first third is an answer about a "
            f"text you did not send")
    corr = _mint_study_corr("smp")
    sub = jobs_submit({
        "kind": "study", "project": pid, "story": story, "corr": corr,
        "origin": {"where": "prompts", "ask": name,
                   "at": time.strftime("%Y-%m-%d %H:%M:%S")},
        "landing": {},
        "target": {"chapter": "prompts", "part": name},
        "inputs": {"structured": {
            "subject": subject, "aspect": leaf,
            "label": str(b.get("label") or "a quick sample"),
            "chunk_no": 1, "chunk_of": 1,
            "ask_draft": {"name": name, "text": entry["text"]}}, "pools": []},
        "gen": {"model": str(b.get("model") or "") or _default_model(),
                "temperature": 0.3, "max_tokens": ASK_SAMPLE_MAX_TOKENS,
                "n": 1, "seed": 7},
        "priority": "interactive"})
    rec = E._ask_pick({"asks": {name: entry}}, name, "")
    _trace("ask.sample", project=pid, story=story, name=name, corr=corr,
           version=rec["version"], chars=len(subject))
    return {"ok": True, "job_id": sub.get("job_id"), "corr": corr,
            "ask": {"name": name, "version": rec["version"],
                    "source": rec["source"]},
            "max_tokens": ASK_SAMPLE_MAX_TOKENS}


def doctrine_rides_for(pid: str) -> dict:
    base = {k: list(v) for k, v in (E.DOCTRINE_RIDES.items() if E else [])}
    try:
        f = _project_dir(pid) / DOCTRINE_DIRNAME / "rides.json"
        raw = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return base
    if not isinstance(raw, dict):
        _trace("doctrine.rides_malformed", project=pid, kind=type(raw).__name__)
        return base
    for k, v in raw.items():
        if k in base and isinstance(v, list):
            base[k] = [str(x) for x in v]
        else:
            _trace("doctrine.rides_ignored_key", project=pid, key=str(k)[:40])
    return base


def _pool_scope(e: dict) -> str:
    sc = e.get("scope")
    return sc if sc in POOL_SCOPES else "both"


POOL_LESS_KINDS = ("consolidate", "compose", "bake", "summarize_part",
                   "suggest_extracts")
DOCTRINE_LESS_KINDS = ("consolidate", "compose", "bake", "summarize_part",
                       "judge", "study", "study_merge", "suggest_extracts")


def project_context_for(pid: str, selected=None, step: str = "both",
                        detail: bool = False):
    want = {_pool_id(x) for x in (selected or []) if x}
    entries, used, rows, why = [], [], [], {}
    for e in pools_list(pid):
        if e.get("archived"):
            why[e["id"]] = "archived"
            continue
        if not e.get("current"):
            why[e["id"]] = "empty"
            continue
        always = e.get("include") == "always"
        if not always and e["id"] not in want:
            why[e["id"]] = "not chosen"
            continue
        scope = _pool_scope(e)
        if step != "both" and scope != "both" and scope != step:
            why[e["id"]] = f"scope:{scope} vs step:{step}"
            continue
        text = _pool_text(pid, e["id"], e["current"])
        if not text.strip():
            why[e["id"]] = "empty"
            continue
        entries.append({"id": e["id"], "label": e.get("title") or e["id"],
                        "text": text, "always": always,
                        "version": e["current"], "scope": scope,
                        "role": e.get("role") if e.get("role") in POOL_ROLES
                                else "context"})
        used.append(f"{e['id']}@v{e['current']}")
        rows.append({"id": e["id"], "v": e["current"], "scope": scope,
                     "step": step, "always": always,
                     "role": e.get("role") if e.get("role") in POOL_ROLES
                             else "context"})
        _trace("pool.included", id=e["id"], v=e["current"],
               why="always" if always else "chosen",
               role=e.get("role") or "context", scope=scope, step=step,
               words=len(text.split()))
    for pid_, reason in why.items():
        _trace("pool.skipped", id=pid_, why=reason)
    return (entries, used, rows) if detail else (entries, used)


EXTRACTOR_DATA = None
EXTRACTOR_OVERRIDE = None
_EX_ROOT_CACHE = {}

EX_DIMS = ["plot", "events", "adjectives_subjects", "matches_against_toplist",
           "story_context", "writingstyle", "topics", "person_descriptions",
           "person_behaviour", "interactions", "custom_input"]
DIM_LABEL = {
    "plot": "Plot", "events": "Events", "adjectives_subjects": "Descriptions",
    "matches_against_toplist": "Toplist matches", "story_context": "Story context",
    "writingstyle": "Writing style", "topics": "Topics",
    "person_descriptions": "People (descriptions)",
    "person_behaviour": "People (behaviour)", "interactions": "Interactions",
    "custom_input": "Custom input",
}
DEDUPE_DIMS = {"topics", "adjectives_subjects"}


def _ex_root():
    if "root" in _EX_ROOT_CACHE:
        return _EX_ROOT_CACHE["root"]
    if EXTRACTOR_OVERRIDE is not None:
        _EX_ROOT_CACHE["root"] = EXTRACTOR_OVERRIDE
        return EXTRACTOR_OVERRIDE
    root = EXTRACTOR_DATA
    try:
        got = (_ex.get("/extract/health") or {}).get("data_root")
        if got and Path(got).exists():
            root = Path(got)
    except Exception:
        pass
    if root is not None:
        _EX_ROOT_CACHE["root"] = root
    return root


def _ex_chapters(story: str) -> list:
    root = _ex_root()
    if root is None:
        return []
    d = root / "stories" / story
    out = []
    if d.is_dir():
        for c in d.iterdir():
            m = re.fullmatch(r"Chapter\s+(\d+)", c.name) if c.is_dir() else None
            if m:
                out.append((int(m.group(1)), c))
    return sorted(out)


def _dim_file(cdir: Path, dim: str, n: int) -> Path:
    return cdir / ("custom_input.txt" if dim == "custom_input"
                   else f"{dim}_chapter{n}.txt")


def merge_body(story: str, dim: str):
    seen, parts, used, dropped = set(), [], [], 0
    for n, cdir in _ex_chapters(story):
        f = _dim_file(cdir, dim, n)
        if not f.exists():
            continue
        try:
            raw = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        raw = raw.replace("\r\n", "\n").strip()
        if not raw:
            continue
        if dim in DEDUPE_DIMS:
            keep = []
            for line in raw.split("\n"):
                k = line.strip()
                if k and k in seen:
                    dropped += 1
                    continue
                if k:
                    seen.add(k)
                keep.append(line)
            raw = "\n".join(keep).strip()
            if not raw:
                used.append(n)
                continue
        parts.append(f"## Chapter {n}\n\n{raw}")
        used.append(n)
    return "\n\n".join(parts), used, dropped


def _pool_for(pid: str, story: str, dim: str) -> str:
    base = _pool_id(dim)
    for e in pools_list(pid):
        if e["id"] == base:
            owner = (e.get("source") or {}).get("extractor_story")
            return base if owner in (None, story) else _pool_id(f"{dim}-{story}")
    return base


def ingest_report(pid: str) -> dict:
    root = _ex_root()
    if root is None:
        return {"data_root": None, "stories": [],
                "error": "no extractor root: this server was started without "
                         "--extractor-data, and the hardcoded default is gone "
                         "(R1). Nothing is being read, and nothing is lost."}
    pools = {p["id"]: p for p in pools_list(pid)}
    stories = []
    sroot = root / "stories"
    if sroot.is_dir():
        for sd in sorted([d for d in sroot.iterdir() if d.is_dir()],
                         key=lambda d: d.name.lower()):
            chapters = _ex_chapters(sd.name)
            dims = []
            for dim in EX_DIMS:
                files = [n for n, c in chapters if _dim_file(c, dim, n).exists()]
                if not files:
                    continue
                poolid = _pool_for(pid, sd.name, dim)
                e = pools.get(poolid)
                body, used, dropped = merge_body(sd.name, dim)
                if e is None or not e.get("current"):
                    state = "unmerged"
                elif _pool_text(pid, poolid, e["current"]) == body:
                    state = "merged"
                else:
                    state = "stale"
                dims.append({"dim": dim, "label": DIM_LABEL.get(dim, dim),
                             "chapters": used, "files": len(files),
                             "words": len(body.split()), "dropped": dropped,
                             "pool": poolid, "state": state,
                             "pool_version": (e or {}).get("current")})
            stories.append({"story": sd.name, "chapters": [n for n, _ in chapters],
                            "dimensions": dims})
    return {"data_root": str(root), "stories": stories}


def ingest_merge(pid: str, story: str, dim: str, ai: bool = False,
                 instruction: str = "", jobstory: str = None,
                 model: str = None) -> dict:
    if dim not in EX_DIMS:
        return {"error": f"unknown dimension {dim!r}"}
    body, used, dropped = merge_body(story, dim)
    if not body.strip():
        return {"error": "nothing to merge", "story": story, "dim": dim}
    poolid = _pool_for(pid, story, dim)
    cur = next((p for p in pools_list(pid) if p["id"] == poolid), None)
    title = f"{DIM_LABEL.get(dim, dim)} (merged, {len(used)} ch)"
    if cur and cur.get("current") and _pool_text(pid, poolid, cur["current"]) == body:
        out = {"merged": False, "reason": "already up to date", "pool": poolid,
               "version": cur["current"], "chapters": used, "dropped": dropped}
    else:
        e = pool_save(pid, poolid, body, title=title,
                      kind=dim if dim in POOL_KINDS else "free",
                      source={"extractor_story": story, "dimension": dim,
                              "chapters": used,
                              "merged_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        out = {"merged": True, "pool": poolid, "version": e["current"],
               "chapters": used, "dropped": dropped, "words": e["words"]}
    if ai:
        mk = _default_model(model)
        if not mk:
            out["job_error"] = "no model is present; the mechanical merge stands"
            return out
        text = _pool_text(pid, poolid, out["version"])
        out["job"] = jobs_submit({
            "kind": "consolidate", "project": pid,
            "story": jobstory or story,
            "target": {"pool": poolid, "dim": dim},
            "inputs": {"structured": {"label": title, "text": text,
                                      "instruction": instruction}},
            "gen": {"model": mk, "temperature": 0.2, "max_tokens": 2000, "n": 1},
            "priority": "interactive"})["job_id"]
        out["model"] = mk
    return out


CHARS_PER_TOKEN = 4
_CTX_CACHE = {}


REPO = HERE




class DataRootBusy(RuntimeError):

    def __init__(self, jobs):
        self.jobs = list(jobs)
        self.names = [_job_name(j) for j in self.jobs]
        super().__init__(_busy_sentence(self.jobs))


def _busy_sentence(jobs) -> str:
    run = next((j for j in jobs if j.get("status") == "running"), None)
    head = (f"{_job_name(run)} is still running" if run
            else f"{_job_name(jobs[0])} is still waiting")
    rest = len(jobs) - 1
    return head + (f", and {rest} more in the queue." if rest > 0 else ".")


def unfinished_jobs() -> list:
    return [j for j in (_load_jobs().get("jobs") or [])
            if j.get("status") not in FINISHED]


def bind_data_root(root, *, queue_live: bool = True) -> dict:
    global PROJECTS, TRACE_DIR, JOBS
    if root is None or not str(root).strip():
        raise ValueError(
            "no workspace root. Start with --data <folder>; there is no "
            "default any more (R1) and an empty root would bind the folder "
            "the server happens to be running in.")
    root = Path(root).resolve()
    if queue_live and JOBS is not None:
        busy = unfinished_jobs()
        if busy:
            raise DataRootBusy(busy)
    projects = root / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    _local_runner.use_data_dir(root)
    PROJECTS = projects
    TRACE_DIR = root / "trace"
    JOBS = root / "jobs.json"
    _EX_ROOT_CACHE.clear()
    _CTX_CACHE.clear()
    return {"root": str(root), "projects": str(projects_root()),
            "trace": str(TRACE_DIR), "jobs": str(JOBS)}


def unload_data_root() -> dict:
    global PROJECTS, TRACE_DIR, JOBS
    was = bound_workspace()
    if was is None:
        return {"unloaded": True, "already": True, "was": None}
    busy = unfinished_jobs()
    if busy:
        raise DataRootBusy(busy)
    PROJECTS = None
    TRACE_DIR = None
    JOBS = None
    _EX_ROOT_CACHE.clear()
    _CTX_CACHE.clear()
    return {"unloaded": True, "already": False, "was": was}


def _pick_folder_natively():
    import subprocess
    code = (
        "import tkinter as tk\n"
        "from tkinter import filedialog\n"
        "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
        "p = filedialog.askdirectory(title='Where should Story Loom keep everything?')\n"
        "r.destroy()\n"
        "print(p or '', end='')\n")
    try:
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, timeout=300)
    except Exception as e:
        return None, f"could not open a folder dialog: {e}"
    if out.returncode != 0:
        return None, (out.stderr or "the folder dialog failed").strip()[:200]
    return (out.stdout or "").strip(), None


def _workspace_switch(root: str):
    p = Path(root).expanduser()
    was = bound_workspace()
    try:
        bound = bind_data_root(p / "loom")
    except OSError as e:
        return {"error": f"cannot use that folder: {e}"}
    for d in ("loom/trace", "extractor", "logs"):
        (p / d).mkdir(parents=True, exist_ok=True)
    existing = [d.name for d in projects_root().iterdir() if d.is_dir()]
    return {"ok": True, "switched": True,
            "workspace": str(p).replace("/", "\\"),
            "was": was, "roots": bound, "root": bound["root"],
            "projects": sorted(existing)}


def _model_ctx(key: str):
    if not key:
        return None
    if key in _CTX_CACHE:
        return _CTX_CACHE[key]
    try:
        for m in (_studio_get("/api/status").get("models") or []):
            _CTX_CACHE[m.get("key")] = m.get("ctx")
    except Exception:
        return None
    return _CTX_CACHE.get(key)


def _fit(prompt: str, model: str, max_tokens: int, kind: str = "") -> dict:
    ctx = _model_ctx(model)
    approx = len(prompt) // CHARS_PER_TOKEN
    out = {"approx_tokens": approx, "ctx": ctx, "max_tokens": int(max_tokens or 0),
           "fits": True, "headroom": None, "warning": None}
    if not ctx:
        return out
    room = ctx - approx
    out["headroom"] = room
    if approx >= ctx:
        out["fits"] = False
        remedy = ("Read fewer chapters, or let it run in batches."
                  if kind in POOL_LESS_KINDS else
                  "Retire an always-pool, shorten one, or make it optional so "
                  "it only rides on the scenes that need it.")
        out["warning"] = (
            f"the prompt is about {approx:,} tokens but {model}'s window is "
            f"{ctx:,}. Nothing will run. {remedy}")
    elif room < out["max_tokens"]:
        out["fits"] = False
        out["warning"] = (
            f"the prompt is about {approx:,} tokens, leaving {room:,} of "
            f"{model}'s {ctx:,}-token window - less than the {out['max_tokens']:,} "
            "asked for. The answer would be cut short rather than written short. "
            "Shorten the always-pools or lower max tokens.")
    elif room < out["max_tokens"] * 1.5:
        out["warning"] = (
            f"only {room:,} tokens of headroom for a {out['max_tokens']:,}-token "
            "answer. It will fit, but there is little room to spare.")
    return out


def _default_model(prefer: str = None) -> str:
    if prefer:
        return prefer
    try:
        for m in (_studio_get("/api/status").get("models") or []):
            if m.get("present"):
                return m.get("key")
    except Exception:
        pass
    return None


RECIPES = [
    {"id": "order-list", "how": "mechanical",
     "title": "Tidy a list",
     "why": "Sorting is not a judgement. Done in code: headings stripped, "
            "bullets normalised, exact repeats dropped, sorted.",
     "consumes": ["topics"],
     "output": {"id": "topics-tidy", "kind": "topics", "include": "optional",
                "role": "context", "title": "Topics (tidied)"}},

    {"id": "style-instruction", "how": "ai",
     "title": "Write the style instruction",
     "why": "Ten per-chapter style extractions plus the plot and narrative "
            "function of those same chapters, read together, become one sheet "
            "a writer could follow.",
     "consumes": ["writingstyle", "story_context", "plot"],
     "want": "ONE writing-style instruction sheet a writer could follow: "
             "sentence length and rhythm, paragraph shape, vocabulary register, "
             "the ratio of dialogue to narration, how close the point of view "
             "sits, tense and person, and any recurring habits - each grounded "
             "in the kinds of scene this book actually contains",
     "output": {"id": "style", "kind": "style", "include": "always",
                "role": "context", "title": "Writing style (the book's voice)"}},

    {"id": "situation-library", "how": "ai",
     "title": "Build the situation library",
     "why": "What kinds of scene does this book actually do? Named, it can be "
            "asked for again.",
     "consumes": ["events", "interactions", "topics"],
     "want": "a library of the SITUATION TYPES this book actually uses - one "
             "entry per recurring kind of scene, each naming what happens in it "
             "and what makes it land, so a new scene can be asked for by name",
     "output": {"id": "situations", "kind": "examples", "include": "optional",
                "role": "context", "title": "Situations this book does"}},

    {"id": "underneath", "how": "ai",
     "title": "Name what lies underneath",
     "why": "Currents that should run under the story without being stated. "
            "Filed under role `under`, so every prompt says weave it, never "
            "announce it.",
     "consumes": ["topics", "story_context", "plot"],
     "want": "the list of CURRENTS that run underneath this story without being "
             "stated outright - the pressures, appetites and unfinished things "
             "that shape behaviour. One line each, no explanation of how to use "
             "them",
     "output": {"id": "underneath", "kind": "context", "include": "always",
                "role": "under", "title": "Underneath (never announced)"}},

    {"id": "secrets", "how": "ai",
     "title": "Name the secrets",
     "why": "Withheld information. Filed under role `secret`, so every prompt "
            "carries it AND forbids stating it.",
     "consumes": ["plot", "person_descriptions", "interactions"],
     "want": "the list of things WITHHELD from the reader in this story - what "
             "is known by someone and not said. One line each, stated plainly, "
             "with who knows it where that is clear",
     "output": {"id": "secrets", "kind": "context", "include": "always",
                "role": "secret", "title": "Secrets (never stated)"}},
]


def _tidy_list(text: str) -> str:
    seen, out = set(), []
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        t = line.strip().lstrip("-*\u2022 \t").strip()
        if not t or t.startswith("#"):
            continue
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return "\n".join(sorted(out, key=str.lower))


def recipes_list(pid: str) -> list:
    have = {p["id"] for p in pools_list(pid)
            if p.get("current") and not p.get("archived")}
    out = []
    for r in RECIPES:
        used = [x for x in r["consumes"] if x in have]
        out.append({**{k: v for k, v in r.items() if k != "want"},
                    "sources_present": used,
                    "sources_missing": [x for x in r["consumes"] if x not in have],
                    "ready": bool(used)})
    return out


def recipe_run(pid: str, rid: str, pools=None, instruction: str = "",
               model: str = None, jobstory: str = None) -> dict:
    r = next((x for x in RECIPES if x["id"] == rid), None)
    if r is None:
        return {"error": f"no recipe {rid!r}"}
    ids = [_pool_id(x) for x in (pools if pools else r["consumes"])]
    have = {p["id"]: p for p in pools_list(pid)
            if p.get("current") and not p.get("archived")}
    used = [i for i in ids if i in have]
    missing = [i for i in ids if i not in have]
    if not used:
        return {"error": "none of this recipe's sources exist in this project yet",
                "missing": missing, "recipe": rid}
    sources = [{"label": have[i].get("title") or i,
                "text": _pool_text(pid, i, have[i]["current"])} for i in used]
    out = r["output"]
    if r["how"] == "mechanical":
        text = _tidy_list("\n".join(x["text"] for x in sources))
        e = pool_save(pid, out["id"], text, title=out.get("title"),
                      kind=out.get("kind"), include=out.get("include"),
                      role=out.get("role"),
                      source={"recipe": rid, "from": used,
                              "ran_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        return {"ran": "mechanical", "recipe": rid, "pool": out["id"],
                "version": e["current"], "used": used, "missing": missing,
                "lines": len(text.split("\n")) if text else 0}
    mk = _default_model(model)
    if not mk:
        return {"error": "no model is present", "recipe": rid}
    job = jobs_submit({
        "kind": "compose", "project": pid, "story": jobstory or "",
        "target": {"pool": out["id"], "recipe": rid, "meta": out},
        "inputs": {"structured": {"sources": sources, "want": r.get("want"),
                                  "instruction": instruction}},
        "gen": {"model": mk, "temperature": 0.3, "max_tokens": 2400, "n": 1},
        "priority": "interactive"})
    return {"ran": "queued", "recipe": rid, "job": job["job_id"], "model": mk,
            "pool": out["id"], "used": used, "missing": missing}


def ingest_apply(pid: str, job_id: str, index: int = 0) -> dict:
    j = _get_job(job_id)
    if not j:
        return {"error": "no such job"}
    if j.get("kind") not in ("consolidate", "compose", "bake"):
        return {"error": f"job {job_id} is a {j.get('kind')}, "
                         "not a consolidate, compose or bake"}
    if j.get("status") not in ("done", "consumed"):
        return {"error": f"job is {j.get('status')}, not done"}
    cands = ((j.get("result") or {}).get("candidates") or [])
    if not 0 <= index < len(cands):
        return {"error": "no such candidate", "have": len(cands)}
    text = (cands[index].get("text") or "").strip()
    if not text:
        return {"error": "that candidate is empty"}
    tgt = j.get("target") or {}
    poolid = _pool_id(tgt.get("pool"))
    before = next((p for p in pools_list(pid) if p["id"] == poolid), None)
    meta = {k: v for k, v in (tgt.get("meta") or {}).items()
            if k in POOL_META_KEYS}
    stamp = str(tgt.get("stamp") or "")
    if stamp and stamp.strip() and stamp.strip() not in text:
        text = stamp + text
    e = pool_save(pid, poolid, text, **meta,
                  source={**((before or {}).get("source") or {}),
                          **({"recipe": tgt["recipe"]} if tgt.get("recipe") else {}),
                          "consolidated_from": job_id,
                          "consolidated_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    _mutate(job_id, status="consumed")
    return {"pool": poolid, "version": e["current"], "versions": e["versions"],
            "previous": (before or {}).get("current")}


def _story_dir(pid: str, sid: str, create: bool = False) -> Path:
    d = _project_paths(pid, create)["stories"] / _story_id(sid)
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


PROJECT_OVERRIDE = None


def _story_paths(pid: str, sid: str, create: bool = False) -> dict:
    d = _story_dir(pid, sid, create)
    p = {"dir": d, "project": d / "project.json", "state": d / "state.json",
         "manuscript": d / "manuscript", "meta": d / "story.json"}
    if PROJECT_OVERRIDE is not None:
        p["project"] = PROJECT_OVERRIDE
        p["state"] = PROJECT_OVERRIDE.with_name(PROJECT_OVERRIDE.stem + "-state.json")
    return p


def _story_meta(pid: str, sid: str) -> dict:
    p = _story_paths(pid, sid)["meta"]
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    meta.setdefault("title", sid)
    meta.setdefault("created", None)
    meta.setdefault("notes", "")
    scope = meta.get("scope") or {}
    scope.setdefault("toplist", "story")
    scope.setdefault("examples", "story")
    meta["scope"] = scope
    meta["id"] = sid
    return meta


def _write_story_meta(pid: str, sid: str, **fields) -> dict:
    meta = _story_meta(pid, sid)
    meta.update({k: v for k, v in fields.items() if v is not None})
    if not meta.get("created"):
        meta["created"] = time.strftime("%Y-%m-%d %H:%M:%S")
    meta["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    meta["id"] = sid
    _atomic_write(_story_paths(pid, sid)["meta"], meta)
    return meta


def stories_list(pid: str) -> list:
    root = _project_paths(pid)["stories"]
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        sid = d.name
        meta = _story_meta(pid, sid)
        st = {}
        try:
            st = (json.loads((d / "state.json").read_text(encoding="utf-8"))
                  or {}).get("state") or {}
        except (OSError, ValueError):
            st = {}
        chapters = st.get("chapters") or []
        parts = sum(len(c.get("parts") or []) for c in chapters)
        written = sum(1 for c in chapters for p in (c.get("parts") or [])
                      if p.get("current") is not None)
        words = 0
        for c in chapters:
            for p in c.get("parts") or []:
                i = p.get("current")
                vs = p.get("versions") or []
                if i is not None and 0 <= i < len(vs):
                    words += len((vs[i].get("text") or "").split())
        out.append({**meta, "chapters": len(chapters), "parts": parts,
                    "written": written, "words": words,
                    "updated": time.strftime("%Y-%m-%d %H:%M",
                                             time.localtime((d / "project.json").stat().st_mtime))
                               if (d / "project.json").exists() else None})
    return out


def _backfill(pid: str, sid: str, dry: bool = False) -> dict:
    st = {}
    try:
        st = (json.loads(_story_paths(pid, sid)["state"].read_text(encoding="utf-8"))
              or {}).get("state") or {}
    except (OSError, ValueError):
        return {"story": sid, "error": "no state.json for this story", "filed": 0}
    store = _store(pid, sid)
    have = {(p["chapter"], p["part"]): len(p.get("versions") or [])
            for p in store.list()}
    filed, skipped, plan = 0, 0, []
    for ci, ch in enumerate(st.get("chapters") or [], start=1):
        for pi, part in enumerate(ch.get("parts") or [], start=1):
            versions = part.get("versions") or []
            already = have.get((ci, pi), 0)
            for vi, v in enumerate(versions):
                if vi < already:
                    skipped += 1
                    continue
                text = (v.get("text") or "").strip()
                if not text:
                    continue
                plan.append({"chapter": ci, "part": pi, "version": vi + 1,
                             "words": len(text.split())})
                if not dry:
                    prov = dict(v.get("provenance") or {})
                    prov.setdefault("origin_note", "backfilled from project state")
                    store.accept(ci, pi, text, prov)
                filed += 1
    return {"story": sid, "filed": filed, "skipped_already_filed": skipped,
            "dry": dry, "plan": plan[:50]}


_LOCK = threading.Lock()


_TMP_SEQ = itertools.count()


def _tmp_for(path: Path) -> Path:
    return path.with_suffix(f".{os.getpid()}.{next(_TMP_SEQ)}.tmp")


def _atomic_write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_for(path)
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    for attempt in range(8):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 7:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05 * (attempt + 1))


def _atomic_text(path: Path, text: str) -> None:
    tmp = _tmp_for(path)
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(8):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 7:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05 * (attempt + 1))


def _load_project(pid: str, sid: str) -> dict:
    try:
        return json.loads(_story_paths(pid, sid)["project"].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"rev": 0, "ops": [], "ptr": 0, "saved_at": None, "story": _story_id(sid)}



def slot_chapter_summary(cid):     return E.slot_for("ch", cid, "summary")
def slot_chapter_title(cid):       return E.slot_for("ch", cid, "title")
def slot_chapter_facet(cid, k):    return E.slot_for("ch", cid, "facet", k)
def slot_part_notes(pid):          return E.slot_for("pt", pid, "notes")
def slot_part_summary(pid):        return E.slot_for("pt", pid, "summary")
def slot_instructions(field):      return E.slot_for("sty", "instructions", field)
def slot_style(field):             return E.slot_for("sty", "style", field)


def authored_slots(state: dict):
    st = state or {}
    for c in (st.get("chapters") or []):
        cid = c.get("id")
        if not cid:
            continue
        yield slot_chapter_summary(cid), c.get("summary")
        yield slot_chapter_title(cid), c.get("title")
        for k, v in (c.get("facets") or {}).items():
            yield slot_chapter_facet(cid, k), v
        for p in (c.get("parts") or []):
            pid = p.get("id")
            if not pid:
                continue
            yield slot_part_notes(pid), p.get("notes")
            sm = p.get("summary")
            if isinstance(sm, dict) and sm:
                yield slot_part_summary(pid), "\n".join(
                    f"{h}: {sm.get(k, '')}" for k, h in E.SUMMARY_FIELDS)
    for k, v in (st.get("instructions") or {}).items():
        yield slot_instructions(k), v
    for k, v in (st.get("style") or {}).items():
        yield slot_style(k), v


def freeze_authored(pid: str, state: dict, corr: str = None) -> dict:
    store = _text_store(pid)
    wrote, seen = [], 0
    for slot, text in authored_slots(state):
        if not E.norm_text(text):
            continue
        seen += 1
        r = store.put(slot, text, corr)
        if r["wrote"]:
            wrote.append(r["ref"])
    return {"seen": seen, "wrote": wrote}


def _save_project(pid: str, sid: str, doc: dict) -> dict:
    sid = _story_id(sid)
    with _LOCK:
        cur = _load_project(pid, sid)
        out = {"rev": int(cur.get("rev", 0)) + 1, "story": sid,
               "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "ops": doc.get("ops", []), "ptr": doc.get("ptr", 0)}
        paths = _story_paths(pid, sid, True)
        for path, payload in ((paths["project"], out),
                              (paths["state"], {"rev": out["rev"], "story": sid,
                                                "saved_at": out["saved_at"],
                                                "state": doc.get("state")})):
            _atomic_write(path, payload)
        if not paths["meta"].exists():
            _write_story_meta(pid, sid)
        frozen = {}
        try:
            frozen = freeze_authored(pid, doc.get("state") or {})
            if frozen.get("wrote"):
                _trace("texts.frozen", project=pid, story=sid,
                       n=len(frozen["wrote"]), rev=out["rev"])
        except Exception as e:
            _trace("texts.freeze_failed", project=pid, story=sid,
                   err=str(e)[:160])
        return {"rev": out["rev"], "story": sid,
                "texts_frozen": len(frozen.get("wrote") or [])}


def _store(pid: str, sid: str):
    if E is None:
        raise RuntimeError("loom_engine is not importable on this server")
    return E.FileStore(_story_paths(pid, sid)["manuscript"])


JOB_CAP = 200
_JOBS_LOCK = threading.RLock()
_WAKE = threading.Event()

import extractor as _ex

_EXTRACTOR_STATIC = HERE / "story-extractor"


_JOBS_READ_FAULT = {"kind": "", "detail": "", "at": 0.0, "count": 0}


def jobs_store_fault() -> dict:
    return dict(_JOBS_READ_FAULT)


def _note_jobs_fault(kind: str, detail: str) -> None:
    _JOBS_READ_FAULT["kind"] = kind
    _JOBS_READ_FAULT["detail"] = detail[:200]
    _JOBS_READ_FAULT["at"] = time.time()
    _JOBS_READ_FAULT["count"] += 1
    speak, folded = _warn_ok(f"jobs.unreadable|{kind}")
    if not speak:
        return
    print(f"[loom] THE JOB STORE COULD NOT BE READ: {kind}: {detail[:200]} "
          f"(#{_JOBS_READ_FAULT['count']}, {JOBS}) - the queue is NOT empty, "
          "it is UNREADABLE, and nothing will run until this clears"
          f"{_warn_more(folded)}",
          flush=True)


def jobs_store() -> Path:
    if JOBS is None:
        raise NoProjectLoaded(
            "no project is loaded: this server has no workspace bound, so "
            "there is no job queue to read or write. Choose a folder to work "
            "in first.")
    return JOBS


def _load_jobs() -> dict:
    store = jobs_store()
    try:
        return json.loads(store.read_text(encoding="utf-8"))
    except MemoryError as e:
        _note_jobs_fault("MemoryError", str(e))
        raise
    except (OSError, ValueError):
        return {"seq": 0, "jobs": []}


def _store_rows(doc) -> list:
    if isinstance(doc, dict) and isinstance(doc.get("jobs"), list):
        return doc["jobs"]
    return None


def _save_jobs(doc: dict) -> None:
    doc["jobs"] = doc.get("jobs", [])[-JOB_CAP:]
    _atomic_write(jobs_store(), doc)


def _stalled_for(j: dict) -> float:
    if j.get("status") != "running":
        return 0.0
    at = (j.get("progress") or {}).get("at") or j.get("started") or j.get("created")
    try:
        return max(0.0, round(time.time() - float(at), 1))
    except (TypeError, ValueError):
        return 0.0



PLAN_STAGE_WORDS = {"plot": "the plot", "development": "how it develops",
                    "scenes": "the scenes", "material": "the material",
                    "blueprint": "the blueprint"}

JOB_NAMES = {
    "write_part":       ("New part {}", "New part"),
    "replace_para":     ("New paragraph {}", "New paragraph"),
    "expand_para":      ("Longer paragraph {}", "Longer paragraph"),
    "chapter_plot":     ("New plot for chapter {}", "New plot for a chapter"),
    "part_plan":        ("New plan for part {}", "New plan for a part"),
    "summarize_part":   ("Summary of part {}", "Summary of a part"),
    "suggest":          ("Directions for chapter {}", "Directions"),
    "judge":            ("Verdict on {}", "Verdict"),
    "suggest_extracts": ("Document to Extracts", "Document to Extracts"),
}


def _job_stage(j: dict) -> str:
    st = (j.get("inputs") or {}).get("structured") or {}
    return str(st.get("stage") or (j.get("target") or {}).get("plan_stage")
               or "").strip()


def _job_name(j: dict) -> str:
    kind = str(j.get("kind") or "").strip()
    t = j.get("target") or {}
    st = (j.get("inputs") or {}).get("structured") or {}
    pos = str((j.get("landing") or {}).get("pos") or "").strip()

    if kind == "stage_plan":
        stage = _job_stage(j).lower()
        words = PLAN_STAGE_WORDS.get(stage, stage)
        head = f"Chapter {pos}" if pos else "Chapter"
        return f"{head} · {words}" if words else head
    if kind == "ideas":
        focus = str(t.get("focus") or st.get("focus") or "").strip()
        return f"Brainstorm: {focus}" if focus else "Brainstorm"
    if kind == "consolidate":
        return "Extracts to " + (str(t.get("dim") or t.get("pool") or "").strip()
                                 or "a pool")
    if kind == "compose":
        return "Recipe to " + (str(t.get("pool") or "").strip() or "a pool")
    if kind == "bake":
        return "Sources to " + (str(t.get("artifact") or t.get("pool")
                                    or "").strip() or "an artifact")
    if kind in ("study", "study_merge"):
        what = str(st.get("aspect") or st.get("label") or "").strip()
        base = f"Study of {what}" if what else "Study"
        return base + ", joining" if kind == "study_merge" else base

    row = JOB_NAMES.get(kind)
    if not row:
        return kind or "a job"
    return row[0].format(pos) if pos else row[1]


def _job_public(j: dict) -> dict:
    out = {k: j.get(k) for k in ("id", "kind", "target", "status", "queue_pos",
                                  "progress", "created", "finished", "error",
                                  "priority", "n_done", "n", "story", "project",
                                  "pools_used", "_spent",
                                  "corr", "corr_source", "retry_of",
                                  "target_hash", "origin", "landing",
                                  "playbook_seal",
                                  "adopted")}
    out["stalled_for"] = _stalled_for(j)
    g = j.get("gen") or {}
    out["budget"] = int(g.get("max_tokens") or 0) * max(1, int(g.get("n") or 1))
    out["model"] = g.get("model")
    out["stage"] = _job_stage(j) or None
    out["name"] = _job_name(j)
    return out


def _recompute_queue_positions(doc: dict) -> None:
    order = _queue_order(doc)
    for pos, j in enumerate(order, start=1):
        j["queue_pos"] = pos
    for j in doc["jobs"]:
        if j["status"] != "queued":
            j["queue_pos"] = 0


def _queue_order(doc: dict) -> list:
    q = [j for j in doc["jobs"] if j["status"] == "queued"]
    q.sort(key=lambda j: (0 if j.get("priority") == "interactive" else 1, j["created"]))
    return q


CORR_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"
CORR_KIND = {"write_part": "wrp", "replace_para": "rpl", "suggest": "sug",
             "ideas": "ide", "chapter_plot": "plt", "summarize_part": "sum",
             "consolidate": "con", "compose": "cmp", "judge": "jdg",
             "bake": "bak",
             "study": "stu", "study_merge": "stm",
             "suggest_extracts": "sgx",
             "stage_plan": "stg",
             "part_plan": "ppl"}




def _mint_corr(kind: str) -> str:
    k3 = CORR_KIND.get(kind, "job")
    tail = "".join(random.choice(CORR_ALPHABET) for _ in range(4))
    return f"r-{time.strftime('%Y%m%d-%H%M%S')}-{k3}-{tail}"


class CorrCollision(Exception):
    pass


def _req_dir(project: str, corr: str, create: bool = False) -> Path:
    month = corr[2:8] if len(corr) > 8 else "unknown"
    month = f"{month[:4]}-{month[4:6]}" if len(month) == 6 else "unknown"
    d = _project_dir(project, create) / "requests" / month / corr
    if create:
        d.parent.mkdir(parents=True, exist_ok=True)
        try:
            d.mkdir()
        except FileExistsError as e:
            raise CorrCollision(corr) from e
    return d


def _prompt_blocks(raw: str) -> list:
    out, line = [], 1
    for chunk in raw.split(E.DIVIDER):
        head = next((l for l in chunk.splitlines() if l.strip()), "")
        out.append({"name": head[:80], "line": line, "chars": len(chunk),
                    "lines": len(chunk.splitlines())})
        line += chunk.count("\n") + E.DIVIDER.count("\n")
    return out


def _req_write(project: str, corr: str, name: str, payload, mode: str = "json"):
    try:
        d = _req_dir(project, corr)
        d.mkdir(parents=True, exist_ok=True)
        f = d / name
        f.parent.mkdir(parents=True, exist_ok=True)
        if mode == "json":
            _atomic_write(f, payload)
        elif mode == "append":
            with f.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        else:
            f.write_text(payload, encoding="utf-8", newline="\n")
        return True
    except Exception as e:
        _trace("request.write_failed", corr=corr, name=name, err=str(e)[:120])
        return False


def _req_event(job: dict, event: str, **kv):
    _req_write(job.get("project"), job.get("corr"), "events.jsonl",
               {"t": round(time.time(), 3), "event": event,
                "job": job.get("id"), **kv}, mode="append")



_PICK_FAULT = {"kind": "", "detail": "", "at": 0.0, "count": 0}


def pick_fault() -> dict:
    return dict(_PICK_FAULT)


def _note_pick_fault(kind: str, detail: str) -> None:
    _PICK_FAULT["kind"] = kind
    _PICK_FAULT["detail"] = detail[:200]
    _PICK_FAULT["at"] = time.time()
    _PICK_FAULT["count"] += 1
    _warn("pick.unrecorded", kind=kind, detail=detail[:200],
          total=_PICK_FAULT["count"])


_sample_key = loom_picks.sample_key


def _record_pick(pid: str, prov: dict, wrote: dict,
                 backfilled: bool = False) -> bool:
    prov = prov or {}
    corr, key = prov.get("corr"), _sample_key(prov.get("sample"))
    if not corr or not key:
        return False
    try:
        d = _req_dir(pid, corr)
        out = d / "samples" / key / "output.md"
        sha = (hashlib.sha256(out.read_bytes()).hexdigest()
               if out.exists() else None)
        f = d / "request.json"
        job = ((json.loads(f.read_text(encoding="utf-8")) or {}).get("job")
               if f.exists() else None)
        row = {"t": round(time.time(), 3), "event": "picked",
               "sample": key, "job": job, "sha256": sha,
               "version": (wrote or {}).get("version")}
        if backfilled:
            row["backfilled"] = True
        ok = _req_write(pid, corr, "events.jsonl", row, mode="append")
        if not ok:
            _note_pick_fault("write_failed", f"{corr} sample {key}")
            return False
        if sha is None:
            _note_pick_fault("no_output", f"{corr} sample {key}")
        return True
    except Exception as e:
        _note_pick_fault(type(e).__name__, f"{corr}: {str(e)[:120]}")
        return False


def _playbooks_for(pid: str, sid: str):
    if loom_playbooks is None:
        raise loom_routing.RouteError(
            "playbooks are not available on this server")
    return loom_playbooks.store_for(_story_dir(pid, sid))


def playbooks_list(pid: str, sid: str) -> dict:
    st = _playbooks_for(pid, sid)
    out = [{**rec, "builtin": True} for rec in loom_playbooks.builtins()]
    for row in st.list():
        rec = st.read(row["name"]) if row.get("current") else None
        out.append({**row,
                    "label": (rec or {}).get("label"),
                    "ref": (rec or {}).get("ref"),
                    "settings": (rec or {}).get("settings") or {},
                    "builtin": False})
    return {"playbooks": out}


def playbooks_save(pid: str, sid: str, body: dict) -> dict:
    st = _playbooks_for(pid, sid)
    name = body.get("name")
    settings = body.get("settings")
    if loom_playbooks.is_builtin_name(name):
        return {"error":
                f"{str(name).strip()!r} is a template that ships with the app "
                f"and is the same in every book, so it cannot be saved over. "
                f"Save under another name - or, to vary it, copy its settings "
                f"into one: they are listed beside it in the picker."}
    try:
        rec = st.save(name, settings, corr=body.get("corr"))
    except loom_playbooks.PlaybookError as e:
        return {"error": str(e)}
    return {"playbook": rec}


def _playbook_seal(body: dict) -> dict:
    ref = str(body.get("playbook") or "").strip()
    if not ref:
        return {}
    if loom_playbooks is None:
        raise loom_routing.RouteError(
            "playbooks are not available on this server, so a run cannot "
            "be sealed to one")
    pid, sid = _project_id(body.get("project")), _story_id(body.get("story"))
    try:
        st = loom_playbooks.store_for(_story_dir(pid, sid, True))
        rec = loom_playbooks.builtin_resolve(ref)
        if rec is None:
            rec = st.resolve(ref)
        if rec is None and "@" in ref:
            nm, _, v = ref.rpartition("@")
            rec = st.read(nm, v) if v.isdigit() else None
        if rec is None and "@" not in ref:
            rec = st.read(ref)
    except loom_playbooks.PlaybookError as e:
        raise loom_routing.RouteError(str(e)) from e
    except Exception as e:
        raise loom_routing.RouteError(
            f"playbook {ref!r} could not be read for {sid!r}: {e}") from e
    if not rec:
        raise loom_routing.RouteError(
            f"playbook {ref!r} does not exist in {sid!r}")
    seal = loom_playbooks.seal_fields(rec)
    name = ((rec.get("settings") or {}).get("runbook")
            or loom_playbooks.DEFAULT_RUNBOOK)
    try:
        rb = loom_playbooks.runbooks_for(_story_dir(pid, sid, True)).pin(name)
    except loom_playbooks.PlaybookError as e:
        raise loom_routing.RouteError(
            f"playbook {ref!r} names runbook {name!r}, which does not exist "
            f"in {sid!r}: {e}") from e
    return {**seal, **loom_playbooks.runbook_seal(rb)}


ALWAYS, IF_SET = "always", "if_set"
GEN_FIELDS = (
    ("model",         ALWAYS, lambda v, g: v),
    ("temperature",   IF_SET, lambda v, g: v),
    ("seed",          ALWAYS, lambda v, g: v),
    ("max_tokens",    ALWAYS, lambda v, g: int(v or 600)),
    ("n",             ALWAYS, lambda v, g: max(1, int(v or 1))),
    ("prompt_suffix", IF_SET, lambda v, g: str(v)[:200]),
    ("retries",       IF_SET, lambda v, g: max(0, min(RETRY_MAX, int(v)))),

)
_SET_TEST = {"retries": lambda v: v is not None,
             "temperature": lambda v: v is not None}


def gen_for(gen: dict) -> dict:
    g = gen or {}
    out = {}
    for name, mode, coerce in GEN_FIELDS:
        raw = g.get(name)
        if mode == ALWAYS:
            out[name] = coerce(raw, g)
            continue
        is_set = _SET_TEST.get(name, lambda v: bool(v))
        if is_set(raw):
            out[name] = coerce(raw, g)
    return out


def jobs_submit(body: dict) -> dict:
    _clear_stale_head()
    with _JOBS_LOCK:
        doc = _load_jobs()
        doc["seq"] = int(doc.get("seq", 0)) + 1
        gen = body.get("gen") or {}
        corr = str(body.get("corr") or "").strip()
        corr_source = "client"
        if not re.fullmatch(r"r-\d{8}-\d{6}-[a-z]{3}-[a-z0-9]{4}", corr):
            corr = _mint_corr(body.get("kind", ""))
            corr_source = "server"

        _kind = body.get("kind", "write_part")
        _target = body.get("target") or {}
        loom_routing.check(_kind, _target, body.get("landing") or {})

        _seal = _playbook_seal(body)
        _inputs = body.get("inputs") or {}
        if _seal:
            _applied = loom_playbooks.apply_to(
                _seal.get("playbook_settings") and {"settings": _seal["playbook_settings"]} or {},
                gen, _inputs.setdefault("structured", {}))
            if _applied:
                _seal = {**_seal, "playbook_applied": _applied}
        job = {
            "id": f"j{doc['seq']:05d}",
            "corr": corr,
            "corr_source": corr_source,
            "origin": body.get("origin") or {},
            "landing": body.get("landing") or {},
            "retry_of": (str(body.get("retry_of")).strip() or None)
                        if body.get("retry_of") else None,
            "project": _project_id(body.get("project")),
            "story": _story_id(body.get("story")),
            "kind": body.get("kind", "write_part"),
            "target": body.get("target") or {},
            "inputs": _inputs,
            "gen": gen_for(gen),
            "priority": "interactive" if body.get("priority") == "interactive" else "batch",
            "target_hash": body.get("target_hash"),
            **({"playbook_seal": _seal} if _seal else {}),
            "status": "queued", "queue_pos": 0, "progress": {},
            "created": time.time(), "finished": None, "error": None,
            "studio_job_ids": [], "result": None,
            "n": max(1, int(gen.get("n") or 1)), "n_done": 0,
        }
        doc["jobs"].append(job)
        _recompute_queue_positions(doc)
        _save_jobs(doc)
        _WAKE.set()
        for _attempt in range(5):
            try:
                _req_dir(job["project"], job["corr"], create=True)
                break
            except CorrCollision:
                if corr_source != "server" or _attempt == 4:
                    doc["jobs"].remove(job)
                    _save_jobs(doc)
                    raise
                job["corr"] = corr = _mint_corr(job["kind"])
                _trace("job.corr.reminted", id=job["id"], corr=corr,
                       attempt=_attempt + 1)
                _save_jobs(doc)
        _req_write(job["project"], job["corr"], "request.json", {
            "corr": job["corr"], "corr_source": job.get("corr_source"),
            "retry_of": job.get("retry_of"),
            "job": job["id"], "kind": job["kind"],
            "project": job["project"], "story": job["story"],
            "target": job["target"], "gen": job["gen"],
            "priority": job["priority"], "target_hash": job.get("target_hash"),
            "origin": body.get("origin") or {},
            "landing": body.get("landing") or {},
            **(job.get("playbook_seal") or {}),
            "queued_at": job["created"], "status": "queued"})
        _req_event(job, "queued", kind=job["kind"], n=job["n"])
        _trace("job.queued", corr=job.get("corr"), id=job["id"], kind=job["kind"],
               project=job["project"], story=job["story"],
               model=job["gen"].get("model"), n=job["n"],
               pos=job["queue_pos"])
        return {"job_id": job["id"], "queue_pos": job["queue_pos"],
                "corr": job["corr"], "corr_source": job["corr_source"]}


def _mutate(job_id: str, **fields):
    with _JOBS_LOCK:
        doc = _load_jobs()
        for j in doc["jobs"]:
            if j["id"] == job_id:
                j.update(fields)
                _recompute_queue_positions(doc)
                _save_jobs(doc)
                return j
        return None


def _get_job(job_id: str):
    with _JOBS_LOCK:
        for j in _load_jobs()["jobs"]:
            if j["id"] == job_id:
                return j
    return None


GIVE_UP_GRACE_S = float(os.environ.get("LOOM_GIVE_UP_GRACE_S", "10"))


STALE_HEAD_S = float(os.environ.get("LOOM_STALE_HEAD_S", "90"))


def _clear_stale_head() -> int:
    cleared = 0
    with _JOBS_LOCK:
        doc = _load_jobs()
        for j in doc["jobs"]:
            if j.get("status") != "running":
                continue
            still = _stalled_for(j)
            if still < STALE_HEAD_S:
                continue
            j["cancel"] = True
            j["status"] = "failed"
            j["finished"] = time.time()
            j["error"] = (f"no progress for {int(still)}s, and a new "
                          f"generation was asked for - the queue was given "
                          f"to it")
            cleared += 1
            _trace("job.stale_head_cleared", job=j["id"], still=int(still))
        if cleared:
            _recompute_queue_positions(doc)
            _save_jobs(doc)
    return cleared


def jobs_give_up(job_id: str) -> dict:
    with _JOBS_LOCK:
        doc = _load_jobs()
        for j in doc["jobs"]:
            if j["id"] != job_id:
                continue
            if j["status"] in FINISHED:
                return {"ok": False, "was": j["status"]}
            was = j["status"]
            j["cancel"] = True
            j["give_up_at"] = time.time()
            if was == "queued":
                j["status"] = "cancelled"
                j["finished"] = time.time()
                _recompute_queue_positions(doc)
            _save_jobs(doc)
            for sid in j.get("studio_job_ids", []):
                try:
                    _studio_post(f"/api/jobs/{sid}/cancel", {})
                except Exception:
                    pass
            return {"ok": True, "was": was, "status": j["status"],
                    "grace_s": GIVE_UP_GRACE_S}
    return {"ok": False, "error": "no such job"}


def _reap_given_up() -> None:
    now = time.time()
    with _JOBS_LOCK:
        doc = _load_jobs()
        changed = False
        for j in doc["jobs"]:
            if j.get("status") != "running" or not j.get("give_up_at"):
                continue
            waited = now - float(j["give_up_at"])
            if waited < GIVE_UP_GRACE_S:
                continue
            j["status"] = "failed"
            j["finished"] = now
            j["error"] = (f"given up on by the author; the worker did not "
                          f"let go within {int(waited)}s, so the queue was "
                          f"taken back over its head")
            changed = True
            _trace("job.reaped", job=j["id"], waited=int(waited))
        if changed:
            _recompute_queue_positions(doc)
            _save_jobs(doc)
            _WAKE.set()


def jobs_cancel(job_id: str) -> dict:
    with _JOBS_LOCK:
        doc = _load_jobs()
        for j in doc["jobs"]:
            if j["id"] != job_id:
                continue
            if j["status"] == "queued":
                j["status"] = "cancelled"
                j["finished"] = time.time()
                _recompute_queue_positions(doc)
                _save_jobs(doc)
                return {"ok": True, "was": "queued"}
            if j["status"] == "running":
                j["cancel"] = True
                if not j.get("studio_job_ids"):
                    j["status"] = "cancelled"
                    j["finished"] = time.time()
                    _recompute_queue_positions(doc)
                _save_jobs(doc)
                for sid in j.get("studio_job_ids", []):
                    try:
                        _studio_post(f"/api/jobs/{sid}/cancel", {})
                    except Exception:
                        pass
                return {"ok": True, "was": "running",
                        "status": j["status"]}
            return {"ok": False, "was": j["status"]}
    return {"ok": False, "error": "no such job"}


FINISHED = ("done", "failed", "cancelled", "consumed")


def _finished_at(j: dict):
    f = j.get("finished")
    if isinstance(f, bool) or not isinstance(f, (int, float)):
        return None
    return float(f)


def generation_health() -> dict:
    try:
        with _JOBS_LOCK:
            jobs = list(_load_jobs().get("jobs") or [])
    except Exception:
        jobs = []
    jobs = [j for j in jobs if isinstance(j, dict)]
    terminal = [j for j in jobs
                if j.get("status") in FINISHED and _finished_at(j) is not None]
    terminal.sort(key=_finished_at)
    last = terminal[-1] if terminal else None
    fails = 0
    for j in reversed(terminal):
        if j.get("status") != "failed":
            break
        fails += 1
    return {
        "last": None if last is None else {
            "status": last.get("status"),
            "error": last.get("error"),
            "at": _finished_at(last),
            "job": last.get("id"),
            "corr": last.get("corr"),
        },
        "consecutive_failures": fails,
        "queued": sum(1 for j in jobs if j.get("status") == "queued"),
        "running": sum(1 for j in jobs if j.get("status") == "running"),
    }


def jobs_forget(job_id: str) -> dict:
    with _JOBS_LOCK:
        doc = _load_jobs()
        for i, j in enumerate(doc["jobs"]):
            if j["id"] != job_id:
                continue
            if j["status"] not in FINISHED:
                return {"ok": False, "error": f"job is {j['status']}, not finished"}
            doc["jobs"].pop(i)
            _recompute_queue_positions(doc)
            _save_jobs(doc)
            return {"ok": True, "forgot": job_id}
        return {"ok": False, "error": "no such job"}


def jobs_prune(status=None, project=None, keep: int = 0) -> dict:
    want = set(status or FINISHED) & set(FINISHED)
    with _JOBS_LOCK:
        doc = _load_jobs()
        jobs = doc["jobs"]
        cand = [j for j in jobs
                if j["status"] in want
                and (project is None
                     or _project_id(j.get("project")).lower() == _project_id(project).lower())]
        cand.sort(key=lambda j: j.get("created") or 0)
        drop = {id(j) for j in (cand[:-keep] if keep else cand)}
        doc["jobs"] = [j for j in jobs if id(j) not in drop]
        removed = len(jobs) - len(doc["jobs"])
        if removed:
            _recompute_queue_positions(doc)
            _save_jobs(doc)
    return {"ok": True, "removed": removed, "left": len(doc["jobs"])}


def jobs_consume(job_id: str):
    with _JOBS_LOCK:
        doc = _load_jobs()
        for j in doc["jobs"]:
            if j["id"] != job_id:
                continue
            if j["status"] != "done":
                return None, f"job is {j['status']}, not done"
            j["status"] = "consumed"
            _save_jobs(doc)
            return {"result": j.get("result"),
                    "pools_used": j.get("pools_used") or []}, None
    return None, "no such job"


def _sample_no(job: dict) -> int:
    v = job.get("_variant")
    if isinstance(v, int) and v > 0:
        return v
    if int(job.get("n") or 1) > 1:
        _trace("sample_no.FALLBACK", job=job.get("id"), corr=job.get("corr"),
               n=job.get("n"), why="_variant missing on a multi-sample job - "
                                   "samples will collide, see _sample_no")
    return (job.get("n_done") or 0) + 1


_STUDIO_URL: str | None = None


def _studio_post(path: str, body: dict) -> dict:
    if _STUDIO_URL is None:
        return _local_runner.post(path, body)
    req = urllib.request.Request(_STUDIO_URL + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read() or b"{}"
    try:
        return json.loads(raw)
    except ValueError as e:
        head = raw[:80].decode("utf-8", "replace").replace(chr(10), " ")
        raise GenerationFailed(
            f"the model source answered {path} with something that is not JSON "
            f"({e}); it began {head!r}") from e


STUDIO_STALL_SECONDS = float(os.environ.get("LOOM_STUDIO_STALL_S", "300"))
STUDIO_LOAD_SECONDS = float(os.environ.get("LOOM_STUDIO_LOAD_S", "600"))


RECOVER_TIMEOUT_S = float(os.environ.get("LOOM_RECOVER_TIMEOUT_S", "5"))


def _studio_get(path: str, timeout: float = 60):
    if _STUDIO_URL is None:
        return _local_runner.get(path)
    with urllib.request.urlopen(_STUDIO_URL + path, timeout=timeout) as r:
        raw = r.read()
        ctype = r.headers.get("Content-Type", "")
        if "json" not in ctype:
            return raw.decode("utf-8")
        try:
            return json.loads(raw)
        except ValueError as e:
            head = raw[:80].decode("utf-8", "replace").replace(chr(10), " ")
            raise GenerationFailed(
                f"the model source answered {path} with something that is not "
                f"JSON ({e}); it began {head!r}") from e


def _seal_refused(job: dict, why: str) -> None:
    _seal_request(job, "refused", error=why)


class GenerationFailed(Exception):
    pass


def _run_one_generation(job: dict, prompt: str, seed):
    live = _get_job(job["id"])
    if live and live.get("cancel"):
        raise RuntimeError("cancelled")
    body = {
        "summary_source": f"loom:{job.get('corr') or '?'}#{_sample_no(job):02d}",
        "summary": "", "prompt": prompt, "model_keys": [job["gen"]["model"]],
        "samples": 1, "max_tokens": job["gen"]["max_tokens"],
        **({"temperature": job["gen"]["temperature"]}
           if job["gen"].get("temperature") is not None else {}),
        **({"seed": seed} if seed is not None else {}), "blind": False,
        **_sampling_for_job(job, (job.get("inputs") or {}).get("structured"))}
    sub = _studio_post("/api/jobs/write", body)
    sid = sub["job_id"]
    _trace("studio.submitted", loom_job=job["id"], studio_job=sid,
           chars=len(prompt), model=job["gen"].get("model"))
    _h = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    _req_write(job.get("project"), job.get("corr"),
               f"samples/{_sample_no(job):02d}/call.json", {
                   "corr": job.get("corr"),
                   "sample": _sample_no(job),
                   "at": round(time.time(), 3),
                   "endpoint": (f"POST {_STUDIO_URL}/api/jobs/write"
                                if _STUDIO_URL else
                                "runner.local.post /api/jobs/write"),
                   "body": {**body,
                            "prompt": f"<elided - the verbatim bytes are "
                                      f"prompts/{_h[:12]}.txt>"},
                   "prompt_ref": f"prompts/{_h[:12]}.txt",
                   "prompt_sha256": _h,
                   "prompt_chars": len(prompt),
                   "response": {"job_id": sid}})
    with _JOBS_LOCK:
        doc = _load_jobs()
        for j in doc["jobs"]:
            if j["id"] == job["id"]:
                j.setdefault("studio_job_ids", []).append(sid)
                _save_jobs(doc)
    _last_seen, _last_change = None, time.time()
    while True:
        live = _get_job(job["id"])
        if live and live.get("cancel"):
            try:
                _studio_post(f"/api/jobs/{sid}/cancel", {})
            except Exception:
                pass
            raise RuntimeError("cancelled")
        st = _studio_get(f"/api/jobs/{sid}")
        if st["status"] in ("done", "failed", "cancelled"):
            v = (st.get("variants") or [{}])[0]
            m = v.get("meta") or {}
            if st["status"] == "failed" or v.get("status") == "failed":
                raise GenerationFailed(v.get("error") or st.get("error")
                                       or "story-studio reported the job as failed")
            text = ""
            if v.get("status") == "done":
                try:
                    text = _studio_get(f"/api/jobs/{sid}/variants/{v['label']}/output")
                except Exception:
                    text = ""
            if st["status"] == "done" and not (text or "").strip():
                raise GenerationFailed("the model returned nothing"
                                   + (f" ({m.get('finish_reason')})"
                                      if m.get("finish_reason") else ""))
            _trace("studio.finished", corr=job.get("corr"), studio_job=sid,
                   status=st["status"],
                   finish=m.get("finish_reason"), tokens=m.get("token_count"),
                   tok_s=m.get("tokens_per_second"), out_chars=len(text or ""))
            text, thinking = strip_think(text)
            if thinking:
                _req_write(job.get("project"), job.get("corr"),
                           f"samples/{_sample_no(job):02d}/thinking.md",
                           thinking, mode="text")
            _req_write(job.get("project"), job.get("corr"),
                       f"samples/{_sample_no(job):02d}/output.md",
                       text or "", mode="text")
            _req_write(job.get("project"), job.get("corr"),
                       f"samples/{_sample_no(job):02d}/meta.json", {
                           "corr": job.get("corr"),
                           "sample": _sample_no(job),
                           "studio_job_id": sid,
                           "model_key": v.get("model_key"),
                           "seed": m.get("seed"),
                           "temperature": m.get("temperature"),
                           "finish_reason": m.get("finish_reason"),
                           "token_count": m.get("token_count"),
                           "tokens_per_second": m.get("tokens_per_second"),
                           "chars": len(text or ""),
                           "variant_label": v.get("label"),
                           "prompt_sha256": hashlib.sha256(
                               prompt.encode("utf-8")).hexdigest(),
                           "payload_sha256": ((st.get("request") or {}) or {})
                                              .get("payload_sha256"),
                           "run_dir": f"data/runs/{sid}"})
            _req_event(job, "sample_done",
                       sample=_sample_no(job), studio_job=sid,
                       tokens=m.get("token_count"),
                       finish=m.get("finish_reason"))
            ours = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            theirs = ((st.get("request") or {}) or {}).get("payload_sha256")
            match = None if not theirs else (theirs == ours)
            if match is False:
                _trace("studio.payload_mismatch", corr=job.get("corr"),
                       studio_job=sid, ours=ours[:12], theirs=str(theirs)[:12])
                _req_event(job, "payload_mismatch", studio_job=sid,
                           ours=ours[:12], theirs=str(theirs)[:12])
            return {"text": text, "seed": m.get("seed"),
                    "prompt_sha256": ours,
                    "payload_sha256": theirs,
                    "payload_match": match,
                    "variant_label": v.get("label"),
                    "finish_reason": m.get("finish_reason"),
                    "tokens_per_second": m.get("tokens_per_second"),
                    "token_count": m.get("token_count"),
                    "temperature": m.get("temperature"),
                    "model_key": v.get("model_key"),
                    "error": v.get("error"), "studio_job_id": sid,
                    "corr": job.get("corr"),
                    "sample": _sample_no(job)}
        v = (st.get("variants") or [{}])[0]
        vp = v.get("progress") or {}
        gen = job.get("gen") or {}
        n = max(1, int(gen.get("n") or 1))
        budget = int(gen.get("max_tokens") or 0) * n

        seen = (st.get("status"), vp.get("stage"), vp.get("tokens"),
                v.get("status"))
        loading = False
        if not vp:
            try:
                rn = (_studio_get("/api/status") or {}).get("runner") or {}
                loading = not rn.get("loaded")
            except Exception:
                loading = True
        deadline = STUDIO_LOAD_SECONDS if loading else STUDIO_STALL_SECONDS
        if seen != _last_seen:
            _last_seen, _last_change = seen, time.time()
        elif time.time() - _last_change > deadline:
            _trace("studio.stalled", corr=job.get("corr"), studio_job=sid,
                   status=st.get("status"), stage=vp.get("stage"),
                   tokens=vp.get("tokens"),
                   seconds=int(time.time() - _last_change))
            _req_event(job, "studio_stalled", studio_job=sid,
                       status=st.get("status"), stage=vp.get("stage"),
                       seconds=int(time.time() - _last_change))
            try:
                _studio_post(f"/api/jobs/{sid}/cancel", {})
            except Exception:
                pass
            raise GenerationFailed(
                f"story-studio reported no progress for "
                f"{int(time.time() - _last_change)}s (status "
                f"{st.get('status')!r}, stage {vp.get('stage')!r}) - giving up "
                f"on studio job {sid} (cancel sent). The run is under "
                f"data/runs/{sid}.")
        _mutate(job["id"], progress={
            "at": _last_change,
            "studio": st["status"],
            "loading": bool(loading),
            "waited": int(time.time() - _last_change),
            "stage": vp.get("stage") or ("loading the model" if loading else None),
            "tokens": vp.get("tokens"),
            "tok_s": vp.get("tok_s"),
            "elapsed": vp.get("elapsed"),
            "spent": int(job.get("_spent") or 0),
            "budget": budget,
            "variant": int(job.get("_variant") or 1), "of": n})
        time.sleep(1.0)




SCALAR_KEYS = {
    "n", "target_part", "target_words", "feedback_window", "seed", "canon_words",
    "target_paragraphs", "target_paragraphs_max", "target_parts",
    "transition_paras",
    "craft_lever", "craft_mode", "craft_words",
    "temperature", "max_tokens", "model", "kind", "steer",
    "plan_mode",
    "target_index", "target_sentences",
    "stage",
    "doctrine_off",
    "aspect", "chunk_no", "chunk_of",
    "batch_no", "batch_of",
    "count",
    "dry_multiplier", "dry_base", "dry_allowed_length",
    "part_in_chapter",
    "feedback_cap",
    "subject_kind",
}


def _sampling_for_job(job: dict, st: dict) -> dict:
    policy = E.sampling_for(job.get("kind"), st or {})
    gen = job.get("gen") or {}
    keys = list(policy) + sorted(k for k in gen
                                 if str(k).startswith("dry_") and k not in policy)
    out = {}
    for k in keys:
        v = gen.get(k, policy.get(k))
        if v is not None:
            out[k] = v
    return out


def _text_store(project: str) -> "E.TextStore":
    return E.TextStore(_project_dir(project, True) / "texts")


def _text_store_ro(project: str) -> "E.TextStore":
    return E.TextStore(_project_dir(project) / "texts")


def freeze_inputs(job: dict, structured: dict) -> dict:
    st = structured or {}
    project = job.get("project")
    corr = job.get("corr")
    chid = (job.get("target") or {}).get("chapter") or "x"
    pid = (job.get("target") or {}).get("part") or "x"
    store = _text_store(project)
    out, scalars, pools = {}, {}, []

    def put(path: str, slot: str, text, parse: str = None) -> None:
        body = E.norm_text(text)
        if not body:
            return
        r = store.put(slot, body, corr)
        rec = {"ref": r["ref"], "sha256": r["sha256"],
               "bytes": len(body.encode("utf-8"))}
        if parse:
            rec["parse"] = parse
        out[path] = rec

    ch = st.get("chapter") or {}
    put("chapter.summary", E.slot_for("ch", chid, "summary"), ch.get("summary"))
    put("chapter.title", E.slot_for("ch", chid, "title"), ch.get("title"))
    for k, v in (ch.get("facets") or {}).items():
        put(f"chapter.facets.{k}", E.slot_for("ch", chid, "facet", k), v)

    pt = st.get("part") or {}
    put("part.notes", E.slot_for("pt", pid, "notes"), pt.get("notes"))
    put("part.plan", E.slot_for("pt", pid, "plan"), pt.get("plan"))
    put("part.title", E.slot_for("drv", "parttitle", pid), pt.get("title"))

    for k, v in (st.get("instructions") or {}).items():
        put(f"instructions.{k}", E.slot_for("sty", "instructions", k), v)
    for k, v in (st.get("style") or {}).items():
        put(f"style.{k}", E.slot_for("sty", "style", k), v)

    put("canon", E.slot_for("drv", "canon", chid, pid), st.get("canon"))
    put("canon_before", E.slot_for("drv", "canonbefore", chid, pid),
        st.get("canon_before"))
    put("focus", E.slot_for("drv", "focus", chid), st.get("focus"))
    put("label", E.slot_for("drv", "label", pid), st.get("label"))
    put("instruction", E.slot_for("drv", "instruction", pid), st.get("instruction"))
    put("chapter_plot", E.slot_for("drv", "chplot", chid), st.get("chapter_plot"))
    put("current", E.slot_for("drv", "curplan", chid, pid), st.get("current"))
    put("before", E.slot_for("drv", "planbefore", chid, pid), st.get("before"))
    put("after", E.slot_for("drv", "planafter", chid, pid), st.get("after"))
    put("text", E.slot_for("drv", "text", pid), st.get("text"))
    put("title", E.slot_for("drv", "title", pid), st.get("title"))
    put("want", E.slot_for("drv", "want", pid), st.get("want"))
    put("passage", E.slot_for("drv", "passage", pid), st.get("passage"))
    put("subject", E.slot_for("drv", "subject", pid), st.get("subject"))
    put("brief", E.slot_for("drv", "brief", pid), st.get("brief"))
    put("ask", E.slot_for("drv", "studyask", pid), st.get("ask"))
    for i, f in enumerate(st.get("findings") or []):
        put(f"findings[{i}]", E.slot_for("drv", "finding", pid, i), f)
    for i, s in enumerate(st.get("subjects") or []):
        if isinstance(s, dict):
            put(f"subjects[{i}].text", E.slot_for("drv", "sgxsub", pid, i),
                s.get("text"))
            scalars[f"subjects[{i}].label"] = str(s.get("label") or "")
    for i, p in enumerate(st.get("prior") or []):
        if isinstance(p, dict):
            put(f"prior[{i}].text", E.slot_for("drv", "stageprior", pid, i),
                p.get("text"))
            scalars[f"prior[{i}].label"] = str(p.get("label") or "")
    for i, d in enumerate(st.get("dropped") or []):
        scalars[f"dropped[{i}]"] = str(d or "")[:120]
    for i, p in enumerate(st.get("proposed") or []):
        if isinstance(p, dict):
            scalars[f"proposed[{i}].name"] = str(p.get("name") or "")[:120]
            scalars[f"proposed[{i}].ask"] = str(p.get("ask") or "")[:500]
    crit = st.get("criterion")
    if isinstance(crit, dict):
        put("criterion.ask", E.slot_for("drv", "critask", pid),
            crit.get("ask"))
        put("criterion.evidence", E.slot_for("drv", "critev", pid),
            crit.get("evidence"))
        for ck, cv in crit.items():
            if ck not in ("ask", "evidence"):
                scalars[f"criterion.{ck}"] = cv
    elif "criterion" in st:
        scalars["criterion"] = crit

    for i, e in enumerate(st.get("part_summaries") or []):
        if not isinstance(e, dict):
            continue
        spid = e.get("pid") or e.get("part") or i
        block = "\n".join(f"{h}: {e.get(k, '')}" for k, h in E.SUMMARY_FIELDS)
        put(f"part_summaries[{i}]", E.slot_for("pt", spid, "summary"), block,
            parse="part_summary")
        put(f"part_summaries[{i}].title",
            E.slot_for("pt", spid, "summarytitle"), e.get("title"))
        scalars[f"part_summaries[{i}].part"] = e.get("part")
        if isinstance(e.get("covers"), list) and e["covers"]:
            scalars[f"part_summaries[{i}].covers"] = [str(x) for x in e["covers"]]

    for i, f in enumerate(st.get("feedback") or []):
        if not isinstance(f, dict):
            continue
        fpid = f.get("pid") or f.get("part") or i
        para = f.get("para") if f.get("para") is not None else i
        ch = f.get("channel") or "story"
        put(f"feedback[{i}].comment",
            E.slot_for("drv", "fbcomment", fpid, para, ch), f.get("comment"))
        put(f"feedback[{i}].paragraph",
            E.slot_for("drv", "fbquote", fpid, para), f.get("paragraph"))
        scalars[f"feedback[{i}].vote"] = f.get("vote")
        scalars[f"feedback[{i}].channel"] = f.get("channel")

    for i, sr in enumerate(st.get("steers") or []):
        put(f"steers[{i}]", E.slot_for("drv", "steer", pid, i), sr)

    for i, para in enumerate(st.get("paragraphs") or []):
        put(f"paragraphs[{i}]", E.slot_for("drv", "para", pid, i), para)

    for i, cp in enumerate(st.get("canon_parts") or []):
        if not isinstance(cp, dict):
            continue
        cpid = str(cp.get("pid") or f"i{i}")
        put(f"canon_parts[{i}].text", E.slot_for("drv", "canonpart", cpid),
            cp.get("text"))
        scalars[f"canon_parts[{i}].pid"] = cp.get("pid")
        scalars[f"canon_parts[{i}].part"] = cp.get("part")

    for i, src in enumerate(st.get("sources") or []):
        if not isinstance(src, dict):
            continue
        sid = src.get("id") or src.get("label") or i
        put(f"sources[{i}].text", E.slot_for("drv", "source", sid), src.get("text"))
        put(f"sources[{i}].label", E.slot_for("drv", "sourcelabel", sid),
            src.get("label"))

    for i, e in enumerate(st.get("project_context") or []):
        if not isinstance(e, dict):
            continue
        body = E.norm_text(e.get("text"))
        pools.append({"id": e.get("id"), "ref": f"pool:{e.get('id')}@v{e.get('version') or 0}",
                      "sha256": E.text_sha(body), "bytes": len(body.encode("utf-8")),
                      "label": e.get("title") or e.get("label"),
                      "role": e.get("role"), "always": e.get("always"),
                      "scope": e.get("scope") or "both", "order": i})

    for bi, blk in enumerate(st.get("ledgers") or []):
        if not isinstance(blk, dict):
            continue
        cat = blk.get("cat") or f"cat{bi}"
        put(f"ledgers[{bi}].instruction",
            E.slot_for("led", cat, "instruction"), blk.get("instruction"))
        scalars[f"ledgers[{bi}].cat"] = cat
        for gi, grp in enumerate(blk.get("groups") or []):
            if not isinstance(grp, dict):
                continue
            scalars[f"ledgers[{bi}].groups[{gi}].label"] = grp.get("label")
            for ei, ent in enumerate(grp.get("entries") or []):
                if not isinstance(ent, dict):
                    continue
                put(f"ledgers[{bi}].groups[{gi}].entries[{ei}]",
                    E.slot_for("led", cat, str(ent.get("id") or f"{gi}-{ei}")),
                    ent.get("text"))

    for which, d in (st.get("doctrines") or {}).items():
        if isinstance(d, dict):
            put(f"doctrines.{which}", E.slot_for("doc", str(which)),
                d.get("text"))
            scalars[f"doctrines.{which}.version"] = d.get("version")

    if E is not None:
        a = E.ask_for_request(job.get("kind"), st)
        if a:
            scalars["ask.name"] = a["name"]
            scalars["ask.version"] = a["version"]
            scalars["ask.source"] = a["source"]

    craft = st.get("craft")
    if isinstance(craft, dict):
        for f in ("lever", "mode", "seam", "label", "craft_words"):
            scalars[f"craft.{f}"] = craft.get(f)
        if craft.get("instruction"):
            put("craft.instruction", E.slot_for("crf", "rule"),
                craft.get("instruction"))
        for i, ex in enumerate(craft.get("exemplars") or [], 1):
            put(f"craft.exemplars.{i}", E.slot_for("crf", f"ex{i}"), ex)
        names = craft.get("exemplar_names") or []
        scalars["craft.exemplar_names"] = ",".join(str(n) for n in names)

    fw = st.get("feedback_windows")
    if isinstance(fw, dict):
        scalars["feedback_windows.story"] = fw.get("story")
        scalars["feedback_windows.style"] = fw.get("style")
    elif "feedback_windows" in st:
        scalars["feedback_windows"] = fw

    for k, v in st.items():
        if k in ("chapter", "part", "instructions", "style", "canon", "canon_before",
                 "focus", "label", "instruction", "text", "title", "part_summaries",
                 "chapter_plot", "current", "before", "after",
                 "feedback", "steers", "paragraphs", "canon_parts",
                 "entries",
                 "project_context",
                 "asks",
                 "ask_draft",
                 "sources", "want", "ledgers", "doctrines", "feedback_windows",
                 "criterion", "passage", "subject", "brief", "findings",
                 "ask", "subjects", "dropped", "proposed", "prior",
                 "craft"):
            continue
        if k in SCALAR_KEYS or isinstance(v, (int, float, bool)) or v is None:
            scalars[k] = v
            continue
        raise ValueError(f"unclassified input key: {k}")

    scalars.update(_sampling_for_job(job, st))

    return {"inputs": out, "scalars": scalars, "pools": pools,
            "compiler": job.get("kind"),
            "frozen_at": round(time.time(), 3),
            "text_count": len(out)}



PROSE_KINDS = ("write_part", "replace_para")
PLANNING_KINDS = ("chapter_plot", "stage_plan", "part_plan")


def _extracts_dir(pid: str, create: bool = False) -> Path:
    d = (_project_dir(pid, create) if create else _project_dir(pid)) / "extracts"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def _suggestions_read(pid: str) -> dict:
    f = _extracts_dir(pid) / "suggestions.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {"at": None, "corr": None, "suggestions": []}


def _suggestions_write(pid: str, doc: dict) -> None:
    (_extracts_dir(pid, True) / "suggestions.json").write_text(
        json.dumps(doc, indent=1), encoding="utf-8")


TOPICS_FILENAME = "topics.json"

TOPIC_ORIGINS = ("authored", "suggested", "corrected")

TOPIC_CAP_CTX_FLOOR = 16384
TOPIC_CAP_MAX_TOKENS = 700


def _topic_ask_cap() -> int:
    chunk = E.study_chunks("x" * 10 ** 6, TOPIC_CAP_CTX_FLOOR,
                           TOPIC_CAP_MAX_TOKENS)[0]
    spent = len(E.compile_study({"subject": chunk, "aspect": "custom",
                                 "label": "x" * 64,
                                 "chunk_no": 1, "chunk_of": 1}))
    room = int((TOPIC_CAP_CTX_FLOOR - TOPIC_CAP_MAX_TOKENS)
               * E.STUDY_CHARS_PER_TOKEN)
    return max(room - spent, 0)


def _topics_path(pid: str, sid: str, create: bool = False) -> Path:
    return _story_dir(pid, sid, create) / TOPICS_FILENAME


def _topics_read(pid: str, sid: str) -> dict:
    try:
        doc = json.loads(_topics_path(pid, sid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"at": None, "topics": []}
    if not isinstance(doc, dict):
        return {"at": None, "topics": []}
    rows = doc.get("topics")
    doc["topics"] = rows if isinstance(rows, list) else []
    doc.setdefault("at", None)
    return doc


def _topics_write(pid: str, sid: str, rows: list) -> dict:
    doc = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "topics": rows}
    _atomic_write(_topics_path(pid, sid, True), doc)
    return doc


def _topic_clean(row, no: int, seen: set, cap: int) -> tuple:
    where = f"topic {no}"
    if not isinstance(row, dict):
        return None, f"{where} is not a topic"
    name = str(row.get("name") or "").strip()
    ask = str(row.get("ask") or "").strip()
    if not name:
        return None, (f"{where} has no name - the name is the label, in this "
                      f"list and on every study the topic runs, so a row "
                      f"without one is a row you cannot tell from another")
    if "\n" in name or "\r" in name:
        return None, (f"{where}: a name is one line. It is joined into the "
                      f"study label, which is read as one line in the Study "
                      f"column")
    if STUDY_LABEL_SEP in name:
        return None, (f"{where}: a name may not contain {STUDY_LABEL_SEP!r} - "
                      f"the Study list splits a study's label on it to work "
                      f"out which question produced the finding")
    key = name.casefold()
    if key in seen:
        return None, (f"{where}: there is already a topic called {name!r}. "
                      f"Two topics with one name produce two sets of findings "
                      f"with one label")
    if not ask:
        return None, (f"{where} ({name}) has no ask - the ask IS the study; "
                      f"the name is only what this list calls it")
    if len(ask) > cap:
        return None, (f"{where} ({name}): the ask is {len(ask)} characters and "
                      f"{cap} is what a study request can still carry around "
                      f"the chapter it is reading. Shorten it, or split it "
                      f"into two topics")
    origin = str(row.get("origin") or "authored").strip()
    if origin not in TOPIC_ORIGINS:
        return None, (f"{where} ({name}): origin {origin!r} is not one of "
                      + ", ".join(TOPIC_ORIGINS))
    seen.add(key)
    out = {"name": name, "ask": ask, "on": bool(row.get("on", True)),
           "origin": origin}
    for k, v in row.items():
        if k not in out:
            out[k] = v
    return out, None


def topics_read(pid: str, sid: str) -> dict:
    doc = _topics_read(pid, sid)
    return {"story": sid, "at": doc.get("at"),
            "topics": doc.get("topics") or [], "ask_cap": _topic_ask_cap()}


def topics_save(pid: str, sid: str, body: dict) -> dict:
    rows = body.get("topics")
    if not isinstance(rows, list):
        return {"error": "topics must be a list"}
    cap = _topic_ask_cap()
    out, seen = [], set()
    for i, row in enumerate(rows, start=1):
        clean, why = _topic_clean(row, i, seen, cap)
        if why:
            return {"error": why, "row": i}
        out.append(clean)
    doc = _topics_write(pid, sid, out)
    return {"ok": True, "story": sid, "at": doc["at"], "topics": out,
            "ask_cap": cap}


_SGX_LOCK = threading.RLock()


def _sgx_runs_dir(pid: str, create: bool = False) -> Path:
    d = _extracts_dir(pid, create) / "runs"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def _sgx_run_read(pid: str, run_id: str) -> dict:
    try:
        return json.loads((_sgx_runs_dir(pid) / f"{run_id}.json")
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _sgx_run_write(pid: str, doc: dict) -> None:
    _sgx_runs_dir(pid, create=True)
    _atomic_write(_sgx_runs_dir(pid) / f"{doc['id']}.json", doc)


def _sgx_name_key(name) -> str:
    return " ".join(str(name or "").split()).lower()


def _sgx_standing_names(pid: str) -> set:
    return {_sgx_name_key(x.get("name"))
            for x in (_suggestions_read(pid).get("suggestions") or [])
            if x.get("state") in ("dropped", "checked")}


def _sgx_merge_facts(doc: dict) -> dict:
    log = doc.get("proposal_log") or []
    merged = doc.get("merged") or []
    return {
        "proposed": len(log),
        "readings": sorted({n for n in (int(r.get("batch") or 0) for r in log)
                            if n > 0}),
        "questions": len(merged),
        "unlogged": list(doc.get("merge_unlogged") or []),
        "rows": {_sgx_name_key(g.get("name")): {
            "origin": "reading" if g.get("batches") else "unnamed",
            "batches": list(g.get("batches") or []),
            "chapters": list(g.get("chapters") or []),
            "agreement_rank": g.get("agreement_rank"),
            "model_rank": g.get("model_rank"),
        } for g in merged},
    }


def _sgx_write_suggestions(pid: str, proposed: list, corr: str,
                           story=None, partial: bool = False,
                           unread: list = None, merge: dict = None) -> tuple:
    prev = _suggestions_read(pid)
    keep = [x for x in prev.get("suggestions") or []
            if x.get("state") in ("dropped", "checked")]
    keep_names = {str(x.get("name") or "").strip().lower() for x in keep}
    facts = dict(merge or {})
    prov = facts.pop("rows", None) or {}
    fresh = []
    for i, sg in enumerate(proposed):
        if str(sg.get("name") or "").strip().lower() in keep_names:
            continue
        row = {"id": f"sx-{int(time.time())}-{i}",
               "name": sg.get("name"), "ask": sg.get("ask"),
               "state": "open"}
        if merge is not None:
            row["from"] = dict(prov.get(_sgx_name_key(sg.get("name")))
                               or {"origin": "unaccounted"})
        fresh.append(row)
    kept = [{k: v for k, v in x.items() if k != "from"} for x in keep]
    if merge is not None:
        for x in kept:
            x["from"] = {"origin": "earlier"}
    out = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "corr": corr,
           "story": story, "partial": bool(partial),
           "unread": list(unread or []), "suggestions": fresh + kept}
    if merge is not None:
        out["merge"] = facts
    _suggestions_write(pid, out)
    return len(fresh), len(kept)


def _sgx_unlogged(doc: dict, no: int) -> tuple:
    prev = doc.get("merged")
    if prev is not None:
        return ([{"name": g.get("name"), "ask": g.get("ask")}
                 for g in prev if not g.get("batches")],
                list(doc.get("merge_unlogged") or []))
    if "proposal_log" in doc:
        return [], []
    labels = doc.get("labels") or []
    lost = [int(b.get("no") or 0) for b in (doc.get("batches") or [])
            if "parsed" in b and b.get("no") != no]
    return (list(doc.get("proposed") or []),
            [lab for n in lost if 0 < n <= len(labels)
             for lab in labels[n - 1]])


def _sgx_merge_rank(log: list, count: int, standing: set,
                    carried: list = None) -> tuple:
    standing_keys = {_sgx_name_key(n) for n in (standing or ())}
    by_key, order = {}, []

    def group(name, ask):
        key = _sgx_name_key(name)
        if not key or key in standing_keys:
            return None
        g = by_key.get(key)
        if g is None:
            g = by_key[key] = {"name": str(name or "").strip(),
                               "ask": str(ask or ""),
                               "batches": [], "chapters": []}
            order.append(key)
        return g

    for r in log or []:
        g = group((r or {}).get("name"), (r or {}).get("ask"))
        if g is None:
            continue
        no = int(r.get("batch") or 0)
        rank = int(r.get("rank") or 0) or 10 ** 6
        if no not in g["batches"]:
            g["batches"].append(no)
        for lab in (r.get("chapters") or []):
            if lab not in g["chapters"]:
                g["chapters"].append(lab)
        g["best"] = min(g.get("best") or (rank, no), (rank, no))

    for i, r in enumerate(carried or [], start=1):
        g = group((r or {}).get("name"), (r or {}).get("ask"))
        if g is not None and not g["batches"] and "best" not in g:
            g["best"] = (i, 0)

    rows = [by_key[k] for k in order]
    named = (lambda g: (0 if g["batches"] else 1, ))
    for i, g in enumerate(sorted(rows, key=lambda g: named(g) + g["best"]),
                          start=1):
        g["model_rank"] = i
    out, delivered = [], []
    for i, g in enumerate(sorted(rows, key=lambda g: named(g)
                                 + (-len(g["batches"]), ) + g["best"]),
                          start=1):
        out.append({
            "name": g["name"], "ask": g["ask"],
            "batches": sorted(g["batches"]), "chapters": g["chapters"],
            "agreement": len(g["batches"]),
            "best_rank": g["best"][0] if g["batches"] else None,
            "agreement_rank": i, "model_rank": g["model_rank"],
            "delivered": i <= count})
        if i <= count:
            delivered.append({"name": g["name"], "ask": g["ask"]})
    return out, delivered


def _sgx_record_proposals(doc: dict, no: int, parsed: list) -> list:
    labels = doc.get("labels") or []
    chapters = list(labels[no - 1]) if 0 < no <= len(labels) else []
    rows, seen = [], set()
    for p in parsed or []:
        name = str((p or {}).get("name") or "").strip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        rows.append({"name": name, "ask": str(p.get("ask") or ""),
                     "batch": int(no), "chapters": chapters,
                     "rank": len(rows) + 1})
    log = [r for r in (doc.get("proposal_log") or [])
           if int(r.get("batch") or 0) != int(no)]
    doc["proposal_log"] = log + rows
    return rows


def _sgx_submit_batch(pid: str, doc: dict, no: int) -> dict:
    jcorr = _mint_corr("suggest_extracts")
    of = len(doc.get("batch_texts") or [])
    sub = jobs_submit({
        "kind": "suggest_extracts", "project": pid, "story": doc.get("story"),
        "corr": jcorr,
        "origin": {"where": "extracts",
                   "at": time.strftime("%Y-%m-%d %H:%M:%S")},
        "landing": {"where": "extracts", "applies": [], "run": doc["id"]},
        "target": {"chapter": "datamine", "part": doc["id"]},
        "inputs": {"structured": {
            "subjects": doc["batch_texts"][no - 1],
            "count": doc["count"], "dropped": doc.get("dropped") or [],
            "batch_no": no, "batch_of": of},
            "pools": []},
        "gen": doc["gen"], "priority": "batch"})
    return {"no": no, "jcorr": jcorr, "job_id": sub.get("job_id"),
            "carried": 0}


def _sgx_on_complete(job: dict) -> None:
    text = _study_output(_project_id(job.get("project")), job.get("corr"))
    if not str(text).strip():
        res = job.get("result") or {}
        for c in (res.get("candidates") or []):
            text = str((c or {}).get("text") or "")
            if text.strip():
                break
    parsed = E.parse_extract_suggestions(text)
    pid = _project_id(job.get("project"))
    run_id = str((job.get("landing") or {}).get("run") or "")
    if not run_id:
        fresh, keep = _sgx_write_suggestions(pid, parsed, job.get("corr"),
                                             job.get("story"))
        _trace("sgx.landed", corr=job.get("corr"), fresh=fresh,
               standing_kept=keep)
        return

    with _SGX_LOCK:
        doc = _sgx_run_read(pid, run_id)
        if not doc or doc.get("status") != "running":
            _trace("sgx.run.stale", corr=job.get("corr"), run=run_id,
                   status=(doc or {}).get("status"))
            return
        row = next((b for b in doc.get("batches") or []
                    if b.get("jcorr") == job.get("corr")), None)
        if row is None:
            _trace("sgx.run.unknown_batch", corr=job.get("corr"), run=run_id)
            return
        no, of = row["no"], len(doc["batch_texts"])
        row["parsed"] = len(parsed)
        orphans, unnamed = _sgx_unlogged(doc, no)
        _sgx_record_proposals(doc, no, parsed)
        if not parsed:
            _trace("sgx.batch.unreadable", corr=job.get("corr"), run=run_id,
                   batch=no)
        doc["merge_unlogged"] = unnamed
        doc["merged"], doc["proposed"] = _sgx_merge_rank(
            doc.get("proposal_log") or [], int(doc.get("count") or 10),
            _sgx_standing_names(pid), orphans)
        _trace("sgx.merge", run=run_id, batch=no,
               readings=len({int(r.get("batch") or 0)
                             for r in doc["proposal_log"]}),
               proposals=len(doc["proposal_log"]),
               merged=len(doc["merged"]),
               agreement={n: sum(1 for g in doc["merged"]
                                 if len(g.get("batches") or []) == n)
                          for n in sorted({len(g.get("batches") or [])
                                           for g in doc["merged"]})},
               top=[g["name"] for g in doc["merged"][:3]],
               unlogged=unnamed)
        row["kept"] = len(doc.get("proposed") or [])
        if no < of:
            doc["batches"].append(_sgx_submit_batch(pid, doc, no + 1))
        else:
            fresh, keep = _sgx_write_suggestions(
                pid, doc.get("proposed") or [], doc["batches"][0]["jcorr"],
                doc.get("story"), merge=_sgx_merge_facts(doc))
            doc["status"] = "done"
            doc["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _trace("sgx.landed", corr=job.get("corr"), run=run_id,
                   batches=of, fresh=fresh, standing_kept=keep)
        _sgx_run_write(pid, doc)


def _sgx_mark_partial(pid: str, run_id: str, corr: str,
                      why: str) -> bool:
    with _SGX_LOCK:
        doc = _sgx_run_read(pid, run_id)
        if not doc or doc.get("status") != "running":
            return False
        row = next((b for b in doc.get("batches") or []
                    if b.get("jcorr") == corr), None)
        if row is None:
            _trace("sgx.run.unknown_batch", corr=corr, run=run_id)
            return False
        if "parsed" in row or "failed" in row:
            return False
        row["failed"] = why
        doc["status"] = "partial"
        doc["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        doc["refused"] = {"batch": row.get("no"), "why": why,
                          "at": doc["finished"]}
        card = _sgx_run_card(doc)
        fresh, keep = _sgx_write_suggestions(
            pid, doc.get("proposed") or [], doc["batches"][0]["jcorr"],
            doc.get("story"), partial=True, unread=card.get("unread") or [],
            merge=_sgx_merge_facts(doc))
        _sgx_run_write(pid, doc)
        _trace("sgx.run.partial", run=run_id, batch=row.get("no"),
               read=card.get("read"), fresh=fresh, why=str(why)[:120])
        return True


def _sgx_sweep(project: str = None) -> int:
    want = _project_id(project) if project else None
    dead = []
    try:
        with _JOBS_LOCK:
            for j in _load_jobs().get("jobs") or []:
                if j.get("kind") != "suggest_extracts":
                    continue
                if j.get("status") not in ("failed", "cancelled"):
                    continue
                run_id = str((j.get("landing") or {}).get("run") or "")
                if not run_id:
                    continue
                pid = _project_id(j.get("project"))
                if want and pid != want:
                    continue
                why = str(j.get("error") or "").strip() or (
                    "the batch was cancelled"
                    if j.get("status") == "cancelled" else "the batch failed")
                dead.append((pid, run_id, j.get("corr"), why[:300]))
    except Exception:
        return 0
    return sum(1 for d in dead if _sgx_mark_partial(*d))


def _judgements_dir(pid: str, create: bool = False) -> Path:
    d = (_project_dir(pid, create) if create else _project_dir(pid)) / "judgements"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def rubric_for(pid: str, story: str = None) -> tuple:
    candidates = []
    try:
        proj = _project_dir(pid)
    except Exception:
        proj = None
    if proj and story:
        candidates.append((proj / "stories" / story / DOCTRINE_DIRNAME
                           / "rubric.md", f"story:{story}"))
    if proj:
        candidates.append((proj / DOCTRINE_DIRNAME / "rubric.md", "project"))
    candidates.append((_doctrine_seed_dir() / "rubric.md", "shipped-default"))
    for f, src in candidates:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        if text.strip():
            return text, (E.doctrine_version(text) if E else ""), src
    return "", "", "none"


def _tests_calibration(pid: str) -> dict:
    try:
        return json.loads((_judgements_dir(pid) / "calibration.json")
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def tests_rubric(pid: str, story: str = None) -> dict:
    text, version, source = rubric_for(pid, story)
    parsed = E.parse_rubric(text) if (E and text) else {"criteria": []}
    cal = _tests_calibration(pid)
    results = cal.get("results") or {}
    flags = {}
    if E:
        docs, _src = doctrine_for(pid, story)
        for which, d in docs.items():
            for line in E.parse_doctrine(d.get("text") or "")["lines"]:
                flags[line["id"]] = line.get("flag")
    out = []
    for c in parsed["criteria"]:
        r = results.get(c["id"]) or {}
        if flags.get(c["serves"]) == "ADVISORY":
            status = "ADVISORY-ONLY"
        elif r.get("bad_failed") is True and r.get("good_passed") is True:
            status = "LIVE"
        else:
            status = "UNCALIBRATED"
        out.append({"id": c["id"], "serves": c["serves"],
                    "kinds": list(c["kinds"]), "ask": c["ask"],
                    "evidence": c["evidence"], "status": status})
    return {"source": source, "version": version, "criteria": out,
            "calibrated_at": cal.get("at"),
            "calibration_model": cal.get("model")}


def _corpus_file(pid: str) -> Path:
    return _judgements_dir(pid) / "corpus.json"


def _corpus_read(pid: str) -> dict:
    try:
        return json.loads(_corpus_file(pid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"pinned": []}


def _req_kind(pid: str, corr: str) -> str:
    try:
        man = json.loads((_req_dir(pid, corr) / "manifest.json")
                         .read_text(encoding="utf-8"))
        if man.get("compiler"):
            return str(man["compiler"])
    except (OSError, ValueError):
        pass
    bits = corr.split("-")
    code = bits[3] if len(bits) >= 5 else ""
    return {v: k for k, v in CORR_KIND.items()}.get(code, "")


def strip_think(text: str) -> tuple:
    t = str(text or "")
    if "<think>" not in t:
        return t, None
    pre, _, rest = t.partition("<think>")
    inner, closed, post = rest.partition("</think>")
    if not closed:
        return pre.strip(), inner.strip() or None
    return (pre + post).strip(), inner.strip() or None


def _request_output(pid: str, corr: str) -> str:
    try:
        return (_req_dir(pid, corr) / "samples" / "01" / "output.md"
                ).read_text(encoding="utf-8")
    except OSError:
        return ""


def _recent_generations(pid: str, limit: int = 20) -> list:
    base = _project_dir(pid) / "requests"
    rows = []
    try:
        months = sorted((d for d in base.iterdir() if d.is_dir()),
                        reverse=True)
    except OSError:
        return []
    for month in months:
        try:
            folders = sorted((d for d in month.iterdir() if d.is_dir()),
                             reverse=True)
        except OSError:
            continue
        for f in folders:
            if len(rows) >= limit:
                return rows
            kind = _req_kind(pid, f.name)
            if kind not in PROSE_KINDS:
                continue
            out = _request_output(pid, f.name)
            if not out.strip():
                continue
            rows.append({"corr": f.name, "kind": kind,
                         "label": f"{kind} · …{f.name[-8:]}",
                         "words": len(out.split()),
                         "sha256": E.text_sha(out) if E else ""})
    return rows


def tests_corpus(pid: str) -> dict:
    return {"pinned": _corpus_read(pid).get("pinned") or [],
            "recent": _recent_generations(pid)}


def tests_pin(pid: str, corr: str, pin: bool) -> dict:
    doc = _corpus_read(pid)
    pinned = [p for p in (doc.get("pinned") or []) if p.get("corr") != corr]
    if pin:
        out = _request_output(pid, corr)
        if not out.strip():
            return {"error": f"no output on record for {corr}"}
        kind = _req_kind(pid, corr)
        pinned.append({"corr": corr, "kind": kind,
                       "label": f"{kind} · …{corr[-8:]}",
                       "sha256": E.text_sha(out) if E else "",
                       "words": len(out.split()),
                       "pinned_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    doc["pinned"] = pinned
    _judgements_dir(pid, create=True)
    _atomic_write(_corpus_file(pid), doc)
    return {"pinned": pinned}


def _runs_dir(pid: str, create: bool = False) -> Path:
    d = _judgements_dir(pid, create) / "runs"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def _mint_judge_corr() -> str:
    tail = "".join(random.choice(CORR_ALPHABET) for _ in range(4))
    return time.strftime("r-%Y%m%d-%H%M%S-jdg-") + tail


def tests_run_start(pid: str, story: str, gen: dict) -> dict:
    rub = tests_rubric(pid, story)
    if not rub["criteria"]:
        return {"error": "no rubric in force - nothing can judge"}
    pinned = _corpus_read(pid).get("pinned") or []
    if not pinned:
        return {"error": "the pinned corpus is empty - pin a passage first"}

    gen = gen or {}
    genome = {"model": gen.get("model") or _default_model(),
              "temperature": 0,
              "max_tokens": int(gen.get("max_tokens") or 160),
              "n": 3, "seed": 7}
    run_id = time.strftime("run-%Y%m%d-%H%M%S-") + "".join(
        random.choice(CORR_ALPHABET) for _ in range(4))
    cells, changed = [], []
    for p in pinned:
        out = _request_output(pid, p["corr"])
        if E and p.get("sha256") and E.text_sha(out) != p["sha256"]:
            changed.append(p["corr"])
            continue
        for c in rub["criteria"]:
            if c["kinds"] and p.get("kind") not in c["kinds"]:
                cells.append({"crit": c["id"], "corr": p["corr"],
                              "applicable": False})
                continue
            jcorr = _mint_judge_corr()
            sub = jobs_submit({
                "kind": "judge", "project": pid, "story": story,
                "corr": jcorr,
                "origin": {"where": "tests-tab",
                           "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                "landing": {"where": "tests-run", "applies": [],
                            "run": run_id},
                "target": {"chapter": "tests", "part": p["corr"]},
                "inputs": {"structured": {
                    "criterion": {"id": c["id"], "ask": c["ask"],
                                  "evidence": c["evidence"]},
                    "passage": out}, "pools": []},
                "gen": genome, "priority": "batch"})
            cells.append({"crit": c["id"], "corr": p["corr"],
                          "applicable": True, "jcorr": jcorr,
                          "job_id": sub.get("job_id")})
    doc = {"id": run_id, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "story": story,
           "rubric_version": rub["version"],
           "rubric_source": rub["source"],
           "doctrine_versions": {},
           "criteria": [{"id": c["id"], "status": c["status"]}
                        for c in rub["criteria"]],
           "corpus": [p for p in pinned if p["corr"] not in changed],
           "excluded_changed": changed,
           "gen": genome, "cells": cells}
    if E:
        docs, dsrc = doctrine_for(pid, story)
        doc["doctrine_versions"] = {k: v.get("version")
                                    for k, v in docs.items()}
        doc["doctrine_source"] = dsrc
    _runs_dir(pid, create=True)
    f = _runs_dir(pid) / f"{run_id}.json"
    if f.exists():
        return {"error": f"run id collision: {run_id}"}
    _atomic_write(f, doc)
    _trace("tests.run", id=run_id, cells=len(cells),
           excluded=len(changed))
    return {"run": doc}


def tests_runs(pid: str) -> list:
    out = []
    try:
        files = sorted(_runs_dir(pid).glob("run-*.json"), reverse=True)
    except OSError:
        return []
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"id": d.get("id"), "at": d.get("at"),
                    "rubric_version": d.get("rubric_version"),
                    "doctrine_versions": d.get("doctrine_versions"),
                    "cells": len(d.get("cells") or [])})
    return out


def tests_run_read(pid: str, run_id: str) -> dict:
    try:
        doc = json.loads((_runs_dir(pid) / f"{run_id}.json")
                         .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"error": f"no run {run_id!r}"}
    with _JOBS_LOCK:
        jobs = {j["id"]: j for j in _load_jobs().get("jobs", [])}
    for cell in doc.get("cells") or []:
        if not cell.get("applicable"):
            continue
        v = None
        try:
            v = json.loads((_req_dir(pid, cell["jcorr"]) / "verdict.json")
                           .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        if v and (v.get("verdicts") or []):
            cell["verdict"] = v.get("agreed") or v["verdicts"][0]
        else:
            j = jobs.get(cell.get("job_id"))
            cell["pending"] = j.get("status") if j else "unknown"
    return {"run": doc}



_STUDY_LOCK = threading.RLock()

SUBJECT_KIND_DEFAULT = "prose"
SUBJECT_KIND_FEEDBACK = "feedback"

FEEDBACK_REFUSED_ASPECTS = {"plot"}


def _studies_dir(pid: str, create: bool = False) -> Path:
    d = (_project_dir(pid, create) if create else _project_dir(pid)) / "studies"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def _study_read(pid: str, study_id: str) -> dict:
    try:
        return json.loads((_studies_dir(pid) / f"{study_id}.json")
                         .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _study_write(pid: str, doc: dict) -> None:
    _studies_dir(pid, create=True)
    _atomic_write(_studies_dir(pid) / f"{doc['id']}.json", doc)


def _mint_study_corr(code: str) -> str:
    tail = "".join(random.choice(CORR_ALPHABET) for _ in range(4))
    return time.strftime(f"r-%Y%m%d-%H%M%S-{code}-") + tail


STUDY_RUN_ID_CAP = 64


def _mint_study_run() -> str:
    return time.strftime("run-%Y%m%d-%H%M%S-") + "".join(
        random.choice(CORR_ALPHABET) for _ in range(4))


def _study_submit_chunk(pid: str, doc: dict, no: int, brief: str = "") -> dict:
    chunk = doc["chunk_texts"][no - 1]
    jcorr = _mint_study_corr("stu")
    sub = jobs_submit({
        "kind": "study", "project": pid, "story": doc.get("story"),
        "corr": jcorr,
        "origin": {"where": "study", "at": time.strftime("%Y-%m-%d %H:%M:%S")},
        "landing": {"where": "study", "applies": [], "study": doc["id"]},
        "target": {"chapter": "study", "part": doc["id"]},
        "inputs": {"structured": {
            "subject": chunk, "aspect": doc["aspect"], "label": doc["label"],
            **({"ask": doc["ask"]} if doc.get("ask") else {}),
            "brief": brief or "", "chunk_no": no,
            "chunk_of": len(doc["chunk_texts"])}, "pools": []},
        "gen": doc["gen"], "priority": "batch"})
    return {"no": no, "jcorr": jcorr, "job_id": sub.get("job_id")}


def _study_submit_merge(pid: str, doc: dict, level: int, group: list,
                        findings: list) -> dict:
    jcorr = _mint_study_corr("stm")
    sub = jobs_submit({
        "kind": "study_merge", "project": pid, "story": doc.get("story"),
        "corr": jcorr,
        "origin": {"where": "study", "at": time.strftime("%Y-%m-%d %H:%M:%S")},
        "landing": {"where": "study", "applies": [], "study": doc["id"]},
        "target": {"chapter": "study", "part": doc["id"]},
        "inputs": {"structured": {
            "findings": findings, "aspect": doc["aspect"],
            **({"ask": doc["ask"]} if doc.get("ask") else {}),
            "label": doc["label"], "chunk_of": len(findings)}, "pools": []},
        "gen": doc["gen"], "priority": "batch"})
    return {"level": level, "group": group, "jcorr": jcorr,
            "job_id": sub.get("job_id")}


def study_counts(body: dict) -> dict:
    if E is None:
        return {"error": "loom_engine is not importable"}
    text = str((body or {}).get("subject") or "")
    if not text.strip():
        return {"error": "nothing to count - the subject is empty"}
    return {"block": E.counted_facts_block(text), "words": len(text.split())}


SGX_CTX_FALLBACK = 16384


def _sgx_wire_subjects(b: dict) -> tuple:
    budgets = b.get("feedback_words")
    budgets = budgets if isinstance(budgets, dict) else {}
    out, fb = [], None
    for s in (b.get("subjects") or []):
        if not isinstance(s, dict):
            continue
        kind = str(s.get("kind") or "")
        ent = s.get("entries")
        ent = ent if isinstance(ent, list) else None
        text = str(s.get("text") or "")
        if ent is not None and (kind == "feedback" or not text.strip()):
            fb = E.sgx_feedback_subject(ent, budgets)
            out.append({"id": str(s.get("id") or "fb:all"), "kind": "feedback",
                        "label": str(s.get("label") or "Feedback"),
                        "text": fb["text"]})
            continue
        out.append({"id": str(s.get("id") or ""), "kind": kind,
                    "label": str(s.get("label") or ""), "text": text})
    return out, fb


def extracts_plan(body: dict) -> dict:
    if E is None:
        return {"error": "loom_engine is not importable"}
    b = body or {}
    subs, fb = _sgx_wire_subjects(b)
    if not subs:
        return {"error": "nothing to plan - no subjects were sent"}
    gen = b.get("gen") or {}
    model = str(gen.get("model") or "") or _default_model() or ""
    ctx = _model_ctx(model)
    plan = E.sgx_batch_plan(
        subs, ctx or SGX_CTX_FALLBACK, int(gen.get("max_tokens") or 0),
        count=b.get("count") or 10, dropped=b.get("dropped") or [],
        word_budget=b.get("word_budget") or 0)
    for bat in plan.get("batches") or []:
        for d in bat.get("docs") or []:
            d.pop("text", None)
    plan["feedback"] = fb
    plan["model"] = model
    plan["ctx_source"] = "live" if ctx else "fallback"
    plan["armed"] = True
    plan["runnable"] = bool(
        plan["armed"] and not plan.get("error") and (plan.get("batches") or [])
        and all(b.get("verdict") != "refused" for b in plan["batches"]))
    return {"plan": plan}


def extracts_suggest(pid: str, body: dict) -> dict:
    if E is None:
        return {"error": "loom_engine is not importable"}
    b = body or {}
    subs, fb = _sgx_wire_subjects(b)
    if not subs:
        return {"error": "nothing to suggest from - no subjects were sent"}
    gen = b.get("gen") or {}
    model = str(gen.get("model") or "") or _default_model() or ""
    max_tokens = int(gen.get("max_tokens") or 900)
    ctx = _model_ctx(model) or SGX_CTX_FALLBACK
    plan = E.sgx_batch_plan(subs, ctx, max_tokens,
                            count=b.get("count") or 10,
                            dropped=b.get("dropped") or [],
                            word_budget=b.get("word_budget") or 0)
    if plan.get("error"):
        return {"error": plan["error"]}
    refused = [x["no"] for x in plan["batches"] if x["verdict"] == "refused"]
    if refused:
        return {"error": f"batch {refused[0]} of {len(plan['batches'])} would "
                         f"still not fit the {ctx:,}-token window - read "
                         f"fewer chapters, or raise the window"}
    doc = {
        "id": time.strftime("sgx-%Y%m%d-%H%M%S-") + "".join(
            random.choice(CORR_ALPHABET) for _ in range(4)),
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "story": b.get("story"),
        "count": plan["count"], "dropped": b.get("dropped") or [],
        "params": {"ctx_tokens": ctx, "max_tokens": max_tokens,
                   "ceiling": plan["ceiling"],
                   "budget_chars": plan["ceiling"],
                   "ctx_source": "live" if _model_ctx(model) else "fallback"},
        "batch_texts": [[{"label": d["label"], "text": d["text"]}
                         for d in x["docs"]] for x in plan["batches"]],
        "labels": [[d["label"] for d in x["docs"]] for x in plan["batches"]],
        "subjects": plan.get("subjects") or [],
        "gen": {"model": model,
                "temperature": float(gen.get("temperature") or 0.3),
                "max_tokens": max_tokens, "n": 1,
                **({"prompt_suffix": gen["prompt_suffix"]}
                   if gen.get("prompt_suffix") else {})},
        "batches": [], "proposed": [], "status": "running",
        "proposal_log": [],
        "merged": [], "merge_unlogged": [],
        "excluded": plan.get("excluded") or [], "refused": None,
        "feedback": ({k: v for k, v in fb.items() if k != "text"}
                     if fb else None),
    }
    with _SGX_LOCK:
        doc["batches"].append(_sgx_submit_batch(pid, doc, 1))
        _sgx_run_write(pid, doc)
    _trace("sgx.run.start", id=doc["id"], batches=len(doc["batch_texts"]),
           count=doc["count"], ctx=ctx, excluded=len(doc["excluded"]))
    return {"run": {k: v for k, v in doc.items() if k != "batch_texts"},
            "plan": {"batches": len(doc["batch_texts"]),
                     "count": doc["count"]}}


def _sgx_run_card(doc: dict) -> dict:
    labels = doc.get("labels") or []
    of = len(doc.get("batch_texts") or []) or len(labels)
    rows = doc.get("batches") or []
    status = str(doc.get("status") or "running")
    read_nos = {r.get("no") for r in rows if "parsed" in r}
    live = (next((r for r in reversed(rows) if "parsed" not in r), None)
            if status == "running" else None)
    live_no = live.get("no") if live else None
    subs = doc.get("subjects") or []
    read_subs = [s for s in subs if s.get("batches")
                 and all(n in read_nos for n in s["batches"])]

    def tally(rows_: list) -> dict:
        t = {}
        for s in rows_:
            k = str(s.get("kind") or "document")
            t[k] = t.get(k, 0) + 1
        return t

    unread_nos = ([n for n in range(1, of + 1)
                   if n not in read_nos and n != live_no]
                  if status != "running" else [])
    unread = [lab for n in unread_nos
              for lab in (labels[n - 1] if n - 1 < len(labels) else [])]
    return {
        "id": doc.get("id"), "at": doc.get("at"), "story": doc.get("story"),
        "status": status, "count": doc.get("count"),
        "batches": of, "read": len(read_nos),
        "batch_no": live_no,
        "reading": (labels[live_no - 1]
                    if live_no and live_no - 1 < len(labels) else []),
        "carried": int((live or {}).get("carried") or 0),
        "extracts": len(doc.get("proposed") or []),
        "by_kind": tally(read_subs), "by_kind_all": tally(subs),
        "subjects": len(subs), "subjects_read": len(read_subs),
        "unread": unread,
        "resume_at": unread_nos[0] if unread_nos else None,
        "excluded": len(doc.get("excluded") or []),
        "feedback": doc.get("feedback"),
        "finished": doc.get("finished"), "refused": doc.get("refused"),
    }


def extracts_runs(pid: str) -> list:
    out = []
    try:
        files = sorted(_sgx_runs_dir(pid).glob("sgx-*.json"), reverse=True)
    except OSError:
        return []
    for f in files:
        try:
            out.append(_sgx_run_card(
                json.loads(f.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            continue
    return out


def extracts_retry(pid: str, run_id: str) -> dict:
    with _SGX_LOCK:
        doc = _sgx_run_read(pid, run_id)
        if not doc:
            return {"error": "no such run"}
        if doc.get("status") == "running":
            return {"error": "that run is still going - nothing to resume"}
        card = _sgx_run_card(doc)
        no = card.get("resume_at")
        if not no:
            return {"error": "every batch of this run was read - there is "
                             "nothing left to resume"}
        doc["status"] = "running"
        doc["refused"] = None
        doc.pop("finished", None)
        doc["batches"].append(_sgx_submit_batch(pid, doc, no))
        _sgx_run_write(pid, doc)
    _trace("sgx.run.retry", run=run_id, batch=no, carrying=0,
           proposals=len(doc.get("proposal_log") or []))
    return {"ok": True, "run": run_id, "batch": no,
            "of": len(doc.get("batch_texts") or []),
            "carrying": 0,
            "proposals": len(doc.get("proposal_log") or [])}


def study_start(pid: str, body: dict) -> dict:
    if E is None:
        return {"error": "loom_engine is not importable"}
    subject = str(body.get("subject") or "")
    if not subject.strip():
        return {"error": "a study needs a subject - nothing was sent"}
    aspect = str(body.get("aspect") or "style")
    if aspect not in E.STUDY_ASPECTS:
        return {"error": f"unknown aspect {aspect!r} - one of "
                         + ", ".join(sorted(E.STUDY_ASPECTS))}
    kind = str(body.get("subject_kind") or SUBJECT_KIND_DEFAULT).strip().lower()
    if kind == SUBJECT_KIND_FEEDBACK and aspect in FEEDBACK_REFUSED_ASPECTS:
        ok = ", ".join(sorted(set(E.STUDY_ASPECTS) - FEEDBACK_REFUSED_ASPECTS))
        return {"error": f"the {aspect} aspect cannot study feedback - {aspect}"
                         f" runs the sequential strategy, which carries a "
                         f"rolling brief from chunk to chunk so a later chunk "
                         f"knows what the earlier ones established. A pile of "
                         f"remarks has no order to follow, so the brief would "
                         f"invent a sequence that is not there. Study feedback "
                         f"with {ok}."}
    ask = str(body.get("ask") or "").strip()
    if aspect == "custom" and not ask:
        return {"error": "a custom study needs an ask - the question that "
                         "was checked off in the datamine list"}
    params = body.get("params") or {}
    gen = body.get("gen") or {}
    max_tokens = int(gen.get("max_tokens") or 700)
    ctx = _model_ctx(gen.get("model") or _default_model()) or 16384
    chunks = E.study_chunks(subject, ctx, max_tokens,
                            budget_chars=params.get("budget_chars"))
    run = str(body.get("run") or "").strip()[:STUDY_RUN_ID_CAP] \
        or _mint_study_run()
    doc = {
        "id": time.strftime("study-%Y%m%d-%H%M%S-") + "".join(
            random.choice(CORR_ALPHABET) for _ in range(4)),
        "at": time.strftime("%Y-%m-%d %H:%M:%S"), "run": run,
        "subject_label": str(body.get("subject_label") or "").strip() or None,
        "finding": str(body.get("finding") or "").strip() or None,
        "story": body.get("story"), "label": str(body.get("label") or "?"),
        "aspect": aspect, "ask": ask if aspect == "custom" else None,
        "subject_kind": kind,
        "strategy": E.STUDY_ASPECTS[aspect]["strategy"],
        "source": body.get("source") or {},
        "params": {"ctx_tokens": ctx, "max_tokens": max_tokens,
                   "budget_chars": params.get("budget_chars"),
                   "merge_budget_chars": params.get("merge_budget_chars")},
        "gen": {"model": gen.get("model") or _default_model(),
                "temperature": float(gen.get("temperature") or 0.3),
                "max_tokens": max_tokens, "n": 1, "seed": 7,
                **({"prompt_suffix": gen["prompt_suffix"]}
                   if gen.get("prompt_suffix") else {})},
        "chunk_texts": chunks,
        "chunks": [], "merges": [], "final": None, "refused": None,
    }
    with _STUDY_LOCK:
        if doc["strategy"] == "blind":
            for no in range(1, len(chunks) + 1):
                doc["chunks"].append(_study_submit_chunk(pid, doc, no))
        else:
            doc["chunks"].append(_study_submit_chunk(pid, doc, 1))
        _study_write(pid, doc)
    _trace("study.start", id=doc["id"], run=run, aspect=aspect,
           subject_kind=kind, chunks=len(chunks), strategy=doc["strategy"])
    return {"study": doc}


def _study_output(pid: str, jcorr: str) -> str:
    return _request_output(pid, jcorr)


def _study_merge_budget(doc: dict) -> int:
    if doc["params"].get("merge_budget_chars"):
        return max(int(doc["params"]["merge_budget_chars"]), 200)
    return max(int((doc["params"]["ctx_tokens"] * E.STUDY_SAFETY
                    - doc["params"]["max_tokens"])
                   * E.STUDY_CHARS_PER_TOKEN) - 1500, 2000)


def _study_on_complete(job: dict) -> None:
    pid = job.get("project")
    study_id = ((job.get("landing") or {}).get("study")) or ""
    if not (pid and study_id):
        return
    with _STUDY_LOCK:
        doc = _study_read(pid, study_id)
        if not doc or doc.get("final") or doc.get("refused"):
            return
        total = len(doc["chunk_texts"])

        def out_of(jc):
            return _study_output(pid, jc)

        chunk_outs = {c["no"]: out_of(c["jcorr"]) for c in doc["chunks"]}
        if any(not v.strip() for v in chunk_outs.values()):
            pass
        elif len(doc["chunks"]) < total:
            brief = "\n".join(chunk_outs[k] for k in sorted(chunk_outs))
            if len(brief) > E.STUDY_BRIEF_CAP:
                brief = brief[-E.STUDY_BRIEF_CAP:]
                cut = brief.find("\n")
                if 0 <= cut < 200:
                    brief = brief[cut + 1:]
            doc["chunks"].append(
                _study_submit_chunk(pid, doc, len(doc["chunks"]) + 1, brief))
        else:
            if total == 1:
                doc["final"] = {"jcorr": doc["chunks"][0]["jcorr"],
                                "merged": False}
            else:
                budget = _study_merge_budget(doc)
                lvl1 = [m for m in doc["merges"] if m["level"] == 1]
                if not lvl1:
                    findings = [chunk_outs[k] for k in sorted(chunk_outs)]
                    groups, cur, size = [], [], 0
                    for i, f in enumerate(findings, start=1):
                        if cur and size + len(f) > budget:
                            groups.append(cur)
                            cur, size = [], 0
                        cur.append(i)
                        size += len(f)
                    if cur:
                        groups.append(cur)
                    for g in groups:
                        doc["merges"].append(_study_submit_merge(
                            pid, doc, 1, g,
                            [chunk_outs[i] for i in g]))
                else:
                    outs = [out_of(m["jcorr"]) for m in lvl1]
                    if all(o.strip() for o in outs):
                        if len(lvl1) == 1:
                            doc["final"] = {"jcorr": lvl1[0]["jcorr"],
                                            "merged": True}
                        else:
                            fin = [m for m in doc["merges"]
                                   if m["level"] == 2]
                            if not fin:
                                if sum(len(o) for o in outs) > budget:
                                    doc["refused"] = (
                                        "the level-1 merge findings still "
                                        "outgrow one merge window - raise "
                                        "the budget or study smaller pieces")
                                else:
                                    doc["merges"].append(_study_submit_merge(
                                        pid, doc, 2,
                                        [m["jcorr"] for m in lvl1], outs))
                            else:
                                out2 = out_of(fin[0]["jcorr"])
                                if out2.strip():
                                    doc["final"] = {"jcorr": fin[0]["jcorr"],
                                                    "merged": True}
        _study_write(pid, doc)


def study_read(pid: str, study_id: str) -> dict:
    doc = _study_read(pid, study_id)
    if not doc:
        return {"error": f"no study {study_id!r}"}
    with _JOBS_LOCK:
        jobs = {j["id"]: j for j in _load_jobs().get("jobs", [])}
    for c in doc.get("chunks") or []:
        c["findings"] = _study_output(pid, c["jcorr"])
        if not c["findings"].strip():
            j = jobs.get(c.get("job_id"))
            c["pending"] = j.get("status") if j else "unknown"
    for m in doc.get("merges") or []:
        m["findings"] = _study_output(pid, m["jcorr"])
    if doc.get("final"):
        doc["final"]["text"] = _study_output(pid, doc["final"]["jcorr"])
    doc.pop("chunk_texts", None)
    return {"study": doc}


def studies_list(pid: str) -> list:
    out = []
    try:
        files = sorted(_studies_dir(pid).glob("study-*.json"), reverse=True)
    except OSError:
        return []
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"id": d.get("id"), "at": d.get("at"),
                    "run": d.get("run") or None,
                    "label": d.get("label"), "aspect": d.get("aspect"),
                    "strategy": d.get("strategy"),
                    "chunks": len(d.get("chunk_texts") or []),
                    "done": bool(d.get("final")),
                    "refused": d.get("refused")})
    return out


def _preview_doctrine(kind: str, pid: str, sid: str, st: dict) -> dict:
    if E is None or kind in DOCTRINE_LESS_KINDS:
        return {"source": "none", "versions": {}}
    if (st or {}).get("doctrine_off"):
        return {"source": "off-by-request", "versions": {}}
    d, src = doctrine_for(_project_id(pid), sid)
    return {"source": src,
            "versions": {k: (v.get("version") or "") for k, v in d.items()}}


def _preview_conflicts(kind: str, pid: str, sid: str, st: dict, pcs) -> dict:
    if AX is None:
        return {"checked": False, "why": "loom_axes is not importable",
                "rows": []}
    if E is None or (st or {}).get("doctrine_off"):
        return {"checked": False,
                "why": "the doctrine is off for this send" if E else "no engine",
                "rows": []}

    sources = []
    docs, _src = doctrine_for(_project_id(pid), sid)
    for which, d in (docs or {}).items():
        riding = [ln["text"] for ln in E.parse_doctrine(d.get("text") or "")["lines"]
                  if kind in E.doctrine_rides(ln, which)]
        if riding:
            sources.append({"label": f"{which} doctrine",
                            "text": "\n".join(riding)})
    for e in (pcs or []):
        if (e or {}).get("text"):
            sources.append({"label": e.get("label") or e.get("id") or "pool",
                            "text": e["text"]})
    return {"checked": True, "why": "", "rows": AX.steering_conflicts(sources)}


def _feedback_reach(st: dict, prompt: str) -> dict:
    entries = st.get("feedback") or []
    if not entries or E is None:
        return {}
    windows = st.get("feedback_windows",
                     st.get("feedback_window", E.FEEDBACK_WINDOW if E else 10))
    target = st.get("target_part")
    digest = E.feedback_digest(entries, windows, target) or ""
    rendered = bool(digest) and E.norm_text(digest) in E.norm_text(prompt)

    dropped = {r["i"]: r for r in E.feedback_dropped(entries, windows, target)
               if "i" in r}

    out = {}
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            continue
        ch = e.get("channel") or "story"
        part = e.get("part")
        row = dropped.get(i)
        if row is None:
            if not (e.get("vote") or 0) and not (e.get("comment") or "").strip():
                out[i] = (None, "it carries neither a vote nor a comment - "
                                "nothing was said")
            elif rendered:
                out[i] = (digest, None)
            elif not digest:
                out[i] = (None, "the digest came out empty")
            else:
                out[i] = (None, "the compiler did not render the digest")
            continue
        if row["why"] == "channel":
            why = f"its channel {ch!r} is not one the digest collects"
        elif row["why"] == "off":
            why = f"{ch} feedback is turned off for this story"
        elif row["why"] == "window":
            w = row.get("window")
            lo = (target - w) if (target is not None and w) else None
            why = (f"it is outside its {w}-part {ch.upper()} window: this "
                   f"remark is on part {part}, and only parts "
                   f"{lo}-{target} are sent")
        else:
            why = (f"the {ch} bucket was full - the newest "
                   f"{E.FEEDBACK_CAP} ride, and this one is older")
        out[i] = (None, why)
    return out


def preview_losses(kind: str, structured: dict, prompt: str) -> list:
    st = structured or {}
    if E is None:
        return []
    reads = E.COMPILER_READS.get(kind, set())
    out, seen = [], set()

    def note(what, why, sample=""):
        if what in seen:
            return
        seen.add(what)
        out.append({"what": what, "why": why,
                    "sample": " ".join(str(sample).split())[:90]})

    for i, (region, why) in sorted(_feedback_reach(st, prompt).items()):
        if region is None:
            e = (st.get("feedback") or [])[i]
            note(f"feedback #{i + 1}", why,
                 e.get("comment") or e.get("paragraph") or "")

    for blk in (st.get("ledgers") or []):
        if not isinstance(blk, dict):
            continue
        cat = blk.get("label") or blk.get("cat") or "ledger"
        for grp in (blk.get("groups") or []):
            for ent in (grp.get("entries") or []):
                text = str((ent or {}).get("text") or "").strip()
                if not text:
                    continue
                if E.reach_of(prompt, text)["reach"] == "none":
                    nm = str(grp.get("label") or "").strip()
                    note(f"{cat}: {nm}" if nm and nm != "—" else cat,
                         "this planted row did not reach the prompt", text)

    for k, v in st.items():
        if k == "feedback" or v in (None, "", [], {}):
            continue
        if k == "ledgers":
            continue
        if k not in reads:
            note(k, f"a {kind.replace('_', ' ')} request does not read it",
                 v if isinstance(v, str) else "")
            continue
        if k in ("canon", "canon_parts") and kind == "chapter_plot" \
                and E.settled_block(st.get("part_summaries")):
            note(k, "a planner is handed the book's events, not its prose "
                    "(A-10); the join rides as the one line under ALREADY "
                    "HAPPENED")
            continue
        if isinstance(v, str):
            if E.reach_of(prompt, v)["reach"] == "none":
                note(k, "it is not in the prompt anywhere", v)
        elif isinstance(v, dict):
            for k2, v2 in v.items():
                if isinstance(v2, str) and v2.strip() \
                        and E.reach_of(prompt, v2)["reach"] == "none":
                    note(f"{k}.{k2}", "it is not in the prompt anywhere", v2)
    return out


def measure_reach(kind: str, structured: dict, manifest: dict, prompt: str) -> dict:
    st = structured or {}
    reads = E.COMPILER_READS.get(kind, set())
    store_inputs = manifest.get("inputs") or {}
    fb = _feedback_reach(st, prompt)

    dropped, absent = [], []
    for path, rec in store_inputs.items():
        top = path.split(".")[0].split("[")[0]
        body = None
        try:
            body = _text_store_ro(manifest.get("_project") or "").resolve(rec["ref"])
        except Exception:
            body = None
        why_not = None
        m = re.match(r"feedback\[(\d+)\]\.", path)
        if m and int(m.group(1)) in fb:
            region, why_not = fb[int(m.group(1))]
            r = E.reach_of(region, body if body is not None else "") if region \
                else {"reach": "none", "chars": 0}
        else:
            r = E.reach_of(prompt, body if body is not None else "")
        rec["reach"] = r["reach"]
        if r.get("chars"):
            rec["reach_chars"] = r["chars"]
        if r.get("side"):
            rec["reach_side"] = r["side"]
        if r["reach"] == "none":
            dropped.append({"path": path, "ref": rec["ref"],
                            "why": why_not or ("not read by this compiler"
                                               if top not in reads
                                               else "read but not rendered")})

    unread = [k for k in st
              if k not in reads and k not in ("steers",) or
              (k == "steers" and "steers" not in reads)]
    unread = sorted({k for k in st if k not in reads})
    for k in sorted(reads):
        v = st.get(k)
        if k not in st:
            continue
        if v in (None, "", [], {}):
            absent.append(k)
    never_sent = sorted(k for k in reads if k not in st)

    return {"dropped": dropped, "unread": unread,
            "absent": sorted(absent), "never_sent": never_sent}


MODEL_DIALECT = {"qwen3-32b": "/no_think"}


def _apply_prompt_suffix(job: dict, prompts: list) -> list:
    gen = job.get("gen") or {}
    sfx = str(gen.get("prompt_suffix") or "").strip() \
        or MODEL_DIALECT.get(str(gen.get("model") or ""), "")
    if not sfx:
        return prompts
    return [p + "\n\n" + sfx for p in prompts]


def _prompts_for(job: dict) -> list:
    inp = job.get("inputs") or {}
    if inp.get("prompts") or inp.get("prompt"):
        prompts = inp.get("prompts") or [inp.get("prompt") or ""]
    else:
        st = inp.get("structured")
        if not st:
            raise ValueError("job has neither inputs.prompt nor inputs.structured")
        if E is None:
            raise RuntimeError("loom_engine is not importable on this server")
        fn = {"write_part": E.compile_part, "replace_para": E.compile_replace,
              "suggest": E.compile_suggest, "ideas": E.compile_ideas,
              "chapter_plot": E.compile_chapter_plot,
              "judge": E.compile_judge,
              "study": E.compile_study,
              "study_merge": E.compile_study_merge,
              "suggest_extracts": E.compile_suggest_extracts,
              "stage_plan": E.compile_stage_plan,
              "part_plan": E.compile_part_plan,
              "consolidate": E.compile_consolidate,
              "compose": E.compile_compose,
              "bake": E.compile_bake,
              "summarize_part": E.compile_part_summary}.get(job["kind"])
        if fn is None:
            raise ValueError(f"no compiler for kind {job['kind']!r}")
        pcs, used = ([], []) if job["kind"] in POOL_LESS_KINDS else \
            project_context_for(_project_id(job.get("project")), inp.get("pools"),
                                E.step_of(job["kind"], st))
        if pcs:
            st = {**st, "project_context": pcs}
            if job.get("id"):
                _mutate(job["id"], pools_used=used)

        docs, doc_src = ({}, "none")
        if job["kind"] not in DOCTRINE_LESS_KINDS:
            if st.get("doctrine_off"):
                doc_src = "off-by-request"
            else:
                docs, doc_src = doctrine_for(_project_id(job.get("project")),
                                             job.get("story"))
        if docs:
            st = {**st, "doctrines": docs, "kind": job["kind"]}
        asks = asks_for(_project_id(job.get("project")), job.get("story"))
        if asks:
            st = {**st, "asks": asks}
        dname, dentry = _ask_draft_entry(st.get("ask_draft"))
        if dname:
            st = {**st, "asks": {**(st.get("asks") or {}), dname: dentry}}
        dropped = E.feedback_dropped(
            st.get("feedback"),
            st.get("feedback_windows",
                   st.get("feedback_window", E.FEEDBACK_WINDOW)),
            st.get("target_part")) if E else []
        if dropped:
            _trace("compile.feedback_dropped", n=len(dropped),
                   why=sorted({d.get("why", "?") for d in dropped}),
                   channels=sorted({d["channel"] for d in dropped
                                    if "channel" in d}))
        if job.get("corr") and job["kind"] not in ("consolidate", "compose",
                                                   "bake",
                                                   "summarize_part", "judge",
                                                   "study", "study_merge"):
            try:
                _req_write(job.get("project"), job["corr"], "doctrine.json", {
                    "source": doc_src,
                    "versions": {k: v.get("version") for k, v in docs.items()},
                    "rode": {k: [l["id"] for l in E.parse_doctrine(v["text"])["lines"]
                                 if job["kind"] in E.doctrine_rides(l, k)]
                             for k, v in docs.items()},
                    "text": {k: v.get("text") for k, v in docs.items()}})
                _req_event(job, "doctrine", source=doc_src,
                           versions=sorted(v.get("version") or ""
                                           for v in docs.values()))
            except Exception as e:
                _trace("doctrine.seal_failed", corr=job.get("corr"),
                       err=str(e)[:160])

        if job.get("corr"):
            try:
                man = freeze_inputs(job, st)
                job["_manifest"] = man
                _req_write(job.get("project"), job["corr"], "manifest.json", man)
                _req_event(job, "frozen", texts=man["text_count"],
                           pools=len(man["pools"]), scalars=len(man["scalars"]))
            except ValueError as e:
                _req_event(job, "freeze_refused", err=str(e))
                raise
            except Exception as e:
                _trace("freeze.failed", corr=job.get("corr"), err=str(e)[:160])
                _req_event(job, "freeze_failed", err=str(e)[:160])
        _trace("compile.start", corr=job.get("corr"), kind=job["kind"], pools=len(pcs),
               summaries=len(st.get("part_summaries") or []),
               canon_words=len(str(st.get("canon") or "").split()),
               feedback=len(st.get("feedback") or []),
               facets=sum(1 for v in (st.get("chapter") or {}).get("facets", {}).values() if v))
        steers = st.get("steers") or []
        n = max(1, int(job.get("n") or 1))
        if any((s or "").strip() for s in steers):
            prompts = [fn({**st, "steers": [steers[i] if i < len(steers) else ""],
                           "n": 1, "steer": steers[i] if i < len(steers) else ""})
                       for i in range(n)]
        else:
            prompts = [fn({**st, "n": n})] if job["kind"] in ("suggest", "ideas") \
                      else [fn(st)] if job["kind"] == "summarize_part" \
                      else [fn(st) for _ in range(n)]
    if len(prompts) < job.get("n", 1):
        prompts = (prompts * job["n"])[:job["n"]]
    if job.get("corr") and prompts:
        seen = []
        for p_text in prompts:
            h = hashlib.sha256(p_text.encode("utf-8")).hexdigest()
            if h[:12] not in seen:
                seen.append(h[:12])
                _req_write(job.get("project"), job["corr"],
                           f"prompts/{h[:12]}.txt", p_text, mode="text")
        _req_event(job, "compiled", prompts=len(prompts),
                   distinct=len(seen), sha12=seen)
    if job.get("corr") and prompts and job.get("_manifest"):
        try:
            man = job["_manifest"]
            man["_project"] = job.get("project")
            lists = measure_reach(job["kind"], st, man, prompts[0])
            man.pop("_project", None)
            man.update(lists)
            _req_write(job.get("project"), job["corr"], "manifest.json", man)
            if lists["dropped"] or lists["never_sent"]:
                _trace("reach.losses", corr=job.get("corr"),
                       dropped=len(lists["dropped"]),
                       never_sent=",".join(lists["never_sent"]))
            _req_event(job, "reach",
                       full=sum(1 for r in (man.get("inputs") or {}).values()
                                if r.get("reach") == "full"),
                       partial=sum(1 for r in (man.get("inputs") or {}).values()
                                   if r.get("reach") == "partial"),
                       dropped=len(lists["dropped"]),
                       never_sent=len(lists["never_sent"]))
        except Exception as e:
            _trace("reach.failed", corr=job.get("corr"), err=str(e)[:160])
    if TRACE and prompts:
        blocks = prompts[0].split(E.DIVIDER) if E is not None else [prompts[0]]
        _trace("compile.done", n=len(prompts), chars=len(prompts[0]),
               blocks=len(blocks))
        for b in blocks:
            _trace("compile.block", head=b.split("\n", 1)[0][:58].replace(" ", "_"),
                   chars=len(b))
    return prompts


_WORKER_STOP = threading.Event()


_WORKER_BEAT = {"gen": 0, "cycle_at": 0.0, "started_at": 0.0, "restarts": 0}
_WORKER_THREAD = None

_WORKER_RESTART_FAULT = {"kind": "", "detail": "", "at": 0.0, "count": 0}


def worker_restart_fault() -> dict:
    return dict(_WORKER_RESTART_FAULT)


def _note_restart_fault(kind: str, detail: str) -> None:
    _WORKER_RESTART_FAULT["kind"] = kind
    _WORKER_RESTART_FAULT["detail"] = detail[:200]
    _WORKER_RESTART_FAULT["at"] = time.time()
    _WORKER_RESTART_FAULT["count"] += 1
    speak, folded = _warn_ok(f"worker.restart_failed|{kind}")
    if not speak:
        return
    print(f"[loom] THE JOBS WORKER IS DEAD AND COULD NOT BE RESTARTED: "
          f"{kind}: {detail[:200]} (#{_WORKER_RESTART_FAULT['count']}) - the "
          "server will keep answering and nothing will generate; the "
          f"supervisor retries every 2s{_warn_more(folded)}", flush=True)


def _fault_aged(fault: dict, now: float) -> dict:
    out = dict(fault)
    out["age_s"] = round(now - fault["at"], 1) if fault.get("at") else None
    return out


def worker_health() -> dict:
    t = _WORKER_THREAD
    now = time.time()
    return {
        "alive": bool(t and t.is_alive()),
        "generation": _WORKER_BEAT["gen"],
        "restarts": _WORKER_BEAT["restarts"],
        "idle_s": round(now - (_WORKER_BEAT["cycle_at"] or now), 1),
        "uptime_s": round(now - (_WORKER_BEAT["started_at"] or now), 1),
        "store_fault": _fault_aged(jobs_store_fault(), now),
        "restart_fault": _fault_aged(worker_restart_fault(), now),
    }


def _worker_banner() -> str:
    if worker_health()["alive"]:
        return f"worker running (generation {_WORKER_BEAT['gen']})"
    return ("WORKER NOT RUNNING - nothing will generate; the supervisor "
            "retries every 2s")


def _worker() -> None:
    _WORKER_BEAT["started_at"] = time.time()
    while not _WORKER_STOP.is_set():
        try:
            _worker_cycle()
        except Exception as e:
            speak, folded = _warn_ok(f"worker.cycle_failed|{type(e).__name__}")
            if speak:
                print(f"[loom] worker cycle failed, continuing: "
                      f"{type(e).__name__}: {e}{_warn_more(folded)}",
                      flush=True)
            time.sleep(1.0)


def _start_worker() -> None:
    global _WORKER_THREAD
    old = _WORKER_THREAD
    if old is not None and old.is_alive():
        _warn("worker.two_engines", had=old.name, gen=_WORKER_BEAT["gen"],
              why="a second worker is starting while the first is still alive")
    nxt = _WORKER_BEAT["gen"] + 1
    t = threading.Thread(target=_worker, daemon=True, name=f"loom-jobs-{nxt}")
    t.start()
    _WORKER_THREAD = t
    _WORKER_BEAT["gen"] = nxt
    _trace("worker.started", gen=nxt, thread=t.name)


def _supervise_worker() -> None:
    while not _WORKER_STOP.is_set():
        time.sleep(2.0)
        try:
            _supervise_pass()
        except BaseException as e:
            _note_restart_fault(type(e).__name__,
                                f"the supervisor pass itself failed: {e}")


def _supervise_pass() -> None:
    try:
        _reap_given_up()
    except BaseException as e:
        _warn("reap.failed", kind=type(e).__name__, err=f"{e}"[:120])
    try:
        _sgx_sweep()
    except BaseException as e:
        _warn("sgx.sweep_failed", kind=type(e).__name__, err=f"{e}"[:120])
    t = _WORKER_THREAD
    if _WORKER_STOP.is_set() or (t and t.is_alive()):
        return
    _restart_worker()


def _restart_worker() -> None:
    if _warn_ok("worker.died|")[0]:
        print("[loom] the jobs worker died - starting a new one", flush=True)
    try:
        _start_worker()
    except BaseException as e:
        _note_restart_fault(type(e).__name__, str(e))
        return
    _WORKER_BEAT["restarts"] += 1
    speak, folded = _warn_ok("worker.restarted|")
    if speak:
        print(f"[loom] the jobs worker is running again "
              f"(generation {_WORKER_BEAT['gen']}, restart "
              f"#{_WORKER_BEAT['restarts']}){_warn_more(folded)}", flush=True)


def _worker_cycle() -> None:
    while not _WORKER_STOP.is_set():
        _WAKE.wait(timeout=2.0)
        _WAKE.clear()
        _WORKER_BEAT["cycle_at"] = time.time()
        if _WORKER_STOP.is_set():
            return
        while True:
            with _JOBS_LOCK:
                doc = _load_jobs()
                rows = _store_rows(doc)
                if rows is None:
                    _warn("jobs.not_a_store", kind=type(doc).__name__,
                          store=JOBS, why="valid JSON with no jobs list; "
                          "nothing can be picked until it is fixed by hand")
                    break
                if any(j["status"] == "running" for j in rows):
                    break
                nxt = _queue_order(doc)
                if not nxt:
                    break
                job = nxt[0]
                job["status"] = "running"
                job["cancel"] = False
                _recompute_queue_positions(doc)
                _save_jobs(doc)
            if _ex.core.is_extract_job(job):
                if _ex.core.DATA is None:
                    _mutate(job["id"], status="failed",
                            error="this extract job needs a scan tree and this "
                                  "server has no extractor root (--extractor-data)",
                            finished=time.time())
                    continue
                try:
                    _ex.core.execute_job(job)
                except Exception as e:
                    _trace("extract.failed",
                           job=job["id"], err=f"{type(e).__name__}: {e}"[:200])
                    _mutate(job["id"], status="failed",
                            error=f"{type(e).__name__}: {e}"[:200],
                            finished=time.time())
                continue
            try:
                prompts = _apply_prompt_suffix(job, _prompts_for(job))
            except Exception as e:
                _mutate(job["id"], status="failed", error=f"compile failed: {e}",
                        finished=time.time())
                continue
            fit = _fit(prompts[0], job["gen"].get("model"),
                       job["gen"].get("max_tokens"), job.get("kind"))
            _trace("fit.check", approx=fit["approx_tokens"], ctx=fit["ctx"],
                   headroom=fit["headroom"], fits=fit["fits"])
            _trace_prompt(job, prompts, fit,
                          (_get_job(job["id"]) or {}).get("pools_used") or [])
            if not fit["fits"]:
                _trace("fit.REFUSED", why=fit["warning"])
                _mutate(job["id"], status="failed", error=fit["warning"],
                        finished=time.time(), progress={"fit": fit})
                continue
            cands, err = [], None
            for i, p in enumerate(prompts):
                try:
                    seed = job["gen"].get("seed")
                    cands.append(_generate_with_retries(job, p, None if seed is None else seed + i))
                    job["_spent"] = int(job.get("_spent") or 0) + int(
                        cands[-1].get("token_count") or 0)
                    _mutate(job["id"], n_done=len(cands), _spent=job["_spent"])
                except GenerationFailed as e:
                    err = str(e)
                    break
                except RuntimeError:
                    err = "cancelled"
                    break
                except Exception as e:
                    err = str(e)
                    break
            samples = int(job.get("_samples_used") or 0) or len(cands)
            if err == "cancelled":
                _seal_request(job, "cancelled", samples=samples)
                _mutate(job["id"], status="cancelled", finished=time.time(),
                        result={"candidates": cands, "target_hash": job.get("target_hash")})
            elif err:
                _seal_request(job, "failed", error=err, samples=samples)
                _mutate(job["id"], status="failed", error=err, finished=time.time())
            else:
                _seal_request(job, "done", samples=samples)
                try:
                    _copy_on_complete(job, cands)
                    _restate_on_complete(job, cands)
                except Exception as e:
                    _trace("copy.check_failed", corr=job.get("corr"),
                           err=str(e)[:160])
                res = {"candidates": cands, "target_hash": job.get("target_hash")}
                if job["kind"] == "judge":
                    passage = str(((job.get("inputs") or {})
                                   .get("structured") or {})
                                  .get("passage") or "")
                    res["verdicts"] = [
                        E.judge((c.get("text") or ""), passage)
                        for c in cands] if E else []
                    res["agreed"] = (E.judge_agree(res["verdicts"])
                                     if E else None)
                    if job.get("corr"):
                        try:
                            _req_write(job.get("project"), job["corr"],
                                       "verdict.json",
                                       {"verdicts": res["verdicts"],
                                        "agreed": res["agreed"]})
                        except Exception as e:
                            _trace("verdict.seal_failed",
                                   corr=job.get("corr"), err=str(e)[:160])
                if job["kind"] == "suggest_extracts":
                    try:
                        _sgx_on_complete(job)
                    except Exception as e:
                        _trace("sgx.hook_failed", corr=job.get("corr"),
                               err=str(e)[:160])
                if job["kind"] == "bake":
                    try:
                        _bake_on_complete(job)
                    except Exception as e:
                        _trace("bake.leakcheck_failed", corr=job.get("corr"),
                               err=str(e)[:160])
                if job["kind"] in ("study", "study_merge"):
                    try:
                        _study_on_complete(job)
                    except Exception as e:
                        _trace("study.hook_failed", corr=job.get("corr"),
                               err=str(e)[:160])
                if job["kind"] == "summarize_part":
                    raw = (cands[0].get("text") if cands else "") or ""
                    parsed = E.parse_part_summary(raw) if E else {}
                    res["summary"] = parsed or None
                    _trace("summary.parsed", fields=sorted(parsed),
                           ok=bool(parsed), chars=len(raw))
                if job["kind"] == "stage_plan":
                    raw = (cands[0].get("text") if cands else "") or ""
                    extra = _stage_result(
                        (job.get("inputs") or {}).get("structured") or {}, raw)
                    if extra:
                        res.update(extra)
                        _trace("stage.parsed", scenes=len(extra["plan"]),
                               chars=len(raw))
                if job["kind"] == "part_plan" and E is not None:
                    got = 0
                    for cd in cands:
                        scenes = E.parse_blueprint(cd.get("text") or "")
                        if scenes:
                            cd["plan"] = scenes[:1]
                            got += 1
                    _trace("partplan.parsed", candidates=len(cands),
                           parsed=got, ok=bool(got))
                if job["kind"] == "chapter_plot" and E is not None:
                    want = int(((job.get("inputs") or {})
                                .get("structured") or {}).get("target_parts") or 0)
                    if want > 0:
                        got = 0
                        for cd in cands:
                            scenes = E.parse_blueprint(cd.get("text") or "")
                            if scenes:
                                cd["plan"] = scenes
                                got += 1
                        _trace("divplot.parsed", candidates=len(cands),
                               parsed=got, want=want,
                               scenes=[len(c.get("plan") or []) for c in cands],
                               ok=bool(got))
                _mutate(job["id"], status="done", finished=time.time(),
                        result=res)



_BLUEPRINT_STAGE = ("blueprint", "finalize the structure and layout")


def _stage_result(structured: dict, text: str) -> dict:
    stage = str((structured or {}).get("stage") or "").strip().lower()
    if stage not in _BLUEPRINT_STAGE or E is None:
        return {}
    return {"plan": E.parse_blueprint(text or "")}


PEEK_DROP = ()


def _peek_candidates(job: dict) -> list:
    return [{k: v for k, v in c.items() if k not in PEEK_DROP}
            for c in ((job.get("result") or {}).get("candidates") or [])]


def _copy_on_complete(job: dict, cands: list) -> None:
    if E is None or job.get("kind") not in PROSE_KINDS:
        return
    canon = E.canon_from((job.get("inputs") or {}).get("structured") or {})
    for c in cands or []:
        c["copied"] = E.copied_runs(c.get("text") or "", canon)


def _restate_on_complete(job: dict, cands: list) -> None:
    if E is None or job.get("kind") not in PLANNING_KINDS:
        return
    sums = ((job.get("inputs") or {}).get("structured") or {}).get("part_summaries")
    for c in cands or []:
        c["restated"] = E.restated_runs(c.get("text") or "", sums)


RETRY_MAX = 3
RETRY_WHEN = {"copied": 5, "restated": 1, "short": 0.5}

_PLAN_BEATS = re.compile(r"^\s*\**\s*BEATS\s*\**\s*:", re.I)
_PLAN_FIELD = re.compile(r"^\s*\**\s*(?:SCENE|PHASE|TENSION|MATERIAL)\b", re.I)
_PLAN_ITEM = re.compile(r"^\s*(?:[-*•]\s+|\d+[.)]\s+)\S")


def _scene_worth_words(st: dict) -> int:
    if E is None:
        return 0
    plan = str(((st.get("part") or {}).get("plan") or "")).strip()
    if not plan:
        return 0
    beats, in_beats = 0, False
    for ln in plan.split("\n"):
        if _PLAN_BEATS.match(ln):
            in_beats = True
        elif _PLAN_FIELD.match(ln):
            in_beats = False
        elif in_beats and _PLAN_ITEM.match(ln):
            beats += 1
    return beats * E.WORDS_PER_PARAGRAPH


def _retry_verdict(job: dict, cand: dict):
    if E is None:
        return None, None, None
    st = (job.get("inputs") or {}).get("structured") or {}
    text = cand.get("text") or ""
    if job.get("kind") in PROSE_KINDS:
        rep = E.copied_runs(text, E.canon_from(st))
        why = None
        if int(rep.get("runs") or 0) >= RETRY_WHEN["copied"]:
            why = "copied"
        else:
            target = int(st.get("target_words") or 0)
            worth = _scene_worth_words(st)
            if target and worth:
                target = min(target, worth)
            if target and len(text.split()) < RETRY_WHEN["short"] * target:
                why = "short"
        return "copied", rep, why
    if job.get("kind") in PLANNING_KINDS:
        rep = E.restated_runs(text, st.get("part_summaries"))
        runs = int(rep.get("runs") or 0)
        return "restated", rep, ("restated" if runs >= RETRY_WHEN["restated"]
                                 else None)
    return None, None, None


def _generate_with_retries(job: dict, prompt: str, seed, run=None) -> dict:
    run = run or _run_one_generation
    asked = (job.get("gen") or {}).get("retries")
    tries = RETRY_MAX if asked is None else max(1, min(RETRY_MAX, 1 + int(asked)))
    attempts, cand = [], None
    for attempt in range(1, tries + 1):
        job["_variant"] = int(job.get("_samples_used") or 0) + 1
        job["_samples_used"] = job["_variant"]
        s = None if seed is None else seed + 100 * (attempt - 1)
        cand = run(job, prompt, s)
        key, report, why = _retry_verdict(job, cand)
        if key is None:
            return cand
        runs = int(report.get("runs") or 0)
        words = len(str(cand.get("text") or "").split())
        retry = bool(why) and attempt < tries
        attempts.append({"attempt": attempt, "sample": job["_variant"],
                         "seed": s, "runs": runs, "words": words, "why": why})
        _req_write(job.get("project"), job.get("corr"),
                   f"samples/{job['_variant']:02d}/report.json",
                   {"attempt": attempt, "sample": job["_variant"], "seed": s,
                    key: report, "words": words, "why": why, "retry": retry})
        cand[key] = report
        if not retry:
            break
        _trace("retry", corr=job.get("corr"), attempt=attempt, why=why,
               runs=runs, words=words)
    cand["attempts"] = attempts
    return cand


def _seal_request(job: dict, status: str, **kv) -> None:
    corr = job.get("corr")
    if not corr:
        return
    try:
        d = _req_dir(job.get("project"), corr)
        f = d / "request.json"
        doc = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except Exception:
        doc = {}
    doc.update({"status": status, "sealed_at": round(time.time(), 3), **kv})
    _req_write(job.get("project"), corr, "request.json", doc)
    _req_event(job, "sealed", status=status, **kv)




def texts_backfill(pid: str, dry: bool = False) -> dict:
    out = {"project": pid, "stories": [], "wrote": 0, "seen": 0,
           "note": "seeded from the CURRENT state; history starts now"}
    sdir = _project_paths(pid)["stories"]
    if not sdir.exists():
        return {**out, "error": "no stories"}
    for story in sorted(p.name for p in sdir.iterdir() if p.is_dir()):
        try:
            st = (json.loads(_story_paths(pid, story)["state"]
                             .read_text(encoding="utf-8")) or {}).get("state") or {}
        except (OSError, ValueError):
            out["stories"].append({"story": story, "error": "no state.json"})
            continue
        if dry:
            n = sum(1 for _, t in authored_slots(st) if E.norm_text(t))
            out["stories"].append({"story": story, "would_freeze": n})
            out["seen"] += n
            continue
        r = freeze_authored(pid, st)
        out["stories"].append({"story": story, "seen": r["seen"],
                               "wrote": len(r["wrote"])})
        out["seen"] += r["seen"]
        out["wrote"] += len(r["wrote"])
    return out


def requests_adopt(pid: str, dry: bool = False) -> dict:
    out = {"project": pid, "adopted": [], "skipped": 0, "orphan_runs": 0,
           "note": "adopted folders carry adopted:true; their corr is minted "
                   "now and is NOT evidence of what ran"}
    with _JOBS_LOCK:
        doc = _load_jobs()
        jobs = [j for j in doc.get("jobs", [])
                if _project_id(j.get("project")) == _project_id(pid)]
    for j in jobs:
        if j.get("corr"):
            out["skipped"] += 1
            continue
        st = ((j.get("inputs") or {}).get("structured")) or {}
        if not st:
            out["skipped"] += 1
            continue
        stamp = time.strftime("%Y%m%d-%H%M%S",
                              time.localtime(j.get("created") or time.time()))
        k3 = CORR_KIND.get(j.get("kind", ""), "job")
        corr = f"r-{stamp}-{k3}-adop"
        if dry:
            out["adopted"].append({"job": j.get("id"), "corr": corr})
            continue
        try:
            _req_dir(pid, corr, create=True)
        except CorrCollision:
            out["skipped"] += 1
            continue
        shadow = {**j, "corr": corr, "project": pid}
        try:
            man = freeze_inputs(shadow, st)
        except ValueError as e:
            man = {"inputs": {}, "scalars": {}, "pools": [],
                   "error": f"could not freeze: {e}"}
        _req_write(pid, corr, "manifest.json", man)
        _req_write(pid, corr, "request.json", {
            "corr": corr, "corr_source": "adopted", "adopted": True,
            "job": j.get("id"), "kind": j.get("kind"),
            "project": pid, "story": j.get("story"),
            "target": j.get("target") or {}, "gen": j.get("gen") or {},
            "status": j.get("status"), "queued_at": j.get("created"),
            "adopted_at": round(time.time(), 3),
            "note": "adopted from a job record that predates correlation ids; "
                    "the prompt was not retained and is NOT reconstructed here"})
        _req_write(pid, corr, "events.jsonl",
                   {"t": round(time.time(), 3), "event": "adopted",
                    "job": j.get("id"), "status": j.get("status")},
                   mode="append")
        out["adopted"].append({"job": j.get("id"), "corr": corr,
                               "texts": len(man.get("inputs") or {})})

    try:
        runs = projects_root().parent.parent / "data" / "runs"
        known = set()
        for j in jobs:
            known.update(j.get("studio_job_ids") or [])
        if runs.exists():
            allr = [d.name for d in runs.iterdir() if d.is_dir()]
            out["orphan_runs"] = len([r for r in allr if r not in known])
            out["studio_runs_total"] = len(allr)
    except OSError:
        pass
    return out


def _adopt_extract_jobs() -> None:
    if _ex.core.DATA is None:
        return
    old = _ex.core.DATA / "jobs.json"
    if not old.exists():
        return
    try:
        legacy = json.loads(old.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"[loom] could not read the extractor's old queue: {e}")
        return
    rows = [j for j in legacy.get("jobs", []) if j.get("id")]
    if not rows:
        return
    with _JOBS_LOCK:
        doc = _load_jobs()
        have = {j.get("id") for j in doc["jobs"]}
        new = [j for j in rows if j["id"] not in have]
        if new:
            doc["jobs"].extend(new)
            doc["seq"] = max(int(doc.get("seq", 0)),
                             int(legacy.get("seq", 0)))
            _save_jobs(doc)
    try:
        old.rename(old.with_suffix(".json.adopted"))
    except OSError:
        pass
    print(f"  adopted       {len(new)} extract job(s) into the shared queue "
          f"(old store kept as jobs.json.adopted)")


def _recover_jobs() -> None:
    with _JOBS_LOCK:
        doc = _load_jobs()
        changed = _ex.core.requeue_stale(doc)
        for j in doc["jobs"]:
            if j["status"] != "running" or _ex.core.is_extract_job(j):
                continue
            changed = True
            done, ok = [], True
            for sid in j.get("studio_job_ids", []):
                try:
                    st = _studio_get(f"/api/jobs/{sid}", timeout=RECOVER_TIMEOUT_S)
                except Exception:
                    ok = False
                    break
                if st["status"] != "done":
                    ok = False
                    break
                v = (st.get("variants") or [{}])[0]
                m = v.get("meta") or {}
                try:
                    text = _studio_get(f"/api/jobs/{sid}/variants/{v['label']}/output")
                except Exception:
                    text = ""
                done.append({"text": text, "seed": m.get("seed"),
                             "finish_reason": m.get("finish_reason"),
                             "tokens_per_second": m.get("tokens_per_second"),
                             "token_count": m.get("token_count"),
                             "model_key": v.get("model_key"),
                             "studio_job_id": sid})
            if ok and len(done) == j.get("n", 1):
                j["status"] = "done"
                j["result"] = {"candidates": done, "target_hash": j.get("target_hash")}
            else:
                j["status"] = "queued"
                j["studio_job_ids"] = []
            j["finished"] = time.time() if j["status"] == "done" else None
        if changed:
            _recompute_queue_positions(doc)
            _save_jobs(doc)
    _WAKE.set()



ARTIFACT_USAGE = ("USAGE: follow these mechanics. Never reuse the quoted "
                  "fragments verbatim; invent your own names, places and "
                  "objects.\n\n")

def artifact_source_label(stage: str, n: int, aspect: str = "") -> str:
    a = str(aspect or "").strip()
    return f"{stage}{(' · ' + a) if a else ''} · v{int(n)}"


_LEAK_COMMON = {
    "The", "A", "An", "And", "But", "He", "She", "It", "They", "We", "You",
    "His", "Her", "Their", "This", "That", "There", "Then", "When", "What",
    "Where", "Why", "How", "Not", "For", "Nor", "Yet", "So", "One", "Two",
    "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Use",
    "Write", "Keep", "End", "Make", "Let", "Give", "Take", "Put", "Set",
    "Show", "Tell", "Build", "Create", "Develop", "Present", "Reveal",
    "Employ", "Maintain", "Include", "Avoid", "Never", "Always", "Every",
    "Each", "Some", "Most", "Both", "All", "No", "Yes", "If", "As", "At",
    "By", "In", "On", "Of", "To", "Up", "Out", "Now", "Here", "Style",
    "Plot", "Theme", "Prose", "Story", "Chapter", "Part", "Section",
    "Dialogue", "Paragraph", "Sentence", "Point", "View", "Voice",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
    "Sunday", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
}


def _names_in(text: str) -> set:
    t = str(text or "")
    lower = set(re.findall(r"[a-z']{3,}", t))
    caps = re.findall(r"\b[A-Z][a-z]{2,}\b", t)
    return {w for w in caps
            if caps.count(w) >= 2 and w.lower() not in lower
            and w not in _LEAK_COMMON}


def _runs_of(text: str, n: int) -> set:
    w = str(text or "").split()
    return {" ".join(w[i:i + n]).lower().strip(".,;:!?\"'“”")
            for i in range(max(0, len(w) - n + 1))}


LEAK_RUN_WORDS = 7


def artifact_leaks(baked: str, read: list) -> dict:
    names, runs = [], []
    for src in (read or []):
        text = str(src.get("text") or "")
        if not text:
            continue
        where = str(src.get("from") or "a studied document")
        for n in sorted(_names_in(text)):
            if re.search(rf"\b{re.escape(n)}\b", baked or ""):
                names.append({"name": n, "from": where})
        shared = _runs_of(baked, LEAK_RUN_WORDS) & _runs_of(text, LEAK_RUN_WORDS)
        for r in sorted(shared)[:3]:
            runs.append({"run": r, "from": where})
    seen, uniq = set(), []
    for row in names:
        if row["name"] not in seen:
            seen.add(row["name"])
            uniq.append(row)
    return {"names": uniq, "runs": runs,
            "clean": not uniq and not runs}


ARTIFACT_KINDS = {
    "style": {
        "id": "style-artifact",
        "title": "Style artifact (how we write)",
        "pool_kind": "style",
        "scope": "prose",
        "want": "ONE writing-style artifact for this author",
        "instruction": (
            "Fold these findings into ONE instruction sheet titled with "
            "'## ' section headings, one section per lane. State HOW this "
            "author writes - mechanics a writer could follow. Quote at most "
            "SIX consecutive words in any example, and REPLACE every proper "
            "noun with a neutral one, INCLUDING inside a quoted example - "
            "the style must travel without the setting. End each section "
            "with its process labels in parentheses, exactly as given."),
    },
    "narrative": {
        "id": "narrative-artifact",
        "title": "Narrative artifact (what happens, and how it reaches the reader)",
        "pool_kind": "context",
        "scope": "plot",
        "want": ("ONE narrative artifact: what happens and how it reaches "
                 "the reader"),
        "instruction": (
            "Fold these findings into ONE sheet with exactly THREE '## ' "
            "section headings, one per lane and in this order:\n"
            "1. how stories are built here\n"
            "2. how people treat each other\n"
            "3. what is withheld, and how it reaches the reader without "
            "being stated\n"
            "Lane 3 is about WITHHOLDING - what the reader is not told "
            "outright and how it reaches them anyway. It is NOT about the "
            "prose style: sentence length, rhythm, paragraph shape and "
            "attribution belong to the style sheet and must not appear in "
            "this one at all. A lane with nothing to say from these findings "
            "says so in one line; it is never filled with another lane's "
            "material. Describe MECHANISMS and roles, never the source "
            "settings: no proper nouns from the stories, not even inside a "
            "quoted example. End each section with its process labels in "
            "parentheses, exactly as given."),
    },
}

ARTIFACT_UPDATE_INSTRUCTION = (
    "The first source is the artifact AS IT STANDS; the rest are NEW FINDINGS "
    "to fold into it - raw material, never sections to keep. Return the "
    "artifact UPDATED, not a report about it and not the artifact with the "
    "findings appended: keep every section that still holds, in its own "
    "words, and revise or extend a section only where a new finding says "
    "something the sheet does not. A new finding that repeats what a section "
    "already says changes nothing. Keep the same '## ' section shape, keep "
    "the sheet AT OR UNDER 500 WORDS - it may grow toward that as the book "
    "teaches more, and a section that has nothing new to say does not grow at "
    "all - quote at most SIX consecutive words, and use no proper nouns from "
    "any story - not even inside a quoted example. Update each section's "
    "process labels to include the new material's, exactly as given.")


def _bake_on_complete(job: dict) -> None:
    out = ""
    try:
        out = (job.get("result") or {}).get("samples", [{}])[0].get("text") or ""
    except Exception:
        out = ""
    if not out:
        outs = (job.get("outputs") or job.get("samples") or [])
        out = (outs[0] if outs else "") if isinstance(outs, list) else ""
        out = out if isinstance(out, str) else (out or {}).get("text") or ""
    pid = job.get("project")
    read = []
    for row in ((job.get("target") or {}).get("trace") or []):
        doc = _study_read(pid, row.get("study") or "") or {}
        read.append({"text": "\n".join(doc.get("chunk_texts") or []),
                     "from": row.get("read") or row.get("label") or "a study"})
    rep = artifact_leaks(out, read)
    rep["words"] = len(str(out).split())
    with _JOBS_LOCK:
        d = _load_jobs()
        for j in d.get("jobs", []):
            if j.get("id") == job.get("id"):
                j.setdefault("target", {})["leaks"] = rep
        _save_jobs(d)
    job.setdefault("target", {})["leaks"] = rep
    _trace("bake.leakcheck", corr=job.get("corr"), clean=rep["clean"],
           names=len(rep["names"]), runs=len(rep["runs"]))


STUDY_RUN_GAP_SECONDS = 30

STUDY_LABEL_SEP = " \u00b7 "


def _study_at_seconds(at: str):
    try:
        return time.mktime(time.strptime(str(at), "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError, OverflowError):
        return None


def _study_runs(rows: list) -> dict:
    at = {r.get("id"): _study_at_seconds(r.get("at")) for r in rows}
    timed = sorted((r for r in rows if at.get(r.get("id")) is not None),
                   key=lambda r: (at[r["id"]], str(r.get("id") or "")))
    out, start, prev = {}, None, None
    for r in timed:
        t = at[r["id"]]
        if start is None or t - prev > STUDY_RUN_GAP_SECONDS:
            start = str(r.get("at") or "")
        prev = t
        stamp = start.replace("-", "").replace(":", "").replace(" ", "-")
        out[r["id"]] = ((r.get("run"), False) if r.get("run")
                        else ("run-d-" + stamp, True))
    for r in rows:
        if r.get("id") not in out:
            out[r.get("id")] = (r.get("run")
                                or "run-d-" + str(r.get("id") or "unknown"),
                                not r.get("run"))
    return out


def _study_subject_finding(doc: dict) -> tuple:
    subject = (doc.get("subject_label") or "").strip()
    finding = (doc.get("finding") or "").strip()
    if subject and finding:
        return subject, finding
    label = str(doc.get("label") or "").strip()
    aspect = str(doc.get("aspect") or "").strip()
    if aspect != "custom":
        return subject or label, finding or (aspect or "study")
    head, sep, tail = label.rpartition(STUDY_LABEL_SEP)
    if sep and head.strip() and tail.strip():
        return subject or head.strip(), finding or tail.strip()
    return subject or label, finding or "custom"


def artifact_studies(pid: str) -> list:
    rows = studies_list(pid)
    runs = _study_runs(rows)
    out = []
    for row in rows:
        doc = (study_read(pid, row.get("id")) or {}).get("study") or {}
        text = ((doc.get("final") or {}).get("text") or "").strip()
        run, derived = runs.get(row.get("id"), (None, True))
        subject, finding = _study_subject_finding(
            {**row, "subject_label": doc.get("subject_label"),
             "finding": doc.get("finding")})
        out.append({
            "id": row.get("id"), "label": row.get("label") or row.get("id"),
            "aspect": row.get("aspect") or "", "at": row.get("at"),
            "run": run, "run_derived": derived,
            "subject": subject, "finding": finding,
            "ask": doc.get("ask") or None,
            "words": len(text.split()),
            "ready": bool(text),
            "refused": row.get("refused") or None,
        })
    return out


def artifact_pools(pid: str) -> list:
    out = []
    for p in pools_list(pid):
        if p.get("archived") or not p.get("current"):
            continue
        if p.get("include") != "always":
            continue
        out.append({"id": p["id"], "title": p.get("title") or p["id"],
                    "kind": p.get("kind"), "version": p.get("current"),
                    "words": p.get("words") or 0,
                    "scope": _pool_scope(p)})
    return out


def artifact_build(pid: str, which: str = "", pool: str = "",
                   studies=None, instruction: str = "", model: str = None,
                   story: str = None) -> dict:
    ids = [str(x) for x in (studies or []) if str(x).strip()]
    if not ids:
        return {"error": "pick at least one finished study to build from"}

    sources, used, skipped = [], [], []
    spec = None
    target_pool = ""
    if pool:
        target_pool = _pool_id(pool)
        have = next((p for p in pools_list(pid)
                     if p["id"] == target_pool and p.get("current")), None)
        if have is None:
            return {"error": f"no artifact {target_pool!r} to update yet"}
        sources.append({"label": artifact_source_label("the artifact so far",
                                                       have["current"]),
                        "text": _pool_text(pid, target_pool, have["current"])})
        meta = {"title": have.get("title"), "kind": have.get("kind"),
                "include": "always", "role": have.get("role") or "context",
                "scope": have.get("scope") or "both"}
        want = f"the UPDATED {have.get('title') or target_pool}"
        instr = ARTIFACT_UPDATE_INSTRUCTION
    else:
        spec = ARTIFACT_KINDS.get(str(which or "").strip())
        if spec is None:
            return {"error": "say which artifact to build: "
                             + ", ".join(sorted(ARTIFACT_KINDS))}
        target_pool = spec["id"]
        meta = {"title": spec["title"], "kind": spec["pool_kind"],
                "include": "always", "role": "context",
                "scope": spec["scope"]}
        want = spec["want"]
        instr = spec["instruction"]

    trace = []
    for sid in ids:
        doc = (study_read(pid, sid) or {}).get("study") or {}
        text = ((doc.get("final") or {}).get("text") or "").strip()
        if not text:
            skipped.append(sid)
            continue
        label = artifact_source_label("initial study", len(used) + 1,
                                      doc.get("aspect") or "")
        sources.append({"label": label, "text": text})
        trace.append({"label": label, "study": sid,
                      "read": doc.get("label") or "", "aspect": doc.get("aspect") or ""})
        used.append(sid)
    if not used:
        return {"error": "none of those studies has finished findings yet",
                "skipped": skipped}

    extra = " ".join(str(instruction or "").split())
    if extra:
        instr = instr + "\n\nALSO, from the author: " + extra[:600]

    mk = _default_model(model)
    if not mk:
        return {"error": "no model is present"}
    job = jobs_submit({
        "kind": "bake", "project": pid, "story": story or "",
        "target": {"pool": target_pool, "meta": meta,
                   "artifact": which or target_pool,
                   "stamp": ARTIFACT_USAGE,
                   "from_studies": used,
                   "trace": trace},
        "inputs": {"structured": {"sources": sources, "want": want,
                                  "instruction": instr}},
        "gen": {"model": mk, "temperature": 0.3, "max_tokens": 1200, "n": 1},
        "priority": "interactive"})
    _trace("artifact.queued", project=pid, pool=target_pool,
           studies=len(used), update=bool(pool), model=mk)
    return {"queued": job["job_id"], "pool": target_pool, "model": mk,
            "used": used, "skipped": skipped,
            "update": bool(pool), "sources": len(sources)}



REVIEW_ROOT = Path(__file__).resolve().parent / "tests" / "e2e" / "_kept"
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,80}$")
REVIEW_VERDICTS = ("keep", "prefer-raw", "drop")
_REVIEW_SKIP = {"RAW-vs-REWRITE"}


def _review_root() -> Path:
    return REVIEW_ROOT


def _app_runs_root() -> Path:
    return projects_root().parent / "review" / "runs"


def _both_roots():
    return ((_review_root(), "suite"), (_app_runs_root(), "app"))


def _run_dir(run_id: str) -> Path:
    rid = str(run_id or "").strip()
    if not _RUN_ID.fullmatch(rid):
        raise ValueError(f"not a run id: {rid!r}")
    for root0, _origin in _both_roots():
        root = root0.resolve() if root0.exists() else root0
        d = (root / rid).resolve()
        if d != root and root not in d.parents:
            raise ValueError(f"run id escapes the evidence root: {rid!r}")
        if d.is_dir():
            return d
    raise FileNotFoundError(f"no such run: {rid}")


def _pane_label(stem: str) -> str:
    s = stem.replace("_", "-")
    m = re.match(r"^(stage|part)(\d+)-(.+)$", s)
    if m:
        return f"{m.group(1)} {m.group(2)} · {m.group(3).replace('-', ' ')}"
    return s.replace("-", " ")


def _pane_order(stem: str):
    m = re.match(r"^stage(\d+)-", stem)
    if m:
        return (0, int(m.group(1)), stem)
    if "rewrite" in stem:
        return (3, 0, stem)
    hint = {"plot": 1, "part1": 2, "part2": 3}.get(stem)
    if hint:
        return (1, hint, stem)
    return (2, 0, stem)


def _review_panes(d: Path) -> list:
    panes, thinking = [], {}
    for f in sorted(d.glob("*.md")):
        stem = f.stem
        if stem.endswith("-thinking"):
            thinking[stem[:-len("-thinking")]] = f
            continue
        if stem in _REVIEW_SKIP:
            continue
        panes.append({"id": stem, "label": _pane_label(stem), "file": f})
    out = []
    for p in panes:
        t = thinking.get(p["id"])
        try:
            text = p["file"].read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        row = {"id": p["id"], "label": p["label"], "text": text,
               "words": len(text.split())}
        if t is not None:
            try:
                row["thinking"] = t.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
        out.append(row)
    out.sort(key=lambda r: _pane_order(r["id"]))
    return out


def _rewrite_pane(panes: list) -> str:
    for p in panes:
        if "rewrite" in p["id"]:
            return p["id"]
    return ""


def review_runs() -> list:
    out = []
    for root, origin in _both_roots():
        if not root.is_dir():
            continue
        out.extend(_review_rows(root, origin))
    return sorted(out, key=lambda r: r["id"], reverse=True)


def _review_rows(root: Path, origin: str) -> list:
    out = []
    for d in sorted(root.iterdir(), reverse=True):
        if not d.is_dir() or d.name.startswith("."):
            continue
        panes = _review_panes(d)
        if not panes:
            continue
        verdicts = review_verdicts(d.name).get("verdicts") or {}
        out.append({
            "id": d.name,
            "origin": origin,
            "kind": "split" if d.name.startswith("split") else "journey",
            "bailed": "BAILED" in d.name,
            "panes": len(panes),
            "rewrite": _rewrite_pane(panes),
            "words": sum(p["words"] for p in panes),
            "thought": sum(1 for p in panes if p.get("thinking")),
            "judged": len(verdicts),
        })
    return out


_KEEP_STAGES = ("plot", "development", "scenes", "material", "blueprint")
_KEEP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,60}$")


def review_keep(pid: str, story: str, corrs: dict, parts=None,
                run_id: str = "") -> dict:
    corrs = corrs if isinstance(corrs, dict) else {}
    parts = [p for p in (parts or []) if isinstance(p, dict)]
    sid = re.sub(r"[^A-Za-z0-9._+-]+", "-", str(story or "story")).strip("-")[:40]
    rid = str(run_id or "").strip()
    if rid and not _KEEP_ID.fullmatch(rid):
        return {"error": f"not a run id: {rid!r}"}
    if not rid:
        rid = f"split-app-{sid or 'story'}-{time.strftime('%Y%m%d-%H%M%S')}"
    root = _app_runs_root()
    root.mkdir(parents=True, exist_ok=True)
    root_r = root.resolve()
    d = (root_r / rid).resolve()
    if d != root_r and root_r not in d.parents:
        return {"error": f"run id escapes the runs root: {rid!r}"}
    d.mkdir(parents=True, exist_ok=True)

    missing, wrote = [], []

    def _output(corr: str) -> str:
        try:
            f = _req_dir(pid, corr) / "samples" / "01" / "output.md"
            return f.read_text(encoding="utf-8") if f.is_file() else ""
        except (OSError, ValueError):
            return ""

    for i, stage in enumerate(_KEEP_STAGES, start=1):
        corr = str(corrs.get(stage) or "").strip()
        if not corr:
            continue
        text = _output(corr)
        if not text.strip():
            missing.append(corr)
            continue
        name = f"stage{i}-{stage}.md"
        (d / name).write_text(text, encoding="utf-8", newline="\n")
        wrote.append(name)
    for p in parts:
        corr = str(p.get("corr") or "").strip()
        try:
            n = int(p.get("ordinal") or 0)
        except (TypeError, ValueError):
            n = 0
        if not corr or n < 1:
            continue
        text = _output(corr)
        if not text.strip():
            missing.append(corr)
            continue
        name = f"part{n}-rewrite.md"
        (d / name).write_text(text, encoding="utf-8", newline="\n")
        wrote.append(name)
    _trace("review.kept", run=rid, panes=len(wrote), missing=len(missing))
    return {"id": rid, "wrote": wrote, "missing": missing, "root": str(d)}


def review_run(run_id: str) -> dict:
    d = _run_dir(run_id)
    panes = _review_panes(d)
    tl = d / "TIMELINE.txt"
    return {"id": d.name, "panes": panes, "rewrite": _rewrite_pane(panes),
            "timeline": (tl.read_text(encoding="utf-8", errors="replace")
                         if tl.is_file() else ""),
            "verdicts": (review_verdicts(d.name).get("verdicts") or {})}


def _verdicts_path(run_id: str) -> Path:
    return projects_root().parent / "review" / f"{run_id}.json"



NOTES_MAX_BYTES = 1_000_000


def _notes_path() -> Path:
    return projects_root().parent / "NOTES.md"


def notes_read() -> dict:
    p = _notes_path()
    try:
        text = p.read_text(encoding="utf-8")
        at = round(p.stat().st_mtime, 3)
    except (OSError, ValueError):
        text, at = "", None
    return {"text": text, "path": str(p), "at": at,
            "bytes": len(text.encode("utf-8")), "max_bytes": NOTES_MAX_BYTES}


def notes_write(text) -> dict:
    body = "" if text is None else str(text)
    n = len(body.encode("utf-8"))
    if n > NOTES_MAX_BYTES:
        return {"error": f"that is {n} bytes and the notepad holds "
                         f"{NOTES_MAX_BYTES} - keep it as a file instead",
                "bytes": n}
    p = _notes_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        old = p.read_text(encoding="utf-8") if p.is_file() else None
        if old is not None and old != body:
            _atomic_text(p.with_suffix(".prev.md"), old)
        _atomic_text(p, body)
    except OSError as e:
        return {"error": str(e)[:200], "bytes": n}
    return {"ok": True, "bytes": n, "path": str(p),
            "at": round(p.stat().st_mtime, 3)}


def _deleted_dir() -> Path:
    return projects_root().parent / "Deleted_backup"


def _fname_bit(raw, n: int = 40) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", str(raw or "").strip()).strip("-._")
    return (s or "x")[:n]


def deleted_write(project, story, what, where, text, meta=None) -> dict:
    who = f"{_fname_bit(project)}/{_fname_bit(story)}"
    body = "" if text is None else str(text)
    words = len(body.split())
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = (_deleted_dir() / who
            / f"{stamp}-{_fname_bit(what, 16)}-{_fname_bit(where)}")
    try:
        (_deleted_dir() / who).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"error": f"the backup could not be written, so nothing was "
                         f"deleted: {str(e)[:160]}"}
    p, n = base.with_suffix(".md"), 1
    while True:
        try:
            os.close(os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
            break
        except FileExistsError:
            n += 1
            p = base.with_name(f"{base.name}-{n}").with_suffix(".md")
            if n > 10000:
                return {"error": "the backup could not be given a free name"}
        except OSError as e:
            return {"error": f"the backup could not be written, so nothing was "
                             f"deleted: {str(e)[:160]}"}
    head = (f"# DELETED {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"- project: {project}\n- story: {story}\n"
            f"- what: {what}\n- where: {where}\n- words: {words}\n")
    if meta:
        head += "".join(f"- {k}: {v}\n" for k, v in dict(meta).items())
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        _atomic_text(p, head + "\n" + body.strip() + "\n")
    except OSError as e:
        return {"error": f"the backup could not be written, so nothing was "
                         f"deleted: {str(e)[:160]}"}
    return {"ok": True, "path": str(p), "words": words,
            "bytes": len(body.encode("utf-8"))}


def review_verdicts(run_id: str) -> dict:
    f = _verdicts_path(run_id)
    try:
        doc = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"run": run_id, "verdicts": {}}
    if not isinstance(doc, dict):
        return {"run": run_id, "verdicts": {}}
    doc.setdefault("run", run_id)
    if not isinstance(doc.get("verdicts"), dict):
        doc["verdicts"] = {}
    return doc


def review_verdict_write(run_id: str, pane: str, verdict: str,
                         comment: str = "") -> dict:
    d = _run_dir(run_id)
    pane = str(pane or "").strip()
    if not any(p["id"] == pane for p in _review_panes(d)):
        raise ValueError(f"no such pane in {run_id}: {pane!r}")
    v = str(verdict or "").strip()
    if v not in REVIEW_VERDICTS:
        raise ValueError(f"verdict must be one of {list(REVIEW_VERDICTS)}, "
                         f"not {v!r}")
    doc = review_verdicts(run_id)
    prev = doc["verdicts"].get(pane)
    row = {"verdict": v, "comment": str(comment or "").strip()[:2000],
           "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    if prev:
        row["was"] = {k: prev.get(k) for k in ("verdict", "comment", "at")}
    doc["verdicts"][pane] = row
    f = _verdicts_path(run_id)
    f.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(f, doc)
    _trace("review.verdict", run=run_id, pane=pane, verdict=v,
           commented=bool(row["comment"]))
    return doc


def _next_doctrine_id(text: str, prefix: str = "R") -> str:
    used = {int(m) for m in re.findall(rf"^{prefix}(\d{{1,3}})\s", text or "",
                                       re.M)}
    n = 1
    while n in used:
        n += 1
    return f"{prefix}{n}"


def review_promote(run_id: str, pane: str, pid: str, text: str,
                   story: str = None, flag: str = "ADVISORY",
                   rides: str = "write_part") -> dict:
    _run_dir(run_id)
    body = " ".join(str(text or "").split())
    if len(body) < 8:
        raise ValueError("a doctrine line has to say something - "
                         "write the instruction, not the reaction")
    if flag not in ("TESTABLE", "ADVISORY"):
        raise ValueError("a doctrine line is TESTABLE or ADVISORY")
    proj = _project_dir(pid)
    d = (proj / "stories" / story / DOCTRINE_DIRNAME) if story \
        else (proj / DOCTRINE_DIRNAME)
    f = d / "prose.md"
    seeded = ""
    if f.is_file():
        existing = f.read_text(encoding="utf-8")
    else:
        current, source = doctrine_for(_project_id(pid), story)
        existing = (current.get("prose") or {}).get("text") or ""
        if existing.strip():
            seeded = source
    line = (f"{_next_doctrine_id(existing)} {flag} feedback:review-{run_id} "
            f"[rides:{rides}] {body}")
    out = (existing.rstrip("\n") + "\n" if existing.strip() else "")
    if seeded:
        out = ("# Seeded from the " + seeded + " doctrine when the first "
               "review promotion landed - without this, creating this file "
               "would have retired every line that was riding.\n\n" + out)
    out += line + "\n"
    d.mkdir(parents=True, exist_ok=True)
    _atomic_text(f, out)
    _trace("review.promoted", run=run_id, pane=pane, project=pid,
           story=story, line=line[:120], seeded=seeded or None)
    return {"line": line, "path": str(f), "seeded_from": seeded or None,
            "version": E.doctrine_version(out) if E else ""}




def _doctor_root() -> Path:
    return projects_root().parent


def _workspace_root() -> Path:
    given = _doctor_root()
    if (given / "loom" / "projects").is_dir() or (given / "loom").is_dir():
        return given
    return given.parent


def doctor_verbs() -> dict:
    if loom_repair is None:
        return {}
    return {name: {"target": spec["target"],
                   "opts": sorted(set(spec["opts"]) - {"workspace"})}
            for name, spec in sorted(loom_repair.VERBS.items())}


def _same_path(a, b) -> bool:
    if not a or not b:
        return False
    try:
        return Path(str(a)).resolve() == Path(str(b)).resolve()
    except OSError:
        return (str(a).replace("\\", "/").lower()
                == str(b).replace("\\", "/").lower())


def _is_under(child, parent) -> bool:
    if not child or not parent:
        return False
    try:
        return Path(str(parent)).resolve() in Path(str(child)).resolve().parents
    except OSError:
        return False


def _doctor_named_targets(verb: str) -> list:
    if loom_doctor is None:
        return []
    try:
        rep = loom_doctor.report(_doctor_root())
    except Exception as e:
        _trace("doctor.named_targets_failed", verb=verb, err=str(e)[:160])
        return []
    out = []
    for f in rep.get("findings") or []:
        if f.get("fix") == verb and f.get("target"):
            try:
                out.append(Path(str(f["target"])).resolve())
            except OSError:
                continue
    return out


def _fix_target(verb: str, raw):
    raw = str(raw or "").strip()
    if not raw:
        return None, "a fix needs a target - the path the finding names."
    try:
        d = Path(raw.replace("\\", "/")).expanduser().resolve()
    except OSError as e:
        return None, f"that target cannot be resolved: {e}"
    ws = _workspace_root()
    root = ws.resolve() if ws.exists() else ws
    if d == root or root in d.parents:
        return d, None
    for named in _doctor_named_targets(verb):
        if named == d:
            return d, None
    return None, (f"{d} resolves outside the workspace at {root}, and the "
                  f"report does not name it for {verb}, so nothing here will "
                  f"touch it.")


def _fix_opts(verb: str, body: dict):
    if "workspace" in body:
        return None, ("workspace is not yours to choose - adopt_runs moves "
                      "into the workspace this server is bound to.")
    spec = loom_repair.VERBS[verb]["opts"]
    mine = sorted(set(spec) - {"workspace"})
    every = {k for s in loom_repair.VERBS.values()
             for k in s["opts"]} - {"workspace"}
    opts = {}
    for k in sorted(every):
        if k not in body:
            continue
        if k not in spec:
            return None, (f"{verb} does not take {k} - "
                          + (f"it takes {', '.join(mine)}." if mine
                             else "it takes no options."))
        if spec[k] is bool and not isinstance(body[k], bool):
            return None, f"{k} is true or false, not {body[k]!r}."
        opts[k] = body[k]
    if "workspace" in spec:
        opts["workspace"] = str(_workspace_root())
    return opts, None


def _doctor_container(rep: dict, want: dict):
    projects = rep.get("projects") or []
    pid, sid = want.get("project"), want.get("story")
    if sid:
        for p in projects:
            if pid and p["id"] != pid:
                continue
            for s in p["stories"]:
                if s["id"] == sid:
                    return "story", p, s
        return "story", next((p for p in projects if p["id"] == pid), None), None
    if pid:
        return "project", next((p for p in projects if p["id"] == pid),
                               None), None
    t = want.get("target")
    for p in projects:
        for s in p["stories"]:
            if _same_path(t, s["path"]) or _is_under(t, s["path"]):
                return "story", p, s
        if _same_path(t, p["path"]) or _is_under(t, p["path"]):
            return "project", p, None
    return "workspace", None, None


def _doctor_same_row(rows: list, want: dict):
    fid, where = want.get("id"), want.get("where")
    if fid:
        same = [f for f in rows if f.get("id") == fid]
        if where:
            hit = [f for f in same if _same_path(f.get("where"), where)]
            return (hit[0] if hit else None), True
        return (same[0] if same else None), len(same) <= 1
    verb, target = want.get("verb"), want.get("target")
    hit = [f for f in rows
           if f.get("fix") == verb and _same_path(f.get("target"), target)]
    return (hit[0] if hit else None), len(hit) <= 1


def _doctor_recheck(rep: dict, want: dict) -> dict:
    scope, proj, story = _doctor_container(rep, want)
    if scope == "story":
        rows, gone = (story["findings"] if story else []), story is None
    elif scope == "project":
        rows, gone = (proj["findings"] if proj else []), proj is None
    else:
        rows, gone = (rep.get("workspace_findings") or []), False
    hit, exact = _doctor_same_row(rows, want)
    return {"scope": scope, "gone": gone, "finding": hit,
            "cleared": hit is None, "exact": exact, "findings": rows,
            "story": story,
            "project": ({k: v for k, v in proj.items() if k != "stories"}
                        if proj else None)}


def doctor_report() -> dict:
    if loom_doctor is None:
        return {"error": "the Loading tab needs tools/loom_doctor.py, which "
                         "this server could not import.", "missing": True}
    try:
        out = loom_doctor.report(_doctor_root())
    except Exception as e:
        return {"error": f"the doctor failed while reading {_doctor_root()}: "
                         f"{type(e).__name__}: {e}",
                "crashed": True, "root": str(_doctor_root())}
    out["verbs"] = doctor_verbs()
    _trace("doctor.report", root=out.get("workspace"),
           stories=(out.get("totals") or {}).get("stories"),
           words=(out.get("totals") or {}).get("words"),
           findings=(out.get("counts") or {}).get("findings"),
           prose=out.get("prose"), plot=out.get("plot"))
    return out


def doctor_fix(body: dict) -> dict:
    if loom_doctor is None or loom_repair is None:
        which = ("tools/loom_doctor.py" if loom_doctor is None
                 else "tools/loom_repair.py")
        return {"error": f"the Loading tab needs {which}, which this server "
                         f"could not import.", "missing": True}
    if not isinstance(body, dict):
        return {"error": "a fix is an object: {verb, target}."}

    verb = str(body.get("verb") or "").strip()
    if verb not in loom_repair.VERBS:
        return {"error": (f"there is no verb called {verb!r}. The verbs are: "
                          f"{', '.join(sorted(loom_repair.VERBS))}."),
                "verb": verb, "verbs": doctor_verbs()}

    target, refusal = _fix_target(verb, body.get("target"))
    if refusal:
        return {"error": refusal, "verb": verb,
                "target": str(body.get("target") or ""),
                "workspace": str(_workspace_root()), "verbs": doctor_verbs()}

    opts, refusal = _fix_opts(verb, body)
    if refusal:
        return {"error": refusal, "verb": verb, "target": str(target),
                "verbs": doctor_verbs()}

    try:
        result = loom_repair.run(verb, str(target), **opts)
    except Exception as e:
        _trace("doctor.fix_crashed", verb=verb, target=str(target),
               err=f"{type(e).__name__}: {e}")
        return {"error": f"{verb} failed on {target}: {type(e).__name__}: {e}",
                "verb": verb, "target": str(target), "crashed": True}

    out = {"ok": bool(result.get("ok")), "verb": verb, "target": str(target),
           "opts": {k: v for k, v in opts.items() if k != "workspace"},
           "result": result}
    if result.get("refused"):
        out["refused"] = result["refused"]
    _trace("doctor.fix", verb=verb, target=str(target), ok=out["ok"],
           opts=(",".join(f"{k}={v}" for k, v in sorted(opts.items())) or None),
           refused=result.get("refused"))

    want = {"verb": verb, "target": str(target),
            "id": str(body.get("id") or "").strip() or None,
            "project": str(body.get("project") or "").strip() or None,
            "story": str(body.get("story") or "").strip() or None,
            "where": str(body.get("where") or "").strip() or None}
    try:
        rep = loom_doctor.report(_doctor_root())
    except Exception as e:
        out["crashed"] = True
        out["error"] = (f"{verb} ran and its result is below, but the re-check "
                        f"failed: {type(e).__name__}: {e}")
        return out
    out.update(_doctor_recheck(rep, want))
    out.update({"generated": rep.get("generated"),
                "headline": rep.get("headline"),
                "prose": rep.get("prose"), "plot": rep.get("plot"),
                "counts": rep.get("counts"), "totals": rep.get("totals"),
                "report_ok": bool(rep.get("ok"))})
    return out


def _doctor_status(out: dict, reading: bool = False) -> int:
    if out.get("missing"):
        return 503
    if out.get("crashed"):
        return 500
    return 200 if reading else (400 if out.get("error") else 200)


def _no_project_is_a_conflict(method):
    def answer(self):
        try:
            return method(self)
        except NoProjectLoaded as e:
            return self._json({"error": str(e), "no_project": True}, 409)
    return answer


class Handler(SimpleHTTPRequestHandler):
    studio_url: str | None = None

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(HERE), **kw)

    def _relay(self, method: str) -> None:
        raw_in = b""
        if (n := int(self.headers.get("Content-Length") or 0)) > 0:
            raw_in = self.rfile.read(n)
        if self.studio_url:
            req = urllib.request.Request(self.studio_url + self.path,
                                         data=raw_in or None, method=method)
            if raw_in:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=1800) as r:
                    payload, status = r.read(), r.status
                    ctype = r.headers.get("Content-Type", "application/json")
            except urllib.error.HTTPError as e:
                payload, status = e.read(), e.code
                ctype = e.headers.get("Content-Type", "application/json")
            except urllib.error.URLError as e:
                payload = json.dumps({
                    "error": "the model source is unreachable at " + self.studio_url,
                    "detail": str(e.reason)}).encode()
                status, ctype = 503, "application/json"
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        try:
            body = json.loads(raw_in or b"{}")
        except ValueError:
            body = {}
        try:
            out = (_local_runner.post(self.path, body) if method == "POST"
                   else _local_runner.get(self.path))
            status = 200
        except LocalHTTPish as e:
            out, status = {"detail": e.detail}, e.code
        if isinstance(out, str):
            payload, ctype = out.encode("utf-8"), "text/plain; charset=utf-8"
        else:
            payload, ctype = json.dumps(out).encode("utf-8"), "application/json"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, obj, status=200):
        payload = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else {}

    def _q(self, key, default=None):
        q = urllib.parse.urlparse(self.path).query
        return urllib.parse.parse_qs(q).get(key, [default])[0]

    def _sid(self, body=None):
        return _story_id((body or {}).get("story") or self._q("story") or DEFAULT_STORY)

    def _pid(self, body=None):
        return _project_id((body or {}).get("project") or self._q("project") or DEFAULT_PROJECT)

    def _scope(self, body=None):
        return self._pid(body), self._sid(body)

    def _extract(self, method: str) -> None:
        try:
            if method == "GET":
                out = _ex.get(self.path)
            else:
                try:
                    body = self._body()
                except ValueError:
                    return self._json({"error": "bad json"}, 400)
                out = _ex.post(self.path, body)
        except _ex.local.HTTPish as e:
            return self._json({"error": e.message}, e.status)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        return self._json(out)

    def _extractor_asset(self) -> None:
        rel = self.path.split("?", 1)[0].lstrip("/") or "extractor.html"
        target = (_EXTRACTOR_STATIC / rel).resolve()
        if (not str(target).startswith(str(_EXTRACTOR_STATIC.resolve()))
                or not target.is_file()):
            return self._json({"error": f"no such asset: {rel}"}, 404)
        ctype = ("text/html; charset=utf-8" if target.suffix == ".html"
                 else "text/javascript" if target.suffix == ".js"
                 else "text/css" if target.suffix == ".css"
                 else "application/octet-stream")
        payload = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _relay_to(self, base: str, method: str) -> None:
        url = base + self.path
        body = None
        if (n := int(self.headers.get("Content-Length") or 0)) > 0:
            body = self.rfile.read(n)
        req = urllib.request.Request(url, data=body, method=method)
        if body is not None:
            req.add_header("Content-Type", self.headers.get("Content-Type",
                                                            "application/json"))
        try:
            with urllib.request.urlopen(req, timeout=1800) as r:
                payload, status = r.read(), r.status
                ctype = r.headers.get("Content-Type", "application/octet-stream")
        except urllib.error.HTTPError as e:
            payload, status = e.read(), e.code
            ctype = e.headers.get("Content-Type", "application/json")
        except urllib.error.URLError as e:
            payload = json.dumps({"error": "extractor unreachable at " + base,
                                  "detail": str(e.reason),
                                  "hint": "start it: cd story-extractor && "
                                          "py -3.12 extractor-server.py --port 8789"}).encode()
            status, ctype = 503, "application/json"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    @_no_project_is_a_conflict
    def do_GET(self):
        if self.path.startswith("/extractor"):
            return self._extractor_asset()
        if self.path.startswith("/extract"):
            return self._extract("GET")
        if self.path.startswith("/loom/projects"):
            return self._json({"projects": projects_list()})
        if self.path.startswith("/loom/workspace"):
            return self._json({**workspace_binding(), "repo": str(REPO)})
        if self.path.startswith("/loom/recipes"):
            return self._json({"recipes": recipes_list(self._pid())})
        if self.path.startswith("/loom/stages"):
            return self._json({"stages": [
                {"stage": s["stage"], "label": s["label"], "budget": s["budget"]}
                for s in (E.STAGE_ASKS if E is not None else [])]})
        if self.path.startswith("/loom/asks"):
            return self._json(asks_overview(*self._scope()))
        if self.path.startswith("/loom/studies"):
            return self._json({"studies": studies_list(self._pid())})
        if self.path.startswith("/loom/topics"):
            return self._json(topics_read(*self._scope()))
        if self.path.startswith("/loom/extracts/runs"):
            return self._json({"runs": extracts_runs(self._pid())})
        if self.path.startswith("/loom/extracts/suggestions"):
            return self._json(_suggestions_read(self._pid()))
        if self.path.startswith("/loom/study"):
            return self._json(study_read(self._pid(),
                                         str(self._q("id") or "")))
        if self.path.startswith("/loom/tests/rubric"):
            return self._json(tests_rubric(self._pid(), self._q("story")))
        if self.path.startswith("/loom/tests/corpus"):
            return self._json(tests_corpus(self._pid()))
        if self.path.startswith("/loom/tests/runs"):
            return self._json({"runs": tests_runs(self._pid())})
        if self.path.startswith("/loom/tests/run"):
            return self._json(tests_run_read(self._pid(),
                                             str(self._q("id") or "")))
        if self.path.startswith("/loom/ingest"):
            return self._json(ingest_report(self._pid()))
        if self.path.startswith("/loom/artifacts"):
            pid = self._pid()
            return self._json({
                "kinds": [{"id": k, "title": v["title"], "pool": v["id"],
                           "want": v["want"]}
                          for k, v in sorted(ARTIFACT_KINDS.items())],
                "artifacts": artifact_pools(pid),
                "studies": artifact_studies(pid)})
        if self.path.startswith("/loom/playbooks"):
            try:
                return self._json(playbooks_list(*self._scope()))
            except loom_routing.RouteError as e:
                return self._json({"error": str(e)}, 400)
        if self.path.startswith("/loom/notes"):
            return self._json(notes_read())
        if self.path.startswith("/loom/doctor"):
            if self.path.split("?")[0].rstrip("/") != "/loom/doctor":
                return self._json({"error": "GET /loom/doctor is the reading; "
                                            "a repair is POST /loom/doctor/fix "
                                            "{verb, target}."}, 404)
            out = doctor_report()
            return self._json(out, _doctor_status(out, reading=True))
        if self.path.startswith("/loom/review/runs"):
            return self._json({"runs": review_runs()})
        if self.path.startswith("/loom/review/run"):
            try:
                return self._json({"run": review_run(self._q("id"))})
            except (ValueError, FileNotFoundError) as e:
                return self._json({"error": str(e)}, 404)
        if self.path.startswith("/loom/review/verdicts"):
            return self._json(review_verdicts(str(self._q("id") or "")))
        if self.path.startswith("/loom/pools/read"):
            pid, poolid = self._pid(), _pool_id(self._q("id"))
            e = next((x for x in pools_list(pid) if x["id"] == poolid), None)
            if e is None or not e["versions"]:
                return self._json({"error": "no such pool", "id": poolid}, 404)
            raw = self._q("v")
            v = int(raw) if (raw or "").isdigit() else e["current"]
            if v not in e["versions"]:
                return self._json({"error": "no such version",
                                   "have": e["versions"]}, 404)
            return self._json({**e, "v": v, "text": _pool_text(pid, poolid, v)})
        if self.path.startswith("/loom/pools"):
            return self._json({"pools": pools_list(self._pid())})
        if self.path.startswith("/loom/stories"):
            return self._json({"stories": stories_list(self._pid())})
        if self.path.startswith("/loom/project"):
            return self._json(_load_project(*self._scope()))
        if self.path.startswith("/loom/jobs"):
            parts = self.path.split("?")[0].strip("/").split("/")
            if len(parts) == 4 and parts[3] == "result":
                j = _get_job(parts[2])
                if not j:
                    return self._json({"error": "no such job"}, 404)
                out = {"job": _job_public(j),
                       "candidates": _peek_candidates(j)}
                summ = (j.get("result") or {}).get("summary")
                if summ is not None:
                    out["summary"] = summ
                return self._json(out)
            if len(parts) == 4 and parts[3] == "prompt":
                j = _get_job(parts[2])
                if not j:
                    return self._json({"error": "no such job"}, 404)
                corr = str(j.get("corr") or "")
                if not corr:
                    return self._json({"error": "this job has no corr, so it "
                                                "has no request folder"}, 404)
                d = _req_dir(str(j.get("project") or ""), corr) / "prompts"
                files = sorted(p.name for p in d.glob("*.txt")) if d.is_dir() else []
                if not files:
                    return self._json({"error": "no prompt was recorded for "
                                                f"{corr}"}, 404)
                raw = (d / files[0]).read_text(encoding="utf-8")
                return self._json({"corr": corr, "hash": files[0][:-4],
                                   "files": files, "text": raw,
                                   "lines": len(raw.splitlines()),
                                   "blocks": _prompt_blocks(raw)})
            pid, sid = self._scope()
            proj_only = (self._q("scope") or "").lower() == "project"
            jobs = [_job_public(j) for j in _load_jobs()["jobs"]
                    if j.get("story") is None
                    or ((proj_only
                         or _story_id(j.get("story")).lower() == sid.lower())
                        and _project_id(j.get("project")).lower() == pid.lower())]
            return self._json({"jobs": jobs})
        if self.path.startswith("/loom/files/manuscript"):
            return self._json({"text": _store(*self._scope()).manuscript()})
        if self.path.startswith("/loom/files/list"):
            return self._json({"parts": _store(*self._scope()).list()})
        if self.path.startswith("/loom/health"):
            return self._json({"ok": True,
                               "studio": self.studio_url or "in-process",
                               **workspace_binding(),
                               "trace": str(TRACE_DIR) if TRACE_DIR else None,
                               "tracing": TRACE,
                               "worker": worker_health(),
                               "pick_fault": _fault_aged(pick_fault(),
                                                         time.time()),
                               "generation": generation_health()})
        if self.path.startswith("/api/"):
            return self._relay("GET")
        return super().do_GET()

    @_no_project_is_a_conflict
    def do_POST(self):
        if self.path.startswith("/extract"):
            return self._extract("POST")
        if self.path.startswith("/loom/extracts/runs"):
            try:
                b = self._body()
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            parts = self.path.split("?")[0].strip("/").split("/")
            if len(parts) != 5 or parts[4] != "retry":
                return self._json({"error": "unknown run action"}, 404)
            out = extracts_retry(self._pid(b), parts[3])
            return self._json(out, 400 if out.get("error") else 200)
        if self.path.startswith("/loom/asks"):
            try:
                b = self._body()
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            parts = self.path.split("?")[0].strip("/").split("/")
            action = parts[2] if len(parts) > 2 else ""
            pid, sid = self._scope(b)
            try:
                if action == "save":
                    out = ask_save_at(pid, sid, str(b.get("name") or ""),
                                      str(b.get("text") or ""),
                                      str(b.get("level") or ""))
                elif action == "sample":
                    out = ask_sample(pid, sid, b)
                else:
                    return self._json(
                        {"error": f"unknown ask action {action!r} - this "
                                  f"route takes save or sample"}, 404)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            return self._json(out)
        if self.path.startswith("/loom/extracts/plan"):
            try:
                b = self._body()
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            out = extracts_plan(b)
            return self._json(out, 400 if out.get("error") else 200)
        if self.path.startswith("/loom/extracts/suggestions"):
            try:
                b = self._body()
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            state = str(b.get("state") or "")
            if state not in ("open", "checked", "dropped"):
                return self._json({"error": "state must be open, checked "
                                            "or dropped"}, 400)
            pid = self._pid(b)
            doc = _suggestions_read(pid)
            hit = next((x for x in doc.get("suggestions") or []
                        if x.get("id") == b.get("id")), None)
            if hit is None:
                return self._json({"error": "no such suggestion"}, 404)
            hit["state"] = state
            _suggestions_write(pid, doc)
            return self._json({"ok": True, "id": hit["id"], "state": state})
        if self.path.startswith("/loom/extracts/suggest"):
            try:
                b = self._body()
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            out = extracts_suggest(self._pid(b), b)
            return self._json(out, 400 if out.get("error") else 200)
        if self.path.startswith("/loom/topics"):
            try:
                b = self._body()
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            out = topics_save(*self._scope(b), b)
            return self._json(out, 400 if out.get("error") else 200)
        if self.path.startswith("/loom/study/counts"):
            try:
                b = self._body()
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            out = study_counts(b)
            return self._json(out, 400 if out.get("error") else 200)
        if self.path.startswith("/loom/study"):
            try:
                b = self._body()
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            out = study_start(self._pid(b), b)
            return self._json(out, 400 if out.get("error") else 200)
        if self.path.startswith("/loom/tests/corpus"):
            try:
                b = self._body()
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            out = tests_pin(self._pid(b), str(b.get("corr") or ""),
                            bool(b.get("pin", True)))
            return self._json(out, 400 if out.get("error") else 200)
        if self.path.startswith("/loom/tests/run"):
            try:
                b = self._body()
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            out = tests_run_start(self._pid(b), b.get("story"),
                                  b.get("gen") or {})
            return self._json(out, 400 if out.get("error") else 200)
        if self.path.startswith("/loom/projects"):
            try:
                b = self._body()
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            title = (b.get("title") or "").strip()
            if not title:
                return self._json({"error": "a project needs a title"}, 400)
            pid = _project_id(b.get("id") or title)
            if _project_paths(pid)["meta"].exists():
                n = 2
                while _project_paths(f"{pid}-{n}")["meta"].exists():
                    n += 1
                pid = f"{pid}-{n}"
            _project_dir(pid)
            _project_paths(pid)["stories"].mkdir(parents=True, exist_ok=True)
            _project_paths(pid)["pools"].mkdir(parents=True, exist_ok=True)
            meta = _write_project_meta(pid, title=title, notes=b.get("notes"))
            return self._json({"project": meta})
        if self.path.startswith("/loom/workspace/unload"):
            try:
                return self._json(unload_data_root())
            except DataRootBusy as e:
                return self._json({"error": str(e), "busy": True}, 409)
        if self.path.startswith("/loom/workspace/pick"):
            path, err = _pick_folder_natively()
            if err:
                return self._json({"error": err}, 500)
            if not path:
                return self._json({"cancelled": True})
            return self._json({"path": path})
        if self.path.startswith("/loom/workspace"):
            try:
                b = self._body()
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            path = str(b.get("path") or "").strip()
            if not path:
                return self._json({"error": "no folder given"}, 400)
            try:
                r = _workspace_switch(path)
            except DataRootBusy as e:
                return self._json({"error": str(e), "busy": True}, 409)
            return self._json(r, 400 if r.get("error") else 200)
        if self.path.startswith("/loom/texts/backfill"):
            try:
                b = self._body()
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            return self._json(texts_backfill(_project_id(b.get("project")),
                                             bool(b.get("dry"))))
        if self.path.startswith("/loom/requests/adopt"):
            try:
                b = self._body()
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            return self._json(requests_adopt(_project_id(b.get("project")),
                                             bool(b.get("dry"))))
        if self.path.startswith("/loom/compile/preview"):
            try:
                b = self._body()
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            pid, sid = self._scope(b)
            inp = json.loads(json.dumps(b.get("inputs") or {}))
            gen = json.loads(json.dumps(b.get("gen") or {}))
            pbv = None
            if str(b.get("playbook") or "").strip():
                try:
                    _pseal = _playbook_seal(b)
                    _papp = loom_playbooks.apply_to(
                        {"settings": _pseal.get("playbook_settings") or {}},
                        gen, inp.setdefault("structured", {}))
                    pbv = {"label": _pseal.get("playbook"),
                           "ref": _pseal.get("playbook_ref"),
                           "sha256": _pseal.get("playbook_sha256"),
                           "settings": _pseal.get("playbook_settings") or {},
                           "runbook": _pseal.get("runbook"),
                           "runbook_sha256": _pseal.get("runbook_sha256"),
                           "applied": _papp}
                except Exception as e:
                    return self._json({"error": f"{e}"}, 400)
            job = {"kind": b.get("kind") or "write_part", "project": pid,
                   "story": sid, "inputs": inp, "gen": gen,
                   "n": max(1, int(gen.get("n") or 1))}
            try:
                prompts = _apply_prompt_suffix(job, _prompts_for(job))
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 400)
            pcs, used, detail = ([], [], []) \
                if job["kind"] in POOL_LESS_KINDS \
                else project_context_for(
                    pid, inp.get("pools"),
                    E.step_of(job["kind"], inp.get("structured") or {}),
                    detail=True)
            fit = _fit(prompts[0] if prompts else "", gen.get("model")
                       or _default_model(), gen.get("max_tokens") or 1600,
                       job["kind"])
            return self._json({
                "kind": job["kind"], "project": pid, "story": sid,
                "pools_used": used,
                "pools_detail": detail, "fit": fit,
                "sampling": _sampling_for_job(job, inp.get("structured") or {}),
                "doctrine": _preview_doctrine(job["kind"], pid, sid,
                                              inp.get("structured") or {}),
                "conflicts": _preview_conflicts(job["kind"], pid, sid,
                                                inp.get("structured") or {},
                                                pcs),
                "prompts": prompts,
                "chars": [len(p) for p in prompts],
                "approx_tokens": [len(p) // 4 for p in prompts],
                "blocks": [[blk.split("\n", 1)[0][:72] for blk in p.split(E.DIVIDER)]
                           for p in prompts] if E is not None else [],
                "losses": preview_losses(job["kind"],
                                         (inp.get("structured") or {}),
                                         prompts[0] if prompts else ""),
                "playbook": pbv})
        if self.path.startswith("/loom/recipes"):
            try:
                b = self._body()
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            r = recipe_run(self._pid(b), str(b.get("recipe") or ""),
                           b.get("pools"), str(b.get("instruction") or ""),
                           b.get("model"), jobstory=self._sid(b))
            return self._json(r, 400 if r.get("error") else 200)
        if self.path.startswith("/loom/ingest"):
            try:
                b = self._body()
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            pid = self._pid(b)
            if self.path.startswith("/loom/ingest/apply"):
                r = ingest_apply(pid, str(b.get("job") or ""),
                                 int(b.get("index") or 0))
            else:
                r = ingest_merge(pid, str(b.get("extractor_story")
                                          or b.get("story") or ""),
                                 str(b.get("dim") or ""), bool(b.get("ai")),
                                 str(b.get("instruction") or ""),
                                 jobstory=self._sid(b), model=b.get("model"))
            return self._json(r, 400 if r.get("error") else 200)
        if self.path.startswith("/loom/artifacts/build"):
            try:
                b = self._body()
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            pid, sid = self._scope(b)
            out = artifact_build(pid, which=b.get("which", ""),
                                 pool=b.get("pool", ""),
                                 studies=b.get("studies") or [],
                                 instruction=b.get("instruction", ""),
                                 model=b.get("model"), story=sid)
            return self._json(out, 400 if out.get("error") else 200)
        if self.path.startswith("/loom/notes"):
            try:
                b = self._body()
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            out = notes_write(b.get("text"))
            return self._json(out, 400 if out.get("error") else 200)
        if self.path.startswith("/loom/doctor"):
            if not self.path.startswith("/loom/doctor/fix"):
                return self._json({"error": "the reading is GET /loom/doctor; "
                                            "the only POST here is "
                                            "/loom/doctor/fix {verb, target}."},
                                  404)
            try:
                b = self._body()
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            out = doctor_fix(b)
            return self._json(out, _doctor_status(out))
        if self.path.startswith("/loom/playbooks"):
            try:
                b = self._body()
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            try:
                out = playbooks_save(*self._scope(b), b)
            except loom_routing.RouteError as e:
                return self._json({"error": str(e)}, 400)
            return self._json(out, 400 if out.get("error") else 200)
        if self.path.startswith("/loom/trace"):
            try:
                b = self._body()
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            pid, sid = self._pid(b), self._sid(b)
            _sdir = _story_dir(pid, sid, True)
            out = loom_trace.trace_append(_sdir, b.get("rows"))
            if not out.get("error"):
                out.update(loom_trace.texts_append(_sdir, b.get("texts")))
            return self._json(out, 400 if out.get("error") else 200)
        if self.path.startswith("/loom/deleted"):
            try:
                b = self._body()
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            if not str(b.get("what") or "").strip() \
                    or not str(b.get("where") or "").strip():
                return self._json({"error": "a backup needs to say WHAT was "
                                            "deleted and WHERE it came from"}, 400)
            out = deleted_write(self._pid(b), b.get("story") or "",
                                b.get("what"), b.get("where"), b.get("text"),
                                b.get("meta"))
            return self._json(out, 400 if out.get("error") else 200)
        if self.path.startswith("/loom/review/"):
            try:
                b = self._body()
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            try:
                if self.path.startswith("/loom/review/verdict"):
                    return self._json(review_verdict_write(
                        b.get("id"), b.get("pane"), b.get("verdict"),
                        b.get("comment", "")))
                if self.path.startswith("/loom/review/keep"):
                    out = review_keep(self._pid(b), b.get("story") or "",
                                      b.get("corrs") or {}, b.get("parts") or [],
                                      b.get("run_id") or "")
                    return self._json(out, 400 if out.get("error") else 200)
                if self.path.startswith("/loom/review/promote"):
                    return self._json(review_promote(
                        b.get("id"), b.get("pane"), self._pid(b),
                        b.get("text", ""), b.get("story") or None,
                        b.get("flag", "ADVISORY"),
                        b.get("rides", "write_part")))
            except (ValueError, FileNotFoundError) as e:
                return self._json({"error": str(e)}, 400)
            return self._json({"error": "bad review route"}, 404)
        if self.path.startswith("/loom/pools"):
            try:
                b = self._body()
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            pid = self._pid(b)
            if self.path.startswith("/loom/pools/context"):
                entries, used, detail = project_context_for(
                    pid, b.get("pools"),
                    E.step_of(b.get("kind"), b) if E is not None else "both",
                    detail=True)
                return self._json({"project_context": entries, "pools_used": used,
                                   "pools_detail": detail,
                                   "block": (E.project_context_block(entries)
                                             if E is not None else "")})
            poolid = _pool_id(b.get("id") or b.get("title"))
            if not (b.get("id") or b.get("title")):
                return self._json({"error": "a pool needs an id or a title"}, 400)
            if self.path.startswith("/loom/pools/meta"):
                e = pool_meta(pid, poolid, **b)
                return self._json({"pool": e}) if e else \
                    self._json({"error": "no such pool", "id": poolid}, 404)
            return self._json({"pool": pool_save(pid, poolid, b.get("text", ""),
                                                 **_pool_fields(b))})
        if self.path.startswith("/loom/stories"):
            try:
                b = self._body()
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            title = (b.get("title") or "").strip()
            if not title:
                return self._json({"error": "a story needs a title"}, 400)
            pid = self._pid(b)
            sid = _story_id(b.get("id") or title)
            if _story_paths(pid, sid)["project"].exists():
                n = 2
                while _story_paths(pid, f"{sid}-{n}")["project"].exists():
                    n += 1
                sid = f"{sid}-{n}"
            _story_dir(pid, sid, True)
            meta = _write_story_meta(pid, sid, title=title)
            _save_project(pid, sid, {"ops": [{"t": "reset_empty"}], "ptr": 1,
                                     "state": None})
            return self._json({"story": meta, "project": pid})
        if self.path.startswith("/loom/project"):
            try:
                b = self._body()
                return self._json(_save_project(*self._scope(b), b))
            except ValueError:
                return self._json({"error": "bad json"}, 400)
        if self.path.startswith("/loom/files/"):
            try:
                b = self._body()
                pid, sid = self._scope(b)
                s = _store(pid, sid)
                if self.path.startswith("/loom/files/accept"):
                    out = s.accept(b.get("chapter"), b.get("part"),
                                   b.get("text", ""), b.get("provenance"))
                    _record_pick(pid, b.get("provenance"), out)
                    return self._json(out)
                if self.path.startswith("/loom/files/replace_para"):
                    out = s.replace_para(b.get("chapter"), b.get("part"),
                                         int(b.get("para_index", 0)),
                                         b.get("text", ""), b.get("provenance"))
                    _record_pick(pid, b.get("provenance"), out)
                    return self._json(out)
                if self.path.startswith("/loom/files/read"):
                    return self._json(s.read(b.get("path")))
                if self.path.startswith("/loom/files/backfill"):
                    return self._json(_backfill(*self._scope(b), dry=bool(b.get("dry"))))
            except FileNotFoundError as e:
                return self._json({"error": "not found", "detail": str(e)}, 404)
            except Exception as e:
                return self._json({"error": "file store", "detail": f"{type(e).__name__}: {e}"}, 400)
            return self._json({"error": "bad files route"}, 404)
        if self.path.startswith("/loom/jobs"):
            parts = self.path.split("?")[0].strip("/").split("/")
            try:
                if len(parts) == 2:
                    return self._json(jobs_submit(self._body()))
                if len(parts) == 4 and parts[3] == "cancel":
                    return self._json(jobs_cancel(parts[2]))
                if len(parts) == 4 and parts[3] == "giveup":
                    return self._json(jobs_give_up(parts[2]))
                if len(parts) == 3 and parts[2] == "prune":
                    b = self._body()
                    return self._json(jobs_prune(b.get("status"), b.get("project"),
                                                 int(b.get("keep") or 0)))
                if len(parts) == 4 and parts[3] == "forget":
                    return self._json(jobs_forget(parts[2]))
                if len(parts) == 4 and parts[3] == "consume":
                    res, err = jobs_consume(parts[2])
                    return self._json(res) if res else self._json({"error": err}, 409)
            except loom_routing.RouteError as e:
                return self._json({"error": f"routing: {e}"}, 400)
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            return self._json({"error": "bad jobs route"}, 404)
        if self.path.startswith("/api/"):
            return self._relay("POST")
        self.send_error(405)

    def end_headers(self):
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    QUIET_POLLS = ("/api/status", "/loom/health", "/loom/jobs", "/loom/project?")

    def log_message(self, fmt, *args):
        line = str(args[0]) if args else ""
        code = str(args[1]) if len(args) > 1 else ""
        quiet = (line.startswith("GET ")
                 and any(q in line for q in self.QUIET_POLLS))
        if code[:1] in ("2", "3") and quiet:
            return
        super().log_message(fmt, *args)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8888)
    ap.add_argument("--studio", type=int, default=None,
                    help="TEST ONLY: talk HTTP to a fake model source on this "
                         "port instead of the in-process runner. The realstack "
                         "suites use it to inject faults a function call cannot "
                         "have. Absent in every real launch.")
    ap.add_argument("--page", default="index.html")
    ap.add_argument("--project", default=None, help="project json (test isolation)")
    ap.add_argument("--jobs", default=None, help="jobs json (test isolation)")
    ap.add_argument("--data", required=True,
                    help="REQUIRED. The loom workspace root; projects/, trace/ "
                         "and jobs.json are made inside it. There is no default "
                         "- a server with no workspace refuses to start rather "
                         "than writing into one nobody chose.")
    ap.add_argument("--extractor-data", default=None,
                    help="READ extractor scans from here. There is no default "
                         "either (R1): without this flag the Extract tab has no "
                         "root and says so, instead of reading a folder on C: "
                         "that this launch was never pointed at.")
    ap.add_argument("--trace", action="store_true",
                    help="cold-path detail: one line per step, and the FULL "
                         "prompt of every generation saved beside its metadata")
    a = ap.parse_args()
    if a.extractor_data:
        globals()["EXTRACTOR_OVERRIDE"] = Path(a.extractor_data)

    global JOBS, PROJECT_OVERRIDE, _STUDIO_URL
    if a.studio:
        _STUDIO_URL = f"http://127.0.0.1:{a.studio}"
        Handler.studio_url = _STUDIO_URL
        print(f"  runner        HTTP to 127.0.0.1:{a.studio} (TEST MODE — "
              "the in-process runner is bypassed)")
    if a.project:
        PROJECT_OVERRIDE = Path(a.project).resolve()
    if a.jobs:
        JOBS = Path(a.jobs).resolve()

    try:
        bind_data_root(a.data, queue_live=False)
    except (ValueError, OSError) as e:
        print(f"  workspace     REFUSED — {e}")
        raise SystemExit(2)

    try:
        st = _studio_get("/api/status")
        present = [m["key"] for m in st.get("models", []) if m.get("present")]
        print(f"  runner        in-process — backend={st.get('backend')}, "
              f"{len(present)} of {len(st.get('models', []))} model(s) on disk")
        if not present:
            print(f"                NO MODELS under {st.get('models_dir')} —"
                  " generation will fail until one is there")
    except Exception as e:
        print(f"  runner        FAILED TO START ({e})")
        print("                the lab will load, but nothing will generate")

    global TRACE
    print(f"  workspace     {projects_root().parent}")
    if getattr(a, "trace", False):
        TRACE = True
    if TRACE:
        print(f"  trace         ON \u2014 step lines below, full prompts in "
              f"{TRACE_DIR}")
    _ex_data = EXTRACTOR_OVERRIDE or EXTRACTOR_DATA
    if _ex_data is not None:
        _ex.configure(_ex_data, a.port)
    _ex.use_store(_load_jobs, _save_jobs, _JOBS_LOCK, _WAKE,
                  _recompute_queue_positions)
    _adopt_extract_jobs()
    _recover_jobs()
    _start_worker()
    threading.Thread(target=_supervise_worker, daemon=True,
                     name="loom-worker-watch").start()
    projs = projects_list()
    if not projs:
        _project_dir(DEFAULT_PROJECT, True)
        _project_paths(DEFAULT_PROJECT, True)["stories"].mkdir(
            parents=True, exist_ok=True)
        _write_project_meta(DEFAULT_PROJECT, title=DEFAULT_PROJECT)
        _save_project(DEFAULT_PROJECT, DEFAULT_STORY,
                      {"ops": [{"t": "reset_empty"}], "ptr": 1, "state": None})
        _write_story_meta(DEFAULT_PROJECT, DEFAULT_STORY, title=DEFAULT_STORY)
        projs = projects_list()
    print(f"  projects      {projects_root().name}/ — {len(projs)} "
          + f"project(s), "
          + f"{sum(p['story_count'] for p in projs)} story(ies)")
    for p in projs[:4]:
        print(f"                  {p['id']}: "
              + (", ".join(x["id"] for x in p["stories"][:3]) or "(no stories yet)"))
    q = _store_rows(_load_jobs())
    print(f"  jobs          {JOBS.name} — "
          + (f"{len(q)} in store" if q is not None
             else "NOT A JOB STORE - it parsed and has no jobs list")
          + f", {_worker_banner()}")

    if _ex_data is not None:
        _ex.configure(_ex_data, a.port)
    _ex_n = len([j for j in _load_jobs()["jobs"] if _ex.core.is_extract_job(j)])
    if _ex.core.DATA is None:
        print(f"  extractor     in-process — NO ROOT; pass --extractor-data to "
              f"read scans ({_ex_n} job(s) in the shared queue)")
    else:
        print(f"  extractor     in-process — {_ex.core.DATA} "
              f"({_ex_n} job(s) in the shared queue)")
    print(f"  lab           http://127.0.0.1:{a.port}/{a.page}")
    print("                  (the Extract tab is inside it - one door)")
    print("  stop          Ctrl-C\n")
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
