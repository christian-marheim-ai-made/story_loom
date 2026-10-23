from __future__ import annotations

import re
import statistics


_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+")
_SPEECH = re.compile(r'"[^"]+"|“[^”]+”')
_WORD = re.compile(r"[A-Za-zÀ-ɏ'’-]+")


def paragraphs(text: str) -> list:
    out = []
    for block in re.split(r"\n\s*\n", text or ""):
        block = block.strip()
        if block and not _is_scene_break(block):
            out.append(block)
    return out


def sentences(text: str) -> list:
    return [s for s in _SENT_SPLIT.split(text.strip()) if _WORD.search(s)]


def words(text: str) -> list:
    return _WORD.findall(text or "")


def speech_segments(par: str) -> list:
    return _SPEECH.findall(par)


def _is_scene_break(block: str) -> bool:
    return bool(re.fullmatch(r"[\s*#~—-]{3,}", block.strip())) \
        and bool(re.search(r"[*#~—-]", block))


def _quartiles(values: list) -> dict:
    if not values:
        return {"n": 0, "min": 0, "q1": 0, "median": 0, "q3": 0, "max": 0,
                "spread": 0}
    if len(values) == 1:
        v = values[0]
        return {"n": 1, "min": v, "q1": v, "median": v, "q3": v, "max": v,
                "spread": 0}
    q1, med, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return {"n": len(values), "min": min(values), "q1": round(q1, 1),
            "median": round(med, 1), "q3": round(q3, 1), "max": max(values),
            "spread": round(q3 - q1, 1)}


PLAIN_ATTRIBUTION = ("said", "says", "asked", "asks")
ATTRIBUTION_VERBS = PLAIN_ATTRIBUTION + (
    "whispered", "shouted", "exclaimed", "murmured", "muttered", "hissed",
    "growled", "snapped", "breathed", "groaned", "cried", "yelled",
    "replied", "answered", "demanded", "insisted", "observed", "remarked")

FILTER_WORDS = (
    "saw", "sees", "see", "heard", "hears", "hear", "felt", "feels", "feel",
    "noticed", "notices", "notice", "watched", "watches", "watch",
    "realised", "realized", "realises", "realizes", "seemed", "seems",
    "wondered", "wonders")

SENSES = {
    "sight": ("saw", "see", "light", "dark", "colour", "color", "red", "grey",
              "gray", "white", "black", "bright", "shadow", "glint", "pale",
              "gleam", "flicker", "shone"),
    "sound": ("heard", "hear", "sound", "quiet", "silence", "creak", "echo",
              "hum", "crack", "whisper", "roar", "rustle", "thud", "ring"),
    "touch": ("felt", "cold", "warm", "rough", "smooth", "wet", "dry",
              "sting", "weight", "pressed", "grip", "heat", "chill"),
    "smell": ("smell", "smelled", "smelt", "scent", "stink", "reek",
              "perfume", "smoke"),
    "taste": ("taste", "tasted", "sweet", "bitter", "sour", "salt", "salty"),
}

ABSTRACT_SUFFIXES = ("ness", "tion", "sion", "ment", "ity", "ance", "ence",
                     "ship", "ism", "dom", "tude", "hood")

TIME_COMPRESSION = (
    "for weeks", "for days", "for months", "for years", "every day",
    "every night", "each morning", "each day", "would often", "would always",
    "by then", "over the next", "in the weeks", "in the months",
    "as the weeks", "as the months", "day after day")



def count_metrics(text: str) -> dict:
    pars = paragraphs(text)
    all_words = words(text)
    total_words = len(all_words)
    lower_words = [w.lower() for w in all_words]

    par_lens = [len(words(p)) for p in pars]
    c1 = _quartiles(par_lens)

    sent_lens = [len(words(s)) for p in pars for s in sentences(p)]
    c2 = _quartiles(sent_lens)
    c2["stdev"] = round(statistics.pstdev(sent_lens), 2) if sent_lens else 0

    dial_pars = [p for p in pars if speech_segments(p)]
    c3 = {"dialogue_paragraphs": len(dial_pars),
          "ratio": round(len(dial_pars) / len(pars), 3) if pars else 0}

    per = []
    for p in dial_pars:
        n = 0
        for seg in speech_segments(p):
            inner = seg.strip('"“”')
            n += max(1, len(sentences(inner)))
        per.append(n)
    c4 = {"mean": round(statistics.mean(per), 2) if per else 0,
          "max": max(per) if per else 0}

    beats = 0
    for p in dial_pars:
        rest = _SPEECH.sub(" ", p)
        if len(words(rest)) >= 4:
            beats += 1
    c5 = {"beat_rate": round(beats / len(dial_pars), 3) if dial_pars else 0,
          "with_beat": beats}

    plain = fancy = 0
    for p in dial_pars:
        rest = [w.lower() for w in words(_SPEECH.sub(" ", p))]
        for w in rest:
            if w in PLAIN_ATTRIBUTION:
                plain += 1
            elif w in ATTRIBUTION_VERBS:
                fancy += 1
    c6 = {"plain": plain, "elaborate": fancy,
          "plain_share": round(plain / (plain + fancy), 3)
          if (plain + fancy) else None}

    filt = sum(1 for w in lower_words if w in FILTER_WORDS)
    c7 = {"count": filt,
          "per_1000": round(1000 * filt / total_words, 2) if total_words else 0}

    hits = {sense: sum(1 for w in lower_words if w in lex)
            for sense, lex in SENSES.items()}
    c8 = {**hits, "senses_present": sum(1 for v in hits.values() if v)}

    abst = sum(1 for w in lower_words
               if len(w) > 6 and w.endswith(ABSTRACT_SUFFIXES))
    c9 = {"abstract": abst,
          "per_1000": round(1000 * abst / total_words, 2) if total_words else 0}

    opens = [words(p)[0].lower() for p in pars if words(p)]
    repeats = sum(1 for a, b in zip(opens, opens[1:]) if a == b)
    run = best = 1 if opens else 0
    for a, b in zip(opens, opens[1:]):
        run = run + 1 if a == b else 1
        best = max(best, run)
    c10 = {"repeat_share": round(repeats / (len(opens) - 1), 3)
           if len(opens) > 1 else 0, "longest_run": best}

    low = " ".join(lower_words)
    tc = sum(low.count(m) for m in TIME_COMPRESSION)
    c11 = {"count": tc,
           "per_1000": round(1000 * tc / total_words, 2) if total_words else 0}

    breaks = sum(1 for block in re.split(r"\n\s*\n", text or "")
                 if block.strip() and _is_scene_break(block))
    scenes = breaks + 1
    c12 = {"scenes": scenes,
           "words_per_scene": round(total_words / scenes, 1) if scenes else 0}

    return {"words": total_words, "paragraphs": len(pars),
            "c1_paragraph_lengths": c1, "c2_sentence_lengths": c2,
            "c3_dialogue_ratio": c3, "c4_speech_per_dialogue_par": c4,
            "c5_beat_rate": c5, "c6_attribution": c6,
            "c7_filter_words": c7, "c8_senses": c8,
            "c9_abstractness": c9, "c10_opening_variety": c10,
            "c11_time_compression": c11, "c12_scene_grain": c12}
