from __future__ import annotations

import re

from loom_measures import INTERIOR, SENSES, POSTURE, CHANGE, _content_words


_INT_VERBS = (r"thought|felt|feeling|wondered|knew|realised|realized"
              r"|remembered|hoped|wanted|considered|forgot|respected|minded"
              r"|mind|sure|liked|noticed|decided|sensed|understood|smelling"
              r"|smelled|saw|see|watching")
_SUBJ_INT = re.compile(
    r"\b(he|He|she|She|they|They|[A-Z][a-z]+)\s+"
    r"(?:just\s+|slowly\s+|still\s+)?"
    r"(?:did\s?n[o']t\s+|was\s?n[o']t\s+|could\s?n[o']t\s+|was\s+)?"
    rf"(?i:{_INT_VERBS})\b")

_PRONOUNS = ("he", "she", "they")


def focal(text: str) -> dict:
    counts = {}
    for m in _SUBJ_INT.finditer(text):
        who = m.group(1).lower()
        counts[who] = counts.get(who, 0) + 1
    if not counts:
        return {"focaliser": "-", "focal_breaks": 0.0, "focal_owners": 0}
    focaliser = max(counts, key=lambda k: (counts[k], -len(k)))
    pro = {k: v for k, v in counts.items() if k in _PRONOUNS}
    away = sum(v for k, v in pro.items() if k != focaliser) if focaliser in _PRONOUNS \
        else 0
    words = max(1, len(text.split()))
    return {"focaliser": focaliser,
            "focal_breaks": round(1000 * away / words, 2),
            "focal_owners": len(counts)}



def senses_detail(text: str) -> dict:
    per = {k: len(re.findall(p, text, re.I)) for k, p in SENSES.items()}
    total = sum(per.values())
    sight = per.get("sight", 0)
    return {"senses_kinds": sum(1 for v in per.values() if v),
            "senses_nonsight_kinds": sum(1 for k, v in per.items()
                                         if v and k != "sight"),
            "senses_hits": total,
            "nonsight_share": round((total - sight) / total, 2) if total else 0.0,
            "per_sense": per}



_EMOTION = re.compile(
    r"\b(happy|happily|sad|sadly|angry|angrily|afraid|scared|frightened|nervous"
    r"|nervously|excited|excitedly|anxious|anxiously|furious|delighted|miserable"
    r"|embarrassed|ashamed|proud|jealous|lonely|relieved|terrified|joy|sorrow"
    r"|rage|fear|grief|love|hatred)\b", re.I)

_TELL = re.compile(
    r"\b(he|she|they|[A-Z][a-z]+)\s+(?:was|were|felt|seemed|looked)\s+"
    r"(?:very\s+|quite\s+|so\s+)?"
    r"(happy|sad|angry|afraid|scared|nervous|excited|anxious|furious|delighted"
    r"|miserable|embarrassed|ashamed|proud|jealous|lonely|relieved|terrified)\b",
    re.I)


def subtext_proxy(text: str) -> dict:
    words = max(1, len(text.split()))
    return {"emotion_named": round(1000 * len(_EMOTION.findall(text)) / words, 2),
            "emotion_told": len(_TELL.findall(text))}



_BODY = (r"eyes?|eyebrows?|brows?|lips?|jaw|mouth|shoulders?|chin|throat"
         r"|hands?|fingers?|chest|stomach|face|cheeks?|neck|breath|head|toes")
_REACT_V = (r"lit|widened|narrowed|flickered|dropped|rose|lifted|tightened"
            r"|tensed|twitched|trembled|shook|flushed|reddened|paled|bit|caught"
            r"|stiffened|softened|closed|opened|clenched|curled|went|squeez\w+"
            r"|claw\w+|smil\w+|grin\w+|blush\w+|nod\w+|shrug\w+|sigh\w+"
            r"|swallow\w+|lick\w+|wiggl\w+|jump\w+|star\w+|look\w+")
_REACTION = re.compile(
    rf"(?:\b(?:{_BODY})\b[^.!?]{{0,40}}?\b(?:{_REACT_V})\b)"
    rf"|(?:\b(?:{_REACT_V})\b[^.!?]{{0,25}}?\b(?:{_BODY})\b)", re.I)

