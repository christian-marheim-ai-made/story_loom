from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

FACETS = [
    ("atmosphere", "Atmosphere"),
    ("location", "Location"),
    ("people", "People involved"),
    ("mood", "Mood shifts to"),
    ("under", "Underneath (thoughts, games)"),
    ("secret", "Secret (withheld)"),
    ("over", "Overarching (why here, history)"),
]
DIVIDER = "\n\n---\n\n"
TAIL_WORDS = 1200



WORDS_PER_PARAGRAPH = 46

TOKENS_PER_UNIT = 700
PARAS_PER_UNIT = (5, 10)

PARAS_PER_UNIT_SLOW = (3, 6)

PARAGRAPH_BANDS = {"auto": PARAS_PER_UNIT, "slow": PARAS_PER_UNIT_SLOW}

CLEAN_WORDS_CEILING = 1600
PADDING_WORDS_CERTAIN = 2600


def paragraph_range(max_tokens, band="auto") -> tuple:
    try:
        n = max(1, round(float(max_tokens or 0) / TOKENS_PER_UNIT))
    except (TypeError, ValueError):
        n = 1
    lo, hi = PARAGRAPH_BANDS.get(band or "auto", PARAS_PER_UNIT)
    return lo * n, hi * n


def plan_range(lo, hi) -> tuple:
    try:
        lo = int(lo or 0)
        hi = int(hi or 0)
    except (TypeError, ValueError):
        return 0, 0
    if lo <= 0 or hi <= lo:
        return 0, 0
    return lo, hi

_SENTENCE_END = re.compile(r'[.!?…]+["\'’”)\]»]*(?=\s|$)')

_NOT_AN_END = ('mr', 'mrs', 'ms', 'dr', 'st', 'prof', 'sgt', 'lt', 'jr', 'sr')


def last_full_sentence(text: str) -> str:
    t = (text or "").rstrip()
    if not t:
        return text or ""
    for m in reversed(list(_SENTENCE_END.finditer(t))):
        word = re.search(r'([A-Za-z]+)$', t[:m.start()])
        if word and word.group(1).lower() in _NOT_AN_END:
            continue
        return t[:m.end()]
    return text or ""


def tail_words(text: str, n: int = TAIL_WORDS) -> str:
    body = (text or "").strip()
    starts = [m.start() for m in re.finditer(r"\S+", body)]
    if len(starts) <= n:
        return body
    return body[starts[-n]:]


def steer_block(req: dict) -> str:
    one = str(req.get("steer") or "").strip()
    if not one:
        for cand in (req.get("steers") or []):
            if str(cand or "").strip():
                one = str(cand).strip()
                break
    if not one:
        return ""
    return ("DIRECTION FOR THIS VERSION - follow it in substance, not by "
            "quoting it:" + chr(10) + one)


def whole_in_tail(canon_parts, n: int = TAIL_WORDS) -> set:
    kept, used = set(), 0
    for part in reversed(list(canon_parts or [])):
        words = len(str(part.get("text") or "").split())
        if used + words > n:
            break
        used += words
        if part.get("pid"):
            kept.add(part["pid"])
    return kept


def canon_from(req: dict) -> str:
    parts = req.get("canon_parts")
    if parts:
        return (chr(10) * 2).join(
            str(x.get("text") or "").strip() for x in parts
            if str(x.get("text") or "").strip())
    return req.get("canon") or ""


CANON_WORDS_MIN = 100

OPENING_TAIL_WORDS = 250


def canon_window(req: dict) -> int:
    try:
        n = int(req.get("canon_words") or 0)
    except (TypeError, ValueError):
        n = 0
    if n > 0:
        return max(CANON_WORDS_MIN, n)
    return (OPENING_TAIL_WORDS if opens_chapter(req.get("part_in_chapter"))
            else TAIL_WORDS)


CANON_HEAD_CONTINUE = "STORY SO FAR (continue directly from the end of this):"

CANON_HEAD_OPENS = (
    "WHAT CAME BEFORE (context only — this part OPENS a new chapter):\n"
    "Start the scene CHAPTER above asks for. Do not continue the scene below."
)

CANON_HEAD_NONE = "(This is the opening of the story.)"

TRANSITION_HEAD = (
    "HOW THE PREVIOUS PART ENDED — its last few paragraphs, and nothing else.\n"
    "This is here for ONE reason: so your first sentence joins on without a "
    "jolt — same place, same moment, the same people where they were left. It "
    "is NOT what this part is about, and it is NOT the story so far. THE PLAN "
    "above is what happens. Do not continue its train of thought, do not "
    "re-tell it, and do not treat it as material to build on."
)


def TRANSITION_WORD_CEILING(n):
    return max(1, int(n)) * WORDS_PER_PARAGRAPH * 2


def tail_paragraphs(text, n, max_words=None) -> str:
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    paras = [p.strip() for p in re.split(r"\n\s*\n", str(text or "")) if p.strip()]
    picked = paras[-n:]
    cap = TRANSITION_WORD_CEILING(n) if max_words is None else int(max_words or 0)
    if cap <= 0:
        return "\n\n".join(picked)
    out, spent = [], 0
    for para in reversed(picked):
        w = para.split()
        if spent + len(w) <= cap:
            out.append(para)
            spent += len(w)
            continue
        room = cap - spent
        if room > 0 and not out:
            out.append(last_full_sentence(" ".join(w[-room:])) or " ".join(w[-room:]))
        break
    return "\n\n".join(reversed(out))


def transition_block(req: dict) -> str:
    try:
        n = int(req.get("transition_paras") or 0)
    except (TypeError, ValueError):
        return ""
    if n <= 0 or opens_chapter(req.get("part_in_chapter")):
        return ""
    parts = req.get("canon_parts") or []
    prev = str((parts[-1] or {}).get("text") or "").strip() if parts else ""
    if not prev:
        prev = str(req.get("canon") or "").strip()
    body = tail_paragraphs(prev, n)
    return f"{TRANSITION_HEAD}\n\n{body}" if body else ""


def opens_chapter(part_in_chapter) -> bool:
    try:
        return int(part_in_chapter or 0) == 1
    except (TypeError, ValueError):
        return False


def summaries_for_prompt(req: dict) -> list:
    entries = req.get("part_summaries") or []
    parts = req.get("canon_parts")
    if not parts:
        return entries
    whole = whole_in_tail(parts, canon_window(req))

    def already_in_prose(e) -> bool:
        if not isinstance(e, dict):
            return False
        covers = e.get("covers")
        if isinstance(covers, list) and covers:
            return all(pid in whole for pid in covers)
        return e.get("pid") in whole

    return [e for e in entries if not already_in_prose(e)]


def _facet_lines(facets: dict) -> list:
    return [f"{label.upper()}: {facets[key]}"
            for key, label in FACETS if (facets or {}).get(key)]


FEEDBACK_WINDOW = 10
FEEDBACK_CAP = 8


def _snippet(entry: dict) -> str:
    c = (entry.get("comment") or "").strip()
    para = (entry.get("paragraph") or "").strip()
    quote = ""
    if para:
        words = para.split()
        quote = '"' + " ".join(words[:14]) + ("…\"" if len(words) > 14 else "\"")
    if quote and c:
        return quote + " — " + c
    return c or quote or "(no detail)"


def feedback_subject(entries) -> str:
    out = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        c = (e.get("comment") or "").strip()
        para = (e.get("paragraph") or "").strip()
        if not c and not para:
            continue
        quote = '"' + " ".join(para.split()) + '"' if para else ""
        if quote and c:
            out.append(quote + " — " + c)
        else:
            out.append(c or quote)
    return "\n\n".join(out)


def feedback_dropped(entries, windows=FEEDBACK_WINDOW, target_part=None) -> list:
    out, survived = [], []
    for i, e in enumerate(entries or []):
        if not isinstance(e, dict):
            continue
        ch = e.get("channel") or "story"
        if ch not in ("story", "style"):
            out.append({"why": "channel", "i": i, "channel": ch,
                        "comment": (e.get("comment") or "")[:60]})
            continue
        if _said_nothing(e):
            continue
        w = _channel_window(windows, ch)
        if isinstance(windows, dict) and w == 0:
            out.append({"why": "off", "i": i, "channel": ch,
                        "part": e.get("part"),
                        "comment": (e.get("comment") or "")[:60]})
            continue
        if not _in_window(e, w, target_part):
            out.append({"why": "window", "i": i, "channel": ch, "window": w,
                        "part": e.get("part"),
                        "comment": (e.get("comment") or "")[:60]})
            continue
        survived.append((i, e))
    buckets = {}
    for i, e in survived:
        buckets.setdefault((e.get("channel") or "story", _polarity(e)),
                           []).append((i, e))
    for items in buckets.values():
        for i, e in items[:-FEEDBACK_CAP]:
            out.append({"why": "cap", "i": i, "channel": e.get("channel") or "story",
                        "part": e.get("part"),
                        "comment": (e.get("comment") or "")[:60]})
    return out


def _polarity(entry: dict):
    v = entry.get("vote") or 0
    return 0 if v == 0 else (1 if v > 0 else -1)


def _said_nothing(entry: dict) -> bool:
    return _polarity(entry) == 0 and not (entry.get("comment") or "").strip()


def _in_window(entry, window, target_part) -> bool:
    if target_part is None or not window:
        return True
    part = entry.get("part")
    if part is None:
        return True
    return (target_part - window) <= part <= target_part


def _channel_window(windows, channel):
    if isinstance(windows, dict):
        v = windows.get(channel, FEEDBACK_WINDOW)
        try:
            v = int(v)
        except (TypeError, ValueError):
            v = FEEDBACK_WINDOW
        return max(0, v)
    return windows


def _rides(entry, windows, target_part) -> bool:
    ch = entry.get("channel") or "story"
    w = _channel_window(windows, ch)
    if isinstance(windows, dict) and w == 0:
        return False
    return _in_window(entry, w, target_part)


FEEDBACK_KEEP_RANK = {3: 0, 2: 1, 0: 2, 1: 3}


def _stars_of(entry) -> int:
    try:
        n = int((entry or {}).get("stars") or 0)
    except (TypeError, ValueError):
        return 0
    return n if n in (1, 2, 3) else 0


def _levers_of(entry) -> list:
    lv = (entry or {}).get("levers")
    if isinstance(lv, str):
        lv = lv.split(",")
    if not isinstance(lv, (list, tuple)):
        return []
    seen, out = set(), []
    for k in lv:
        k = str(k or "").strip()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _dedupe_key(entry) -> str:
    c = " ".join(str((entry or {}).get("comment") or "").lower().split())
    return c or f"\x00vote:{(entry or {}).get('vote')}"


def feedback_for_budget(entries, budget_words: int = 0) -> dict:
    rows = [e for e in (entries or []) if isinstance(e, dict)]
    order = {id(e): i for i, e in enumerate(rows)}

    groups = {}
    for e in rows:
        groups.setdefault(_dedupe_key(e), []).append(e)
    winners, collapsed = [], 0
    for g in groups.values():
        collapsed += len(g) - 1
        winners.append(max(g, key=lambda e: (_stars_of(e), order[id(e)])))

    winners.sort(key=lambda e: (FEEDBACK_KEEP_RANK.get(_stars_of(e), 2),
                                -order[id(e)]))

    def cost(e):
        return len(_snippet(e).split())

    if not budget_words or budget_words <= 0:
        return {"kept": sorted(winners, key=lambda e: order[id(e)]),
                "dropped": [], "collapsed": collapsed,
                "words": sum(cost(e) for e in winners), "budget": 0}

    kept, dropped, used = [], [], 0
    for i, e in enumerate(winners):
        c = cost(e)
        if kept and used + c > budget_words:
            dropped.extend(winners[i:])
            break
        kept.append(e)
        used += c
    kept.sort(key=lambda e: order[id(e)])
    return {"kept": kept, "dropped": dropped, "collapsed": collapsed,
            "words": used, "budget": budget_words}


FEEDBACK_HEADINGS = (
    (("story", 1), "PLOT, more like this:"),
    (("story", -1), "PLOT, avoid this:"),
    (("style", 1), "STYLE, more like this:"),
    (("style", -1), "STYLE, avoid this:"),
    (("story", 0), "PLOT, notes:"),
    (("style", 0), "STYLE, notes:"),
)


def feedback_digest(entries, windows=FEEDBACK_WINDOW, target_part=None,
                    cap=None) -> str:
    entries = [e for e in (entries or [])
               if isinstance(e, dict) and not _said_nothing(e)
               and _rides(e, windows, target_part)]
    buckets = {key: [] for key, _ in FEEDBACK_HEADINGS}
    for e in entries:
        key = (e.get("channel") or "story", _polarity(e))
        if key in buckets:
            buckets[key].append((e, _snippet(e)))
    out = []
    for key, heading in FEEDBACK_HEADINGS:
        items = _kept(buckets[key], cap)
        if items:
            out.append(heading + "\n" + "\n".join("- " + s for s in items))
    return "\n\n".join(out)


def _kept(pairs, cap):
    if cap is None:
        return [s for _, s in pairs[-FEEDBACK_CAP:]]
    n = max(0, int(cap))
    if n >= len(pairs):
        return [s for _, s in pairs]
    ranked = sorted(
        range(len(pairs)),
        key=lambda i: (FEEDBACK_KEEP_RANK.get(_stars_of(pairs[i][0]), 2), -i))
    return [pairs[i][1] for i in sorted(ranked[:n])]


