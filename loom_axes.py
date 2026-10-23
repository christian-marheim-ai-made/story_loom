from __future__ import annotations

import re
import statistics

import loom_study as ST

TEMPORAL = (
    "by the time", "the next morning", "that evening", "that night",
    "that morning", "minutes later", "an hour later", "hours later",
    "moments later", "a moment later", "at dawn", "at dusk", "at noon",
    "at night", "at last", "then", "later", "after", "before", "until",
    "meanwhile", "soon", "already", "finally", "afterwards", "afterward",
)

NUMBER_WORDS = (
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen", "twenty", "thirty", "forty", "fifty",
    "sixty", "seventy", "eighty", "ninety", "hundred", "thousand", "dozen",
    "half", "twice", "first", "second", "third", "fourth", "fifth",
)

BODY = (
    "hand", "hands", "arm", "arms", "leg", "legs", "knee", "knees",
    "shoulder", "shoulders", "back", "chest", "face", "jaw", "finger",
    "fingers", "boot", "boots", "foot", "feet", "spine", "neck", "throat",
    "lungs", "breath", "sweat", "blood", "muscle", "muscles", "skin", "body",
    "eyes", "mouth", "teeth", "ribs", "hip", "hips", "wrist", "palm", "palms",
)
EXERTION = (
    "hauled", "dragged", "heaved", "staggered", "stumbled", "crawled",
    "climbed", "slumped", "sagged", "braced", "strained", "gripped",
    "clutched", "shoved", "pushed", "pulled", "lifted", "carried", "trudged",
    "waded", "lurched", "knelt", "crouched", "leaned", "dropped", "fell",
    "scrambled", "hoisted", "wrenched", "sprinted", "panting", "gasping",
    "trembling", "shaking", "aching", "limping",
)

_EM_DASH = re.compile(r"—|--|–")
_DIGITS = re.compile(r"\b\d+\b")
_SHORT_SENT = 6


def _per_1000(n: int, words: int) -> float:
    return round(1000 * n / words, 2) if words else 0.0


def _phrase_hits(lower_text: str, phrases) -> int:
    t = " " + re.sub(r"\s+", " ", lower_text) + " "
    n = 0
    for ph in sorted(phrases, key=len, reverse=True):
        pat = r"(?<![a-z])" + re.escape(ph) + r"(?![a-z])"
        found = re.findall(pat, t)
        n += len(found)
        if found:
            t = re.sub(pat, " ", t)
    return n


def flat_profile(text: str) -> dict:
    text = str(text or "")
    m = ST.count_metrics(text)
    pars = ST.paragraphs(text)
    all_words = ST.words(text)
    lower = [w.lower() for w in all_words]
    total = len(all_words)
    lower_text = text.lower()
    sents = [s for p in pars for s in ST.sentences(p)]
    sent_lens = [len(ST.words(s)) for s in sents]
    par_sents = [len(ST.sentences(p)) for p in pars]
    return {
        "sentence_median": m["c2_sentence_lengths"]["median"],
        "sentence_spread": m["c2_sentence_lengths"]["spread"],
        "paragraph_median": m["c1_paragraph_lengths"]["median"],
        "dialogue_ratio": m["c3_dialogue_ratio"]["ratio"],
        "beat_rate": m["c5_beat_rate"]["beat_rate"],
        "plain_attribution": (m["c6_attribution"] or {}).get("plain_share")
                             if (m["c6_attribution"] or {}).get("plain_share")
                             is not None else 1.0,
        "filter_per_1000": m["c7_filter_words"]["per_1000"],
        "senses_present": m["c8_senses"]["senses_present"],
        "abstract_per_1000": m["c9_abstractness"]["per_1000"],
        "opening_repeat_share": m["c10_opening_variety"]["repeat_share"],
        "time_compression_per_1000": m["c11_time_compression"]["per_1000"],
        "em_dash_per_1000": _per_1000(len(_EM_DASH.findall(text)), total),
        "temporal_per_1000": _per_1000(_phrase_hits(lower_text, TEMPORAL), total),
        "numeral_per_1000": _per_1000(
            sum(1 for w in lower if w in NUMBER_WORDS)
            + len(_DIGITS.findall(text)), total),
        "physical_per_1000": _per_1000(
            sum(1 for w in lower if w in BODY or w in EXERTION), total),
        "short_sentence_share": round(
            sum(1 for n in sent_lens if n <= _SHORT_SENT) / len(sent_lens), 3)
            if sent_lens else 0.0,
        "brief_paragraph_share": round(
            sum(1 for n in par_sents if 1 <= n <= 3) / len(par_sents), 3)
            if par_sents else 0.0,
        "words": total,
    }


