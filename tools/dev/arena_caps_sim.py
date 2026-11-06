"""arena-v0 2v2 (Fighter + Rogue L2 twins) rounds-to-wipe Monte Carlo with real dice. Sheet values from the live
smoke's get_character (Fighter AC 17 HP 20 Str 17 Longsword two-handed 1d10+3; Rogue AC 14 HP 17 Dex 17 Shortbow /
Shortsword 1d6+3, Sneak Attack 1d6), proficiency +2. Policy as both DeepSeek sides played: focus the weakest enemy,
Fighter Action Surge in round 1 (once), Lacerate once (an attack), Pommel Strike bonus once (1d4), Second Wind at <=
half HP (bonus, 1d10+2, once), potions 2 each (bonus, 2d4+2) at <= 40% HP when the bonus is free; the Rogue
sneak-attacks when its Fighter is up (ally next to the target) or it hid last turn (Hide uses its bonus action). A
downed member is out (variant: revive)."""

import random
import statistics


def d(n, s, r):
    return sum(r.randint(1, s) for _ in range(n))


def attack(r, bonus, ac, dice, mod, adv=False, extra=None):
    a = r.randint(1, 20)
    if adv:
        a = max(a, r.randint(1, 20))
    if a == 1:
        return 0
    crit = a == 20
    if not crit and a + bonus < ac:
        return 0
    n, s = dice
    dmg = d(n * (2 if crit else 1), s, r) + mod
    if extra:
        dmg += d(extra[0] * (2 if crit else 1), extra[1], r)
    return dmg


def match(r, revive=False):
    units = []
    for side in "AB":
        units.append(
            {
                "side": side,
                "cls": "F",
                "hp": 20,
                "mx": 20,
                "ac": 17,
                "init": r.randint(1, 20) + 1,
                "surge": 1,
                "lac": 1,
                "pom": 1,
                "wind": 1,
                "pot": 2,
            }
        )
        units.append(
            {
                "side": side,
                "cls": "R",
                "hp": 17,
                "mx": 17,
                "ac": 14,
                "init": r.randint(1, 20) + 3,
                "hid": False,
                "pot": 2,
            }
        )
    order = sorted(units, key=lambda u: (-u["init"], r.random()))
    for rnd in range(1, 41):
        for u in order:
            if u["hp"] <= 0:
                continue
            foes = [f for f in units if f["side"] != u["side"] and f["hp"] > 0]
            if not foes:
                break
            mates = [m for m in units if m["side"] == u["side"] and m is not u]
            bonus_free = True
            if u["hp"] <= 0.4 * u["mx"] and u["pot"] > 0:
                u["pot"] -= 1
                u["hp"] = min(u["mx"], u["hp"] + d(2, 4, r) + 2)
                bonus_free = False
            if revive:
                down = [m for m in mates if m["hp"] <= 0]
                if down and r.random() < 0.3:  # spends its action on Help (as m1's Cedar did)
                    down[0]["hp"] = 1
                    continue
            t = min(foes, key=lambda f: f["hp"])
            if u["cls"] == "F":
                if bonus_free and u["wind"] and u["hp"] <= u["mx"] / 2:
                    u["wind"] = 0
                    u["hp"] = min(u["mx"], u["hp"] + d(1, 10, r) + 2)
                    bonus_free = False
                n = 1 + u.pop("surge", 0)  # Action Surge: its one use on the first turn
                u["surge"] = 0
                for _ in range(n):
                    t = min([f for f in units if f["side"] != u["side"] and f["hp"] > 0] or [t], key=lambda f: f["hp"])
                    if t["hp"] <= 0:
                        break
                    t["hp"] -= attack(r, 5, t["ac"], (1, 10), 3)
                if bonus_free and u["pom"] and t["hp"] > 0:
                    u["pom"] = 0
                    t["hp"] -= attack(r, 5, t["ac"], (1, 4), 3)
            else:
                fighter_up = any(m["cls"] == "F" and m["hp"] > 0 for m in mates)
                sa = fighter_up or u["hid"]
                t["hp"] -= attack(r, 5, t["ac"], (1, 6), 3, adv=u["hid"], extra=(1, 6) if sa else None)
                u["hid"] = bonus_free  # Cunning Action: Hide with the bonus action
        for s in "AB":
            if all(x["hp"] <= 0 for x in units if x["side"] == s):
                return rnd
    return 41


def run(n, revive):
    r = random.Random(2026)
    res = [match(r, revive) for _ in range(n)]
    q = statistics.quantiles(res, n=20)
    return res, statistics.mean(res), statistics.median(res), q[1], q[-2], max(res)


for revive in (False, True):
    res, mean, med, p10, p95, mx = run(20000, revive)

    def share(k, res=res):
        return sum(1 for x in res if x > k) / len(res)

    print(
        f"revive={revive}: mean {mean:.2f} median {med} p10 {p10} p95 {p95} max {mx}; >4 {share(4):.3f} "
        f">6 {share(6):.3f} >8 {share(8):.4f} >10 {share(10):.4f} >14 {share(14):.5f} >20 {share(20):.5f}"
    )
    hist = {k: sum(1 for x in res if x == k) / len(res) for k in range(1, 12)}
    print("   dist", {k: round(v, 3) for k, v in hist.items()})
