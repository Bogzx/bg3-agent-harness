"""Fight debrief text: key facts, the summary and the full debrief, the RETRY / LAST TIME line, fight notes and
the fights.jsonl history. Pure. debrief.py builds the records and re-exports these names."""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

ELEV_M = 2.0               # enemies this much above/below the party's mean height count as high/low ground
HISTORY_MAX = 60           # past fights kept in memory for warnings
UNSEEN_SHARE = 0.25        # damage from no visible source (traps, hidden attackers) this share of all taken is a fact
HEALED_PCT = 10            # an enemy seen this many HP points (%) below where it ended healed back
LOW_MIN_HITS = 3           # compared hits of a type before it can read as "did little": one is noise (S074: Gale's
                           # lone 3 Psychic on Cazador), two were S077's mid-fight WARNINGs (Piercing vs Cazador)
V = 3                      # record version: v1's "low" flags counted riders, v2's compared other attacks (see _trust)
_ESCAPE = re.compile(r"(?i)\b(dash|disengage)\b")   # Dash, Disengage, Cunning Action: Dash, Step of the Wind: …


FACTS_SHOWN = 4   # key facts on the RETRY / LAST TIME line (the 4th was cut before; S065's 4th was the real one)


def _graded(r: dict) -> str:
    """The fight's own grade (a reload's grade_before)."""
    return str(r.get("grade_before") or r.get("grade") or "")


def _outcome(r: dict) -> str:
    """"wipe", or "reloaded, was costly" (how it went before the reload)."""
    g = str(r.get("grade") or "")
    return f"{g}, was {r['grade_before']}" if r.get("grade_before") and r["grade_before"] != g else g


def _hhmm(r: dict) -> str:
    m = re.search(r"T(\d{2}:\d{2})", str(r.get("t") or ""))
    return f" {m.group(1)}" if m else ""


def _trust(r: dict) -> dict:
    """A v1 record (before 2026-10-17) counted a hit's rider as its own damage type: of ~80 "did little" types in
    claude-run-2 S035-S065 nearly all were a 1-3 Radiant rider beside a weapon's 10-15 (avg 2.0), or Orin's
    Unstoppable (every type 1). A v2 record (to 2026-10-18) compared a type with the other attacks' damage and
    counted killing blows (S077's WARNINGs from S076: Radiant 11.7/hit vs werewolves, Piercing on 1-HP bats). Their
    low flags and the facts built from them are dropped, not trusted; so are flags from fewer than LOW_MIN_HITS hits."""
    if not isinstance(r, dict):
        return r
    old = int(r.get("v") or 1) < V
    bad = {(t.get("enemy"), t.get("type")) for t in r.get("dmg_types") or []
           if t.get("low") and (old or int(t.get("hits") or 0) < LOW_MIN_HITS)}
    if not bad:
        return r
    r = dict(r, dmg_types=[dict(t, low=False) if (t.get("enemy"), t.get("type")) in bad else t
                           for t in r.get("dmg_types") or []],
             enemies=[dict(e, resisted=[x for x in e.get("resisted") or [] if (e.get("name"), x) not in bad])
                      for e in r.get("enemies") or []])
    r["key_facts"] = [x for x in r.get("key_facts") or []
                      if not any(x.startswith(f"{t} did little to {e} (") for e, t in bad)]
    r["summary"] = summary(r)
    return r




REVIVE_HINT = ("revive now: Scroll of Revivify / Revivify on the body; Withers (camp) raises only party members; "
               "or reload before the fight")


def _enemy_list(rec: dict, n: int = 4) -> str:
    es = rec.get("enemies") or []
    out = ", ".join(e["name"] + (f" x{e['count']}" if e["count"] > 1 else "") for e in es[:n])
    return out + (f" +{len(es) - n} more" if len(es) > n else "") if out else "unseen enemies"


def _hits(n: int) -> str:
    return f"{n} hit" + ("" if n == 1 else "s")


