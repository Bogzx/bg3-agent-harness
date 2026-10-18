"""Fight debriefs: what a fight did, from what the player saw (the mod's events + the bridge's state.combat reads).

The agent should learn from a fight that went badly: at combat end (or a reload during a fight) the bridge builds a
compact factual debrief, writes it to the session log and memory/fights.jsonl, and a reload soon after a bad fight
carries a LAST ATTEMPT line; past debriefs vs the same enemies raise one-line WARNINGs for repeated patterns.
Format and consumers: research/fight-debrief.md. Pure (no game I/O): server.py feeds it and writes the outputs.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

RETRY_WINDOW_S = 600.0     # a load_game this soon after a bad fight is a retry of it
SURROUND_M = 2.5           # a hostile this close (centre to centre) is in melee with the member
SURROUND_N = 3             # this many hostiles in melee with one member = surrounded
ELEV_M = 2.0               # enemies this much above/below the party's mean height count as high/low ground
HISTORY_MAX = 60           # past fights kept in memory for warnings
CAP_HITS = 4               # this many party hits that did 1 per damage part, at least CAP_SHARE of all hits on the
CAP_SHARE = 1 / 3          # enemy: a per-hit shield (S065 Orin's Unstoppable: 5 of 11 / 18 of 31 hits), no resisted type
UNSEEN_SHARE = 0.25        # damage from no visible source (traps, hidden attackers) this share of all taken is a fact
HEALED_PCT = 10            # an enemy seen this many HP points (%) below where it ended healed back
LOW_MIN_HITS = 3           # compared hits of a type before it can read as "did little": one is noise (S074: Gale's
                           # lone 3 Psychic on Cazador), two were S077's mid-fight WARNINGs (Piercing vs Cazador)
LOW_SHARE = 0.5            # "did little": the hits did under this share of what the same attacks did to other enemies
V = 3                      # record version: v1's "low" flags counted riders, v2's compared other attacks (see _trust)
BAD = ("costly", "near_wipe", "wipe", "reloaded")
_CONSUMABLE = re.compile(r"(?i)\b(potion|elixir|scroll|grenade|flask|bomb|oil of|arrow of)\b")
_BASIC = {"Main Hand Attack", "Ranged Attack", "Off Hand Attack", "Dash", "Disengage", "Jump", "Shove", "Help",
          "Dodge", "Hide", "Throw", "Improvised Melee Weapon"}
_ESCAPE = re.compile(r"(?i)\b(dash|disengage)\b")   # Dash, Disengage, Cunning Action: Dash, Step of the Wind: …


def _ref(d: Any) -> tuple[str | None, str | None]:
    if isinstance(d, dict):
        return d.get("uuid"), d.get("name")
    return (d, None) if isinstance(d, str) else (None, None)


class Fight:
    def __init__(self, now: float, joined_round: int | None = None):
        self.t0 = now
        self.wall0 = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.round = joined_round or 1
        self.joined = joined_round
        self.names: dict[str, str] = {}
        self.enemies: set[str] = set()                # confirmed hostile: hurt us / statused us / hostile row
        self.touched: set[str] = set()                # characters the party damaged (enemies if no hostile rows)
        self.acted: set[str] = set()                  # non-party characters that cast, or hit or statused the party
        self.affected: set[str] = set()               # non-party characters the party put a status on
        self.hurt: set[str] = set()                   # non-party characters any damage event hit (a surface too)
        self.turned: set[str] = set()                 # non-party characters that had a turn
        self.rows_seen = False
        self.race: dict[str, str] = {}
        self.killed: set[str] = set()
        self.party_seen: set[str] = set()
        self.dealt: Counter = Counter()
        self.taken: Counter = Counter()
        self.enemy_dmg: Counter = Counter()          # enemy name -> damage to the party
        self.enemy_casts: dict[str, Counter] = defaultdict(Counter)
        self.enemy_status: dict[str, Counter] = defaultdict(Counter)   # statuses enemies put on the party
        self.last_hit_by: dict[str, str] = {}
        self.downed_now: set[str] = set()
        self.max_down = 0
        self.downs: list[dict] = []
        self.died: set[str] = set()
        self.ally_died: list[dict] = []               # allied NPCs (Jaheira S043, Aylin S045) that died: {uuid, name, round}
        self.casting: dict[str, str] = {}             # party caster uuid -> current ability name
        self.rolls: dict[tuple, str] = {}             # (caster name, ability) -> "save" / "attack" (the cast event)
        self.pending: dict[str, list] = defaultdict(list)   # party source uuid -> [(target, amount)] before its cast
        self.hit_open: dict[str, list] = defaultdict(list)  # enemy uuid -> our downs/deaths its next cast explains
        self.last_hit_src: dict[str, str] = {}        # party uuid -> the enemy uuid that hit it last
        self.abil: dict[tuple, dict] = {}
        self.abil_targets: dict[tuple, set] = defaultdict(set)
        self.types: dict[tuple, list] = defaultdict(list)   # (enemy name, type) -> each party hit's main part:
        # {amt, ab: (caster name, ability) once its cast is known, kill: it killed the target}
        self.pending_hits: dict[str, list] = defaultdict(list)   # party source uuid -> its hits before its cast
        self.burst: dict[str, dict] = {}              # enemy uuid -> our hit on it in the damage run just read: a
        # death right after it marks it kill (the run ends at any event but damage/downed/died)
        self.hit: dict | None = None                  # the party hit being read: one damage event per type
        self.hit_totals: dict[str, list] = defaultdict(list)   # enemy name -> each party hit's total
        self.hit_ones: Counter = Counter()             # enemy name -> party hits that did 1 per damage part
        self.enemy_self: dict[str, Counter] = defaultdict(Counter)   # statuses on an enemy not from the party
        self.enemy_last: dict[str, tuple] = {}        # enemy uuid -> (its latest non-basic cast or None, round)
        self.last_hit_ab: dict[str, str | None] = {}  # party uuid -> that cast, for the down / death line
        self.unseen: dict[str, list] = defaultdict(list)   # type -> (target, amount): hits on us from no visible source
        self.deaths: list[dict] = []
        self.kills: list[dict] = []                   # {name, round} in kill order
        self.hp: dict[str, list] = {}                 # hostile name -> [lowest hp_pct seen, last hp_pct seen]
        self.out: dict[str, int] = {}                 # enemy uuid -> round it was seen at 0 HP and not dead: knocked
        # out by a non-lethal blow (S080 Viconia: `now neutral (was hostile), HP 65->0/86, +Knocked Out`, no died event)
        self.used: Counter = Counter()
        self.surrounded: dict | None = None
        self.elev: tuple[float, int] | None = None        # worst (enemies above us) mean height difference
        self.warned: set = set()

    def name(self, u: str | None) -> str:
        return self.names.get(u or "", u or "?")


class Tracker:
    """One fight at a time. feed() every drained event batch; observe() every state.combat read; close() at the end."""

    def __init__(self, history: list[dict] | None = None):
        self.cur: Fight | None = None
        self.history: list[dict] = [_trust(r) for r in list(history or [])[-HISTORY_MAX:]]
        self.last: dict | None = None        # the last closed debrief
        self.last_t = 0.0
        self.allies: set[str] = set()        # allied non-party characters seen on any turn order (kept across fights)
        self.dead_allies: list[dict] = []    # {uuid, name, t}: ally deaths until revived (server's ALERTS line)
        self.summons: set[str] = set()       # our summons seen on the turn order (their hits come as the owner's)

    # ---- input ----

    def feed(self, evs: list[dict], party: set | None, now: float | None = None) -> tuple[list[str], bool]:
        """Events in order. Returns (WARNING lines to show now, the fight ended in this batch)."""
        now = time.time() if now is None else now
        party = party or set()
        warns: list[str] = []
        ended = False
        for e in evs or []:
            if not isinstance(e, dict):
                continue
            k, d = e.get("kind"), e.get("data") or {}
            if k != "damage" and self.cur is not None:
                warns += self._flush_hit(self.cur)
                if k not in ("downed", "died"):
                    self.cur.burst.clear()
            if k in ("turn_started", "round_started", "combat_ended") and self.cur is not None:
                self._settle(self.cur)
            if k in ("combat_started", "round_started", "turn_started") and self.cur is None:
                if k == "turn_started" and not party:
                    continue
                r0 = int(d.get("round") or 0) if k == "round_started" else 0
                self.cur = Fight(now, r0 if r0 > 1 else None)
            if k == "revived":
                self.dead_allies = [a for a in self.dead_allies if a["uuid"] != _ref(d)[0]]
            f = self.cur
            if f is None:
                continue
            if k == "turn_started" and _ref(d)[0] not in party:
                f.turned.add(_ref(d)[0])
            if k == "round_started" and d.get("round"):
                f.round = int(d["round"])
            elif k == "combat_ended":
                ended = True
            elif k == "damage":
                warns += self._damage(f, d, party)
            elif k in ("downed", "died"):
                u, n = _ref(d)
                if not u:
                    continue
                if n:
                    f.names[u] = n
                if u in party:
                    src = f.last_hit_src.get(u)
                    if k == "died":
                        f.died.add(u)
                        f.deaths.append({"uuid": u, "name": f.name(u), "round": f.round,
                                         "by": f.last_hit_by.get(u), "ability": f.last_hit_ab.get(u)})
                        if src:
                            f.hit_open[src].append(f.deaths[-1])
                    if u not in f.downed_now:
                        f.downed_now.add(u)
                        f.max_down = max(f.max_down, len(f.downed_now))
                        first = not f.downs
                        f.downs.append({"uuid": u, "name": f.name(u), "round": f.round,
                                        "by": f.last_hit_by.get(u), "ability": f.last_hit_ab.get(u)})
                        if src:
                            f.hit_open[src].append(f.downs[-1])
                        if first:
                            warns += self._first_down_warning(f, f.name(u))
                elif k == "died" and u in self.allies:
                    # S043 Jaheira, S045 Aylin: an allied NPC's death was counted as a kill in a "clean" fight
                    f.ally_died.append({"uuid": u, "name": f.name(u), "round": f.round})
                    self.dead_allies = [a for a in self.dead_allies if a["uuid"] != u] + [
                        {"uuid": u, "name": f.name(u), "t": now}]
                elif k == "died":
                    if u in f.burst:   # a killing blow is capped by the HP left (S077: 1-HP bats)
                        f.burst[u]["kill"] = True
                    if u not in f.killed:
                        f.kills.append({"uuid": u, "name": f.name(u), "round": f.round})
                    f.killed.add(u)
            elif k == "revived":
                u, _ = _ref(d)
                f.downed_now.discard(u)
                self.dead_allies = [a for a in self.dead_allies if a["uuid"] != u]
            elif k == "cast":
                u, n = _ref(d.get("caster"))
                if not u:
                    continue
                if n:
                    f.names[u] = n
                nm = str(d.get("name") or d.get("spell") or "?")
                if u in party:
                    f.party_seen.add(u)
                    f.casting[u] = nm
                    a = f.abil.setdefault((f.name(u), nm), {"casts": 0, "hits": 0, "misses": 0, "saves": 0, "dmg": 0})
                    a["casts"] += 1
                    if d.get("roll"):
                        f.rolls[(f.name(u), nm)] = str(d["roll"])
                    f.abil_targets[(f.name(u), nm, a["casts"])] = set()
                    # The mod sends a hit's damage before its cast (S074: Gale's Scorching Ray, 4 Fire hits = 49,
                    # then the cast): the damage held since this caster's last cast is this cast's. A summon's hits
                    # come with its owner as the source (the mod sends attackerOwner first): the one other source
                    # holding damage. Not any member's: S074 booked Gale's 49 under Shadowheart's Mass Cure Wounds.
                    own = f.pending.pop(u, [])
                    hits = f.pending_hits.pop(u, [])
                    held = [s for s, v in f.pending.items() if v]
                    if not own and u in self.summons and len(held) == 1:
                        own = f.pending.pop(held[0])
                        hits = f.pending_hits.pop(held[0], [])
                    self._credit(f, u, nm, own)
                    for h in hits:
                        h["ab"] = (f.name(u), nm)
                else:
                    f.acted.add(u)
                    f.enemy_casts[f.name(u)][nm] += 1   # kept by name; only confirmed enemies are reported
                    f.enemy_last[u] = (None if nm in _BASIC else nm, f.round)
                    for x in f.hit_open.pop(u, []):     # the hit that downed us came before this cast
                        x["ability"] = f.enemy_last[u][0]
            elif k == "status_applied":
                tu, tn = _ref(d.get("target"))
                su, sn = _ref(d.get("source"))
                if tn and tu:
                    f.names[tu] = tn
                if sn and su:
                    f.names[su] = sn
                if tu in party and su and su not in party and d.get("name"):
                    f.enemies.add(su)
                    f.acted.add(su)
                    f.enemy_status[f.name(su)][str(d["name"])] += 1
                elif tu and tu not in party and su in party:
                    f.affected.add(tu)
                elif tu and tu not in party and su not in party and d.get("name"):
                    # its own buffs (S065 Orin: Slayer Form, Unstoppable: 7): named with the per-hit cap
                    f.enemy_self[f.name(tu)][re.sub(r":\s*\d+$", "", str(d["name"]))] += 1
            elif k == "used":
                cu, _ = _ref(d.get("char"))
                _, iname = _ref(d.get("item"))
                if cu in party and iname and _CONSUMABLE.search(iname):
                    f.used[iname] += 1
        if self.cur is not None:
            warns += self._flush_hit(self.cur)
        return warns, ended

    def _damage(self, f: Fight, d: dict, party: set) -> list[str]:
        tu, tn = _ref(d.get("target"))
        su, sn = _ref(d.get("source"))
        if tu and tn:
            f.names[tu] = tn
        if su and sn:
            f.names[su] = sn
        amt = d.get("amount")
        amt = int(amt) if isinstance(amt, (int, float)) else 0
        dtype = str(d.get("type") or "")
        if tu in party:
            f.party_seen.add(tu)
            f.taken[tu] += amt
            if su and su not in party:
                f.enemies.add(su)
                f.acted.add(su)
                if amt > 0:
                    f.enemy_dmg[f.name(su)] += amt
                    f.last_hit_by[tu] = f.name(su)
                    f.last_hit_src[tu] = su
                    ab, rnd = f.enemy_last.get(su) or (None, 0)
                    f.last_hit_ab[tu] = ab if rnd == f.round else None   # its cast this round, not an old one
            elif not su and amt > 0:
                # no visible source (the mod's "Someone", or none): traps, surfaces, hidden attackers. S059-S064:
                # 298 of 298, 434 of 666, 563 of 660 taken, while "most damage to us" named a minor enemy.
                f.unseen[dtype or "?"].append((tu, amt))
            return []
        if tu:
            f.hurt.add(tu)
        if not (su in party and tu):
            return []
        f.touched.add(tu)
        f.dealt[su] += amt   # a summon's hit too: the total stays with the owner
        f.pending[su].append((tu, amt))   # its ability: the cast event that follows (feed)
        # One hit = one damage event per type, back to back (Events.lua AttackedBy): a type seen again starts the
        # next hit (extra attack).
        h = f.hit
        warns: list[str] = []
        if h is None or h["key"] != (su, tu) or dtype in h["types"]:
            warns = self._flush_hit(f)
            h = f.hit = {"key": (su, tu), "types": set(), "parts": []}
        h["types"].add(dtype)
        h["parts"].append((dtype, amt))
        return warns

    def _credit(self, f: Fight, who: str, nm: str, items: list) -> None:
        """Hits (target, amount) of `who`'s cast `nm`: a hit, a made save or a miss."""
        a = f.abil.get((f.name(who), nm))
        if a is None:
            return
        for tu, amt in items:
            if amt > 0:
                a["hits"] += 1
                a["dmg"] += amt
            elif f.rolls.get((f.name(who), nm)) == "save":
                a["saves"] = a.get("saves", 0) + 1
            else:
                a["misses"] += 1
            f.abil_targets[(f.name(who), nm, a["casts"])].add(tu)

    def _settle(self, f: Fight) -> None:
        """A turn or round boundary (or the end): damage no cast claimed (a reaction, a damage-over-time tick, a
        cast sent before its hits) goes to its source's latest cast; the open downs keep the cast they had."""
        for su, items in f.pending.items():
            if items and f.casting.get(su):
                self._credit(f, su, f.casting[su], items)
        for su, hits in f.pending_hits.items():
            for h in hits if f.casting.get(su) else []:
                h["ab"] = (f.name(su), f.casting[su])
        f.pending.clear()
        f.pending_hits.clear()
        f.hit_open.clear()

    def _flush_hit(self, f: Fight) -> list[str]:
        """The hit read so far: its parts for the per-hit shield, its main part for the damage-type stats. A rider
        (S060-S062: every weapon hit carried 1-3 Radiant next to 10-15 Slashing; Gortash took 16-20 from Guiding
        Bolt and Divine Smite) says nothing about a resistance. A 0 is a miss or a made save, not a resisted type
        (S035: three saved Toll the Dead read as "Necrotic resisted")."""
        h, f.hit = f.hit, None
        if not h:
            return []
        tn = f.name(h["key"][1])
        total = sum(a for _, a in h["parts"])
        if total > 0:
            f.hit_totals[tn].append(total)
            f.hit_ones[tn] += all(a <= 1 for _, a in h["parts"])
        dtype, amt = max(h["parts"], key=lambda p: p[1])
        if dtype and amt > 0:
            rec = {"amt": amt, "ab": None, "kill": False}
            f.types[(tn, dtype)].append(rec)
            f.pending_hits[h["key"][0]].append(rec)
            f.burst[h["key"][1]] = rec
            return self._type_warning(f, tn, dtype)
        return []

    def observe(self, cs: dict, party: set | None) -> None:
        """A state.combat read: surrounded members, high/low ground."""
        f = self.cur
        if not isinstance(cs, dict):
            return
        party = party or set()
        rows = [o for o in cs.get("order") or [] if isinstance(o, dict)]
        self.summons |= {o["uuid"] for o in rows if o.get("summon") and o.get("uuid") in party}
        if f is None:
            return
        f.rows_seen = f.rows_seen or bool(rows)
        for o in rows:
            if o.get("uuid") and o.get("name"):
                f.names[o["uuid"]] = o["name"]
            if o.get("relation") == "ally" and o.get("uuid") and not o.get("summon") and o["uuid"] not in party:
                self.allies.add(o["uuid"])
            if o.get("relation") == "hostile" and o.get("uuid"):
                f.enemies.add(o["uuid"])
                if o.get("race"):
                    f.race[o["name"] if o.get("name") else o["uuid"]] = str(o["race"])
                if isinstance(o.get("hp_pct"), (int, float)) and o.get("name") and not o.get("dead"):
                    lo = f.hp.setdefault(o["name"], [o["hp_pct"], o["hp_pct"]])
                    lo[0], lo[1] = min(lo[0], o["hp_pct"]), o["hp_pct"]
            # a knocked-out enemy turns neutral: any row of a known enemy (a living one reads >= 1 %, State.hp_pct)
            u = o.get("uuid")
            if u in f.enemies and isinstance(o.get("hp_pct"), (int, float)) and not o.get("dead"):
                if o["hp_pct"] <= 0:
                    f.out.setdefault(u, f.round)
                else:
                    f.out.pop(u, None)
        hostile = [o for o in rows if o.get("relation") == "hostile" and not o.get("dead")]
        for o in rows:
            u = o.get("uuid")
            if u not in party or o.get("dead"):
                continue
            n = sum(1 for h in hostile if isinstance((h.get("from") or {}).get(u), (list, tuple))
                    and h["from"][u] and isinstance(h["from"][u][0], (int, float)) and h["from"][u][0] <= SURROUND_M)
            if n >= SURROUND_N and (f.surrounded is None or n > f.surrounded["n"]):
                f.surrounded = {"name": f.name(u), "n": n, "round": f.round}
        ys = lambda rs: [r["pos"][1] for r in rs if isinstance(r.get("pos"), (list, tuple)) and len(r["pos"]) == 3]  # noqa: E731
        py, hy = ys([o for o in rows if o.get("uuid") in party]), ys(hostile)
        if py and hy:
            dy = sum(hy) / len(hy) - sum(py) / len(py)
            if f.elev is None or abs(dy) > abs(f.elev[0]):
                f.elev = (round(dy, 1), f.round)

    # ---- warnings (repeated patterns from past debriefs) ----

    def _past(self, enemy: str) -> list[dict]:
        return [r for r in self.history if any(e.get("name") == enemy for e in r.get("enemies") or [])]

    def _first_down_warning(self, f: Fight, who: str) -> list[str]:
        names = {f.name(u) for u in f.enemies}
        for r in reversed(self.history):
            if r.get("grade") == "clean" or not names & {e.get("name") for e in r.get("enemies") or []}:
                continue
            downs = r.get("downed") or []
            if downs and downs[0].get("name") == who and ("down", who) not in f.warned:
                f.warned.add(("down", who))
                by = downs[0].get("by")
                return [f"WARNING: {who} went down first again (last time vs these enemies too: r{downs[0].get('round')}"
                        + (f", by {by}" if by else "") + "): keep them back, heal or shield them first"]
            break
        return []

    def _type_warning(self, f: Fight, enemy: str, dtype: str) -> list[str]:
        if ("type", enemy, dtype) in f.warned:
            return []
        for r in reversed(self._past(enemy)):
            for t in r.get("dmg_types") or []:
                if t.get("enemy") == enemy and t.get("type") == dtype and t.get("low"):
                    f.warned.add(("type", enemy, dtype))
                    return [f"WARNING: {dtype} did little to {enemy} last time too ({_low_why(t)}, "
                            f"{r.get('session') or 'earlier'}): use another damage type on it"]
        return []

    def last_time_rec(self, cs: dict) -> dict | None:
        """The past bad fight vs any of these enemies that first_turn_warnings cites: the latest that was going
        badly, else the latest bad one. A fight reloaded while it went clean (S061 09:53/09:56: two 2-round lure
        practices) is the latest but says little; S062's LAST TIME cited it instead of 09:37's wipe."""
        names = {o.get("name") for o in (cs or {}).get("order") or [] if isinstance(o, dict)
                 and o.get("relation") == "hostile" and o.get("name")}
        return self.informative(names)

    def informative(self, names: set, before: dict | None = None) -> dict | None:
        """The latest bad fight vs any of `names` with key facts (older than `before` when given) whose own grade
        was not clean; else the latest bad one."""
        hist = self.history
        if before is not None:
            hist = hist[:next((i for i, r in enumerate(hist) if r is before), len(hist))]
        bad = [r for r in hist if r.get("grade") in BAD and names & {e.get("name") for e in r.get("enemies") or []}
               and (r.get("key_facts") or [])]
        return next((r for r in reversed(bad) if _graded(r) != "clean"), bad[-1] if bad else None)

    def chain_start(self, rec: dict) -> str | None:
        """When `rec`'s retry chain began (its first attempt's start): chain_t, else the earliest of the reloaded
        attempts right before it in the same session with >= 2 of its enemies (a record from before chain_t: S065
        11:50 → 11:43, so try 3 also sees try 1's [lesson])."""
        if rec.get("chain_t"):
            return rec["chain_t"]
        t = rec.get("t")
        names = {e.get("name") for e in rec.get("enemies") or []}
        i = next((i for i, r in enumerate(self.history) if r is rec), None)
        for r in reversed(self.history[:i] if i is not None else []):
            if r.get("session") != rec.get("session") or not r.get("reloaded") or \
                    len(names & {e.get("name") for e in r.get("enemies") or []}) < 2:
                break
            t = r.get("t") or t
        return t

    def first_turn_warnings(self, cs: dict) -> list[str]:
        """At a fight's first state read: past debriefs vs these enemies with a bad grade → the key facts."""
        names = {o.get("name") for o in (cs or {}).get("order") or [] if isinstance(o, dict)
                 and o.get("relation") == "hostile" and o.get("name")}
        r = self.informative(names)
        if r is None:
            return []
        return [f"LAST TIME vs {', '.join(sorted(names & {e.get('name') for e in r['enemies']}))} "
                f"({_outcome(r)}, {r.get('session') or 'earlier'}{_hhmm(r)}): " + "; ".join(r["key_facts"][:FACTS_SHOWN])]

    # ---- output ----

    def close(self, party_rows: list[dict] | None = None, unused: list[str] | None = None,
              meta: dict | None = None, reloaded: bool = False, now: float | None = None) -> dict | None:
        f, self.cur = self.cur, None
        if f is None:
            return None
        self._flush_hit(f)
        self._settle(f)
        now = time.time() if now is None else now
        rec = build(f, party_rows or [], unused or [], meta or {}, reloaded, now)
        if not rec["enemies"] and not rec["downed"] and not any(p["dealt"] or p["taken"] for p in rec["party"]):
            return None   # S059: 7 "[fight] clean … vs unseen enemies | dealt 0 / taken 0" from round events alone
        self.history = (self.history + [rec])[-HISTORY_MAX:]
        self.last, self.last_t = rec, now
        return rec

    def retry_of(self, now: float | None = None) -> dict | None:
        """The fight a load_game now retries: the open one (closed as reloaded by the caller) or a bad one that
        ended within RETRY_WINDOW_S."""
        now = time.time() if now is None else now
        if self.last and self.last.get("grade") in BAD and now - self.last_t <= RETRY_WINDOW_S:
            return self.last
        return None


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


