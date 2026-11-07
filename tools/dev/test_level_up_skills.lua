-- Offline test: a level's skill groups in ui.level_up, the part of test_level_up.lua it runs with dofile. It reads that
-- file's globals: check, openPage, run, page.
local j

-- The rows as read live (2026-11-07, Rogue L6, Bard L3 and L10): 18 in every group, and a level's class and race groups
-- at max 0 with all 18 disabled, unless the level grants picks.
local NAMES = { "Deception", "Intimidation", "Performance", "Persuasion", "Acrobatics", "SleightOfHand", "Stealth",
    "Arcana", "History", "Investigation", "Nature", "Religion", "Athletics", "AnimalHandling", "Insight", "Medicine",
    "Perception", "Survival" }
local function set(names)
    local t = {}
    for _, n in ipairs(names) do t[n] = true end
    return t
end
local function closed() return { max = 0, names = NAMES, disabled = set(NAMES) } end
-- A page with these expertise rows, and the class and race groups as read.
local function real(class, expertise)
    return { class = class, class_skills = closed(), race_skills = closed(), expertise = expertise }
end
-- Expertise's 18 rows: `open` the ones the game enables and nothing selects, the rest disabled.
local function rows18(e)
    local on = set(e.open)
    for k in pairs(e.held or {}) do on[k] = true end
    for k in pairs(e.preset or {}) do on[k] = true end
    local disabled = {}
    for _, n in ipairs(NAMES) do
        if not on[n] then disabled[n] = true end
    end
    e.names, e.disabled, e.open = NAMES, disabled, nil
    return e
end
local function rogue6(full)
    return real("Rogue", rows18({ max = 2, full = full, open = { "Acrobatics", "Investigation", "Athletics" },
        held = { Stealth = true, Perception = true }, preset = { Deception = true, SleightOfHand = true } }))
