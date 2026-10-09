# 03 — The Action Layer: how an external AI performs every player action in BG3

**Scope.** This report covers how an agent can do everything a human player can do in Baldur's Gate 3: programmatically, fairly (same rules, costs and information as a player), and reliably. It compares four mechanisms:

- **(a) Osiris** calls and queries through the Script Extender's `Osi.*`.
- **(b) Engine/ECS-level requests** through `Ext.System`, `Ext.Entity` and `Ext.Input`.
- **(c) Driving the game's own Noesis UI.** This means invoking the `ICommand`s behind the buttons.
- **(d) OS- or process-level mouse and keyboard injection.**

It then maps every action category to the best mechanism, proposes the agent's tool API, and explains how to confirm that an action actually happened.

Date: 2026-10-09. Target: native Linux build, Steam buildid 25605617, `Patch8_HotFix10`. The binary reports version string `4.1.1.7631656`.

Legend for ratings:

- **Fair**: does the action go through the same rules and costs as a player? ✅ yes, ⚠️ partly or bypassable, ❌ bypasses rules.
- **Rel**: reliability. H, M or L.
- **Eff**: implementation effort. L, M or H.

---

## 0. TL;DR

1. **The UI path is fair by construction.** Every BG3 player action is ultimately a Noesis `ICommand` on a `gui::DC*` DataContext, or a world click. I extracted all 310 XAML files from the local `Game.pak` and mapped the exact command names, for example `SelectAnswerCommand`, `EndTurn`, `InterruptReactCommand`, `RollAgainCommand`, `TradeItemCommand`, `ProcessRestCommand`, `GotoWaypoint`, `FinishLevelUp`, `QuickSaveCommand` and `LaunchUpcastCommand`. bg3se's Lua bindings expose `Execute()` and `CanExecute()` on the game's own `ICommand` objects (verified in `BG3Extender/Lua/Libs/ClientUI/NsHelpers.inl`). Invoking a command sends the same network request the button would, so server-side validation and costs apply.
2. **Osiris (`Osi.*`) is the story scripting API, and it is not fair by default.** The official modding wiki says `UseSpell` "will always ignore any pre-conditions (… spells that the character does not have … or does not have resources to cast)". It is still the best mechanism for movement (`CharacterMoveTo` / `CharacterMoveToPosition`), item interactions (`Use`, `Pickup`, `ToInventory`, `Equip`) and party control (`MakePlayerActive`, `AttachToPartyGroup`). For those, the player-equivalent rule set is small and can be enforced with a guard layer in our own mod.
3. **For spells and attacks, the fairest programmatic route is the engine's own cast request.** That is a `CastStartRequest` with no `Ignore*` flags, placed on the network or request path (`Ext.System.ServerCastRequest`). This is the path a client spell cast takes (`NETMSG_SPELL_CAST_CAST`). An acceptable alternative is the UI hotbar `UseSlotCommand` followed by a world-target click. Plain `Osi.UseSpell` should only be used behind a strict pre-validation guard. Whether `UseSpell` deducts resources is **UNVERIFIED**, and it must be tested.
4. **Platform reality.** Official bg3se does **not** run on the native Linux build. Two options exist:
   - **bg3le** is a third-party native-Linux port built on bg3se, announced in bg3se issue #610 on 2026-09-30. Its forks disagree on how complete `Ext.UI` (Noesis) is. One fork says `Ext.ClientUI` is "blocked on placeholder Noesis RTTI". Another says "`Ext.UI` runs on the game's own Noesis … input events and key injection".
   - **Proton plus official bg3se** on the Windows build.

   The project must pick one. That choice gates mechanism (c).
5. **The universal fallback should be in-process SDL event injection, not Wayland-level injection.** Use an `LD_PRELOAD` shim that interposes the game's `SDL_PollEvent` and `SDL_GetGlobalMouseState`. These are dynamic imports of the `bg3` ELF (verified with `nm -D`). This avoids the Wayland and KWin input-emulation mess. `xdotool` doesn't work on Wayland, `ydotool`/uinput needs root-ish setup, and libei/portal needs an interactive prompt.
6. **Recommended layering:**
   - UI commands (c) for all menu-like decisions: dialogue, rolls, reactions, trade, level-up, character creation, rest, save/load, waypoints.
   - Engine cast requests (b) or UI hotbar + click for combat actions.
   - Guarded Osiris (a) for movement, item interaction and party management.
   - SDL injection (d) as a universal fallback, plus camera, cutscene skipping and anything unmapped.

   Confirm every action through Osiris events or ECS one-frame components, never by assuming success.

---

## 1. Platform facts that constrain the action layer

| Fact | Evidence | Consequence |
|---|---|---|
| Native Linux ELF, unstripped, `libOsiris.so` shipped | BRIEF; `nm`/`strings` on `bin/bg3` | Symbols make hooking easy. Osiris lives in its own `.so`. |
| Version string in binary: `4.1.1.7631656` | `strings bin/bg3` (line 23383 of dump) | bg3le (AaronJordison fork) says its layouts match "`v4.76.31.656`" and that "hotfix `v4.76.31.656` moved most functions and bg3le re-found its targets without a code change". This looks like the same build written differently (7631656 → 76.31.656). **UNVERIFIED** match. |
| Official bg3se is Windows-only | bg3se issue #225; bg3le README ("Linux players have had to run the Windows build under Proton") | Either use bg3le on native, or run the Windows build under Proton (Proton 11 / Experimental are installed). |
| bg3le Osiris: works | igortarasuk/bg3le README: "`Osi.*` and bare-global helpers are callable … 19,078 procedures and databases are callable"; `Ext.Osiris.RegisterListener` fires | Mechanism (a) is available on native Linux today. |
| bg3le `Ext.Entity` / `Ext.Net`: work (some field kinds missing) | both bg3le READMEs | Most of mechanism (b) is available. |
| bg3le `Ext.UI` (Noesis): **disputed** | igortarasuk fork: "`Ext.ClientUI` is blocked on placeholder Noesis RTTI". AaronJordison fork: "`Ext.UI` runs on the game's own Noesis … missing hit testing and visual transforms". | Mechanism (c) on native Linux is **UNVERIFIED**. It must be smoke-tested first (see §8). If it fails, use Proton + official bg3se for (c), or fall back to (d). |
| bg3le `Ext.Level` pathfinding: disputed | igortarasuk: "24 [refusing] in `Ext.Level` (physics and pathfinding)". AaronJordison: "`Ext.Level` provides physics queries, the AI grid, pathfinding". | Path previews (cost of a move in metres) may need to wait. |
| Game links SDL2 dynamically and imports `SDL_PollEvent`, `SDL_GetGlobalMouseState`, `SDL_WarpMouseInWindow` | `nm -D --undefined-only bin/bg3` | `LD_PRELOAD` interposition of input is trivial (§5.3). |
| Raw Osiris story goals ship in the paks: 946 `.txt` goals in `Gustav.pak`/`Shared.pak`/`GustavX.pak` | Extracted with a small LSPK v18 reader (`lz4` file list) | Story rules can be read to see how the game handles player-initiated events, e.g. `DialogStartRequested` → `QRY_SelectAndStartDialog` in `Mods/Shared/Story/RawFiles/Goals/__GLOBAL_Dialogs.txt:2167`. |

---

## 2. Mechanism (a): Osiris via `Osi.*`

### 2.1 How it works

From bg3se `Docs/API.md`:

- Server context only. "Lua server contexts have a special global table called `Osi` that contains every Osiris symbol." Built-in calls, queries and events are also globals.
- Calls: `Osi.CharacterResetCooldowns(player)`.
- Queries return a boolean when they have no OUT args, otherwise the OUT values: `local x, y, z = GetPosition(player)`.
- Events can be *raised* from Lua and run synchronously through every story rule: `StoryEvent(player, "event name")`.
- Procs and user queries: `Osi.PROC_Foo(...)`, `Osi.QRY_Foo(...)`. Databases: `Osi.DB_X:Get(nil, ...)`, `:Delete(...)`, `Osi.DB_X(a, b)` to insert.
- Listening: `Ext.Osiris.RegisterListener(name, arity, "before"|"after", handler)`. "Capture support for built-in calls will be added in a later version", so you can listen to *events* and queries but not to calls.
- `Osi.Something ~= nil` is always true, because names resolve lazily at call time. Don't use it to test whether a symbol exists.

**Osiris "tasks".** Movement and interaction calls queue an `OsirisTask` on the character's Osiris controller. bg3se `GameDefinitions/Controllers/OsirisTask.h` lists `OsirisMoveToLocationTask`, `OsirisMoveToObjectTask`, `OsirisPickupItemTask`, `OsirisUseItemTask`, `OsirisMoveItemTask`, `OsirisUseSpellTask`, `OsirisMoveToAndTalkTask`, `OsirisSteerTask`, `OsirisTeleportToLocationTask` and others. `PurgeOsirisQueue(_Char)` / `PurgeOsirisQueue(_Char, 0)` cancels them (used this way in the shipped story, and the event is `QueuePurged`). This is the "NPC scripting" layer. It works on player characters too: the shipped story calls `CharacterMoveTo(` 468 times and `MakePlayerActive(` 42 times. But it was designed for scripted scenes, **not** for player-equivalent actions.

`OsirisUseSpellTask` carries `uint8_t SpellCastOptions`, and the engine's `SpellCastOptions` bitmask includes `IgnoreHasSpell=0x1`, `IgnoreCastChecks=0x2`, `IgnoreSpellRolls=0x4`, `IgnoreTargetChecks=0x200`, `Forced=0x2000` and `FromClient=0x10000` (bg3se `Enumerations/ECS.inl:2195`). The wiki says Osiris casts ignore preconditions. Taken together, this suggests Osiris sets the `Ignore*` flags. That is an inference and **UNVERIFIED**.

### 2.2 Signatures (exact)

Where the official modding wiki (docs.baldursgate3.game) has a "Full definition", it is quoted. Otherwise the arity and types come from the HF4 function dump (Liareth gist). Parameter names in *italics* are taken from call sites in the shipped story `.txt` files.