_BEAT = re.compile(r'["“”]|\b(said|asked|answered|replied|told)\b', re.I)


def reaction(text: str) -> dict:
    sents = [s for s in re.split(r"(?<=[.!?])\s+", text) if len(s) > 20] or [text]
    hits = [bool(_REACTION.search(s)) for s in sents]
    beats = [bool(_BEAT.search(s)) for s in sents]
    near = sum(1 for i, h in enumerate(hits)
               if h and (beats[i] or (i and beats[i - 1])))
    return {"reactions": sum(hits),
            "reaction_rate": round(sum(hits) / len(sents), 2),
            "reactions_on_beat": near}



def _intensity(seg: str) -> float:
    w = max(1, len(seg.split()))
    hits = (len(CHANGE.findall(seg)) + len(POSTURE.findall(seg))
            + len(_BEAT.findall(seg)) + len(_REACTION.findall(seg))
            + len(VISUAL.findall(seg))
            + sum(len(re.findall(p, seg, re.I)) for p in SENSES.values()))
    return round(100 * hits / w, 2)


def escalation(text: str, segments: int = 4) -> dict:
    w = text.split()
    if len(w) < segments * 25:
        return {"escal_slope": 0.0, "escal_rises": 0, "escal_peak": 0.0,
                "escal_resets": 0, "escal_curve": []}
    size = len(w) // segments
    segs = [" ".join(w[i * size:(i + 1) * size]) for i in range(segments)]
    curve = [_intensity(s) for s in segs]
    rises = sum(1 for i in range(1, len(curve)) if curve[i] > curve[i - 1])
    peak = curve.index(max(curve))
    resets = sum(1 for i in range(1, peak + 1) if curve[i] < curve[i - 1])
    return {"escal_slope": round(curve[-1] - curve[0], 2),
            "escal_rises": rises,
            "escal_peak": round(peak / max(1, len(curve) - 1), 2),
            "escal_resets": resets,
            "escal_curve": curve}




_COLOUR = (r"white|black|red|blue|green|yellow|grey|gray|brown|pink|purple"
           r"|golden|gold|silver|pale|dark|bright|crimson|scarlet|amber|blond\w*")
_LIGHT = (r"light|lights|lit|shadow\w*|glow\w*|shine|shining|shone|gleam\w*"
          r"|glint\w*|flicker\w*|dim|glare|sunlight|daylight|moonlight|dusk|dusty")
_MATERIAL = (r"silk|cotton|wool|leather|denim|linen|velvet|satin|lace|rubber"
             r"|steel|iron|wooden|wood|glass|plastic|metal|concrete|paper"
             r"|fabric|cloth|fur|skin")
_SHAPE = (r"slim|thin|thick|wide|narrow|tall|short|small|little|big|large|huge"
          r"|tight|loose|round|flat|curved|petite|slender|broad|heavy|light"
          r"|long|square|sharp|smooth|rough")
_GARMENT = (r"\b(?:blouses?|shirts?|dress(?:es)?|skirts?|trousers|pants"
            r"|coats?|jackets?|sleeves?|collars?|hems?|buttons?|zips?"
            r"|boots?|shoes?|gloves?|hats?|scarf|scarves)\b")

_PART_CORE = (r"shoulders?|arms?|elbows?|wrists?|hands?|fingers?|thumbs?"
              r"|knuckles?|palms?|nails?"
              r"|head|face|jaw|chin|cheeks?|mouth|lips?|tongue|teeth|nose"
              r"|ears?|eyes?|eyebrows?|brows?|forehead|temples?|scalp|hair"
              r"|neck|throat|chest|ribs?|ribcage|stomach|belly"
              r"|waist|hips?|pelvis|behind"
              r"|legs?|thighs?|knees?|calves|calf|shins?|ankles?|feet|foot"
              r"|toes?|heels?|skin|flesh|bone|muscles?|tendons?|nerves?"
              r"|spine|frame|build|posture|stance|bearing|figure|body|bodies")

_PART_OWNED = r"back|side|sides|front"

