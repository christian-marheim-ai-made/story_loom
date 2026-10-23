from __future__ import annotations

import random
import time

CORR_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"

_CAND = "{kind}:{part}:{para}"

PICKABLE_KINDS = ("write_part", "replace_para", "part_plan")

CONDITIONAL_PICKABLE = {"chapter_plot": "plan_stage"}


def pickable(kind: str, target: dict) -> bool:
    if kind in PICKABLE_KINDS:
        return True
    veto = CONDITIONAL_PICKABLE.get(kind)
    return veto is not None and not (target or {}).get(veto)


class RouteError(ValueError):
    pass


def cand_key(kind: str, target: dict, corr: str = None) -> str:
    t = target or {}
    if t.get("part") is None:
        base = f"{kind}:ch:{t.get('chapter')}:"
    else:
        para = t.get("para")
        base = _CAND.format(kind=kind, part=t.get("part"),
                            para="" if para is None else para)
    return f"{base}#{corr}" if corr else base


def check(kind: str, target: dict, landing: dict) -> None:
    if not isinstance(landing, dict) or not landing:
        return
    where = landing.get("where")
    if where == "candidates" and not pickable(kind, target):
        raise RouteError(
            f"landing.where is 'candidates' but {kind!r} with this target is "
            f"not something the author picks, so nothing would ever claim the "
            f"result")
    key = landing.get("key")
    if key:
        want = cand_key(kind, target)
        got = key.split("#", 1)[0]
        if got != want:
            raise RouteError(
                f"landing.key says the result goes to {got!r} but this "
                f"request's own kind and target compute {want!r} - the answer "
                f"would land on something the author did not ask about")
    lt = landing.get("target")
    if isinstance(lt, dict) and lt and (target or {}) != lt:
        raise RouteError(
            f"landing.target {lt!r} is not this request's target "
            f"{(target or {})!r}")


def mint(kind: str, kinds: dict = None, taken=()) -> str:
    k3 = (kinds or {}).get(kind, "job")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    seen = set(taken)
    for _ in range(64):
        tail = "".join(random.choice(CORR_ALPHABET) for _ in range(4))
        corr = f"r-{stamp}-{k3}-{tail}"
        if corr not in seen:
            return corr
    raise RouteError("could not mint a distinct corr in 64 tries")


def fan(intent: dict, arms: list, expand, kinds: dict = None) -> list:
    kind = intent.get("kind") or "write_part"
    target = intent.get("target") or {}
    base_landing = dict(intent.get("landing") or {})
    check(kind, target, base_landing)

    out, taken = [], []
    for rec in arms:
        corr = mint(kind, kinds, taken)
        taken.append(corr)
        ex = expand(rec, intent.get("gen"), intent.get("structured"))
        landing = dict(base_landing)
        if landing.get("where") == "candidates":
            landing["key"] = cand_key(kind, target, corr)
        out.append({
            "corr": corr,
            "playbook": rec.get("label"),
            "playbook_ref": rec.get("ref"),
            "kind": kind, "target": target,
            "gen": ex["gen"], "structured": ex["structured"],
            "landing": landing,
            "seal": ex["seal"],
        })
    return out


def accept(subs: list, result: dict):
    corr = str(result.get("corr") or "")
    by_corr = {s["corr"]: s for s in subs}
    sub = by_corr.get(corr)
    if sub is None:
        raise RouteError(
            f"no arm of this fan minted {corr!r} - there are "
            f"{len(subs)} arms and none of them asked for this result")
    land = result.get("landing")
    if isinstance(land, dict) and land and land != sub["landing"]:
        raise RouteError(
            f"{corr} arrived with a landing that is not the one it was minted "
            f"with: {land!r} != {sub['landing']!r}")
    ref = result.get("playbook_ref")
    if ref is not None and ref != sub["playbook_ref"]:
        raise RouteError(
            f"{corr} is arm {sub['playbook_ref']!r} but the result is sealed "
            f"{ref!r} - the comparison would credit the wrong playbook")
    return sub


def fold(subs: list, results: list) -> dict:
    out = {}
    for r in results:
        sub = accept(subs, r)
        if sub["corr"] in out:
            raise RouteError(
                f"{sub['corr']} already has a result - a second one means one "
                f"of the two answers is unaccounted for")
        out[sub["corr"]] = r
    return out
