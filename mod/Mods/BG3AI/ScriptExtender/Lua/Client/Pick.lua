-- World -> screen projection and hover checks for click-on-entity flows
-- (client context; owner: worker "act").
--
-- ops:
--   ui.screen_pos  {uuid, aim?="auto"|"feet"|"low"|"mid"|"chest", dy?, ox?}
--       -> {x, y, on_screen, ndc=[x,y], depth, size=[w,h], aim, side}
--       ox: metres along the entity's own X axis (a door's origin is its
--       hinge; +-0.5 aims at the leaf). side: "left"|"right" = where the
--       point is relative to the view direction (also when behind).
--       x,y are window pixels (Noesis root size, which matches the SDL
--       window size the input shim uses); ndc is [-1,1] with +y = down.
--   ui.hover_check {uuid}
--       -> {hovered, hover?=uuid, cursor=[x,y], world?=[x,y,z]}
--       What the picking helper says is under the cursor right now
--       (PlayerPickingHelper.Inner.Inner[1|2].GameObject).
--
-- Camera: the active ls::CameraComponent's Controller.Camera View and
-- Projection matrices (glm, column-major).

BG3AI = BG3AI or {}
local U = BG3AI.Util
local RPC = BG3AI.RPC
local P = {}
BG3AI.Pick = P

local function guid(s)
    if type(s) ~= "string" then return nil end
    local g = s:match("(%x%x%x%x%x%x%x%x%-%x%x%x%x%-%x%x%x%x%-%x%x%x%x%-%x%x%x%x%x%x%x%x%x%x%x%x)$")
    return g and g:lower() or nil
end

local function entity_uuid(e)
    local ok, u = pcall(function() return e.Uuid.EntityUuid end)
    return ok and u and guid(tostring(u)) or nil
end

-- Active game camera controller (prefers Active, then any with a controller).
function P.camera()
    local ok, list = pcall(Ext.Entity.GetAllEntitiesWithComponent, "Camera")
    if not ok or not list then return nil end
    local fallback, fallback_e
    for _, e in ipairs(list) do
        local okc, c = pcall(function() return e.Camera end)
        if okc and c then
            local okk, ctrl = pcall(function() return c.Controller end)
            if okk and ctrl then
                local okA, active = pcall(function() return c.Active end)
                if okA and active then return ctrl, e end
                if not fallback then fallback, fallback_e = ctrl, e end
            end
        end
    end
    return fallback, fallback_e
end

function P.screen_size()
    local ok, w, h = pcall(function()
        local root = Ext.UI.GetRoot()
        return root.ActualWidth, root.ActualHeight
    end)
    if ok and type(w) == "number" and w > 0 then return w, h end
    return nil
end

-- Projection model (verified live 2026-10-09 against the picking helper's
-- per-pixel cursor rays on a 5120x1440 window):
--   camera-local l = conj(q) * (p - C), with C/q the active camera entity's
--   ls::TransformComponent; the camera looks along local +Z, +Y up, +X right.
--   ndc_x = fx * l.x / l.z,  ndc_y(down) = -fy * l.y / l.z,  fx = fy / aspect.
-- bg3le reads rf::CameraController with a field misalignment: the fields
-- named FOV/NearPlane hold the far plane / aspect, and ProjectionMatrix[2]
-- holds the real P[1][1] (= fy). Those are accepted only when plausible,
-- then the correctly-aligned names, then the calibrated constant (vFOV ~55 deg).
P.FY_DEFAULT = 1.921

local function plausible(x, lo, hi) return type(x) == "number" and x >= lo and x <= hi end

function P.intrinsics(ctrl)
    local fy, aspect
    pcall(function()
        local m = ctrl.Camera.ProjectionMatrix
        if plausible(m[2], 0.3, 6) then fy = m[2] elseif plausible(m[6], 0.3, 6) then fy = m[6] end
    end)
    pcall(function()
        if plausible(ctrl.NearPlane, 0.8, 6) then aspect = ctrl.NearPlane
        elseif plausible(ctrl.AspectRatio, 0.8, 6) then aspect = ctrl.AspectRatio end
    end)
    local w, h = P.screen_size()
    if not aspect and w and h and h > 0 then aspect = w / h end
    fy = fy or P.FY_DEFAULT
    aspect = aspect or (16 / 9)
    return fy / aspect, fy, aspect
end