_PART_STATE = (r"straight|upright|hunched|stooped|slouch\w*|sagg\w*|limp\w*"
               r"|steady|unsteady|swaying|stagger\w*|lean|wiry|broad|stocky"
               r"|gaunt|scarred|bruised|bloodied|filthy|caked|streaked"
               r"|sunburn\w*|weathered|carrying|shouldering|hauling|laden"
               r"|burdened")

_BODY_VIS = (rf"(?:{_PART_CORE})"
             rf"|(?:(?:his|her|their|its|my|your|the)\s+(?:{_PART_OWNED}))"
             rf"|(?:{_PART_STATE})")

_WORN = (_GARMENT + r"|uniform\w*|kit|webbing|helmet|cap|belt|strap\w*|pack\w*"
         r"|backpack\w*|rucksack\w*|boots?|laces?|cuffs?|pocket\w*|seam\w*"
         r"|muddied|muddy|bloody|bloodstained|ripped|torn|frayed|patched"
         r"|soaked|sodden|stained|worn|threadbare|pressed|starched|buttoned")

WORN = re.compile(rf"\b(?:{_WORN}|{_MATERIAL})\b", re.I)
BODY_VIS = re.compile(rf"\b(?:{_BODY_VIS})\b", re.I)
SCENE_VIS = re.compile(rf"\b(?:{_COLOUR}|{_LIGHT}|{_SHAPE})\b", re.I)

VISUAL = re.compile(
    rf"\b(?:{_COLOUR}|{_LIGHT}|{_MATERIAL}|{_SHAPE}|{_GARMENT})\b", re.I)


def visual_detail(text: str) -> dict:
    words = max(1, len(text.split()))
    vis = VISUAL.findall(text)
    sight = re.findall(SENSES["sight"], text, re.I)
    worn = WORN.findall(text)
    body = BODY_VIS.findall(text)
    scene = SCENE_VIS.findall(text)
    return {"visual_detail": round(100 * len(vis) / words, 2),
            "visual_hits": len(vis),
            "sight_verbs": round(100 * len(sight) / words, 2),
            "shown_vs_looked": round(len(vis) / max(1, len(sight)), 2),
            "worn": round(100 * len(worn) / words, 2),
            "body_vis": round(100 * len(body) / words, 2),
            "scene_vis": round(100 * len(scene) / words, 2),
            "worn_vs_body": round(len(worn) / max(1, len(body)), 2)}



_WEAR_V = (r"wear|wears|wearing|wore|worn|dress\w*\s+in|clad|put\s+on|puts\s+on"
           r"|pull\w*\s+on|slip\w*\s+on|button\w*|unbutton\w*|zip\w*|unzip\w*"
           r"|buckl\w*|lac\w*\s+up|don|donn\w*|strapp?\w*\s+on")

_NOT_A_NAME = (r"The|A|An|It|There|This|That|These|Those|But|And|Then|When"
               r"|After|Before|If|So|Yet|Still|Now|Here|Outside|Inside|Above"
               r"|Below|Behind|Beyond|Across|Somewhere|Everything|Nothing"
               r"|Something|Someone|Nobody|Everyone|One|Two|Both|Each|Every"
               r"|What|Why|How|Where|Who|Once|Later|Soon|Today|Tomorrow")

_INTRANSITIVE_UNDRESS_SRC = (
    r"\b(?:he|she|they|(?-i:(?!(?:" + (
        r"The|A|An|It|There|This|That|These|Those|But|And|Then|When"
        r"|After|Before|If|So|Yet|Still|Now|Here|Outside|Inside|Above"
        r"|Below|Behind|Beyond|Across|Somewhere|Everything|Nothing"
        r"|Something|Someone|Nobody|Everyone|One|Two|Both|Each|Every"
        r"|What|Why|How|Where|Who|Once|Later|Soon|Today|Tomorrow"
    ) + r")\b)[A-Z][a-z]+))\s+"
    r"(?:slowly\s+|quickly\s+|quietly\s+|carefully\s+)?"
    r"(?:undress\w*|disrob\w*)\b"
    r"(?!\s+(?:the|a|an|this|that|these|those|as|it|its)\b)")