| Purpose | Signature | Source |
|---|---|---|
| Move to entity | `CharacterMoveTo((CHARACTER)_Character, (GUIDSTRING)_Target, (STRING)_MovementSpeed, (STRING)_Event [, (INTEGER)_MoveID])`. Speed is `"Walk"` or `"Run"`. `_Event` is raised for the character on arrival. | wiki |
| Move to point | `CharacterMoveToPosition(_Char, (REAL)_X, _Y, _Z, (STRING)_MovementSpeed, (STRING)_Event [, (INTEGER)_MovementID])` | gist (`None, UnknownType, Real, Real, Real, String, String, Integer`) + story |
| Move and talk | `CharacterMoveToAndTalk(_Character, _Target, (DIALOGRESOURCE)_Dialog, _MoveEvent, _MovementSpeed, (REAL)_TimeOut)`. Events: `CharacterMoveToAndTalkRequestDialog`, `CharacterMoveToAndTalkFailed`. | gist + story |
| Cancel task queue | `PurgeOsirisQueue(_Char [, (INTEGER)_?])`. Event: `QueuePurged`. | gist + story |
| Cast on target | `UseSpell((GUIDSTRING)_Caster, (STRING)_SpellID, (GUIDSTRING)_Target [, (GUIDSTRING)_Target2 [, (INTEGER)_WithoutMove]])`. **"Casting a spell from Osiris will always ignore any pre-conditions."** | wiki |
| Cast at point | `UseSpellAtPosition((GUIDSTRING)_Caster, (STRING)_Spell, (REAL)_X, _Y, _Z)`. The story uses it for `"Projectile_Jump"`. | gist + story |
| Weapon attack (NPC style) | `Attack((CHARACTER)_Attacker, (GUIDSTRING)_Target, (INTEGER)_?)` | gist + story |
| Use item / interact | `Use((CHARACTER)_Character, (ITEM)_Item, (STRING)_Event)` / `Use(_Character, _Item, (INTEGER)_UseItem, (INTEGER)_IsInteraction, (STRING)_Event)`. Levers, doors and chairs work with this. Defaults: `_IsInteraction=0`, `_UseItem=1`. | wiki |
| Pick up | `Pickup((CHARACTER)_Character, (ITEM)_Item, (STRING)_Event, (INTEGER)_ForcePickUpOnFailure)`. Walks there. Event `PickupFailed`. Cancellable with `PurgeOsirisQueue`. | wiki |
| Move item | `ToInventory((GUIDSTRING)_Object, (GUIDSTRING)_TargetObject [, (INTEGER)_Amount [, _ShowNotification [, _ClearOriginalOwner]]])`. `_ClearOriginalOwner=1` launders stolen items, so **never set it**. | wiki |
| Equip / unequip | `Equip((CHARACTER)_Character, (ITEM)_Item [, _AddToMainInventoryOnFail [, _ShowNotification [, _ClearOriginalOwner]]])` "provided the character meets the prerequisites". `Unequip(_Character, _Item)`. Events: `Equipped`, `EquipFailed`, `Unequipped`, `UnequipFailed`. | wiki |
| Camp chest | `SendToCampChest((GUIDSTRING)_Item, _HostPlayer)` | gist + story |
| Open/close/unlock | `Open(_Item)`, `Unlock(_Door [, _Key?])`, `Lock(_Item, (STRING)_?)`. **Cheats**: these bypass the lock, the key and the roll. | gist + story |
| End turn | `EndTurn((GUIDSTRING)_Target)`. "Immediately ends the turn of `_Target`." Events: `TurnEnded`, `TurnStarted`. | wiki |
| Turn-based mode | `ForceTurnBasedMode(_Char, (INTEGER)_Enable)`. Query `IsInForceTurnBasedMode`. Events `EnteredForceTurnBased`, `LeftForceTurnBased`. | gist |
| Select character | `MakePlayerActive((CHARACTER)_Target)`. "Makes `_Target` the currently controlled/focused character for its owning user." Event `CharacterSelectedForUser`. | wiki |
| Chain / unchain | `AttachToPartyGroup(_Player, _Leader)`, `DetachFromPartyGroup(_Player)`. Events `AttachedToPartyGroup`, `DetachedFromPartyGroup`. Query `InSamePartyGroup`. | gist + story |
| Ability check (scripted) | `RequestActiveRoll((CHARACTER)_Roller, (GUIDSTRING)_RollSubject, (STRING)_RollType, (STRING)_SkillOrAbility, (DIFFICULTYCLASS)_DifficultyClassID, (INTEGER)_RollerAdvantage, (STRING)_Event)`. Opens the roll window. Event `RollResult`. | wiki |
| Long rest | `RequestLongRest((CHARACTER)_Initiator, (INTEGER)_IsForced)`. Events `LongRestStarted`, `LongRestStartFailed`, `LongRestCancelled`, `LongRestFinished`. | wiki |
| Go to camp | `RequestGatherAtCamp(_Player)`. Events `RequestGatherAtCampSuccess`, `RequestGatherAtCampFail`, `TeleportedToCamp`. | gist + story |
| Waypoint UI | `OpenWaypointUI((CHARACTER)_Character, (STRING)_CurrentWaypoint, (ITEM)_Item, (INTEGER)_IsFleeing)`. Opens the menu only. Travel raises `TeleportToWaypoint(_Player, _Trigger)` (event). | wiki + story |
| Trade UI | `ActivateTrade((CHARACTER)_Player, (CHARACTER)_Trader, (INTEGER)_CanSell, (TRADEMODE)_TradeMode, (GUIDSTRING)_InitiallySelectedTradeObject, (STRING)_ItemsTagFilter)`. Opens the UI. Events `RequestTrade`, `TradeEnds`. | wiki |
| Respec / mirror | `StartRespec((GUIDSTRING)_Target)`, `StartRespecToSubclass(_Target, _Subclass)`, `StartChangeAppearance(_Player)`. Each opens the CC UI. Events `RespecCompleted`, `RespecCancelled`. | wiki + story |
| Character creation | `StartCharacterCreation()`, `NotifyCharacterCreationFinished()`. Events `CharacterCreationStarted`, `CharacterCreationFinished`. | gist |
| Book | `OpenCustomBookUI(_Char, (STRING)_BookId)`. Event `CustomBookUIClosed`, `GameBookInterfaceClosed`. | gist |
| Save | `AutoSave()` | gist |
| Dialogue start (story) | User query `QRY_StartDialog_Fixed((DIALOGRESOURCE)_Dialog, _Speaker1 [, _Speaker2 …])` (574 uses) and `QRY_StartDialog(...)`. The engine-side player request is the **event** `DialogStartRequested(_Npc, _Player)`, which story handles via `QRY_SelectAndStartDialog(_Npc, _Player)` (`__GLOBAL_Dialogs.txt:2167`). | shipped story |
| Dialog stop | `DialogRequestStop(_Speaker)` | gist |

Useful completion events from the dump: `UseStarted`, `UseFinished`, `Opened`, `Unlocked`, `StartedLockpicking`/`StoppedLockpicking`, `StartedDisarmingTrap`/`StoppedDisarmingTrap`, `CharacterLootedCharacter`, `DialogStarted(_Dialog, _InstanceID)`, `DialogEnded`, `DialogRollResult`, `CastSpell`, `CastedSpell`, `CastSpellFailed`, `UsingSpell…`, `StartAttack`, `AttackedBy`, `TurnStarted`, `TurnEnded`, `CombatStarted`, `CombatEnded`, `CombatRoundStarted`, `ReactionInterruptActionNeeded`, `ReactionInterruptUsed`, `ShortRested`, `LongRestFinished`, `Teleported`, `TeleportToWaypoint`, `TemplateAddedTo`/`AddedTo`, `RemovedFrom`, `Equipped`, `GoldChanged`, `TadpolePowerAssigned`, `ApprovalRatingChanged`, `SavegameLoaded`, `LevelGameplayStarted`, `CharacterMoveToCancelled`, `CharacterMoveFailedUseJump`.

### 2.3 Fairness verdict for Osiris

- **Unfair by default** for casting, opening and unlocking, applying statuses, teleporting and `ToInventory` with `_ClearOriginalOwner=1`.
- **Essentially fair** for `CharacterMoveTo*`, provided we compute paths and check movement budgets ourselves in turn-based mode. Whether Osiris-driven movement in combat spends the character's Movement resource is **UNVERIFIED**, so it must be tested. Also fair: `MakePlayerActive`, `AttachToPartyGroup`/`DetachFromPartyGroup`, `Pickup` (walks there; ownership flags stay intact), `Equip`/`Unequip` (prerequisites checked; costs nothing outside combat, but in combat equipping armour or swapping is restricted and Osiris may not apply those restrictions, **UNVERIFIED**), and `RequestLongRest(_,0)` (non-forced, normal conditions apply).
- `EndTurn` is fair: ending your own turn early is always legal.
- **Never use**: `TeleportTo*`, `ApplyStatus`, `Open`/`Unlock` (except after a legitimate key or lockpick success), `AddGold`, `TemplateAddTo`, `Forced` flags, `UseSpell` without the guard (§7).

---

## 3. Mechanism (b): engine/ECS requests through Script Extender

### 3.1 What exists in the engine (from `strings bin/bg3` and bg3se headers)

**Client→server network messages** (324 `NETMSG_*` in the binary). These are exactly the UI's channel. Relevant ones:

