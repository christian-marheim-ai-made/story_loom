from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

import loom_engine as E

__all__ = [
    "PlaybookError", "PlaybookStore", "SETTINGS", "SETTING_ORDER", "DEFAULTS",
    "check_name", "playbook_slot", "normalise", "body_for", "seal_fields",
    "expand", "GEN_KEYS", "STRUCTURED_KEYS", "RECORD_ONLY",
    "store_for", "PLAYBOOKS_DIRNAME", "REF_PREFIX", "NAME_MAX",
    "BUILTIN_SETTINGS", "BUILTIN_PREFIX", "builtins", "builtin_record",
    "builtin_resolve", "is_builtin_name",
]


class PlaybookError(ValueError):
    pass



NAME_MAX = 64
_NAME_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def check_name(raw) -> str:
    if isinstance(raw, bool) or not isinstance(raw, str):
        raise PlaybookError(
            f"a playbook name must be text, not {type(raw).__name__} "
            f"({raw!r})")
    name = raw.strip()
    if not name:
        raise PlaybookError(
            "a playbook needs a name — this one is empty. It is how you will "
            "pick it out of a comparison of four.")
    if len(name) > NAME_MAX:
        raise PlaybookError(
            f"a playbook name is at most {NAME_MAX} characters; this one is "
            f"{len(name)}: {name[:32]!r}…")
    bad = sorted(set(_NAME_ILLEGAL.findall(name)))
    if bad:
        shown = ", ".join(repr(c) for c in bad)
        raise PlaybookError(
            f"a playbook name may not contain {shown} — the name becomes a "
            f"filename under the story's playbooks/ folder. Got {name!r}.")
    if not re.search(r"[A-Za-z0-9]", name):
        raise PlaybookError(
            f"a playbook name needs at least one letter or digit; {name!r} "
            "would sanitise away to nothing and leave the file unnameable.")
    return name


def _version(raw):
    if isinstance(raw, bool):
        raise PlaybookError(f"a playbook version is a number, not {raw!r}")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        if not float(raw).is_integer():
            raise PlaybookError(
                f"a playbook version is a whole number; got {raw!r}")
        return int(raw)
    s = str(raw).strip()
    if s.startswith("v"):
        s = s[1:]
    if not s.isdigit():
        raise PlaybookError(
            f"a playbook version must be a whole number; got {str(raw)!r}. "
            f"Versions are the integers in the filenames — "
            f"`pb-<name>.v<N>.json` — and `?v=` arrives as text.")
    return int(s)


def playbook_slot(name: str) -> str:
    return E.slot_for("pb", name)


DEFAULT_RUNBOOK = "default"



SETTING_ORDER = (
    "model", "temperature", "max_tokens", "n", "seed_policy",
    "canon_words", "plan_mode", "retries", "pipeline",
    "summaries", "feedback", "feedback_words", "style", "paragraphs",
    "transition_paras", "summarise",
    "craft_lever", "craft_mode", "craft_words",
    "runbook",
)

