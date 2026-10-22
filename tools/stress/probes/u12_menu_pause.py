"""U12 does the ESC game menu pause the world (live1 §1, L1)? A companion walks 25 m and Bless ticks on another;
samples before, under the menu (ui.key ESCAPE) and after closing it with the menu's own Resume button
(ui.press {button: Resume, widget: GameMenu}). live1 and live5: the world runs on under the menu (FAIL, game
behaviour; think-pause through the menu is not viable). PASS would mean it paused. A Resume press that fails is a
FAIL of the mod (live5: "parameter is a string (CloseGameMenu)")."""

from __future__ import annotations

import json

from probelib import Probe, Verdict, main

SETUP = """local w=%s local b=%s
local g=Ext.Entity.Get(w).Transform.Transform.Translate
Osi.CharacterMoveToPosition(w, g[1]-25, g[2], g[3]+2, "Walk", "", 0)
Osi.ApplyStatus(b, "BLESS", 120, 1)
return 'ok'"""
SAMPLE = """local g=Ext.Entity.Get(%s).Transform.Transform.Translate
return Ext.Json.Stringify({gt=Ext.Utils.GameTime(), x=g[1], z=g[3], bless=Osi.GetStatusCurrentLifetime(%s, "BLESS")})"""
MENU_S = 10.0


def judge(before: dict, under: dict, resume: str, closed: str | None) -> Verdict:
    if resume:
        return "FAIL", f"Resume on the game menu: {resume}"
    if closed != "game":
        return "FAIL", f"the menu didn't close with Resume (screen {closed})"
    gt = under["gt"] - before["gt"]
    moved = ((under["x"] - before["x"]) ** 2 + (under["z"] - before["z"]) ** 2) ** 0.5
    bless = (before.get("bless") or 0) - (under.get("bless") or 0)
    what = f"under the menu for {MENU_S:.0f} s: GameTime +{gt:.1f} s, walker moved {moved:.1f} m, Bless -{bless:.1f} s"
    if gt > 1.0 or moved > 1.0 or bless > 1.0:
        return "FAIL", f"the world runs under the ESC menu (game behaviour, live1 §1): {what}; Resume closed it"
    return "PASS", f"the world paused under the menu: {what}; Resume closed it"


def run(p: Probe) -> Verdict:
    party = [m["uuid"] for m in p.game.rpc("state.status").get("party") or [] if isinstance(m, dict)]
    if len(party) < 2:
        return "FAIL", "needs two party members (a walker and a Bless target)"
    walker, blessed = json.dumps(party[1]), json.dumps(party[-1] if len(party) > 2 else party[0])
    p.log(p.lua(SETUP % (walker, blessed)))
    p.sleep(1.0)
    p.game.rpc("ui.key", {"key": "ESCAPE"}, context="client")
    if p.wait_screen("game_menu") != "game_menu":
        return "FAIL", "ESCAPE didn't open the game menu"
    before = json.loads(p.lua(SAMPLE % (walker, blessed)))
    p.sleep(MENU_S)
    under = json.loads(p.lua(SAMPLE % (walker, blessed)))
    try:
        p.game.rpc("ui.press", {"button": "Resume", "widget": "GameMenu"}, context="client")
        resume = ""
    except Exception as e:
        resume = str(e)
    closed = p.wait_screen("game", 5.0)
    p.log(json.dumps({"before": before, "under": under, "resume": resume, "closed": closed}))
    return judge(before, under, resume, closed)


if __name__ == "__main__":
    raise SystemExit(main(run))