AXIS_CUES = (
    (r"em[- ]?dash|dashes", "em_dash_per_1000", "more"),
    (r"temporal|chronolog|time marker|sequence of time", "temporal_per_1000", "more"),
    (r"\bcount(s|ed|ing)?\b|\bnumbers?\b|\bnumer(al|ic)|\btally\b",
     "numeral_per_1000", "more"),
    (r"short(er)?,? (declarative |terse |clipped )?sentences?|staccato|terse|clipped",
     "sentence_median", "less"),
    (r"short(er)?,? (declarative |terse |clipped )?sentences?|staccato|terse|clipped|fragment",
     "short_sentence_share", "more"),
    (r"long(er)? sentences|\bflowing\b|sinuous", "sentence_median", "more"),
    (r"\bvary|varied|variation|mix of (long|short)|alternat", "sentence_spread", "more"),
    (r"brief paragraph|short paragraph|1-3 sentences|one or two sentences",
     "brief_paragraph_share", "more"),
    (r"sparse dialogue|little dialogue|dialogue (is )?sparse", "dialogue_ratio", "less"),
    (r"minimal attribution|plain attribution|attribution\w*[^.]{0,20}\bplain\b"
     r"|\bsaid\b|attribution (is )?minimal|few(er)? (dialogue )?tags"
     r"|plain (dialogue )?tags",
     "plain_attribution", "more"),
    (r"elaborate tags?|emotional tags?|expressive (dialogue )?tags?"
     r"|attribut\w+[^.]{0,24}heavily"
     r"|\b(whispered|hissed|growled|snapped|muttered|barked)\b",
     "plain_attribution", "less"),
    (r"action beat|beats?\b.*dialogue|gesture", "beat_rate", "more"),
    (r"sensory|senses|visual|auditory|tactile|smell|sound", "senses_present", "more"),
    (r"concrete|physical|\bbod(y|ies)\b|\bstrain(s|ed|ing)?\b|exertion"
     r"|weight of", "physical_per_1000", "more"),
    (r"\babstract", "abstract_per_1000", "less"),
    (r"filter|perceiv|realiz|realis|noticed?", "filter_per_1000", "less"),
    (r"vary (the )?(sentence )?opening|opening variety|begin sentences differently",
     "opening_repeat_share", "less"),
    (r"summar|compress time|time passes quickly", "time_compression_per_1000", "more"),
)

UNMEASURABLE_CUES = (
    r"understat", r"implic", r"subtext", r"withh[oe]ld", r"restraint",
    r"irony", r"tone\b", r"mood", r"metaphor", r"motif", r"theme",
    r"perspective|point of view|third[- ]person|first[- ]person",
    r"tension", r"pacing", r"voice",
)


def rules_of(artifact: str) -> list:
    out = []
    for line in str(artifact or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.upper().startswith("USAGE:"):
            continue
        s = re.sub(r"^\d+[.)]\s*", "", s)
        for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z])", s):
            sent = sent.strip()
            if len(sent.split()) >= 3:
                out.append(sent)
    return out


def claims_of(artifact: str) -> dict:
    rules = rules_of(artifact)
    claims, axes, unmeasured = [], {}, []
    for rule in rules:
        low = rule.lower()
        neg = re.split(r"\b(?:rather than|instead of|not|avoid|never|without)\b",
                       low, maxsplit=1)
        negated = neg[1] if len(neg) > 1 else ""
        hit = False
        for pat, axis, direction in AXIS_CUES:
            if re.search(pat, low):
                hit = True
                if negated and re.search(pat, negated) and not re.search(pat, neg[0]):
                    direction = "less" if direction == "more" else "more"
                claims.append({"rule": rule, "axis": axis, "direction": direction})
                axes.setdefault(axis, direction)
        if not hit and any(re.search(p, low) for p in UNMEASURABLE_CUES):
            unmeasured.append(rule)
    return {"claims": claims, "axes": axes, "unmeasured": unmeasured,
            "rules": len(rules)}


def moved_toward(axis: str, source: float, control: float, steered: float,
                 tol: float = 0.0) -> str:
    try:
        s, c, t = float(source), float(control), float(steered)
    except (TypeError, ValueError):
        return "same"
    gain = abs(c - s) - abs(t - s)
    if gain > tol:
        return "toward"
    if gain < -tol:
        return "away"
    return "same"