def _style_block(style: dict) -> str:
    style = style or {}
    ex, rules = (style.get("example") or "").strip(), (style.get("rules") or "").strip()
    if not ex and not rules:
        return ""
    b = ("STYLE — match the formatting, paragraph length and register of this "
         "example exactly (its content is irrelevant, only its shape):")
    if ex:
        b += "\n\n" + ex
    if rules:
        b += "\n\nStyle rules: " + rules
    return b



CRAFT_HEAD = (
    "CRAFT — {label}. This author has marked the passages below as examples of "
    "{label} done the way they want it. Match what they DO, not what they are "
    "about: they belong to a different story, and none of their characters, "
    "places or events may enter this one.")

CRAFT_RULE_HEAD = "CRAFT — {label}. {instruction}"

CRAFT_JOIN = "\n\n---\n\n"


def _craft_fit(passages, words):
    cap = int(words or 0)
    out, spent = [], 0
    for p in passages:
        n = len(str(p).split())
        if cap and spent + n > cap:
            continue
        out.append(str(p).strip())
        spent += n
    return out


def craft_block(req: dict, seam: str = "style") -> str:
    craft = req.get("craft") or {}
    if not craft:
        return ""
    if str(craft.get("seam") or "style") != seam:
        return ""
    label = str(craft.get("label") or craft.get("lever") or "").strip()
    mode = str(craft.get("mode") or "both").strip()
    rule = str(craft.get("instruction") or "").strip()
    picks = _craft_fit(craft.get("exemplars") or [],
                       craft.get("craft_words") or craft.get("words") or 0)
    if not label:
        return ""
    parts = []
    if mode in ("instruction", "both") and rule:
        parts.append(CRAFT_RULE_HEAD.format(label=label, instruction=rule))
    if mode in ("exemplars", "both") and picks:
        head = str(craft.get("head") or "").strip() or CRAFT_HEAD
        try:
            head = head.format(label=label, n=len(picks))
        except (KeyError, IndexError):
            pass
        parts.append(head + "\n\n" + CRAFT_JOIN.join(picks))
    return "\n\n".join(parts)



PROJECT_CONTEXT_HEAD = (
    "PROJECT CONTEXT — established material for this project. Treat it as "
    "already true: do not contradict it, and do not restate it as exposition.")

POOL_ROLES = ("context", "under", "secret")
ROLE_HEAD = {
    "context": PROJECT_CONTEXT_HEAD,
    "under": (
        "UNDERNEATH — currents running under this whole project. Weave them "
        "through the viewpoint character's interiority and behaviour; show the "
        "current, never announce it as a summary."),
    "secret": (
        "SECRET — withheld information for this whole project. No one states, "
        "reveals or resolves any of it in the prose, and it is never said aloud. "
        "A character who knows it may think about it privately and act under its "
        "weight; characters who do not know it encounter only its unexplained "
        "traces."),
}


def ledgers_block(blocks) -> str:
    out = []
    for b in (blocks or []):
        if not isinstance(b, dict):
            continue
        groups = []
        for g in (b.get("groups") or []):
            texts = [str(e.get("text") or "").strip()
                     for e in (g.get("entries") or [])
                     if str(e.get("text") or "").strip()]
            if not texts:
                continue
            label = str(g.get("label") or "").strip()
            body = "\n".join("- " + t for t in texts)
            groups.append((label + ":\n" + body) if label and label != "—" else body)
        if not groups:
            continue
        head = str(b.get("instruction") or "").strip()
        out.append((head + "\n\n" if head else "") + "\n\n".join(groups))
    return "\n\n".join(out)


QUOTE_LINES_CAT = {
    "id": "quote_lines",
    "label": "Lines",
    "instruction": (
        "LINES — one-liners the author has already written and wants spoken "
        "in this story. Place one only where a scene ALREADY gives a "
        "character the chance to say it, and place NONE where no scene does: "
        "using none of them is a correct answer, and a line forced in is "
        "worse than a line left unused — nothing is lost by leaving one for "
        "later. Never bend a scene to fit one, and never add a beat so a line "
        "has somewhere to land: no chance, no line. Each is a line of speech: "
        "a character says it aloud, in dialogue on the page — never narrated, "
        "never summarised, never folded into description. Keep it close to as "
        "written, but fit it to the speaker and the moment: a name may "
        "change, a tail may be trimmed, a clause may move."),
}

QUOTE_SETPIECES_CAT = {
    "id": "quote_setpieces",
    "label": "Set pieces",
    "instruction": (
        "SET PIECES — long lines the author has already written and wants "
        "spoken in this story. Each one CARRIES A WHOLE BEAT of its own — a "
        "briefing, a speech, a moment the story has stopped for — so placing "
        "it is a plot decision, not a dialogue one. Place one only where the "
        "scene it needs ALREADY EXISTS in the plan, or is being planned "
        "anyway for its own reasons: NEVER invent a scene to house one, and "
        "never stretch or reshape a scene so a set piece has somewhere to "
        "live. Placing NONE is a correct answer — one forced into a scene "
        "that cannot hold it is worse than one left unused, and nothing is "
        "lost by leaving it for a later chapter that has room. It is speech: "
        "a character delivers it aloud, in dialogue on the page — never "
        "narrated, never summarised, never folded into description. Keep it "
        "close to as written, but fit it to the speaker and the moment: a "
        "name may change, a tail may be trimmed, a clause may move."),
}

QUOTE_CATS = (QUOTE_LINES_CAT, QUOTE_SETPIECES_CAT)

QUOTES_RIDES = ("chapter_plot", "suggest")


def project_context_block(entries) -> str:
    items = [e for e in (entries or [])
             if isinstance(e, dict) and str(e.get("text") or "").strip()]
    if not items:
        return ""
    out = []
    for role in POOL_ROLES:
        group = [e for e in items
                 if (e.get("role") if e.get("role") in POOL_ROLES else "context") == role]
        if not group:
            continue
        ordered = ([e for e in group if e.get("always")]
                   + [e for e in group if not e.get("always")])
        out.append(ROLE_HEAD[role])
        for e in ordered:
            label = str(e.get("label") or e.get("id") or "Context").strip()
            out.append("## " + label + "\n" + str(e["text"]).strip())
    return "\n\n".join(out)


def compile_compose(req: dict) -> str:
    sources = [x for x in (req.get("sources") or [])
               if isinstance(x, dict) and str(x.get("text") or "").strip()]
    want = str(req.get("want") or "one consolidated document").strip()
    instr = str(req.get("instruction") or "").strip()
    blocks = [
        (f"HOUSEKEEPING TASK — not prose, not a story. Read the labelled "
         f"material below and produce {want}."),
        ("RULES:\n"
         "- Use ONLY what is in the material. Invent nothing.\n"
         "- Keep every distinct item; fold duplicates into one entry.\n"
         "- Where the sources disagree, keep both readings rather than picking "
         "one.\n"
         "- Drop the source headings \u2014 the result covers the whole book.\n"
         "- Output the result only: no preamble, no commentary, no closing "
         "remark."),
    ]
    if instr:
        blocks.append("WHAT THE AUTHOR ASKED FOR:\n" + instr)
    if not sources:
        blocks.append("THE MATERIAL:\n\n(nothing to work from)")
    else:
        body = "\n\n".join("## " + str(x.get("label") or "Source").strip()
                            + "\n" + str(x["text"]).strip() for x in sources)
        blocks.append(f"THE MATERIAL — {len(sources)} source"
                      + ("s" if len(sources) != 1 else "") + ":\n\n" + body)
    return DIVIDER.join(blocks)


EVIDENCE_MIN_WORDS = 4

_QUOTE_CHARS = "\"“”«»"
_EVIDENCE_LINE = re.compile(r"^\s*[" + _QUOTE_CHARS + r"].*$")
_INLINE_QUOTE = re.compile(r"[" + _QUOTE_CHARS + r"]([^" + _QUOTE_CHARS
                           + r"]+)[" + _QUOTE_CHARS + r"]")
_TRAILING_QUOTE = re.compile(r"\s*[" + _QUOTE_CHARS + r"][^" + _QUOTE_CHARS
                             + r"]+[" + _QUOTE_CHARS + r"][\s.,;:!?)]*$")


def strip_evidence(text: str) -> str:
    out = []
    for line in str(text or "").splitlines():
        if _EVIDENCE_LINE.match(line):
            continue
        line = _TRAILING_QUOTE.sub("", line)
        line = _INLINE_QUOTE.sub(
            lambda m: "" if len(m.group(1).split()) >= EVIDENCE_MIN_WORDS
            else m.group(0), line)
        if line.strip():
            out.append(line.rstrip())
    return "\n".join(out).strip()


BAKE_RULES = (
    "RULES:\n"
    "- FOLD the material into ONE sheet. This is not a collection: two "
    "findings that say the same thing become one line, and a finding that "
    "adds nothing to the sheet is dropped.\n"
    "- The sheet is AT MOST 500 WORDS, and rides in every later request, so "
    "every line must earn its place. Within that it may grow as the book "
    "teaches more: a sheet folded from four chapters is allowed to say more "
    "than one folded from a single study. Length is never the goal - a "
    "section earns words only by saying something the sheet does not "
    "already say.\n"
    "- The material below is RAW INPUT, never sections to keep. Do not carry "
    "its headings, its numbering or its blocks through to the result.\n"
    "- Name NO proper noun from any studied text - no person, no place, no "
    "title, not even inside an example. Where you need one, INVENT a neutral "
    "substitute: the craft must travel without the setting.\n"
    "- Quote at most SIX consecutive words from any source.\n"
    "- End each section with its provenance in parentheses, using the PROCESS "
    "LABELS exactly as they are given below - never a story's name.\n"
    "- Output the sheet only: no preamble, no commentary, no closing remark.")


def compile_bake(req: dict) -> str:
    sources = [x for x in (req.get("sources") or []) if isinstance(x, dict)]
    want = str(req.get("want") or "one standing artifact").strip()
    instr = str(req.get("instruction") or "").strip()
    blocks = [(f"BAKE TASK - not prose, not a story, and not a report about "
               f"the material. Read the labelled material below and produce "
               f"{want}."), BAKE_RULES]
    if instr:
        blocks.append("WHAT THE AUTHOR ASKED FOR:\n" + instr)
    clean = [(str(x.get("label") or "source").strip(),
              strip_evidence(x.get("text"))) for x in sources]
    clean = [(lab, txt) for lab, txt in clean if txt]
    if not clean:
        blocks.append("THE MATERIAL:\n\n(nothing to work from)")
    else:
        body = "\n\n".join(f"## {lab}\n{txt}" for lab, txt in clean)
        blocks.append(f"THE MATERIAL - {len(clean)} source"
                      + ("s" if len(clean) != 1 else "")
                      + ", labelled by the process that made them:\n\n" + body)
    return DIVIDER.join(blocks)


INSTRUCTION_FIELDS = [
    ("plot", "PLOT"),
    ("descriptions", "DESCRIPTIONS"),
    ("custom", "ALSO"),
]


def instructions_block(instr) -> str:
    instr = instr or {}
    lines = [f"{label}: {str(instr[key]).strip()}"
             for key, label in INSTRUCTION_FIELDS
             if str(instr.get(key) or "").strip()]
    if not lines:
        return ""
    return ("AUTHOR'S STANDING INSTRUCTIONS for this book \u2014 these outrank "
            "everything except what is already on the page:\n" + "\n".join(lines))



SUMMARY_FIELDS = [
    ("events", "WHAT HAPPENED"),
    ("ending", "HOW IT ENDED"),
    ("characters", "WHO IS INVOLVED"),
    ("atmosphere", "ATMOSPHERE"),
    ("style", "WRITING STYLE"),
]
SUMMARY_HEAD = "EARLIER, PART BY PART:"


def compile_part_summary(req: dict) -> str:
    text = (req.get("text") or "").strip()
    title = (req.get("title") or "this part").strip()
    lines = "\n".join(f"{head}: <one or two sentences>"
                       for _, head in SUMMARY_FIELDS)
    return DIVIDER.join([
        ("SUMMARY TASK - not prose. Summarise the passage below so the model "
         "writing the NEXT part knows what it needs to know without reading it "
         "in full."),
        ("FORMAT - exactly these five lines, each starting with its label in "
         "capitals, nothing before or after:\n" + lines),
        ("Be concrete: name people and places rather than describing them "
         "abstractly. WHAT HAPPENED is events, not theme. HOW IT ENDED is the "
         "final beat, so the next part can continue from it. WRITING STYLE "
         "names person, tense and register, so the voice carries over."),
        f"THE PASSAGE ({title}):\n\n{text}",
    ])


def parse_part_summary(text: str) -> dict:
    out = {}
    if not text:
        return out
    heads = {head.lower(): key for key, head in SUMMARY_FIELDS}
    heads.update({key.lower(): key for key, _ in SUMMARY_FIELDS})
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*#> ").strip()
        line = line.replace("**", "")
        if ":" not in line:
            continue
        label, _, val = line.partition(":")
        key = heads.get(label.strip().lower())
        if key and val.strip() and key not in out:
            out[key] = " ".join(val.split())
    return out


def part_summaries_block(entries) -> str:
    if not entries:
        return ""
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        fields = [(head, (e.get(key) or "").strip())
                  for key, head in SUMMARY_FIELDS]
        fields = [(h, v) for h, v in fields if v]
        if not fields:
            continue
        title = (e.get("title") or "").strip() or f"part {e.get('part')}"
        out.append(title + "\n"
                   + "\n".join(f"  {h}: {v}" for h, v in fields))
    if not out:
        return ""
    return (SUMMARY_HEAD + "\n"
            "(what the story has done so far, part by part. The most recent "
            "part also appears in full at the end - these are its facts, "
            "that is its voice.)\n\n" + "\n\n".join(out))


SETTLED_HEAD = ("ALREADY HAPPENED — none of it is repeated, re-told or re-planned. "
                "Refer to it only as the past:")
STANDS_HERE = "THE STORY STANDS HERE, at the end of"
AFTER_THIS = ("Everything you plan happens AFTER this line. "
              "Do not begin by re-telling it.")


def settled_block(entries) -> str:
    rows, last_title, last_end = [], "", ""
    for e in (entries or []):
        if not isinstance(e, dict):
            continue
        ev = (e.get("events") or "").strip()
        if not ev:
            continue
        title = (e.get("title") or "").strip() or f"part {e.get('part')}"
        rows.append(f"- {title}: {ev}")
        last_title, last_end = title, (e.get("ending") or "").strip()
    if not rows:
        return ""
    out = SETTLED_HEAD + "\n" + "\n".join(rows)
    if last_end:
        out += f"\n{STANDS_HERE} {last_title}: {last_end}\n{AFTER_THIS}"
    return out


RESTATED_RUN_WORDS = 8


def settled_text(entries) -> str:
    bits = []
    for e in (entries or []):
        if not isinstance(e, dict):
            continue
        for k in ("events", "ending"):
            v = (e.get(k) or "").strip()
            if v:
                bits.append(v)
    return "\n".join(bits)


def restated_runs(text, entries, n: int = RESTATED_RUN_WORDS) -> dict:
    return copied_runs(text, settled_text(entries), n)



DOCTRINE_KINDS = ("plot", "prose")

DOCTRINE_RIDES = {
    "plot": ("chapter_plot", "suggest", "write_part", "stage_plan"),
    "prose": ("write_part", "expand_para", "replace_para"),
}

DOCTRINE_HEAD = {
    "plot": (
        "STORY CRAFT — the standard this book is written to. It governs how "
        "groundwork is laid, how people talk, and how withheld things behave. "
        "Follow it in the shape of the scene, not by mentioning it."),
    "prose": (
        "PROSE CRAFT — the standard this book is written to. It governs how the "
        "words sit on the page: what gets a paragraph, what gets a sentence, "
        "and what a reader is able to see and hear. Follow it in the writing, "
        "never by naming it."),
}

FEEDBACK_RIDES = {
    "story": ("chapter_plot", "suggest", "write_part", "stage_plan"),
    "style": ("write_part", "expand_para", "replace_para"),
}

FEEDBACK_RIDDEN_KINDS = frozenset(
    k for kinds in FEEDBACK_RIDES.values() for k in kinds)


def feedback_riding(entries, kind) -> list:
    k = str(kind or "")
    if k not in FEEDBACK_RIDDEN_KINDS:
        return list(entries or [])
    out = []
    for e in entries or []:
        ch = (e.get("channel") or "story") if isinstance(e, dict) else None
        rides = FEEDBACK_RIDES.get(ch)
        if rides is None or k in rides:
            out.append(e)
    return out


STEP_TABLE = {
    "steps": {
        "chapter_plot": "plot",
        "suggest": "plot",
        "ideas": "plot",
        "stage_plan": "plot",
        "write_part": "prose",
        "expand_para": "prose",
        "replace_para": "prose",
    },
    "both_stages": ["material"],
    "default": "both",
}


def step_of(kind, structured=None) -> str:
    k = str(kind or "")
    if k == "stage_plan":
        stage = str((structured or {}).get("stage") or "")
        if any(name in stage for name in STEP_TABLE["both_stages"]):
            return "both"
    return STEP_TABLE["steps"].get(k, STEP_TABLE["default"])


SAMPLING_BY_STEP = {
    "prose": {"dry_multiplier": 0.8, "dry_base": 1.75, "dry_allowed_length": 4},
    "plot": {},
    "both": {},
}


def sampling_for(kind, structured=None) -> dict:
    return dict(SAMPLING_BY_STEP.get(step_of(kind, structured)) or {})


COPIED_RUN_WORDS = 10

_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)


def _words_for_copy(text) -> list:
    return _WORD.findall(str(text or "").lower())


def copied_runs(text, canon, n: int = COPIED_RUN_WORDS) -> dict:
    tw, cw = _words_for_copy(text), _words_for_copy(canon)
    n = max(1, int(n))
    if len(tw) < n or len(cw) < n:
        return {"runs": 0, "words": 0, "sample": []}
    canon_grams = {tuple(cw[i:i + n]) for i in range(len(cw) - n + 1)}
    seen, sample, covered = set(), [], set()
    for i in range(len(tw) - n + 1):
        g = tuple(tw[i:i + n])
        if g not in canon_grams:
            continue
        covered.update(range(i, i + n))
        if g not in seen:
            seen.add(g)
            if len(sample) < 3:
                sample.append(" ".join(g))
    return {"runs": len(seen), "words": len(covered), "sample": sample}


def self_echo(text, n: int = COPIED_RUN_WORDS) -> dict:
    tw = _words_for_copy(text)
    n = max(1, int(n))
    if len(tw) < n * 2:
        return {"runs": 0, "words": 0, "sample": []}
    first, seen, sample, covered = {}, set(), [], set()
    for i in range(len(tw) - n + 1):
        g = tuple(tw[i:i + n])
        if g not in first:
            first[g] = i
            continue
        if i - first[g] < n:
            continue
        covered.update(range(first[g], first[g] + n))
        covered.update(range(i, i + n))
        if g not in seen:
            seen.add(g)
            if len(sample) < 3:
                sample.append(" ".join(g))
    return {"runs": len(seen), "words": len(covered), "sample": sample}


DOCTRINE_FLAGS = ("TESTABLE", "ADVISORY")

_DOC_LINE = re.compile(
    r"^([A-Z]{1,3}\d{1,3})\s+(TESTABLE|ADVISORY)\s+"
    r"(owner:|seed:|study:\S+|feedback:\S+)\s+"
    r"(?:\[rides:([a-z_,]+)\]\s+)?"
    r"(\S.*)$")


def parse_doctrine(text: str) -> dict:
    lines, bad = [], []
    for raw in (text or "").splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or s.startswith(">"):
            continue
        m = _DOC_LINE.match(s)
        if m:
            rides = tuple(r for r in (m.group(4) or "").split(",") if r)
            lines.append({"id": m.group(1), "flag": m.group(2),
                          "provenance": m.group(3), "rides": rides,
                          "text": m.group(5).strip()})
        elif re.match(r"^[A-Z]{1,3}\d{1,3}\s", s):
            bad.append(s)
    return {"lines": lines, "bad": bad}


def doctrine_version(text: str) -> str:
    return text_sha(norm_text(text or ""))[:12]




def ask_version(text: str) -> str:
    return doctrine_version(text)


def _ask_pick(req: dict, name: str, shipped: str) -> dict:
    over = req.get("asks")
    e = over.get(name) if isinstance(over, dict) else None
    if isinstance(e, dict) and str(e.get("text") or "").strip():
        text, src = str(e["text"]), str(e.get("source") or "project")
    else:
        text, src = str(shipped or ""), "shipped"
    return _ask_record(name, text, src)


def _ask_record(name: str, text: str, source: str) -> dict:
    body = str(text or "")
    rode = bool(body.strip())
    return {"name": name, "text": body,
            "source": source if rode else "none",
            "version": ask_version(body) if rode else ""}


def ask_edit_row(name: str, text: str, source: str, parent: str,
                 at: str) -> dict:
    rec = _ask_record(name, text, source)
    return {"version": rec["version"], "parent": str(parent or ""),
            "at": str(at or ""), "source": rec["source"], "text": rec["text"]}


def doctrine_rides(line: dict, kind_of_doctrine: str) -> tuple:
    return line.get("rides") or DOCTRINE_RIDES.get(kind_of_doctrine, ())


def doctrine_block(req: dict) -> str:
    kind = req.get("kind") or ""
    docs = req.get("doctrines") or {}
    out = []
    for which in DOCTRINE_KINDS:
        d = docs.get(which) or {}
        parsed = parse_doctrine(d.get("text") or "")
        riding = [ln for ln in parsed["lines"]
                  if not kind or kind in doctrine_rides(ln, which)]
        if not riding:
            continue
        body = "\n".join("- " + ln["text"] for ln in riding)
        out.append(DOCTRINE_HEAD[which] + "\n\n" + body)
    return ("\n\n".join(out)) if out else ""



_CRIT_HEAD = re.compile(
    r"^###\s+(C-[A-Z]{1,3}\d{1,3}[a-z]?)\s+serves:([A-Z]{1,3}\d{1,3})"
    r"(?:\s+kinds:([a-z_,]+))?\s*$")


def parse_rubric(text: str) -> dict:
    crit, bad, cur, mode, buf = [], [], None, None, []

    def flush():
        if cur is not None and mode in ("good", "bad"):
            cur[mode] = "\n".join(buf).strip()

    for raw in (text or "").splitlines():
        s = raw.rstrip()
        m = _CRIT_HEAD.match(s.strip())
        if m:
            flush()
            if cur is not None:
                crit.append(cur)
            cur = {"id": m.group(1), "serves": m.group(2),
                   "kinds": tuple(k for k in (m.group(3) or "").split(",") if k),
                   "ask": "", "evidence": "", "good": "", "bad": ""}
            mode, buf = None, []
            continue
        if cur is None:
            if s.strip().startswith("### "):
                bad.append(s.strip()[:90])
            continue
        if s.strip() in ("GOOD>>>", "BAD>>>"):
            flush()
            mode, buf = s.strip()[:-3].lower(), []
            continue
        if s.strip() == "<<<":
            flush()
            mode, buf = None, []
            continue
        if mode:
            buf.append(raw)
        elif s.startswith("ASK:"):
            cur["ask"] = s[4:].strip()
        elif s.startswith("EVIDENCE:"):
            cur["evidence"] = s[9:].strip()
    flush()
    if cur is not None:
        crit.append(cur)
    return {"criteria": crit, "bad": bad}


def rubric_coverage(doctrine_text: str, rubric_text: str) -> dict:
    testable = [l["id"] for l in parse_doctrine(doctrine_text)["lines"]
                if l["flag"] == "TESTABLE"]
    served = {c["serves"] for c in parse_rubric(rubric_text)["criteria"]}
    return {"covered": [i for i in testable if i in served],
            "uncovered": [i for i in testable if i not in served]}



READER_HEAD = (
    "You are reading ONE passage against ONE criterion. You are not the "
    "author's friend and you are not looking for things to praise. You are "
    "also not looking for things to punish.")

VERDICTS = ("PASS", "FAIL", "UNCLEAR")
QUOTE_MIN_WORDS = 3


def reader_prompt(criterion: dict, passage: str, reader_head: str = "") -> str:
    c = criterion or {}
    return "\n\n".join([
        (reader_head or READER_HEAD).strip(),
        "THE CRITERION:\n" + (c.get("ask") or "").strip(),
        "IF YOU ANSWER FAIL, YOU MUST QUOTE:\n" + (c.get("evidence") or "").strip(),
        "Answer on two lines exactly:\nVERDICT: PASS or FAIL or UNCLEAR\n"
        "QUOTE: the words from the passage, or leave empty",
        "THE PASSAGE:\n" + (passage or "").strip(),
    ])


def judge_norm(s: str) -> str:
    s = (s or "")
    for a, b in (("’", "'"), ("‘", "'"), ("“", '"'),
                 ("”", '"'), ("—", "-"), ("–", "-"),
                 ("…", "...")):
        s = s.replace(a, b)
    return " ".join(s.split()).casefold()


_V_LINE = re.compile(r"^\W*verdict\W*[: ]\s*\**\s*(pass|fail|unclear)\b",
                     re.I | re.M)
_Q_LINE = re.compile(r"^\W*quote\W*[: ]\s*(.*)$", re.I | re.M)


def parse_verdict(text: str) -> dict:
    m = _V_LINE.search(text or "")
    q = _Q_LINE.search(text or "")
    quote = (q.group(1).strip() if q else "").strip('"“” ').strip()
    return {"verdict": (m.group(1).upper() if m else ""), "quote": quote}


def judge(text: str, passage: str) -> dict:
    p = parse_verdict(text)
    v, quote = p["verdict"], p["quote"]
    out = {"verdict": v or "UNCLEAR", "quote": quote, "void": False,
           "void_reason": "", "raw": (v or "")}

    if len(_V_LINE.findall(text or "")) > 1:
        out.update(verdict="UNCLEAR", quote="",
                   void_reason="more than one verdict in the answer")
    elif v not in VERDICTS:
        out.update(verdict="UNCLEAR", void_reason="no verdict could be read")
    elif v == "FAIL":
        why = _fail_is_evidenced(quote, passage)
        if why:
            out.update(verdict="UNCLEAR", void=True, void_reason=why)
    out["green"] = out["verdict"] != "FAIL"
    return out



STUDY_CHARS_PER_TOKEN = 4.4
STUDY_SAFETY = 0.90

STUDY_ASPECTS = {
    "style": {
        "strategy": "blind",
        "task": (
            "Describe the WRITING STYLE of this text: sentence length and "
            "rhythm, paragraph shape, dialogue handling and attribution, "
            "point-of-view distance, sensory texture, and any recurring "
            "habits. Report MECHANICAL decisions a writer could imitate, "
            "not content."),
    },
    "plot": {
        "strategy": "sequential",
        "task": (
            "Describe how the PLOT is built in this text: what changes, what "
            "is set up and what pays off, how tension moves, what is "
            "withheld. Report the craft decisions, in the order the text "
            "makes them."),
    },
    "theme": {
        "strategy": "sequential",
        "task": (
            "List the THEMES present in this text - concrete subject matter "
            "and thematic currents - and where each surfaces. Short noun "
            "phrases with their evidence."),
    },
    "custom": {
        "strategy": "blind",
        "task": None,
    },
}


def ask_of_study(req: dict) -> dict:
    aspect = str(req.get("aspect") or "style")
    cfg = STUDY_ASPECTS.get(aspect) or STUDY_ASPECTS["style"]
    name = "study." + (aspect if aspect in STUDY_ASPECTS else "style")
    if cfg.get("task"):
        return _ask_pick(req, name, cfg["task"])
    return _ask_record(name, str(req.get("ask") or "").strip(), "request")


STUDY_BRIEF_CAP = 4000


def study_chunks(text: str, ctx_tokens: int, max_tokens: int,
                 overhead_chars: int = 1200, budget_chars: int = None) -> list:
    if budget_chars:
        budget = max(int(budget_chars), 50)
    else:
        budget = int((int(ctx_tokens or 16384) * STUDY_SAFETY
                      - int(max_tokens or 0)) * STUDY_CHARS_PER_TOKEN)                  - int(overhead_chars)
        budget = max(budget, 2000)
    text = text or ""
    if len(text) <= budget:
        return [text]
    chunks, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        if not para.strip():
            continue
        if len(para) > budget:
            if cur:
                chunks.append(cur)
                cur = ""
            for i in range(0, len(para), budget):
                chunks.append(para[i:i + budget])
            continue
        cand = (cur + "\n\n" + para) if cur else para
        if len(cand) > budget:
            chunks.append(cur)
            cur = para
        else:
            cur = cand
    if cur:
        chunks.append(cur)
    return chunks


STUDY_FINDINGS_FORMAT = (
    "FORMAT: numbered FINDINGS, one per line, most important first. Every "
    "finding carries a SHORT QUOTE from the text in double quotes as its "
    "evidence. No preamble, no commentary about the task.")


def counted_facts_block(text: str) -> str:
    try:
        import loom_axes as X
        import loom_study as S
        p = X.flat_profile(text)
        m = S.count_metrics(text)
    except Exception:
        return ""
    if not p.get("words"):
        return ""
    a = m.get("c6_attribution") or {}
    plain, fancy = int(a.get("plain") or 0), int(a.get("elaborate") or 0)
    tags = plain + fancy
    senses = [k for k in ("sight", "sound", "touch", "smell", "taste")
              if (m.get("c8_senses") or {}).get(k)]
    lines = [
        f"- {p['words']:,} words in {m.get('paragraphs', 0)} paragraphs; dialogue "
        f"in {round(100 * p['dialogue_ratio'])}% of paragraphs",
        (f"- dialogue tags: {tags} - {plain} plain (said/asked), {fancy} "
         f"elaborate. PLAIN means said, says, asked, asks and nothing else; "
         f"whispered, hissed, ordered, instructed, shouted, muttered, replied "
         f"are ELABORATE"
         if tags else "- dialogue tags: none"),
        f"- sentences: median {p['sentence_median']} words, quartile spread "
        f"{p['sentence_spread']}; {round(100 * p['short_sentence_share'])}% are "
        f"6 words or fewer; paragraphs median {p['paragraph_median']} words",
        f"- filter words (saw, felt, noticed, realized, seemed...): "
        f"{p['filter_per_1000']} per 1000 words",
        f"- abstract nouns (-tion, -ness, -ment...): {p['abstract_per_1000']} per 1000",
        f"- em dashes {p['em_dash_per_1000']} per 1000; temporal markers (then, "
        f"later, by the time...) {p['temporal_per_1000']} per 1000; numerals "
        f"{p['numeral_per_1000']} per 1000",
        f"- bodies and physical effort (hands, shoulders, hauled, staggered...): "
        f"{p['physical_per_1000']} per 1000",
        f"- senses present: {len(senses)} of 5"
        + (f" ({', '.join(senses)})" if senses else ""),
    ]
    return ("COUNTED FACTS about this text - measured, not judged. A finding "
            "may not contradict them: do not call dialogue sparse when it is "
            "in half the paragraphs, or attribution minimal when most tags are "
            "elaborate. Describe THIS text, not what good prose usually does.\n"
            + "\n".join(lines))


STUDY_SUBJECT_KINDS = ("prose", "feedback")


def remark_census_block(entries) -> str:
    rows = [e for e in (entries or []) if isinstance(e, dict)]
    if not rows:
        return ""
    votes = len(rows)
    remarks = [e for e in rows if (e.get("comment") or "").strip()]
    distinct = len({_dedupe_key(e) for e in remarks})

    channels = {}
    for e in remarks:
        k = str(e.get("channel") or "story")
        channels[k] = channels.get(k, 0) + 1
    ch_order = [k for k in ("story", "style") if channels.get(k)]
    ch_order += sorted(k for k in channels if k not in ("story", "style"))

    stars = {}
    for e in rows:
        s = _stars_of(e)
        stars[s] = stars.get(s, 0) + 1

    lever_counts, no_lever = {}, 0
    for e in rows:
        named = _levers_of(e)
        if not named:
            no_lever += 1
        for k in named:
            lever_counts[k] = lever_counts.get(k, 0) + 1
    ranked = sorted(lever_counts.items(), key=lambda kv: (-kv[1], kv[0]))

    lines = [
        f"- {len(remarks)} remarks carrying a comment, of {votes} votes; "
        f"{distinct} distinct",
        "- channel: " + (", ".join(f"{channels[k]} {k}" for k in ch_order)
                         or "no remark carries a comment"),
        "- " + ", ".join(
            [f"{stars[s]} at {s} star" + ("" if s == 1 else "s")
             for s in (3, 2, 1) if stars.get(s)]
            + ([f"{stars[0]} unrated"] if stars.get(0) else [])),
        "- levers named: " + (", ".join(f"{k} {n}" for k, n in ranked)
                              or "none"),
        f"- {no_lever} of {votes} carry no lever at all",
    ]
    return ("COUNTED FACTS about these remarks - measured, not judged. A "
            "finding may not contradict them: do not call his attention mostly "
            "stylistic when half the remarks are on the story channel, or say "
            "he rarely rates when most of them carry three stars. Describe "
            "THESE remarks, not what reader feedback usually says.\n"
            + "\n".join(lines))


LEVER_ASK_NO_MEASURE = "[NOT MEASURED]"


def lever_ask() -> str:
    import loom_measures as M
    levers = dict(M.LEVERS)
    if not levers:
        return ""

    buckets = {}
    for name, spec in levers.items():
        spec = spec or {}
        buckets.setdefault(str(spec.get("group") or "ungrouped"),
                           []).append((name, spec))
    order = [g for g in M.LEVER_GROUPS if g in buckets]
    order += sorted(g for g in buckets if g not in M.LEVER_GROUPS)

    unmeasured, blocks = [], []
    for g in order:
        lines = [g.upper()]
        for name, spec in buckets[g]:
            label = str(spec.get("label") or name)
            if spec.get("unmeasured"):
                unmeasured.append(label)
                lines.append(f"- {label}  {LEVER_ASK_NO_MEASURE}")
            else:
                lines.append(f"- {label}")
        blocks.append("\n".join(lines))

    parts = [
        f"DATAMINE THE CRAFT LEVERS. This project steers writing by "
        f"{len(levers)} named levers, grouped below by where an author is "
        f"working when he reaches for one. Go through the text and report, "
        f"lever by lever, what it gives you on each - how the lever is used, "
        f"if the text is a passage; what is said about it, if the text is a "
        f"pile of remarks. A lever the text never touches is a finding too: "
        f"say so plainly rather than leaving it out.",
    ]
    parts += blocks
    if unmeasured:
        parts.append(
            f"NOT EVERY LEVER HAS A MEASURE. {len(unmeasured)} of the "
            f"{len(levers)} above are marked {LEVER_ASK_NO_MEASURE}: "
            + ", ".join(unmeasured)
            + ". Nothing in this project counts them - there is no score "
              "behind them and no band they sit high or low against. Read "
              "them out of the text exactly as carefully, and do not write as "
              "though a number agreed with you.")
    parts.append(
        "EVIDENCE. Every lever you report carries a SHORT QUOTE from the "
        "text, in double quotes - the words that made you say it. If you "
        "cannot point at the words, you cannot report the lever; say instead "
        "that the text does not use it.")
    return "\n\n".join(parts)


def compile_study(req: dict) -> str:
    no = int(req.get("chunk_no") or 1)
    of = int(req.get("chunk_of") or 1)
    head = (f"STUDY TASK - you are analysing one passage to learn how it was "
            f"made. Work ONLY from the text given.")
    subject_head = (f"THE TEXT (part {no}/{of} of "
                    f"{str(req.get('label') or 'the subject')}):"
                    if of > 1 else
                    f"THE TEXT ({str(req.get('label') or 'the subject')}):")
    blocks = [head]
    brief = str(req.get("brief") or "").strip()
    if brief:
        blocks.append("FINDINGS SO FAR, from the parts before this one - "
                      "continue them, do not repeat them:\n"
                      + brief[:STUDY_BRIEF_CAP])
    blocks.append(subject_head + "\n" + str(req.get("subject") or ""))
    if str(req.get("subject_kind") or "prose") == "feedback":
        facts = remark_census_block(req.get("entries") or [])
    else:
        facts = counted_facts_block(str(req.get("subject") or ""))
    if facts:
        blocks.append(facts)
    task = ask_of_study(req)["text"]
    blocks.append("TASK: " + (task or "(no ask was given)"))
    blocks.append(STUDY_FINDINGS_FORMAT)
    return DIVIDER.join(blocks)


def compile_study_merge(req: dict) -> str:
    aspect = str(req.get("aspect") or "style")
    items = [str(f or "").strip() for f in (req.get("findings") or [])]
    items = [f for f in items if f]
    blocks = [
        ("MERGE THE FINDINGS - below are per-part findings from "
         f"{int(req.get('chunk_of') or len(items))} parts of "
         f"{str(req.get('label') or 'one subject')}, analysed for "
         f"{aspect.upper()}. Produce ONE deduplicated findings sheet for the "
         "whole subject: keep every distinct finding, join the ones that are "
         "the same, keep each finding's quote."),
        "TASK CONTEXT: " + (ask_of_study(req)["text"] or "(no ask was given)"),
    ]
    for i, f in enumerate(items, start=1):
        blocks.append(f"FINDINGS, part {i}:\n{f}")
    blocks.append(STUDY_FINDINGS_FORMAT)
    return DIVIDER.join(blocks)



SGX_CHARS_PER_TOKEN = 4

SGX_RESERVE_MULT = 2.0

SGX_NAME_CAP = 80
SGX_ASK_CAP = 500

SGX_CARRY_ROW_CHARS = len("99. NAME: \n   ASK: \n")

SGX_CARRY_HEADER = ("PROPOSED SO FAR - from the earlier documents of this "
                    "same collection:")

SGX_SPLIT_SUFFIX_CHARS = len(" [99 of 99]")


def compile_suggest_extracts(req: dict) -> str:
    subs = [s for s in (req.get("subjects") or [])
            if str((s or {}).get("text") or "").strip()]
    if not subs:
        raise ValueError("suggest_extracts needs at least one subject")
    count = max(3, min(20, int(req.get("count") or 10)))
    dropped = [str(d or "").strip() for d in (req.get("dropped") or [])]
    dropped = [d for d in dropped if d]
    proposed = [p for p in (req.get("proposed") or [])
                if isinstance(p, dict)
                and str(p.get("name") or "").strip()
                and str(p.get("ask") or "").strip()]
    batch_no = max(1, int(req.get("batch_no") or 1))
    batch_of = max(1, int(req.get("batch_of") or 1))
    blocks = [
        ("SUGGEST EXTRACTS - you are reading a collection of documents that "
         "will be DATAMINED to learn how their author writes and builds "
         "stories. Propose the extraction questions most worth asking of "
         "THIS material: what is distinctive here that a writer would want "
         "captured? Base every suggestion on what the documents actually "
         "contain, never on genre habit."
         + (f" You are reading part {batch_no} of {batch_of} of this "
            "collection; the rest is read in its own requests."
            if batch_of > 1 else "")),
    ]
    for i, s in enumerate(subs, start=1):
        blocks.append(f"DOCUMENT {i} ({str(s.get('label') or f'doc {i}')}):\n"
                      + str(s.get("text") or ""))
    if dropped:
        blocks.append("ALREADY REJECTED by the author - do not suggest these "
                      "again, or anything that is the same idea renamed:\n- "
                      + "\n- ".join(dropped))
    if proposed:
        blocks.append(SGX_CARRY_HEADER + "\n" + "\n".join(
            f"{i}. NAME: {str(p.get('name') or '').strip()[:SGX_NAME_CAP]}\n"
            f"   ASK: {str(p.get('ask') or '').strip()[:SGX_ASK_CAP]}"
            for i, p in enumerate(proposed, start=1)))
    head = (f"TASK: propose exactly {count} extract-questions."
            if not proposed else
            f"TASK: you have {len(proposed)} proposals from the earlier "
            f"documents of this collection, listed above. Revise the list so "
            f"it fits ALL the material read so far: keep what still holds, "
            f"replace what this new material supersedes, and return exactly "
            f"{count}.")
    blocks.append(
        head + " Cover BOTH "
        "families: how the prose is WRITTEN (style: pace, rhythm, dialogue, "
        "tension, scene shape...) and how the STORY is told beneath the "
        "style (characters, their treatment of each other, development, "
        "structure...). Each suggestion is one line pair:\n"
        "N. NAME: <a short name, 2-5 words>\n"
        "   ASK: <the extraction question, one sentence, answerable from a "
        "single document>\n"
        "Nothing else - no preamble, no closing remarks.")
    return DIVIDER.join(blocks)


def parse_extract_suggestions(text: str) -> list:
    import re as _re
    out = []
    blocks = _re.split(r"\n(?=\s*\d+[.)]\s)", "\n" + str(text or ""))
    for b in blocks:
        b = b.replace("**", "")
        ask = _re.search(r"ASK:\s*(.+(?:\n(?!\s*\d+[.)]|\s*NAME:).+)*)", b)
        if not ask:
            continue
        name = _re.search(r"NAME:\s*(.+)", b)
        if name:
            n = name.group(1).strip()
        else:
            head = _re.match(r"\s*\d+[.)]\s*(.+)", b)
            n = head.group(1).splitlines()[0].strip() if head else ""
            if n.upper().startswith("ASK:"):
                n = ""
        n = n.strip().rstrip(".").rstrip(":").strip()
        a = " ".join(x.strip() for x in ask.group(1).splitlines()).strip()
        if n and a:
            out.append({"name": n[:SGX_NAME_CAP], "ask": a[:SGX_ASK_CAP]})
    return out




def sgx_scaffold_chars(n_docs: int, n_dropped: int, proposed_chars: int = 0,
                       label_chars: int = 0, dropped_chars: int = 0) -> int:
    n_docs = max(1, int(n_docs or 0))
    n_dropped = max(0, int(n_dropped or 0))

    def probe(**extra):
        return compile_suggest_extracts({
            "subjects": [{"label": "x", "text": "x"} for _ in range(n_docs)],
            "dropped": ["x"] * n_dropped,
            "count": 20,
            "batch_no": 1, "batch_of": 99, **extra})

    bare = probe()
    frame = len(bare) - 2 * n_docs - n_dropped
    carry = 0
    if int(proposed_chars or 0) > 0:
        one = probe(proposed=[{"name": "x", "ask": "x"}])
        carry = (len(one) - len(bare)
                 - SGX_CARRY_ROW_CHARS - 2
                 + int(proposed_chars))
    return frame + int(label_chars or 0) + int(dropped_chars or 0) + carry


def sgx_balanced_chunks(text: str, cap: int) -> list:
    cap = max(50, int(cap or 0))
    text = text or ""
    greedy = study_chunks(text, 999_999, 0, budget_chars=cap)
    k = len(greedy)
    if k <= 1:
        return greedy
    lo, hi = max(50, -(-len(text) // k)), cap
    while lo < hi:
        mid = (lo + hi) // 2
        if len(study_chunks(text, 999_999, 0, budget_chars=mid)) <= k:
            hi = mid
        else:
            lo = mid + 1
    out = study_chunks(text, 999_999, 0, budget_chars=lo)
    return out if len(out) == k else greedy


SGX_FEEDBACK_CHANNELS = (("story", "plot feedback"),
                         ("style", "style feedback"))


def sgx_feedback_subject(entries, budgets=None) -> dict:
    rows = [e for e in (entries or []) if isinstance(e, dict)]
    budgets = budgets if isinstance(budgets, dict) else {}
    known = {k for k, _ in SGX_FEEDBACK_CHANNELS}
    survivors, channels, limited = set(), [], False

    def words_of(items):
        return sum(len(_snippet(e).split()) for e in items)

    for key, label in SGX_FEEDBACK_CHANNELS:
        try:
            budget = max(0, int(budgets.get(key) or 0))
        except (TypeError, ValueError):
            budget = 0
        mine = [e for e in rows if str(e.get("channel") or "story") == key]
        if not budget:
            survivors.update(id(e) for e in mine)
            channels.append({"key": key, "label": label, "budget": 0,
                             "total": len(mine), "kept": len(mine),
                             "dropped": 0, "collapsed": 0,
                             "words": words_of(mine)})
            continue
        limited = True
        got = feedback_for_budget(mine, budget)
        survivors.update(id(e) for e in got["kept"])
        channels.append({"key": key, "label": label, "budget": budget,
                         "total": len(mine), "kept": len(got["kept"]),
                         "dropped": len(got["dropped"]),
                         "collapsed": got["collapsed"],
                         "words": got["words"]})
    survivors.update(id(e) for e in rows
                     if str(e.get("channel") or "story") not in known)

    kept = [e for e in rows if id(e) in survivors]
    return {"text": feedback_subject(kept), "channels": channels,
            "limited": limited, "entries": len(rows), "kept": len(kept),
            "dropped": len(rows) - len(kept)}


def sgx_batch_plan(subjects: list, ctx_tokens: int, max_tokens: int,
                   count: int = 10, dropped: list = None,
                   word_budget: int = 0) -> dict:
    subs = []
    for i, s in enumerate(subjects or []):
        s = s if isinstance(s, dict) else {}
        text = str(s.get("text") or "")
        if not text.strip():
            continue
        subs.append({"i": len(subs), "id": str(s.get("id") or ""),
                     "kind": str(s.get("kind") or ""),
                     "label": str(s.get("label") or f"doc {i + 1}"),
                     "text": text, "words": len(text.split())})

    ctx = int(ctx_tokens or 0)
    maxtok = max(0, int(max_tokens or 0))
    count = max(3, min(20, int(count or 10)))
    drops = [str(d or "").strip() for d in (dropped or [])]
    drops = [d for d in drops if d]
    out = {"ctx_tokens": ctx, "max_tokens": maxtok,
           "chars_per_token": SGX_CHARS_PER_TOKEN,
           "reserve_mult": SGX_RESERVE_MULT, "count": count,
           "word_budget": max(0, int(word_budget or 0)),
           "ceiling": 0, "batches": [], "excluded": [],
           "subjects": [],
           "totals": {"subjects": 0, "docs": 0, "chars": 0, "words": 0,
                      "batches": 0, "split": 0},
           "error": None}
    if not subs:
        out["error"] = "nothing to plan - every subject was empty"
        return out
    if ctx <= 0:
        out["error"] = "the model's context window is not known yet"
        return out

    kept, budget_words = subs, out["word_budget"]
    if budget_words:
        kept, used, stop = [], 0, False
        for s in subs:
            if stop or used + s["words"] > budget_words:
                stop = True
                out["excluded"].append({
                    "label": s["label"], "words": s["words"],
                    "why": f"over the {budget_words:,}-word budget"})
                continue
            used += s["words"]
            kept.append(s)
        if not kept:
            out["error"] = (
                f"the {budget_words:,}-word budget is smaller than the first "
                f"subject ({subs[0]['words']:,} words), and a subject is "
                "never cut to fit - raise the budget or untick it")
            return out

    ceiling = int((ctx - SGX_RESERVE_MULT * maxtok) * SGX_CHARS_PER_TOKEN)
    out["ceiling"] = ceiling
    carry = count * (SGX_NAME_CAP + SGX_ASK_CAP + SGX_CARRY_ROW_CHARS)
    drop_chars = sum(len(d) for d in drops)

    def cost(docs: list) -> int:
        return (sgx_scaffold_chars(len(docs), len(drops), carry,
                                   label_chars=sum(len(d["label"])
                                                   for d in docs),
                                   dropped_chars=drop_chars)
                + sum(len(d["text"]) for d in docs))

    def alone_cap(label: str) -> int:
        return ceiling - sgx_scaffold_chars(
            1, len(drops), carry,
            label_chars=len(label) + SGX_SPLIT_SUFFIX_CHARS,
            dropped_chars=drop_chars)

    docs = []
    for s in kept:
        cap = alone_cap(s["label"])
        if cap <= 0:
            out["error"] = (
                "not even one document fits this window once the request's "
                "own framing is counted - the window is too small for this "
                "kind at any split")
            return out
        if len(s["text"]) <= cap:
            docs.append({**s, "part": 1, "parts": 1})
            continue
        pieces = sgx_balanced_chunks(s["text"], cap)
        out["totals"]["split"] += 1
        for k, piece in enumerate(pieces, start=1):
            docs.append({**s, "text": piece, "words": len(piece.split()),
                         "label": f"{s['label']} [{k} of {len(pieces)}]",
                         "part": k, "parts": len(pieces)})

    batches, cur = [], []
    for d in docs:
        if cur and cost(cur + [d]) > ceiling:
            batches.append(cur)
            cur = []
        cur.append(d)
    if cur:
        batches.append(cur)

    for n, group in enumerate(batches, start=1):
        chars = cost(group)
        approx = chars // SGX_CHARS_PER_TOKEN
        room = ctx - approx
        verdict = ("refused" if approx >= ctx or room < maxtok
                   else "tight" if room < maxtok * 1.5 else "ok")
        out["batches"].append({
            "no": n, "of": len(batches), "chars": chars,
            "approx_tokens": approx, "room": room, "verdict": verdict,
            "pct": int(round(100 * approx / ctx)) if ctx else None,
            "docs": [{"label": d["label"], "id": d["id"],
                      "subject": d["i"], "chars": len(d["text"]),
                      "words": d["words"], "part": d["part"],
                      "parts": d["parts"], "text": d["text"]}
                     for d in group]})

    where = {}
    for bat in out["batches"]:
        for d in bat["docs"]:
            where.setdefault(d["subject"], []).append(bat["no"])
    out["subjects"] = [{"i": s["i"], "id": s["id"],
                        "kind": s["kind"] or "document", "label": s["label"],
                        "words": s["words"], "batches": where.get(s["i"], [])}
                       for s in kept]

    by_kind = {}
    for s in kept:
        k = s["kind"] or "document"
        by_kind[k] = by_kind.get(k, 0) + 1
    out["totals"].update({
        "subjects": len(kept), "docs": len(docs), "batches": len(batches),
        "chars": sum(len(d["text"]) for d in docs),
        "words": sum(d["words"] for d in docs),
        "by_kind": by_kind})
    return out


def judge_agree(verdicts: list) -> dict:
    vs = [v for v in (verdicts or []) if isinstance(v, dict)]
    if not vs:
        return {"verdict": "UNCLEAR", "quote": "", "void": False,
                "void_reason": "no samples to judge", "green": True,
                "flaky": False, "split": []}
    split = [v.get("verdict") or "UNCLEAR" for v in vs]
    if len(set(split)) == 1:
        first = dict(vs[0])
        first["void"] = all(v.get("void") for v in vs)
        if not first["void"]:
            first["void_reason"] = ""
        first["flaky"] = False
        first["split"] = split
        return first
    return {"verdict": "UNCLEAR", "quote": "", "void": False,
            "void_reason": "the samples disagreed: " + "/".join(split),
            "green": True, "flaky": True, "split": split}


def _fail_is_evidenced(quote: str, passage: str) -> str:
    if not quote:
        return "FAIL with no quote"
    if len(quote.split()) < QUOTE_MIN_WORDS:
        return (f"quote is under {QUOTE_MIN_WORDS} words, which any passage "
                f"would contain by accident")
    hay = judge_norm(passage)
    frags = [f for f in judge_norm(quote).split("...") if len(f.split()) >= 3]
    for f in (frags or [judge_norm(quote)]):
        if f.strip() and f.strip() not in hay:
            return "quoted words do not occur in the passage"
    return ""


def compile_judge(req: dict) -> str:
    crit = req.get("criterion")
    if not isinstance(crit, dict):
        crit = {"ask": str(crit or "")}
    return reader_prompt(crit, str(req.get("passage") or ""))


def _digest_block(req: dict) -> str:
    d = feedback_digest(feedback_riding(req.get("feedback"), req.get("kind")),
                        req.get("feedback_windows",
                                req.get("feedback_window", FEEDBACK_WINDOW)),
                        req.get("target_part"),
                        req.get("feedback_cap"))
    return ("AUTHOR FEEDBACK — follow these before all else:\n" + d) if d else ""


def _lead_doctrine(blocks: list, req: dict) -> list:
    b = doctrine_block(req)
    return ([b] + blocks) if b else blocks


def _append_digest(blocks: list, req: dict) -> list:
    b = _digest_block(req)
    if b:
        blocks.append(b)
    return blocks


PLAN_HEADS = {
    "verbatim": (
        "THE PLAN FOR THIS PART — follow it top to bottom, in its order. "
        "Where it quotes a drafted line, keep the strongest verbatim and "
        "smooth only what needs smoothing. It outranks invention: do not "
        "add a beat it does not have, and do not skip one it does."),
    "dramatise": (
        "THE PLAN FOR THIS PART — this is what happens, told plainly and in "
        "order. It is not the passage; it is the account you are writing FROM. "
        "Dramatise every beat where it stands and summarise none of them. "
        "None of these sentences appears in your prose — the wording here is "
        "scaffolding, the scene is yours. It outranks invention: do not add a "
        "beat it does not have, and do not skip one it does."),
}
PLAN_MODE_DEFAULT = "verbatim"


def plan_head(req: dict) -> str:
    asked = str(req.get("plan_mode") or "").strip()
    if asked:
        return PLAN_HEADS.get(asked, PLAN_HEADS[PLAN_MODE_DEFAULT])
    plan = str((req.get("part") or {}).get("plan") or "")
    if plan and "MATERIAL" not in plan:
        return PLAN_HEADS["dramatise"]
    return PLAN_HEADS[PLAN_MODE_DEFAULT]


def compile_part(req: dict) -> str:
    chapter = req.get("chapter") or {}
    part = req.get("part") or {}
    facets = chapter.get("facets") or {}
    words = int(req.get("target_words") or 400)
    paras = req.get("target_paragraphs")
    try:
        paras = int(paras) if paras else 0
    except (TypeError, ValueError):
        paras = 0
    if paras > 0:
        words = paras * WORDS_PER_PARAGRAPH

    plan = str(part.get("plan") or "").strip()

    head = []
    if chapter.get("summary") and not plan:
        head.append("CHAPTER: " + chapter["summary"])
    head += _facet_lines(facets)
    head.append(f"THIS PART: {part.get('title', '')}"
                + (("\n" + part["notes"]) if part.get("notes") else ""))

    pmax = req.get("target_paragraphs_max")
    try:
        pmax = int(pmax) if pmax else 0
    except (TypeError, ValueError):
        pmax = 0

    if paras > 0 and pmax > paras:
        length = (f"in between {paras} and {pmax} paragraphs "
                  f"(roughly {words} words). Give each beat its own paragraph; "
                  "do not compress several events into one, and do not pad to "
                  "reach the count - if the scene is done inside the range, "
                  "stop. ")
    elif paras > 0:
        length = (f"in AT LEAST {paras} paragraphs (roughly {words} words). "
                  "Give each beat its own paragraph; do not compress several "
                  "events into one. ")
    else:
        length = f"about {words} words. "
    instr = (f"Write this part of the story as continuous prose, {length}"
             "Dramatise — do not summarise, do not conclude the whole story; stop "
             "mid-motion at the part's edge. Match the voice, tense and person already "
             "on the page. Introduce no new named characters. Never repeat or copy "
             "sentences from the story so far — write only what happens next. "
             "Do not open with a recap of anything the story so far already "
             "showed — the first sentence is new action. "
             "Output prose only: no headings, no preamble, no notes.")
    if facets.get("under"):
        instr += (" Weave UNDERNEATH through the viewpoint character's interiority and"
                  " behaviour — show the current, never announce it as a summary.")
    if facets.get("secret"):
        instr += (" SECRET is withheld information: no one states, reveals or resolves"
                  " it in this passage, and it is never said aloud. A character who knows"
                  " it may think about it privately and act under its weight; characters"
                  " who do not know it encounter only its unexplained traces.")

    blocks = ["\n".join(head)]
    if plan:
        blocks.append(plan_head(req) + "\n\n" + plan)
    cpb = craft_block(req, "plot")
    if cpb:
        blocks.append(cpb)
    for b in (project_context_block(req.get("project_context")),
              ledgers_block(req.get("ledgers")),
              instructions_block(req.get("instructions")),
              _style_block(req.get("style")),
              craft_block(req, "style"),
              part_summaries_block(summaries_for_prompt(req))):
        if b:
            blocks.append(b)
    blocks.append(instr)
    sb = steer_block(req)
    if sb:
        blocks.append(sb)
    trans = transition_block(req)
    canon = canon_from(req).strip()
    if int(req.get("transition_paras") or 0) > 0:
        if trans:
            blocks.append(trans)
        elif not canon:
            blocks.append(CANON_HEAD_NONE)
    elif not canon:
        blocks.append(CANON_HEAD_NONE)
    else:
        opens = opens_chapter(req.get("part_in_chapter"))
        tail = tail_words(canon, canon_window(req))
        if opens:
            tail = last_full_sentence(tail)
        blocks.append((CANON_HEAD_OPENS if opens else CANON_HEAD_CONTINUE)
                      + "\n\n" + tail)
    return DIVIDER.join(_lead_doctrine(_append_digest(blocks, req), req))


def compile_replace(req: dict) -> str:
    paras = req.get("paragraphs") or []
    i = int(req.get("target_index", 0))
    n = int(req.get("n") or 1)
    steers = req.get("steers") or []
    if not paras or i < 0 or i >= len(paras):
        raise ValueError("target_index out of range")

    before = paras[i - 1] if i > 0 else ""
    after = paras[i + 1] if i + 1 < len(paras) else ""
    what = (f"REWRITE TASK — rewrite ONE paragraph. Produce {n} version"
            + ("s" if n > 1 else "")
            + (", separated by a line containing only ===" if n > 1 else "")
            + ". Output only the rewritten paragraph text, no numbering, no commentary.")
    ctx = []
    if before:
        ctx.append("PARAGRAPH BEFORE (do not rewrite, for continuity only):\n" + before)
    ctx.append("PARAGRAPH TO REWRITE:\n" + paras[i])
    if after:
        ctx.append("PARAGRAPH AFTER (do not rewrite, the rewrite must lead into it):\n" + after)

    blocks = [what, "INSTRUCTION FOR WHAT TO CHANGE:\n" + (req.get("instruction") or "")]
    sb = steer_block(req)
    if sb:
        blocks.append(sb)
    pc = project_context_block(req.get("project_context"))
    if pc:
        blocks.append(pc)
    lg = ledgers_block(req.get("ledgers"))
    if lg:
        blocks.append(lg)
    ib = instructions_block(req.get("instructions"))
    if ib:
        blocks.append(ib)
    blocks += ctx
    canon = (req.get("canon_before") or "").strip()
    if canon:
        blocks.append("EARLIER STORY (context only):\n\n"
                      + tail_words(canon, canon_window(req)))
    return DIVIDER.join(_lead_doctrine(_append_digest(blocks, req), req))


def compile_suggest(req: dict) -> str:
    n = int(req.get("n") or 3)
    chapter = req.get("chapter") or {}
    steers = req.get("steers") or []
    title = chapter.get("title") or chapter.get("summary") or "the next part"

    fields = "\n".join([f"{label.split(' (')[0].upper()}: <one line>"
                        for _, label in FACETS])
    blocks = [
        (f"PLANNING TASK — no prose. Propose exactly {n} genuinely DIFFERENT "
         f"direction{'s' if n > 1 else ''} for the next part of the story: \"{title}\". "
         "They must diverge in where the story goes, not just in wording. Ground every "
         "one of them in what has actually happened so far."),
        (f"FORMAT — exactly this block {n} time{'s' if n > 1 else ''}, blocks separated "
         "by a line containing only ===\nTITLE: <a few words>\n" + fields
         + "\nOne line per field, under 18 words each. No other text before, between or "
           "after the blocks."),
    ]
    if chapter.get("summary") and chapter.get("title"):
        blocks.append("CHAPTER: " + chapter["summary"])
    if steers and any(s for s in steers):
        blocks.append("VARIANT GUIDANCE — obey per variant:\n"
                      + "\n".join(f"Variant {k+1} must explore: {s}"
                                  for k, s in enumerate(steers) if s))
    pc = project_context_block(req.get("project_context"))
    if pc:
        blocks.append(pc)
    lg = ledgers_block(req.get("ledgers"))
    if lg:
        blocks.append(lg)
    ib = instructions_block(req.get("instructions"))
    if ib:
        blocks.append(ib)
    sb = part_summaries_block(summaries_for_prompt(req))
    if sb:
        blocks.append(sb)
    canon = canon_from(req).strip()
    if canon:
        blocks.append("THE STORY SO FAR:" + chr(10) + chr(10)
                      + tail_words(canon, canon_window(req)))
    elif summaries_for_prompt(req):
        blocks.append("(The prose itself is not included here - the story so "
                      "far is the summarised parts above.)")
    else:
        blocks.append("(The story has not started yet - use the description "
                      "alone.)")
    return DIVIDER.join(_lead_doctrine(_append_digest(blocks, req), req))


def compile_ideas(req: dict) -> str:
    n = int(req.get("n") or 5)
    focus = (req.get("focus") or "").strip()
    blocks = [
        (f"BRAINSTORM TASK — not prose, not scene plans. Based on the story so far and "
         f"the author's feedback, list exactly {n} concrete ideas for what we could do "
         "with the story from here" + (f", focused on: {focus}" if focus else "") + "."),
        (f"Each idea is 1-3 sentences, numbered \"1.\" to \"{n}.\", and genuinely "
         "different from the others — different in kind where possible (a plot turn, a "
         "relationship shift, a structural move, a reveal, a change of scene). No other "
         "text before or after the list."),
    ]
    pc = project_context_block(req.get("project_context"))
    if pc:
        blocks.append(pc)
    ib = instructions_block(req.get("instructions"))
    if ib:
        blocks.append(ib)
    sb = part_summaries_block(summaries_for_prompt(req))
    if sb:
        blocks.append(sb)
    canon = canon_from(req).strip()
    blocks.append(("THE STORY SO FAR:\n\n" + tail_words(canon, canon_window(req)))
                  if canon
                  else "(Nothing accepted yet — base the ideas on the outline.)")
    return DIVIDER.join(_lead_doctrine(_append_digest(blocks, req), req))


def compile_chapter_plot(req: dict) -> str:
    chapter = req.get("chapter") or {}
    asked = (chapter.get("summary") or "").strip()
    facets = chapter.get("facets") or {}
    n = int(req.get("target_sentences") or 6)
    lo, hi = plan_range(req.get("target_paragraphs"),
                        req.get("target_paragraphs_max"))

    head = ["PLANNING TASK \u2014 write the PLOT of this chapter, not its prose."]
    if chapter.get("title"):
        head.append("CHAPTER: " + chapter["title"])
    lines = _facet_lines(facets)
    if lines:
        head.append("ALREADY SETTLED about this chapter \u2014 do not contradict:\n"
                    + "\n".join(lines))
    instr = (
        f"WHAT THE AUTHOR ASKED FOR:\n{asked}" if asked else
        "WHAT THE AUTHOR ASKED FOR:\n(nothing written yet \u2014 propose the "
        "chapter's plot from the material below)")

    try:
        parts_n = int(req.get("target_parts") or 0)
    except (TypeError, ValueError):
        parts_n = 0

    if parts_n > 0:
        how = (
            f"Answer as EXACTLY {parts_n} sequential part plots, in order, "
            f"together covering the whole chapter and nothing beyond it.\n"
            f"Write each one as:\n\n"
            f"SCENE <n>: <a short name for what this part is>\n"
            f"BEATS:\n"
            f"- <what happens, one event per line, in order>\n\n"
            f"Rules for the division:\n"
            f"- EXACTLY {parts_n} scenes. Not {parts_n - 1}, not {parts_n + 1}.\n"
            + (f"- Between {lo} and {hi} beats in each part plot. Give each "
               f"event its own beat; do not compress several into one, and do "
               f"not pad to reach the count - if the part is covered inside "
               f"the range, stop.\n" if lo else "")
            + "- They run in order and do not overlap: each begins where the one "
            "before it ended.\n"
            "- Divide by what HAPPENS, not by length. A part is a scene, not a "
            "quota of words.\n"
            "- Every beat is an event, told plainly. Not prose, not dialogue, "
            "no scene-setting flourish \u2014 the events, so a writer could work "
            "from them.\n"
            "- Build only on what is given. Invent no new named characters.\n"
            "- Never re-tell, restate or re-plan anything under ALREADY "
            "HAPPENED \u2014 not even as a one-line recap. The chapter begins "
            "with the first thing that has NOT happened yet.\n"
            "- Output the scenes only: no preamble, no commentary, no closing "
            "summary.")
    else:
        how = ((f"Answer in between {lo} and {hi} paragraphs of plain plot "
                f"summary. Give each event its own paragraph; do not compress "
                f"several into one, and do not pad to reach the count - if the "
                f"chapter is covered inside the range, stop. What HAPPENS "
                if lo else
                f"Answer in about {n} sentences of plain plot summary: what HAPPENS ")
               + "in this chapter, in order. Not prose, not dialogue, no scene-setting "
               "flourish \u2014 the events, so a writer could work from them.\n"
               "- Build only on what is given. Invent no new named characters.\n"
               "- If the author already wrote part of the plot, CONTINUE it rather "
               "than restating it.\n"
               "- Never re-tell, restate or re-plan anything under ALREADY HAPPENED "
               "— not even as a one-line recap. The plot begins with the first "
               "thing that has NOT happened yet. If the author's request already "
               "happened, plan what follows it.\n"
               "- Output the summary only: no headings, no preamble, no commentary.")

    blocks = ["\n\n".join(head)]
    pc = project_context_block(req.get("project_context"))
    if pc:
        blocks.append(pc)
    lg = ledgers_block(req.get("ledgers"))
    if lg:
        blocks.append(lg)
    ib = instructions_block(req.get("instructions"))
    if ib:
        blocks.append(ib)
    sb = settled_block(req.get("part_summaries"))
    if sb:
        blocks.append(sb)
    blocks.append(instr)
    sb2 = _style_block(req.get("style"))
    if sb2:
        blocks.append(sb2)
    blocks.append(how)
    if not sb:
        canon = canon_from(req).strip()
        blocks.append("THE STORY SO FAR:\n\n" + tail_words(canon, canon_window(req))
                      if canon else "(Nothing has been written yet.)")
    return DIVIDER.join(_lead_doctrine(_append_digest(blocks, req), req))


STAGE_ASKS = [
    {"stage": "development", "label": "how the plot develops", "budget": 500,
     "ask": ("Explain how this plot DEVELOPS across the chapter: the causality "
             "- what each event forces next - where tension rises, where it "
             "releases, and what the reader believes at each point that is not "
             "yet true. About 8 sentences, plain statements, in order.")},
    {"stage": "scenes", "label": "plan the scenes", "budget": 550,
     "ask": ("Plan the SCENES as a numbered list. For each: its setting, who is "
             "present, what changes or turns in it, and the note it ends on. "
             "No prose, no dialogue yet - the shape of each scene.")},
    {"stage": "material", "label": "draft the strong material", "budget": 900,
     "ask": ("Draft the strong MATERIAL for these scenes: key dialogue "
             "exchanges with speaker labels, striking images, and the objects "
             "that carry meaning. This is raw creative stock - the writer may "
             "keep your lines verbatim, so make each line worth keeping. Group "
             "it by scene number.")},
    {"stage": "blueprint", "label": "finalize the structure and layout",
     "budget": 1000,
     "ask": ("FINALIZE the blueprint the writer will follow top to bottom. "
             "Output ONLY blocks of this exact shape, one per scene, in final "
             "order, and nothing else:\n\n"
             "SCENE 1: <the title of the scene>\n"
             "PHASE: intro | business | main | after\n"
             "TENSION: <1-5>\n"
             "BEATS:\n"
             "- <one beat per line, a paragraph-sized moment, in order>\n"
             "MATERIAL:\n"
             "- \"<a drafted line you are keeping, verbatim, with its speaker "
             "if it is dialogue>\"\n\n"
             "Every scene from the scene list appears exactly once. Under "
             "MATERIAL quote only lines drafted in the earlier stage. This is "
             "the last plan before prose: complete, ordered, nothing left to "
             "decide.")},
]


def ask_of_stage(req: dict) -> dict:
    stage = str(req.get("stage") or "").strip()
    row = next((s for s in STAGE_ASKS
                if s["stage"] == stage or s["label"] == stage), None)
    name = "stage." + ((row["stage"] if row else stage) or "unnamed")
    given = (req.get("ask") or "").strip()
    if given:
        return _ask_record(name, given, "request")
    return _ask_pick(req, name, row["ask"] if row else "")


_SCENE_HEAD = re.compile(
    r"^\s*(?:#{1,6}\s*)?\**\s*scene\s*\d+\s*\**\s*[:\-–—]\s*(.+?)\s*\**\s*$",
    re.I)
_FIELD = re.compile(r"^\s*\**\s*(PHASE|TENSION|BEATS|MATERIAL)\s*\**\s*:?\s*\**"
                    r"\s*(.*?)\s*$", re.I)
_ITEM = re.compile(r"^\s*(?:[-*•]\s+|\d+[.)]\s+)(.*\S)\s*$")
PHASE_IDS = ("intro", "business", "main", "after")


def _unquote(s: str) -> str:
    s = s.strip().strip("*").strip()
    for a, b in (('"', '"'), ("'", "'"), ("“", "”")):
        if len(s) >= 2 and s.startswith(a) and s.endswith(b):
            return s[1:-1].strip()
    return s


def parse_blueprint(text) -> list:
    lines = _norm(text or "").split("\n")
    heads = [i for i, ln in enumerate(lines) if _SCENE_HEAD.match(ln)]
    out = []
    for n, start in enumerate(heads):
        end = heads[n + 1] if n + 1 < len(heads) else len(lines)
        block = lines[start:end]
        scene = {"scene": _unquote(_SCENE_HEAD.match(block[0]).group(1)),
                 "phase": None, "tension": None, "beats": [], "material": [],
                 "raw": "\n".join(block).strip()}
        field = None
        for ln in block[1:]:
            m = _FIELD.match(ln)
            if m:
                key, val = m.group(1).upper(), _unquote(m.group(2))
                if key == "PHASE":
                    scene["phase"] = val.lower() if val.lower() in PHASE_IDS else None
                    field = None
                elif key == "TENSION":
                    scene["tension"] = int(val) if val.isdigit() and 1 <= int(val) <= 5 else None
                    field = None
                else:
                    field = key.lower()
                continue
            item = _ITEM.match(ln)
            if item and field:
                scene[field].append(_unquote(item.group(1)))
        out.append(scene)
    return out


def compile_stage_plan(req: dict) -> str:
    stage = str(req.get("stage") or "").strip()
    ask = ask_of_stage(req)["text"]
    head = ["STAGED STORY BUILD" + (f" — stage: {stage}" if stage else "")
            + "\nYou are one stage of a pipeline. Later stages obey what you "
            "write here, and a prose writer will follow it line by line."]
    blocks = ["\n".join(head)]
    pc = project_context_block(req.get("project_context"))
    if pc:
        blocks.append(pc)
    ib = instructions_block(req.get("instructions"))
    if ib:
        blocks.append(ib)
    sb = settled_block(req.get("part_summaries"))
    if sb:
        blocks.append(sb)
        blocks.append("Never re-tell, restate or re-plan anything under ALREADY "
                      "HAPPENED — not even as a recap. This stage begins "
                      "after the story's current end.")
    for p in (req.get("prior") or []):
        if not isinstance(p, dict) or not str(p.get("text") or "").strip():
            continue
        blocks.append("SETTLED BY AN EARLIER STAGE — "
                      + str(p.get("label") or "").strip()
                      + " — build on it, contradict nothing in it:\n"
                      + str(p["text"]).strip())
    blocks.append("THIS STAGE MUST PRODUCE:\n" + ask)
    blocks.append("Output only what this stage asks for — no preamble, no "
                  "commentary, no full prose scenes unless the ask itself "
                  "asks for written lines.")
    return DIVIDER.join(_lead_doctrine(_append_digest(blocks, req), req))


def compile_part_plan(req: dict) -> str:
    lo, hi = plan_range(req.get("target_paragraphs"),
                        req.get("target_paragraphs_max"))
    blocks = ["RE-PLAN ONE PART\nYou are replanning a single part of a chapter "
              "that is already planned. Everything around it stays exactly as "
              "it is; only this part changes."]
    pc = project_context_block(req.get("project_context"))
    if pc:
        blocks.append(pc)
    ib = instructions_block(req.get("instructions"))
    if ib:
        blocks.append(ib)
    plot = str(req.get("chapter_plot") or "").strip()
    if plot:
        blocks.append("THE CHAPTER\u2019S PLOT \u2014 this part serves it and "
                      "contradicts nothing in it:\n" + plot)
    sb = settled_block(req.get("part_summaries"))
    if sb:
        blocks.append(sb)
        blocks.append("Never re-tell, restate or re-plan anything under ALREADY "
                      "HAPPENED \u2014 not even as a recap.")
    for label, key in (("THE PART BEFORE THIS ONE", "before"),
                       ("THE PART AFTER THIS ONE", "after")):
        v = str(req.get(key) or "").strip()
        if v:
            blocks.append(label + " \u2014 it keeps its plan; yours must lead "
                          "into and out of these:\n" + v)
    cur = str(req.get("current") or "").strip()
    if cur:
        blocks.append("THIS PART\u2019S PLAN AS IT STANDS \u2014 what you are "
                      "replacing:\n" + cur)
    ask = str(req.get("instruction") or "").strip()
    blocks.append("WHAT TO CHANGE:\n"
                  + (ask or "Plan this part better: sharper beats, a clearer "
                            "turn, and material worth keeping."))
    blocks.append("Output ONLY ONE block of this exact shape, and nothing "
                  "else:\n\n"
                  "SCENE 1: <the title of this part>\n"
                  "PHASE: intro | business | main | after\n"
                  "TENSION: <1-5>\n"
                  "BEATS:\n"
                  "- <one beat per line, a paragraph-sized moment, in order>\n"
                  "MATERIAL:\n"
                  "- \"<a drafted line worth keeping verbatim, with its "
                  "speaker if it is dialogue>\"\n\n"
                  + (f"Between {lo} and {hi} beats. Give each event its own "
                     f"beat; do not compress several into one, and do not pad "
                     f"to reach the count.\n\n" if lo else "")
                  + "ONE scene block. Do not plan the parts around it, do not "
                  "write the prose, and do not explain your changes.")
    return DIVIDER.join(_lead_doctrine(_append_digest(blocks, req), req))


def compile_consolidate(req: dict) -> str:
    label = (req.get("label") or "the material").strip()
    text = (req.get("text") or "").strip()
    instr = (req.get("instruction") or "").strip()
    blocks = [
        (f"CONSOLIDATION TASK \u2014 not prose, not a summary. Below is {label}, "
         "extracted chapter by chapter and simply concatenated, so the same thing "
         "appears many times in different words. Merge it into ONE clean list "
         "covering the whole book."),
        ("RULES:\n"
         "- Keep EVERY distinct item. Losing one is the only real failure here.\n"
         "- Fold duplicates and near-duplicates into a single entry, keeping the "
         "clearest wording.\n"
         "- Do not invent, generalise, or summarise detail away.\n"
         "- Drop the chapter headings \u2014 the result is one list for the book.\n"
         "- Output the list only: no preamble, no commentary, no closing remark."),
    ]
    if instr:
        blocks.append("ADDITIONAL INSTRUCTION FROM THE AUTHOR:\n" + instr)
    blocks.append("THE MATERIAL:\n\n" + (text or "(nothing to consolidate)"))
    return DIVIDER.join(blocks)


import os
import threading
import re


def _norm(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def parse_suggest(text: str, n: int = 3) -> list:
    text = _norm(text)
    if not text.strip():
        return []
    blocks = re.split(r"^\s*===+\s*$", text, flags=re.M)
    if len(blocks) < 2:
        blocks = re.split(r"\n(?=TITLE\s*:)", text)
    out = []
    for b in blocks:
        d = {"title": "", "facets": {}}
        for m in re.finditer(r"^([A-Z]+)\s*:\s*(.+)$", b, flags=re.M):
            if m.group(1) == "TITLE":
                d["title"] = m.group(2).strip()
            else:
                d["facets"][m.group(1)] = m.group(2).strip()
        if d["title"] and len(d["facets"]) >= 3:
            out.append(d)
    return out[:max(1, int(n))]


def parse_ideas(text: str, n: int = 5) -> list:
    text = _norm(text)
    if not text.strip():
        return []
    parts = re.split(r"^\s*\d+[.)]\s*", text, flags=re.M)
    if not re.match(r"^\s*\d+[.)]", text):
        parts = parts[1:]
    out = [p.strip() for p in parts if p.strip()]
    return out[:max(1, int(n))]




ASK_OF_REQUEST = {"study": ask_of_study, "study_merge": ask_of_study,
                  "stage_plan": ask_of_stage}


def ask_for_request(kind: str, req: dict) -> dict:
    fn = ASK_OF_REQUEST.get(str(kind or ""))
    return fn(req or {}) if fn else {}


def ask_seeds() -> dict:
    out = {f"study.{a}": c["task"]
           for a, c in STUDY_ASPECTS.items() if c.get("task")}
    out.update({f"stage.{s['stage']}": s["ask"] for s in STAGE_ASKS})
    return out


def ask_in_force(asks: dict, name: str) -> dict:
    return _ask_pick({"asks": asks}, name, ask_seeds().get(name) or "")


COMPILER_READS = {
    "write_part": {"chapter", "part", "canon", "canon_parts", "style", "instructions",
                   "feedback", "part_summaries", "project_context",
                   "target_words", "steer", "steers", "feedback_window",
                   "feedback_windows", "feedback_cap", "target_part", "ledgers", "canon_words",
                   "target_paragraphs",
                   "target_paragraphs_max",
                   "transition_paras",
                   "craft",
                   "plan_mode",
                   "part_in_chapter"},
    "replace_para": {"paragraphs", "target_index", "instruction", "steer", "canon_before",
                     "instructions", "feedback", "project_context", "steers", "n",
                     "feedback_window", "feedback_windows", "feedback_cap", "target_part", "ledgers",
                     "canon_words"},
    "suggest": {"chapter", "canon", "canon_parts", "instructions", "feedback",
                "part_summaries", "project_context", "steers", "n",
                "feedback_window", "feedback_windows", "feedback_cap", "target_part", "ledgers",
                "canon_words"},
    "ideas": {"canon", "canon_parts", "focus", "instructions", "feedback", "part_summaries",
              "project_context", "n", "feedback_window", "feedback_windows", "feedback_cap", "target_part",
              "canon_words"},
    "chapter_plot": {"chapter", "canon", "canon_parts", "style", "instructions",
                      "target_parts", "feedback",
                     "part_summaries", "project_context", "target_sentences",
                     "feedback_window", "feedback_windows", "feedback_cap", "target_part", "ledgers",
                     "target_paragraphs", "target_paragraphs_max",
                     "canon_words"},
    "judge": {"criterion", "passage"},
    "study": {"subject", "aspect", "label", "brief", "chunk_no", "chunk_of",
              "ask", "asks", "entries", "subject_kind"},
    "study_merge": {"findings", "aspect", "label", "chunk_of", "ask", "asks"},
    "suggest_extracts": {"subjects", "count", "dropped",
                         "proposed", "batch_no", "batch_of"},
    "stage_plan": {"stage", "ask", "asks", "prior", "instructions",
                   "project_context", "part_summaries"},
    "part_plan": {"chapter_plot", "current", "before", "after", "instruction",
                  "target_paragraphs", "target_paragraphs_max",
                  "instructions", "project_context", "part_summaries"},
    "consolidate": {"text", "label", "instruction"},
    "compose": {"sources", "want", "instruction"},
    "bake": {"sources", "want", "instruction"},
    "summarize_part": {"text", "title"},
}


def reach_of(prompt: str, text: str) -> dict:
    body = norm_text(text)
    if not body:
        return {"reach": "absent", "chars": 0}
    hay = norm_text(prompt)
    if body in hay:
        return {"reach": "full", "chars": len(body)}
    def longest(pick):
        lo, hi, best = 0, len(body), 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if mid and pick(mid) in hay:
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        return best
    pre = longest(lambda n: body[:n])
    suf = longest(lambda n: body[-n:])
    if max(pre, suf) < 24:
        return {"reach": "none", "chars": 0}
    return {"reach": "partial", "chars": max(pre, suf),
            "side": "head" if pre >= suf else "tail",
            "of": len(body)}



def norm_text(s) -> str:
    return (s or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def text_sha(s) -> str:
    return hashlib.sha256(norm_text(s).encode("utf-8")).hexdigest()


_SLOT_OK = re.compile(r"[^a-z0-9]+")


def slot_for(kind: str, *ids) -> str:
    parts = [kind, *[str(x) for x in ids if x is not None and str(x) != ""]]
    out = []
    for p in parts:
        clean = _SLOT_OK.sub("-", str(p).lower()).strip("-") or "x"
        out.append(clean[:40])
    return "-".join(out)[:120]


class VersionedStore:

    def __init__(self, root: Path, ext: str = "md"):
        self.root = Path(root)
        self.ext = ext

    def path(self, slot: str, v: int) -> Path:
        return self.root / f"{slot}.v{v}.{self.ext}"

    def versions(self, slot: str) -> list:
        out = []
        for f in self.root.glob(f"{slot}.v*.{self.ext}"):
            try:
                out.append(int(f.name[len(slot) + 2:-(len(self.ext) + 1)]))
            except ValueError:
                continue
        return sorted(out)

    def read(self, slot: str, v: int):
        f = self.path(slot, v)
        return f.read_text(encoding="utf-8") if f.exists() else None


class TextStore(VersionedStore):

    def __init__(self, root: Path):
        super().__init__(root, ext="md")
        self.index = self.root / "index.json"

    def _load(self) -> dict:
        if not self.index.exists():
            return {"slots": {}}
        try:
            d = json.loads(self.index.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) and "slots" in d else {"slots": {}}
        except (OSError, ValueError):
            return {"slots": {}}

    def put(self, slot: str, text: str, corr: str = None) -> dict:
        body = norm_text(text)
        sha = text_sha(body)
        doc = self._load()
        e = doc["slots"].setdefault(slot, {"versions": {}, "current": None})

        for v, meta in e["versions"].items():
            if meta.get("sha256") == sha:
                e["current"] = int(v)
                self._save(doc)
                return {"slot": slot, "version": int(v), "sha256": sha,
                        "ref": f"text:{slot}@v{v}", "wrote": False}

        vs = self.versions(slot)
        known = [int(v) for v in e["versions"]]
        v = (max(vs + known) + 1) if (vs or known) else 1
        self.root.mkdir(parents=True, exist_ok=True)
        self.path(slot, v).write_text(body, encoding="utf-8", newline="\n")
        e["versions"][str(v)] = {"sha256": sha, "bytes": len(body.encode("utf-8")),
                                 "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                 "first_corr": corr}
        e["current"] = v
        self._save(doc)
        return {"slot": slot, "version": v, "sha256": sha,
                "ref": f"text:{slot}@v{v}", "wrote": True}

    def _save(self, doc: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.index.with_name(
            f"{self.index.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False),
                       encoding="utf-8", newline="\n")
        for attempt in range(8):
            try:
                tmp.replace(self.index)
                return
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def resolve(self, ref: str):
        m = re.fullmatch(r"text:([^@]+)@v(\d+)", str(ref or ""))
        if not m:
            return None
        return self.read(m.group(1), int(m.group(2)))

    def rebuild_index(self) -> dict:
        doc = {"slots": {}}
        for f in sorted(self.root.glob(f"*.v*.{self.ext}")):
            name = f.name[:-(len(self.ext) + 1)]
            slot, _, vtxt = name.rpartition(".v")
            if not slot or not vtxt.isdigit():
                continue
            body = f.read_text(encoding="utf-8")
            e = doc["slots"].setdefault(slot, {"versions": {}, "current": None})
            e["versions"][vtxt] = {
                "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "bytes": len(body.encode("utf-8")),
                "created": time.strftime("%Y-%m-%dT%H:%M:%S",
                                         time.localtime(f.stat().st_mtime)),
                "first_corr": None}
            e["current"] = max(int(v) for v in e["versions"])
        self._save(doc)
        return doc


class FileStore:
    def __init__(self, root):
        self.root = Path(root)

    def _dir(self, chapter) -> Path:
        return self.root / f"chapter-{int(chapter):02d}"

    def _file(self, chapter, part, version) -> Path:
        return self._dir(chapter) / f"part-{int(part):02d}.v{int(version)}.md"

    def _versions(self, chapter, part) -> list:
        d = self._dir(chapter)
        if not d.exists():
            return []
        pre = f"part-{int(part):02d}.v"
        out = []
        for p in d.glob(pre + "*.md"):
            try:
                out.append(int(p.stem.split(".v")[-1]))
            except ValueError:
                continue
        return sorted(out)

    def _current_path(self, chapter, part):
        vs = self._versions(chapter, part)
        return self._file(chapter, part, vs[-1]) if vs else None

    def _write(self, chapter, part, text, provenance, origin) -> dict:
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        vs = self._versions(chapter, part)
        version = (vs[-1] + 1) if vs else 1
        self._dir(chapter).mkdir(parents=True, exist_ok=True)
        path = self._file(chapter, part, version)
        path.write_text(text, encoding="utf-8", newline="\n")
        meta = dict(provenance or {})
        meta.update({
            "chapter": int(chapter), "part": int(part), "version": version,
            "origin": origin,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "words": len(text.split()),
        })
        for k in ("corr", "sample", "variant_label", "prompt_sha256",
                  "payload_sha256", "payload_match", "request", "pools_used",
                  "manifest_sha256"):
            meta.setdefault(k, (provenance or {}).get(k))
        meta.setdefault("model", None)
        meta.setdefault("temperature", None)
        meta.setdefault("seed", None)
        meta.setdefault("prompt_sha256", None)
        meta.setdefault("canon_refs", [])
        path.with_suffix(".json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        return {"path": str(path), "version": version, "chapter": int(chapter),
                "part": int(part)}

    def accept(self, chapter, part, text, provenance=None) -> dict:
        return self._write(chapter, part, text, provenance, origin="accept")

    def replace_para(self, chapter, part, para_index, text, provenance=None) -> dict:
        cur = self._current_path(chapter, part)
        if cur is None:
            raise FileNotFoundError(f"chapter {chapter} part {part} has no version yet")
        paras = cur.read_text(encoding="utf-8").split("\n\n")
        if not 0 <= int(para_index) < len(paras):
            raise ValueError(f"para_index {para_index} out of range (0..{len(paras)-1})")
        paras[int(para_index)] = (text or "").strip()
        prov = dict(provenance or {})
        prov["replaced_paragraph"] = int(para_index)
        prov["from_version"] = int(cur.stem.split(".v")[-1])
        return self._write(chapter, part, "\n\n".join(paras), prov, origin="replace_para")

    def read(self, path) -> dict:
        p = Path(path)
        if not p.is_absolute():
            p = self.root / path
        if not p.exists():
            raise FileNotFoundError(str(p))
        text = p.read_text(encoding="utf-8")
        hand = False
        side = p.with_suffix(".json")
        if side.exists():
            try:
                meta = json.loads(side.read_text(encoding="utf-8"))
                recorded = meta.get("text_sha256")
                if recorded:
                    now = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
                    hand = (now != recorded)
            except ValueError:
                hand = False
        return {"text": text, "hand_edited": hand, "path": str(p)}

    def list(self) -> list:
        out = []
        if not self.root.exists():
            return out
        for d in sorted(self.root.glob("chapter-*")):
            try:
                ch = int(d.name.split("-")[-1])
            except ValueError:
                continue
            parts = set()
            for f in d.glob("part-*.md"):
                try:
                    parts.add(int(f.name.split(".")[0].split("-")[-1]))
                except ValueError:
                    continue
            for pt in sorted(parts):
                vs = self._versions(ch, pt)
                out.append({"chapter": ch, "part": pt, "versions": vs,
                            "current": vs[-1] if vs else None,
                            "current_path": str(self._file(ch, pt, vs[-1])) if vs else None})
        return out

    def manuscript(self) -> str:
        chunks = []
        for entry in self.list():
            p = Path(entry["current_path"]) if entry["current_path"] else None
            if p and p.exists():
                chunks.append(p.read_text(encoding="utf-8").strip())
        return "\n\n".join(chunks)

    def parts(self) -> list:
        out = []
        for entry in self.list():
            item = dict(entry)
            item["text"], item["hand_edited"] = "", False
            if entry["current_path"]:
                try:
                    r = self.read(entry["current_path"])
                    item["text"] = r["text"]
                    item["hand_edited"] = r["hand_edited"]
                except FileNotFoundError:
                    pass
            out.append(item)
        return out


import urllib.error
import urllib.request


def _studio(base, path, data=None, timeout=1800):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(base + path, data=body,
                               method="POST" if body else "GET")
    if body is not None:
        r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        ct = resp.headers.get("Content-Type", "")
        return json.loads(raw) if "json" in ct else raw


def _run_one(base, prompt, model, temperature, seed, max_tokens, samples, poll_s=1.0,
             timeout=900):
    payload = {"summary": "", "prompt": prompt, "model_keys": [model],
               "samples": int(samples), "max_tokens": int(max_tokens), "blind": False}
    if seed is not None:
        payload["seed"] = seed
    if temperature is not None:
        payload["temperature"] = temperature
    job = _studio(base, "/api/jobs/write", payload)
    jid = job["job_id"]
    t0 = time.time()
    while True:
        j = _studio(base, f"/api/jobs/{jid}")
        if j["status"] in ("done", "failed", "cancelled"):
            break
        if time.time() - t0 > timeout:
            raise TimeoutError(f"job {jid} still {j['status']} after {timeout}s")
        time.sleep(poll_s)
    out = []
    for v in sorted(j.get("variants", []), key=lambda v: v["label"]):
        text = ""
        if v["status"] == "done":
            try:
                text = _studio(base, f"/api/jobs/{jid}/variants/{v['label']}/output")
            except urllib.error.HTTPError:
                text = ""
        vm = v.get("meta") or {}
        out.append({"text": text, "label": v["label"], "status": v["status"],
                    "error": v.get("error"), "seed": vm.get("seed"),
                    "finish_reason": vm.get("finish_reason"),
                    "tok_s": vm.get("tokens_per_second"),
                    "token_count": vm.get("token_count"),
                    "temperature": vm.get("temperature"), "job_id": jid})
    return out, jid


def generate(req: dict, studio_base: str) -> dict:
    prompt = req.get("prompt") or ""
    if not prompt.strip():
        raise ValueError("prompt is empty")
    model = req.get("model") or "cydonia-24b"
    temperature = req.get("temperature")
    seed = req.get("seed")
    max_tokens = int(req.get("max_tokens") or 400)
    n = max(1, int(req.get("n") or 1))
    steers = [s for s in (req.get("steers") or [])]

    candidates, job_ids = [], []
    if steers:
        for i in range(n):
            steer = steers[i] if i < len(steers) else ""
            p = prompt + (DIVIDER + "FOR THIS VERSION: " + steer if steer else "")
            outs, jid = _run_one(studio_base, p, model, temperature,
                                 (seed + i) if isinstance(seed, int) else seed,
                                 max_tokens, 1)
            job_ids.append(jid)
            candidates.append(outs[0] if outs else {"text": "", "status": "missing"})
    else:
        remaining = n
        batch_index = 0
        while remaining > 0:
            take = min(remaining, 3)
            batch_seed = (seed + batch_index * 3) if isinstance(seed, int) else seed
            outs, jid = _run_one(studio_base, prompt, model, temperature,
                                 batch_seed, max_tokens, take)
            job_ids.append(jid)
            candidates.extend(outs[:take])
            remaining -= take
            batch_index += 1
    return {"candidates": candidates, "job_ids": job_ids}