_STRIP_V = (r"tak\w*|took|get|got|pull\w*|strip\w*|peel\w*|shrug\w*|remov\w*"
            r"|tore|tear\w*|rip\w*|cut|undress\w*|disrob\w*")

_NOUNISH = r"(?:the|a|an|his|her|their|its|my|your|[A-Z][a-z]+'s)?\s*(?:\w+\s+){0,3}(\w+)"

WORN_FRAME = re.compile(
    rf"\b(?:{_STRIP_V})\s+(?:of\s+)?{_NOUNISH}\s+off\b"
    rf"|\b(?:{_STRIP_V})\s+off\s+{_NOUNISH}"
    rf"|\b(?:{_STRIP_V})\s+of\s+{_NOUNISH}"
    rf"|\b(?:{_WEAR_V})\s+(?:in\s+)?{_NOUNISH}"
    rf"|\breveal\w*\s+{_NOUNISH}"
    rf"|\bin\s+(?:his|her|their|the)\s+(?:\w+\s+){{0,3}}(\w+s)\b",
    re.I)

WORN_ON_BODY = re.compile(
    rf"\b(?:on|over|under|around|about)\s+(?:his|her|their|the)\s+"
    rf"(?:{_BODY_VIS})\b[^.!?]{{0,40}}?\b(?:had|wore|was|were|is|are)\b"
    rf"[^.!?]{{0,30}}?{_NOUNISH}", re.I)

_SPATIAL = (r"under|underneath|beneath|over|above|around|behind|between|against"
            r"|across|onto|into|out\s+of|up|down|back|forward|toward\w*|away"
            r"|past|through|beside|apart|together|closer|nearer|aside|upright")

MOVE_FRAME = re.compile(
    rf"\b(?:{_BODY_VIS})\b[^.!?]{{0,50}}?\b(?:{_SPATIAL})\b"
    rf"|\b(?:{_SPATIAL})\b[^.!?]{{0,30}}?\b(?:{_BODY_VIS})\b", re.I)

_OTHER = (r"him|her|them|his|hers|their|the\s+other|the\s+opponent|each\s+other"
          r"|[A-Z][a-z]+'s")
MOVE_RELATIVE = re.compile(
    rf"\b(?:{_SPATIAL})\s+(?:{_OTHER})\b", re.I)


_PERSON_MARK = (r"\b(?:his|her|their|my|your|hers|theirs)\b"
                r"|\b(?:man|woman|men|women|boy|girl|soldier|guard|body"
                r"|himself|herself|themselves|shoulders?|chest|legs?|arms?)\b")

_OFF_IDIOM = re.compile(
    r"\b(?:tak\w*|took|get|got|pull\w*)\s+"
    r"(?:the\s+|his\s+|her\s+|their\s+|my\s+|your\s+)?"
    r"(?:time|day|days|week|month|year|edge|weight|pressure|heat|eyes?|gaze"
    r"|mind|hands?\s+off\s+(?!h(?:is|er)\s+\w*(?:shirt|coat|dress))|foot|load)"
    r"\b[^.!?]{0,12}\boff\b"
    r"|\b(?:get|got|getting|keep|kept|tak\w*|took|pull\w*|ward\w*)\s+"
    r"(?:him|her|them|me|us|you|it)\s+off\s+"
    r"(?:his|her|their|my|your|our)\s+(?:back|case|chest|tail|neck|hands?)\b",
    re.I)

_INTRANSITIVE_UNDRESS = re.compile(_INTRANSITIVE_UNDRESS_SRC, re.I)

DRESSING_FRAME = re.compile(
    rf"\b(?:{_STRIP_V})\s+(?:of\s+)?{_NOUNISH}\s+off\b"
    rf"|\b(?:{_STRIP_V})\s+off\s+{_NOUNISH}"
    rf"|\b(?:{_STRIP_V})\s+of\s+{_NOUNISH}"
    rf"|\bunbutton\w*|\bunzip\w*|\bunlac\w*|\bunbuckl\w*|\bunfasten\w*"
    rf"|\bshrug\w*\s+(?:it\s+|them\s+)?off\b"
    rf"|\breveal\w*\s+(?:\w+\s+){{0,2}}(?:{_GARMENT}|{_PART_CORE})\b"
    rf"|\b(?:put\w*|pull\w*|slip\w*|shrug\w*)\s+"
    rf"(?!(?:\w+\s+){{0,2}}(?:{_PART_CORE})\b)(?:{_NOUNISH}\s+)?on\b"
    rf"|\bbutton\w*|\bzip\w*|\blac\w*\s+up|\bbuckl\w*|\bfasten\w*"
    rf"|\bdress\w*\s+(?:himself|herself|themselves)\b"
    rf"|{_INTRANSITIVE_UNDRESS_SRC}"
    rf"|\bstrapp?\w*\s+(?:{_NOUNISH}\s+)?on\b", re.I)