def _low_why(t: dict) -> str:
    """'3 hits, avg 4.0 vs 9.0 from the same attacks on other enemies' (v3), else the v2 shape."""
    if t.get("expected") is not None:
        return (f"{_hits(t.get('compared') or 0)}, avg {t.get('got')} vs {t['expected']} from the same attacks "
                "on other enemies")
    return f"{_hits(t.get('hits') or 0)}, avg {t.get('avg')}"


def _rounds(rec: dict) -> str:
    n = rec.get("rounds") or 1
    return f"{n} round" + ("" if n == 1 else "s")


def _by(d: dict) -> str:
    """", by Orin: Slay" — the enemy and the cast that hit last."""
    if not d.get("by"):
        return ""
    return f", by {d['by']}" + (f": {d['ability']}" if d.get("ability") else "")


def _unseen_top(rec: dict) -> dict | None:
    """The no-visible-source damage when it is a fact: >= UNSEEN_SHARE of all taken and more than any enemy did."""
    u = rec.get("unseen") or {}
    taken = sum(p.get("taken", 0) for p in rec.get("party") or [])
    top = max((e.get("dmg_to_us", 0) for e in rec.get("enemies") or []), default=0)
    return u if u.get("dmg") and taken and u["dmg"] >= UNSEEN_SHARE * taken and u["dmg"] > top else None


def _left(rec: dict, name: str) -> str:
    """", left at 33% (was down to 21%)" for an enemy that survived hurt."""
    x = next((x for x in rec.get("left") or [] if x.get("name") == name), None)
    if not x:
        return ""
    healed = x["pct"] - x["low_pct"] >= HEALED_PCT
    return f", left at {x['pct']}%" + (f" (was down to {x['low_pct']}%)" if healed else "")


def _gone(rec: dict) -> dict[str, str]:
    """Enemy name -> "killed r4" / "knocked out r2" when the record shows every one of that name down (S080: LAST
    ATTEMPT said `focus Viconia DeVir first` after she died r4; the kill order cut her off at 4 kills)."""
    when = {k["name"]: f"killed r{k['round']}" for k in rec.get("kills") or []}
    when |= {o["name"]: f"knocked out r{o['round']}" for o in rec.get("out") or []}
    n_out = Counter(o["name"] for o in rec.get("out") or [])
    return {e["name"]: when.get(e["name"], "killed") for e in rec.get("enemies") or []
            if e.get("count") and e.get("killed", 0) + n_out[e["name"]] >= e["count"]}


def fled(rec: dict) -> bool:
    """The party ran instead of fighting: every ability it used was a Dash/Disengage, it dealt no damage and killed
    nothing (S069 20:25 Sorcerous Sundries: the shop's guards turned hostile, all four Dashed for 3 rounds toward the
    door, reloaded mid-escape; its LAST ATTEMPT said `focus Water Elemental first`). Read from the record, so a
    stored one qualifies too."""
    ours = [a for a in rec.get("abilities") or [] if isinstance(a, dict)]
    return (bool(ours) and all(_ESCAPE.search(str(a.get("name") or "")) for a in ours)
            and not any(p.get("dealt") for p in rec.get("party") or [])
            and not any(e.get("killed") for e in rec.get("enemies") or []))


def _fled_facts(rec: dict) -> list[str]:
    """What a retreat measured: how it went, what hit it hardest, who took most (no kill order: nothing was fought)."""
    ways = sorted({a["name"] for a in rec.get("abilities") or [] if isinstance(a, dict) and a.get("name")})
    total = sum(e.get("count", 0) for e in rec.get("enemies") or [])
    facts = [f"the party fled ({' + '.join(ways)} only, {_rounds(rec)}, dealt 0, killed 0/{total})"]
    hard = max(rec.get("enemies") or [], key=lambda e: e.get("dmg_to_us", 0), default=None)
    unseen = _unseen_top(rec)
    if unseen:
        facts.append(f"hit hardest while fleeing: no visible source {unseen['dmg']} (traps, surfaces or unseen attackers)")
    elif hard and hard.get("dmg_to_us"):
        ab = f" ({', '.join(hard['abilities'][:2])})" if hard.get("abilities") else ""
        facts.append(f"hit hardest while fleeing: {hard['name']}{ab} {hard['dmg_to_us']}")
    pos = rec.get("pos") or {}
    if pos.get("surrounded"):
        s = pos["surrounded"]
        facts.append(f"{s['name']} surrounded by {s['n']} (r{s['round']})")
    slow = max(rec.get("party") or [], key=lambda p: p.get("taken", 0), default=None)
    if slow and slow.get("taken"):
        facts.append(f"took most: {slow['name']} {slow['taken']}")
    return facts