end
local function expertiseOn()
    local on = {}
    for _, it in ipairs(page.expertiseItems) do
        if it._p.Selected == true then on[#on + 1] = it._p.Name end
    end
    return table.concat(on, ",")
end
local function offOf(key) return table.concat(j.result.set[key .. "_off"] or {}, ",") end
local TWO = { "Acrobatics", "Athletics" }

-- 33j. Rogue L6's page: it opens full of its own picks and the game calls it complete. By either key the plan's picks
--      land, an earlier level's stay, and what left the page is said. plan.skills is the one key the tool's definition
--      names, and the class and race groups' disabled rows don't stop it reaching expertise.
for _, full in ipairs({ "replaces", "clears" }) do
    for _, key in ipairs({ "expertise", "skills" }) do
        local how = " (" .. full .. ", plan." .. key .. ", "
        openPage(rogue6(full))
        j = run({ uuid = "claude", plan = { [key] = TWO } })
        check(j.status == "done" and page.finished == true
              and expertiseOn() == "Acrobatics,Stealth,Athletics,Perception"
              and offOf("expertise") == "Deception,SleightOfHand",
              "two names: the plan's land, held stay, what left is said" .. how .. tostring(j.status) .. ", "
              .. expertiseOn() .. ", " .. offOf("expertise") .. ", " .. tostring(j.error) .. ")")
        -- one name: once the game has taken a row the group may have room, and then nothing more of the page's own goes
        openPage(rogue6(full))
        j = run({ uuid = "claude", plan = { [key] = { "Acrobatics" } } })
        check(j.status == "done" and page.finished == true
              and expertiseOn() == "Acrobatics,SleightOfHand,Stealth,Perception"
              and offOf("expertise") == "Deception",
              "one name: only what it needs comes off" .. how .. tostring(j.status) .. ", " .. expertiseOn() .. ", "
              .. offOf("expertise") .. ", " .. tostring(j.error) .. ")")
        -- a named preset a toggle took and the room stage put back is on the page: not said
        openPage(rogue6(full))
        j = run({ uuid = "claude", plan = { [key] = { "Deception", "Acrobatics" } } })
        check(j.status == "done" and expertiseOn() == "Deception,Acrobatics,Stealth,Perception"
              and offOf("expertise") == "SleightOfHand",
              "a named preset back on the page isn't said" .. how .. expertiseOn() .. ")")
        -- one name, a preset: no toggle, nothing leaves
        openPage(rogue6(full))
        j = run({ uuid = "claude", plan = { [key] = { "Deception" } } })
        check(j.status == "done" and expertiseOn() == "Deception,SleightOfHand,Stealth,Perception"
              and offOf("expertise") == "", "one name, a preset: nothing leaves" .. how .. expertiseOn() .. ")")
    end
    openPage(rogue6(full))
    j = run({ uuid = "claude", plan = {} })
    check(j.status == "done" and expertiseOn() == "Deception,SleightOfHand,Stealth,Perception"
          and offOf("expertise") == "", "no skill in the plan: nothing leaves (" .. full .. ")")
end
-- 33k. Bard L10's page: nothing preset, both land by their own toggles, nothing turned off, by either key
for _, key in ipairs({ "expertise", "skills" }) do
    openPage(real("Bard", rows18({ max = 2, held = { Performance = true, Persuasion = true },
        open = { "Deception", "Acrobatics", "Arcana", "Insight", "Medicine", "Perception" } })))
    j = run({ uuid = "claude", plan = { [key] = { "Medicine", "Insight" } } })
    check(j.status == "done" and expertiseOn() == "Performance,Persuasion,Insight,Medicine"
          and offOf("expertise") == "",
          "a free group: toggled, nothing turned off (plan." .. key .. ", " .. expertiseOn() .. ")")
end
-- 33k2. Bard L3's page with the subclass taken: the class group has picks and shows earlier ones selected and disabled.
--       plan.skills goes to the first group offering the name. A name the plan gives to expertise stays there, though
--       the class group offers it first: the room is made in the group the expertise stage toggled in.
local function bard3(full)
    return { class = "Bard", race_skills = closed(),
        class_skills = { max = 3, names = NAMES,
            held = set({ "Performance", "Persuasion", "Acrobatics", "Insight", "Medicine" }) },
        expertise = rows18({ max = 2, full = full, preset = { Performance = true, Persuasion = true },
            open = { "Deception", "Acrobatics", "Arcana", "Insight", "Medicine", "Perception" } }) }
end
local function classOn()
    local on = {}
    for _, it in ipairs(page.classItems) do
        if it._p.Selected == true and it._p.Enabled == true then on[#on + 1] = it._p.Name end
    end
    return table.concat(on, ",")
end
openPage(bard3("replaces"))
j = run({ uuid = "claude", plan = { skills = { "Arcana", "Perception", "Deception", "Persuasion", "Performance" } } })
check(j.status == "done" and classOn() == "Deception,Arcana,Perception" and expertiseOn() == "Performance,Persuasion"
      and offOf("expertise") == "" and offOf("class") == "",
      "Bard L3, plan.skills: class picks in the class group, presets kept (" .. classOn() .. ", " .. expertiseOn()
      .. ", " .. tostring(j.error) .. ")")
openPage(bard3())
j = run({ uuid = "claude", plan = { expertise = { "Deception", "Persuasion" } } })
check(j.status == "done" and expertiseOn() == "Deception,Persuasion" and classOn() == ""
      and offOf("expertise") == "Performance",
      "plan.expertise: room made in expertise, not in the group offering it first (" .. expertiseOn() .. ", "
      .. classOn() .. ", " .. tostring(j.error) .. ")")
-- 33l. a plan's pick that can't land (the toggle does nothing): not finished, and the pick is named
openPage(rogue6())
page.expertiseStuck = true
j = run({ uuid = "claude", plan = { expertise = TWO } })
check(j.status == "failed" and not page.finished
      and tostring(j.error):find("skill Acrobatics: not on the page", 1, true),
      "a pick that doesn't land: not finished (" .. tostring(j.status) .. ", " .. tostring(j.error) .. ")")
-- 33m. a pick the game offers in no group, by either key: the same, and nothing leaves
for _, key in ipairs({ "expertise", "skills" }) do
    openPage(rogue6("replaces"))
    j = run({ uuid = "claude", plan = { [key] = { "History" } } })
    check(j.status == "failed" and not page.finished and tostring(j.error):find("skill History", 1, true)
          and expertiseOn() == "Deception,SleightOfHand,Stealth,Perception" and offOf("expertise") == "",
          "a pick not offered: not finished (plan." .. key .. ", " .. tostring(j.status) .. ", "
          .. tostring(j.error) .. ")")
end
-- 33m2. plan.skills naming an earlier level's pick: nothing to toggle, and the other name still lands
openPage(rogue6("replaces"))
j = run({ uuid = "claude", plan = { skills = { "Stealth", "Acrobatics" } } })
check(j.status == "done" and expertiseOn() == "Acrobatics,SleightOfHand,Stealth,Perception"
      and offOf("expertise") == "Deception" and #(j.result.problems or {}) == 0,
      "plan.skills, a held name: kept, no problem said (" .. expertiseOn() .. ", " .. tostring(j.error) .. ")")

-- The tests below leave the toggle doing nothing on a full group, so the room stage's guards are what's held.
-- 33n. F1: an earlier level's picks ahead of the page's own in the list: only the page's own comes off
openPage(real("Rogue", { max = 2,
    names = { "Stealth", "Perception", "SleightOfHand", "Acrobatics", "Athletics" },
    held = { Stealth = true, Perception = true }, preset = { SleightOfHand = true, Athletics = true } }))
j = run({ uuid = "claude", plan = { expertise = TWO } })
check(j.status == "done" and expertiseOn() == "Stealth,Perception,Acrobatics,Athletics"
      and offOf("expertise") == "SleightOfHand",
      "held ahead in the list: spared (" .. expertiseOn() .. ", " .. tostring(j.error) .. ")")
-- 33o. F1: two unnamed, one lacking: one comes off, the other stays
openPage(real("Rogue", { max = 2, names = { "Deception", "SleightOfHand", "Acrobatics" },
    preset = { Deception = true, SleightOfHand = true } }))
j = run({ uuid = "claude", plan = { expertise = { "Acrobatics" } } })
check(j.status == "done" and expertiseOn() == "SleightOfHand,Acrobatics" and offOf("expertise") == "Deception",
      "two unnamed, one lacking: only one off (" .. expertiseOn() .. ", " .. tostring(j.error) .. ")")
-- 33o2. a group over its max: no more comes off than the pick lacks, and the pick that still can't land is refused
openPage(real("Rogue", { max = 1, names = { "Deception", "SleightOfHand", "Acrobatics" },
    preset = { Deception = true, SleightOfHand = true } }))
j = run({ uuid = "claude", plan = { expertise = { "Acrobatics" } } })
check(j.status == "failed" and expertiseOn() == "SleightOfHand" and offOf("expertise") == "Deception",
      "over its max: only what's lacking comes off (" .. expertiseOn() .. ", " .. tostring(j.error) .. ")")
-- 33p. F1: a named pick the page already holds, ahead of an unnamed one: kept
openPage(real("Rogue", { max = 2, names = { "Athletics", "SleightOfHand", "Acrobatics" },
    preset = { Athletics = true, SleightOfHand = true } }))
j = run({ uuid = "claude", plan = { expertise = TWO } })
check(j.status == "done" and expertiseOn() == "Athletics,Acrobatics" and offOf("expertise") == "SleightOfHand",
      "a named preset ahead: kept (" .. expertiseOn() .. ", " .. tostring(j.error) .. ")")
-- 33r. F2: the group skillsStage toggled it in, the first offering the name: the class group; expertise is left alone
openPage({ class = "Bard", race_skills = closed(),
    class_skills = { max = 1, names = { "Deception", "Arcana" }, preset = { Deception = true } },
    expertise = { max = 2, names = { "Arcana", "Persuasion", "Insight" },
        preset = { Persuasion = true, Insight = true } } })
j = run({ uuid = "claude", plan = { skills = { "Arcana" } } })
check(j.status == "done" and page.classItems[2]._p.Selected == true and page.classItems[1]._p.Selected == false
      and expertiseOn() == "Persuasion,Insight" and offOf("class") == "Deception" and offOf("expertise") == "",
      "plan.skills: the first group offering the name, the other untouched (" .. expertiseOn() .. ", "
      .. tostring(j.error) .. ")")
-- 33s. Q4: a named pick the game shows disabled can't land, so it costs no unnamed pick; the read-back names it
openPage(real("Rogue", { max = 2, names = { "SleightOfHand", "Acrobatics", "Athletics" },
    preset = { SleightOfHand = true, Athletics = true }, disabled = { Acrobatics = true } }))
j = run({ uuid = "claude", plan = { expertise = TWO } })
check(j.status == "failed" and not page.finished and expertiseOn() == "SleightOfHand,Athletics"
      and offOf("expertise") == "" and tostring(j.error):find("skill Acrobatics: not on the page", 1, true) ~= nil
      and not table.concat(j.result.problems or {}, ";"):find("not offered", 1, true),
      "a disabled name: nothing turned off, named (" .. expertiseOn() .. ", " .. tostring(j.error) .. ")")

-- 33t. Rows reading Enabled nil, not seen live, held ones first. Nothing comes off, no earlier pick is lost, and the
--      named pick that can't land is refused, not finished
openPage(real("Rogue", { max = 2, unread = true,
    names = { "Stealth", "Perception", "SleightOfHand", "Acrobatics", "Athletics" },
    held = { Stealth = true, Perception = true }, preset = { SleightOfHand = true, Athletics = true } }))
j = run({ uuid = "claude", plan = { expertise = TWO } })
check(j.status == "failed" and not page.finished and expertiseOn() == "Stealth,Perception,SleightOfHand,Athletics"
      and offOf("expertise") == "" and tostring(j.error):find("skill Acrobatics", 1, true) ~= nil,
      "rows reading Enabled nil: nothing off, refused (" .. expertiseOn() .. ", " .. tostring(j.error) .. ")")
-- 33t2. One row reading nil among rows reading true: it stays though it comes first, and a true one goes
openPage(real("Rogue", { max = 2, names = { "SleightOfHand", "Deception", "Acrobatics", "Athletics" },
    preset = { SleightOfHand = true, Deception = true } }))
page.expertiseItems[1]._p.Enabled = nil
j = run({ uuid = "claude", plan = { expertise = TWO } })
check(j.status == "failed" and not page.finished and expertiseOn() == "SleightOfHand,Acrobatics"
      and offOf("expertise") == "Deception",
      "a row reading Enabled nil stays, a true one goes (" .. expertiseOn() .. ", " .. tostring(j.error) .. ")")