_TOUCH_V = (r"touch\w*|brush\w*|press\w*|grip\w*|grasp\w*|grab\w*|hold|held"
            r"|stroke\w*|caress\w*|cup\w*|squeez\w*|clasp\w*|cradl\w*|nudg\w*"
            r"|tug\w*|pull\w*|push\w*|lean\w*|rest\w*|slid\w*|trac\w*"
            r"|wrap\w*|lock\w*|curl\w*")

_OTHER = r"(?:him|her|them|his|their|hers|theirs)"

_GAP = r"(?:[\w'’]+\s+){0,2}"

TOUCH_VERB = re.compile(
    rf"\b(?:{_TOUCH_V})\s+(?:{_OTHER}\s+)?{_GAP}(?:{_PART_CORE})\b"
    rf"|\b(?:{_TOUCH_V})\s+(?:against|into|onto)\s+{_OTHER}\b", re.I)

TOUCH_PARTS = re.compile(
    rf"\b(?:his|her|their|the)\s+{_GAP}(?:{_PART_CORE})\b"
    rf"\s+{_GAP}"
    rf"(?:on|against|over|across|along|around|under|beneath|behind|between)\s+"
    rf"(?:his|her|their|the)\s+{_GAP}(?:{_PART_CORE})\b", re.I)


def worn_frames(text: str) -> dict:
    words = max(1, len(text.split()))
    a = WORN_FRAME.findall(text)
    b = WORN_ON_BODY.findall(text)
    n = len(a) + len(b)
    return {"worn_frame": round(100 * n / words, 2), "worn_frame_hits": n}


def dressing(text: str) -> dict:
    words = max(1, len(text.split()))
    n = 0
    for clause in re.split(r"[.!?;]|\band\b|\bthen\b", text):
        if not DRESSING_FRAME.search(clause):
            continue
        if _OFF_IDIOM.search(clause):
            continue
        if _INTRANSITIVE_UNDRESS.search(clause):
            n += 1
            continue
        if re.search(_PERSON_MARK, clause) or re.search(_GARMENT, clause, re.I):
            n += 1
    return {"dressing": round(100 * n / words, 2), "dressing_hits": n}


_LOOK_V = (r"glanc\w*|look\w*|watch\w*|catch\w*|caught|meet|met|hold|held"
           r"|stud\w*|eye\w*|gaz\w*|stare\w*|peek\w*|steal\w*|stole")
_PLAY_V = (r"smil\w*|grin(?:s|ned|ning)?\b|smirk\w*|laugh\w*|chuckl\w*|teas\w*|flirt\w*"
           r"|wink\w*|blush\w*|flush\w*|bit\w*\s+(?:his|her|their)\s+lip"
           r"|rais\w*\s+(?:an?\s+)?(?:eye)?brow|banter\w*|quip\w*")
_PERSONISH = (r"(?:him|her|them|his|their|hers|he|she|they"
              rf"|(?-i:(?!(?:{_NOT_A_NAME})\b)[A-Z][a-z]+))")

FLIRT_FRAME = re.compile(
    rf"\b(?:{_LOOK_V})\s+{_GAP}(?:at|toward\w*|back\s+at|over\s+at)\s+"
    rf"{_PERSONISH}\b"
    rf"|\b(?:{_PLAY_V})\b[^.!?]{{0,40}}?\b{_PERSONISH}\b"
    rf"|\b{_PERSONISH}\b[^.!?]{{0,40}}?\b(?:{_PLAY_V})\b"
    rf"|\b(?:eyes?|gaze)\s+{_GAP}(?:met|meet|locked|held)\b", re.I)