def score_claims(claims: dict, source: dict, control: dict, steered: dict,
                 tol: dict | None = None) -> dict:
    tol = tol or {}
    rows, toward, away, same = [], 0, 0, 0
    for axis, direction in claims.get("axes", {}).items():
        if axis not in source or axis not in control or axis not in steered:
            continue
        verdict = moved_toward(axis, source[axis], control[axis], steered[axis],
                               tol.get(axis, 0.0))
        rows.append({"axis": axis, "direction": direction,
                     "source": source[axis], "control": control[axis],
                     "steered": steered[axis], "verdict": verdict})
        toward += verdict == "toward"
        away += verdict == "away"
        same += verdict == "same"
    unclaimed = []
    for axis in source:
        if axis in claims.get("axes", {}) or axis == "words":
            continue
        v = moved_toward(axis, source[axis], control.get(axis), steered.get(axis),
                         tol.get(axis, 0.0))
        if v != "same":
            unclaimed.append({"axis": axis, "verdict": v,
                              "source": source[axis], "control": control.get(axis),
                              "steered": steered.get(axis)})
    return {"rows": rows, "toward": toward, "away": away, "same": same,
            "claimed": len(rows), "unclaimed_moves": unclaimed,
            "unmeasured": claims.get("unmeasured", [])}


def fidelity_of(claims: dict, source: dict, baseline: dict,
                tol: dict | None = None) -> dict:
    tol = tol or {}
    rows, d, p, i = [], 0, 0, 0
    for axis, direction in claims.get("axes", {}).items():
        if axis not in source or axis not in baseline:
            continue
        try:
            s, b = float(source[axis]), float(baseline[axis])
        except (TypeError, ValueError):
            continue
        gap = s - b
        floor = float(tol.get(axis, 0.0))
        why = ""
        if direction == "less" and b <= 0:
            verdict, why = "indistinguishable", "baseline at zero - unresolvable"
        elif abs(gap) <= floor:
            verdict = "indistinguishable"
        elif (gap > 0) == (direction == "more"):
            verdict = "describes"
        else:
            verdict = "prescribes"
        rows.append({"axis": axis, "direction": direction, "source": s,
                     "baseline": b, "verdict": verdict, "why": why,
                     "rule": next((c["rule"] for c in claims.get("claims", [])
                                   if c["axis"] == axis), "")})
        d += verdict == "describes"
        p += verdict == "prescribes"
        i += verdict == "indistinguishable"
    return {"rows": rows, "describes": d, "prescribes": p,
            "indistinguishable": i, "claimed": len(rows)}


def by_kind(text: str) -> dict:
    pars = ST.paragraphs(str(text or ""))
    dial = [p for p in pars if ST.speech_segments(p)]
    narr = [p for p in pars if not ST.speech_segments(p)]
    out = {}
    for kind, group in (("dialogue", dial), ("narration", narr)):
        joined = "\n\n".join(group)
        out[kind] = {"paragraphs": len(group),
                     **({k: v for k, v in flat_profile(joined).items()
                         if k in ("sentence_median", "sentence_spread",
                                  "short_sentence_share", "physical_per_1000",
                                  "filter_per_1000", "abstract_per_1000",
                                  "em_dash_per_1000", "temporal_per_1000",
                                  "words")}
                        if group else {})}
    return out


def median_profile(profiles: list) -> dict:
    if not profiles:
        return {}
    out = {}
    for k in profiles[0]:
        vals = [p[k] for p in profiles if isinstance(p.get(k), (int, float))]
        out[k] = round(statistics.median(vals), 4) if vals else profiles[0][k]
    return out


def steering_conflicts(sources) -> list:
    by_axis = {}
    for src in (sources or []):
        label = str((src or {}).get("label") or "?")
        for c in claims_of((src or {}).get("text") or "")["claims"]:
            by_axis.setdefault(c["axis"], []).append(
                {"label": label, "rule": c["rule"], "direction": c["direction"]})

    rows = []
    for axis, sides in sorted(by_axis.items()):
        dirs = {s["direction"] for s in sides}
        if "more" in dirs and "less" in dirs:
            seen, keep = set(), []
            for s in sides:
                key = (s["label"], s["direction"])
                if key not in seen:
                    seen.add(key)
                    keep.append(s)
            rows.append({"axis": axis, "sides": keep})
    return rows


def spread_profile(profiles: list) -> dict:
    if len(profiles) < 2:
        return {k: 0.0 for k in (profiles[0] if profiles else {})}
    out = {}
    for k in profiles[0]:
        vals = [p[k] for p in profiles if isinstance(p.get(k), (int, float))]
        out[k] = round((max(vals) - min(vals)) / 2, 4) if vals else 0.0
    return out
