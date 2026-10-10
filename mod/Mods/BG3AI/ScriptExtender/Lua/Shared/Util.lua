-- Small helpers shared by both contexts.
BG3AI = BG3AI or {}
local U = {}
BG3AI.Util = U

-- Osiris bool-ish results come back as 1/0 or true/false.
function U.truthy(x) return x == 1 or x == true end

-- Translated string from a handle ("h123...;1" or bare handle). Returns nil if unknown.
function U.loca(handle)
    if handle == nil or handle == "" then return nil end
    local h = tostring(handle):gsub(";%d+$", "")
    local ok, s = pcall(Ext.Loca.GetTranslatedString, h)
    -- "%%% EMPTY": the game's text for a handle with no string (unnamed statuses)
    -- trimmed: some names end in a space ("Empty Bloodbank Bottle ", live 13:10)
    if ok and type(s) == "string" then s = s:gsub("^%s+", ""):gsub("%s+$", "") end
    if ok and s and s ~= "" and not s:find("^%%%%%% EMPTY") then return s end
    return nil
end

function U.round(x, digits)
    if type(x) ~= "number" then return x end
    local m = 10 ^ (digits or 1)
    return math.floor(x * m + 0.5) / m
end

function U.vec(v, digits)
    if not v then return nil end
    return { U.round(v[1], digits or 2), U.round(v[2], digits or 2), U.round(v[3], digits or 2) }
end

function U.dist(a, b)
    local dx, dy, dz = a[1] - b[1], a[2] - b[2], a[3] - b[3]
    return math.sqrt(dx * dx + dy * dy + dz * dz)
end

-- Safe component read: entity.Component may throw for unmapped components.
function U.comp(entity, name)
    if not entity then return nil end
    local ok, c = pcall(function() return entity[name] end)
    if ok then return c end
    return nil
end

-- Load a module by path relative to the Lua/ folder; a missing or broken
-- module is reported but doesn't stop the rest of the mod from loading.
function U.require(path)
    local ok, err = pcall(Ext.Require, path)
    if not ok then
        Ext.Utils.PrintWarning("[BG3AI] failed to load " .. path .. ": " .. tostring(err))
    end
    return ok
end

return U