| Message | What it carries |
|---|---|
| `NETMSG_SPELL_CAST_START_PREVIEW`, `_UPDATE_PREVIEW`, `_CAST`, `_CONFIRM`, `_CANCEL` | Spell casting, attacks, jump, shove, dash, throw (all are "spells" in BG3) |
| `NETMSG_TURNBASED_ENDTURN_REQUEST`, `NETMSG_TURNBASED_CANCEL_ENDTURN_REQUEST`, `NETMSG_TURNBASED_FLEE_REQUEST` | End turn, flee |
| `NETMSG_FORCE_TURN_BASED_TOGGLE_REQUEST`, `NETMSG_FORCE_TURN_BASED_END_PLAYER_TURN_REQUEST` | Turn-based mode |
| `NETMSG_INTERRUPT_DECISION`, `NETMSG_INTERRUPT_SET_ASK_DATA`, `NETMSG_INTERRUPT_SET_ENABLED_DATA` | Reactions (Shield, Counterspell, Hellish Rebuke, AoO) |
| `NETMSG_DIALOG_ANSWER_MESSAGE`, `NETMSG_DIALOG_LISTEN`, `NETMSG_TIMELINE_REQUEST_FASTFORWARD` | Choose a dialogue option, listen in, skip a line |
| `NETMSG_REQUESTED_ROLL`, `NETMSG_ACTIVE_ROLL_MODIFIERS`, `NETMSG_SELECT_DICE` | Skill-check rolls, Guidance/Bardic/Inspiration modifiers |
| `NETMSG_CHARACTER_PICKUP`, `NETMSG_CHARACTER_LOOT`, `NETMSG_CHARACTER_LOCKPICK`, `NETMSG_CHARACTER_TRAPDISARM`, `NETMSG_CHARACTER_PICKPOCKET`, `NETMSG_CHARACTER_DROP_ENTITY`, `NETMSG_USE_ITEM_ACTION`, `NETMSG_ITEM_USE_REMOTELY`, `NETMSG_ITEM_ACTION` | Interact, loot, use |
| `NETMSG_INVENTORY_EQUIP`, `_UNEQUIP`, `_TRANSFER`, `_MOVETOSLOT`, `_SORT`, `_LOCK` | Inventory |
| `NETMSG_TRADE_ACTION`, `NETMSG_MULTITRADE_ACTION`, `NETMSG_DONATE_ACTION` | Trade/barter |
| `NETMSG_PARTYGROUP`, `NETMSG_PARTYORDER`, `NETMSG_PARTY_BLOCKFOLLOW` | Chain/unchain, order |
| `NETMSG_SHORT_REST`, `NETMSG_LONG_REST_DECISION`, `NETMSG_END_THE_DAY`, `NETMSG_READYCHECK` | Resting and camp |
| `NETMSG_TELEPORT_WAYPOINT`, `NETMSG_UNLOCK_WAYPOINT` | Fast travel |
| `NETMSG_CHARACTER_CREATION_UPDATE`, `_LEVELUP`, `_RESPEC`, `_CHANGEAPPEARANCE`, `_READY`, `NETMSG_LEVEL_UP_UPDATE`, `NETMSG_FULL_RESPEC_UPDATE`, `NETMSG_SPELL_PREPARE`, `NETMSG_SPELL_LEARN` | CC, level-up, spell prep, learning scrolls |
| `NETMSG_TADPOLE_POWER_ADD` | Illithid powers |
| `NETMSG_SAVEGAME`, `NETMSG_REQUESTAUTOSAVE` | Saving |
| `NETMSG_CHOOSE_REWARD`, `NETMSG_GIVE_REWARD`, `NETMSG_PASSIVE_TOGGLE`, `NETMSG_DUALWIELDING_TOGGLE`, `NETMSG_CHARACTER_REQUEST_WEAPON_SET_SWITCH`, `NETMSG_REQUEST_RELEASE_CONCENTRATION`, `NETMSG_COMBINE_ITEMS` | Misc. player actions |

bg3se does **not** expose a way to send the game's own `NETMSG_*` packets. `Ext.Net` is its own mod channel. Constructing these packets would need custom native hooks, which is high effort and fragile. Treat them as **documentation of what the UI does**, which is useful when reading `ecl::` code.

**Server-side request queues reachable from Lua** (`Ext.System.*`, bg3se `ExtIdeHelpers.lua` `ExtSystemType`):

- `Ext.System.ServerCastRequest` (`esv::spell_cast::CastRequestSystem`). It has separate queues: `AnubisCastRequests`, `OsirisCastRequests`, **`NetworkStartRequests`** (the player path), `ReactionStartRequests`, `ItemStartRequests`, `JumpStartRequests`, `CommandProtocolStartRequests`, plus matching `*CancelRequests`. Each `CastStartRequest` = `{Spell: SpellId, CastOptions: SpellCastOptions, Caster, Targets: InitialTarget[], Originator: ActionOriginator, Item, CastPosition?, StoryActionId, NetGuid, RequestGuid}` (`GameDefinitions/Components/SpellCast.h:260`).
- `Ext.System.ServerInterruptDecision` (`esv::interrupt::DecisionSystem`), with `Decisions: HashMap<EntityHandle, InterruptDecision>`. The client-side counterpart component is `ecl::interrupt::PlayerDecisionComponent`.
- Also `ServerShortRest`, `ServerLongRest`, `ServerInventoryInteractionRequest`, `ServerInventoryEquipment`, `ServerParty`, `ServerProgression`, `ServerSpellLearning`, `ServerRoll`, `ServerTurnOrder`, `ServerCombat`, `ServerDialog`.

**ECS request components visible in the binary**, which are the engine's internal action requests:

- `esv::pickup::PickUpRequestComponent`
- `esv::lock::ActiveRollRequestOneFrameComponent` and `esv::lock::SpendActionResourceRequestOneFrameComponent` (lockpicking costs actions)
- `esv::trap::ActiveRollRequestOneFrameComponent`
- `esv::jump::InitJumpRequestOneFrameComponent`
- `esv::rest::RestTypeRequestOneFrameComponent`
- `esv::camp::ReadyCheckRequestOneFrameComponent`
- `esv::active_roll::StartRequestOneFrameComponent`
- `ecl::RequestedEndTurnOneFrameComponent`
- `ecl::ftb::ToggleRequestComponent`
- `esv::inventory::InteractionRequestsComponent`
- `esv::one_time_reward::SendToCampChestRequestOneFrameComponent`

### 3.2 Engine cast request: the fair way to cast

Push a `CastStartRequest` into `Ext.System.ServerCastRequest.NetworkStartRequests`, or into the caster's `ServerSpellCastRequests.StartRequests`, with `CastOptions` = `FromClient` and **none** of `IgnoreHasSpell | IgnoreCastChecks | IgnoreSpellRolls | IgnoreTargetChecks | Forced`. This makes the engine run its normal validation: has the spell, resources, range, line of sight, target conditions. The spell then runs the normal reaction window, and Counterspell or Shield can trigger. Assessment: **Fair ✅ (if flags respected), Rel M, Eff M–H.**

- **UNVERIFIED.** Field layouts change per patch. Whether appending to those arrays from Lua is supported (bg3se arrays are writable, but the system may consume them before or after the Lua tick) needs a test. The `Originator` must probably be filled with a `ActionOriginator{ActionGuid, PassiveId, InterruptId, CanApplyConcentration}` matching what a client cast creates.
- **Upcasting** works by passing the upcast variant's `SpellId` (`Prototype` = e.g. `Projectile_MagicMissile_3`, `OriginatorPrototype` = base). Verify against what the hotbar's `LaunchUpcastCommand` produces.
- **Move-then-cast.** The player UI automatically paths into range and that movement costs movement. With `NoMovement` (0x10) off, the engine does the same.

### 3.3 Other ECS-level APIs

- `Ext.Input.InjectKeyPress(SDLScanCode, SDLKeyModifier?)`, `InjectKeyDown` and `InjectKeyUp` are client-side. They push a real `SDL_KEYDOWN`/`SDL_KEYUP` into the game's SDL queue (`Lua/Libs/ClientInput.inl`). There is **no** mouse injection. `Ext.Input.GetInputManager()` exposes the key-binding map, so you can look up the key bound to `UIEndTurn` instead of hardcoding it.
- `Ext.UI.GetPickingHelper(playerId)` returns `Selection` (entity under cursor), `Inner.WorldPosition` and `WindowCursorPos`. Use it to verify that the cursor is over the intended target before an injected click.
- `Ext.Level.BeginPathfinding`, `FindPath`, `GetPathById` and the raycast/sweep functions give path length for movement budgeting and line-of-sight checks. Their availability on bg3le is disputed.
- `Ext.Utils.GetDialogManager()` is server-side. It exposes dialog instances and **hidden flag state**, so it is fairness-dangerous. Use it for nothing the player can't see.

---

## 4. Mechanism (c): driving the Noesis UI

### 4.1 API (bg3se)

- `Ext.UI.GetRoot()` is the Noesis root visual. You must wrap access in `Ext.UI.Defer(fn)`: "code that uses Noesis objects … must be done via `Ext.UI.Defer()`".
- `element:Find("Name")`, `.DataContext`, `:VisualChild(i)`, `.VisualParent`, and reflection over properties.
- **`ICommand:Execute(param?)` and `:CanExecute(param?)` on game-owned commands.** These are confirmed in `BG3Extender/Lua/Libs/ClientUI/NsHelpers.inl` lines 660–678 (`CommandHelpers::Execute`, `InterfaceExecute`, `InterfaceCanExecute`). The only special-casing there is a deferral for five mod-browser commands (`EndorseModCommand` etc., line 367).
- `Ext.UI.RegisterType` / `Ext.UI.Instantiate("se::Name", wrapped)` wrap a DataContext to intercept commands. This is useful for *observing* what the human or UI does, but not needed for invoking.
- `Ext.UI.GetStateMachine()` and `Ext.UI.SetState(state, subState?, clearState?, playerId?)` drive the UI state machine. This opens and closes panels like the inventory, map and journal.
- The UI is client-side. The command handler sends the server request, so **server rules apply**. This is what makes (c) fair.

### 4.2 DataContexts present in the binary

From the `gui::` strings in `bin/bg3`:

`gui::DCDialogue`, `DCActiveRoll`, `DCReactionDecision`, `DCTrade`, `DCLongRest`, `DCHotBar`, `DCContainerInventory`, `DCCharacterCreation`, `DCCharacterLevelUp`, `DCCharacterFullRespec`, `DCLearnSpells`, `DCTadpolePowersTree`, `DCSavegames`, `DCGameMenu`, `DCMainMenu`, `DCBook`, `DCCombine`, `DCAlchemy`, `DCPickpocket`, `DCRewardPanel`, `DCSortInventory`, `DCCombatants`, `DCFTBParticipants`, `DCNotifications`, `DCTutorialPopup`, `DCNewSavegamePopup`, `DCSelectionFlyOut`, `DCCharacterPanels`, `DCLoadingScreen`.

ViewModels: `gui::VMDialogue`, `VMDialogueAnswer`, `VMCharacter`, `VMItem`, `VMWaypointGroup`, `VMSpellPrepare`, `VMProgressionGroup`, `VMFeatures`, `VMSplitItemData`, `VMLevel`.

### 4.3 Command map: button → command

Extracted from the XAML in `Game.pak`, which I extracted locally. Paths are `Mods/MainUI/GUI/Pages/*.xaml` (`*_c.xaml` = controller variant) and `Public/Game/GUI/Library/*.xaml`.

| Screen (XAML) | Commands bound in XAML (exact names) | Parameter |
|---|---|---|
| **Dialogue** (`DialogueTemplates.xaml:112`, `Dialogue.xaml`) | `SelectAnswerCommand` (answer button), `HighlightAnswerCommand` (hover), `ShowAnswersCommand`, `StopListeningCommand`, `AttackCommand` (in-dialogue attack), `StartTrade`, `DialogueAllowSpeakerSelectionChangedCommand`, `SelectedCharacterActiveDialogueChangedCommand`, `SelectorUp/DownCommand` | `SelectAnswerCommand` takes `{Binding .}`, i.e. the `VMDialogueAnswer` item from the answers collection |
| **Active roll** (`ActiveRoll.xaml`, `ActiveRoll_c.xaml`) | `UserTriggeredNextPhaseCommand` / `TriggerNextPhaseCommand` (roll / continue), `ContinueCommand`, `CancelCommand`, `AddModifierCommand` / `RemoveModifierCommand` (Guidance, Bardic Inspiration, etc.), `RollAgainCommand` (spend Inspiration), `TryAgainCommand`, `SelectDiceTypeCommand`, `DiscardRollCommand` (binary) | the modifier VM for Add/Remove |
| **Reaction prompt** (`ReactionDecisionPopup.xaml`) | `InterruptReactCommand`, `InterruptAcceptPlayerCommand`, `InterruptDontReactPlayerCommand`. Also in the binary: `InterruptDontReactCommand`, `InterruptUndecidedCommand`, `InterruptChangeAskCommand`, `InterruptToggleDisableCommand` | `DataContext.Data.InterruptEvents[0]` |
| **Hotbar** (`HotBar.xaml`) | `UseSlotCommand`, `LaunchDefaultActionCommand`, `LaunchUpcastCommand`, `ShortRest`, `CancelTaskCommand`, `SetCurrentShownDeckCommand`, `ChangeThrowFilter`. Also in the binary: `UseMeleeWeaponCommand`, `UseRangedWeaponCommand`, `SwitchWeaponSetCommand`, `ToggleDualWieldingCommand`, `ReleaseConcentrationCommand`, `ToggleCombatLogCommand` | slot VM / `CurrentPlayer.SelectedCharacter` |
| **End turn** (`ActionResources_c.xaml:117`) | `DataContext.EndTurn`, `DataContext.CancelEndTurn`. The button's `BoundEvent="UIEndTurn"` is the key-bound input event. | — |
| **Container / loot** (`Container.xaml`) | `TakeAllAndCloseCommand.ActionCommand`, `CloseContainerCommand`, `DropOnInventorySlotCommand`, `DragInventoryCommand`, `SortContainerInventoryCommand`. Binary also has: `TakeItemCommand`, `PickUpCommand`, `ItemMoveToContainerCommand`, `GiveItemCommand`, `EquipItemCommand`, `UnequipItemCommand`, `DropCommand`, `RequestSplitCommand`/`ApplySplitCommand`, `ToggleInventoryLockCommand`, `SwapSlotCommand`, `DropOnPartyMemberCommand`, `ExamineCommand`, `TranscribeScrollsCommand` | item VM |
| **Trade** (`Trade.xaml`) | `TradeItemCommand`, `FinishTrade`, `FinishBarter`, `SellWares`, `SetTradeMode`, `SwitchTraderCharacterCommand`, `Drop{Player,Trader}{Offer,}InventoryTradeSlotCommand`, `DragTradeSlotCommand`, `SelectCharacter` | item VM / selected items |
| **Camp / rest** (`MakeCamp.xaml`, `LongRest.xaml`, `RestPanel_c.xaml`) | `ProcessRestCommand`, `CancelCampCommand`, `AutoSelectCommand` (supplies), `OnItemSelectedCommand`; Hotbar `ShortRest` | selected supplies |
| **Waypoints** (`JournalMap.xaml:247`) | `GotoWaypoint` | the waypoint VM (`VMWaypointGroup` children) |
| **Level up** (`CharacterLevelUp.xaml`, `CCLib.xaml`) | `SetLevelUpStep`, `SelectFirstUsedClass`, `SelectNextUsedClass`/`SelectPrevUsedClass`, `SelectFirstUnusedClass` (multiclass), `SelectSpell`/`DeselectSpell`, `ApplySpellReplacement`, `SelectAbility`/`DeselectAbility`, `ToggleSkill`, `TogglePassive` (feats / fighting styles), `FinishLevelUp`. `SelectedItem="{Binding SelectedMultiClass}"` is two-way. | VMs from collections |
| **Character creation** (`CharacterCreation.xaml`, `CCLib.xaml`) | Two-way `SelectedItem` bindings: `SelectedOrigin`, `SelectedRace`, `SelectedSubRace`, `SelectedBackground`. Commands: `IncreaseAbility`/`DecreaseAbility`, `SelectAbilityBonus`, `UseRecommendedAbilities`, `ResetAbilities`, `ToggleSkill`, `SelectSpell`, `SetCharacterCreationStep`, `RandomCharacter`, `RandomAppearance`, `FinishCreating`, `ExitCharacterCreation`, `SelectNext/PreviousIdentity`, `SelectNext/PreviousBodyType`, `ValueChanged`/`AppearanceValueChanged` | — |
| **Respec (Withers) / mirror** (`CharacterRespec.xaml`, `CharacterFullRespec.xaml`) | Same CC commands, plus `FinishCreating`, `ExitCharacterRespec`, `FinishRespec` | — |
| **Tadpole powers** (`TadpolePowersTree.xaml`) | `EnterPreparationStage`, `CancelPreparationStage`, `SelectCharacter`, plus the node buttons via `{Binding}` | power-node VM |
| **Save/load** (`SaveGame.xaml`, `GameMenu.xaml`, `SaveLoad.xaml`) | `QuickSaveCommand`, `QuickLoadCommand`, `OpenSaveGameDialogCommand`, `DoSaveWithName`, `DataContext.DoSaveOverwrite`, `SelectSavefileCommand`, `DoLoad`, `UseSavefileCommand`, `MainMenuCommand`, `QuitGameCommand` | save VM |
| **Main menu** (`MainMenu.xaml`) | `ContinueGameCommand`, `StartGameCommand`, `OpenLoadGameDialog`, `SetMainMenuStep`, `UpdateDifficultyCommand`/`UpdateCustomDifficultyCommand` (binary) | — |
| **Spell prep / scrolls** | `OpenLearnSpellsCommand`, `TranscribeScrollsCommand` (binary/XAML) | — |
| **Rewards** (`RewardPanel_c.xaml`) | `GetRewardsAndClose.ActionCommand` | — |
| **Generic** | `CustomEvent` (sends a named event to the UI state machine), `ActionCommand`, `ButtonCommand.ActionCommand`, `CloseCommand`, `BackCommand`, `ContinueCommand` | — |

Note: XAML property names like `EndTurn`, `FinishTrade` and `GotoWaypoint` are resolved by Noesis reflection at runtime. Even where a name doesn't appear as a `*Command` string in the binary, `dc.EndTurn` should be readable through bg3se's reflection. **UNVERIFIED** until the smoke test in §8.

### 4.4 Example (Lua, client context)

```lua
-- Choose dialogue answer #i (1-based) as shown in the UI. Fair: it's exactly the button.
local function FindWidgetWithDC(root, typeName)
  -- BFS over the visual tree; return the first element whose DataContext type name matches.
  local q = {root}
  while #q > 0 do
    local e = table.remove(q, 1)
    local dc = e.DataContext
    if dc and Ext.Types.GetObjectType and tostring(dc):find(typeName, 1, true) then return e, dc end
    for i = 1, (e.VisualChildrenCount or 0) do q[#q+1] = e:VisualChild(i) end
  end
end

function SelectAnswer(i, reply)
  Ext.UI.Defer(function()
    local _, dc = FindWidgetWithDC(Ext.UI.GetRoot(), "DCDialogue")
    if not dc then return reply({ok=false, err="no dialogue"}) end
    -- the answers collection name must be discovered once with a property dump (e.g. dc.Answers / dc.CurrentDialogue.Answers)
    local answer = dc.Answers[i]                                    -- UNVERIFIED property path
    if not dc.SelectAnswerCommand:CanExecute(answer) then return reply({ok=false, err="cannot execute"}) end
    dc.SelectAnswerCommand:Execute(answer)
    reply({ok=true})
  end)
end
```

Confirm with the server-side Osiris events `DialogActorJoined`, `DialogEnded`, or the next node's `ecl::dialog::NodeEventsSingletonComponent`. A changed answers list in the next UI tick also confirms it.

### 4.5 Reliability caveats for (c)

- Property paths in the DataContexts aren't documented. Build a one-time **reflection dumper** that walks `GetRoot()` and prints each widget's name, DataContext type, properties and commands. Generate the per-screen paths from that dump and re-run it after each game patch.
- Commands often assume UI state: an open panel, a selected character, sometimes a selected item. Call `CanExecute(param)` first. Open the panel with `Ext.UI.SetState` or by raising its `CustomEvent`.
- Rendering runs on another thread. Do every access inside `Ext.UI.Defer`, and keep callbacks short.
- If bg3le's Noesis support is incomplete on the native build, (c) is not available there (§1).

---

## 5. Mechanism (d): mouse and keyboard injection

### 5.1 Why not desktop-level injection on this machine

- KDE Plasma 6 on Wayland. `xdotool` uses XTEST, and its README says it "will not work correctly under Wayland".
- `ydotool`/`dotool` use the kernel uinput device. They work regardless of compositor but need `/dev/uinput` permissions and a daemon. Absolute pointer positioning is awkward, and a KWin developer calls it "not the proper way".
- libei via the `RemoteDesktop` portal is the proper Wayland route. It needs an interactive consent prompt, and tools for it (`wdotool`) are very new (AUR, April 2026) and their KDE backend is untested.
- Whether BG3's SDL 2 runs on Xwayland or native Wayland here is **UNVERIFIED**. The bundled `libSDL2.so` contains Wayland support strings, and SDL2 defaults to X11 when `DISPLAY` is set.

### 5.2 Recommended: in-process SDL injection

`bin/bg3` imports `SDL_PollEvent`, `SDL_GetGlobalMouseState` and `SDL_WarpMouseInWindow` from `libSDL2.so`. A tiny `LD_PRELOAD` library can interpose them:

- `SDL_PollEvent` returns the real events, then any queued synthetic `SDL_MOUSEMOTION`, `SDL_MOUSEBUTTONDOWN/UP`, `SDL_MOUSEWHEEL`, `SDL_KEYDOWN/UP` and `SDL_TEXTINPUT` events.
- `SDL_GetGlobalMouseState` returns the synthetic cursor position and buttons while the agent "owns" the mouse. This is needed because the game reads the **global** mouse state for hover and picking, not just events.
- Commands arrive over a Unix socket from the harness.

This is independent of the compositor, needs no focus and no permissions, and is invisible to the user's real mouse. bg3se's `Ext.Input.InjectKey*` already does the keyboard half through the same SDL queue, which proves the approach works for keys. Assessment: **Fair ✅ (it is literally player input), Rel M (needs coordinates), Eff L–M.**

Gotchas:

- bg3le itself is loaded by a launcher wrapper. Both shims must be chained in `LD_PRELOAD`, or the SDL shim can live inside the bg3le build.
- Steam runtime "sniper" containers keep `LD_PRELOAD` only if it is passed through the launch options.

### 5.3 Turning screenshots into clicks

For the screenshot loop, a pure vision model clicking pixels is unreliable for BG3's small, dense UI. Compute coordinates from game state instead:

- **World targets**: project the entity's world position to screen using the client camera. The camera component is exposed through `Ext.Entity` (`Camera`, `GameCameraBehavior`), or more simply: move the cursor, then read `Ext.UI.GetPickingHelper(0).Selection` and nudge until it equals the intended entity. This read-back makes clicking verifiable.
- **UI targets**: when (c) works, prefer `Execute()`. When it doesn't, Noesis element bounds (`ActualWidth`/`ActualHeight` and the visual-tree offsets) give click coordinates. bg3le says it lacks "visual transforms", so this is **UNVERIFIED**.
- **Keyboard shortcuts** come from `Ext.Input.GetInputManager()`: hotbar 1–0, Space for end turn, F5/F8 for quicksave/quickload, Z for jump, X for shove, C for sneak, etc. They're cheap and reliable for actions with keybinds.

---

## 6. Per-category analysis

Columns: **Best** (recommended), **Alt** (alternatives), with Fair/Rel/Eff for the best option.

### 6.1 Exploration

| Action | Best | Fair / Rel / Eff | Alt and notes |
|---|---|---|---|
| Click-to-move to a point | (a) `CharacterMoveToPosition(char, x,y,z, "Run", evt)` | ✅ / H / L | (d) click the ground. Out of combat there is no resource cost, so Osiris movement is equivalent. The party follows via the follow system if chained. **Guard**: reject targets the player couldn't click, i.e. unexplored or unseen positions. Use the visibility data from the state layer. |
| Path to an entity | (a) `CharacterMoveTo(char, target, "Run", evt)` | ✅ / H / L | Completion: the custom `_Event` arrives as `EntityEvent(char, evt)`. Failure: `CharacterMoveToCancelled`, or `CharacterMoveFailedUseJump` (path needs a jump). |
| Jump | (b) cast `Projectile_Jump` via the engine request, or (a) `UseSpellAtPosition(char,"Projectile_Jump",x,y,z)` (used in story) **with guard** | ⚠️ / M / M | Jump costs a bonus action plus movement in combat. Outside combat Osiris is fine. |
| Sneak / hide | UI hotbar `UseSlotCommand` on the Sneak slot or the `C` key (d) | ✅ / H / L | `Hide` is a spell (`Shout_Hide`) and can go through the cast path. |
| Camera | (d) keys/mouse wheel | ✅ / M / L | Irrelevant to the game rules, but it affects screenshots. |

### 6.2 Interaction

| Action | Best | Fair / Rel / Eff | Notes |
|---|---|---|---|
| Doors, levers, buttons | (a) `Use(char, item, evt)` (walks there) | ✅* / H / L | *Locked doors: `Use` on a locked door does not unlock it. The player flow for a locked door is the lockpick roll (below). Never call `Open`/`Unlock`. Completion: `UseFinished`, `Opened`, the custom event. |
| Open container (chest, corpse) | (a) `Use(char, container, evt)` → container UI opens; or (d) click | ✅ / M / L | The loot UI is `DCContainerInventory`. Corpses: `CharacterLootedCharacter`. Item contents must only be reported once the container UI is open, for fairness. |
| Loot items out of a container | (c) `TakeAllAndCloseCommand.ActionCommand`, `TakeItemCommand(item)` | ✅ / M / M | (a) `ToInventory(item, char)` is equivalent outside combat if `_ClearOriginalOwner` stays 0. The crime system still sees theft (ownership is preserved). Moving items costs nothing outside combat. **Guard**: only items in an opened, visible container. |
| Pick up from the ground | (a) `Pickup(char, item, evt, 0)` | ✅ / H / L | `PickupFailed` on failure. In combat, picking up costs an action per the engine (`esv::drop::ActionResourceSpendRequest…`). Whether Osiris charges it is **UNVERIFIED**, so avoid Osiris pickup in combat and use (d) or (c) there. |
| Talk / start dialogue | **Raise the engine event** `Osi.DialogStartRequested(npc, player)` after moving into range with `CharacterMoveTo`; or (d) click the NPC | ✅ / M / L | The shipped story handles exactly this event (`__GLOBAL_Dialogs.txt:2167`: `DialogStartRequested(_Npc,_Player) AND QRY_SelectAndStartDialog(_Npc,_Player)`), so dialogue selection, crime dialogs and companion interjections behave as if clicked. Raising events from Lua is documented as synchronous. That the engine does nothing *else* on a real click (e.g. range or privacy checks) is **UNVERIFIED**, so check range ≤ ~3 m first. Don't use `QRY_StartDialog_Fixed` with a self-chosen dialog resource: that can start dialogs the player couldn't. |
| Read books / notes | (a) `Use(char, book, "")` (opens the book UI) or the inventory UI "Use" | ✅ / H / L | Text for the agent: read the book's localized content only after the UI opened, i.e. `DCBook` is live (fairness). Close with the `CustomEvent`/close command or Escape. |
| Lockpicking | Player flow: (d) click the locked door/chest with thieves' tools in inventory. The engine raises `RequestCanLockpick`, `StartedLockpicking`, then the roll UI `DCActiveRoll`. | ✅ / M / M | No clean Osiris call exists. The engine path is `NETMSG_CHARACTER_LOCKPICK` → `esv::lock::ActiveRollRequestOneFrameComponent` + `SpendActionResourceRequest`. Using `Use(char, door, 1, 1, evt)` on a locked door may route through the same interaction (**UNVERIFIED**). Then resolve the roll via §6.4. |
| Disarm traps | Same pattern: click the **detected** trap with a trap kit. `NETMSG_CHARACTER_TRAPDISARM` → `esv::trap::ActiveRollRequest…`. Events `StartedDisarmingTrap`, `AttemptedDisarm`, `CharacterDisarmed`. | ✅ / M / M | Fairness: only traps the party has detected (passive Perception). Only expose them from the state layer once detected. |
| Pickpocket | (d) click while sneaking → `DCPickpocket` UI → (c) take items | ✅ / L / M | `NETMSG_CHARACTER_PICKPOCKET`. |

### 6.3 Dialogue and cutscenes

| Action | Best | Fair / Rel / Eff | Notes |
|---|---|---|---|
| Choose a dialogue option | (c) `DCDialogue.SelectAnswerCommand:Execute(answerVM)` | ✅ / H / M | Send only the **visible** answers (text, tags like [PERSUASION], [CLERIC]) and their index. Fallback (d): the number keys 1–9 select answers (BG3 keybinds, **UNVERIFIED** binding name). No Osiris path: Osiris cannot pick an answer. |
| Advance a line / skip | (d) Space/Enter (`UIAccept`-style binding) or the left click that skips cinematic lines; there is also `NETMSG_TIMELINE_REQUEST_FASTFORWARD` | ✅ / H / L | Lines auto-advance if voice plays. Skipping is optional. |
| Listen in (multiplayer) | n/a | — | Single-player only. |
| Dialogue skill check | It opens the **active roll** UI. See §6.4. | | `DialogRollResult` event confirms. |
| Cutscene (Bink movie / timeline) | Wait; optionally (d) Escape/Space to skip where allowed | ✅ / H / L | Detect via `NETMSG_PLAYMOVIE` / `ecl::screen_fade::*` / `DialogStarted` with a cinematic dialog. |
| Companion approval events | These are just dialogues and camp-night scenes triggered by story. Act with the same dialogue tools. | ✅ | `ApprovalRatingChanged(_Npc,_Player,_NewValue)` is an Osiris event. Only show approval changes the UI shows (the "X approves" toast). |

### 6.4 Rolls

| Action | Best | Notes |
|---|---|---|
| Roll the die | (c) `DCActiveRoll.UserTriggeredNextPhaseCommand` (or `TriggerNextPhaseCommand`) | Fair ✅. Confirm via the Osiris `RollResult(_EventName, _Roller, _Subject, _ResultType, _IsActiveRoll, _CriticalType)` (arity per dump) or `DialogRollResult`. Then `ContinueCommand`. |
| Add Guidance / Bardic Inspiration / item modifiers | (c) `AddModifierCommand(modifierVM)` / `RemoveModifierCommand` | The modifiers list lives on the DC. These consume the resource as in the UI. |
| Use Inspiration to reroll | (c) `RollAgainCommand` | Consumes an Inspiration point server-side. |
| Fallback | (d) click the d20, or Space/Enter | |

Never call `RequestActiveRoll` yourself. It is a story call that creates rolls the player didn't trigger.

### 6.5 Combat

All attacks, cantrips, spells, Dash, Disengage, Help, Shove, Jump, Throw, Hide, Dip, potion drinking and Illithid powers are **spells** (`Target_MainHandAttack`, `Projectile_Jump`, `Shout_Dash`, `Shout_Disengage`, `Target_Help`, `Target_Shove`, `Throw_Throw`, etc.) on the character's `SpellBook` and hotbar. The canonical "do X in combat" is one function: **cast spell S (variant/upcast) at target T or position P**.