def flirting(text: str) -> dict:
    words = max(1, len(text.split()))
    n = len(FLIRT_FRAME.findall(text))
    look = len(re.findall(
        rf"\b(?:{_LOOK_V})\s+{_GAP}(?:at|toward\w*|back\s+at)\s+"
        rf"{_PERSONISH}\b", text, re.I))
    play = len(re.findall(rf"\b(?:{_PLAY_V})\b", text, re.I))
    return {"flirting": round(100 * n / words, 2),
            "flirt_look": round(100 * look / words, 2),
            "flirt_play": round(100 * play / words, 2),
            "flirt_hits": n}


def touch(text: str) -> dict:
    words = max(1, len(text.split()))
    v = len(TOUCH_VERB.findall(text))
    p = len(TOUCH_PARTS.findall(text))
    return {"touch": round(100 * (v + p) / words, 2),
            "touch_parts": round(100 * p / words, 2),
            "touch_hits": v + p}


def movement(text: str) -> dict:
    words = max(1, len(text.split()))
    mv = MOVE_FRAME.findall(text)
    rel = []
    for sent in re.split(r"(?<=[.!?])\s+", text):
        hits = MOVE_FRAME.findall(sent)
        if hits and (MOVE_RELATIVE.search(sent)
                     or re.search(rf"(?:{_OTHER})", sent)):
            rel.extend(hits)
    return {"movement": round(100 * len(mv) / words, 2),
            "movement_hits": len(mv),
            "movement_rel": round(100 * len(rel) / words, 2),
            "two_bodies": round(len(rel) / len(mv), 2) if mv else 0.0}



_PART = (_BODY_VIS + r"|skin|flesh|bone|ribs?|ribcage|knee|ankle|wrist|elbow"
         r"|thigh|calf|hip|foot|feet|toes?|scalp|temple|ear|eye|nose|tongue"
         r"|lip|gut|belly|side|palm|knuckles?|nail|joint|muscle|tendon|nerve")

BODY_STATE = re.compile(
    rf"\b(?:{_PART})\b[^.!?]{{0,40}}?"
    rf"\b(?:was|were|is|are|became|becoming|turned|turning|went|going|looked"
    rf"|felt|feels|seemed|stayed|remained|had\s+gone)\b[^.!?]{{0,30}}"
    rf"|\b(?:swell\w*|bruis\w*|bleed\w*|bled|blood\w*|numb\w*|throb\w*"
    rf"|ach\w*|cramp\w*|blister\w*|burn\w*|frostbit\w*|sprain\w*|torn|tore"
    rf"|broken|fractur\w*|stiff\w*|raw|sore|tender|infect\w*|fever\w*)\b",
    re.I)

_WHEN = (r"now|by\s+now|then|by\s+then|later|already|still|yet|since"
         r"|an?\s+hour|hours?|minutes?|days?|weeks?|morning|evening|overnight"
         r"|starting\s+to|beginning\s+to|getting|growing|worse|better"
         r"|no\s+longer|at\s+first|after\s+a\s+while|by\s+the\s+time")

BODY_PROGRESS = re.compile(
    rf"\b(?:{_PART}|swell\w*|bruis\w*|pain|wound|injur\w*)\b"
    rf"[^.!?]{{0,60}}?\b(?:{_WHEN})\b"
    rf"|\b(?:{_WHEN})\b[^.!?]{{0,60}}?"
    rf"\b(?:{_PART}|swell\w*|bruis\w*|pain|wound|injur\w*)\b", re.I)


_SENTENCES = re.compile(r"(?<=[.!?])\s+")

def body_state(text: str) -> dict:
    words = max(1, len(text.split()))
    st = BODY_STATE.findall(text)
    sents = [s for s in _SENTENCES.split(text) if s.strip()]
    stateful = [s for s in sents if BODY_STATE.search(s)]
    pr = [m for s in stateful for m in BODY_PROGRESS.findall(s)]
    timed = [s for s in stateful if BODY_PROGRESS.search(s)]
    followed = (len(timed) / len(stateful)) if len(stateful) >= 2 else 0.0
    return {"body_state": round(100 * len(st) / words, 2),
            "body_progress": round(100 * len(pr) / words, 2),
            "body_followed": round(followed, 2)}



_QUOTED = re.compile(r"[\"\u201c][^\"\u201d\u201c]{0,400}?[\"\u201d]")

_PAST = (r"was|were|had|said|went|took|saw|came|got|knew|felt|looked|turned"
         r"|walked|stood|sat|made|thought|heard|held|put|kept|found|told"
         r"|asked|began|left|gave|moved|watched|waited|pulled|pushed|opened")

_PRESENT = (r"is|are|has|have|says|goes|takes|sees|comes|gets|knows|feels"
            r"|looks|turns|walks|stands|sits|makes|thinks|hears|holds|puts"
            r"|keeps|finds|tells|asks|begins|leaves|gives|moves|watches"
            r"|waits|pulls|pushes|opens")

PAST_V = re.compile(rf"\b(?:{_PAST})\b", re.I)
PRESENT_V = re.compile(rf"\b(?:{_PRESENT})\b", re.I)


def _narration(text: str) -> str:
    return _QUOTED.sub(" ", str(text or ""))


_PERSON = r"he|she|they|I|[A-Z][a-z]+"
PRESENT_ACTOR = re.compile(
    rf"\b(?:{_PERSON})\s+(?:just\s+|slowly\s+|still\s+|now\s+)?"
    rf"(?:{_PRESENT})\b")


def tense_slips(text: str):
    out = []
    for m in PRESENT_ACTOR.finditer(_narration(text)):
        a, b = max(0, m.start() - 40), m.end() + 20
        out.append(_narration(text)[a:b].strip())
    return out


def tense(text: str, min_verbs: int = 4) -> dict:
    whole = _narration(text)
    pa, pr = len(PAST_V.findall(whole)), len(PRESENT_V.findall(whole))
    total = pa + pr
    if total < min_verbs:
        return {"tense_past": None, "tense_drift": 0.0, "tense_slip": 0,
                "tense_slip_rate": 0.0, "tense_verbs": total, "tense_paras": 0}
    share = pa / total
    dominant = "past" if share >= 0.5 else "present"

    off, counted = 0, 0
    for para in re.split(r"\n\s*\n", text):
        w = _narration(para)
        a, b = len(PAST_V.findall(w)), len(PRESENT_V.findall(w))
        if a + b < min_verbs:
            continue
        counted += 1
        if ("past" if a / (a + b) >= 0.5 else "present") != dominant:
            off += 1
    slips = len(PRESENT_ACTOR.findall(whole)) if dominant == "past" else 0
    return {"tense_past": round(share, 2),
            "tense_drift": round(off / counted, 2) if counted else 0.0,
            "tense_slip": slips,
            "tense_slip_rate": round(1000 * slips / max(1, len(text.split())), 2),
            "tense_verbs": total, "tense_paras": counted}


def measure2(text: str) -> dict:
    out = {}
    out.update(focal(text))
    out.update(senses_detail(text))
    out.update(subtext_proxy(text))
    out.update(reaction(text))
    out.update(escalation(text))
    out.update(visual_detail(text))
    out.update(worn_frames(text))
    out.update(dressing(text))
    out.update(flirting(text))
    out.update(touch(text))
    out.update(movement(text))
    out.update(body_state(text))
    out.update(tense(text))
    return out


TIER2_KEYS = ("focal_breaks", "nonsight_share", "senses_hits",
              "emotion_named", "emotion_told",
              "reactions", "reaction_rate", "reactions_on_beat",
              "escal_slope", "escal_rises", "escal_peak", "escal_resets",
              "visual_detail", "sight_verbs", "shown_vs_looked",
              "worn", "body_vis", "scene_vis", "worn_vs_body",
              "worn_frame", "movement", "movement_rel", "two_bodies",
              "dressing", "dressing_hits", "touch", "touch_parts", "touch_hits",
              "flirting", "flirt_look", "flirt_play", "flirt_hits",
              "body_state", "body_progress", "body_followed",
              "tense_past", "tense_drift", "tense_slip_rate")