local function rotate_inv(q, v)
    -- v rotated by the conjugate of the unit quaternion q = (x, y, z, w)
    local x, y, z, w = -q[1], -q[2], -q[3], q[4]
    local tx = 2 * (y * v[3] - z * v[2])
    local ty = 2 * (z * v[1] - x * v[3])
    local tz = 2 * (x * v[2] - y * v[1])
    return { v[1] + w * tx + (y * tz - z * ty), v[2] + w * ty + (z * tx - x * tz), v[3] + w * tz + (x * ty - y * tx) }
end

-- Project world point p -> ndc (x right, y down), depth, on_screen.
function P.project(p)
    local ctrl, cam = P.camera()
    if not cam then return nil, "no active camera" end
    local ok, C, q = pcall(function()
        local t = cam.Transform.Transform
        return t.Translate, t.RotationQuat
    end)
    if not ok or not C or not q then return nil, "camera transform unavailable" end
    local l = rotate_inv(q, { p[1] - C[1], p[2] - C[2], p[3] - C[3] })
    local side = l[1] < 0 and "left" or "right"
    if l[3] <= 0.05 then return { behind = true, side = side } end
    local fx, fy = P.intrinsics(ctrl)
    local nx = fx * l[1] / l[3]
    local ny = -fy * l[2] / l[3]
    return { ndc = { nx, ny }, depth = l[3], side = side,
        on_screen = nx >= -1 and nx <= 1 and ny >= -1 and ny <= 1 }
end

-- Where to aim on an entity: characters at chest height, corpses low, items
-- slightly above their origin.
function P.aim_point(uuid, aim, dy, ox)
    local e = Ext.Entity.Get(uuid)
    if not e then return nil, "unknown entity" end
    local ok, t = pcall(function() return e.Transform.Transform.Translate end)
    if not ok or not t then return nil, "no transform" end
    local p = { t[1], t[2], t[3] }
    local is_char = U.comp(e, "ClientCharacter") ~= nil
    if aim == nil or aim == "auto" then
        local dead = U.comp(e, "Death") ~= nil
        if is_char then aim = dead and "low" or "chest" else aim = "low" end
    end
    local off = ({ feet = 0.05, low = 0.3, mid = 0.9, chest = 1.1 })[aim] or 0.3
    p[2] = p[2] + off + (tonumber(dy) or 0)
    ox = tonumber(ox)
    if ox and ox ~= 0 then
        pcall(function()
            local q = e.Transform.Transform.RotationQuat
            local d = rotate_inv({ -q[1], -q[2], -q[3], q[4] }, { ox, 0, 0 }) -- rotate by q
            p[1], p[3] = p[1] + d[1], p[3] + d[3]
        end)
    end
    return p, aim
end

RPC.register("ui.screen_pos", function(args)
    local uuid = guid(args.uuid)
    if not uuid then error("bad uuid") end
    local p, aim = P.aim_point(uuid, args.aim, args.dy, args.ox)
    if not p then error(aim) end
    local r, err = P.project(p)
    if not r then error(err) end
    if r.behind then return { on_screen = false, behind = true, side = r.side } end
    local w, h = P.screen_size()
    local out = { ndc = { U.round(r.ndc[1], 4), U.round(r.ndc[2], 4) }, on_screen = r.on_screen,
        depth = U.round(r.depth, 2), aim = aim, side = r.side }
    if w then
        out.size = { w, h }
        out.x = math.floor((r.ndc[1] * 0.5 + 0.5) * w + 0.5)
        out.y = math.floor((r.ndc[2] * 0.5 + 0.5) * h + 0.5)
    end
    return out
end)

-- Entities currently under the cursor according to the picking helper.
function P.hovered()
    local ok, ph = pcall(Ext.UI.GetPickingHelper, 1)
    if not ok or not ph then return {}, nil end
    local out = {}
    pcall(function()
        local inner = ph.Inner.Inner
        for i = 1, #inner do
            local go = inner[i].GameObject
            local u = go and entity_uuid(go)
            if u then out[#out + 1] = u end
        end
    end)
    return out, ph
end

RPC.register("ui.hover_check", function(args)
    local want = guid(args.uuid)
    local list, ph = P.hovered()
    local res = { hovered = false }
    for _, u in ipairs(list) do
        if u == want then res.hovered = true end
    end
    res.hover = list[1]
    if ph then
        pcall(function() res.cursor = { ph.WindowCursorPos[1], ph.WindowCursorPos[2] } end)
        pcall(function() res.world = U.vec(ph.Inner.WorldPosition, 1) end)
    end
    return res
end)

return P