def key_facts(rec: dict) -> list[str]:
    facts = []
    deaths = rec.get("deaths")
    dead = [p["name"] for p in rec.get("party") or [] if p.get("died")]
    if deaths:
        facts.append("DIED: " + ", ".join(f"{d['name']} (r{d['round']}{_by(d)})" for d in deaths))
    elif dead:
        facts.append("DIED: " + ", ".join(dead))
    if rec.get("allies_died"):
        facts.append("ally DIED: " + ", ".join(f"{a['name']} (r{a['round']})" for a in rec["allies_died"]))
    downs = rec.get("downed") or []
    if downs and (downs[0]["name"], downs[0]["round"]) not in {(d["name"], d["round"]) for d in deaths or []}:
        d = downs[0]   # (a first down that died that round is the DIED fact already)
        facts.append(f"{d['name']} went down first (r{d['round']}{_by(d)})")
    if fled(rec):
        return (facts + _fled_facts(rec))[:4]
    hard = max(rec.get("enemies") or [], key=lambda e: e.get("dmg_to_us", 0), default=None)
    unseen = _unseen_top(rec)
    taken = sum(p.get("taken", 0) for p in rec.get("party") or [])
    if unseen:
        facts.append(f"most damage to us: no visible source {unseen['dmg']} of {taken} ("
                     + ", ".join(f"{t['type']} {_hits(t['hits'])} avg {t['avg']}" for t in unseen["types"])
                     + "): traps, surfaces or unseen attackers")
    elif hard and hard.get("dmg_to_us"):
        ab = f" ({', '.join(hard['abilities'][:2])})" if hard.get("abilities") else ""
        gone = _gone(rec).get(hard["name"])
        facts.append(f"most damage to us: {hard['name']}{ab} {hard['dmg_to_us']}"
                     + (f", {gone}" if gone else _left(rec, hard["name"])))
    for c in rec.get("dmg_cap") or []:
        on = f"; on it: {', '.join(c['statuses'])}" if c.get("statuses") else ""
        facts.append(f"{c['ones']} of {c['hits']} hits on {c['enemy']} did 1 per damage type (any type{on})")
        break
    else:
        for t in rec.get("dmg_types") or []:
            if t.get("low"):
                facts.append(f"{t['type']} did little to {t['enemy']} ({_low_why(t)})")
                break
    pos = rec.get("pos") or {}
    if pos.get("surrounded"):
        s = pos["surrounded"]
        facts.append(f"{s['name']} surrounded by {s['n']} (r{s['round']})")
    elif (pos.get("elev_m") or 0) >= ELEV_M:
        facts.append(f"enemies held high ground (+{pos['elev_m']}m)")
    unused = (rec.get("consumables") or {}).get("unused") or []
    if unused and rec.get("grade") != "clean":
        facts.append("unused: " + ", ".join(unused[:3]))
    return facts[:4]


def summary(rec: dict) -> str:
    g = rec.get("grade")
    head = f"{g}" + (f" (was {rec['grade_before']})" if rec.get("grade_before") else "") + (
        " after fleeing" if fled(rec) else "")
    return (f"{head} vs {_enemy_list(rec)} ({_rounds(rec)})"
            + (": " + "; ".join(rec.get("key_facts") or []) if rec.get("key_facts") else ""))


