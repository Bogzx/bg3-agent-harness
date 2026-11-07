-- Offline test: a level's skill groups in ui.level_up, the part of test_level_up.lua it runs with dofile. It reads that
-- file's globals: check, openPage, run, page.
local j

-- 33j. live 2026-11-07, Rogue L6's rows as read: the group opens full of the page's own picks, the game calls the page
--      complete. The plan's picks land, an earlier level's stay, and both of the page's own are said to have come off,
--      the one the game took as well as the one the room stage turned off
local function rogue6(full) return { class = "Rogue", expertise = { max = 2, full = full,
    names = { "Deception", "Acrobatics", "SleightOfHand", "Stealth", "Athletics", "Perception" },
    held = { Stealth = true, Perception = true }, preset = { Deception = true, SleightOfHand = true } } } end
local function expertiseOn()
    local on = {}
    for _, it in ipairs(page.expertiseItems) do
        if it._p.Selected == true then on[#on + 1] = it._p.Name end
    end
    return table.concat(on, ",")
end
for _, full in ipairs({ "replaces", "clears" }) do
    openPage(rogue6(full))
    j = run({ uuid = "claude", plan = { expertise = { "Acrobatics", "Athletics" } } })
    check(j.status == "done" and page.finished == true and expertiseOn() == "Acrobatics,Stealth,Athletics,Perception"
          and table.concat(j.result.set.expertise_off or {}, ",") == "Deception,SleightOfHand",
          "the plan's land, held stay, all that left is said (" .. full .. ", " .. expertiseOn() .. ")")
    -- a named preset a toggle took and the room stage put back is on the page: not said
    openPage(rogue6(full))
    j = run({ uuid = "claude", plan = { expertise = { "Deception", "Acrobatics" } } })
    check(j.status == "done" and expertiseOn() == "Deception,Acrobatics,Stealth,Perception"
          and table.concat(j.result.set.expertise_off or {}, ",") == "SleightOfHand",
          "a named preset back on the page isn't said (" .. full .. ", " .. expertiseOn() .. ")")
end
-- 33k. Bard L10's shape: nothing preset, both land by their own toggles, nothing turned off
openPage({ class = "Bard", expertise = { max = 2, names = { "Performance", "Persuasion", "Insight", "Medicine" },
    held = { Performance = true, Persuasion = true } } })
j = run({ uuid = "claude", plan = { expertise = { "Medicine", "Insight" } } })
check(j.status == "done" and expertiseOn() == "Performance,Persuasion,Insight,Medicine"
      and j.result.set.expertise_off == nil, "a free group: toggled, nothing turned off")
-- 33l. a plan's pick that can't land (the toggle does nothing): not finished, and the pick is named
openPage(rogue6())
page.expertiseStuck = true
j = run({ uuid = "claude", plan = { expertise = { "Acrobatics", "Athletics" } } })
check(j.status == "failed" and not page.finished
      and tostring(j.error):find("skill Acrobatics: not on the page", 1, true),
      "a pick that doesn't land: not finished (" .. tostring(j.status) .. ", " .. tostring(j.error) .. ")")
-- 33m. a pick the group doesn't list: the same
openPage(rogue6())
j = run({ uuid = "claude", plan = { expertise = { "History" } } })
check(j.status == "failed" and not page.finished and tostring(j.error):find("skill History", 1, true),
      "a pick not offered: not finished (" .. tostring(j.status) .. ", " .. tostring(j.error) .. ")")

-- 33n. F1: an earlier level's picks ahead of the page's own in the list: only the page's own comes off
openPage({ class = "Rogue", expertise = { max = 2,
    names = { "Stealth", "Perception", "SleightOfHand", "Acrobatics", "Athletics" },
    held = { Stealth = true, Perception = true }, preset = { SleightOfHand = true, Athletics = true } } })
j = run({ uuid = "claude", plan = { expertise = { "Acrobatics", "Athletics" } } })
check(j.status == "done" and expertiseOn() == "Stealth,Perception,Acrobatics,Athletics"
      and table.concat(j.result.set.expertise_off or {}, ",") == "SleightOfHand",
      "held ahead in the list: spared (" .. expertiseOn() .. ", " .. tostring(j.error) .. ")")
-- 33o. F1: two unnamed, one lacking: one comes off, the other stays
openPage({ class = "Rogue", expertise = { max = 2, names = { "Deception", "SleightOfHand", "Acrobatics" },
    preset = { Deception = true, SleightOfHand = true } } })
j = run({ uuid = "claude", plan = { expertise = { "Acrobatics" } } })
check(j.status == "done" and expertiseOn() == "SleightOfHand,Acrobatics"
      and table.concat(j.result.set.expertise_off or {}, ",") == "Deception",
      "two unnamed, one lacking: only one off (" .. expertiseOn() .. ", " .. tostring(j.error) .. ")")
-- 33p. F1: a named pick the page already holds, ahead of an unnamed one: kept
openPage({ class = "Rogue", expertise = { max = 2, names = { "Athletics", "SleightOfHand", "Acrobatics" },
    preset = { Athletics = true, SleightOfHand = true } } })
j = run({ uuid = "claude", plan = { expertise = { "Acrobatics", "Athletics" } } })
check(j.status == "done" and expertiseOn() == "Athletics,Acrobatics"
      and table.concat(j.result.set.expertise_off or {}, ",") == "SleightOfHand",
      "a named preset ahead: kept (" .. expertiseOn() .. ", " .. tostring(j.error) .. ")")
-- 33q. F2: the tool's documented key on the same page: plan.skills lands too (its definition names only skills)
openPage(rogue6("replaces"))
j = run({ uuid = "claude", plan = { skills = { "Acrobatics", "Athletics" } } })
check(j.status == "done" and page.finished == true and expertiseOn() == "Acrobatics,Stealth,Athletics,Perception"
      and table.concat(j.result.set.expertise_off or {}, ",") == "Deception,SleightOfHand",
      "plan.skills on a full group: lands (" .. tostring(j.status) .. ", " .. expertiseOn() .. ", "
      .. tostring(j.error) .. ")")
-- 33r. F2: the group skillsStage toggled it in, the first holding the name: the class group; expertise is left alone
openPage({ class = "Bard",
    class_skills = { max = 1, names = { "Deception", "Arcana" }, preset = { Deception = true } },
    expertise = { max = 2, names = { "Arcana", "Persuasion", "Insight" },
        preset = { Persuasion = true, Insight = true } } })
j = run({ uuid = "claude", plan = { skills = { "Arcana" } } })
check(j.status == "done" and page.classItems[2]._p.Selected == true and page.classItems[1]._p.Selected == false
      and expertiseOn() == "Persuasion,Insight" and table.concat(j.result.set.class_off or {}, ",") == "Deception"
      and j.result.set.expertise_off == nil,
      "plan.skills: the first group holding the name, the other untouched (" .. expertiseOn() .. ", "
      .. tostring(j.error) .. ")")
-- 33s. Q4: a named pick the game shows disabled can't land, so it costs no unnamed pick; the read-back names it
openPage({ class = "Rogue", expertise = { max = 2, names = { "SleightOfHand", "Acrobatics", "Athletics" },
    preset = { SleightOfHand = true, Athletics = true }, disabled = { Acrobatics = true } } })
j = run({ uuid = "claude", plan = { expertise = { "Acrobatics", "Athletics" } } })
check(j.status == "failed" and not page.finished and expertiseOn() == "SleightOfHand,Athletics"
      and j.result.set.expertise_off == nil and tostring(j.error):find("skill Acrobatics", 1, true) ~= nil,
      "a disabled name: nothing turned off, named (" .. expertiseOn() .. ", " .. tostring(j.error) .. ")")

-- 33t. Rows reading Enabled nil, not seen live, held ones first. Nothing comes off, no earlier pick is lost, and the
--      named pick that can't land is refused, not finished
openPage({ class = "Rogue", expertise = { max = 2, unread = true,
    names = { "Stealth", "Perception", "SleightOfHand", "Acrobatics", "Athletics" },
    held = { Stealth = true, Perception = true }, preset = { SleightOfHand = true, Athletics = true } } })
j = run({ uuid = "claude", plan = { expertise = { "Acrobatics", "Athletics" } } })
check(j.status == "failed" and not page.finished and expertiseOn() == "Stealth,Perception,SleightOfHand,Athletics"
      and j.result.set.expertise_off == nil and tostring(j.error):find("skill Acrobatics", 1, true) ~= nil,
      "rows reading Enabled nil: nothing off, refused (" .. expertiseOn() .. ", " .. tostring(j.error) .. ")")