| Action | Best | Fair / Rel / Eff | Notes |
|---|---|---|---|
| Attack / cast at an entity | **(b)** engine `CastStartRequest` (no ignore flags, `FromClient`); or **(c+d)** hotbar `UseSlotCommand(slot)` then a click on the target (verified via picking helper) | ✅ / M / M–H | (a) `UseSpell(caster, spell, target)` only behind the guard (§7): spell in book, prepared, resources available, in combat it's our turn, range/LoS checked via `Ext.Level.Raycast*`. Whether `UseSpell` *consumes* resources is **UNVERIFIED**. If it doesn't, the guard must also deduct them, which is ugly. So prefer (b) or (c+d). |
| AoE at a point | (b) request with `CastPosition` / position target; or (c+d) slot + click ground; or (a) `UseSpellAtPosition` + guard | same | AoE previews: the UI shows targets; the agent should get the same "would hit" list. Projectile arcs and LoS are computed by the engine when it validates the cast. |
| Multi-target (Magic Missile, Scorching Ray, Eldritch Blast beams) | (c+d) slot, then click each target, then confirm; or (b) request with multiple `Targets` | ✅ / M / H | `NETMSG_SPELL_CAST_UPDATE_PREVIEW`/`CONFIRM` sequence. Multi-target via Osiris `UseSpell` supports at most 2 targets. |
| Upcast | (c) hotbar `LaunchUpcastCommand` then pick the level; or (b) request with the upcast `SpellId` variant | ✅ / M / M | |
| Bonus actions (off-hand attack, Healing Word, Cunning Action…) | Same as casting. The engine knows the cost type. | ✅ | |
| Movement in combat | (a) `CharacterMoveToPosition` **only if verified** to spend movement and stop at the movement limit; otherwise (d) click the ground (the UI enforces the movement budget and shows AoO warnings) | ⚠️ / M / L | Pre-check path length with `Ext.Level.FindPath`. Opportunity attacks happen naturally (engine-side `IsInDangerOfAttackOfOpportunity` query exists). |
| Dash, Disengage, Help, Hide, Shove, Throw, Jump | cast the corresponding spell (see first row) | ✅ | |
| End turn | (c) HUD DC `EndTurn` command, or (d) the `UIEndTurn` keybind, or (a) `EndTurn(char)` | ✅ / H / L | All three are fair. Confirm with `TurnEnded`. |
| Reactions prompt (Shield, Counterspell, Hellish Rebuke, Riposte…) | (c) `DCReactionDecision.InterruptReactCommand(interruptEvent)` / `InterruptDontReactPlayerCommand` / `InterruptAcceptPlayerCommand` | ✅ / M / M | The prompt appears when the reaction is set to "Ask". The Osiris event `ReactionInterruptActionNeeded(_Char)` tells the harness a decision is pending. Alternative (b): write `Ext.System.ServerInterruptDecision.Decisions[char]` (**UNVERIFIED**; risky). Set reaction preferences per character to "Ask" for the ones the agent should decide, via the UI (`InterruptChangeAskCommand`). |
| Opportunity attacks | Same reaction system (prompt or auto) | ✅ | |
| Toggle passives / non-lethal / weapon set | (c) `SwitchWeaponSetCommand`, the passive toggles (`NETMSG_PASSIVE_TOGGLE`) | ✅ | |
| Concentration drop | (c) `ReleaseConcentrationCommand` | ✅ | |
| Turn-based mode outside combat | (a) `ForceTurnBasedMode(char,1)` or (d) the keybind (`NETMSG_FORCE_TURN_BASED_TOGGLE_REQUEST`) | ✅ | |
| Flee | (c) flee button / `NETMSG_TURNBASED_FLEE_REQUEST` | ✅ | |

### 6.6 Items and inventory

| Action | Best | Fair / Rel / Eff | Notes |
|---|---|---|---|
| Drink potion / read scroll / use consumable | Cast path: the item's use is a spell with `Item` set, i.e. `ItemStartRequests`. Easiest is (c) inventory/hotbar `UseSlotCommand`, or (a) `Use(char, item, evt)` out of combat | ✅ / M / M | In combat, potions cost a bonus action and scrolls an action. Use the UI or the engine path so costs apply. `Use` out of combat is fine. |
| Equip / unequip | (a) `Equip(char, item)` / `Unequip(char, item)` out of combat; (c) `EquipItemCommand`/`UnequipItemCommand` in combat | ✅ / H / L | Events `Equipped`/`EquipFailed`. |
| Move item between party members | (a) `ToInventory(item, otherChar, amount, 0, 0)` | ✅ / H / L | Out of combat equivalent to drag-and-drop. In combat, giving items costs actions; use the UI. |
| Camp chest | (a) `SendToCampChest(item, hostPlayer)` | ✅ / H / L | The UI's "Send to camp" only works for items the player holds. Guard: the item is in a party inventory. Taking items *out* of the chest needs to be at camp: open the chest with `Use` → container UI. |
| Split / sort / lock | (c) `RequestSplitCommand`, `SortCharacterInventoryCommand`, `ToggleInventoryLockCommand` | ✅ | Cosmetic. |
| Combine / alchemy / dye | (c) `DCCombine` / `DCAlchemy` | ✅ / M / M | `NETMSG_COMBINE_ITEMS`. |
| Learn spell from scroll (wizard) | (c) `TranscribeScrollsCommand` / `OpenLearnSpellsCommand` + `DCLearnSpells` | ✅ / M / M | Costs gold server-side. |
| Prepare spells | (c) spellbook UI `VMSpellPrepare` toggles → `NETMSG_SPELL_PREPARE` | ✅ / M / M | |

### 6.7 Trade

| Action | Best | Notes |
|---|---|---|
| Start trade | Via dialogue answer "[Trade]" (`StartTrade` command in `Dialogue.xaml`) or right-click → Trade | Don't call `ActivateTrade`: it's story-side and can open trades that aren't offered. |
| Move items in the offer, set gold, barter | (c) `DCTrade.TradeItemCommand(itemVM)`, `Drop*TradeSlotCommand`, `SetTradeMode`, `SellWares` | Prices and approval-based discounts are computed by the engine. Fair. |
| Confirm / leave | (c) `FinishBarter` / `FinishTrade` | Confirm with `TradeEnds(_Player,_Trader)` and inventory or `GoldChanged` events. |
| Non-UI path | None that is fair. `ToInventory` + `UserAddGold` would be cheating. | |

### 6.8 Party control

| Action | Best | Notes |
|---|---|---|
| Select character | (a) `MakePlayerActive(char)` or (c) `SelectCharacterCommand` / portrait click | Event `CharacterSelectedForUser`. Changing selection is a UI-only concept and fair. |
| Chain / unchain / group | (a) `AttachToPartyGroup(char, leader)`, `DetachFromPartyGroup(char)`; or (c) `GroupCharactersCommand`, `SeparateCharacterCommand`, `ToggleGroupMode` | Fair. Events `AttachedToPartyGroup`/`DetachedFromPartyGroup`. |
| Party composition (swap companions at camp) | Dialogue with the companion at camp ("Join me" / "Wait at camp") | Use the dialogue tools. Never call `AddPartyFollower`/`RegisterAsCompanion`. |

### 6.9 Rest, camp and travel

