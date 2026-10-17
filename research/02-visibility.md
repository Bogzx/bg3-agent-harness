# 02: Fairness / visibility model: exposing only what a human player can perceive

Topic owner: research task 02. Date: 2026-10-09. Game build: Steam buildid 25605617 (Patch 8 Hotfix 10), native Linux `bin/bg3`.
bg3se reference: `Norbyte/bg3se` HEAD `91abbf2` (2026-10-08).

**TL;DR.**
* The engine has no single "the player can see this" flag. The client process holds full, unfiltered replicated state for every loaded entity, including hidden ones. 580 component types are replicated server→client, among them `Health`, `Stealth`, `Invisibility`, `Lock` (with `LockDC`) and `StatusContainer`. So "it exists on the client" is not a fairness filter, and "it is rendered" is only a weak hint.
* The sound approach is a **server-side filter that uses the engine's own perception queries** (`Osi.CanSee`, `Osi.HasLineOfSight`, `Osi.IsInvisible`, `Osi.IsTrapDiscovered`, `Osi.GetCanInteract`, `Osi.IsOnStage`, `Osi.GetObscuredState`). It ORs them over the party, then projects each entity to **the fields the in-game UI shows**: the hover tooltip, the Examine panel and the turn-order bar.
* The projection must honour the Custom-Mode ruleset toggles `HIDE_NPC_HP`, `DIALOGUE_DC_HIDE`, `DIALOGUE_DC_PREVIEW`, `HIDE_FAILED_PERCEPTION_ROLLS` and `DIALOGUE_PASSIVE_ROLL_HIDE`. The game's own XAML reads these flags, and their UUIDs are listed below.

---

## 0. Method and evidence base

Everything marked *(binary)* was verified locally and read-only:

