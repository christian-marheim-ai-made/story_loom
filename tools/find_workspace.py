from __future__ import annotations

import sys
from pathlib import Path

MAX_DEPTH = 2

SKIP = {".git", ".venv", "__pycache__", "node_modules", "models", "logs",
        "trace", "manuscript", "projects", "stories", "samples", "prompts"}


def _dirs(p: Path) -> list:
    try:
        return [d for d in p.iterdir() if d.is_dir()]
    except OSError:
        return []


def _count(loom: Path) -> tuple:
    projects = stories = 0
    for base, deep in ((loom / "projects", True), (loom / "stories", False)):
        if not base.is_dir():
            continue
        for d in _dirs(base):
            if deep:
                projects += 1
                stories += len(_dirs(d / "stories"))
            else:
                stories += 1
    return projects, stories


def looks_like_loom(d: Path) -> int:
    score = 0
    if (d / "projects").is_dir():
        score += 60
    if (d / "stories").is_dir():
        score += 50
    if (d / "project.json").is_file():
        score += 40
    if (d / "trace").is_dir():
        score += 10
    if (d / "jobs.json").is_file():
        score += 10
    if not score:
        return 0
    p, s = _count(d)
    return score + min(p * 25 + s * 15, 400)


def score_root(d: Path) -> tuple:
    loom = d / "loom"
    if not loom.is_dir():
        return 0, ""
    s = looks_like_loom(loom)
    if not s:
        return 0, ""
    bits = []
    p, st = _count(loom)
    if p:
        bits.append(f"{p} project{'' if p == 1 else 's'}")
    if st:
        bits.append(f"{st} stor{'y' if st == 1 else 'ies'}")
    if (loom / "stories").is_dir() and not (loom / "projects").is_dir():
        bits.append("the older stories/ layout")
    if (d / "extractor").is_dir():
        s += 15
        bits.append("extractor scans")
    if (d / "logs").is_dir():
        s += 5
    return s, ", ".join(bits) or "the folders but nothing written yet"


def find(root: Path, max_depth: int = MAX_DEPTH) -> dict:
    root = Path(root)
    if not root.is_dir():
        return {"status": "missing", "path": str(root), "why": "", "others": []}

    found = []
    seen = set()

    def visit(d: Path, depth: int):
        key = str(d).lower()
        if key in seen or depth > max_depth:
            return
        seen.add(key)
        s, why = score_root(d)
        if s:
            found.append((s, d, why))
            return
        for sub in _dirs(d):
            if sub.name.lower() in SKIP or sub.name.startswith("."):
                continue
            visit(sub, depth + 1)

    visit(root, 0)

    if len(found) > 1:
        found.sort(key=lambda t: (-t[0], str(t[1])))
        return {"status": "many", "path": "", "why": "",
                "others": [{"path": str(p), "why": w} for _, p, w in found]}
    if found:
        s, p, why = found[0]
        same = str(p).lower().rstrip("\\/") == str(root).lower().rstrip("\\/")
        return {"status": "here" if same else "deeper",
                "path": str(p), "why": why, "others": []}

    named = []
    for d in [root] + [x for x in _dirs(root) if x.name.lower() not in SKIP]:
        if d.name.lower() == "loom":
            continue
        if looks_like_loom(d):
            named.append(d)
    if len(named) == 1:
        p, s = _count(named[0])
        return {"status": "rename", "path": str(named[0]), "others": [],
                "why": f"{p} project{'' if p == 1 else 's'}, "
                       f"{s} stor{'y' if s == 1 else 'ies'}"}
    if len(named) > 1:
        return {"status": "many", "path": "", "why": "",
                "others": [{"path": str(d), "why": "a loom folder under "
                                                   "another name"} for d in named]}

    try:
        empty = not any(root.iterdir())
    except OSError:
        empty = False
    return {"status": "empty" if empty else "none", "path": str(root),
            "why": "", "others": []}


def main(argv) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    r = find(Path(argv[1].replace("\\", "/")))
    st, p = r["status"], r["path"]
    print(f"{st}\t{p}")
    e = sys.stderr
    if st == "here":
        print(f"  this is the workspace ({r['why']})", file=e)
    elif st == "deeper":
        print(f"  the workspace is one folder in: {p}", file=e)
        print(f"  {r['why']}", file=e)
    elif st == "rename":
        d = Path(p)
        print(f"  {p}", file=e)
        print(f"  is a loom folder under another name ({r['why']}).", file=e)
        print(f'\n    mv "{p}" "{d.parent / "loom"}"\n', file=e)
        print(f"  then point at {d.parent} again.", file=e)
    elif st == "many":
        print("  MORE THAN ONE workspace is in there:", file=e)
        for o in r["others"]:
            print(f"    {o['path']}   ({o['why']})", file=e)
        print("  Name the one you want - picking for you would be picking "
              "whose book gets opened.", file=e)
    elif st == "empty":
        print("  nothing in there yet - a new workspace will be laid out",
              file=e)
    elif st == "none":
        print("  no workspace in there, and the folder is not empty.", file=e)
        print("  Starting here would put a new one beside whatever is already "
              "in it - which is how an empty workspace once got opened next to "
              "somebody's book.", file=e)
        print(f'\n  If that is really what you want:  '
              f'./start.sh "{p}" --new "Title"\n', file=e)
    elif st == "missing":
        print(f"  that folder is not there: {p}", file=e)
    return 0 if st in ("here", "deeper", "empty") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