SETTINGS = {
    "model": {"kind": "id", "default": None, "max_len": 120},

    "temperature": {"kind": "number", "default": 0.7, "min": 0.0, "max": 2.0},
    "max_tokens": {"kind": "int", "default": 600, "min": 1, "max": 32000},
    "n": {"kind": "int", "default": 1, "min": 1, "max": 16},

    "seed_policy": {"kind": "enum", "default": "fixed",
                    "values": ("fixed", "varying")},

    "canon_words": {"kind": "int", "default": int(E.TAIL_WORDS),
                    "min": int(E.CANON_WORDS_MIN), "max": 100000},

    "plan_mode": {"kind": "enum", "default": E.PLAN_MODE_DEFAULT,
                  "values": tuple(sorted(E.PLAN_HEADS))},

    "retries": {"kind": "int", "default": 3, "min": 0, "max": 3,
                "why": " — submit clamps to RETRY_MAX (loom-server.py:1638), "
                       "so a larger number here would be a record that lies"},

    "pipeline": {"kind": "enum", "default": "direct", "values": ("direct",),
                 "not_yet": {
                     "staged": "pipeline 'staged' is the named seam in "
                               "PLAN-playbooks.md §5, not a setting: it is a "
                               "different job graph and it attaches when the "
                               "Command layer's changeset lands. Only 'direct' "
                               "is honoured today, and a playbook that recorded "
                               "'staged' would describe a run that never "
                               "happened."}},

    "summaries": {"kind": "enum", "default": "full",
                  "values": ("full", "off"),
                  "not_yet": {"aggregate":
                              "summaries 'aggregate' is S3 of "
                              "PLAN-request-templates.md - the compaction does "
                              "not exist yet, and a bundle recording it would "
                              "describe a run that never happened"}},
    "feedback": {"kind": "enum", "default": "full",
                 "values": ("full", "off", "aggregate")},

    "feedback_words": {"kind": "int", "default": 0, "min": 0, "max": 20000},

    "craft_lever": {"kind": "enum", "default": None,
                    "values": ("distance", "senses", "blocking",
                               "change", "dialogue")},

    "craft_mode": {"kind": "enum", "default": None,
                   "values": ("exemplars", "instruction", "both")},

    "craft_words": {"kind": "int", "default": None, "min": 0, "max": 4000},
    "style": {"kind": "enum", "default": "full",
              "values": ("full", "off"),
              "not_yet": {"aggregate":
                          "style 'aggregate' is S3 - combined and compacted "
                          "rather than additive. Not built yet"}},

    "runbook": {"kind": "id", "default": DEFAULT_RUNBOOK, "max_len": 120},

    "transition_paras": {"kind": "int", "default": None, "min": 0, "max": 20,
                         "why": " — paragraphs of the PREVIOUS part only, and "
                                "none at all when the part opens a chapter."},

    "summarise": {"kind": "enum", "default": "after_each",
                  "values": ("after_each", "off")},

    "paragraphs": {"kind": "enum", "default": "auto",
                   "values": ("auto", "slow", "off"),
                   "not_yet": {
                       "fast": "there is no band above `auto`, and adding one "
                               "needs a measurement rather than a preference. "
                               "`auto`'s ceiling is already AT this model's "
                               "natural paragraph length (10 per 700-token "
                               "unit is 60 paragraphs at 4000 tokens, and 60 "
                               "x 46 measured words is 2,760 - the whole "
                               "budget), so a denser band asks for paragraphs "
                               "shorter than it writes, which is how G-6 got "
                               "36-41% verbatim repetition. To write faster, "
                               "lower max_tokens: the range follows it down.",
                       "dense": "see `fast` - there is no band above `auto`. "
                                "Lower max_tokens instead."}},
}

DEFAULTS = {k: SETTINGS[k]["default"] for k in SETTING_ORDER}


def _unknown_key(key, where: str) -> "PlaybookError":
    near = difflib.get_close_matches(str(key), SETTING_ORDER, n=1, cutoff=0.6)
    hint = f" Did you mean {near[0]!r}?" if near else ""
    return PlaybookError(
        f"unknown playbook setting {key!r} in {where}.{hint} A playbook "
        f"records exactly these: {', '.join(SETTING_ORDER)}. It is REFUSED "
        f"rather than dropped on purpose — `gen.retries` was dropped by a "
        f"whitelist for months and every 'retries off' measurement in this "
        f"lane ran with the full ladder (AppMap/ADDENDA.md A-013). A new axis "
        f"is added to SETTINGS in loom_playbooks.py, or it does not exist.")