def render(rec: dict) -> str:
    """The FIGHT DEBRIEF block for the tool result (≤ 8 lines)."""
    lines = [f"FIGHT DEBRIEF ({rec['grade']}{', fled' if fled(rec) else ''}, {_rounds(rec)}, {rec['duration_s'] // 60}m{rec['duration_s'] % 60:02d}s)"
             f" vs {_enemy_list(rec, 6)}"]
    es = [e for e in rec.get("enemies") or [] if e.get("abilities") or e.get("statuses_on_us")]
    if es:
        lines.append("  enemies used: " + "; ".join(
            f"{e['name']}: " + ", ".join(e.get("abilities", []) + [f"{s} on us" for s in e.get("statuses_on_us", [])])
            for e in es[:3]))
    killed = sum(e["killed"] for e in rec.get("enemies") or [])
    total = sum(e["count"] for e in rec.get("enemies") or [])
    ps = rec.get("party") or []
    gone = total - killed - len(rec.get("out") or []) if rec.get("grade") in ("clean", "costly", "near_wipe") else 0
    kills = Counter((k["name"], k["round"]) for k in rec.get("kills") or [])   # in kill order; "Bat x3 r2"
    order = (" (" + ", ".join(n + (f" x{c}" if c > 1 else "") + f" r{r}" for (n, r), c in kills.items())
             + (", …" if sum(kills.values()) < killed else "") + ")") if kills else ""   # (older records kept 12)
    outs = rec.get("out") or []
    left = rec.get("left") or []
    unseen = (rec.get("unseen") or {}).get("dmg")
    lines.append(f"  killed {killed}/{total}" + order
                 + (" + knocked out " + ", ".join(f"{o['name']} r{o['round']}" for o in outs) if outs else "")
                 + (f" ({gone} gone at the end)" if gone else "")
                 + (" · left: " + ", ".join(f"{x['name']} {x['pct']}% (lowest {x['low_pct']}%)" for x in left[:3])
                    if left else "")
                 + " · dealt " + ", ".join(f"{p['name']} {p['dealt']}" for p in ps)
                 + " · took " + ", ".join(f"{p['name']} {p['taken']}" for p in ps)
                 + (f" (from no visible source: {unseen})" if unseen else ""))
    dead = [p["name"] for p in ps if p.get("died")]
    if rec.get("deaths"):
        dead = [f"{d['name']} (r{d['round']}{_by(d)})" for d in rec["deaths"]]
    if dead or rec.get("allies_died"):
        # first after the header: a death is the one thing the next fight must not repeat (S043 Jaheira: "clean")
        lines.insert(1, "  DIED: " + ", ".join(dead + [f"{a['name']} (ally, r{a['round']})" for a in rec.get("allies_died") or []])
                     + " — " + REVIVE_HINT)
    if rec.get("downed"):
        lines.append("  downed: " + ", ".join(f"{d['name']} r{d['round']}" + (f" by {d['by']}" if d.get("by") else "")
                                             + (f" ({d['ability']})" if d.get("ability") else "") for d in rec["downed"]))
    ab = [a for a in rec.get("abilities") or [] if a["hits"] or a["misses"] or a.get("saves") or a["targets_max"] > 1][:5]
    if ab:
        lines.append("  ours: " + "; ".join(
            f"{a['caster']} {a['name']} x{a['casts']}: {a['hits']} hit/"
            + (f"{a['saves']} saved" if a.get("roll") == "save" else f"{a['misses']} miss")
            + (f", up to {a['targets_max']} targets" if a["targets_max"] > 1 else "") + f", {a['dmg']} dmg"
            for a in ab))
    low = [f"{c['ones']} of {c['hits']} hits did 1 per type vs {c['enemy']} (any type"
           + (f"; on it: {', '.join(c['statuses'])}" if c.get("statuses") else "") + ")"
           for c in rec.get("dmg_cap") or []][:2]
    low += [f"{t['type']} vs {t['enemy']} ({_low_why(t)})"
            for t in rec.get("dmg_types") or [] if t.get("low")][:3 - len(low)]
    if low:
        lines.append("  low damage: " + "; ".join(low))
    pos, cons = rec.get("pos") or {}, rec.get("consumables") or {}
    bits = []
    if pos.get("surrounded"):
        s = pos["surrounded"]
        bits.append(f"{s['name']} surrounded by {s['n']} r{s['round']}")
    if pos.get("elev_m"):
        bits.append(("enemies above us" if pos["elev_m"] > 0 else "we held high ground") + f" ({pos['elev_m']:+}m)")
    idle = rec.get("idle") or []
    if idle:
        bits.append("hostile but idle, not counted: " + ", ".join(
            x["name"] + (f" (died r{x['died']})" if x.get("died") else "") for x in idle[:4])
            + (f" +{len(idle) - 4}" if len(idle) > 4 else ""))
    if cons.get("used"):
        bits.append("used " + ", ".join(cons["used"]))
    if cons.get("unused"):
        bits.append("carried, unused: " + ", ".join(cons["unused"][:4]))
    if bits:
        lines.append("  " + " · ".join(bits))
    return "\n".join(lines[:8])