| Action | Best | Notes |
|---|---|---|
| Short rest | (c) hotbar DC `ShortRest` command | `NETMSG_SHORT_REST`. Events `ShortRestProcessing`/`ShortRested`. The engine enforces 2 short rests per long rest. |
| Go to camp | (c) camp button / `CampTravel` UI event; or (a) `RequestGatherAtCamp(player)` (what the UI's story hook calls) | Events `RequestGatherAtCampSuccess`/`Fail`, `TeleportedToCamp`. Fails in combat or restricted zones, as for a player. |
| Long rest | At camp: (c) `DCLongRest` / `MakeCamp` `AutoSelectCommand` + `ProcessRestCommand` (supply selection). Alt (a) `RequestLongRest(player, 0)` | Supplies are consumed only via the UI path. `RequestLongRest(_,0)` probably skips the supply UI (**UNVERIFIED**), so prefer (c). Events `LongRestStarted`, `LongRestFinished`, `UserCharacterLongRested`. |
| Camp night events | These are dialogues/cinematics triggered by story during the rest. Use the dialogue tools. | |
| Fast travel | (c) Map `GotoWaypoint(waypointVM)` with only unlocked waypoints (`PartyWaypoints` component), from a waypoint or anywhere out of combat as the UI allows | Event `TeleportToWaypoint`/`Teleported`. `OpenWaypointUI` exists for scripted openings. Never use `TeleportTo*`. |
| Leave camp | Waypoint or the camp exit (`TeleportedFromCamp`) | |

### 6.10 Progression

| Action | Best | Notes |
|---|---|---|
| Level up | (c) open `DCCharacterLevelUp` (portrait level-up arrow / UI state), then `SelectFirstUsedClass`/`SelectNextUsedClass`/`SelectedMultiClass`, `SelectSpell`, `ToggleSkill`, `TogglePassive` (feats), `SelectAbility`, `ApplySpellReplacement`, `SetLevelUpStep`, `FinishLevelUp` | Complex but fully fair. Generate the step script from the reflection dump. Confirm via `LevelChanged`/`ProgressionLevelUpChanged` components or the Osiris `LeveledUp` event (**UNVERIFIED** name). |
| Non-UI path for level-up | ECS `CCLevelUpDefinition` / `ClientCCLevelUpDefinition` + `NETMSG_CHARACTER_CREATION_LEVELUP`. bg3se can read and write these components, but submitting needs the client protocol. | **UNVERIFIED and not recommended.** It's effectively writing a level-up packet by hand, and it can create illegal builds if the engine trusts the client definition. |
| Illithid powers | (c) `DCTadpolePowersTree`: select node → confirm (`NETMSG_TADPOLE_POWER_ADD`). Using powers in combat = casting spells. | `AddTadpolePower` is a cheat. Event `TadpolePowerAssigned`. |
| Respec (Withers) | Dialogue with Withers (`StartRespec` is called by story) → `CharacterRespec` / `CharacterFullRespec` UI → CC commands → `FinishCreating`/`FinishRespec` | Non-UI: none fair. `StartRespec(char)` only *opens* the UI. Events `RespecCompleted`/`RespecCancelled`. |
| Mirror (appearance) | Story calls `StartChangeAppearance(_Player)` → CC appearance UI | Cosmetic. Can be skipped entirely. |

### 6.11 Character creation and the main menu

| Action | Best | Notes |
|---|---|---|
| New game | (c) `DCMainMenu.StartGameCommand`, difficulty `UpdateDifficultyCommand`, `SetMainMenuStep` | `ContinueGameCommand` loads the last save. |
| Character creation | (c) `DCCharacterCreation`: set `SelectedOrigin` / `SelectedRace` / `SelectedSubRace` / `SelectedBackground` (two-way bound properties; writing them from Lua should trigger the same logic as the ListBox, **UNVERIFIED**), `IncreaseAbility`/`DecreaseAbility` or `UseRecommendedAbilities`, `SelectAbilityBonus`, `ToggleSkill`, `SelectSpell`, `RandomAppearance`, `FinishCreating` | Simplest fair choice: pick a premade **Origin** character (Lae'zel, Shadowheart, etc.) with `UseRecommendedAbilities`, which removes most CC complexity. |
| Non-UI path | `CCCharacterDefinition` component + `NETMSG_CHARACTER_CREATION_UPDATE`/`READY` | Not recommended (same reasoning as level-up). |
| Tutorial popups | (c) `DCTutorialPopup` close / `HideUnifiedTutorialCommand`, or disable tutorials in options | |

### 6.12 Saving and loading

| Action | Best | Notes |
|---|---|---|
| Quicksave | (c) `QuickSaveCommand` or (d) F5 | `NETMSG_SAVEGAME`. Confirm with a new file in `~/.local/share/Larian Studios/Baldur's Gate 3/PlayerProfiles/Public/Savegames/Story/` (read-only watch). |
| Named save | (c) Game menu → `OpenSaveGameDialogCommand` → `DoSaveWithName` | Use named saves per agent session ("session-NN"). |
| Autosave | (a) `AutoSave()` | Fine; equivalent to the game's own autosaves. |
| Load | (c) `DCSavegames.SelectSavefileCommand(saveVM)` + `DoLoad`, or `QuickLoadCommand` / F8 | bg3le: "Loading a second save without restarting leaves Osiris bound to the first story's mappings." **The harness should restart the game process to load on native bg3le**, or call `Ext.Debug.Reset()`. Osiris event `SavegameLoaded` and Ext `SessionLoaded` confirm. |
| Main menu / quit | (c) `MainMenuCommand`, `QuitGameCommand` | |

---

## 7. Fairness guard for any non-UI action

Every Osiris or ECS action passes through a server-side Lua guard in our mod before execution. It mirrors the checks the UI would make:

1. **Visibility.** The target entity or position must be in the set the state layer exposes to the agent: seen by a party member, explored, not hidden or sneaking-undetected, and the trap or item already detected.
2. **Ownership and legality.** Never set `_ClearOriginalOwner`, `Forced`, `IgnoreHasSpell`, `IgnoreCastChecks`, `IgnoreSpellRolls` or `IgnoreTargetChecks`.
3. **Combat gating.** If `IsInCombat(char)==1`:
   - it must be the character's turn (`CombatGetActiveEntity`, `TurnStarted` tracking), or the action is a reaction the engine prompted;
   - only movement via the engine's budget is allowed; Osiris `Use`/`Pickup`/`ToInventory`/`Equip` are **disallowed** in combat, so route those through (c) or (d).
4. **Spell gating** (when Osiris `UseSpell` is used at all):
   - the spell is in the `SpellBook` and prepared (`SpellBookPrepares`), with cooldown free (`SpellBookCooldowns`);
   - the action resources required by the spell's `UseCosts` are available (`ActionResources`);
   - the range and line of sight check passes.

   Then compare `ActionResources` before and after the cast. If nothing was deducted, flag it and switch that spell to path (b) or (c+d).
5. **Blocklist** the cheat calls: `TeleportTo*`, `ApplyStatus`, `RemoveStatus`, `Open`, `Unlock`, `AddGold`/`UserAddGold`/`PartyAddGold`, `TemplateAddTo`, `AddSpell`, `AddTadpolePower`, `SetFlag`, `RequestActiveRoll`, `ActivateTrade`, `StartRespec`, `AddExplorationExperience`, `SetHitpointsPercentage`, `Resurrect`, `MakePlayer`, `SetFaction`/`SetRelation`, and `DB_*` writes.

The guard also logs each decision. The playthrough memory (BRIEF point 4) can then record why an action was rejected.

---

## 8. Smoke tests to run before building on any of this

Run each in a disposable save. The game must not be launched by this research task.

1. **Noesis on the chosen platform.** In a client console:

   ```lua
   Ext.UI.Defer(function() _D(Ext.UI.GetRoot():Find("ContentRoot")) end)
   ```

   Then walk the tree, find the `gui::DCDialogue` widget during any dialogue, and call `dc.SelectAnswerCommand:CanExecute(dc.<answers>[1])`. If it crashes on bg3le, decide between Proton + official bg3se and (d) for UI.
2. **`UseSpell` cost.** In combat on your turn:

   ```lua
   _D(_C().ActionResources)
   Osi.UseSpell(GetHostCharacter(), "Projectile_FireBolt", target)
   _D(_C().ActionResources)
   ```

   Repeat for a levelled spell (`Projectile_MagicMissile`) to see if a slot is spent. Repeat with a spell the character doesn't know, to confirm the "ignore pre-conditions" claim.
3. **Movement cost.** `CharacterMoveToPosition` in combat: is Movement deducted, and does movement stop at the limit?
4. **Engine cast request.** Append a `CastStartRequest` (copied from one observed during a real UI cast, via `Ext.Entity.Subscribe("ServerSpellCastRequests", …)` or by reading `Ext.System.ServerCastRequest.NetworkStartRequests` on the frame a cast is made). Then replay it with a different target.
5. **`DialogStartRequested`.** Raise it from Lua for an NPC in range and confirm the same dialogue starts as with a click.
6. **SDL shim.** A synthetic click on the end-turn button and on the ground, verified with `GetPickingHelper`.

---

## 9. Recommendation

### 9.1 Layered design

```
Agent (LLM) ──tools──► Harness (Python, MCP server) ──socket──► bg3 mod (Lua: server + client contexts)
                                     │                                   ├─ L1 UI command layer   (client, Ext.UI.Execute)
                                     │                                   ├─ L2 Engine request layer (server, Ext.System / CastStartRequest)
                                     │                                   ├─ L3 Guarded Osiris layer (server, Osi.*)
                                     │                                   └─ Confirmation bus (Osiris listeners + Ext.Entity subscriptions)
                                     └──► L4 SDL input shim (LD_PRELOAD .so in bg3 process): keys, mouse, global mouse state
```

| Category | Primary | Fallback |
|---|---|---|
| Dialogue answers, rolls (incl. Guidance/Inspiration), reactions, trade, loot UI, level-up, character creation, respec, tadpole tree, camp/long rest, short rest, fast travel, save/load/menu | **L1 UI commands** | L4 clicks/keys at UI element coordinates |
| Spells, attacks, actions, bonus actions, AoE, upcast, potions/scrolls *in combat* | **L2 engine cast request** (after smoke test 4) | L1 hotbar `UseSlotCommand` + L4 click on target verified with `GetPickingHelper`; L3 `UseSpell` only with the guard if tests show costs apply |
| End turn | L1 `EndTurn` | L3 `EndTurn(char)`, L4 keybind |
| Exploration movement, move to entity | **L3** `CharacterMoveTo`/`CharacterMoveToPosition` (+ guard) | L4 ground click |
| Combat movement | L4 ground click (UI enforces budget) or L3 if test 3 shows the budget is enforced | — |
| Interact/use, pick up, equip, transfer, camp chest (out of combat) | **L3 guarded Osiris** | L1 inventory/container commands |
| Talk | L3 move into range + raise `DialogStartRequested(npc, player)` | L4 click NPC |
| Lockpick / disarm | L4 click (with tools) → L1 roll UI | — |
| Party select/chain | L3 `MakePlayerActive`, `AttachToPartyGroup`/`DetachFromPartyGroup` | L1 |
| Cutscene advance, camera, anything unmapped | **L4** | — |

**Why this design.** L1 reuses the game's own validation and is fair by construction. Its only cost is discovering property paths, and a reflection dumper automates that. L2 gives a precise "cast X at Y" without pixel targeting, and it is still validated by the engine. L3 is the most reliable and simplest mechanism, so it covers the actions where its lack of rule checks doesn't matter or a small guard covers the gap. L4 is the universal fallback. It must exist anyway for the "screen access" part of the brief, and in-process SDL injection avoids every Wayland problem on this KDE machine.

### 9.2 Platform decision

Prefer **bg3le on native Linux** if smoke test 1 passes, since everything else in the brief targets native. Otherwise use the **Windows build under Proton with official bg3se**. That route has full `Ext.UI`, but it changes the save/profile paths (compatdata) and the screenshot capture path. L4 then needs a Wine-side SDL hook instead of `LD_PRELOAD`, because the Windows build ships its own SDL DLL. That is **UNVERIFIED**.

### 9.3 Proposed agent tools

The harness exposes these tools. Every tool returns `{ok, action_id, confirmed, events[], error?}` and blocks until confirmation or timeout (default 10 s, or the `wait` arg).

**World and exploration**

- `move_to(character, target: entity_id | {x,y,z}, run=true)`: L3 path. Rejects unseen targets. Confirms on arrival, or reports `cancelled` or `needs_jump`.
- `jump_to(character, {x,y,z})`: cast `Projectile_Jump` via the cast tool's path.
- `interact(character, entity_id)`: doors, levers, containers, corpses, books, waypoints, ladders. Returns what UI opened, if any (`container`, `book`, `waypoint_map`, `roll`).
- `pick_up(character, item_id)`.
- `talk_to(character, npc_id)`: moves into range, then raises `DialogStartRequested`. Confirms on `DialogStarted`.
- `lockpick(character, entity_id)` / `disarm(character, trap_id)`: L4 click flow. Returns a pending roll.
- `toggle_sneak(character)`, `toggle_turn_based(on: bool)`.

**Dialogue, rolls and reactions**

- `choose_dialogue(index: int)`: index into the visible answer list from `get_state().dialogue.answers`.
- `advance_dialogue()`: skip or continue the current line or cutscene where allowed.
- `roll(modifiers: [modifier_id]=[], use_inspiration_on_fail=false)`: adds modifiers, rolls, optionally rerolls, continues. Returns `{natural, total, dc_visible?, success}`. The DC is returned only if the UI shows it.
- `react(decision: "use" | "skip", reaction_id?)`: answers the pending reaction prompt.
- `set_reaction_policy(character, reaction_id, "ask"|"always"|"never")`.

**Combat**

- `cast(character, spell_id, targets: [entity_id|{x,y,z}], upcast_level?: int, item_id?: str)`: covers attacks, cantrips, spells, Dash, Disengage, Help, Hide, Shove, Throw, Dip, consumables and Illithid powers. Validated, then L2 (or L1+L4). Confirms on `CastedSpell`/`SpellCastFinishedEvent`. Returns hits and damage only as shown in the combat log.
- `move_in_combat(character, {x,y,z})`: returns remaining movement.
- `end_turn(character)`.
- `release_concentration(character)`, `switch_weapon_set(character)`, `toggle_passive(character, passive_id)`.

**Items**

- `use_item(character, item_id, targets?)`: potions, scrolls, food. Uses the cast path in combat.
- `equip(character, item_id, slot?)` / `unequip(character, item_id)`.
- `transfer_item(item_id, to: character_id | "camp_chest", amount=1)`.
- `loot(container_id, item_ids | "all")`: requires the container UI to be open.
- `learn_spell_from_scroll(character, item_id)`, `prepare_spells(character, spell_ids)`, `combine(item_a, item_b)`.

**Trade**

- `trade_open(character, npc_id)`: through dialogue, if offered.
- `trade_set(offer_items, request_items, gold_delta?)`, then `trade_confirm()`, then `trade_close()`.

**Party, rest and travel**

- `select_character(character)`, `chain(character, leader)`, `unchain(character)`.
- `short_rest()`, `go_to_camp()`, `long_rest(supplies: "auto" | [item_ids])`, `fast_travel(waypoint_id)`.

**Progression**

- `level_up(character, plan: {class, subclass?, feat?|asi:{STR:+2…}, spells_add[], spells_replace?:{from,to}, skills[], expertise[], passives[]})`: a step-by-step L1 driver. Fails loudly with the UI's own error text if a choice is illegal.
- `choose_tadpole_power(character, power_id)`.
- `respec(character, plan)`, available only when the UI is open (i.e. after the Withers dialogue).
- `create_character(plan: {origin | race, subrace, class, subclass?, background, abilities: "recommended"|{...}, skills, cantrips, spells})`.

**Meta**

- `save(name?)` (quicksave when `name` is omitted), `load(save_name)` (restarts the process on bg3le), `main_menu(action)`.
- `press_key(binding_name)` and `click(screen_xy | entity_id | ui_element_path)`: raw L4 escape hatches, logged and rate-limited.
- `wait_for(event_name, filter, timeout)`: for cutscenes and NPC turns.

### 9.4 Confirming that an action happened

The mod keeps a **confirmation bus**. It is a ring buffer of game events tagged with `action_id`, filled from two sources.

`Ext.Osiris.RegisterListener` on these events:

- **Movement**: `EntityEvent` (custom move events), `CharacterMoveToCancelled`, `CharacterMoveFailedUseJump`.
- **Interaction**: `UseStarted`, `UseFinished`, `Opened`, `PickupFailed`, `AddedTo`/`TemplateAddedTo`/`RemovedFrom`.
- **Equipment**: `Equipped`/`EquipFailed`, `Unequipped`/`UnequipFailed`.
- **Dialogue**: `DialogStarted`, `DialogEnded`, `DialogActorJoined`, `DialogRollResult`.
- **Rolls**: `RollResult`.
- **Spells and attacks**: `CastSpell`/`CastedSpell`/`CastSpellFailed`, `UsingSpellOnTarget`, `StartAttack`, `AttackedBy`.
- **Turns and combat**: `TurnStarted`/`TurnEnded`, `CombatStarted`/`CombatEnded`/`CombatRoundStarted`.
- **Reactions**: `ReactionInterruptActionNeeded`/`ReactionInterruptUsed`.
- **Rest and camp**: `ShortRested`, `LongRestStarted`/`LongRestFinished`/`LongRestStartFailed`, `TeleportedToCamp`/`TeleportedFromCamp`.
- **Travel**: `TeleportToWaypoint`/`Teleported`.
- **Trade**: `RequestTrade`/`TradeEnds`, `GoldChanged`.
- **Lockpicking and traps**: `StartedLockpicking`/`StoppedLockpicking`, `Unlocked`, `StartedDisarmingTrap`/`AttemptedDisarm`/`CharacterDisarmed`.
- **Progression**: `TadpolePowerAssigned`, `RespecCompleted`/`RespecCancelled`, `CharacterCreationFinished`.
- **Session and party**: `SavegameLoaded`, `CharacterSelectedForUser`, `AttachedToPartyGroup`/`DetachedFromPartyGroup`, `ApprovalRatingChanged` (shown only when the UI shows it).

`Ext.Entity.Subscribe` / `OnCreate` for one-frame components:

- **Casting**: `SpellCastFinishedEvent`, `SpellCastCounteredEvent`, `SpellCastTargetHitEvent`.
- **Hits and resources**: `HitResultEvent`, `ActionResourceChangeResults`/`ActionResourceSpendEvent`.
- **Status and turns**: `ServerStatusApplyEvent`/`ServerStatusRemoveEvent`, `TurnStartedEvent`/`TurnEndedEvent`, `CombatLeftEvent`.
- **Rolls and rest**: `ServerRollFinishedEvent`, `ServerShortRestResultEvent`, `ServerRestTypeChosenEvent`.
- **Progression and inventory**: `LevelChanged`/`ProgressionLevelUpChanged`, `EquipmentSlotChangedEvent`, `SpellsLearnedEvent`.

Each tool also checks **post-conditions** explicitly:

- **Movement**: the position is within 1 m of the target.
- **Spells**: the resource deltas match the spell's cost.
- **Item transfers**: the item's inventory owner changed.
- **Dialogue**: the dialog instance node changed.
- **Saves**: the save file appeared on disk.

UI-driven actions are also confirmed client-side: the panel closed or changed, and the next tick's DataContext state differs. A screenshot is taken after every action for the agent's visual check.

If no confirming event arrives before the timeout, the tool returns `confirmed:false`. It includes the latest relevant UI message, e.g. "Out of range" or "Not enough movement", read from `DCNotifications` or the cursor text, so the agent sees the same feedback a player would.

---

## Sources

- bg3se Lua API docs (Osiris from Lua, `RegisterListener`, `Ext.UI`, `Defer`, `RegisterType`, picking helper): https://github.com/Norbyte/bg3se/blob/main/Docs/API.md
- bg3se source, commit `91abbf23` (2026-10-08), cloned locally:
  - `BG3Extender/Lua/Libs/ClientUI/NsHelpers.inl`: `CommandHelpers::Execute`/`CanExecute`/`InterfaceExecute`
  - `Lua/Libs/ClientInput.inl`: `InjectKeyPress`/`Down`/`Up` via SDL
  - `GameDefinitions/Components/SpellCast.h`: `CastStartRequest`, `CastRequestSystem` queues
  - `Enumerations/ECS.inl:2195`: `SpellCastOptions`
  - `Components/Interrupt.h`: interrupt decision system
  - `Controllers/OsirisTask.h`: Osiris tasks
  - `IdeHelpers/ExtIdeHelpers.lua`: `Ext.System`, component list
  - `Lua/Libs/Level.inl`: pathfinding and raycasts

  Repo: https://github.com/Norbyte/bg3se
- bg3le (native Linux port):
  - Announcement, bg3se issue #610 (2026-09-30): https://github.com/Norbyte/bg3se/issues/610
  - igortarasuk fork: https://github.com/igortarasuk/bg3le
  - AaronJordison fork: https://github.com/AaronJordison/bg3le
  - lenonk fork: https://github.com/lenonk/bg3le
- Official BG3 modding wiki Osiris pages:
  - [UseSpell](https://docs.baldursgate3.game/index.php?title=UseSpell)
  - [CharacterMoveTo](https://docs.baldursgate3.game/index.php?title=CharacterMoveTo)
  - [Use](https://docs.baldursgate3.game/index.php?title=Use)
  - [Equip](https://docs.baldursgate3.game/index.php?title=Equip)
  - [ToInventory](https://docs.baldursgate3.game/index.php?title=ToInventory)
  - [Pickup](https://docs.baldursgate3.game/index.php?title=Pickup)
  - [MakePlayerActive](https://docs.baldursgate3.game/index.php?title=MakePlayerActive)
  - [EndTurn](https://docs.baldursgate3.game/index.php?title=EndTurn)
  - [RequestActiveRoll](https://docs.baldursgate3.game/index.php?title=RequestActiveRoll)
  - [OpenWaypointUI](https://docs.baldursgate3.game/index.php?title=OpenWaypointUI)
  - [RequestLongRest](https://docs.baldursgate3.game/index.php?title=RequestLongRest)
  - [ActivateTrade](https://docs.baldursgate3.game/index.php?title=ActivateTrade)
  - [StartRespec](https://docs.baldursgate3.game/index.php?title=StartRespec)
  - [Introduction to Osiris](https://docs.baldursgate3.game/Scripting:_Introduction_to_Osiris)
- Osiris function dump (BG3 HF4, types and arity): https://gist.github.com/Liareth/f9b5b752eead6a66c0cfd95ae7946663
- Local game files (read-only):
  - `bin/bg3`: `strings` (`NETMSG_*`, `gui::DC*`, `*Command`, ECS request components) and `nm -D` (SDL imports)
  - `Data/Game.pak`: 310 XAML files, extracted to the scratchpad. Command bindings quoted in §4.3.
  - `Data/Gustav.pak`, `Shared.pak`, `GustavX.pak`: 946 raw story goals, e.g. `Mods/Shared/Story/RawFiles/Goals/__GLOBAL_Dialogs.txt` (`DialogStartRequested` → `QRY_SelectAndStartDialog`)
- Wayland input injection:
  - xdotool: https://github.com/jordansissel/xdotool
  - wdotool (libei/portal, KDE backend experimental): https://github.com/cushycush/wdotool
  - kdotool: https://github.com/jinliu/kdotool
  - KDE forum on Plasma 6 Wayland and xdotool: https://discuss.kde.org/t/previous-touchpad-multi-touch-gesture-solutions-seem-to-fail-on-plasma-6/19952
