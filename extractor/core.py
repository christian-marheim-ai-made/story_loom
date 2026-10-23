from __future__ import annotations

import argparse
import json
import hashlib
import os
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = None
_IO = threading.Lock()

_SAFE = re.compile(r'[<>:"|?*\x00-\x1f]')


def safe_name(s: str, fallback: str = "untitled") -> str:
    s = _SAFE.sub("", (s or "").strip()).strip(". ")
    s = s.replace("/", "-").replace("\\", "-")
    return s[:80] or fallback


SCOPE_DEFAULT = {"toplist": "story", "examples": "story"}


def story_dir(story: str) -> Path:
    return DATA / "stories" / safe_name(story, "story")


def story_meta_path(story: str) -> Path:
    return story_dir(story) / "story.json"


def story_meta(story: str) -> dict:
    p = story_meta_path(story)
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            d.setdefault("scope", dict(SCOPE_DEFAULT))
            for k, v in SCOPE_DEFAULT.items():
                d["scope"].setdefault(k, v)
            return d
        except ValueError:
            pass
    return {"title": story, "created": None, "notes": "", "scope": dict(SCOPE_DEFAULT)}


def story_meta_save(story: str, patch: dict) -> dict:
    d = story_meta(story)
    d["title"] = patch.get("title", d.get("title") or story)
    if "notes" in patch:
        d["notes"] = patch["notes"]
    if "scope" in patch and isinstance(patch["scope"], dict):
        d["scope"] = {**d.get("scope", SCOPE_DEFAULT), **patch["scope"]}
    d.setdefault("created", time.strftime("%Y-%m-%d %H:%M:%S"))
    d["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    rel = f"stories/{safe_name(story,'story')}/story.json"
    write_text(rel, json.dumps(d, indent=1, ensure_ascii=False))
    return d


def chapter_dirs(story: str) -> list[Path]:
    sd = story_dir(story)
    if not sd.exists():
        return []
    return sorted([p for p in sd.iterdir() if p.is_dir() and p.name.lower().startswith("chapter")],
                  key=lambda p: (len(p.name), p.name.lower()))


def next_chapter_index(story: str) -> int:
    used = set()
    for d in chapter_dirs(story):
        m = re.search(r"(\d+)", d.name)
        if m:
            used.add(int(m.group(1)))
    n = 1
    while n in used:
        n += 1
    return n - 1


def scoped_toplist_path(story: str | None) -> Path:
    if story and story_meta(story).get("scope", {}).get("toplist") == "story":
        return story_dir(story) / "toplist.json"
    return DATA / "shared" / "toplist.json"


def scoped_examples_path(story: str | None, dim: str) -> Path:
    if story and story_meta(story).get("scope", {}).get("examples") == "story":
        return story_dir(story) / "examples" / f"{dim}.txt"
    return DATA / "shared" / "examples" / f"{dim}.txt"


def list_stories() -> dict:
    out = []
    sroot = DATA / "stories"
    if sroot.exists():
        for sd in sorted([d for d in sroot.iterdir() if d.is_dir()], key=lambda d: d.name.lower()):
            meta = story_meta(sd.name)
            chs = chapter_dirs(sd.name)
            tl = scoped_toplist_path(sd.name)
            n_top = 0
            if tl.exists():
                try:
                    n_top = len(json.loads(tl.read_text(encoding="utf-8")).get("entries", []))
                except ValueError:
                    n_top = -1
            ex = sd / "examples"
            out.append({
                "name": sd.name, "title": meta.get("title") or sd.name,
                "created": meta.get("created"), "notes": meta.get("notes", ""),
                "scope": meta.get("scope", SCOPE_DEFAULT),
                "chapters": [c.name for c in chs],
                "next_chapter_index": next_chapter_index(sd.name),
                "toplist_entries": n_top,
                "examples": sorted(f.stem for f in ex.glob("*.txt")) if ex.exists() else [],
            })
    return {"data_root": str(DATA), "stories": out}


def migrate_globals() -> list[str]:
    moved = []
    old_tl, new_tl = DATA / "toplist.json", DATA / "shared" / "toplist.json"
    if old_tl.exists() and not new_tl.exists():
        new_tl.parent.mkdir(parents=True, exist_ok=True)
        os.replace(old_tl, new_tl)
        moved.append("toplist.json -> shared/toplist.json")
    old_ex, new_ex = DATA / "examples", DATA / "shared" / "examples"
    if old_ex.exists() and any(old_ex.glob("*.txt")) and not new_ex.exists():
        new_ex.parent.mkdir(parents=True, exist_ok=True)
        os.replace(old_ex, new_ex)
        moved.append("examples/ -> shared/examples/")
    return moved


def under_data(rel: str) -> Path:
    p = (DATA / rel.replace("\\", "/").lstrip("/")).resolve()
    root = DATA.resolve()
    if p != root and root not in p.parents:
        raise ValueError(f"path escapes the data root: {rel}")
    return p


def write_text(rel: str, content: str) -> dict:
    p = under_data(rel)
    with _IO:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8", newline="\n")
        os.replace(tmp, p)
    return {"path": str(p), "rel": rel, "bytes": len(content.encode("utf-8"))}


def read_text(rel: str) -> dict:
    p = under_data(rel)
    if not p.exists():
        return {"rel": rel, "exists": False, "text": ""}
    return {"rel": rel, "exists": True, "text": p.read_text(encoding="utf-8", errors="replace"),
            "mtime": p.stat().st_mtime}


def list_tree() -> dict:
    stories = []
    sroot = DATA / "stories"
    if sroot.exists():
        for sd in sorted([d for d in sroot.iterdir() if d.is_dir()], key=lambda d: d.name.lower()):
            chapters = []
            for cd in chapter_dirs(sd.name):
                files = {}
                for f in cd.iterdir():
                    if f.is_file():
                        files[f.name] = f.stat().st_size
                chapters.append({"name": cd.name, "files": files})
            stories.append({"name": sd.name, "chapters": chapters})
    examples = {}
    ex = DATA / "examples"
    if ex.exists():
        for f in sorted(ex.glob("*.txt")):
            examples[f.stem] = f.stat().st_size
    tl = DATA / "toplist.json"
    n_top = 0
    if tl.exists():
        try:
            n_top = len(json.loads(tl.read_text(encoding="utf-8")).get("entries", []))
        except ValueError:
            n_top = -1
    return {"data_root": str(DATA), "stories": stories,
            "examples": examples, "toplist_entries": n_top}


CTX_TOKENS = 16384
CHARS_PER_TOKEN = 4.4
SAFETY = 0.90
EXAMPLES_CAP = 3000
TOPLIST_CAP = 14000
TOPLIST_ENTRY_CAP = 320

PREAMBLE = ("You are analyzing one chapter of a story. Read it, then follow the "
            "TASK that comes after it. Work ONLY from this text.")
FORMAT = ("FORMAT: plain text, concise bullet lines, no preamble, no commentary about "
          'the task itself. If the text contains nothing relevant, write exactly: "nothing found".')

DIM_TASKS = {
    "plot":
        "State what the plot of this chapter was about — the situation, the central "
        "problem or tension driving it, and where that stands by the end of the chapter.",
    "events":
        "List what actually happened, in the order it happened. One line per event. "
        "Concrete actions and occurrences only, not interpretation.",
    "adjectives_subjects":
        "Report HOW things were described. For each notable subject (person, place, object, "
        "atmosphere), list the adjectives, imagery and sensory language the text attaches to it. "
        "One block per subject, formatted 'SUBJECT — the words and images used'.",
    "matches_against_toplist":
        "Compare this chapter against OUR CURRENT TOPLIST shown below. Report three things: "
        "(a) which toplist entries this chapter OVERLAPS with, and how; "
        "(b) what this chapter EXTENDS or adds beyond them; "
        "(c) anything here that is adjacent in spirit to a toplist entry without being the "
        "same thing. If a toplist entry has no counterpart here, do not mention it.",
    "story_context":
        "Classify the narrative function of each paragraph — what it sets up or pays off.",
    "writingstyle":
        "Describe the writing style: sentence length and rhythm, paragraph shape, vocabulary "
        "register, the ratio of dialogue to narration, how close the point of view sits, tense "
        "and person, and any recurring stylistic habits. Quote two or three short phrases as "
        "evidence for the traits you name.",
    "topics":
        "List the topics present in the text — one per line, as short noun phrases. Include both "
        "concrete subject matter and thematic topics.",
    "person_descriptions":
        "For every person who appears: name, how they look, their characteristics, their origin "
        "or background, and their role in the book. One block per person, one field per line. Record only what "
        "the text states or clearly implies; write 'not stated' for anything absent.",
    "person_behaviour":
        "For every person: how they behaved, and how others reacted to that behaviour. "
        "Name the behaviour, then the reaction it drew.",
    "interactions":
        "Describe the key interactions between characters: who was involved, what "
        "happened between them, what changed as a result, and how the text "
        "wrote it. Be specific and concrete rather than summarising.",
}
CONTEXT_FORMAT = (
    'Number the paragraphs yourself, starting at 1, in the order they appear. Write one line '
    'per paragraph, and REPLACE the number — do not write the letter N:\n'
    '¶1 — <leading-up | payoff-after-buildup | aftermath | free-standing> — <one sentence on '
    'what it sets up or pays off>\n'
    '¶2 — <…>\n'
    '¶3 — <…>\n'
    'Continue until every paragraph of the chapter has its own numbered line.')


def read_examples(dim: str, story: str | None = None) -> str:
    p = scoped_examples_path(story, dim)
    if not p.exists():
        return ""
    t = p.read_text(encoding="utf-8", errors="replace").strip()
    if len(t) > EXAMPLES_CAP:
        t = t[:EXAMPLES_CAP].rsplit("\n", 1)[0] + "\n[… example library truncated …]"
    return t


def read_toplist_block(only: list[str] | None = None,
                       story: str | None = None) -> tuple[str, dict]:
    scope = (story_meta(story).get("scope", {}).get("toplist") if story else "shared")
    entries, borrowed = gather_entries(story, scope)
    if not entries:
        return "", {"total": 0, "included": 0, "scope": scope, "borrowed": 0}
    if only:
        want = {c.lower() for c in only}
        entries = [e for e in entries if want & {c.lower() for c in (e.get("categories") or [])}]
    if not entries:
        return "", {"total": 0, "included": 0, "scope": scope, "filtered_out": True}

    def line(e: dict) -> str:
        cats = ", ".join(e.get("categories") or []) or "uncategorised"
        ex = " ".join((e.get("excerpt") or "").split())
        if len(ex) > TOPLIST_ENTRY_CAP:
            ex = ex[:TOPLIST_ENTRY_CAP].rsplit(" ", 1)[0] + " […]"
        src = f' (transferable, from "{e.get("story")}")' if e.get("_borrowed") else ""
        return f"- [{cats}]{src} {ex}"

    buckets: dict[str, list] = {}
    for e in entries:
        key = (e.get("categories") or ["uncategorised"])[0].lower()
        buckets.setdefault(key, []).append(e)

    picked, used, dropped = [], 0, 0
    round_i = 0
    while True:
        added_this_round = False
        for key in sorted(buckets):
            col = buckets[key]
            if round_i >= len(col):
                continue
            added_this_round = True
            ln = line(col[round_i])
            if used + len(ln) + 1 > TOPLIST_CAP:
                dropped += 1
                continue
            picked.append(ln)
            used += len(ln) + 1
        if not added_this_round:
            break
        round_i += 1

    stats = {"total": len(entries), "included": len(picked), "dropped": dropped,
             "categories": len(buckets), "scope": scope, "borrowed": borrowed}
    if dropped:
        picked.append(f"[{dropped} further entr{'y' if dropped == 1 else 'ies'} omitted for space "
                      f"— every category above is represented]")
    return "\n".join(picked), stats


def split_paragraphs(text: str) -> list[str]:
    return [p for p in re.split(r"\n\s*\n", text) if p.strip()]


def chunk_text(text: str, budget: int) -> list[str]:
    if len(text) <= budget:
        return [text]
    chunks, cur = [], ""
    for para in split_paragraphs(text):
        if len(para) > budget:
            if cur:
                chunks.append(cur); cur = ""
            for i in range(0, len(para), budget):
                chunks.append(para[i:i + budget])
            continue
        candidate = (cur + "\n\n" + para) if cur else para
        if len(candidate) > budget:
            chunks.append(cur); cur = para
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks


def compile_prompts(dim: str, text: str, *, custom: str = "", max_tokens: int = 900,
                    fix: str = "", only: list[str] | None = None,
                    story: str | None = None) -> dict:
    if dim == "custom_input":
        task = (custom or "").strip() or "Extract anything noteworthy from this chapter."
    else:
        task = DIM_TASKS.get(dim)
        if task is None:
            raise ValueError(f"unknown dimension: {dim}")

    tail_parts = [f"TASK: {task}", FORMAT]
    if dim == "story_context":
        tail_parts.append(CONTEXT_FORMAT)
    ex = read_examples(dim, story)
    if ex:
        tail_parts.append("EXAMPLES — the kind of material to look for and the shape of a "
                          "good answer:\n" + ex)
    tl_stats = None
    if dim == "matches_against_toplist":
        tl, tl_stats = read_toplist_block(only, story)
        tail_parts.append("OUR CURRENT TOPLIST (compare against this):\n" + tl if tl else
                          "OUR CURRENT TOPLIST: (empty — no comparison material yet; say so "
                          "and describe only what this chapter offers).")
    if fix.strip():
        tail_parts.append("THE AUTHOR REJECTED THE PREVIOUS ATTEMPT AT THIS TASK AND SAID:\n"
                          + fix.strip() + "\nFix exactly that this time.")
    tail = "\n\n".join(tail_parts)

    overhead = len(PREAMBLE) + len(tail) + 120
    budget = int((CTX_TOKENS * SAFETY - max_tokens) * CHARS_PER_TOKEN) - overhead
    budget = max(budget, 2000)
    chunks = chunk_text(text, budget)
    k = len(chunks)
    prompts = []
    for i, c in enumerate(chunks, start=1):
        header = f"CHAPTER TEXT (part {i}/{k}):" if k > 1 else "CHAPTER TEXT:"
        prompts.append(f"{PREAMBLE}\n\n{header}\n{c}\n\n---\n\n{tail}")
    return {"dim": dim, "chunks": k, "budget_chars": budget, "overhead_chars": overhead,
            "has_examples": bool(ex), "toplist": tl_stats, "prompts": prompts}


_JLOCK = threading.RLock()
_WAKE = threading.Event()
STUDIO = "http://127.0.0.1:8888"
JOB_CAP = 500


_STORE = None


def jobs_path() -> Path:
    return DATA / "jobs.json"


def load_jobs() -> dict:
    if _STORE is not None:
        return _STORE["load"]()
    try:
        return json.loads(jobs_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"seq": 0, "jobs": []}


def save_jobs(doc: dict) -> None:
    if _STORE is not None:
        _STORE["save"](doc)
        return
    doc["jobs"] = doc.get("jobs", [])[-JOB_CAP:]
    jobs_path().parent.mkdir(parents=True, exist_ok=True)
    tmp = jobs_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    os.replace(tmp, jobs_path())


def job_public(j: dict) -> dict:
    return {k: j.get(k) for k in ("id", "story", "chapter", "chapter_index", "dim",
                                  "status", "queue_pos", "error", "created", "finished",
                                  "out_file", "chars", "parts", "parts_done")}


def stable_seed(story: str, idx: int, dim: str) -> int:
    h = hashlib.sha256(f"{story}|{idx}|{dim}".encode()).hexdigest()
    return int(h[:8], 16) % 2_000_000_000


def extract_rows(doc: dict) -> list:
    return [j for j in doc.get("jobs", []) if is_extract_job(j)]


def queue_order(doc: dict) -> list:
    q = [j for j in extract_rows(doc) if j["status"] == "queued"]
    q.sort(key=lambda j: j["created"])
    return q


def recompute_positions(doc: dict) -> None:
    if _STORE is not None and _STORE.get("positions"):
        _STORE["positions"](doc)
        return
    for pos, j in enumerate(queue_order(doc), start=1):
        j["queue_pos"] = pos
    for j in extract_rows(doc):
        if j["status"] != "queued":
            j["queue_pos"] = 0


def jobs_submit(b: dict) -> dict:
    story = safe_name(b.get("story"), "story")
    idx = int(b.get("chapter_index") or 0)
    chapter = safe_name(b.get("chapter"), f"Chapter {idx+1}")
    dim = b.get("dim", "")
    prompts = b.get("prompts") or []
    if not prompts:
        raise ValueError("no prompts")
    gen = b.get("gen") or {}
    with _JLOCK:
        doc = load_jobs()
        doc["seq"] = int(doc.get("seq", 0)) + 1
        job = {
            "id": f"x{doc['seq']:05d}", "story": story, "chapter": chapter,
            "chapter_index": idx, "dim": dim, "prompts": prompts,
            "gen": {"model": gen.get("model"), "temperature": float(gen.get("temperature", 0.3)),
                    "max_tokens": int(gen.get("max_tokens") or 900),
                    "seed": gen.get("seed") if gen.get("seed") is not None
                            else stable_seed(story, idx, dim)},
            "out_file": b.get("out_file") or f"{dim}_chapter{idx+1}.txt",
            "status": "queued", "queue_pos": 0, "created": time.time(), "finished": None,
            "error": None, "studio_job_ids": [], "parts": len(prompts), "parts_done": 0,
            "chars": 0, "cancel": False,
        }
        doc["jobs"].append(job)
        recompute_positions(doc)
        save_jobs(doc)
    _WAKE.set()
    return {"job_id": job["id"], "queue_pos": job["queue_pos"]}


def job_mutate(jid: str, **fields):
    with _JLOCK:
        doc = load_jobs()
        for j in doc["jobs"]:
            if j["id"] == jid:
                j.update(fields)
                recompute_positions(doc)
                save_jobs(doc)
                return j
    return None


def job_get(jid: str):
    with _JLOCK:
        for j in load_jobs()["jobs"]:
            if j["id"] == jid:
                return j
    return None


def jobs_cancel(jid: str | None) -> dict:
    n = 0
    with _JLOCK:
        doc = load_jobs()
        for j in extract_rows(doc):
            if jid and j["id"] != jid:
                continue
            if j["status"] == "queued":
                j["status"] = "cancelled"; j["finished"] = time.time(); n += 1
            elif j["status"] == "running":
                j["cancel"] = True; n += 1
                for sid in j.get("studio_job_ids", []):
                    try:
                        studio_post(f"/api/jobs/{sid}/cancel", {})
                    except Exception:
                        pass
        recompute_positions(doc)
        save_jobs(doc)
    return {"ok": True, "affected": n}


def studio_post(path: str, body: dict) -> dict:
    req = urllib.request.Request(STUDIO + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read() or b"{}")


def studio_get(path: str):
    with urllib.request.urlopen(STUDIO + path, timeout=120) as r:
        raw = r.read()
        return json.loads(raw) if "json" in r.headers.get("Content-Type", "") \
            else raw.decode("utf-8")


def update_sidecar(job: dict, meta: dict) -> None:
    rel = f"stories/{job['story']}/{job['chapter']}/sidecar.txt"
    p = under_data(rel)
    doc = {}
    if p.exists():
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            doc = {}
    doc.setdefault("story", job["story"])
    doc.setdefault("chapter", job["chapter"])
    doc.setdefault("source_file", f"chapter{job['chapter_index']+1}.txt")
    doc.setdefault("dimensions", {})
    doc["dimensions"][job["dim"]] = meta
    doc["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    write_text(rel, json.dumps(doc, indent=1, ensure_ascii=False))


def run_one(job: dict, prompt: str) -> dict:
    sub = studio_post("/api/jobs/write", {
        "summary": "", "prompt": prompt, "model_keys": [job["gen"]["model"]],
        "samples": 1, "max_tokens": job["gen"]["max_tokens"],
        "temperature": job["gen"]["temperature"], "seed": job["gen"]["seed"], "blind": False})
    sid = sub["job_id"]
    with _JLOCK:
        doc = load_jobs()
        for j in doc["jobs"]:
            if j["id"] == job["id"]:
                j.setdefault("studio_job_ids", []).append(sid)
                save_jobs(doc)
    while True:
        live = job_get(job["id"])
        if live and live.get("cancel"):
            try:
                studio_post(f"/api/jobs/{sid}/cancel", {})
            except Exception:
                pass
            raise RuntimeError("cancelled")
        st = studio_get(f"/api/jobs/{sid}")
        if st["status"] in ("done", "failed", "cancelled"):
            v = (st.get("variants") or [{}])[0]
            m = v.get("meta") or {}
            text = ""
            if v.get("status") == "done":
                try:
                    text = studio_get(f"/api/jobs/{sid}/variants/{v['label']}/output")
                except Exception:
                    text = ""
            return {"text": text, "seed": m.get("seed"), "finish_reason": m.get("finish_reason"),
                    "token_count": m.get("token_count"),
                    "tokens_per_second": m.get("tokens_per_second"),
                    "error": v.get("error") or st.get("error"),
                    "studio_job_id": sid}
        time.sleep(1.0)


def is_extract_job(job: dict) -> bool:
    return str(job.get("id", "")).startswith("x")


def execute_job(job: dict) -> None:
    texts, err, metas = [], None, []
    for i, p in enumerate(job["prompts"]):
        try:
            r = run_one(job, p)
            if r.get("text"):
                texts.append(r["text"].strip())
            metas.append({k: r.get(k) for k in
                          ("seed", "finish_reason", "token_count",
                           "tokens_per_second")})
            job_mutate(job["id"], parts_done=i + 1)
        except RuntimeError:
            err = "cancelled"
            break
        except Exception as e:
            err = str(e)[:200]
            break
    if err == "cancelled":
        job_mutate(job["id"], status="cancelled", finished=time.time())
        return
    if err or not texts:
        job_mutate(job["id"], status="failed", error=err or "empty result",
                   finished=time.time())
        return
    body = ("\n\n".join(f"=== part {i+1}/{len(texts)} ===\n{t}"
                        for i, t in enumerate(texts))
            if len(texts) > 1 else texts[0])
    rel = f"stories/{job['story']}/{job['chapter']}/{job['out_file']}"
    try:
        write_text(rel, body + "\n")
        update_sidecar(job, {
            "file": job["out_file"], "status": "done",
            "model": job["gen"]["model"],
            "temperature": job["gen"]["temperature"],
            "seed": job["gen"]["seed"], "parts": len(job["prompts"]),
            "prompt_sha256": [hashlib_sha(p) for p in job["prompts"]],
            "generation": metas, "at": time.strftime("%Y-%m-%d %H:%M:%S")})
        job_mutate(job["id"], status="done", finished=time.time(),
                   chars=len(body))
    except Exception as e:
        job_mutate(job["id"], status="failed", error=f"write failed: {e}",
                   finished=time.time())


def hashlib_sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def requeue_stale(doc: dict) -> bool:
    changed = False
    for j in doc.get("jobs", []):
        if is_extract_job(j) and j.get("status") == "running":
            j["status"] = "queued"
            j["studio_job_ids"] = []
            j["parts_done"] = 0
            changed = True
    return changed


def review_rel(story: str, chapter: str) -> str:
    return f"stories/{safe_name(story)}/{safe_name(chapter)}/review.json"


def review_load(story: str, chapter: str) -> dict:
    p = under_data(review_rel(story, chapter))
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def review_set(b: dict) -> dict:
    story, chapter, dim = b.get("story", ""), b.get("chapter", ""), b.get("dim", "")
    doc = review_load(story, chapter)
    entry = doc.get(dim, {})
    if "vote" in b:
        entry["vote"] = int(b.get("vote") or 0)
    if "comment" in b:
        entry["comment"] = (b.get("comment") or "").strip()
    entry["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if not entry.get("vote") and not entry.get("comment"):
        doc.pop(dim, None)
    else:
        doc[dim] = entry
    write_text(review_rel(story, chapter), json.dumps(doc, indent=1, ensure_ascii=False))
    return {"ok": True, "review": doc}


def gather_entries(story: str | None, scope: str) -> tuple[list, int]:
    own = toplist_load(story).get("entries", [])
    for e in own:
        e["_borrowed"] = False
    if scope == "isolated" or not story:
        return own, 0
    borrowed = []
    sroot = DATA / "stories"
    mine = safe_name(story, "story")
    if sroot.exists():
        for sd in sorted([d for d in sroot.iterdir() if d.is_dir()], key=lambda d: d.name.lower()):
            if sd.name == mine:
                continue
            p = sd / "toplist.json"
            if not p.exists():
                continue
            try:
                other = json.loads(p.read_text(encoding="utf-8")).get("entries", [])
            except ValueError:
                continue
            for e in other:
                if e.get("shared"):
                    e = dict(e); e["_borrowed"] = True; e.setdefault("story", sd.name)
                    borrowed.append(e)
    return own + borrowed, len(borrowed)


def toplist_load(story: str | None = None) -> dict:
    p = scoped_toplist_path(story)
    if not p.exists():
        return {"entries": []}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        d.setdefault("entries", [])
        return d
    except ValueError:
        return {"entries": []}


def toplist_save(d: dict, story: str | None = None) -> None:
    p = scoped_toplist_path(story)
    write_text(str(p.relative_to(DATA)).replace("\\", "/"),
               json.dumps(d, indent=1, ensure_ascii=False))


def suggest_categories(story: str, chapter: str, dim: str) -> list[str]:
    cats: list[str] = []
    try:
        folder = under_data(f"stories/{safe_name(story)}/{safe_name(chapter)}")
        for f in folder.glob("topics_*.txt"):
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                t = line.strip().lstrip("-*•").strip()
                t = re.split(r"[:—–]", t)[0].strip()
                if 2 < len(t) <= 40 and not t.lower().startswith(("nothing found", "===")):
                    cats.append(t.lower())
    except (OSError, ValueError):
        pass
    seen, out = set(), []
    for c in cats + [dim]:
        if c not in seen:
            seen.add(c); out.append(c)
    return out[:12]


def toplist_add(b: dict) -> dict:
    story = b.get("story") or None
    d = toplist_load(story)
    entry = {
        "id": f"t{int(time.time()*1000)%100000000:08d}",
        "excerpt": (b.get("excerpt") or "").strip(),
        "story": b.get("story", ""), "chapter": b.get("chapter", ""),
        "dimension": b.get("dim", ""),
        "categories": [c.strip().lower() for c in (b.get("categories") or []) if c.strip()],
        "shared": bool(b.get("shared")),
        "rated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if not entry["excerpt"]:
        raise ValueError("empty excerpt")
    d["entries"].append(entry)
    toplist_save(d, story)
    return {"ok": True, "entry": entry, "count": len(d["entries"]),
            "scope": str(scoped_toplist_path(story).relative_to(DATA)).replace("\\", "/")}


def toplist_update(b: dict) -> dict:
    story = b.get("story") or None
    d = toplist_load(story)
    for e in d["entries"]:
        if e["id"] == b.get("id"):
            if "categories" in b:
                e["categories"] = [c.strip().lower() for c in (b.get("categories") or []) if c.strip()]
            if "shared" in b:
                e["shared"] = bool(b["shared"])
            toplist_save(d, story)
            return {"ok": True, "entry": e}
    raise ValueError("no such entry")


def toplist_remove(b: dict) -> dict:
    story = b.get("story") or None
    d = toplist_load(story)
    n = len(d["entries"])
    d["entries"] = [e for e in d["entries"] if e["id"] != b.get("id")]
    toplist_save(d, story)
    return {"ok": True, "removed": n - len(d["entries"])}


def toplist_view(story: str | None = None) -> dict:
    scope = (story_meta(story).get("scope", {}).get("toplist") if story else "shared")
    entries, borrowed = gather_entries(story, scope)
    cats: dict[str, int] = {}
    for e in entries:
        for c in e.get("categories") or []:
            cats[c] = cats.get(c, 0) + 1
    return {"entries": entries, "categories": sorted(cats.items(), key=lambda kv: -kv[1]),
            "story": story, "own": len(entries) - borrowed, "borrowed": borrowed,
            "path": str(scoped_toplist_path(story).relative_to(DATA)).replace("\\", "/"),
            "scope": scope}


def examples_append(b: dict) -> dict:
    dim = b.get("dim", "")
    if dim not in DIM_TASKS and dim != "custom_input":
        raise ValueError(f"unknown dimension: {dim}")
    text = (b.get("text") or "").strip()
    if not text:
        raise ValueError("empty example")
    story = b.get("story") or None
    p = scoped_examples_path(story, dim)
    rel = str(p.relative_to(DATA)).replace("\\", "/")
    cur = p.read_text(encoding="utf-8") if p.exists() else ""
    body = (cur.rstrip() + "\n---\n" + text + "\n") if cur.strip() else (text + "\n")
    write_text(rel, body)
    return {"ok": True, "rel": rel, "entries": body.count("\n---\n") + 1, "bytes": len(body)}