def thin(rec: dict) -> str | None:
    """Why an attempt measured too little to plan from: one member fought alone (S066 19:12: Claude pulled into
    Orin's arena, the other three dealt and took 0), or it lasted 1 round (S066 19:25/19:27/19:30 tests)."""
    ps = [p for p in rec.get("party") or [] if isinstance(p, dict)]
    on = [p.get("name") for p in ps if p.get("dealt") or p.get("taken")]
    if len(ps) >= 2 and len(on) <= 1:
        return f"{on[0]} fought alone" if on else "nobody in the party fought"
    if (rec.get("rounds") or 1) <= 1:
        return "it lasted 1 round"
    return None


def _foes(r: dict) -> set:
    return {e.get("name") for e in r.get("enemies") or [] if e.get("name")}


def fuller(rec: dict, history: list[dict]) -> dict | None:
    """The latest attempt in history vs the same enemies (>= 2 of them, or all of a smaller fight) that is not
    thin() and has key facts: what a thin attempt's LAST ATTEMPT plans from. Not a flight (fled): it fought nothing."""
    names = _foes(rec)
    need = min(2, len(names))
    return next((r for r in reversed(history) if r is not rec and need and len(names & _foes(r)) >= need
                 and not thin(r) and not fled(r) and r.get("key_facts")), None)


def _fled_tips(rec: dict) -> list[str]:
    """A flight's plan: don't set the fight off again; if it starts, get out cleaner (no kill order)."""
    tips = ["avoid it: don't repeat what started it (or be gone before it starts)"]
    hard = max(rec.get("enemies") or [], key=lambda e: e.get("dmg_to_us", 0), default=None)
    if hard and hard.get("dmg_to_us") and not _unseen_top(rec):
        tips.append(f"if it starts again, leave by a way out of {hard['name']}'s reach")
    if (rec.get("pos") or {}).get("surrounded"):
        tips.append("Disengage first when in melee")
    unused = (rec.get("consumables") or {}).get("unused") or []
    if unused:
        scroll = any(re.search(r"(?i)\bscroll\b", x) for x in unused)
        tips.append("an unused scroll/potion may cover the escape"
                    + (" (in combat a scroll costs its spell's action)" if scroll else ""))
    return tips


def capped_enemies(rec: dict, history: list[dict] | None = None) -> set:
    """Enemies a per-hit cap (dmg_cap) was measured on in this record or any record in history: the cap is the
    enemy's own (Orin's Unstoppable), so one measure holds for every attempt (S066: 19:17 measured it, the solo
    19:12 try and the 1-round tests could not)."""
    return {c["enemy"] for r in [rec, *(history or [])] for c in r.get("dmg_cap") or [] if c.get("enemy")}


