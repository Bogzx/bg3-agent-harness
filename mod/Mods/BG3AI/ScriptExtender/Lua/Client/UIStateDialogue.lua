-- ui.state's dialogue, part 2 (UIState.lua is at the 1000-line cap): the answer list's window op and the tag runs
-- an answer row renders.
BG3AI = BG3AI or {}
local RPC = BG3AI.RPC
local UI = BG3AI.UI
local S = BG3AI.UIState

-- The tag runs an AnswerText renders ("[PALADIN] ", "[DECEPTION] "): the ls.LSRun children of its top inline. Window
-- 10 L1 (ans5-client.json): a plain row's Span holds one ls.LSSpan (the line); row 3 ([PERSUASION]) held one ls.LSRun
-- before it, row 4 ([PALADIN] [DECEPTION]) two; the runs' Text reads nil, so only their count is known. Named reads
-- only (S017), never the row's DataContext (S001). 0 when the inlines don't read.
function S.tagRuns(textEl)
    local n = 0
    for _, top in UI.items(textEl and UI.prop(textEl, "Inlines"), 4) do
        for _, x in UI.items(UI.get(top, "Inlines"), 12) do
            if UI.typeName(x) == "ls.LSRun" then n = n + 1 end
        end
    end
    return n
end

-- ui.answer_list -> {over, first, total, offset, scrollable} of the open dialogue's answer list: the bridge
-- finds the list under the cursor (over) and wheels it to read the rows below the fold (a player scrolls).
RPC.register("ui.answer_list", function()
    local w = UI.widgetMap().Dialogue
    local list = w and UI.isVisible(w.el) and UI.byName(w.el, "answerList")
    if not list then return { open = false } end
    local items = UI.findAll(list, function(e)
        local ty = UI.typeName(e)
        return ty == "ls.LSListBoxItem" or ty == "ListBoxItem"
    end, { prune = true })
    local out = { open = true, over = UI.get(list, "IsMouseOver") == true, rows = #items }
    local win = #items > 0 and S.listWindow(list, items)
    if win then
        out.first, out.total, out.offset, out.scrollable = win.first, win.total, win.offset, win.scrollable
    end
    return out
end)