def build(f: Fight, party_rows: list[dict], unused: list[str], meta: dict, reloaded: bool, now: float) -> dict:
    rows = [r for r in party_rows if isinstance(r, dict)]
    for r in rows:
        if r.get("uuid") and r.get("name"):
            f.names[r["uuid"]] = r["name"]
    party_u = [r.get("uuid") for r in rows if r.get("uuid")] or sorted(f.party_seen)
    pnames = {f.name(u) for u in party_u}

    def foe(n: str | None) -> str | None:
        # An enemy named like a party member (the Self-Same Trial's mirror copies, S035) reads "Wyll (foe)".
        return f"{n} (foe)" if n in pnames else n
    enemies: dict[str, dict] = {}
    # A hostile that had turns but never acted, and that nothing hit and the party never statused, is no foe of
    # this fight (S074 Cazador: six Sacrificial Lamb spawn, "Temporarily Hostile", passing every turn, whose ritual
    # deaths read as our kills, "killed 3/27"): listed apart, not counted. One that had no turn yet stays a foe.
    idle = {u for u in f.enemies & f.turned if u not in f.acted | f.touched | f.affected | f.hurt}
    foes = (f.enemies | (set() if f.rows_seen else f.touched)) - idle
    for u in foes:
        n = f.name(u)
        e = enemies.setdefault(n, {"name": foe(n), "race": f.race.get(n), "count": 0, "killed": 0,
                                   "dmg_to_us": f.enemy_dmg.get(n, 0),
                                   "abilities": [a for a, _ in f.enemy_casts.get(n, Counter()).most_common()
                                                 if a not in _BASIC][:3],
                                   "statuses_on_us": [s for s, _ in f.enemy_status.get(n, Counter()).most_common(3)]})
        e["count"] += 1
        e["killed"] += u in f.killed
    party = [{"name": f.name(u), "dealt": f.dealt.get(u, 0), "taken": f.taken.get(u, 0),
              "downed": sum(1 for d in f.downs if d["uuid"] == u), "died": u in f.died} for u in party_u]
    # our summons go down too (S059/S061: "Spiritual Weapon went down first" → "protect Spiritual Weapon")
    downs = [d for d in f.downs if d["uuid"] in party_u] if rows else f.downs
    capped = {}
    for en, tot in f.hit_totals.items():
        if f.hit_ones[en] >= CAP_HITS and f.hit_ones[en] >= CAP_SHARE * len(tot):
            capped[en] = {"enemy": foe(en), "hits": len(tot), "ones": f.hit_ones[en],
                          "statuses": [s for s, _ in f.enemy_self.get(en, Counter()).most_common(3)]}
    abilities = []
    for (caster, nm), a in f.abil.items():
        tmax = max((len(t) for (c, n, _i), t in f.abil_targets.items() if c == caster and n == nm), default=0)
        abilities.append({"caster": caster, "name": nm, "casts": a["casts"], "hits": a["hits"],
                          "misses": a["misses"], "saves": a.get("saves", 0), "roll": f.rolls.get((caster, nm)),
                          "targets_max": tmax, "dmg": a["dmg"]})
    abilities.sort(key=lambda a: (-a["dmg"], -a["casts"]))
    dmg_types = []
    by_enemy: dict[str, list] = defaultdict(list)
    for (en, t), amts in f.types.items():
        by_enemy[en].append((t, amts))
    # "Did little" = the same attacks did far more to other enemies. S077 (from S076's records): `Radiant did little to
    # Predator Werewolf (7 hits, avg 11.7)` was Sacred Flame/Guiding Bolt next to Fireball's 33 (14.5 on the bats),
    # and `Piercing did little to Bat (2 hits, avg 5.0)` was killing blows on 1-HP bats. Killing blows and hits whose
    # attack has no other enemy to compare with are left out.
    seen: dict[tuple, dict] = defaultdict(lambda: defaultdict(list))   # (attack, type) -> enemy -> amounts
    for (en, t), hits in f.types.items():
        for h in hits:
            if h["ab"] and not h["kill"]:
                seen[(h["ab"], t)][en].append(h["amt"])
    for en, lst in by_enemy.items():
        for t, hits in lst:
            amts = [h["amt"] for h in hits]
            avg = round(sum(amts) / len(amts), 1) if amts else 0.0
            got, want = [], []
            for h in hits:
                elsewhere = [a for e2, am in seen.get((h["ab"], t), {}).items() if e2 != en for a in am]
                if h["ab"] and not h["kill"] and elsewhere:
                    got.append(h["amt"])
                    want.append(sum(elsewhere) / len(elsewhere))
            low = en not in capped and len(got) >= LOW_MIN_HITS and sum(got) < LOW_SHARE * sum(want)
            row = {"enemy": foe(en), "type": t, "hits": len(amts), "avg": avg, "low": bool(low)}
            if got:
                row.update(compared=len(got), got=round(sum(got) / len(got), 1),
                           expected=round(sum(want) / len(want), 1))
            dmg_types.append(row)
    max_hp = sum(int(r.get("max_hp") or 0) for r in rows)
    taken = sum(f.taken.get(u, 0) for u in party_u)   # the max HP's members: not a summon's (S078-r1 Cambion)
    n_party = max(1, len(party_u))
    # Thresholds (act owns them; docs/ARCHITECTURE.md "Learning from fights"): near_wipe = a member died, half the
    # party down at once, one member left standing, or an allied NPC died (a story ally can be gone for good);
    # costly = someone downed or ≥ 30 % of the party's max HP taken.
    if f.max_down >= n_party:
        grade = "wipe"
    elif f.died or f.ally_died or (f.max_down >= 2 and (f.max_down * 2 >= n_party or f.max_down >= n_party - 1)):
        grade = "near_wipe"
    elif f.downs or (max_hp and taken >= 0.3 * max_hp):
        grade = "costly"
    else:
        grade = "clean"
    pos: dict = {}
    if f.surrounded:
        pos["surrounded"] = f.surrounded
    if f.elev and abs(f.elev[0]) >= ELEV_M:
        pos["elev_m"] = f.elev[0]
    rec = {"v": V, "session": meta.get("session"), "t": f.wall0, "region": meta.get("region"),
           "grade": "reloaded" if reloaded else grade, "reloaded": reloaded, "rounds": f.round,
           "duration_s": int(now - f.t0), "enemies": sorted(enemies.values(), key=lambda e: (-e["count"], -e["dmg_to_us"], e["name"])),
           "party": party, "allies_died": [{"name": a["name"], "round": a["round"]} for a in f.ally_died],
           "downed": [{"name": d["name"], "round": d["round"], "by": foe(d["by"])}
                      | ({"ability": d["ability"]} if d.get("ability") else {}) for d in downs],
           "abilities": abilities[:8], "dmg_types": dmg_types,
           "consumables": {"used": sorted(f.used), "unused": list(unused)[:6]}}
    rec["deaths"] = [{"name": d["name"], "round": d["round"], "by": foe(d["by"])}
                     | ({"ability": d["ability"]} if d.get("ability") else {}) for d in f.deaths if d["uuid"] in party_u]
    rec["kills"] = [{"name": foe(k["name"]), "round": k["round"]} for k in f.kills if k["uuid"] in foes]
    out = [{"name": foe(f.name(u)), "round": r} for u, r in f.out.items() if u in foes and u not in f.killed]
    if out:
        rec["out"] = out
    # Only the hits on the members the party list shows: a summon's (S078-r1 Viconia: the Cambion's) are in neither
    # their "taken" nor this, else "no visible source 623 of 564".
    unseen = {t: [a for u, a in hits if u in party_u] for t, hits in f.unseen.items()}
    unseen = {t: a for t, a in unseen.items() if a}
    if unseen:
        rec["unseen"] = {"dmg": sum(sum(a) for a in unseen.values()),
                         "types": [{"type": t, "hits": len(a), "avg": round(sum(a) / len(a), 1)}
                                   for t, a in sorted(unseen.items(), key=lambda kv: -sum(kv[1]))][:3]}
    if capped:
        rec["dmg_cap"] = list(capped.values())
    if idle:
        died = {k["uuid"]: k["round"] for k in f.kills}
        rec["idle"] = sorted(({"name": foe(f.name(u)), "died": died.get(u)} for u in idle),
                             key=lambda x: (x["died"] is None, x["died"] or 0, x["name"]))[:8]
    idle_names = {f.name(u) for u in idle} - {f.name(u) for u in foes}
    left = [{"name": foe(n), "low_pct": lo, "pct": last} for n, (lo, last) in f.hp.items()
            if lo < 100 and n not in {f.name(u) for u in f.killed | set(f.out)} and n not in idle_names]
    if left:
        rec["left"] = sorted(left, key=lambda x: -enemies.get(x["name"], {}).get("dmg_to_us", 0))[:4]
    # setup's interface fields (docs/ARCHITECTURE.md): group = the retry key; per-enemy n/used/resisted; per-member
    # downed_by; ours; position as a list; top-level unused.
    names = sorted({e["name"].lower() for e in rec["enemies"]})
    rec["group"] = "|".join(names[:4])
    for e in rec["enemies"]:
        e["n"] = e["count"]
        e["used"] = e["abilities"]
        e["resisted"] = [t["type"] for t in dmg_types if t["enemy"] == e["name"] and t["low"]]
    for p in party:
        p["downed_by"] = next((d.get("by") for d in downs if d["name"] == p["name"]), None)
    rec["ours"] = [{"ability": a["name"], "caster": a["caster"], "casts": a["casts"], "hits": a["hits"],
                    "misses": a["misses"],
                    "saves": a["saves"] if a.get("roll") == "save" else None} for a in abilities[:8]]
    rec["pos"] = pos
    rec["position"] = (["surrounded"] if pos.get("surrounded") else []) + \
        (["enemy_high_ground"] if (pos.get("elev_m") or 0) >= ELEV_M else []) + \
        (["our_high_ground"] if (pos.get("elev_m") or 0) <= -ELEV_M else [])
    rec["unused"] = rec["consumables"]["unused"]
    if reloaded:
        rec["grade_before"] = grade
    if f.joined:
        rec["joined_round"] = f.joined
    if getattr(f, "chain_t", None):
        rec["chain_t"] = f.chain_t   # a retry: the first attempt's start (the agent's lessons count from there)
    rec["key_facts"] = key_facts(rec)
    rec["summary"] = summary(rec)
    return rec


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