def last_attempt(rec: dict, history: list[dict] | None = None) -> str:
    if fled(rec):
        return (f"LAST ATTEMPT: {rec.get('summary')} — an escape, not a fight: plan the way out or avoid it: "
                + "; ".join(_fled_tips(rec)[:4]))
    why = thin(rec)
    src = fuller(rec, history or []) if why else None
    base = src or rec
    tips = []
    if rec.get("allies_died"):
        tips.append(f"keep {rec['allies_died'][0]['name']} alive (heal/shield first, pull the enemies off them)")
    downs = base.get("downed") or []
    if downs:
        tips.append(f"protect {downs[0]['name']} (pre-buff, keep back, heal first)")
    hard = max(base.get("enemies") or [], key=lambda e: e.get("dmg_to_us", 0), default=None)
    capped = capped_enemies(rec, history)
    if _unseen_top(base):
        tips.append("find what hits you unseen (traps, hidden attackers) or fight out of its reach")
    elif hard and hard.get("dmg_to_us") and hard["name"] in capped:
        tips.append(f"{hard['name']} often took 1 per hit: many small hits, or end what shields it first")
    elif hard and hard.get("dmg_to_us"):
        down = _gone(base)
        if hard["name"] not in down:
            tips.append(f"focus {hard['name']} first")
        else:   # never a focus on an enemy the record shows down: the hardest hitter still standing
            up = max((e for e in base.get("enemies") or [] if e["name"] not in down), default=None,
                     key=lambda e: e.get("dmg_to_us", 0))
            if up and up.get("dmg_to_us"):
                tips.append(f"after {hard['name']} ({down[hard['name']]}), focus {up['name']} first")
    if any(t.get("low") for t in base.get("dmg_types") or []):
        tips.append("switch damage types")
    unused = (rec.get("consumables") or {}).get("unused") or []
    if unused:
        # BG3 reads a scroll in combat for the spell's action cost (our tools' S061 "not available during combat"
        # refusal was a harness gap, act A7), so no "out of combat only" here
        scroll = any(re.search(r"(?i)\bscroll\b", x) for x in unused)
        tips.append("use the unused scroll/potion" + (" (in combat a scroll costs its spell's action)" if scroll else ""))
    pos = base.get("pos") or {}
    if pos.get("surrounded") or (pos.get("elev_m") or 0) >= ELEV_M:
        tips.append("open from range / high ground, fight at a chokepoint")
    tips = tips or ["pre-buff, open from range or high ground, focus one target"]
    thin_note = ""
    if why:
        thin_note = f" — {why}, so it measured little" + (
            f"; the fuller attempt vs them ({_outcome(src)}, {src.get('session') or 'earlier'}{_hhmm(src)}): "
            + "; ".join((src.get("key_facts") or [])[:3]) if src else "")
    return f"LAST ATTEMPT: {rec.get('summary')}{thin_note} — plan a different approach: " + "; ".join(tips[:4])


def fight_note(rec: dict) -> str:
    """The [fight] line for the session log: one line, fixed '|' fields (docs/ARCHITECTURE.md):
    <grade> @<region> vs <Enemy xN (race)>, … | rounds N | downed: <M> by <E> … | dealt A / taken B
    | resisted: <E>:<Type> … | unused: <item> … | <short summary>"""
    es = ", ".join(e["name"] + (f" x{e['count']}" if e["count"] > 1 else "") + (f" ({e['race']})" if e.get("race") else "")
                   for e in rec.get("enemies") or []) or "unseen enemies"
    downs = " ".join(f"{d['name']} by {d.get('by') or '?'}" for d in rec.get("downed") or []) or "none"
    dealt = sum(p.get("dealt", 0) for p in rec.get("party") or [])
    taken = sum(p.get("taken", 0) for p in rec.get("party") or [])
    res = " ".join(f"{t['enemy']}:{t['type']}" for t in rec.get("dmg_types") or [] if t.get("low")) or "none"
    unused = ", ".join(rec.get("unused") or []) or "none"
    facts = "; ".join(rec.get("key_facts") or [])[:200]
    return (f"{rec.get('grade')} @{rec.get('region') or '?'} vs {es} | rounds {rec.get('rounds')} | downed: {downs}"
            f" | dealt {dealt} / taken {taken} | resisted: {res} | unused: {unused} | {facts or 'no notable facts'}")


def load_history(path: Path) -> list[dict]:
    try:
        lines = path.read_text().splitlines()[-HISTORY_MAX:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def append(path: Path, rec: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass
