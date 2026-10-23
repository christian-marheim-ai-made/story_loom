import pathlib
import re


SUBJ = r"(?:he|she|i|[A-Z][a-z]+)"
INTERIOR = re.compile(
    rf"\b{SUBJ}\s+(?:just\s+)?(?:did\s?n[o']t\s+|was\s?n[o']t\s+|could\s?n[o']t\s+)?"
    r"(thought|felt|wondered|knew|realised|realized|remembered|hoped|wanted"
    r"|considered|forgot|respected|minded|mind|sure|liked|noticed|decided)"
    r"|\bseemed\b|\bmaybe\b|\bit occurred to\b|\bfelt like\b", re.I)

SENSES = {
    "sight": r"\b(saw|see(?:ing|s)?|looked?|looking|watch(?:ed|ing)?|glanc\w+"
             r"|star(?:e|ed|ing)|view)\b",
    "sound": r"\b(heard|hearing|sound|noise|silence|whisper|voice)\b",
    "touch": r"\b(touch\w*|warmt?h?|cold|rough|smooth|press\w+|skin|grip\w*"
             r"|squeez\w+|clawing)\b",
    "smell": r"\b(smell\w*|scent|perfume|odou?r)\b",
    "taste": r"\b(taste\w*|bitter|sweet(?:ish)?|salt\w*)\b",
    "body":  r"\b(breath\w*|heartbeat|pulse|nausea|ache|tired|dizzy|sweat\w*"
             r"|conditioning|sick)\b",
}

SPEECH = r"(?:said|says|answers?|answered|asked|replied|responds?|responded|told)"
DIALOGUE_QUOTED = re.compile(
    rf'(?:{SPEECH}[^.?!]{{0,30}}["“])|(?:["”][^"“]{{0,30}}{SPEECH})',
    re.I)
DIALOGUE_BARE = re.compile(
    rf"\b{SPEECH}\s+to\s+\w+,\s+(?!he\b|she\b|that\b|if\b)[a-z]", re.I)

CHANGE = re.compile(
    r"\b(went|goes|came in|arrived|left|entered|exited|ran|walked out|drove"
    r"|shot|cleared|sent|gave|handed|took|grabbed|bought|cooked|finished"
    r"|started|stopped|opened|closed|paid|swiped|jumped)\b", re.I)
POSTURE = re.compile(
    r"\b(stood|sat|moved|turned|stretched|leaned|rose|bit|placed|rested"
    r"|put|brought|reached|stepped|crossed)\b", re.I)

BANDS = dict(interior=(0.8, 1.5), senses=(2, 4), dialogue=(1, 3), change=(0.5, 1.0))

MEASURE_KEYS = ("interior", "senses", "dialogue", "change", "blocking")


def measure(text: str) -> dict:
    words = max(1, len(text.split()))
    sents = max(1, len([s for s in re.split(r"(?<=[.!?])\s+", text) if len(s) > 20]))
    senses = [k for k, p in SENSES.items() if re.search(p, text, re.I)]
    return dict(
        interior=round(100 * len(INTERIOR.findall(text)) / words, 1),
        senses=len(senses), which="+".join(senses) or "-",
        dialogue=len(DIALOGUE_QUOTED.findall(text)) + len(DIALOGUE_BARE.findall(text)),
        change=round(len(CHANGE.findall(text)) / sents, 2),
        blocking=round(len(POSTURE.findall(text)) / sents, 2),
        words=len(text.split()))


def band(value, key: str) -> str:
    if key not in BANDS:
        raise KeyError(f"{key} has no calibrated band")
    lo, hi = BANDS[key]
    return "L" if value < lo else ("H" if value >= hi else "M")



LEVER_GROUPS = ("sentence craft", "narration", "plot")

UNMEASURED = None

LEVERS = {
    "distance": dict(key="interior", seam="style", group="narration",
                     label="psychic distance"),
    "senses":   dict(key="senses",   seam="style", group="sentence craft",
                     label="sensory register"),
    "blocking": dict(key="blocking", seam="style", group="narration",
                     label="blocking"),
    "change":   dict(key="change",   seam="plot",  group="plot",
                     label="plot advancement"),
    "dialogue": dict(key="dialogue", seam="plot",  group="sentence craft",
                     label="dialogue"),

    "reaction": dict(key="reactions", seam="style", tier=2,
                     group="sentence craft", label="micro-reaction"),
    "visual":   dict(key="visual_detail", seam="style", tier=2,
                     group="sentence craft", label="visual rendering"),
    "worn":     dict(key="worn_frame", seam="style", tier=2,
                     group="sentence craft", label="what people are wearing"),
    "movement": dict(key="movement", seam="style", tier=2,
                     group="narration", label="body movement"),
    "condition": dict(key="body_followed", seam="style", tier=2,
                      group="narration",
                      label="a body's condition over time"),
    "dressing": dict(key="dressing", seam="style", tier=2,
                     group="sentence craft",
                     label="clothing and props handled"),
    "touch":    dict(key="touch", seam="style", tier=2,
                     group="sentence craft",
                     label="physical contact"),
    "flirting": dict(key="flirting", seam="style", tier=2,
                     group="sentence craft",
                     label="subtext between characters (PROXY - uncalibrated)"),

    "tension":  dict(key=UNMEASURED, seam="plot", tier=2, group="plot",
                     unmeasured=True,
                     label="tension (NO MEASURE - not calibrated, not counted)"),
    "boundary": dict(key=UNMEASURED, seam="plot", tier=2, group="plot",
                     unmeasured=True,
                     label="boundary crossed "
                           "(NO MEASURE - not calibrated, not counted)"),
}

UNMEASURED_LEVERS = tuple(k for k, v in LEVERS.items() if v.get("unmeasured"))

MEASURED_LEVERS = tuple(k for k in LEVERS if k not in UNMEASURED_LEVERS)

STYLE_LEVERS = tuple(k for k, v in LEVERS.items() if v["seam"] == "style")
PLOT_LEVERS = tuple(k for k, v in LEVERS.items() if v["seam"] == "plot")

LEVERS_BY_GROUP = {g: tuple(k for k, v in LEVERS.items() if v["group"] == g)
                   for g in LEVER_GROUPS}

LEVER_INSTRUCTION = {
    "distance": "Stay close inside the viewpoint character. Name what they "
                "think, notice and want, not only what they do.",
    "senses":   "Engage more than sight. Let sound, smell, texture or "
                "temperature carry part of what the reader perceives.",
    "blocking": "Keep bodies present in the space. Let characters shift, turn, "
                "sit, reach - physical positioning a reader can picture.",
    "change":   "Let the situation move. Each paragraph should leave something "
                "different from how it found it.",
    "dialogue": "Let the characters speak. Render their lines rather than "
                "summarising what they said.",
    "reaction": "Let somebody's body answer what just happened - eyes, mouth, "
                "hands, breath - so the beat lands on a person rather than "
                "passing.",
    "visual":   "Show what things look like: colour, light, material, what a "
                "body is wearing, how a movement appears from outside.",
    "worn":     "Say what people have on, and let it change: a coat taken off, "
                "a sleeve pushed back, gear stripped from someone who is hurt.",
    "movement": "Put bodies where the reader can follow them: say where a hand, "
                "an arm or a shoulder goes in relation to the other person.",
    "condition": "When a body is hurt, tired or changed, come back to it later "
                 "and say what it is like now - worse, stiffer, darker - so the "
                 "reader can feel time passing through it.",
    "dressing": "When clothing moves - on or off - make it an action a reader "
                "can watch: name the garment and whose hand moves it.",
    "touch":    "In a paragraph where two people are close, give the reader one "
                "point of contact: whose hand, arm or shoulder, and what of the "
                "other person it meets.",
    "flirting": "Let one of them try something and the other answer it: a look "
                "held a beat too long, a line with two meanings, a smile aimed "
                "at somebody. One exchange per paragraph, not a mood.",
    "tension":  "Keep something pressing on the scene: a cost that is still "
                "running, a thing that could go wrong, someone waiting for an "
                "answer. Name what the pressure is, not that it is tense.",
    "boundary": "Let a character say where their limit is - out loud or to "
                "themselves - and then show them step over it, or refuse to. "
                "The limit named and the choice made, in the same scene.",
}


class UnmeasuredLever(KeyError):
    pass


def lever_value(profile_or_measure: dict, lever: str):
    if LEVERS[lever].get("unmeasured"):
        raise UnmeasuredLever(
            f"{lever} ships without a measure - there is no regex, no key and "
            "no value to report. Do not substitute 0; it would read as "
            "'measured, and absent'.")
    key = LEVERS[lever]["key"]
    if key in profile_or_measure:
        return profile_or_measure[key]
    text = profile_or_measure.get("text")
    if text is None:
        raise KeyError(f"{lever} needs `text` to measure {key}")
    import loom_craft_measures as _C
    return _C.measure2(text)[key]



PROVENANCE = ("owner", "machine", "imported")


def profile(text: str, provenance: str, name: str = "") -> dict:
    if provenance not in PROVENANCE:
        raise ValueError(
            f"provenance must be one of {PROVENANCE}, got {provenance!r}. "
            "It is not inferable - find out where the text came from.")
    out = measure(text)
    out["provenance"] = provenance
    out["name"] = name
    out["text"] = text
    return out



CANON_DIR = pathlib.Path(__file__).resolve().parent / "reference" / "passages"

EXPECT = {
    "passage-01": dict(interior="L", senses="L", dialogue="L", change="H"),
    "passage-02": dict(interior="H", senses="M", dialogue="L", change="L"),
    "passage-03": dict(interior="H", senses="L", dialogue="L", change="L"),
    "passage-04": dict(interior="L", senses="L", dialogue="L", change="H"),
    "passage-05": dict(interior="L", senses="M", dialogue="L", change="H"),
    "passage-06": dict(interior="H", senses="L", dialogue="L", change="M"),
    "passage-07": dict(interior="L", senses="H", dialogue="H", change="L"),
}


def canon_body(path) -> str:
    t = pathlib.Path(path).read_text(encoding="utf-8")
    s = t.split("\n---\n", 1)
    return (s[1] if len(s) > 1 else t).strip()


def canon_passages(directory=None) -> list:
    d = pathlib.Path(directory or CANON_DIR)
    return [profile(canon_body(f), "owner", f.stem)
            for f in sorted(d.glob("*.md")) if f.stem in EXPECT]



EXEMPLAR_MIN_WORDS = 30

EXEMPLAR_MAX_OVERLAP = 0.45

_STOP = frozenset(
    "the a an and or but if of to in on at by for with from as was were is are "
    "be been it its he she they them his her their that this there then when "
    "had has have not no so up out into over down his him".split())


def _content_words(text: str) -> set:
    return {w.strip(".,;:!?\"'()[]").lower() for w in str(text).split()} - _STOP


def _too_similar(a: dict, b: dict) -> bool:
    wa, wb = _content_words(a.get("text", "")), _content_words(b.get("text", ""))
    if not wa or not wb:
        return False
    return len(wa & wb) / min(len(wa), len(wb)) >= EXEMPLAR_MAX_OVERLAP


def exemplars_for(lever: str, pool=None, n: int = 3, min_words=None) -> list:
    if lever not in LEVERS:
        raise KeyError(f"unknown lever {lever!r}; have {sorted(LEVERS)}")
    if LEVERS[lever].get("unmeasured"):
        return []
    floor = EXEMPLAR_MIN_WORDS if min_words is None else int(min_words)
    candidates = [p for p in (pool if pool is not None else canon_passages())
                  if p["words"] >= floor and lever_value(p, lever) > 0]
    if not candidates:
        return []
    ranked = sorted(candidates,
                    key=lambda p: (-float(p.get("stars") or 0),
                                   -lever_value(p, lever), p["words"], p["name"]))
    picked = []
    for p in ranked:
        if len(picked) >= max(0, int(n)):
            break
        if any(_too_similar(p, q) for q in picked):
            continue
        picked.append(p)
    return picked


def seed_report(pool=None, n: int = 3) -> dict:
    out = {}
    for lever in LEVERS:
        picks = exemplars_for(lever, pool, n)
        spec = LEVERS[lever]
        key = spec["key"]
        out[lever] = dict(
            n=len(picks),
            words=sum(p["words"] for p in picks),
            names=[p["name"] for p in picks],
            values=[lever_value(p, lever) for p in picks],
            bands=[band(lever_value(p, lever), key) for p in picks]
            if key in BANDS else [],
            tier=spec.get("tier", 1),
            group=spec["group"],
            unmeasured=bool(spec.get("unmeasured")),
            provenance=sorted({p["provenance"] for p in picks}),
        )
    return out
