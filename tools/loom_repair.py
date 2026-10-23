from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PARTS = ROOT / "client" / "parts"

_SRV = None


def _server():
    global _SRV
    if _SRV is None:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        spec = importlib.util.spec_from_file_location(
            "loomsrv_repair", ROOT / "loom-server.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SRV = mod
    return _SRV


def _ws_of(path: Path):
    p = Path(path).resolve()
    for d in (p, *p.parents):
        if d.name == "projects" and d.parent.name == "loom":
            return d.parent.parent
    if (p / "loom" / "projects").is_dir():
        return p
    return None


def _fname_bit(raw, n: int = 40) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", str(raw or "").strip()).strip("-._")
    return (s or "x")[:n]


def _backup(path: Path, what: str, who: str) -> dict:
    ws = _ws_of(path)
    if ws is None:
        return {"error": f"no workspace above {path}, so there is nowhere to "
                         f"put a backup - nothing was replaced"}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    into = ws / "loom" / "Deleted_backup"
    for bit in str(who).split("/"):
        into = into / _fname_bit(bit)
    try:
        into.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"error": f"the backup could not be written, so nothing was "
                         f"replaced: {str(e)[:160]}"}
    stem = f"{stamp}-{_fname_bit(what, 24)}-{_fname_bit(path.stem)}"
    ext = path.suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", path.suffix or "") else ".bak"
    dst, n = into / (stem + ext), 1
    while True:
        try:
            os.close(os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
            break
        except FileExistsError:
            n += 1
            dst = into / f"{stem}-{n}{ext}"
            if n > 10000:
                return {"error": "the backup could not be given a free name"}
        except OSError as e:
            return {"error": f"the backup could not be written, so nothing "
                             f"was replaced: {str(e)[:160]}"}
    try:
        body = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            shutil.copyfile(path, dst)
        except OSError as e:
            return {"error": f"the backup could not be written, so nothing "
                             f"was replaced: {str(e)[:160]}"}
        return {"path": str(dst), "mode": "bytes", "bytes": dst.stat().st_size}
    except OSError as e:
        return {"error": f"the backup could not be written, so nothing was "
                         f"replaced: {str(e)[:160]}"}
    try:
        _server()._atomic_text(dst, body)
    except OSError as e:
        return {"error": f"the backup could not be written, so nothing was "
                         f"replaced: {str(e)[:160]}"}
    return {"path": str(dst), "mode": "text",
            "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()}


def _up_from(target, names, marker=None):
    p = Path(target)
    if p.name in names and p.parent.exists():
        return p.parent
    if marker and p.is_dir() and not (p / marker).exists() and (p.parent / marker).exists():
        return p.parent
    return p


def _no(verb, target, why) -> dict:
    return {"verb": verb, "target": str(target), "ok": False, "refused": why}


_BRIDGE_JS = r"""
'use strict';
const fs = require('fs');
const partsDir = process.argv[2], inPath = process.argv[3], outPath = process.argv[4];
const names = fs.readFileSync(partsDir + '/MANIFEST', 'utf8').split(/\r?\n/)
  .map(s => s.trim()).filter(s => s && !s.startsWith('#') && s.endsWith('.js'));
const src = names.map(n => fs.readFileSync(partsDir + '/' + n, 'utf8')).join('\n');

// EVERY part, in MANIFEST order, not just fold.js. Function declarations hoist
// across the whole concatenation, which is how `apply()` in fold.js reaches
// `nextOrd()` in writepart.js - a part that loads AFTER it. Slicing the file at
// the fold and hoping is how this failed the first time.
const stub = () => new Proxy(function () {}, {
  get: (t, k) => (k === 'then' ? undefined : stub()),
  set: () => true, apply: () => stub(), has: () => true,
});
for (const k of ['document', 'window', 'navigator', 'Element', 'Blob', 'URL',
                 'Event', 'KeyboardEvent', 'MouseEvent', 'PointerEvent',
                 'performance', 'matchMedia', 'getComputedStyle', 'alert',
                 'confirm', 'prompt', 'IntersectionObserver', 'ResizeObserver',
                 'MutationObserver', 'requestAnimationFrame',
                 'cancelAnimationFrame', 'addEventListener',
                 'removeEventListener', 'scrollTo', 'CSS', 'Range',
                 'getSelection', 'DOMParser', 'FileReader', 'FormData',
                 'AbortController', 'Node', 'HTMLElement', 'customElements',
                 'open', 'close', 'print', 'crypto']) {
  // defineProperty, not assignment: some of these (navigator) are getter-only
  // on the node global and a plain `globalThis.x =` throws.
  try { Object.defineProperty(globalThis, k, { value: stub(), configurable: true, writable: true }); }
  catch (e) { /* the app does not reach it; a stub it cannot have is not fatal */ }
}
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.sessionStorage = globalThis.localStorage;
// A fetch that never settles and timers that never fire: the parts are being
// loaded for their DECLARATIONS. Anything the app queues at load must not run,
// because there is no server here and no screen to render onto.
globalThis.fetch = () => new Promise(() => {});
globalThis.location = { protocol: 'http:', port: '8888', origin: '', search: '', href: '' };
globalThis.innerWidth = 1400; globalThis.innerHeight = 900;
globalThis.setTimeout = () => 0; globalThis.clearTimeout = () => {};
globalThis.setInterval = () => 0; globalThis.clearInterval = () => {};

const inp = JSON.parse(fs.readFileSync(inPath, 'utf8'));
const run = new Function('OPS_IN', 'PTR_IN', src + '\nreturn foldOps(OPS_IN, PTR_IN);');
const state = run(inp.ops, inp.ptr);
fs.writeFileSync(outPath, JSON.stringify({ ok: true, state: state }), 'utf8');
"""


def _fold_via_node(ops: list, ptr: int) -> dict:
    node = shutil.which("node")
    if not node:
        return {"error": "node is not on PATH, and the fold is the client's "
                         "own foldOps() - there is no second copy to use"}
    if not (PARTS / "MANIFEST").is_file():
        return {"error": f"the client parts are not at {PARTS}, so the fold "
                         f"cannot be read"}
    tmp = Path(tempfile.mkdtemp(prefix="loom-refold-"))
    try:
        js, src, dst = tmp / "bridge.js", tmp / "in.json", tmp / "out.json"
        js.write_text(_BRIDGE_JS, encoding="utf-8")
        src.write_text(json.dumps({"ops": ops, "ptr": ptr}), encoding="utf-8")
        try:
            r = subprocess.run([node, str(js), str(PARTS), str(src), str(dst)],
                               capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return {"error": "the fold did not finish within 120s"}
        if r.returncode != 0 or not dst.exists():
            msg = (r.stderr or r.stdout or "").strip().splitlines()
            return {"error": "the client's fold would not run: "
                             + (msg[0] if msg else f"exit {r.returncode}")}
        try:
            doc = json.loads(dst.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return {"error": f"the fold wrote something unreadable: {str(e)[:120]}"}
        if not isinstance(doc, dict) or not doc.get("ok"):
            return {"error": "the fold did not report success"}
        return {"ok": True, "state": doc.get("state")}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _prose_words(state) -> tuple:
    now = total = 0
    if not isinstance(state, dict):
        return 0, 0
    for c in (state.get("chapters") or []):
        if not isinstance(c, dict):
            continue
        for p in (c.get("parts") or []):
            if not isinstance(p, dict):
                continue
            vs = p.get("versions")
            if not isinstance(vs, list):
                continue
            i = p.get("current")
            for k, v in enumerate(vs):
                n = len(str((v or {}).get("text") or "").split()) \
                    if isinstance(v, dict) else 0
                total += n
                if isinstance(i, int) and k == i:
                    now += n
    return now, total


def refold(story_dir, allow_loss: bool = False) -> dict:
    d = _up_from(story_dir, ("state.json", "project.json"))
    proj, state_p = d / "project.json", d / "state.json"
    if not proj.is_file():
        return _no("refold", d, "there is no project.json here, and the fold "
                               "has nothing to be folded from")
    try:
        journal = json.loads(proj.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return _no("refold", d, f"project.json does not parse ({str(e)[:100]}), "
                                f"so the journal cannot be replayed - this is "
                                f"the fault the tab calls lost, and no verb "
                                f"here can repair it")
    if not isinstance(journal, dict):
        return _no("refold", d, f"project.json parses but holds a "
                                f"{type(journal).__name__}, not a journal")
    ops = journal.get("ops")
    if not isinstance(ops, list):
        return _no("refold", d, "project.json carries no ops list")
    ptr = journal.get("ptr", len(ops))
    if not isinstance(ptr, int) or not 0 <= ptr <= len(ops):
        return _no("refold", d, f"ptr is {ptr!r} against {len(ops)} ops, which "
                                f"is outside the journal - refusing to guess "
                                f"where the book ends")

    had_file = state_p.is_file()
    before, unreadable = None, False
    if had_file:
        try:
            doc = json.loads(state_p.read_text(encoding="utf-8"))
            unreadable = not isinstance(doc, dict)
            before = None if unreadable else doc.get("state")
        except (OSError, ValueError):
            unreadable = True
    w_before, t_before = _prose_words(before)

    r = _fold_via_node(ops, ptr)
    if "error" in r:
        return _no("refold", d, r["error"])
    after = r["state"]
    w_after, t_after = _prose_words(after)

    if w_after < w_before and not allow_loss:
        return {**_no("refold", d,
                      f"the refold would leave {w_after:,} words where "
                      f"state.json holds {w_before:,}. The journal is the "
                      f"record and the cache may simply be stale - but this "
                      f"cannot tell that apart from a journal that lost ops, "
                      f"so it is shown and not written."),
                "words_before": w_before, "words_after": w_after,
                "version_words_before": t_before, "version_words_after": t_after}

    if before is not None and json.dumps(before, sort_keys=True) == \
            json.dumps(after, sort_keys=True):
        return {"verb": "refold", "target": str(d), "ok": True, "changed": False,
                "ops": len(ops), "ptr": ptr, "rev": journal.get("rev"),
                "backup": None, "words": w_after,
                "note": "state.json already IS the fold of the journal; "
                        "nothing was written"}

    backup = None
    if state_p.is_file():
        b = _backup(state_p, "refold", f"{d.parent.parent.name}/{d.name}")
        if "error" in b:
            return _no("refold", d, b["error"])
        backup = b["path"]

    payload = {"rev": journal.get("rev", 0), "story": journal.get("story", d.name),
               "saved_at": journal.get("saved_at"), "state": after}
    try:
        _server()._atomic_write(state_p, payload)
    except OSError as e:
        return _no("refold", d, f"state.json could not be written "
                                f"({str(e)[:120]}); the backup at {backup} "
                                f"still holds what was there")
    return {"verb": "refold", "target": str(d), "ok": True, "changed": True,
            "ops": len(ops), "ptr": ptr, "rev": journal.get("rev"),
            "backup": backup,
            "was_missing": not had_file,
            "was_null": had_file and not unreadable and before is None,
            "was_unreadable": unreadable,
            "words_before": w_before, "words_after": w_after,
            "version_words_before": t_before, "version_words_after": t_after}


def reindex(project_dir) -> dict:
    d = _up_from(project_dir, ("index.json",), marker="stories")
    if d.name == "texts":
        d = d.parent
    texts = d / "texts"
    if not texts.is_dir():
        return _no("reindex", d, f"there is no texts/ under {d.name}, so there "
                                 f"is no index to rebuild")
    L = _server()
    if L.E is None:
        return _no("reindex", d, "loom_engine is not importable, and the "
                                 "rebuild is its code")
    idx = texts / "index.json"
    old = {"slots": {}}
    if idx.is_file():
        try:
            doc = json.loads(idx.read_text(encoding="utf-8"))
            if isinstance(doc, dict) and isinstance(doc.get("slots"), dict):
                old = doc
        except (OSError, ValueError):
            old = {"slots": {}}

    backup = None
    if idx.is_file():
        b = _backup(idx, "reindex", d.name)
        if "error" in b:
            return _no("reindex", d, b["error"])
        backup = b["path"]

    try:
        new = L.E.TextStore(texts).rebuild_index()
    except OSError as e:
        return _no("reindex", d, f"the index could not be rebuilt "
                                 f"({str(e)[:120]}); nothing was changed")

    o, n = old.get("slots") or {}, new.get("slots") or {}

    def _was(slot) -> dict:
        e = o.get(slot)
        return e if isinstance(e, dict) else {}

    moved = []
    for slot, e in n.items():
        was = _was(slot).get("current")
        if was is not None and was != e.get("current"):
            moved.append({"slot": slot, "was": was, "now": e.get("current")})
    lost_corr = 0
    for slot, e in n.items():
        old_versions = _was(slot).get("versions")
        if not isinstance(old_versions, dict):
            continue
        for v in (e.get("versions") or {}):
            meta = old_versions.get(v)
            if isinstance(meta, dict) and meta.get("first_corr"):
                lost_corr += 1
    return {"verb": "reindex", "target": str(d), "ok": True, "backup": backup,
            "slots_before": len(o), "slots_after": len(n),
            "versions": sum(len(e.get("versions") or {}) for e in n.values()),
            "recovered": sorted(set(n) - set(o)),
            "dropped": sorted(set(o) - set(n)),
            "pointer_moves": moved, "first_corr_cleared": lost_corr}


def _count_objects(git: str, root: Path) -> dict:
    try:
        r = subprocess.run([git, "-C", str(root), "count-objects", "-v"],
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if r.returncode != 0:
        return {}
    out = {}
    for line in (r.stdout or "").splitlines():
        k, _, v = line.partition(":")
        v = v.strip()
        if v.isdigit():
            out[k.strip()] = int(v)
    return out


def pack(workspace) -> dict:
    root = Path(workspace)
    if root.name == ".git":
        root = root.parent
    git = shutil.which("git")
    if not git:
        return _no("pack", root, "git is not on PATH - the same message "
                                 "start.sh prints when it cannot snapshot")
    if not (root / ".git").exists():
        return _no("pack", root, f"there is no snapshot repo at {root} - "
                                 f"start.sh creates one on the next launch")
    before = _count_objects(git, root)
    try:
        r = subprocess.run([git, "-C", str(root), "gc", "--no-prune", "--quiet"],
                           capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return _no("pack", root, "git gc did not finish within 30 minutes")
    except OSError as e:
        return _no("pack", root, f"git gc could not be started: {str(e)[:120]}")
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip().splitlines()
        return _no("pack", root, "git gc failed: " + (msg[0] if msg else
                                                      f"exit {r.returncode}"))
    after = _count_objects(git, root)
    return {"verb": "pack", "target": str(root), "ok": True,
            "loose_before": before.get("count"), "loose_after": after.get("count"),
            "packs_before": before.get("packs"), "packs_after": after.get("packs"),
            "in_pack": after.get("in-pack"),
            "size_pack_kib": after.get("size-pack"),
            "pruned": False}


def _tree_stat(d: Path) -> tuple:
    files = total = 0
    for p in d.rglob("*"):
        if p.is_file():
            files += 1
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return files, total


def adopt_runs(runs_dir, workspace) -> dict:
    src = Path(runs_dir).resolve()
    ws = Path(workspace).resolve()
    if not src.is_dir():
        return _no("adopt_runs", src, f"there is no directory at {src}")
    if not ws.is_dir():
        return _no("adopt_runs", src, f"the workspace {ws} is not there")
    try:
        if src == ws or ws in src.parents:
            return _no("adopt_runs", src, f"{src} is already inside {ws}; "
                                          f"there is nothing to adopt")
    except OSError:
        pass
    dst = ws / "data" / "runs"
    if dst.exists():
        return _no("adopt_runs", src, f"{dst} already exists. It is a record "
                                      f"too, so this will not merge into it or "
                                      f"write beside it - move or rename it "
                                      f"first and run this again.")
    files, size = _tree_stat(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
        return {"verb": "adopt_runs", "target": str(src), "ok": True,
                "moved_to": str(dst), "files": files, "bytes": size,
                "how": "rename", "in_snapshot": False}
    except OSError:
        pass
    try:
        shutil.copytree(src, dst)
    except (OSError, shutil.Error) as e:
        return _no("adopt_runs", src, f"the copy failed ({str(e)[:120]}); "
                                      f"{src} is untouched and {dst} may be "
                                      f"partial - remove it before retrying")
    got, got_size = _tree_stat(dst)
    if (got, got_size) != (files, size):
        return _no("adopt_runs", src,
                   f"the copy landed {got:,} files / {got_size:,} bytes against "
                   f"{files:,} / {size:,} at the source. Nothing was removed.")
    try:
        shutil.rmtree(src)
    except OSError as e:
        return {"verb": "adopt_runs", "target": str(src), "ok": True,
                "moved_to": str(dst), "files": files, "bytes": size,
                "how": "copy", "in_snapshot": False,
                "note": f"the copy is complete and verified, but the original "
                        f"could not be removed ({str(e)[:100]}) - it is still "
                        f"at {src}"}
    return {"verb": "adopt_runs", "target": str(src), "ok": True,
            "moved_to": str(dst), "files": files, "bytes": size,
            "how": "copy+verify", "in_snapshot": False}


def backfill_texts(project_dir, dry: bool = False) -> dict:
    d = Path(project_dir).resolve()
    L = _server()
    if L.E is None:
        return _no("backfill_texts", d, "loom_engine is not importable")
    if L.PROJECTS is None:
        return _no("backfill_texts", d,
                   "this verb runs only in the bound workspace, and this "
                   "loom-server module has no workspace bound. Run it from a "
                   "running server's Loading tab rather than from a shell.")
    root = d.parent
    try:
        bound = Path(L.PROJECTS).resolve()
    except OSError:
        bound = Path(L.PROJECTS)
    if root != bound:
        return _no("backfill_texts", d,
                   f"this verb runs only in the bound workspace. The server is "
                   f"bound to {bound}, and {d.name} lives under {root}.")
    if not d.is_dir():
        return _no("backfill_texts", d, f"there is no project at {d}")
    if L._project_dir(d.name).resolve() != d:
        return _no("backfill_texts", d,
                   f"{d.name!r} does not survive the id rule this route uses - "
                   f"it would run against {L._project_dir(d.name).name!r} "
                   f"instead. Rename the folder first.")

    backup = None
    idx = d / "texts" / "index.json"
    if idx.is_file() and not dry:
        b = _backup(idx, "backfill", d.name)
        if "error" in b:
            return _no("backfill_texts", d, b["error"])
        backup = b["path"]
    try:
        r = L.texts_backfill(d.name, dry=bool(dry))
    except Exception as e:
        return _no("backfill_texts", d, f"the backfill failed: "
                                        f"{type(e).__name__}: {str(e)[:120]}")
    if r.get("error"):
        return _no("backfill_texts", d, str(r["error"]))
    return {"verb": "backfill_texts", "target": str(d), "ok": True,
            "dry": bool(dry), "backup": backup,
            "seen": r.get("seen"), "wrote": r.get("wrote"),
            "stories": r.get("stories"), "note": r.get("note")}


def forget_empty(folder) -> dict:
    d = Path(folder)
    if not d.is_dir():
        return _no("forget_empty", d, f"there is no directory at {d}")
    ws = _ws_of(d)
    if ws is None:
        return _no("forget_empty", d, f"{d} is not inside a workspace - "
                                      f"refusing to remove a folder this "
                                      f"cannot place")
    if d.resolve() == ws.resolve() or d.resolve() == (ws / "loom" / "projects").resolve():
        return _no("forget_empty", d, "that is the workspace itself, not an "
                                      "empty folder inside it")
    dirs = []
    for p in d.rglob("*"):
        if p.is_file() or p.is_symlink():
            return _no("forget_empty", d, f"{p.name} is in there, so this is "
                                          f"not an empty folder any more - "
                                          f"nothing was removed")
        if p.is_dir():
            dirs.append(p)
    dirs.sort(key=lambda p: len(p.parts), reverse=True)
    removed = 0
    for p in [*dirs, d]:
        try:
            p.rmdir()
            removed += 1
        except OSError as e:
            return {"verb": "forget_empty", "target": str(d), "ok": False,
                    "refused": f"{p} would not come away ({str(e)[:100]}) - "
                               f"something is in it. {removed} empty "
                               f"subfolder(s) below it were removed.",
                    "removed": removed}
    return {"verb": "forget_empty", "target": str(d), "ok": True,
            "removed": removed, "folders": removed}


VERBS = {
    "refold":         {"fn": refold,         "target": "story directory",
                       "opts": {"allow_loss": bool}},
    "reindex":        {"fn": reindex,        "target": "project directory",
                       "opts": {}},
    "pack":           {"fn": pack,           "target": "workspace root",
                       "opts": {}},
    "adopt_runs":     {"fn": adopt_runs,     "target": "stray runs directory",
                       "opts": {"workspace": str}},
    "backfill_texts": {"fn": backfill_texts, "target": "project directory",
                       "opts": {"dry": bool}},
    "forget_empty":   {"fn": forget_empty,   "target": "the empty folder",
                       "opts": {}},
}


def run(verb: str, target, **opts) -> dict:
    spec = VERBS.get(str(verb))
    if not spec:
        return _no(verb, target, f"there is no verb called {verb!r}. "
                                 f"The verbs are: {', '.join(sorted(VERBS))}.")
    extra = sorted(set(opts) - set(spec["opts"]))
    if extra:
        return _no(verb, target, f"{verb} does not take {', '.join(extra)}")
    if "workspace" in spec["opts"] and not opts.get("workspace"):
        return _no(verb, target, f"{verb} needs a workspace to move into")
    return spec["fn"](target, **opts)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "--list"):
        print(__doc__.strip().splitlines()[0])
        print()
        for name, spec in sorted(VERBS.items()):
            o = "".join(f" [--{k.replace('_', '-')}]" for k in spec["opts"])
            print(f"  {name:<16} <{spec['target']}>{o}")
        return 0
    verb, rest = argv[0], argv[1:]
    opts, target = {}, None
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--dry":
            opts["dry"] = True
        elif a == "--allow-loss":
            opts["allow_loss"] = True
        elif a == "--workspace" and i + 1 < len(rest):
            i += 1
            opts["workspace"] = rest[i]
        elif target is None:
            target = a
        else:
            print(f"unexpected argument: {a}")
            return 2
        i += 1
    if target is None:
        print(f"{verb} needs a target")
        return 2
    out = run(verb, target, **opts)
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
