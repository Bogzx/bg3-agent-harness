# 01: Modding runtime: getting our code running inside BG3 on this machine

Research date: 2026-10-09. Local game: native Linux build, Steam buildid 25605617, `Patch8_HotFix10.pak`,
binary version string `4.1.1.7631656` (= **v4.76.31.656** in bg3se/bg3le notation), GNU build ID
`a187af943be20f96b004dcfb74a33c43f65ba613`.

## TL;DR

* **The question changed in the last three weeks.** A community port of bg3se to the native Linux build,
  **bg3le** (github.com/lenonk/bg3le, Nexus mod 25431), appeared on 2026-09-20/21. It was announced to Norbyte on
  2026-09-30 (bg3se issue #610), and it reached **v0.3.6 on 2026-10-08**. It vendors bg3se's game definitions and
  Lua binding layer, injects into native `bin/bg3` with `LD_PRELOAD`, and claims every upstream `Ext.*` function
  in both contexts. **It explicitly supports v4.76.31.656, which is exactly our installed build.**
* bg3le is effectively option (B), "native + custom LD_PRELOAD injector", already built: about 58k lines, with
  the engine signature work done. It also exposes a **TCP Lua-eval server on 127.0.0.1:9998** (bg3se's LuaDebug
  protobuf protocol) that our harness can use as the IPC channel with no file polling.
* Upstream **bg3se remains Windows-only**. On Linux it needs Proton with the Windows build plus
  `WINEDLLOVERRIDES="DWrite.dll=n,b" %command%`. The latest GitHub release is **v32 (2026-06-21)**, and upstream
  `main` is already documenting v33 features (`Ext.UI.Defer`).
* bg3se Lua has **no sockets and no external networking** (stated in the docs). From Lua, the only ways out are
  `Ext.IO` files under the "Script Extender" folder and the debugger TCP port. On bg3le, native plugins (`.so`
  files) are a third route.
* **Recommendation: (C) hybrid.** Write our mod against the bg3se `Ext` API and run it on **native Linux with a
  pinned, self-built bg3le**, talking to it over the LuaDebug TCP eval protocol. Keep **Proton + Windows build +
  official bg3se** as a tested fallback that runs the *same mod and the same IPC client*. Do not write our own
  injector from scratch. Details are at the end.

---

## 1. BG3 Script Extender (Norbyte/bg3se): versions, cadence, Linux

### Versions and cadence
| Release | Date | Notes |
|---|---|---|
| v32 | 2026-06-21 | Latest GitHub release. Adds `Ext.Stats.SpellSet/EquipmentSet/ItemCombo...`, `Ext.Level.CreateSurfaceAction/ExecuteSurfaceAction`, `Ext.Loca.GetAllTranslatedStringKeys`, `Ext.Utils.GenerateGuid`, entity `WasAdded/WasRemoved/GetChangedComponents`, `Ext.Entity.SetupTracing`, timeline/dialog component mappings |
| v31 | 2026-04-17 | Embedded in the launcher as an offline fallback |
| updater-20240430 / 20240329 / 20240328 | 2024 | Launcher (DWrite.dll) builds; Proton instructions are in these release notes |

* Upstream `main` was last committed on **2026-10-08** and is very active. `Docs/API.md` already documents
  **v33** behaviour: "Starting from SE v33, code that uses Noesis objects ... must be done via `Ext.UI.Defer()`".
* Distribution model: `DWrite.dll` is a small launcher/updater that downloads the actual extender DLL matching the
  running game build from Norbyte's update server. So the in-game version often moves ahead of the GitHub releases
  page, which only says "Supports patch 8". Each extender build targets a specific game build, and the FAQ says
  "new extender builds target only the latest game version".
* Cadence: GitHub releases come every 2–3 months. Hotfix-compatibility updates arrive through the auto-updater,
  typically within days of a game patch. (**UNVERIFIED** for the 2026 hotfixes specifically.)

Sources: https://github.com/Norbyte/bg3se/releases, https://github.com/Norbyte/bg3se/releases/tag/v32,
https://github.com/Norbyte/bg3se/releases/tag/v31, `Docs/ReleaseNotes.md` and `Docs/API.md` in the repo (cloned at
`91abbf2`, 2026-10-08).

### Native Linux support in official bg3se: none
* The bg3se README, docs and releases mention Linux only as Proton. The community wiki is explicit: "THIS ONLY
  WORKS IF YOU USE PROTON. NOT IF YOU USE THE NATIVE LINUX VERSION"
  (https://wiki.bg3.community/Tutorials/Mod-Use/How-to-install-Script-Extender, via search snippet).
* The codebase is MSVC/Win32-specific (Detours, `-fms-extensions`-isms, Windows PE pattern scanning), and building
  it needs `first-time-setup.bat` from the MSVC x64 Native Tools prompt.

### The Linux port: bg3le (new, 2026-09)
* Repo: https://github.com/lenonk/bg3le (created 2026-09-21, last pushed 2026-10-09, 417 commits, 406 by Lenon
  Kitchens and 11 by Ihor Tarasiuk, 3 stars, 0 open issues). Forks: https://github.com/AaronJordison/bg3le and
  https://github.com/igortarasuk/bg3le. Nexus: https://www.nexusmods.com/baldursgate3/mods/25431. Announcement:
  https://github.com/Norbyte/bg3se/issues/610 (2026-09-30, with no reply from Norbyte when checked).
* Vendors bg3se at upstream commit `dc2530ab` (2026-09-23) under `vendor/bg3se/`: CoreLib, GameDefinitions, the
  Lua binding framework, Osiris and hook layers, and builtin Lua. It uses Norbyte's Lua fork. Changes to the
  vendored code are listed in `vendor/NOTICE.md`. Most are clang/libc++ conformance fixes, plus Linux ABI layout
  fixes. The license is MIT plus Commons Clause, which is fine for our non-commercial use.
* **Claimed coverage** (README): "every function upstream registers is present in both contexts"
  (`tools/check-api.sh` against upstream's module declarations). Mods load from `.pak` files, and the README lists
  as working: Osiris (calls, queries, PROCs, DBs, `RegisterListener`), Entities (all component fields read/write,
  `OnCreate`/`OnChange`/system-update subscriptions), Stats, StaticData, Resource, Template, Loca, Level (physics,
  AI grid, pathfinding, surface actions), IMGUI, UI (Noesis), input events and key injection, Audio, IO, Net, MCM,
  and PersistentVars compatible with bg3se saves.
* **Known gaps** (`reference/COMPUTED-MEMBERS.md`, audited 2026-10-03): the Noesis methods `TypeInfo`,
  `NumReferences`, `TreeParent`, `PointFromScreen`, `PointToScreen`, `HitTest`; some visual/material methods;
  `esv::Character:GetStatusByType`; a few `Ext.System` methods. Multiplayer was only tested over loopback. **It
  does not have upstream v33's `Ext.UI.Defer`**, because it vendors a pre-v33 snapshot (grep of `lua_host.cpp`).
* **Version fit with us:** the reference capture was taken on v4.73.98.727. The README says: "Hotfix v4.76.31.656
  moved nearly every function by 64-128 bytes without changing it; bg3le found all its targets without a code
  change", and "523 struct layouts still match v4.76.31.656 (`Ext._Internal.SizeAudit()`)". Our binary reports
  `4.1.1.7631656`, which is the same build. (**Inference**: Larian's `4.1.1.<7 digits>` string maps to
  `v4.76.31.656`. The digits match exactly.)
* **Maturity warning:** the project is three weeks old, has one main author, a very high commit rate (about 20
  commits a day; it looks AI-assisted), and 58k lines in `src/`. The changelog shows real mods (Trials Ascension,
  EasyCheat, Wye Fey Potions, Compatibility Framework, MCM) being fixed day by day, and Osiris output-parameter
  bugs were only fixed in v0.3.6 (2026-10-08). Treat it as beta.
* Note: a search-engine summary claimed the lenonk README lists `Ext.Entity` as "not implemented". The current
  README and code contradict that; it is stale or wrong.

### Did Larian's native Linux build change the ecosystem?
* Larian shipped the native build on **2025-09-23** (GamingOnLinux; Notebookcheck calls it Hotfix #34), aimed at
  Steam Deck: "Larian does not provide support for the Linux platform. The Steam Deck Native build is only
  supported on Steam Deck." On desktop Linux it is still served by default once the initial download bug was fixed,
  which is why this machine runs it.
  https://www.gamingonlinux.com/2025/09/baldurs-gate-3-gets-a-native-linux-version-to-improve-it-on-steam-deck/
* Profile, mods and saves live in `~/.local/share/Larian Studios/Baldur's Gate 3/` (XDG), instead of the Proton
  prefix's `AppData/Local/...`. **The Proton build and the native build use different save folders**, bridged only
  by Steam Cloud.
* The in-game mod manager (mod.io, Patch 7, 2024) and `.pak` mods are data-only, so they work natively. The
  Larian **Toolkit is Windows-only** (**UNVERIFIED** whether it runs under Proton for authoring; irrelevant for us,
  since we only need a `.pak` with `ScriptExtender/` Lua and optional story goals).
* Linux-specific `.pak` pitfalls found by bg3le (README): (a) an **empty** `Stats/Generated/Data/*.txt` hangs
  loading at about 95%; (b) **case-sensitive paths**: a mod referring to its own files with different casing breaks.
  bg3le transparently rewrites a corrected copy into `~/.local/share/bg3le/pakfix/`. Our own pak should avoid both.
* The game writes `ModCrashSanityCheck/` into the profile while running and **disables all mods on the next start
  if it was killed**. Any harness that kills the game hits this. bg3le (like bg3se) deletes it on startup.

---

## 2. The Proton route: Windows build + bg3se

### Forcing Proton on a game that has a native build
1. Steam → Library → Baldur's Gate 3 → ⚙ → **Properties → Compatibility** → tick **"Force the use of a specific
   Steam Play compatibility tool"** → choose **Proton 11.0** (or Proton Experimental).
2. Steam downloads the Windows depots into the same `steamapps/common/Baldurs Gate 3/` directory, replacing
   `bin/` with `bin/bg3.exe` (Vulkan) and `bin/bg3_dx11.exe`. Expect a large re-download. (**UNVERIFIED** how much
   is shared; the `.pak` data may largely be reused.) The prefix becomes
   `~/.local/share/Steam/steamapps/compatdata/1086940/pfx/` (currently empty here).
3. Switching back means unticking the box, which re-downloads the native binaries. Only one build can be installed
   at a time per Steam library. A second copy would need a second library folder or `download_depot`.

### Installing bg3se under Proton
1. Download `BG3SE` from the GitHub releases (not the Nexus reupload), and put **`DWrite.dll` in `bin/`** next to
   `bg3.exe`.
2. Launch options (current form from the release notes):
   ```
   WINEDLLOVERRIDES="DWrite.dll=n,b" %command%
   ```
   Older notes (2024-03-28) also had `PROTON_NO_ESYNC=1`; current notes drop it. `n,b` means native (our
   DLL) first, then builtin (Wine's DWrite). Optionally add `--skip-launcher` after `%command%` to bypass the
   Larian launcher (**UNVERIFIED** that this is still needed in 2026). Wine 11.6 changes DLL preference so that a
   non-Microsoft DWrite.dll would be picked automatically, but Proton has not adopted that yet, so keep the override.
3. First launch: the updater downloads the matching extender into the prefix (`AppData/Local/BG3ScriptExtender/`,
   **UNVERIFIED** exact folder) and shows a console if `CreateConsole` is set.
4. `bin/ScriptExtenderSettings.json`:
   ```json
   { "CreateConsole": true, "EnableLogging": true, "EnableLuaDebugger": true, "DeveloperMode": true }
   ```
   `EnableLuaDebugger` opens **127.0.0.1:9998** (`Docs/Debugger.md`: "Lua debugger listening on 127.0.0.1:9998").
   Wine maps Winsock to host sockets, so a Linux process can connect to it.

### Where `Ext.IO` writes under Proton
bg3se's `SaveFile` root is `ToPath("/Script Extender", PathRootType::LocalAppData)`, i.e.
`%LOCALAPPDATA%\Larian Studios\Baldur's Gate 3\Script Extender\`, which on this machine is:
```
~/.local/share/Steam/steamapps/compatdata/1086940/pfx/drive_c/users/steamuser/AppData/Local/Larian Studios/Baldur's Gate 3/Script Extender/
```
(The root comes from the bg3le source comment that ports it 1:1, `src/ext_libs.cpp`. The concrete Proton path is
standard Proton layout; **UNVERIFIED** on this machine because the prefix has never been populated.)

Under **bg3le (native)** the same API writes to:
```
~/.local/share/Larian Studios/Baldur's Gate 3/Script Extender/
```
(`profile_root() + "/Script Extender"`, `src/ext_libs.cpp`). It refuses paths that climb out with `..`.

### Known Proton issues
* Startup deadlock when other DLL injectors are present (fixed or worked around in updater-20240430).
* Saves and profile live in the prefix, separate from the native build's saves.
* Every game hotfix requires the extender to update first. Under Proton this is automatic but needs network
  access at launch.
* The native build loads levels in about 1 s on bg3le's machine against 65–98 s before its fix (bg3le
  `SLOW-LOAD-DIAGNOSIS.md`). That is a native-build bug, so Proton is not necessarily slower; there is no measured
  comparison here.
* ProtonDB reports Proton plus SE as generally working (https://www.protondb.com/app/1086940/).

---

## 3. The bg3se Lua API surface we care about

Names below come from bg3se `Docs/API.md` (labelled "v30" but updated to v33), from the module registrations in
source (`BG3Extender/Lua/Libs/*.inl`, `GameDefinitions/PropertyMaps/UI.inl`), and from bg3le's
`reference/upstream-api.txt`, which is generated from upstream's module declarations. `IdeHelpers/ExtIdeHelpers.lua`
in the repo has full type annotations, and `Ext.Debug.GenerateIdeHelpers()` regenerates them for the live build.
Context: **C** = client only, **S** = server only, otherwise both.

### Client vs server
"Osiris and behavior scripts always run on the server... the extender keeps multiple Lua states: one state for the
server and one for each client." The states share nothing. Single player is a local server plus one client.
* **Server**: Osiris (`Osi.*`), authoritative `esv::` components, Stats, `Ext.Level` surface actions, templates.
* **Client**: Noesis UI (`Ext.UI`), input injection (`Ext.Input`), IMGUI, Audio, picking/cursor, `ecl::`
  components, plus `Ext.Level` raycasts and pathfinding.
* The server Lua state exists **only once a save is loaded**. At the main menu only the client context runs, so
  load and new game must be driven from the client side (UI) or by key injection.

### Ext.Entity (both)
`Get(uuidOrHandle)`, `GetAllEntities()`, `GetAllEntitiesWithComponent(name)`, `GetAllEntitiesWithUuid()`,
`GetEntitiesAroundPosition(pos, radius)`, `UuidToHandle`, `HandleToUuid`, `Create`, `Destroy`,
`GetRegisteredComponentTypes`, subscriptions `Subscribe/Unsubscribe`, `OnCreate[Once|Deferred|DeferredOnce]`,
`OnDestroy*`, `OnChange`, `OnSystemUpdate`, `OnSystemPostUpdate`, and tracing `EnableTracing/SetupTracing/GetTrace/
ClearTrace`.
Entity methods: `GetAllComponentNames()`, `GetAllComponents()`, `GetComponent(name)` (or the shorthand
`entity.Health`), `CreateComponent`, `Replicate`, `IsAlive`, `GetSalt`, `GetIndex`, and v32's `WasAdded`,
`WasRemoved`, `GetChangedComponents`. Helpers: `_C()` returns the host character entity, `_D()` dumps,
`_P()` prints.
```lua
local e = Ext.Entity.Get(Osi.GetHostCharacter())
local hp = e.Health            -- eoc::HealthComponent: Hp, MaxHp, ...
for _, h in ipairs(Ext.Entity.GetAllEntitiesWithComponent("ServerCharacter")) do ... end
```

### Ext.Osiris / Osi (server)
* Calls: `Osi.CharacterResetCooldowns(player)`. Queries: `local x,y,z = Osi.GetPosition(uuid)`, and zero-OUT
  queries return a bool. Events: `Osi.StoryEvent(player, "x")`, which runs synchronously. PROCs:
  `Osi.Proc_CharacterFullRestore(player)`. User queries: `Osi.Qry_IsHealingStatus("DAMAGE")`.
* Databases: `Osi.DB_Name:Get(nil, ...)`, `Osi.DB_Name(args)` to insert, `Osi.DB_Name:Delete(...)`.
* Listening: `Ext.Osiris.RegisterListener(name, arity, "before"|"after"|"beforeDelete"|"afterDelete", fn)`. This
  covers events, queries, DBs, PROCs and QRYs, **but not built-in calls**.
* Gotcha: `Osi.Anything` always returns a callable object, so `~= nil` cannot test for existence.
* This is the main action surface: Osiris has calls for movement, attacking (`UseSpell`), dialogue
  (`StartDialog_Internal`), inventory, resting, and more. The full catalog is out of scope here; bg3le's
  `reference/osi-api-4.8.400.7143220.txt` is a dump of the signatures.

### Ext.Stats (both)
`Get(name, [level], [warn])`, `GetStats(type)`, `Create`, `Sync`, `GetStatsLoadedBefore`, `GetCachedSpell/
Status/Passive/Interrupt/Boost`, `ExecuteFunctor(s)`, `PrepareFunctorParams`, `EnumIndexToLabel/
EnumLabelToIndex`, `GetModifierAttributes`, `GetStatsManager`, `AddAttribute`, `SetPersistence`, plus the
sub-modules `SpellSet`, `EquipmentSet`, `ItemCombo`, `TreasureTable` and others. `Ext.Stats.ExtraData` holds
global constants.

### Ext.Loca (both)
`GetTranslatedString(handle, [fallback])`, `UpdateTranslatedString`, `GetAllTranslatedStringKeys`,
`GetTranslatedStringKey`, `UpdateTranslatedStringKey`. This is needed to turn `DisplayName.NameKey`,
dialogue lines and other handles into text.

### Ext.UI (Noesis, client)
Module functions (`ClientUI/Module.inl`): `GetRoot()`, `GetStateMachine()`, `SetState()`, `RegisterType()`,
`Instantiate()`, `GetPickingHelper(playerId)`, `GetCursorControl()`, `GetDragDrop(playerId)`, `Defer(fn)`
(v33, upstream only), `EnableErrorReporting()`.
Object methods (`PropertyMaps/UI.inl`):
* `BaseObject`: `.Type`, `ToString()`, `GetProperty(name)`, `SetProperty(name, v)`, `GetAllProperties()`,
  `DirectProperties()`, `DependencyProperties()`
* `Visual`: `VisualParent`, `VisualChildrenCount`, `VisualChild(i)`, `PointFromScreen/PointToScreen/HitTest`
  (missing in bg3le)
* `FrameworkElement`: `Parent`, `ChildrenCount`, `Child(i)`, **`Find(name)`** (x:Name lookup), `Resource(key)`,
  `TreeParent`; `UIElement:Subscribe/Unsubscribe` for routed events
* **`ICommand:CanExecute(param)` / `ICommand:Execute(param)`** and the same on `BaseCommand`. Combined with
  `element.DataContext.<SomeCommand>`, this **lets us press UI buttons through their ViewModel commands** without
  synthesizing clicks. `LuaDelegateCommand:SetHandler(fn)` is for our own commands.
```lua
-- e.g. inspect the dialogue VM (names to discover live with GetAllProperties)
local root = Ext.UI.GetRoot()
local w = root:Find("ContentRoot")
_D(w.DataContext:GetAllProperties())
```
Threading: "Since Noesis rendering is performed parallel with Lua code, manipulating Noesis objects has a slight
risk of crashing the game". In v33+ this has to go through `Ext.UI.Defer`. bg3le lacks `Defer`, so on bg3le keep
Noesis access read-mostly and minimal (**risk**).
Also `Ext.Utils.GetDialogManager()` exposes the engine dialog manager, and v32 mapped dialog components; this is
likely the cleaner source for dialogue lines and options than Noesis. (**UNVERIFIED** field coverage.)

### Ext.Events (both)
`Ext.Events.<Name>:Subscribe(fn)`: `ModuleLoadStarted`, `StatsLoaded`, `SessionLoading`, `SessionLoaded`,
`ResetCompleted`, `GameStateChanged`, `Tick`, `NetMessage`, plus others in `ExtIdeHelpers.lua`.
`Ext.Utils.GetGameState()` returns the current state, such as Menu, LoadSession or Running.

### Ext.Timer (both)
`WaitFor(ms, cb)` (game clock, pauses with the game), `WaitForRealtime(ms, cb)`, `WaitForPersistent(ms, name, cb)`,
`Cancel/Pause/Resume/IsPaused(handle)`, `RegisterPersistentHandler`, `GameTime()`, `MicrosecTime()`,
`MonotonicTime()`, `ClockTime()`, `ClockEpoch()`. **Use `WaitForRealtime`** for an IPC poll loop, because the game
clock stops while paused.

### Ext.IO (both)
`LoadFile(path, [context])` (context `"data"` reads game data), `SaveFile(path, content)`, `AppendFile`
(present in bg3le and in upstream's module list), `AddPathOverride`, `GetPathOverride`. Writes are sandboxed to the
"Script Extender" folder (paths in §2).

### Ext.Net, Ext.ClientNet, Ext.ServerNet
* NetChannel API (new): `channel:SetHandler(fn)`, `SetRequestHandler(fn)`, `RequestToServer(data, cb)`,
  `RequestToClient(data, user, cb)`. Local client requests are not delayed by a frame.
* Legacy: `Ext.ServerNet.BroadcastMessage/PostMessageToClient/PostMessageToUser/PlayerHasExtender`,
  `Ext.ClientNet.PostMessageToServer`, `Ext.Events.NetMessage` / `Ext.RegisterNetListener`, `Ext.Net.IsHost()`.
* **"There is no external networking capability in the Script Extender. SE mods cannot communicate with external
  servers or clients."** Ext.Net only connects the client and server Lua states. In our design it carries data
  between server-side state gathering and client-side UI and screen work.

### Ext.Json, Ext.Utils, Ext.Debug, Ext.Types, Ext.Vars, Ext.Mod
* `Ext.Json.Parse/Stringify(value, {Beautify=..., IterateUserdata=..., AvoidRecursion=..., MaxDepth=...})`
  (**UNVERIFIED** option names in detail; see `ExtIdeHelpers.lua`).
* `Ext.Utils`: `Version`, `GameVersion`, `GetGameState`, `GetGlobalSwitches`, `GetDialogManager`,
  `GetCommandLineParams`, `HandleToInteger/IntegerToHandle/IsValidHandle`, `GenerateGuid`, `GetMemoryUsage`,
  `ProfileBegin/End`, `ShowError`.
* `Ext.Debug`: `Reset()` (reloads the Lua states, useful for hot reload during development), `DumpStack`,
  `GenerateIdeHelpers`, `IsDeveloperMode`, `SetEntityRuntimeCheckLevel`.
* `Ext.Types`: `Serialize`, `GetObjectType`, `GetTypeInfo`, `TypeOf`, `IsA`, `GetAllTypes`, for reflection-driven
  dumps.
* `Ext.Vars`: `RegisterModVariable/RegisterUserVariable`, `GetModVariables`, `SyncModVariables`, which hold
  per-save persistent state.

### Input: `Ext.Input` (client)
`InjectKeyPress(scancode, [mods])`, `InjectKeyDown`, `InjectKeyUp`, `GetInputManager()`. These synthesize SDL key
events into the game's SDL queue (`ClientInput.inl`). There is **no mouse injection** in the API. Mouse actions
need either Osiris and ECS calls or external input (xdotool, ydotool, or uinput, outside SE). Related helpers:
`Ext.UI.GetPickingHelper(playerId)` (`Selection`, `Inner.WorldPosition`, `WindowCursorPos`) and
`Ext.UI.GetCursorControl()`. Input *events* are observable through `Ext.Events.KeyInput`, `MouseButtonInput` and
related events.

### Ext.Level (raycasts and pathfinding)
Client and server: `BeginPathfinding(entity, targetPos, opts)`, `BeginPathfindingImmediate`, `FindPath(path)`,
`ReleasePath`, `GetPathById`, `GetActivePathfindingRequests`, `GetEntitiesOnTile(pos)`, `GetHeightsAt(x,z)`,
`GetTileDebugInfo(pos)`, `RaycastAny/RaycastClosest/RaycastAll`, `Sweep{Box,Sphere,Capsule}{Closest,All}`,
`TestBox`, `TestSphere`. Server only: `CreateSurfaceAction`, `ExecuteSurfaceAction`, `GetLevelInfo`,
`AddActivePersistentLevelTemplate`.
Raycasts make line-of-sight checks possible for the **fairness filter** ("is this enemy visible?").

### Ext.Math
Vector and matrix ops: `Add/Sub/Mul/Div`, `Distance`, `Length`, `Normalize`, `Dot`, `Cross`, `Angle`, `Lerp`,
`Clamp`, quaternion ops (`QuatFromEuler`, `QuatRotate`, ...), `Random`, `Round`.

---

## 4. IPC from SE Lua to an external process

| Option | Works on | Latency | Verdict |
|---|---|---|---|
| **LuaDebug TCP eval, 127.0.0.1:9998** | bg3le (on by default; `BG3LE_DEBUG=0` disables, `BG3LE_DEBUG_PORT` changes the port); bg3se (`"EnableLuaDebugger": true`) | about 1 frame | **Primary** |
| `Ext.IO` file mailbox plus `Ext.Timer.WaitForRealtime` poll | both | 50–250 ms | Fallback and logging |
| bg3le native plugin `.so` (`~/.local/share/bg3le/plugins`) | bg3le only | per frame | Not needed for IPC (see below) |
| Lua sockets (`require "socket"`, `io.popen`) | neither | — | Not available: SE's sandbox has no `io`/`os.execute`/`package.loadlib`, and the docs say there is no external networking |
| Named pipes / FIFOs via `Ext.IO` | — | — | `SaveFile` opens, writes and closes paths under its root, so a FIFO would block the game thread. Do not use |

### The TCP eval protocol (verified in both codebases)
* Framing: little-endian `uint32` **total length including the 4-byte header**, then a protobuf message
  (`BG3Extender/Lua/Debugger/LuaDebug.proto`).
* Envelope fields: `1` = `seq` (varint), `3` = `DbgConnectRequest {protocol_version=1}`,
  `7` = `DbgEvaluate {context=1 (DbgContext: 1 = client in bg3le), expression=2, frame=3, flags=4}`.
* Replies (backend envelope): `2` = reply_seq, `3` = `BkConnectResponse{protocol_version}`,
  `5` = `BkEvaluateResponse { MsgValue result=1 (type_id=1, string value=5); string error_message=2 }`,
  `8` = debug output lines.
* Protocol versions: bg3le reports **4**. Upstream's VS Code adapter uses **5** (`DAPMessageHandler.cs`). Our client
  should read the version from `BkConnectResponse` and adapt; for eval the message layout is the same.
* bg3le semantics (`src/debug_server.cpp`): several clients can attach. Each eval is queued and **run on the story
  thread (server) or the client game thread**, never on the socket thread. The socket thread **waits up to 10 s**,
  then replies "timed out waiting for the story thread; is the game paused or still loading?". The server context
  is refused before a save is loaded.
* bg3se semantics (`LuaDebugger.cpp: ContextDebugger::Evaluate`): it pushes a pending action that runs on the
  context's thread. It does not need a breakpoint hit (**UNVERIFIED** in practice under Proton).
* Design rule: the harness never sends large code. It sends `return AIH.rpc([[<json>]])`, where `AIH` is a global
  defined by our mod. Long actions (walk somewhere, wait for a turn) return a job id immediately, and the harness
  polls `AIH.poll(id)`. Events are pushed into an in-Lua ring buffer that the harness drains. This keeps every
  eval far below the 10 s timeout and survives pause and loading screens.

### File mailbox fallback
Lua writes `Script Extender/aih/out-<seq>.json` with `Ext.IO.SaveFile` and polls `aih/in.json` every 100 ms
with `Ext.IO.LoadFile` in a `Ext.Timer.WaitForRealtime` loop. Atomicity: the harness writes `in.tmp` then
`rename()`s it. Lua can't rename, so it writes numbered files and the harness deletes the ones it has consumed.
Paths are in §2.

### bg3le native plugins
A plugin gets `add_event_handler` (SDL events; can swallow them), `sdl_function` (real SDL functions, so
`SDL_PushEvent` can **inject mouse events**), `add_frame_handler` (per client frame, client thread),
`add_setting` (bool/int/float shared with Lua via `Ext.Plugins.Get/Set`), and `log/warn`
(`include/bg3le_plugin.h`, ABI 1). It **cannot call Lua directly**, so it is not an RPC channel. It is a good
place for **mouse and controller injection** and possibly **screen capture** (hook the frame, read the swapchain),
which avoids Wayland screenshot permission problems. That belongs to the screen-access research, not here.

---

## 5. Native Linux alternative: our own LD_PRELOAD `.so`

### What the binaries actually expose (local `nm`)
`bin/bg3` (224 MB, PIE, not stripped): `nm -C` gives **152,400 entries**, but:

| Symbol class | Count | What it is |
|---|---|---|
| `r` (rodata, mostly `.L.str` literals) | 80,986 | no use |
| `b` (BSS data) | 23,710, of which **21,795 are Larian `ls::`/`ecs::`/`esv::`/`ecl::`/`eoc::` entries** | mainly `ls::TypeId<T, Context>::m_TypeIndex` statics |
| `t`/`T` (functions) | about 43.5k | **almost all third-party**: Noesis 19,286 (plus 4,184 thunks), PhysX 5,512, PlayFab 1,036, Wwise (`AK`, `CAk*`), asio and websocketpp, crashpad |
| Larian functions with names | **effectively 0** | `ecs::`, `esv::`, `ecl::`, `eoc::` have 0 function symbols |

So the binary is **not** "unstripped Larian code". Larian's own functions (EntityWorld, game loop, Osiris
dispatch glue, state machine) have **no names** and must be found by byte pattern or cross-reference, which is
exactly what bg3le's `src/targets.cpp` and `src/resolve.h` do.

What *is* useful:
* **5,299 component type-index globals**, for example
  `ls::TypeId<eoc::HealthComponent, ecs::ComponentTypeIdContext>::m_TypeIndex` @ `0x7cfad28`,
  `ls::TypeId<eoc::StatsComponent, ecs::ComponentTypeIdContext>::m_TypeIndex` @ `0x7cf5a50`,
  `ls::TypeId<eoc::TurnBasedComponent, ecs::ComponentTypeIdContext>::m_TypeIndex` @ `0x7cd7530`. There are also
  `ecs::SystemsContext` entries (934), `ecs::sync::ReplicatedTypeContext` entries (582) and
  `net::AbstractPeer::Protocols` entries. These give component and system IDs **by name** without pattern scanning.
* **The whole Noesis API by name**: `Noesis::GUI::CreateView(Noesis::FrameworkElement*)`,
  `Noesis::View::Update(double)`, `Noesis::View::GetContent() const`, `Noesis::View::KeyDown(Noesis::Key)`,
  `Noesis::View::MouseMove(int,int)`, `Noesis::View::MouseButtonDown(int,int,Noesis::MouseButton)`,
  `Noesis::View::Char(unsigned)`. Hooking `View::Update` captures every live view and its content root, and the
  `MouseButtonDown/Up` entry points allow UI clicks without OS input. Noesis's own reflection
  (`Noesis::TypeClass`, `DependencyProperty`) is all named, so walking ViewModels is feasible.
* Imports that can be interposed with `LD_PRELOAD`: `SDL_PollEvent` (a perfect per-frame main-thread hook plus
  input injection point; 71 SDL imports in total), `dlopen`, and all `libOsiris.so` imports (below).

`bin/libOsiris.so` (1.8 MB, 1,344 symbols, mostly `.L.str`): exports only the C++ facade:
* `COsiris::Event(unsigned int, COsiArgumentDesc*)`, which raises an Osiris event. bg3le reads the tuple slot
  from it.
* `COsiris::RegisterDIVFunctions(TOsirisInitFunction*)`, through which **the engine hands Osiris its
  call/query/error/assert callbacks** ("DIV functions"). Interposing it captures the engine's built-in call and
  query dispatchers, the classic bg3se and Divinity technique.
* `COsiris::GetFunctionMappings/GetTypeMappings/GetObjectMappings(MappingInfo**, unsigned*)`, `InitGame()`,
  `Load(COsiSmartBuf&)`, `Save(...)`, `Merge(const wchar_t*)`, `Compile(...)`, `GetOsirisObject()`,
  `COsiris::COsiris()` (bg3le reads Osiris globals from it), `OpenLogFile(const wchar_t*, unsigned)`, and
  `m_LogEnabled`.
* `COsiArgumentDesc::{Set,Get}{Integer,Integer64,Float,String,GUIDString,Handle,...}`, which build argument lists.
* **Missing**: names for the internal function manager, rete nodes, databases and `Query` and `Call` on user
  functions. bg3se reaches them through struct layouts reverse-engineered on Windows (`BG3Extender/Osiris/`),
  which bg3le reuses.
* `bg3` imports 30+ `COsiris::*` and `COsiArgumentDesc::*` symbols, so **PLT interposition of Osiris is trivial
  and survives patches**: a preloaded `.so` that defines `_ZN7COsiris20RegisterDIVFunctionsEP18TOsirisInitFunction`
  wins symbol resolution.

Other `NEEDED` libraries: `libSDL2.so`, `libsteam_api.so`, `libBink2x64.so`, `libnvsdk_ngx.so`,
`libssl.so.1.1`, `libcrypto.so.1.1`. The game runs inside the Steam Linux Runtime 3 ("sniper", glibc 2.31)
container, so an injected `.so` must target glibc ≤ 2.31 and **libc++ ABI v2** (24-byte `std::string`, 1-byte
`std::variant` index), not libstdc++ (bg3le `LIBCXX-ABI.md`).

### Building it ourselves: effort and fragility
| Component | From-scratch effort | Fragility per patch |
|---|---|---|
| Loader, `SDL_PollEvent` hook, embedded Lua or Python, socket server | 1–2 days | Low (exported symbols) |
| Osiris call, query, event and DB access via DIV interpose plus internal layouts | 1–3 weeks | Low to medium |
| ECS: find `EntityWorld`, the component storage layout, and map 2,000+ component structs | **months** (bg3se's 3 years of mappings; reusing bg3se's GameDefinitions is the only sane way) | High: layouts change with content patches |
| Noesis: hook `View::Update`, walk the tree, call `ICommand::Execute` | 1–2 weeks (symbols available) | Low |
| Stats, Loca, Level (pathfinding, raycasts) | weeks each (all need unnamed engine functions) | Medium |

Doing all of this ourselves means re-deriving bg3le. bg3le already took a bg3se-experienced author three weeks
of very high-throughput work, plus three years of Norbyte's mappings. **No.** The one realistic "own injector"
variant is a minimal Osiris-only shim (`SDL_PollEvent` frame hook, `RegisterDIVFunctions` interpose to get the
call/query dispatcher, embedded Lua, Unix socket). That gets about 70% of actions and some state through Osiris
queries in 2–3 weeks, but no ECS reflection, no Loca, and only fragile UI. It is strictly worse than bg3le.

Embedding a runtime: bg3le already embeds Lua 5.4 (Norbyte's fork). Embedding Python in-process is possible
(statically linked CPython in a `.so` inside the sniper runtime) but pointless: keep the game-side logic thin Lua
and put the intelligence in the external harness (Python or Node), connected over TCP.

Prior art: no Frida-based BG3 tooling was found (searched 2026-10-09). bg3se is not portable as a whole (Detours,
MSVC extensions), but bg3le demonstrates its **GameDefinitions and Lua layer are** portable with clang flags
`-fdeclspec -fms-extensions -fdelayed-template-parsing`, libc++ static, and four hook primitives (PLT interposition,
verified vtable-slot patch, `call rel32` → trampoline patch, `DetourAttachEx` emulation via exported forwarders).

Patch survival: bg3le resolves every target **by symbol, by byte pattern (wildcarded rel32 and RIP
displacements), or by the code that uses it**, refuses non-unique matches, disables only the affected feature,
and caches results per GNU build ID in `~/.cache/bg3le/`. It ships `tools/make-sigs.py OLD NEW name=kind:0xADDR`
for re-deriving patterns, and old builds can be fetched with `download_depot 1086940 2330359 <manifest>`. This is
a better patch story than a hand-rolled injector would get.

---

## 6. Pure Osiris (story) mods: no SE at all

* A `.pak` containing `Mods/<Mod>/Story/RawFiles/Goals/*.txt` (and `story_header.div` additions) is merged into
  the story when a save loads (`COsiris::Merge/Compile` are the engine entry points). It runs natively on Linux
  with no extender.
* **Can do:** react to every Osiris event (`CombatStarted`, `TurnStarted`, `DialogStarted`, `UsingSpellOnTarget`,
  `TextEvent`, ...), call every built-in call and query (move, attack, cast, give items, start dialogue, rest, set
  flags), and keep databases. In principle it could play the game *if* it had an input channel.
* **Cannot do any I/O.** Osiris has no file, socket, clock or process access. Its only outputs are game-state
  changes and the Osiris debug log (`COsiris::OpenLogFile`), which needs dev or log flags and has no read-back
  channel (**UNVERIFIED** whether the retail native build exposes a switch for it). Its only inputs are game
  events. It cannot read component data, translated strings, UI state or anything the story does not track.
  Hacky side channels (in-game chat via `TextEvent` console commands, save file contents) are not viable.
* Verdict: pure Osiris cannot serve as the bridge. It is still useful **inside** our SE mod for things Osiris
  already does well, such as a few helper PROCs or reacting to story-only events. Lua's `Osi.*` and
  `RegisterListener` cover that without writing goal files.

---

## Recommendation

**(C) Hybrid, specifically: native Linux + bg3le (pinned, built from source) as the primary runtime, Proton +
Windows build + official bg3se as the fallback, with one shared Lua mod and one shared TCP IPC client.**

Why not (A) Proton + bg3se as primary:
* It is the most mature and complete API (Norbyte, v32/v33, `Ext.UI.Defer`) and is fully functional. But it means
  switching away from the build that is installed and working here, re-downloading the game, separate saves in
  a Wine prefix, a launcher, and screenshot capture of a Wine/Vulkan window under Wayland. It also keeps every
  harness path inside `compatdata/pfx/`. None of this is fatal, which is why it is the fallback.

Why not (B) our own injector:
* The Larian code has no function symbols. ECS and Osiris internals need bg3se's three years of layout work.
  bg3le *is* that injector, done well with a strong patch-survival design. Rebuilding it is months of work for a
  worse result.

Why bg3le as primary:
1. It runs on the exact build installed here (v4.76.31.656), natively, with no reinstall.
2. Upstream API parity means our mod is plain bg3se Lua (`Osi.*`, `Ext.Entity`, `Ext.Loca`, `Ext.UI`,
   `Ext.Level`), which **runs unchanged on official bg3se under Proton** if bg3le breaks.
3. The built-in **TCP eval server on 127.0.0.1:9998** is the IPC we need, wire-compatible with bg3se's debugger
   (`EnableLuaDebugger`), so the same harness client works for both runtimes.
4. The native plugin API gives a clean place for mouse injection and frame capture later.

Risk controls (do these from day one):
* **Pin everything.** Build bg3le from a specific commit in our repo (vendor or submodule, clang + libc++,
  `tools/build-sniper.sh` in Docker). Set Steam's BG3 auto-update to "only update when launched" and launch through
  our harness, keeping `download_depot` manifests so a game patch is never silent. On a game update, verify that the
  `~/.cache/bg3le/` resolve log has no `WARNING: resolve:` lines before playing.
* Install by adding the bg3le wrapper ourselves (`<root>/bin/bg3le-launch %command%`), or launch via bg3le's
  `run-native.sh` from the harness. Note that `install.py` rewrites Steam's `localconfig.vdf` and needs Steam
  closed.
* Keep the game-side Lua thin: state snapshots with fairness filtering on the server and client, an action queue,
  and an event ring buffer. Use `AIH.rpc` for short, non-blocking calls with job ids for long actions, and stay
  well under bg3le's 10 s eval timeout.
* Avoid Noesis writes on bg3le until `Ext.UI.Defer` is ported. Prefer Osiris and ECS for actions and
  `GetDialogManager` and dialogue components for dialogue. Use `ICommand:Execute` only where nothing else exists
  (main menu, level-up, trading). Wrap each call in `pcall`.
* Keep a smoke-test script (`tools/check-api.sh`-style, plus our own RPC checks) that runs at the start of each
  session, and fall back to Proton + bg3se if it fails.
* Contribute fixes upstream to bg3le. It is a one-person project, so our fixes keep it alive.

Next step: build bg3le at `3728a68` (v0.3.6), launch once from a harness script, connect a Python client to
:9998, and evaluate `return Ext.Utils.GameVersion()` (client context) and, after loading a save,
`return Osi.GetHostCharacter()` (server context). That single experiment confirms or rules out the plan.

---

### Sources
* bg3se repo, docs and release notes: https://github.com/Norbyte/bg3se (cloned `91abbf2`, 2026-10-08):
  `Docs/API.md`, `Docs/Debugger.md`, `Docs/ReleaseNotes.md`, `BG3Extender/Lua/Libs/ClientInput.inl`,
  `BG3Extender/Lua/Libs/ClientUI/Module.inl`, `BG3Extender/GameDefinitions/PropertyMaps/UI.inl`,
  `BG3Extender/Lua/Debugger/LuaDebug.proto`, `BG3Extender/Lua/Debugger/LuaDebugger.cpp`
* bg3se releases: https://github.com/Norbyte/bg3se/releases, https://github.com/Norbyte/bg3se/releases/tag/v32,
  https://github.com/Norbyte/bg3se/releases/tag/v31
* bg3le: https://github.com/lenonk/bg3le (cloned `3728a68`, v0.3.6, 2026-10-08): README, CHANGELOG,
  `include/bg3le_plugin.h`, `src/debug_server.cpp`, `src/ext_libs.cpp`, `installer/bg3le-launch`,
  `reference/COMPUTED-MEMBERS.md`, `reference/upstream-api.txt`, `vendor/NOTICE.md`
* bg3le forks and announcement: https://github.com/AaronJordison/bg3le, https://github.com/igortarasuk/bg3le,
  https://github.com/Norbyte/bg3se/issues/610, https://www.nexusmods.com/baldursgate3/mods/25431
* Native Linux build: https://www.gamingonlinux.com/2025/09/baldurs-gate-3-gets-a-native-linux-version-to-improve-it-on-steam-deck/,
  https://www.notebookcheck.com/Baldur-s-Gate-3-gibts-ab-sofort-als-native-Steam-Deck-Version-mit-Upgrades-fuer-Performance-und-Ladezeiten.1122107.0.html
* SE install wiki: https://wiki.bg3.community/Tutorials/Mod-Use/How-to-install-Script-Extender; ProtonDB:
  https://www.protondb.com/app/1086940/; Linux guide: https://steamcommunity.com/sharedfiles/filedetails/?id=3477750360
* Local inspection: `nm -C`, `nm -D`, `readelf -d/-n`, `strings` on `bin/bg3` and `bin/libOsiris.so` (read-only).