def _coerce(key: str, v):
    spec = SETTINGS[key]
    kind = spec["kind"]

    if v is None and kind in ("int", "number", "enum"):
        return None

    if kind == "id":
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, str):
            raise PlaybookError(
                f"{key} must be text or null, not {type(v).__name__} ({v!r})")
        s = v.strip()
        if not s:
            return None
        if len(s) > spec["max_len"]:
            raise PlaybookError(
                f"{key} is at most {spec['max_len']} characters; got {len(s)}")
        if any(ord(c) < 32 for c in s):
            raise PlaybookError(f"{key} may not contain control characters: {s!r}")
        return s

    if kind in ("int", "number"):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise PlaybookError(
                f"{key} must be a number, not {type(v).__name__} ({v!r}). The "
                f"wire sends typed JSON; a form string is how `target_index` "
                f"and `target_sentences` were once classified by accident "
                f"(loom-server.py:2344-2351).")
        if kind == "int":
            if isinstance(v, float) and not float(v).is_integer():
                raise PlaybookError(f"{key} must be a whole number; got {v!r}")
            v = int(v)
        else:
            v = float(v)
        lo, hi = spec["min"], spec["max"]
        if not lo <= v <= hi:
            raise PlaybookError(
                f"{key} must be between {lo} and {hi}; got {v}"
                f"{spec.get('why', '')}")
        return v

    if kind == "enum":
        if isinstance(v, bool) or not isinstance(v, str):
            raise PlaybookError(
                f"{key} must be one of {', '.join(spec['values'])}; got "
                f"{type(v).__name__} ({v!r})")
        s = v.strip()
        if s in spec["values"]:
            return s
        msg = (spec.get("not_yet") or {}).get(s)
        if msg:
            raise PlaybookError(msg)
        raise PlaybookError(
            f"{key} must be one of {', '.join(spec['values'])}; got {s!r}")

    raise PlaybookError(f"no validator for setting kind {kind!r}")


def normalise(settings=None, base=None) -> dict:
    if settings is None:
        settings = {}
    for src, label in ((settings, "these settings"), (base, "the stored playbook")):
        if src is not None and not isinstance(src, dict):
            raise PlaybookError(
                f"{label} must be an object of name → value, not "
                f"{type(src).__name__}")
    out = dict(DEFAULTS)
    for src, label in ((base, "the stored playbook"), (settings, "these settings")):
        for k, v in (src or {}).items():
            if k not in SETTINGS:
                raise _unknown_key(k, label)
            out[k] = _coerce(k, v)
    return {k: out[k] for k in SETTING_ORDER}


def body_for(name: str, settings: dict) -> str:
    return json.dumps({"name": name, "settings": settings},
                      indent=1, sort_keys=True, ensure_ascii=False)


REF_PREFIX = "playbook:"
_REF = re.compile(r"playbook:([a-z0-9][a-z0-9-]*)@v(\d+)")
PLAYBOOKS_DIRNAME = "playbooks"


RECORD_ONLY = ("seed_policy", "pipeline")


def expand(rec: dict, gen=None, structured=None) -> dict:
    out_gen = dict(gen or {})
    out_str = dict(structured or {})
    applied = apply_to(rec, out_gen, out_str)
    seal = seal_fields(rec)
    if applied:
        seal = {**seal, "playbook_applied": applied}
    return {"gen": out_gen, "structured": out_str, "seal": seal}


def seal_fields(rec: dict) -> dict:
    return {"playbook": rec["label"],
            "playbook_ref": rec["ref"],
            "playbook_sha256": rec["sha256"],
            "playbook_settings": dict(rec["settings"])}


APPLIES_TO_GEN = ("model", "temperature", "max_tokens", "n", "retries")
APPLIES_TO_STRUCTURED = ("canon_words", "plan_mode", "transition_paras",
                         "craft_lever", "craft_mode", "craft_words")

GEN_KEYS = APPLIES_TO_GEN
STRUCTURED_KEYS = APPLIES_TO_STRUCTURED

APPLIED_ELSEWHERE = ("seed_policy", "pipeline",
                     "feedback_words",
                     "runbook",
                     "summarise")

COMPOSITION = {
    "summaries": ("part_summaries", list),
    "feedback": ("feedback", list),
    "style": ("style", dict),
}

PARAGRAPH_KEYS = ("target_paragraphs", "target_paragraphs_max")


def apply_to(rec: dict, gen: dict, structured: dict) -> dict:
    settings = (rec or {}).get("settings") or {}
    changed = {}
    for key, target in (*((k, gen) for k in APPLIES_TO_GEN),
                        *((k, structured) for k in APPLIES_TO_STRUCTURED)):
        if key not in settings:
            continue
        want = settings[key]
        if want is None:
            continue
        had = target.get(key)
        if had == want:
            continue
        target[key] = want
        changed[key] = [had, want]

    for axis, (field, empty) in COMPOSITION.items():
        if settings.get(axis) != "off":
            continue
        had = structured.get(field)
        if not had:
            continue
        structured[field] = empty()
        changed[axis] = [f"{len(had)} {field}", "off"]

    if settings.get("feedback") == "aggregate":
        rows = structured.get("feedback") or []
        if rows:
            out = E.feedback_for_budget(rows,
                                        settings.get("feedback_words") or 0)
            structured["feedback"] = out["kept"]
            if out["collapsed"] or out["dropped"]:
                changed["feedback"] = [
                    f"{len(rows)} feedback",
                    f"{len(out['kept'])} kept, {out['collapsed']} collapsed, "
                    f"{len(out['dropped'])} over budget"]

    paras = settings.get("paragraphs")
    if paras == "off":
        gone = [k for k in PARAGRAPH_KEYS if structured.get(k) is not None]
        for k in gone:
            structured.pop(k, None)
        if gone:
            changed["paragraphs"] = [", ".join(gone), "off"]
    elif paras == "slow" or (paras == "auto" and "max_tokens" in changed):
        lo, hi = E.paragraph_range(gen.get("max_tokens"), paras)
        had = [structured.get(k) for k in PARAGRAPH_KEYS]
        if had != [lo, hi]:
            structured["target_paragraphs"] = lo
            structured["target_paragraphs_max"] = hi
            changed["paragraphs"] = [
                f"{had[0]}-{had[1]}" if had[0] else "not set",
                f"{lo}-{hi} ({paras})"]

    return changed


class _JsonVersions(E.TextStore):

    def __init__(self, root):
        super().__init__(root)
        self.ext = "json"


class PlaybookStore:

    def __init__(self, root):
        self.root = Path(root)
        self.docs = _JsonVersions(self.root)

    def save(self, name, settings=None, corr: str = None) -> dict:
        name = check_name(name)
        slot = self._guard_slot(name)
        full = normalise(settings)
        put = self.docs.put(slot, body_for(name, full), corr=corr)
        return self._record(name, slot, put["version"], full, put["sha256"],
                            wrote=put["wrote"])

    def edit(self, name, changes, corr: str = None) -> dict:
        name = check_name(name)
        self._guard_slot(name)
        cur = self.read(name)
        if cur is None:
            raise PlaybookError(
                f"no playbook named {name!r} to edit in {self.root} — "
                f"have: {', '.join(self.names()) or '(none)'}")
        full = normalise(changes, base=cur["settings"])
        put = self.docs.put(playbook_slot(name), body_for(name, full), corr=corr)
        return self._record(name, cur["slot"], put["version"], full,
                            put["sha256"], wrote=put["wrote"])

    def slots(self) -> list:
        found = set()
        ext = self.docs.ext
        for f in self.root.glob(f"*.v*.{ext}"):
            stem = f.name[:-(len(ext) + 1)]
            slot, _, vtxt = stem.rpartition(".v")
            if slot and vtxt.isdigit():
                found.add(slot)
        return sorted(found)

    def names(self) -> list:
        return [e["name"] for e in self.list()]

    def list(self) -> list:
        out = []
        for slot in self.slots():
            vs = self.docs.versions(slot)
            cur = self._current_slot(slot)
            rec = self._read_slot(slot, cur) if cur else None
            out.append({"name": (rec or {}).get("name") or slot,
                        "slot": slot, "current": cur, "versions": vs})
        return sorted(out, key=lambda e: e["name"].lower())

    def versions(self, name) -> list:
        return self.docs.versions(playbook_slot(check_name(name)))

    def current(self, name):
        return self._current_slot(playbook_slot(check_name(name)))

    def read(self, name, version=None):
        slot = playbook_slot(check_name(name))
        v = _version(version) if version is not None else self._current_slot(slot)
        return self._read_slot(slot, v) if v else None

    def pin(self, name, version=None) -> dict:
        rec = self.read(name, version)
        if rec is None:
            at = f" at version {version}" if version is not None else ""
            raise PlaybookError(
                f"no playbook {str(name)!r}{at} in {self.root} — have: "
                f"{', '.join(self.names()) or '(none)'}")
        return rec

    def resolve(self, ref):
        m = _REF.fullmatch(str(ref or ""))
        if not m:
            return None
        return self._read_slot(m.group(1), int(m.group(2)))

    def _record(self, name, slot, version, settings, sha, wrote=None,
                drift=None) -> dict:
        rec = {"name": name, "slot": slot, "version": int(version),
               "label": f"{name}@{int(version)}",
               "ref": f"{REF_PREFIX}{slot}@v{int(version)}",
               "sha256": sha, "settings": settings}
        if wrote is not None:
            rec["wrote"] = wrote
        if drift is not None:
            rec["drift"] = drift
        return rec

    def _current_slot(self, slot):
        try:
            e = (self.docs._load().get("slots") or {}).get(slot) or {}
        except Exception:
            e = {}
        vs = self.docs.versions(slot)
        cur = e.get("current")
        if isinstance(cur, int) and cur in vs:
            return cur
        return vs[-1] if vs else None

    def _owner(self, slot):
        cur = self._current_slot(slot)
        rec = self._read_slot(slot, cur) if cur else None
        return (rec or {}).get("name")

    def _guard_slot(self, name) -> str:
        slot = playbook_slot(name)
        owner = self._owner(slot)
        if owner is not None and owner != name:
            raise PlaybookError(
                f"playbook name {name!r} collides with {owner!r}: both are "
                f"slot {slot!r}, and one slot is one history. Rename one — "
                f"two bundles sharing a slot would merge their pasts.")
        return slot

    def _read_slot(self, slot, v):
        raw = self.docs.read(slot, int(v))
        if raw is None:
            return None
        try:
            doc = json.loads(raw)
        except ValueError as e:
            raise PlaybookError(
                f"{self.docs.path(slot, int(v))} is not readable JSON: {e}") from e
        if not isinstance(doc, dict) or not isinstance(doc.get("settings"), dict):
            raise PlaybookError(
                f"{self.docs.path(slot, int(v))} is not a playbook record: it "
                f"needs a name and a settings object")
        stored = dict(doc["settings"])
        drift = {"unknown": sorted(k for k in stored if k not in SETTINGS),
                 "missing": [k for k in SETTING_ORDER if k not in stored]}
        return self._record(doc.get("name") or slot, slot, int(v), stored,
                            E.text_sha(raw), drift=drift)


def store_for(story_dir) -> PlaybookStore:
    return PlaybookStore(Path(story_dir) / PLAYBOOKS_DIRNAME)



BUILTIN_SETTINGS = {
    "Normal": {
        "model": None, "temperature": None, "max_tokens": None, "n": None,
        "seed_policy": "fixed", "canon_words": None, "plan_mode": None,
        "retries": None, "pipeline": "direct",
        "summaries": "full", "feedback": "full", "feedback_words": None,
        "style": "full", "paragraphs": "auto",
        "transition_paras": None, "summarise": "after_each",
        "runbook": DEFAULT_RUNBOOK,
    },
    "Compact": {
        "model": None, "temperature": None, "max_tokens": None, "n": None,
        "seed_policy": "fixed", "canon_words": None, "plan_mode": None,
        "retries": None, "pipeline": "direct",
        "summaries": "off", "feedback": "full", "feedback_words": None,
        "style": "full", "paragraphs": "auto",
        "transition_paras": 3, "summarise": "off",
        "runbook": DEFAULT_RUNBOOK,
    },
}

CRAFT_MODES = {
    "Craft A: exemplars": "exemplars",
    "Craft B: instruction": "instruction",
    "Craft C: both": "both",
}

CRAFT_WORDS_DEFAULT = 800

for _name, _mode in CRAFT_MODES.items():
    BUILTIN_SETTINGS[_name] = {**BUILTIN_SETTINGS["Compact"],
                               "craft_lever": None,
                               "craft_mode": _mode,
                               "craft_words": CRAFT_WORDS_DEFAULT}

BUILTIN_PREFIX = "builtin:"


def builtin_record(name: str) -> dict:
    settings = normalise(BUILTIN_SETTINGS[name])
    sha = E.text_sha(json.dumps(settings, sort_keys=True, ensure_ascii=False))
    short = sha[:8]
    return {"name": name, "slot": playbook_slot(name), "version": short,
            "label": f"{name}@{short}",
            "ref": f"{BUILTIN_PREFIX}{name}@{short}",
            "sha256": sha, "settings": settings, "builtin": True,
            "current": short, "versions": [short]}


def builtins() -> list:
    return [builtin_record(n) for n in BUILTIN_SETTINGS]


def builtin_resolve(ref) -> dict:
    s = str(ref or "").strip()
    if not s:
        return None
    if s.startswith(BUILTIN_PREFIX):
        s = s[len(BUILTIN_PREFIX):]
    name, _, ver = s.partition("@")
    if name not in BUILTIN_SETTINGS:
        return None
    rec = builtin_record(name)
    if ver and ver != rec["version"]:
        raise PlaybookError(
            f"{name}@{ver} is a built-in template from another version of the "
            f"app; this one has {rec['label']}. A run sealed to the old one "
            f"keeps its own record of what it used - it is not re-labelled.")
    return rec


def is_builtin_name(raw) -> bool:
    return str(raw or "").strip().casefold() in {
        n.casefold() for n in BUILTIN_SETTINGS}


RUNBOOKS_DIRNAME = "runbooks"


DEFAULT_RUNBOOK_TEXT = """\
# default runbook

The procedure this story's runs are conducted under. This one is a placeholder
written by the app so that every request can name a runbook from the first save
— it is not advice, and it is meant to be replaced.

What belongs here: how the card is shared, what is measured before a run is
believed, what is done with a result that looks wrong, and which comparisons
are worth the GPU.
"""


def runbook_slot(name: str) -> str:
    return E.slot_for("rb", name)


class RunbookStore:

    def __init__(self, root: Path):
        self.root = Path(root)
        self.docs = E.TextStore(self.root)

    def save(self, name, text, corr: str = None) -> dict:
        name = check_name(name)
        slot = runbook_slot(name)
        put = self.docs.put(slot, text if text is not None else "", corr=corr)
        return {"name": name, "slot": slot, "version": put["version"],
                "sha256": put["sha256"],
                "label": f"{name}@{put['version']}",
                "ref": f"runbook:{slot}@v{put['version']}",
                "wrote": put["wrote"]}

    def read(self, name, version=None) -> dict:
        slot = runbook_slot(check_name(name))
        doc = self.docs._load()
        e = (doc.get("slots") or {}).get(slot)
        if not e:
            return None
        v = _version(version) if version is not None else e.get("current")
        meta = (e.get("versions") or {}).get(str(v))
        if not meta:
            return None
        return {"name": name, "slot": slot, "version": int(v),
                "sha256": meta.get("sha256"),
                "label": f"{name}@{v}", "ref": f"runbook:{slot}@v{v}",
                "text": self.docs.path(slot, int(v)).read_text(
                    encoding="utf-8", errors="replace")}

    def pin(self, name, version=None) -> dict:
        rec = self.read(name, version)
        if rec is None and str(name) == DEFAULT_RUNBOOK and version is None:
            self.save(DEFAULT_RUNBOOK, DEFAULT_RUNBOOK_TEXT)
            rec = self.read(DEFAULT_RUNBOOK)
        if rec is None:
            at = f" at version {version}" if version is not None else ""
            raise PlaybookError(
                f"no runbook {str(name)!r}{at} in {self.root} — a run must be "
                f"able to say which procedure it was conducted under")
        return rec


def runbooks_for(story_dir) -> RunbookStore:
    return RunbookStore(Path(story_dir) / RUNBOOKS_DIRNAME)


def runbook_seal(rec: dict) -> dict:
    return {"runbook": rec["label"],
            "runbook_ref": rec["ref"],
            "runbook_sha256": rec["sha256"]}