| Evidence | How obtained |
|---|---|
| 1,971 ECS component type names | `nm -C bin/bg3` → `ls::TypeId<X, ecs::ComponentTypeIdContext>` symbols |
| 580 components replicated server→client | `nm -C` → `StaticTypeRegistratorDefault<X, ecs::sync::EntityReplicationPeer>` symbols |
| Osiris built-in names (`CanSee`, `IsTrapDiscovered`, …) | `strings bin/bg3` |
| 940 Osiris goal scripts (story source) | Extracted from `Data/Gustav.pak` and `Data/Shared.pak` (`Story/RawFiles/Goals/*.txt`) using a small LSPK-v18 reader in a throwaway Docker container, with the game dir mounted **read-only** |
| Status definitions | `Public/*/Stats/Generated/Data/Status_*.txt` from the same paks |
| Ruleset modifiers (Custom Mode) | `Public/Shared/Ruleset/RulesetModifiers.lsx` (Shared.pak) |
| 310 Noesis UI XAML files (the real UI) | `Mods/MainUI/GUI/**.xaml`, `Public/Game/GUI/**.xaml` (Game.pak) → `{Binding …}` paths = the UI ViewModel fields each widget shows |
| Lua-side field names | bg3se `BG3Extender/GameDefinitions/**/*.h`, `IdeHelpers/ExtIdeHelpers.lua`, `Docs/API.md` |
| Osiris signatures | Community-generated `Osi.lua` ([LaughingLeader/BG3ModdingTools generated/Osi.lua](https://github.com/LaughingLeader/BG3ModdingTools/blob/master/generated/Osi.lua)), cross-checked against usage in the extracted goal scripts |

No game files were modified, and the game was not launched. Nothing was written under Steam or the Larian profile folder.

---

## 1. How BG3 decides what the player sees (game mechanics)

### 1.1 Sight, line of sight, obscurity, darkvision
* Every character has a sight component (`eoc::sight::BaseComponent` → bg3se `Sight`: `Sight` (range, m), `DarkvisionRange`, `FOV`, `VerticalFOV`). Boost components modify it: `SightRange{Additive,Minimum,Maximum,Override}Boost` and `DarkvisionRange{,Min,Override}Boost`.
* Line of sight is needed to spot a sneaking character and to target "target" spells. It is blocked by walls, closed doors, trees, large rocks and fog/darkness clouds. Projectiles only need a physical path. Source: [bg3.wiki Sight mechanics](https://bg3.wiki/wiki/Sight_mechanics).
* Obscurity has three levels: **Clear / LightlyObscured / HeavilyObscured** (strings `_LightlyObscured`, `_HeavilyObscured`, `ObscuredState`, `GetObscuredState` *(binary)*). Attacks against a heavily obscured target have disadvantage. Darkvision (the wiki page quotes a basic 2 m for many creatures; read the actual per-character value from `Sight.DarkvisionRange`, which the Examine panel also shows) lowers obscurity by one level for that viewer.
  * Server: `esv::light::GameplayObscurityComponent`, `esv::light::GameplayObscuritySystem`, `eoc::GameplayLightComponent`, `esv::darkness::DarknessActiveComponent`, `esv::cover::IsVisionBlockerComponent`, `esv::cover::IsLightBlockerComponent`.
  * Replicated: `eoc::DarknessComponent` (bg3se `Darkness`: `Obscurity` u8, `Sneaking` u8, `SneakingConesVisibility`, `CharacterSneaking`, `ActuallySneaking`, `GracePeriod`, `GraceFrames`).
  * Osiris: `GetObscuredState(obj) -> string`, `GetObscuredStateAtPosition(x,y,z) -> string`, event `ObscuredStateChanged(char, state)` with state ∈ {"Clear","LightlyObscured","HeavilyObscured"} (seen in goals).
  * The UI shows the *player's own* obscurity on the cursor/portrait (`CurrentPlayer.UIData.Cursor.CursorObscuredState`, `eoc::status::IndicateDarknessComponent`).
* Fog Cloud / Darkness are surfaces (`SurfaceFogCloud`, `SurfaceDarknessCloud`, `Darkness_Cloud` *(binary)*). They cut vision cones and block "target" actions.

### 1.2 Stealth / Hide / Sneaking, and vision cones
* **SNEAKING** is a status (`Status_SNEAKING.txt`):
  * `StatusType "SNEAKING"`, `Boosts "Advantage(AttackRoll);MovementSpeedLimit(Walk);Tag(VFX_SNEAKING);…"`.
  * It is removed on attack (unless the spell has the `Stealth` flag) or on damage. It has `StatusPropertyFlags "DisableOverhead"`.
  * Replicated tag component: `eoc::sneak::IsSneakingComponent`, plus `eoc::sneak::RollsContainerComponent`.
* The Hide action applies SNEAKING after a Stealth check. When the sneaker is in an obscured area or near a creature, that creature rolls **passive Perception vs the sneaker's Stealth**. If the creature fails, or the sneaker is invisible, it turns its vision cone toward the last known location and may use *Sense Hidden Presence* within 3 m ([bg3.wiki Sight mechanics](https://bg3.wiki/wiki/Sight_mechanics)).
  * Engine pieces: `eoc::StealthComponent` (bg3se `Stealth`: `SeekHiddenFlag`, `Position` (last-known position), `SeekHiddenTimeout`, …), `esv::StealthSystem`, `esv::sight::StealthRollRequestOneFrameComponent`, combat-log `StealthSpottedRequest{Spotter, Target, Obscurity, Cause}`, and `esv::combat::SurprisedStealthRequestOneFrameComponent`.
  * Osiris: `CanSpotSneakers(char)`, `SetCanSpotSneakers(char, bool)`. Character template flag `SpotSneakers`; server flag `esv::CharacterFlags::SpotSneakers`.
* **Vision cones** (`ShowSneakCones`, `DarknessComponent.SneakingConesVisibility`) are shown to the player *only while a party member sneaks*. They show NPCs' view, which is information the UI gives the player, so we may expose it (see §5).
* **NPCs sneaking against the party.** Ambushes and stealthed goblins/drow are revealed by party passive Perception. Many are scripted: goal scripts use `PROC_RevealAmbush((TRIGGER)…_PerceptionTrigger, _Player)`, `RequestPassiveRoll(...,"SkillCheck","Perception",DC,…)` and `RequestPassiveRollVersusSkill(_Criminal,_Investigator,"RawAbility","Stealth","Perception",…)`. Before reveal they are usually `SetOnStage(…,0)`, `SetVisible(…,0)` or SNEAKING.

### 1.3 Invisibility
* There are 20 entries in `Status_INVISIBLE.txt` (`StatusType "INVISIBLE"`): `INVISIBLE`, `INVISIBILITY` (spell), `GREATER_INVISIBILITY`, `INVISIBILITY_PANTHER`, potion and cloud variants, and others.
  * Boosts are `Invisibility(); Advantage(AttackRoll); DetectDisturbancesBlock(true)`.
  * `StatusPropertyFlags "UnavailableInActiveRoll;ExcludeFromPortraitRendering"` and `StatusGroups "SG_Invisible"`.
* Components: replicated `eoc::InvisibilityComponent`; server `esv::invisibility::RevealingSystem`; client `ecl::InvisibilityVisualComponent`, `ecl::InvisibilityFadingComponent` and `ecl::InvisibilityAttachmentComponent` (rendering only).
* Osiris: `IsInvisible(obj) -> int`, `IsInvisibleByScript(obj) -> int`.
* Reveals: See Invisibility (`SEE_INVISIBILITY`, `MAG_SEE_INVISIBILITY_HIDDEN_IGNORE_RESTING`), Truesight (`TRUESIGHT`), Faerie Fire, AoE damage ([Prima Games](https://primagames.com/tips/how-to-reveal-invisible-enemies-in-baldurs-gate-3)).
* Script-level invisibility is separate: `SetVisible(obj,0)` (196 uses in goals). It makes an entity not rendered at all and sets `esv::ItemFlags::Invisible` / `esv::CharacterFlags::Invisible`.

### 1.4 Hidden objects, traps, levers, secret doors
* **Traps.** These are items with template `IsTrap`. Osiris has `IsTrap(item)`, `IsTrapArmed(item)`, `SetTrapArmed(item,b)`, **`IsTrapDiscovered(item) -> int`** and `SetTrapDiscovered(item,b)`.
  * Discovery is a party-wide passive check (usually Perception). A discovered trap gets an orange outline ([bg3.wiki Trapped Object](https://bg3.wiki/wiki/Trapped_Object)).
  * Disarm DC lives in the template's `DisarmDifficultyClassID`. Server-side state: `esv::trap::DisarmingStateComponent` and `DisarmAttemptComponent`.
  * Goal `_CRIME_ItsATrap.txt` shows the pattern for a planted mine: `SetCanInteract(_Item,0); SetTrapArmed(_Item,1); SetTrapDiscovered(_Item,0);`. So **undiscovered ⇒ not interactable**.
  * A trap that fires reveals itself: `SetTrapDiscovered(_Item,1)` after `CastSpell`.
* **Hidden items, levers, buttons and keyholes** use a scripted perception system in `Shared/Story/RawFiles/Goals/GLO_Skillchecks.txt`:
  * The item fires `DualEntityEvent(_Player,_HiddenItem,"PUZZLE_HiddenPerception_CheckPlayer")`, with the DC from `GetVarInteger(_HiddenItem,"DC",_DC)`.
  * Then `PROC_TrySkillCheck(_Player,_HiddenItem,"Perception",_DC,"GLO_HiddenItemCheck")` runs.
  * On success: `DB_PerceptionSkillCheck(_Player,_HiddenItem,1)` and `SetEntityEvent(_HiddenItem,"StoryReveal")`. The item's own behaviour script then makes it visible/interactable.
  * On failure: `DB_PerceptionSkillCheck(_Player,_HiddenItem,0)`.
  * A parallel `DB_InvestigationSkillCheck` exists for Investigation.
  * The binary also has the behaviour type string `HiddenObject`.
  * **Consequence:** before discovery a hidden object is typically invisible (`IsInvisible==1` / `SetVisible 0`) and/or `GetCanInteract==0`. After discovery it is visible and interactable, and `DB_PerceptionSkillCheck(*, item, 1)` holds.
* **Secret doors.** `esv::ItemFlags::IsSecretDoor` (0x20000000000) is a server item flag. Its existence must never be exported. Report the door only once it is visible and interactable.
* `SetCanExamine(item,b)` / `eoc::item::ExamineDisabledComponent` stops Examine on specific objects (e.g. Halsin portal, magic mirror).

### 1.5 No world fog of war, and what the map shows
* There is no fog of war in the 3D world. Non-hidden NPCs and items in the loaded level render even without party line of sight (the camera can be panned freely). This matters for calibration (§4.1).
* The client fades out characters the party cannot see: `ecl::CharacterOutOfSightFadeRequestsSingletonComponent`, `ecl::CharacterOutOfSightFadeView` *(binary)*. This most plausibly affects sneaking/hidden hostiles. **UNVERIFIED** which characters it applies to.
* **Correction (2026-10-09, impl-vis Navigation):** there **is** a map shroud. The binary has `esv::ShroudManager` / `ecl::ShroudManager` (+ `ShroudProtocol`, `NETMSG_SHROUD_UPDATE`, `SaveLevel_ShroudManager`, states `Shrouded`/`SemiShrouded`, `ShroudSubregionMin/Max`). It is saved per level, but it is **not exposed to Lua** (bg3se keeps it as a `void*`), so our mod records its own breadcrumbs instead (`Server/Navigation.lua`). The paragraph below is the original, superseded finding.
* **Map reveal (original finding).** I found **no evidence of a per-area "explored mask"** in the native binary or the map XAML. There are no `FogOfWar`/`Explored`/`Reveal` symbols except container exploration, and `JournalMap.xaml`/`Minimap.xaml` bind only to `CurrentPlayer.WorldMap`, `CurrentPlayer.Waypoints`, `WaypointList`, `SelectedMapMarker`, custom markers, `RegionName` and `CurrentSubregionName`. The `NotExplored` string is a `gui::EItemFilterType` value (container contents).
  * Whether BG3's local map darkens unexplored areas is therefore **UNVERIFIED**. The brief's premise that the map reveals explored areas may be wrong. Treat map *art* as screenshot-only.
  * What is clearly gated is map **markers**: unlocked waypoints (`eoc::party::WaypointsComponent::m_UnlockedWaypoints`, replicated), trader markers (`eoc::trade::ShownTraderMapMarker{Name,Guid,Translate}Component`), quest and custom markers, and `MapMarkerStyle` (`eoc::item::MapMarkerStyleComponent`). The Osiris calls are `ShowMapMarker(char, markerID, show)` and `MapMarkerChangeLevel`.

### 1.6 Combat turn order
* The data is in `eoc::TurnOrderComponent` (on the combat entity): `CurrentTurnOrder`, `CurrentGroups[]`, `NextRoundTurnOrder`, `NextRoundGroups`, `Round`, `TimeRemaining`, `TurnTime`. Related components: `eoc::combat::ParticipantComponent`, `IsInCombatComponent`, `esv::TurnOrderSkippedComponent` and `esv::combat::JoiningComponent`/`LateJoinPenaltyComponent`.
* The UI timeline (`Public/Game/GUI/Library/TurnOrderLib.xaml`) binds per combatant to `Icon`, `EntityHandle`, `PlayerRelation`, `Stats.Health.PercentValue`, `Stats.IsAlive`, `ConcentrationSpell`, `IsCurrentTurn`, `ActedThisRound`, `IsWaitingNextRound`, `CanAct`, `IsNew`, `ShouldBeRemoved` and `Type`.
  * **Only HP percent**, no numbers. It also offers commands to highlight the combatant or put the camera on it.
* **Do hidden or invisible enemies appear in the bar? UNVERIFIED.** Community reports say an unnoticed sneaking unit is not in combat at all, and so not in the bar ([Steam discussion](https://steamcommunity.com/app/1086940/discussions/0/3823047564756015269/?ctp=2)). An invisible creature that *is* already in combat stays a participant. `INVISIBILITY` carries `ExcludeFromPortraitRendering`, which suggests its portrait is suppressed or neutralised.
  * Safe rule: emit an initiative entry for a non-party combatant only if that combatant currently passes the entity filter. Otherwise emit an anonymous placeholder (`{slot=n, known=false}`) only if the UI-ViewModel probe (§4.6) confirms the bar shows one.

---

## 2. Engine data (bg3se Lua API names)

### 2.1 Most relevant component types from the native binary *(binary)*

**Shared `eoc::` (exists on both server and client; ✓ means also replicated to the client):**

| Component | bg3se name | Fields / meaning |
|---|---|---|
| `eoc::sight::BaseComponent` ✓ | `Sight` | `Sight`, `DarkvisionRange`, `FOV`, `VerticalFOV` |
| `eoc::sight::DataComponent` ✓ | `SightData` | `SightUuid`, ranges |
| `eoc::sight::EntityViewshedComponent` ✓ | `SightEntityViewshed` | `Viewshed: HashSet<Guid>`. A set of sight UUIDs; most likely the **observers whose viewshed contains this entity**, or the reverse. **UNVERIFIED direction**, must be probed |
| `eoc::sight::IgnoreSurfacesComponent` ✓ | `IgnoreSurfaces` | surface types this viewer sees through |
| `eoc::StealthComponent` ✓ | `Stealth` | `SeekHiddenFlag`, `Position`, `SeekHiddenTimeout` |
| `eoc::sneak::IsSneakingComponent` ✓ | (tag) | sneaking |
| `eoc::InvisibilityComponent` ✓ | `Invisibility` | `field_0`, `field_4: vec3`, `field_10` (unnamed) |
| `eoc::DarknessComponent` ✓ | `Darkness` | `Obscurity`, `Sneaking`, `SneakingConesVisibility`, … |
| `eoc::GameplayLightComponent` ✓, `eoc::GameplayObscurityBoostComponent` ✓ | | lights, obscurity boosts |
| `eoc::through::CanSeeThroughComponent`, `eoc::CanSeeThroughBoostComponent` ✓ | | see-through surfaces |
| `eoc::status::ContainerComponent` ✓ | `StatusContainer` | `Statuses: {EntityHandle → StatusId}` |
| `eoc::HealthComponent` ✓ | `Health` | `Hp`, `MaxHp`, `TemporaryHp`, `MaxTemporaryHp`, `IsInvulnerable` |
| `eoc::DisplayNameComponent` ✓ | `DisplayName` | `Name`, `NameKey`, `Title`, **`UnknownKey`** ("unknown name" string) |
| `eoc::CustomNameComponent` ✓ | `CustomName` | `Name` |
| `eoc::lock::LockComponent` ✓ | `Lock` | `LockDC`, `LockDifficultyClassID`, `Key_M` |
| `eoc::inventory::IsLockedComponent` ✓ | | locked |
| `eoc::item::HasOpenedComponent` ✓ | `HasOpened` (tag) | **container has been opened** |
| `eoc::inventory::NewItemsInsideComponent` ✓ | | "new items" marker |
| `eoc::item::ExamineDisabledComponent` ✓ | | examine disabled |
| `eoc::OffStageComponent` ✓ | | off stage (not in world) |
| `eoc::TurnOrderComponent` ✓, `eoc::combat::ParticipantComponent` ✓, `eoc::combat::IsThreatenedComponent` ✓ | | combat |
| `eoc::relation::RelationComponent` ✓, `eoc::relation::FactionComponent` ✓ | | hostility |
| `eoc::party::WaypointsComponent` ✓ | | unlocked waypoints |
| `eoc::ruleset::RulesetComponent`, `eoc::ruleset::RulesetModifiersComponent` | `Ruleset`, `RulesetModifiers` | `Modifiers: {Guid → value}` (Custom Mode toggles) |
| `eoc::tag::HasExclamationDialogComponent` ✓ | | "!" over NPC |

**Server-only `esv::`:**
* `esv::sight::ViewshedParticipantComponent` (`ServerViewshedParticipant`): `Position`, **`CanSee: HashSet<EntityHandle>`**. This is the server's own per-viewer visible set. It is the best direct source, but it is internal, so verify it per patch.
* `esv::sight::AggregatedDataComponent`, `EntityLosCheckQueueComponent` (`EntityLosCheck{Observer, Target, Result}`), `AiGridViewshedComponent`, `EventsEnabledComponent`, `SightEventsOneFrameComponent`, `EntityViewshedContentsChangedEventOneFrameComponent{Added, Removed, Additions, Removals}`.
* Systems: `esv::sight::ViewshedSystem`, `esv::StealthSystem`, `esv::invisibility::RevealingSystem`, `esv::DarknessSystem`, `esv::light::GameplayObscuritySystem` and `esv::through::CanSeeThroughSystem`.
* `esv::darkness::DarknessActiveComponent`, `esv::light::GameplayObscurityComponent`, `esv::cover::IsVisionBlockerComponent`.
* `esv::trap::DisarmingStateComponent`, `DisarmAttemptComponent`.
* `esv::DisplayNameListComponent` (`Names[]`, `Titles[]`, `HideTitle`). Osiris `SetStoryDisplayName`/`ObjectSetTitleHidden` write to it.
* `esv::Character.Flags` (`ServerCharacterFlags`): `Invisible` 0x10000, `OffStage`, `SpotSneakers`, `Deactivated`, `InParty`, `IsPlayer`.
* `esv::Item.Flags` (`ServerItemFlags`): **`Invisible` 0x10000, `Invisible2` 0x8000000, `Known` 0x4000000, `IsSecretDoor` 0x20000000000, `HideHP` 0x400000, `InteractionDisabled` 0x200, `OffStage` 0x2, `IsContainer`, `IsDoor`, `StoryItem`**.

**Client-only `ecl::`:**
* `ecl::activation::CulledComponent` / `ecl::activation::CullingSystem`: **activation/streaming culling** (distance from the camera/party). It is not perception.
* `ecl::CharacterOutOfSightFade*`, `ecl::InvisibilityVisualComponent`/`InvisibilityFadingComponent`/`InvisibilityAttachmentComponent`, `ecl::invisibility::EffectSystem`, `ecl::StealthVFXComponent`/`System`, `ecl::DarknessSystem`, `ecl::TrapSystem`, `ecl::trigger::SightSystem`/`SightHelperComponent`, `ecl::highlight::DataComponent` (Alt-highlight / outline), `ecl::container::OngoingExplorationComponent` / `ClosedExplorationSingletonComponent` (UI "container exploration").
* `ecl::Character.Flags` (`ClientCharacterFlags`) only has `IsPlayer`, `Active`, `InDialog`, `Climbing`, `HasCharacterLight`, `VisibleOffStage` and `Airborne`. There is **no client "detected" flag**.
* `gui::tooltips::WorldTooltipsComponent`, `gui::registration::VMCombatDataSingletonComponent`, `gui::WorldMapSystem`, `gui::WorldMapMarkerSystem`.

### 2.2 Is "client entity is rendered/visible" a sound filter? **No**, for these reasons:
1. **Replication is not perception-filtered.** `Health`, `Stealth`, `Invisibility`, `IsSneaking`, `StatusContainer`, `Lock` (incl. `LockDC`), `DisplayName.Name` (true name), `Relation` and `TurnOrder` are all in the 580 replicated types, and the server pushes them for every loaded entity. A sneaking ambusher exists on the client with full HP and stats.
2. **Rendering is driven by several unrelated systems.** These include activation culling (distance-based streaming), camera-frustum culling, obstruction/roof fading (`ecl::obstruction::FadeSystem`, `ls::FadeGroupMappingSingletonComponent`), invisibility shaders (they *fade* rather than delete, and the player's own invisible characters stay semi-visible), timeline/cinematic visibility swaps, and `CharacterOutOfSightFade`. A culled entity may be perfectly perceivable (just off-screen), and a "rendered" invisible entity may be a translucent ally.
3. Exposing raw client state would leak **true names** of disguised NPCs (`LOW_HAG_DISGUISE`, `HAG_DISGUISE_MAYRINA`, `HAV_PHASM_OX_DISGUISE`, … are statuses on the NPC), **trap existence** (`IsTrap` template data), `LockDC`, and secret doors.

The client render state is useful only as a *secondary signal* (e.g. `ecl::InvisibilityFadingComponent` present and the entity is hostile ⇒ hidden). The authoritative filter should be server-side (§4).

### 2.3 Osiris API (server context; call as `Osi.X(...)` from bg3se Lua)
Signatures are from the generated `Osi.lua` ([LaughingLeader/BG3ModdingTools](https://github.com/LaughingLeader/BG3ModdingTools/blob/master/generated/Osi.lua)) and confirmed by usage in the extracted goals. Bool-ish queries return `1`/`0`.

| Query / call | Signature | Use |
|---|---|---|
| `CanSee` | `(source GUIDSTRING, target GUIDSTRING) -> int` | sight-system visibility (range, LOS, obscurity/darkvision; see caveat) |
| `CanSeeCached` | same | cheaper, cached variant |
| `HasLineOfSight` | `(source, target) -> int` | pure LOS |
| `IsInvisible` | `(object) -> int` | INVISIBLE-type status or script invisibility |
| `IsInvisibleByScript` | `(object) -> int` | `SetVisible(obj,0)` |
| `IsOnStage` | `(object) -> int` | off-stage entities are not in the world |
| `GetObscuredState` / `GetObscuredStateAtPosition` | `(object) -> string` / `(x,y,z) -> string` | "Clear" / "LightlyObscured" / "HeavilyObscured" |
| `IsTrap`, `IsTrapArmed`, **`IsTrapDiscovered`** | `(item) -> int` | traps |
| `GetCanInteract` | `(item) -> int` | hidden objects are usually non-interactable until found |
| `CanSpotSneakers` | `(character) -> int` | |
| `GetDisplayName` | `(target) -> string` (translated-string key) | **the name the UI shows** (story overrides applied); resolve with `Ext.Loca.GetTranslatedString` |
| `HasActiveStatus` | `(target, status) -> int` | `SNEAKING`, `SEE_INVISIBILITY`, `TRUESIGHT`, … |
| `IsLocked` | `(item) -> int` | |
| `GetHitpoints`, `GetMaxHitpoints`, `GetHitpointsPercentage` | `(entity) -> int/number` | |
| `RequestPassiveRoll`, `RequestPassiveRollVersusSkill` | see Osi.lua | (never call: they change game state) |
| Events | `Saw(char1, char2, int)`, `LostSightOf(char1, char2)`, `ObscuredStateChanged(char, state)`, `Opened(item)`, `Closed(item)`, `UseStarted(char,item)`, `CharacterLootedCharacter(...)` | listen with `Ext.Osiris.RegisterListener` |

**Caveats.**
* Whether `CanSee` accounts for the *target's* SNEAKING/INVISIBLE state (as opposed to just range + LOS + light) is **UNVERIFIED**. The Brawl mod's `isVisible()` ([tinybike/Brawl Utils.lua](https://github.com/tinybike/Brawl/blob/master/Brawl/Mods/Brawl/ScriptExtender/Lua/Server/Utils.lua)) adds `IsInvisible==0 and HasActiveStatus(target,"SNEAKING")==0` explicitly, with a See Invisibility (≤9 m) and Truesight override. That suggests modders do not rely on `CanSee` for this, and neither should we.
* `Saw`/`LostSightOf` are only raised for characters with sight events enabled (`StartSightEvents(char)`; component `esv::sight::EventsEnabledComponent`). Whether party members have them enabled by default is UNVERIFIED.

---

## 3. What the in-game UI shows (the fairness baseline)

The bindings below are extracted from the shipped XAML. They are the exact ViewModel properties each widget displays. The exporter's per-entity field set should be the union of what these widgets show for that entity.

| UI element | XAML | What it shows (bound fields) |
|---|---|---|
| **World tooltips** (Alt-highlight labels / hover over items) | `Pages/WorldTooltips.xaml` | `Name`, `Description`, `Item.Rarity`, `Item.IsStoryItem`, `ContainerState`, `IsStealing`, screen `Position`. Only for objects the game chooses to label: interactable, visible items. *Alt does not reveal undiscovered objects.* ([PCGamesN](https://www.pcgamesn.com/baldurs-gate-3/highlight-objects)) |
| **Cursor text / hover** | `Pages/CursorText.xaml` | `CurrentTarget.Name`, `.Type`, `.Rarity`, `.IsStoryItem`, `.IsExaminable`, `PlayerRelation`, `Stats.ArmorClass` (via templates), HP target values, hit chance (`HitChanceDesc` advantages/disadvantages/modifiers), `Cursor.Distance`, `CursorObscuredState`, surface/cloud info, AoO warning, `LethalOutcome` |
| **Target info** (top-centre bar when targeting) | `Pages/TargetInfo.xaml` | target `Stats.Health.Value`, `StatusEffects` (icon, name, duration), `ConcentrationSpell`, `PlayerRelation`, `CharacterType`, `TotalHitChance`, `DamagePreviewWithResistances`, advantage/disadvantage reasons |
| **Examine panel** | `Pages/Examine_c.xaml` (+ `Examine.xaml`) | `Name`, `Title`, `Stats.Race.DisplayName`/`.Type`/`.Description`, `Stats.ClassList`/`ClassDisplayName`/`SubclassDisplayName` + level, **`Stats.Health`** (hidden under `HIDE_NPC_HP`), **`Stats.ArmorClass`**, **`Stats.Abilities`** (6 scores + saving-throw modifiers `SavingThrow.AdditionalValue`), **`Stats.Resistances`** (`DamageType`, `DamageTypeValue` (resist/immune/vulnerable), `Magical`/`NonMagical`), `Stats.Passives` (character features), `StatusEffects` (+ `Duration`), `Stats.Initiative`, `Stats.MovementResource`, `Stats.DarkVisionRange`, `Stats.Weight`, `EncumbranceStats`. Opened with right-click → Examine or `T`. bg3.wiki states HP/AC/ability scores are "always available in Baldur's Gate 3" ([Know Your Enemy](https://bg3.wiki/wiki/Know_Your_Enemy)) |
| **Turn order** | `Library/TurnOrderLib.xaml` | per combatant: portrait `Icon`, `PlayerRelation`, **HP percent only**, alive, concentration spell, current/acted/waiting flags, round grouping |
| **Party panel / portraits** | `PlayerPortraits*.xaml`, `PartyPanel.xaml` | full data for party members (HP, temp HP, statuses, resources, concentration). Party data is never restricted |
| **Overheads** | `Pages/OverheadInfo.xaml`, `Library/OverheadPassives.xaml` | damage numbers, subtitles/speech, **passive roll results** (`EntityOverheads.PassiveRolls`: `IsSkillCheck`, `Successful`), "spotted" indicators. Failed perception rolls are hidden under `HIDE_FAILED_PERCEPTION_ROLLS` |
| **Passive roll toasts** | `Pages/PassiveRoll.xaml` | `SkillOrAbility`, `RollResult`, `Success` (in dialogue, hidden under `DIALOGUE_PASSIVE_ROLL_HIDE`) |
| **Active roll screen** (dialogue/lockpick checks) | `Pages/ActiveRoll.xaml`, `Library/DiceAnimation.xaml` | roll `Name`/`SkillOrAbility`, **`DifficultyCheck` (the DC)** unless `DIALOGUE_DC_HIDE`, `Roll.Modifiers`, `Roll.Advantages`, `BoostModifierList` (Guidance, Bardic Inspiration…), `InspirationPoints`, `NearbyPartyCharacters`, then `FinalResult`, `Success` |
| **Dialogue** | `Pages/Dialogue.xaml`, `Library/DialogueTemplates.xaml` | speaker, `BodyText`, `Answers[]` (`Text`, `Type`; check-tags like "[PERSUASION]" are inside the answer text/inline data). DC is shown only on the roll screen, or in the answer if `DIALOGUE_DC_PREVIEW` is on |
| **Pickpocket** | `Pages/Pickpocket.xaml`, `Pages/Pickpocket_c.xaml` | the NPC's pockets (`Container.Inventory.Slots`) only while the window is open; for the selected item `Roll.DifficultyCheck` under "Target" (**the DC is shown**, no ShowDC/Custom-Mode gate) and `Roll.Chance` as a pie (green >70 %, red <30 %), then `RolledNumber`, `Success` (checked 2026-10-17) |
| **Container / loot** | `Pages/Container.xaml` | `LootInventory.Slots` (items) **only for the opened container**, container HP, owner |
| **Minimap / journal map** | `Minimap.xaml`, `JournalMap.xaml` | map image (`CurrentPlayer.WorldMap`), `RegionName`, `CurrentSubregionName`, `Waypoints`/`WaypointList` (unlocked only), `SelectedMapMarker`, custom markers, `IsInDangerZone`, pings |
| **Journal** | `Journal*.xaml` | quests and steps the player has received (`QuestVisitedSteps`), dialogue history, combat log, books read, tutorials |
| **Combat log** | `Pages/CombatLog.xaml` | text of rolls, damage, statuses, with attacker and defender as the UI names them |
| **Item tooltip** | `Library/Tooltips.xaml` | name, rarity, description, weight, value, damage dice, AC, properties, passives/boosts, spells granted, requirements, `IsHidden` (some tooltip rows are themselves hidden) |

Other UI signals of note:
* `CurrentPlayer.UIData.ExamineWisdomTier` exists *(binary string)* but no shipped XAML binds it. It is likely a vestige of Early-Access Wisdom-gated examine, so ignore it.
* `RuleSetModifierValueBehavior … ModifierName="HIDE_NPC_HP"` appears in `Examine_c.xaml`, `DataTemplates.xaml` (×3) and `CharacterSheetTemplates_k.xaml`. `HIDE_FAILED_PERCEPTION_ROLLS` is in `OverheadPassives.xaml` and `DIALOGUE_PASSIVE_ROLL_HIDE` in `NotificationLib.xaml`. These toggles are **applied in the UI layer**, so our exporter must apply them as well.

### 3.1 Ruleset modifiers that change what the UI shows (`Public/Shared/Ruleset/RulesetModifiers.lsx`)

| Name | UUID | Default | Effect on exporter |
|---|---|---|---|
| `HIDE_NPC_HP` | `9b349f94-e520-4c49-83e1-c0a8e0543710` | false | omit non-party `hp`, `maxHp`, `hpPercent` |
| `DIALOGUE_DC_HIDE` | `743a3998-636a-4b39-951b-f742a56b07fc` | false | omit `dc` on roll screens |
| `DIALOGUE_DC_PREVIEW` | `1e5d2feb-3c81-4c0e-86d8-37337603234a` | false | if true, DCs may be attached to dialogue options *before* selection |
| `HIDE_FAILED_PERCEPTION_ROLLS` | `19e33ff1-76fc-4447-a5ff-09a001e9544f` | false | drop failed passive Perception results from the event log |
| `DIALOGUE_PASSIVE_ROLL_HIDE` | `38fc0d10-ee64-40d6-b1d9-35f8de95b972` | false | drop passive dialogue roll results |

These are read from `eoc::ruleset::RulesetModifiersComponent.Modifiers[guid]`, a singleton on a global entity. Find it with `Ext.Entity.GetAllEntitiesWithComponent("RulesetModifiers")[1]`. Custom Mode options are fixed once a campaign starts ([Prima Games, Custom Mode](https://primagames.com/tips/what-is-custom-mode-in-baldurs-gate-3-explained)).

---

## 4. Visibility filter algorithm

### 4.1 Principles
1. **Mirror the UI, not the engine.** For every entity, export only the fields the tooltip, Examine, target-info and turn-order widgets would show (§3), and only when the entity is perceivable.
2. **Perceivable = the engine's own perception answer, ORed across the party.** A human sees through all party members' eyes, but the screen is not limited to their cones. Default to the slightly *stricter* rule (party sight), so the filter never leaks. The agent can still take screenshots for what is on screen, and screenshots are inherently fair.
3. **Hidden-ness overrides sight.** Hidden-ness means SNEAKING/INVISIBLE hostile or neutral NPCs, script-invisible or off-stage entities, undiscovered traps and hidden objects, and secret doors. It is overridden only by the same counters the game uses (See Invisibility, Truesight, a successful perception reveal).
4. **Memory, not omniscience.** Once an entity has been seen, keep a `lastSeen {position, time, hpPercent}` snapshot (humans remember), but never refresh it while the entity is unseen.
5. **Names come from the UI path.** Use `Osi.GetDisplayName` → `Ext.Loca.GetTranslatedString`, which honours story overrides like "Hooded Figure" and disguises. Never use template names, `DisplayName.NameKey` when an override exists, GUIDs, or `ServerDisplayNameList` entries.

### 4.2 Decision rules per entity class

Notation: `P` = party members that are on stage and not dead-and-gone (use `Osi.DB_PartyMembers` or `PartyMember` entities). `seenBy(E) = any p∈P: Osi.CanSee(p,E)==1`.

**A. Characters (NPCs, enemies, summons)**

| Condition | Report? |
|---|---|
| `IsOnStage(E)==0`, `IsInvisibleByScript(E)==1`, or `ServerCharacterFlags.Invisible` | **No** |
| Party member or party summon/follower | Yes, full data (party sheet) |
| `IsInvisible(E)==1` (INVISIBLE-type status) and hostile/neutral | Only if some `p` has `SEE_INVISIBILITY`/`TRUESIGHT`-class status and is within the reveal range (See Invisibility: the Brawl heuristic uses ≤ 9 m; use the status's actual range if known) and `HasLineOfSight(p,E)`, **or** E is FAERIE_FIRE'd/outlined. Otherwise not reported. If E was already a known combatant, keep only `{id, name, "invisible", lastSeen}` without a position update |
| `HasActiveStatus(E,"SNEAKING")==1` (or `IsSneaking` component) and hostile to party | Report only if `seenBy(E)` **and** the engine has detected it. Treat "detected" as `CanSee` returning 1 *and* (`Darkness.Obscurity`/`GetObscuredState(E)` ≠ HeavilyObscured or some `p` has darkvision covering it). **UNVERIFIED**: confirm with the probe in §4.6 that `CanSee` is 0 for undetected sneakers. Until confirmed, be strict: unreported unless it attacked, cast, or was revealed by script |
| Otherwise | Report if `seenBy(E)`, or (calibration option, default off) if within `CAMERA_RADIUS` (~30 m) of the party and `HasLineOfSight` from any `p` |

Fields when reported (UI-mirrored):
* `name` (UI name), `title` if not hidden, `relation` (hostile/neutral/ally from `Relation` + the `PlayerRelation` the UI uses), `position` (only while seen), `distance`, `isInCombat`.
* `hp`, `maxHp`, `tempHp`: unless `HIDE_NPC_HP`. With the toggle on, nothing; not even percent.
* Examine block (§3): `race`, `classes/level`, `ac`, `abilities` (scores + save mods), `resistances/immunities/vulnerabilities`, `passives` (only those with a display name), `initiative`, `movement`, `darkvisionRange`, `weight`.
* `statuses`: only statuses whose stats entry has a non-empty `DisplayName` and **does not** have `StatusPropertyFlags` containing `DisablePortraitIndicator`. This flag hides 1,058 of 1,656 status entries from portraits. The rule removes technical/story statuses such as `LOW_HAG_DISGUISE`, `*_TECHNICAL` and `WYR_PAINTINGPUZZLE_HIDDEN`. Include `duration` (UI shows it).
* `concentration` spell (shown in turn order and target info).
* `hasDialogue` ("!" exclamation: `eoc::tag::HasExclamationDialogComponent`), `isTrader` (UI shows the trade cursor).
* If `ExamineDisabled` / `SetCanExamine(…,0)` applies: only name, relation and position.

**B. Items: world objects, doors, levers, containers, traps**

| Condition | Report? |
|---|---|
| Off-stage, `IsInvisible==1`, `IsInvisibleByScript==1`, or `ServerItemFlags.Invisible/Invisible2` | **No** (this covers undiscovered hidden objects) |
| `Osi.IsTrap(E)==1 and Osi.IsTrapDiscovered(E)==0` | Standalone traps (pressure plate, tripwire, mine): **no**. Trapped container/door: report the object but **never** a `trapped` field |
| `IsTrap==1 and IsTrapDiscovered==1` | Report with `trap = {discovered=true, armed=IsTrapArmed}`; the orange outline shows armed state. The disarm DC appears only on the roll screen (§C) |
| `ServerItemFlags.IsSecretDoor` and not (visible and `GetCanInteract==1`) | **No.** Never export the `IsSecretDoor` bit itself |
| `GetCanInteract(E)==0` and not a door/container/ladder | No. It is scenery; the game doesn't highlight it either |
| In an inventory (`InventoryMember` present) | Only if the owning container is the party's inventory, an equipped item of a reported NPC (Examine shows equipment? **UNVERIFIED**, so default no), or a container with `HasOpened` while the party has it open (`Opened` event) |
| Otherwise | Report if `seenBy(E)` (characters as observers: `CanSee` works with item targets per the GUIDSTRING signature) or `HasLineOfSight(p,E)` within ~25 m |

Item fields:
* `name` (UI), `rarity`, `isStoryItem`, `description` (item tooltip), `weight`, `value`, and weapon/armour stats as in the tooltip.
* `isLocked` (`IsLocked`). The UI says "Locked"; `LockDC` is *not* exported except on the lockpicking roll screen.
* `isContainer`, `hasBeenOpened` (`HasOpened`), `isOwned` (`IsOwned` → the UI shows red "steal" text: `IsStealing`), `isDoor`, open/closed state, and for destructibles `hp` (unless `ServerItemFlags.HideHP`).
* `foundByPerception` = `DB_PerceptionSkillCheck(_, E, 1)` exists. This is only an annotation; the item is already visible by then.

**C. Rolls, DCs and dice results**
* Active rolls (dialogue skill checks, lockpicking, disarming): when the roll UI is up, export `{skill, dc (unless DIALOGUE_DC_HIDE), modifiers, advantage, availableBoosts}`. After the roll, export `{natural, total, success}`. The UI shows all of these.
* Dialogue options: export the option text including the visible check tag (e.g. "[DECEPTION]"). Export `dc` before selection **only if `DIALOGUE_DC_PREVIEW`** is on.
* Passive rolls: export `{roller, skill, success, total?}` when the overhead/toast shows them. Drop failed Perception rolls if `HIDE_FAILED_PERCEPTION_ROLLS`, and dialogue passive rolls if `DIALOGUE_PASSIVE_ROLL_HIDE`. **Never export the subject of a failed perception check.** A failed roll's subject is exactly the thing you didn't find. The UI shows "Perception: failed" without saying what the check was against.
* Enemy attack rolls and saving throws: export them as the combat log prints them. The log shows enemy rolls and outcomes; do **not** export the log entry for an enemy the party cannot perceive. The game prints "Someone/Unknown" in that case (**UNVERIFIED**), so mirror that.

**D. Combat / initiative**
* Party entries: full data.
* Other combatants: emit `{name, relation, hpPercent (unless HIDE_NPC_HP), concentration, isCurrentTurn, acted}` only if the entity passes rule A. Otherwise emit `{unknown=true}` only if §4.6 shows the bar renders an entry, else omit.
* `Round` and whose turn it is (if that combatant is perceivable): yes.

**E. Map**
* Export `regionName`/`subregionName` (shown on the minimap), the party's own positions, **unlocked** waypoints (`WaypointsComponent`), quest/trader/custom markers the UI has, and `IsInDangerZone`.
* Do not export level-wide entity lists or triggers. Never export an "explored mask" either: none was found, and if one exists, the map is screenshot-only.

**F. Quests / journal**
* Only what the journal VM holds: received quests, visited steps, and the descriptions shown. Never Osiris flags (`DB_*`, `GetFlag`), dialogue node conditions, or unlocked-but-unseen steps.

### 4.3 "Never expose" fields (hard deny-list)
1. Any entity failing the rules above: undetected sneakers, unrevealed invisible creatures, off-stage/script-invisible entities, undiscovered traps, undiscovered hidden objects, secret doors before discovery, and the contents of unopened containers or of NPC inventories (except via the pickpocket UI when it is open).
2. `IsTrap` on undiscovered traps; the `IsSecretDoor` flag; `DisarmDifficultyClassID`/`LockDC`/`LockDifficultyClassID` outside the matching roll screen; `GetVarInteger(item,"DC")` / any hidden-object DC.
3. **True identities:** template names/IDs, root template GUIDs, `DisplayName.NameKey` when a story name override applies, the `ServerDisplayNameList`, entity UUIDs to the agent (use opaque session-stable IDs), and disguise/`*_TECHNICAL`/story statuses (anything with `DisablePortraitIndicator` or no `DisplayName`).
4. Osiris databases and flags (`DB_*`, `GetFlag`, `GetVar*`), dialogue conditions, unchosen-branch content, `DialogFileName`, quest steps not yet in the journal, story timers beyond what the UI shows.
5. AI internals: `AiArchetype`, `AiModifiers`, behaviour/action machines, `ThreatRange`, threat tables, `IsBoss`, planned targets, `InterestedInItems`.
6. Treasure tables, `TradeTreasure`, NPC inventories and equipment sets (only what Examine/visuals show; default: none), unseen spellbooks (`SpellBook` of NPCs: the player learns spells when they are cast).
7. Exact numeric HP of anything when `HIDE_NPC_HP` is on; HP of items with `HideHP`.
8. The *subject* of failed passive checks; the results of hidden (scripted) rolls the UI doesn't display; NPC passive Perception vs our Stealth margins.
9. `StealthComponent.Position` (an NPC's *last-known position of us* is internal), NPC vision cones while the party is not sneaking, and `esv::sight` viewsheds of NPCs (who-sees-whom from the enemy side).
10. Surprise state of enemies ahead of combat, combat group IDs, and `CanJoinCombat`/`StartCombatRange` of not-yet-hostile NPCs.

### 4.4 Data sources ranked by trust

| Rank | Source | Why |
|---|---|---|
| 1 | Osiris built-in queries (`CanSee`, `HasLineOfSight`, `IsInvisible*`, `IsOnStage`, `IsTrapDiscovered`, `GetCanInteract`, `GetDisplayName`, `HasActiveStatus`) | Stable public story API across patches; same semantics the game scripts rely on |
| 2 | Replicated `eoc::` components read via `Ext.Entity` (`Health`, `StatusContainer`, `Stats`, `Resistances`, `Lock`, `HasOpened`, `TurnOrder`, `RulesetModifiers`) | Authoritative values, but **unfiltered**: they must pass through the rules |
| 3 | Server-internal sight (`ServerViewshedParticipant.CanSee`, `SightEntityViewshed`) | Precise, but internal and may break per patch. Use as a cross-check or an optimisation |
| 4 | UI ViewModels via `Ext.UI.GetRoot()` → DataContext (e.g. `CurrentPlayer.WorldTooltips`, `CurrentPlayer.UIData.ExamineTarget`, combatant lists) | **Fair by construction** (it *is* what the UI shows), but tied to Noesis and must be accessed in `Ext.UI.Defer` ([bg3se API.md §Noesis](https://github.com/Norbyte/bg3se/blob/main/Docs/API.md)). Best for validating the filter |
| 5 | Client render state (`Culled`, invisibility fade, `CharacterOutOfSightFade`) | Not a perception signal (§2.2); hint only |
| — | Screenshots | Fair by definition; the fallback for anything ambiguous (map art, scene layout) |

### 4.5 Implementation sketch (bg3se Lua, server context)

```lua
-- Mods/AgentBridge/ScriptExtender/Lua/Server/Visibility.lua
local V = {}

local RULE = {
  HIDE_NPC_HP                  = "9b349f94-e520-4c49-83e1-c0a8e0543710",
  DIALOGUE_DC_HIDE             = "743a3998-636a-4b39-951b-f742a56b07fc",
  DIALOGUE_DC_PREVIEW          = "1e5d2feb-3c81-4c0e-86d8-37337603234a",
  HIDE_FAILED_PERCEPTION_ROLLS = "19e33ff1-76fc-4447-a5ff-09a001e9544f",
  DIALOGUE_PASSIVE_ROLL_HIDE   = "38fc0d10-ee64-40d6-b1d9-35f8de95b972",
}
local REVEAL_INVIS = { SEE_INVISIBILITY = 9.0, MAG_SEE_INVISIBILITY_HIDDEN_IGNORE_RESTING = 9.0,
                       TRUESIGHT = 36.0, MOD_Generic_Truesight = 36.0 } -- ranges: VERIFY from stats
local CAMERA_RADIUS = nil   -- calibration knob; nil = strict party-sight mode

local function b(x) return x == 1 or x == true end

function V.Rules()
  local e = Ext.Entity.GetAllEntitiesWithComponent("RulesetModifiers")[1]
  local m = e and e.RulesetModifiers.Modifiers or {}
  local r = {}
  for k, guid in pairs(RULE) do r[k] = m[guid] == true end
  return r
end

function V.Party()
  local out = {}
  for _, row in pairs(Osi.DB_PartyMembers:Get(nil)) do
    local uuid = row[1]
    if b(Osi.IsOnStage(uuid)) and not b(Osi.IsDead(uuid)) then out[#out+1] = uuid end
  end
  return out
end

-- Hidden-ness that overrides plain sight
local function scriptHidden(E)
  return not b(Osi.IsOnStage(E)) or b(Osi.IsInvisibleByScript(E))
end

local function revealsInvisible(p, E)
  for status, range in pairs(REVEAL_INVIS) do
    if b(Osi.HasActiveStatus(p, status)) and Osi.GetDistanceTo(p, E) <= range
       and b(Osi.HasLineOfSight(p, E)) then return true end
  end
  return b(Osi.HasActiveStatus(E, "FAERIE_FIRE"))   -- VERIFY status id
end

local function seenByParty(P, E)
  for _, p in ipairs(P) do
    if b(Osi.CanSee(p, E)) then return true end
    if CAMERA_RADIUS and Osi.GetDistanceTo(p, E) <= CAMERA_RADIUS
       and b(Osi.HasLineOfSight(p, E)) then return true end
  end
  return false
end

function V.CharacterVisible(P, E)
  if b(Osi.IsPartyMember(E, 1)) then return true end         -- party: always
  if scriptHidden(E) then return false end
  local hostileOrNeutral = not b(Osi.IsAlly(P[1], E))
  if hostileOrNeutral and b(Osi.IsInvisible(E)) then
    for _, p in ipairs(P) do if revealsInvisible(p, E) then return true end end
    return false
  end
  if hostileOrNeutral and b(Osi.HasActiveStatus(E, "SNEAKING")) then
    -- strict until probe confirms CanSee==0 for undetected sneakers
    return V.SneakerDetected(P, E)   -- e.g. seenByParty + DB/listener state from Saw/StealthSpotted
  end
  return seenByParty(P, E)
end

function V.ItemVisible(P, E)
  if scriptHidden(E) or b(Osi.IsInvisible(E)) then return false end
  local ent = Ext.Entity.Get(E)
  local srv = ent.ServerItem
  if srv and (srv.Flags.Invisible or srv.Flags.Invisible2) then return false end   -- bitfield access per bg3se
  if b(Osi.IsTrap(E)) and not b(Osi.IsTrapDiscovered(E)) then
    if not (srv and (srv.Flags.IsContainer or srv.Flags.IsDoor)) then return false end
  end
  if srv and srv.Flags.IsSecretDoor and not b(Osi.GetCanInteract(E)) then return false end
  if not b(Osi.GetCanInteract(E)) and not (srv and (srv.Flags.IsDoor or srv.Flags.IsContainer)) then
    return false
  end
  if ent.InventoryMember then return V.InOpenedContainer(ent) end
  return seenByParty(P, E)
end

-- UI-mirrored projection
local function uiName(E)
  local key = Osi.GetDisplayName(E)
  return key and Ext.Loca.GetTranslatedString(key) or "Unknown"
end

local function visibleStatuses(ent)
  local out = {}
  local sc = ent.StatusContainer
  if not sc then return out end
  for _, id in pairs(sc.Statuses) do
    local st = Ext.Stats.Get(id)
    if st and st.DisplayName ~= "" then
      local flags = st.StatusPropertyFlags or {}
      local hidden = false
      for _, f in ipairs(flags) do if f == "DisablePortraitIndicator" then hidden = true end end
      if not hidden then out[#out+1] = { id = id, name = Ext.Loca.GetTranslatedString(st.DisplayName) } end
    end
  end
  return out
end

function V.ProjectCharacter(E, rules, isParty)
  local ent = Ext.Entity.Get(E)
  local o = { id = V.OpaqueId(E), name = uiName(E), relation = V.Relation(E),
              position = {Osi.GetPosition(E)}, statuses = visibleStatuses(ent) }
  if isParty or not rules.HIDE_NPC_HP then
    o.hp, o.maxHp, o.tempHp = ent.Health.Hp, ent.Health.MaxHp, ent.Health.TemporaryHp
  end
  if not ent.ExamineDisabled then
    o.examine = V.ExamineBlock(ent)   -- AC, abilities+saves, resistances, race, classes/level,
                                      -- displayable passives, initiative, movement, darkvision
  end
  return o
end

-- Memory: last-seen snapshots, never refreshed while unseen
V.memory = {}
function V.Tick()
  local P, rules, out = V.Party(), V.Rules(), { characters = {}, items = {} }
  for _, E in ipairs(V.CandidateCharacters(P, 60.0)) do       -- level-local entities near party
    if V.CharacterVisible(P, E) then
      local o = V.ProjectCharacter(E, rules, b(Osi.IsPartyMember(E, 1)))
      V.memory[o.id] = { name = o.name, position = o.position, time = Ext.Utils.MonotonicTime() }
      table.insert(out.characters, o)
    end
  end
  for _, E in ipairs(V.CandidateItems(P, 30.0)) do
    if V.ItemVisible(P, E) then table.insert(out.items, V.ProjectItem(E, rules)) end
  end
  out.remembered = V.memory   -- agent sees stale positions explicitly marked as such
  return out
end

return V
```

Notes on the sketch:
* `Osi.*` exists only in the **server** Lua context (bg3se API.md §"Calling Osiris from Lua"). Run the exporter on the server side, and ship the state to the client or out-of-process via bg3se `Ext.Net` / a file / a socket (see other research files).
* Candidate enumeration: use `Ext.Entity.GetAllEntitiesWithComponent("ServerCharacter")`/`("ServerItem")` filtered by level and distance. **Don't** use any engine query that returns hidden entities directly to the agent.
* Bitfield access syntax for `Flags` follows bg3se's bitfield rules (API.md §Bitfields). Verify it against the installed SE version.
* Container contents: register `Ext.Osiris.RegisterListener("Opened", 1, "after", …)` and `("Closed", …)`, and check the `HasOpened` tag. Export the contents only while open (live) and remember the last-seen snapshot afterwards.
* Throttle: `CanSee` is cheap-ish (`CanSeeCached` exists), but cap candidates to ~60 m and ≤ 4 observers.

### 4.6 Required runtime probes (before trusting the filter)
These are cheap one-session experiments. Run them once the mod loads, in a debug save:
1. **Sneaking NPC:** find an ambush (e.g. the goblins near the Blighted Village gate or the Risen Road ambush) before the perception reveal. Log `CanSee(p,E)`, `HasLineOfSight`, `IsInvisible`, `HasActiveStatus(E,"SNEAKING")`, `ServerViewshedParticipant.CanSee`, and `SightEntityViewshed.Viewshed` for E and p. Then log the same after the reveal. This settles whether `CanSee` alone handles detection, and which direction `EntityViewshed` points.
2. **Invisible enemy in combat:** take a turn-order screenshot and compare it with `TurnOrder.CurrentGroups`. This settles whether the bar shows an entry or a placeholder.
3. **Hidden object:** log `IsInvisible`, `GetCanInteract`, `ServerItem.Flags` and `DB_PerceptionSkillCheck` before and after a perception reveal. Confirms which flag the `StoryReveal` behaviour toggles.
4. **Trap:** log `IsTrapDiscovered` and `GetCanInteract` around discovery.
5. **UI cross-check:** under `Ext.UI.Defer`, dump `CurrentPlayer.WorldTooltips` (Alt held) and diff it against our item list. Any item we export that the UI never labels is a leak candidate.

---

## Recommendation

**Use a server-side, rule-based filter driven by Osiris perception queries, with a UI-mirroring projection, and validate it against the UI ViewModels.** Specifically:

1. **Gate on perception, using the engine's own answers.** Report an entity if any party member `CanSee`s it. Hidden-ness overrides that: off-stage or script-invisible entities, INVISIBLE-type statuses without a reveal, undetected SNEAKING hostiles, undiscovered traps, non-interactable hidden objects and secret doors. Combine with explicit status checks, because `CanSee`'s handling of stealth is unverified and modders don't rely on it (Brawl). Default to the strict "party sight" mode. A human sees a bit more (non-hidden NPCs anywhere on camera); the optional `CAMERA_RADIUS` mode or screenshots cover that gap fairly.
2. **Project to UI fields only.** Names via `GetDisplayName`. HP/AC/abilities/resistances/displayable passives exactly as the Examine panel shows them (bg3.wiki: always available in BG3). Statuses filtered by `DisablePortraitIndicator`. Turn order with HP percent. Locks as "locked" without a DC. Container contents only while opened. DCs and dice results only on roll screens.
3. **Honour the Custom-Mode toggles** (`HIDE_NPC_HP`, `DIALOGUE_DC_HIDE`, `DIALOGUE_DC_PREVIEW`, `HIDE_FAILED_PERCEPTION_ROLLS`, `DIALOGUE_PASSIVE_ROLL_HIDE`) read from `RulesetModifiersComponent`, since the game's own XAML applies them.
4. **Enforce the deny-list (§4.3) as a final allowlist serializer.** Output only whitelisted keys, so a coding mistake can't leak a raw component.
5. **Don't use client render state as the filter.** The client holds unfiltered replicated state (580 component types including `Health`, `Stealth`, `Lock.LockDC`), and rendering mixes culling, fades and shaders unrelated to perception. Use it, and better the Noesis ViewModels (`Ext.UI`), only for **validation and probes**.
6. **Run the six probes in §4.6** in the first session after the mod works. They resolve the UNVERIFIED points: `CanSee` vs. sneakers, `EntityViewshed` direction, hidden combatants in the turn bar, and map reveal.

Why this approach: Osiris queries are the most stable, best-understood perception API (the game's own story scripts use them for exactly these questions). Mirroring the shipped UI widgets gives a concrete, auditable definition of "what the player can see". The allowlist projection makes leaks a review-time problem rather than a runtime one.

---

## Sources
* bg3se (Norbyte): [repo](https://github.com/Norbyte/bg3se), [Docs/API.md](https://github.com/Norbyte/bg3se/blob/main/Docs/API.md). Local clone at commit `91abbf2`: `BG3Extender/GameDefinitions/Components/{Data,Components,Combat,Item}.h`, `GameDefinitions/Enumerations/ECS.inl` (Server/Client Character/Item flags), `IdeHelpers/ExtIdeHelpers.lua`
* Osiris signatures: [LaughingLeader/BG3ModdingTools generated/Osi.lua](https://github.com/LaughingLeader/BG3ModdingTools/blob/master/generated/Osi.lua) (same file mirrored in many mods' IdeHelpers)
* Community visibility helper: [tinybike/Brawl Server/Utils.lua `isVisible`](https://github.com/tinybike/Brawl/blob/master/Brawl/Mods/Brawl/ScriptExtender/Lua/Server/Utils.lua)
* [bg3.wiki Sight mechanics](https://bg3.wiki/wiki/Sight_mechanics), [Trapped Object](https://bg3.wiki/wiki/Trapped_Object), [Turn order](https://bg3.wiki/wiki/Turn_order), [Know Your Enemy](https://bg3.wiki/wiki/Know_Your_Enemy)
* [PCGamesN: highlighting objects / passive perception](https://www.pcgamesn.com/baldurs-gate-3/highlight-objects); [Prima Games: examine enemy resistances](https://primagames.com/gaming/how-to-examine-enemy-weaknesses-and-resistances-in-baldurs-gate-3); [Prima Games: reveal invisible enemies](https://primagames.com/tips/how-to-reveal-invisible-enemies-in-baldurs-gate-3); [Prima Games: Custom Mode](https://primagames.com/tips/what-is-custom-mode-in-baldurs-gate-3-explained); [Steam discussion on stealth/initiative](https://steamcommunity.com/app/1086940/discussions/0/3823047564756015269/?ctp=2)
* Local, read-only: `~/.local/share/Steam/steamapps/common/Baldurs Gate 3/bin/bg3` (`nm -C`, `strings`); `Data/{Gustav,Shared,Game,Patch8_HotFix10}.pak` (Goals, Stats, Ruleset, GUI XAML extracted to the session scratchpad)
