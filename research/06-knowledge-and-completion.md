# 06: Game data, knowledge and the "finishing the game" problem

Research date: 2026-10-09. Game build on disk: Steam buildid 25605617, `Patch8_HotFix10.pak`. A local
save reports `"Game Version": "4.1.1.7631656"`.

Status markers: **VERIFIED LOCALLY** means I ran it against the installed game files on this machine (read-only).
**UNVERIFIED** means it comes from a secondary source or memory and was not confirmed.

---

## TL;DR

* **Data extraction works on Linux today, without Windows.** Two complementary paths, both verified locally:
  1. A **~60-line pure-Python reader** (`lz4` + `zstandard` from PyPI) unpacks BG3's LSPK v18 `.pak` files and
     parses `english.loca`, giving **232,878 handle→string pairs**. Stats `.txt`, `quest_prototypes.lsx` and other
     LSX files are plain text once unpacked.
  2. Binary **LSF** files (RootTemplates, the `Localization/*.lsf` key tables for levels, waypoints and subregions)
     need LSLib. **Norbyte's `LSLib.dll` v1.20.4 runs on Linux under the .NET 8 runtime** (in Docker,
     `mcr.microsoft.com/dotnet/runtime:8.0`) through a 10-line C# wrapper. The stock `Divine.dll` CLI **does not work** on
     Linux: it crashes in its own path validator (details in §1.2).
* **Translate handles offline, at build time,** into a SQLite lookup DB that ships with the harness. Runtime
  `Ext.Loca.GetTranslatedString` exists in bg3se, but bg3se is a Windows/Proton tool and we run the native Linux
  build. The offline DB is also deterministic and testable. Fairness is enforced in the *exporter*, which only
  translates handles of things already visible to the player. The agent never queries the DB directly.
* **bg3.wiki** runs MediaWiki 1.43 with TextExtracts, CirrusSearch and Cargo. Its content is dual-licensed
  **CC BY-SA 4.0 / CC BY-NC-SA 4.0**; content from before 2024-07-20 is NC-SA only. `robots.txt` disallows
  `/w/api.php` for crawlers. Provide a **rate-limited, cached `wiki_lookup` tool** for on-demand human-style lookups,
  not a mirror or crawl.
* **Finishing the game:** most blockers are not combat. They are **hard timers** (Nautiloid 15 turns, Iron Throne
  5–8 turns, Netherbrain), **game-over dialogue options** (~20 of them), **points of no return** (Rosymorn trail,
  Shadow-Cursed Lands, Shadowfell, leaving Act 2, Morphic Pool) and **long-rest-driven deadlines** (Gale's revival,
  Nere, Florrick). A spoiler-light checklist plus a save cadence covers them.
* **Setup:** Explorer difficulty with Karmic Dice on. Tav (not Durge) as a **Half-Orc Paladin** (Oath of Devotion) as
  the face character. Party: **Lae'zel, Shadowheart, Astarion**. Reactions set to non-prompting. Quicksave before
  every fight, big check and transition. Keep a rotating set of named hard saves.

---

## 1. Game-data tooling on Linux

### 1.1 What is on disk (VERIFIED LOCALLY)

`~/.local/share/Steam/steamapps/common/Baldurs Gate 3/Data/`. All archives start with magic `LSPK`, version **18**,
single part:

| Archive | Files | Interesting content |
|---|---|---|
| `Localization/English.pak` | 9 | `Localization/English/english.loca` (30.8 MB uncompressed, LZ4 flags `0x42`) plus gender-variant stubs |
| `Shared.pak` | 11,147 | `Public/Shared{,Dev}/Stats/Generated/Data/*.txt`, `Public/Shared/RootTemplates/_merged.lsf`, `Public/Shared/Localization/*.lsf` |
| `Gustav.pak` (13 GB) | 145,832 | `Public/Gustav{,Dev}/Stats/...`, `Public/Gustav{,Dev}/RootTemplates/_merged.lsf`, `Mods/GustavDev/Story/Journal/{quest,objective,questcategory}_prototypes.lsx`, `Mods/Gustav{,Dev}/Localization/*.lsf`, `Mods/*/Story/Dialogs*`, `Mods/*/Levels/*`, `Public/*/Flags` (~26k flag files) |
| `GustavX.pak` | 5,018 | Post-release content (`Public/GustavX/Stats`, `RootTemplates`) |
| `Patch8_HotFix10.pak` | 37 | Mostly `Story/RawFiles` and `story.div.osi` overrides |
| `Game.pak` | 29,250 | UI (Noesis XAML pages, icons) |

**Load order matters.** Later paks override earlier ones (`Shared` → `Gustav` → `GustavX` → `Patch*`).
Stats also exist under `Public/Honour*` for Honour-mode overrides, so ignore those unless playing Honour.

Save games (`PlayerProfiles/Public/Savegames/Story/<name>/<name>.lsv`) are **also LSPK v18** packages containing
`SaveInfo.json`, `meta.lsf`, `Globals.lsf`, `StorySave.bin` and `LevelCache/*.lsf`. More on this in §5 and §6.

### 1.2 LSLib / Divine (Norbyte)

* Repo: https://github.com/Norbyte/lslib. Latest release **v1.20.4 (2026-01-24)**, asset `ExportTool-v1.20.4.zip`.
  Releases need **.NET 8**. See https://github.com/Norbyte/lslib/releases.
* `Divine/Divine.csproj` and `LSLib/LSLib.csproj` target plain `net8.0` (cross-platform). Only `ConverterApp` (the
  GUI) is `net8.0-windows` + WinForms. **But** `LSLib.csproj` has a `ProjectReference` to
  `LSLibNative.vcxproj` (C++/CLI), so **building from source on Linux fails**. Use the prebuilt DLLs from the
  release zip instead (`Packed/Tools/{Divine,LSLib,LSLibNative,...}.dll`).
* Divine CLI actions, from `Divine/CLI/CommandLineArguments.cs`: `create-package; list-package; extract-single-file;
  extract-package; extract-packages; convert-model; convert-models; convert-resource; convert-resources;
  convert-loca; build-vt`. Main flags: `-g bg3 -a <action> -s <src> -d <dst> [-x <glob>] [-i/-o format]`.
* **Divine CLI on Linux is broken (VERIFIED LOCALLY with v1.20.4 on `mcr.microsoft.com/dotnet/runtime:8.0`):**

  ```
  Unhandled exception. System.InvalidOperationException: This operation is not supported for a relative URI.
     at System.Uri.get_IsFile()
     at Divine.CLI.CommandLineActions.TryToValidatePath(String path)
  ```

  `TryToValidatePath` calls `Uri.TryCreate(path, UriKind.RelativeOrAbsolute)`, then `uri.IsFile`. On Unix, `/abs/path`
  becomes a *relative* URI, so `IsFile` throws. The `//path` trick doesn't help either. A one-line upstream patch
  would fix it (UNVERIFIED that upstream has fixed it yet).
* **Workaround that works (VERIFIED LOCALLY): call LSLib.dll directly.** Conversions of `Levels.lsf`,
  `Waypointshrines{,2}.lsf`, `Act2_Subregions.lsf`, `Public/Shared/RootTemplates/_merged.lsf` (12.6 MB → 40.5 MB LSX,
  15,188 templates) and `english.loca` → XML all succeeded. LSF headers seen: `LSOF` versions 6 and 7.

  `tools/lsconv/lsconv.csproj`:
  ```xml
  <Project Sdk="Microsoft.NET.Sdk">
    <PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework>
      <Nullable>disable</Nullable><PlatformTarget>x64</PlatformTarget></PropertyGroup>
    <ItemGroup><Reference Include="LSLib"><HintPath>/divine/LSLib.dll</HintPath></Reference></ItemGroup>
  </Project>
  ```
  `tools/lsconv/Program.cs`:
  ```csharp
  // lsconv: tiny Linux wrapper over LSLib.dll. usage: lsconv res IN.lsf OUT.lsx | lsconv loca IN.loca OUT.xml
  using LSLib.LS;
  using LSLib.LS.Enums;
  var (mode, inp, outp) = (args[0], args[1], args[2]);
  if (mode == "loca") { LocaUtils.Save(LocaUtils.Load(inp), outp); }
  else {
    var res = ResourceUtils.LoadResource(inp, ResourceLoadParameters.FromGameVersion(Game.BaldursGate3));
    ResourceUtils.SaveResource(res, outp, ResourceConversionParameters.FromGameVersion(Game.BaldursGate3));
  }
  System.Console.WriteLine($"ok {outp}");
  ```
  Build and run:
  ```bash
  # unzip ExportTool-v1.20.4.zip -> ./lslib/Packed/Tools/*.dll
  docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -e DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    -v "$PWD/lslib/Packed/Tools:/divine:ro" -v "$PWD/tools/lsconv:/src" -w /src \
    mcr.microsoft.com/dotnet/sdk:8.0 dotnet build -c Release -o /src/bin
  docker run --rm -u "$(id -u):$(id -g)" -v "$PWD/tools/lsconv/bin:/app:ro" \
    -v "$PWD/extract:/in:ro" -v "$PWD/converted:/out" \
    mcr.microsoft.com/dotnet/runtime:8.0 dotnet /app/lsconv.dll res /in/Public/Shared/RootTemplates/_merged.lsf /out/Shared_RootTemplates.lsx
  ```
  `LSLibNative.dll` (Windows C++/CLI) is never loaded on these code paths. `granny2.dll` is only for GR2 models.
  A host `dotnet-runtime-8.0` install from Microsoft's Debian feed would avoid Docker, but I didn't test it.

### 1.3 Python options

* **Our own minimal reader (VERIFIED LOCALLY).** Recommended for paks and `.loca` because it has no .NET dependency.
  LSPK v18 layout: header `<4sIQI>` = magic, version, file-list offset, file-list size. At the offset: `u32 numFiles`,
  `u32 compressedSize`, then an LZ4-*block*-compressed table of `numFiles × 272` bytes. Each entry is
  `<256sIHBBII>` = name, offset low 32, offset high 16, archive part, flags, size on disk, uncompressed size.
  Compression is `flags & 0x0F`: 0 none, 1 zlib, 2 LZ4 block, 3 zstd.

  ```python
  # tools/pak.py  -- read-only LSPK v18 reader. usage: pak.py list|extract PAK [GLOB] [OUTDIR]
  import struct, sys, os, zlib, fnmatch
  import lz4.block, zstandard
  def entries(path):
      f = open(path, 'rb')
      magic, ver, off, sz = struct.unpack('<4sIQI', f.read(20))
      assert magic == b'LSPK' and ver == 18, (magic, ver)
      f.seek(off); n, csz = struct.unpack('<II', f.read(8))
      raw = lz4.block.decompress(f.read(csz), uncompressed_size=n * 272)
      for i in range(n):
          name, o1, o2, part, flags, disk, usz = struct.unpack_from('<256sIHBBII', raw, i * 272)
          yield name.split(b'\0', 1)[0].decode(), o1 | (o2 << 32), part, flags, disk, usz
  def read(path, e):
      name, off, part, flags, disk, usz = e
      assert part == 0, 'multi-part paks not handled'
      with open(path, 'rb') as f: f.seek(off); data = f.read(disk)
      m = flags & 0x0F
      if m == 0: return data
      if m == 1: return zlib.decompress(data)
      if m == 2: return lz4.block.decompress(data, uncompressed_size=usz)
      if m == 3: return zstandard.ZstdDecompressor().decompress(data, max_output_size=usz)
      raise ValueError(flags)
  if __name__ == '__main__':
      cmd, pak = sys.argv[1], sys.argv[2]; pat = sys.argv[3] if len(sys.argv) > 3 else '*'
      for e in entries(pak):
          if not fnmatch.fnmatch(e[0], pat): continue
          if cmd == 'list': print(e[0], e[4], e[5], hex(e[3]))
          else:
              out = os.path.join(sys.argv[4], e[0]); os.makedirs(os.path.dirname(out), exist_ok=True)
              open(out, 'wb').write(read(pak, e))
  ```

  `.loca` layout: header `<4sII>` = `LOCA`, entry count, texts offset. Then N × `<64sHI>` = handle, version, length
  (length includes the NUL). The texts follow in order.

  ```python
  # tools/loca.py -- english.loca -> TSV(handle, version, text)
  import struct, sys
  def parse(buf):
      magic, n, toff = struct.unpack_from('<4sII', buf, 0); assert magic == b'LOCA'
      pos, tpos = 12, toff
      for _ in range(n):
          key, ver, ln = struct.unpack_from('<64sHI', buf, pos); pos += 70
          yield key.split(b'\0', 1)[0].decode(), ver, buf[tpos:tpos + ln - 1].decode('utf-8', 'replace')
          tpos += ln
  ```
  Running it gives 232,878 rows in about 1 second, e.g. `h7fddad46g70f6g4c7dg8109gab93722b0495 → "Fire Bolt"`.
  Strings contain markup such as `<LSTag Tooltip="MovementSpeed">Movement Speed</LSTag>: [1]/[2]`. Strip or keep
  `LSTag` and fill `[n]` from the stat's `DescriptionParams` when rendering tooltips.
  Note that `pip`/`ensurepip` isn't installed system-wide here, but `uv` is (`~/.local/bin/uv`). `uv venv && uv pip
  install lz4 zstandard` worked.
* **bg3forge** (PyPI `bg3forge` 0.2.0, 2026-07-24, MIT, https://github.com/crazyace/bg3-forge). It claims a pure-Python,
  LSLib-independent implementation of PAK v15/16/18, LSF v1–7, LOCA, stats with `using` inheritance, RootTemplates and
  quests. It is very new (0 stars when checked), so **UNVERIFIED** and I didn't run it. It could replace the .NET step
  for LSF if it proves correct; validate it against LSLib's output first.
* **bg3rustpaklib** (Rust crate, https://docs.rs/bg3rustpaklib) covers PAK v15/16/18 and loca. It's an alternative
  reference implementation.

### 1.4 Where each kind of human-readable data lives (VERIFIED LOCALLY unless marked)

| Need | Source file(s) | Format | Join key → text |
|---|---|---|---|
| All UI text | `Localization/English/english.loca` | binary LOCA | handle `h…g…` → string |
| Spell, passive, status, item and weapon names and descriptions | `Public/{Shared,SharedDev,Gustav,GustavDev,GustavX}/Stats/Generated/Data/{Spell_*,Passive,Status_*,Armor,Weapon,Object,Character,Interrupt}.txt` | plain text: `new entry "X"` / `type` / `using` / `data "Key" "Value"` | `data "DisplayName" "h7fdd…;1"` (handle;version) → loca |
| Item and character template names | `Public/*/RootTemplates/_merged.lsf` | LSF → LSX | `GameObjects` node: `MapKey` (template GUID), `Name`, `Type` (item, character, scenery…), `DisplayName`/`Description` TranslatedString handle, `Stats` |
| Placed level objects (named NPCs, specific items) | `Mods/*/Globals/<Level>/{Characters,Items}/_merged.lsf`, `Mods/*/Levels/<Level>/{Characters,Items}/_merged.lsf` | LSF | Instance `MapKey` GUID → `DisplayName` override or `TemplateName` → RootTemplate |
| Level (region) names | `Mods/Gustav{,Dev}/Localization/Levels.lsf` | LSF → `TranslatedStringKey { Content(handle), UUID }` | e.g. `WLD_Main_A`→"Wilderness", `TUT_Avernus_C`→"A Nautiloid in Hell", `CRE_Main_A`→"Githyanki Crèche", `SCL_Main_A`→"Shadow-Cursed Lands", `INT_Main_A`→"Wyrm's Lookout", `BGO_Main_A`→"Wyrm's Crossing", `CTY_Main_A`→"Baldur's Gate", `IRN_Main_A`→"Iron Throne", `END_Main`→"High Hall", `EPI_Main_A`→"Reunion Camp" |
| Sub-region names (what the minimap header shows) | `Mods/GustavDev/Localization/Act{1b,2,3}_Subregions.lsf`, `Mods/Gustav/Localization/Act1_Subregions.lsf` | LSF | e.g. `MOO_MainFloorInterior_SUB`→"Moonrise Towers, Main Floor" (40 Act 2 entries) |
| Waypoint names | `Mods/Gustav/Localization/Waypointshrines.lsf` (Act 1, 20 entries), `Mods/GustavDev/Localization/Waypointshrines2.lsf` (Acts 2/3, 24) | LSF | `WAYP_HAV_Outside`→"Last Light Inn", `WAYP_SHA_Temple`→"Gauntlet of Shar", `WAYP_LOW_StartingArea`→"Basilisk Gate", Act 1: "Riverside Teahouse", "Emerald Grove Environs", "Underdark - Grymforge"… |
| Quests and journal | `Mods/GustavDev/Story/Journal/quest_prototypes.lsx` (8.2 MB, **plain XML**), `objective_prototypes.lsx`, `questcategory_prototypes.lsx` | LSX | `Quest{QuestID, QuestGuid, CategoryID, QuestTitle(handle), QuestVisiblity}` → `QuestStep{ID, Description(handle), Objective, DialogFlagGUID, DevComment, ExperienceReward}`. **167 quests, 3,543 steps.** Categories: MainQuest 31, BaldursGate 29, PersonalStory 26, Companions 15, Grove 14, Crashside 14, Underdark 13, Shadowcurse 13, Creche 3, Tutorial 1, plus `*_HiddenBoosters`, which are **hidden and must be excluded** |
| Journal map markers | `Mods/GustavDev/Story/Journal/Markers/<guid>.lsx` (542 files) | LSX | Marker GUID → position and text |
| Misc UI strings | `Public/Shared/Localization/{OsirisStrings,Resources,ReadyChecks_Descriptions}.lsf`, `*_QuestTimers.lsf`, `*_Books.lsf` | LSF | handle → loca. **QuestTimers** contain the on-screen countdown labels |
| Dialog text | `Mods/*/Story/Dialogs/**.lsj` (JSON) and `DialogsBinary/*.lsf` | LSJ/LSF | Node text handles. Only the *current* dialogue's visible lines may be exported (see §1.6) |

Example stats entry (VERIFIED LOCALLY, `Public/Shared/Stats/Generated/Data/Spell_Projectile.txt`):
```
new entry "Projectile_FireBolt"
type "SpellData"
data "SpellType" "Projectile"
data "Level" "0"
data "SpellSchool" "Evocation"
data "SpellRoll" "Attack(AttackType.RangedSpellAttack)"
data "SpellSuccess" "DealDamage(LevelMapValue(D10Cantrip),Fire,Magical)"
data "Icon" "Spell_Evocation_FireBolt"
data "DisplayName" "h7fddad46g70f6g4c7dg8109gab93722b0495;1"
data "Description" "h111ae255g55f1g4a55gb958g517af226971b;4"
```
Stats inheritance (`using "Parent"`) must be resolved: a child without `DisplayName` inherits the parent's.

### 1.5 Offline DB vs runtime translation: decision

| | Offline lookup DB (build-time) | Runtime (`Ext.Loca.GetTranslatedString`) |
|---|---|---|
| Availability | Works now (§1.2–1.4) | bg3se documents `Ext.Loca.GetTranslatedString(handle, [fallbackText]): string` and `Ext.Loca.UpdateTranslatedString` (https://github.com/Norbyte/bg3se/blob/main/Docs/API.md, latest release v32, 2026-06-21). **bg3se is a Windows DLL**, so it needs the Proton build. On the native Linux binary we'd have to find and call the engine's translated-string repository ourselves. `nm -C` shows the `ls::TranslatedString` types, but no exported lookup function was found (UNVERIFIED that one is easy to reach) |
| Patch drift | Rebuild when the buildid changes (cheap: ~1 min loca, ~5 min full) | Always current |
| Mods and dynamic strings | Misses runtime-generated or modded handles | Catches everything |
| Testability | Deterministic and diffable | Needs the game running |

**Decision: offline DB as the primary path, runtime as an optional fallback.** The state exporter (whichever mod or
hook design the other research reports pick) should emit raw handles, GUIDs and template IDs. The harness-side Python translates
them through `bg3_lookup.sqlite`. If the runtime layer later offers a cheap lookup, use it only for handles missing from
the DB, and log every miss.

### 1.6 Fairness rules for translation

Translation itself never leaks anything. The leak risk is in *what gets translated and exposed*:

* Only translate entities the exporter has already judged visible: in line of sight, not hidden or invisible, and
  already revealed or discovered.
* **Display name rule.** The game shows "Gnoll" before you learn a name, or "Hooded Figure" for a disguised NPC.
  Export the name the UI currently shows: the entity's current `DisplayName` (which the engine swaps when revealed),
  not the underlying template's. Never output a RootTemplate `Name` like `S_GOB_DrowCommander` or
  `CRE_GithInquisitor_Hidden`. Internal names leak.
* **Quests:** export only quests the journal shows (accepted, `QuestVisiblity=true`, category not `*_HiddenBoosters`)
  and only *unlocked* steps. Never export `DevComment`, `DialogFlagGUID`, `Objective`, rewards, or future steps.
  The DB has all 3,543 step texts, so the agent must not have a free-form "query DB" tool. The DB is internal to the
  exporter.
* **Items:** unidentified items show a generic name in the UI. Use the generic name until identified.
  Stats fields like `ValueOverride` or hidden `Boosts` are fine **only** where the tooltip shows them.
  Use `Description`/`DescriptionParams` (tooltip text), not raw functors.
* **Dialogue:** export the current speaker line and the selectable options as displayed, including `[PERSUASION]` /
  `[DC 15]` tags *only if the UI shows them*. The custom-difficulty option "Preview dialogue check difficulty" changes
  this, so match the settings. Never export dialog-tree conditions, flags or unseen branches.
* **Map:** only discovered waypoints (`eoc::party::WaypointsComponent::m_UnlockedWaypoints` exists in the native
  binary's symbols, VERIFIED LOCALLY with `nm -C`) and fog-of-war-revealed areas.

---

## 2. bg3.wiki as an external knowledge source

**Facts (VERIFIED LOCALLY with HTTP requests on 2026-10-09):**
* MediaWiki **1.43.11**. 13,158 articles, 56,387 pages. Extensions include **TextExtracts**, **CirrusSearch**,
  **Cargo** (structured tables), Scribunto and PortableInfobox.
* Endpoints: `https://bg3.wiki/w/api.php` (Action API) and `https://bg3.wiki/w/rest.php/v1/page/<Title>`
  (REST API, which returns the license in the payload: `"CC BY-NC-SA 4.0 or CC BY-SA 4.0"`).
* Useful calls:
  ```
  # search
  /w/api.php?action=query&list=search&srsearch=Gauntlet%20of%20Shar&srlimit=5&format=json
  # plain-text article (best for an LLM)
  /w/api.php?action=query&prop=extracts&explaintext=1&exsectionformat=plain&titles=Iron_Throne&format=json&formatversion=2
  # raw wikitext (keeps infobox fields such as uid = Projectile_FireBolt; good for joining to our stats DB)
  /w/api.php?action=parse&page=Fire_Bolt&prop=wikitext&format=json&formatversion=2
  # section list, then fetch one section to save tokens
  /w/api.php?action=parse&page=Gauntlet_of_Shar&prop=sections&format=json
  /w/api.php?action=parse&page=Gauntlet_of_Shar&section=5&prop=wikitext&format=json
  ```
  Spell and item infoboxes carry `uid = <stats entry name>`, so wiki pages join cleanly to our stats DB.
* **License** (https://bg3.wiki/wiki/bg3wiki:Copyrights): dual **CC BY-SA 4.0** and **CC BY-NC-SA 4.0**.
  Content submitted **before 2024-07-20 is CC BY-NC-SA 4.0 only** unless its author agreed otherwise. Larian and WotC
  material is not licensed for commercial use. Attribution means a backlink to the page.
  → Live lookups for an agent's own use are fine. **Do not redistribute a bundled copy** of wiki text with the harness
  unless the project is non-commercial and carries the NC-SA attribution. Write our own checklist instead (§7).
* **robots.txt disallows `/w/api.php` and `/w/rest.php`** for user-agents `*`. An agent's occasional,
  user-initiated lookup is not crawling, but be a good citizen: a descriptive `User-Agent` with contact info, at most
  ~1 request/second, an aggressive local cache keyed by title + revision (cache for days), and no bulk prefetch.
  The wiki has 7 admins and 62 active users, so it's a small community site. If we ever want a bulk offline copy, ask
  the admins for a dump.

**Wiki tool vs built-in model knowledge.** The model already knows BG3 well, but its knowledge has gaps:
patch-specific numbers (Patch 8 changed subclasses and balance), exact DCs, coordinates and puzzle solutions. It
also hallucinates, for example by conflating the Defiled Temple tile puzzle with the Selûnite Outpost. A lookup tool is
cheap and helps the agent recover when stuck, so **provide it**:

```
wiki_search(query: str, limit=5) -> [{title, snippet}]
wiki_page(title: str, section: str | None = None, max_chars=6000) -> {title, revid, url, text}
```
Implementation: TextExtracts plaintext with `exsectionformat=plain`, trimmed, plus a section selector. Cache to
`~/.cache/bg3-ai/wiki/`. Put the page URL in the tool output for attribution. Policy in the agent prompt:
*"Use the wiki when stuck for more than N attempts, before irreversible choices, and to check a puzzle solution.
Don't read ahead for fun."* Fairness is fine: the brief explicitly allows wiki-level knowledge.

---

## 3. The "finishing BG3" problem

### 3.1 Critical path (spoiler-light)

Sources: https://bg3.wiki/wiki/Act_One, /Act_Two, /Act_Three, /Confront_the_Elder_Brain,
/Time-sensitive_activities.

**Prologue: Nautiloid** (`TUT_Avernus_C`). This is linear. **Reach the helm transponder within 15 turns** once that
counter starts, or the game ends. Don't leave the room mid-fight with Zhalk, and don't go to the healing pod after
winning that fight. Both cause a game over.

**Act 1** (`WLD_Main_A` Wilderness, Underdark, Grymforge, `CRE_Main_A` Crèche).
1. Ravaged Beach → recruit Shadowheart (before going to camp or the Grove), Astarion, Gale and Lae'zel.
2. **Emerald (Druid) Grove** → "Save the Refugees" / the Rite of Thorns. Resolve it before leaving (Investigate Kagha
   / Save the First Druid). Leaving for the Mountain Pass or the Shadow-Cursed Lands earlier seals the Grove.
3. **Blighted Village** → Goblin Camp (Shattered Sanctum). "Defeat the Goblins" (kill the three leaders: Priestess
   Gut, Dror Ragzlin, Minthara) **or** the evil route of raiding the Grove. Free Halsin.
4. Celebration long rest at camp → talk to Halsin (or Minthara) → main quest "Reach Moonrise Towers".
5. Two routes to Act 2:
   * **Mountain Pass → Rosymorn Monastery → Crèche Y'llek** (Lae'zel's questline, Blood of Lathander). Entering the
     Rosymorn trail is a time-sensitive choke point.
   * **Underdark → Grymforge** (Nere, the gnomes, the Adamantine Forge) → the "Grymforge to Shadow-Cursed Lands"
     passage (UNVERIFIED exact name).
   Either way, **entering the Shadow-Cursed Lands starts Act 2** and auto-resolves many Act 1 quests. Most of the
   Wilderness stays reachable until the Shadowfell.

**Act 2** (`SCL_Main_A` Shadow-Cursed Lands).
1. Ruined Battlefield → **Last Light Inn** (Isobel's protection, Harpers, Jaheira). You need protection from the
   Shadow Curse to move around: Last Light, a Moonlantern, the pixie blessing (Act 1 "Free the Trapped Spirit"), or
   another light source. Travelling unprotected stacks curse damage.
2. Reithwin Town, Grand Mausoleum ("Find Ketheric Thorm's Relic"). **Visit Moonrise Towers early** (infiltrate as a
   True Soul) before the point of no return.
3. **Gauntlet of Shar** ("Find the Nightsong"): Umbral Gem door, three trials, Silent Library, Yurgir, Balthazar.
   **Entering the Shadowfell is Act 2's point of no return:** Wilderness waypoints become inaccessible and unfinished
   Act 1 quests end.
4. Nightsong choice (affects Shadowheart and the Moonrise assault) → assault on Moonrise Towers → defeat Ketheric on
   the roof → **Mind Flayer Colony** → second Ketheric fight → Astral Prism events → Act 3 at **Wyrm's Lookout**
   camp. Leaving Act 2 is a choke point.

**Act 3** (`BGO_Main_A` Rivington/Wyrm's Crossing, `CTY_Main_A` Lower City, `IRN_Main_A` Iron Throne).
1. Rivington → Wyrm's Crossing → **Wyrm's Rock Fortress** (Gortash's coronation; attend it). Lower City opens
   past the North Span.
2. Main quests: **Get Gortash's Netherstone**, **Get Orin's Netherstone** (Temple of Bhaal, under the Lower City),
   Gather Your Allies. Recommended order: **Iron Throne before Steel Watch Foundry, Foundry before Gortash**.
   * The Iron Throne becomes unreachable if the Steel Watch Foundry is destroyed first.
   * Destroying the Foundry makes the Gortash fight much easier.
   * Talk to Mizora and to Cirrus/Feena at camp **before** the Iron Throne, or Duke Ravengard is found dead.
3. Act 3's "stems" are the three Chosen: Ketheric (already dead, Act 2), Gortash and Orin. With both remaining
   Netherstones → **Morphic Pool = final point of no return.** No camp, rest, stash, party swap or respec after it.
   It needs DC 20 Constitution saves, Netherstone checks, the Emperor/Orpheus choice (an **Orphic Hammer or Raphael's
   deal is required** to free Orpheus), the Upper City/High Hall run, and the Netherbrain fight on a turn counter.

### 3.2 Hard parts for an AI agent

**Timers and turn limits (each one is a game-over or death risk):**

| Where | Limit | Failure |
|---|---|---|
| Nautiloid helm | 15 turns | Game over |
| Iron Throne (Act 3) | **8 turns Explorer / 6 Balanced / 5 Tactician-Honour** after Omeluum's dialogue ends | Anyone not in the sluice chamber or submersible dies. Game over only if nobody makes it back. Leaving immediately through the controls kills everyone inside. See https://bg3.wiki/wiki/Iron_Throne |
| Blood of Lathander trap (Rosymorn) | 4 rounds (without the Dawnmaster's Crest) | Game over if the whole party is inside |
| Lift the Shadow Curse (Halsin portal) | Protect for 4 combat turns | Failure |
| Netherbrain top and Mind of the Netherbrain | 4-turn counter, then a new counter | Game over |
| Long-rest deadlines | Gale's revival (**3 long rests**, then a game over for an unstabilised orb), Nere and the Grymforge gnomes (2 rests/departures), Thulla (3), Florrick (5), Cora (1), Rugan/Olly (1), Mirkon (1) | Death, quest failure, or a game over for Gale |

**Puzzles:**
* **Defiled Temple Selûne tiles** (Act 1, under the Goblin Camp). Moves are unlimited. One 7-move solution is
  N, N, E, W, S, S, E. A hidden lever bypasses it (DC 15 Perception + DC 15 Sleight of Hand). Optional: it's one
  of several Underdark entrances. https://bg3.wiki/wiki/Defiled_Temple
* **Necromancy of Thay** (Apothecary's cellar under the Blighted Village, needs the Dark Amethyst from the Whispering
  Depths). Optional. Reading it means Wisdom saves at DC 10/15/20. Failures apply Whispers of Madness (cleared by a long
  rest) or Baleful Knowledge (needs Remove Curse). Agent advice: **skip, or destroy it.**
* **Gauntlet of Shar** (mandatory for the Nightsong), the hardest puzzle block. https://bg3.wiki/wiki/Gauntlet_of_Shar
  * Umbral Gem door: extinguish **all** light, including torches, Light, Dancing Lights and Moonlanterns, then walk the
    glyph gaps. Skip: Misty Step or Dimension Door past the door and open it from inside.
  * Soft-Step Trial (stealth maze, darkvision shadows; one-shot kills stop respawns).
  * Self-Same Trial (strip gear first so the mirror copies spawn naked; never attack another character's copy, or you
    get "Cheater's Folly").
  * **Faith-Leap Trial** (invisible path; the third failure is fatal, one misstep in Honour). The path is drawn on the
    floor mosaic. Do it with one character and leave the others back.
  * Gem lift drops (send small groups). **Don't destroy "Teachings of Loss: The Nightsinger"**: it locks out the final
    puzzle and Shadowheart.
  * Yurgir's contract has a hidden clause (DC 14 Insight).
* **Grymforge** (if taking the Underdark route): the Runepowder barrel standoff (Philomeen) is a **game over if the whole
  party is within 18 m and the check fails**. Keep someone outside. The Adamantine Forge is optional.
* **Wyrm's Rock / Gortash** has a trap-heavy throne room (Fancy Footwork achievement). Steel Watchers are deadly
  unless the Foundry is down. **Iron Throne** is the timed rescue above. Use Haste, Misty Step, Dimension Door and
  ranged attacks or Magic Missile to open cells remotely.

**Mandatory or high-stakes skill checks** (Charisma face matters):
* The Ravaged Beach dying mind flayer's Wisdom save (a fail or the "lean in" option is a game over).
* Lae'zel's dagger scene at the first camp (fail plus "surrender" while alone with her is a game over).
* Vlaakith with Shadowheart present (don't taunt).
* The Netherbrain Netherstone checks at the Morphic Pool (a fail is a game over).
* Endgame DC 30 Persuasion checks (Lae'zel, Voss) if siding with the Emperor.

**Illithid and choice traps:**
* Activating Gale's orb in Act 2+ (outside two specific moments) is a game over.
* Killing Mizora in her pod is a game over if Wyll is the Origin.
* Refusing Raphael in the Astral Plane with no Orphic Hammer is a game over.
* The Emperor dying in "Help Your Protector" is a game over. So is attacking him in the Astral Plane before the
  Prince's fate is decided.
* The House of Hope "give up body and mind" option is a game over.
* Returning to Act 1 after Nightsong + Relic is a game over.
* Scythe of Myrkul misuse can softlock (Ketheric's body doesn't appear).
* Full list: https://bg3.wiki/wiki/Game_Over_screens. Rule for the agent: **never accept "submit / surrender / lean
  in / give up" options from a mind flayer, devil, hag or god. Never refuse Raphael unless you hold the Orphic Hammer.
  Never attack the Emperor or the Dream Guardian outside dialogue.**

**Soft-locks / known bugs (Patch 8, released 2025-04-15, with hotfixes since):**
* I found no current consolidated soft-lock list. Search results only covered old 2023 hotfixes and Patch 8
  UI/controller issues (radial-menu slowdowns, a kaleidoscope rendering bug fixed in a later hotfix).
* Known design-level soft-locks: sacrificing Gale before his orb is stabilised (forced 3-rest game over); the Scythe
  of Myrkul case above; and saving an Honour or "single save" game right before a wipe.
* Mitigation: **rotating hard saves**, so you can always roll back several hours (§6).
* UNVERIFIED: the Patch 8 hotfix train may have added fresh quest bugs. Search the wiki for the quest name plus "bug"
  when a quest step doesn't advance.

### 3.3 Difficulty choice

From https://bg3.wiki/wiki/Difficulty and https://bg3.wiki/wiki/Long_rest:

| | Explorer | Balanced | Tactician | Honour |
|---|---|---|---|---|
| Enemy HP | ≈ -30% vs Balanced | baseline | +20–30% (bosses more) | Tactician + legendary actions |
| Player bonus | proficiency +2 (custom-table reading, UNVERIFIED) | 0 | enemies +2 attack/DC | as Tactician |
| Long rest cost | 40 supplies | 40 | 80 | 80 |
| Iron Throne timer | 8 | 6 | 5 | 5 |
| Saves | free | free | free | **single save** |
| Restoration pods (endgame) | yes | yes | no? | no |

**Recommendation: start on Explorer.** The goal is *finishing*, and Explorer widens every timer and margin. Difficulty
can be changed later in Options for non-Honour games (UNVERIFIED for the current patch, but long-standing behaviour).
Leave Karmic Dice on. Raise difficulty to Balanced once the agent clears Act 1 without wipes. Never use Honour or Custom
"Honour single save": they turn a party wipe into run termination.
Custom mode helpers worth considering: "No death saving throws" (downed allies stay stable until helped), "Short rest
full heal" and Camp cost ×0.5. These make the run easier, but they don't change what the agent needs to learn.

### 3.4 Camp, long rest and supplies cadence

* Long rest costs 40 supplies (Explorer/Balanced). Without them you get a **Partial Rest** (half HP and slots, no
  short-rest refills). You get **2 short rests per long rest.** Food is plentiful: auto-send food to camp
  (inventory → "Send to Camp") and keep >80 supplies in camp.
* Long rests also *advance the story*. Many companion scenes, the Act 1 celebration, Halsin, Voss and Act 3 events
  trigger on rest, and some deadlines tick per rest (§3.2). Policy:
  1. **Long rest whenever a fight left you with < 50% of party spell slots or HP after short rests, OR when the journal
     says to rest (e.g. "celebrate", "rest and reflect"), OR before entering any choke point.**
  2. **Don't long rest while a known timed quest is open.** Before resting, list open journal entries that mention
     someone dying, poisoned, captured, "soon" or "hurry", and resolve them first. The harness checklist can carry a
     small "rest-sensitive" list: Gale-dead, Nere, Thulla, Waukeen's Rest fire, Florrick, Cora.
  3. Go to camp, rest, and check the camp for new conversations (companion "!" markers) before leaving.

---

## 4. Character creation and party strategy

**Tav, not Durge.** The Dark Urge adds forced events (the Alfira kill, forced rests, urges with game-changing dialogue
options) and more irreversible choices. That means more ways for an agent to go wrong for no gain in finishing.

**Main character: Half-Orc Paladin, Oath of Devotion** (or Oath of the Ancients).
* Why a Paladin: heavy armour and a good AC; Lay on Hands as an emergency heal; Divine Smite for burst damage; and
  **Charisma as the casting stat makes the PC the party face.** The PC speaks in almost every dialogue, and the
  game-critical rolls (Persuasion DC 30 in the endgame, Intimidation/Persuasion to skip fights) are Charisma rolls.
* Why a Half-Orc: Relentless Endurance (drop to 1 HP instead of 0 once per long rest) and Savage Attacks. Githyanki
  (free Misty Step, Astral Knowledge) is a good alternative and avoids friction with Lae'zel.
* Abilities: STR 16, DEX 10, CON 14, INT 8, WIS 10, CHA 16 (roughly; boost STR and CHA). Background: Noble or Soldier
  (Persuasion/Intimidation/Athletics). Fighting style: Defence or Great Weapon Fighting with a two-handed weapon.
  Feat at level 4: +2 STR; Great Weapon Master later. Stay single-class: no multiclass micro-management.
* **Configure reactions** (Character sheet → Reactions) so nothing prompts: Smite on crit only or never;
  Attack of Opportunity on. Prompts are UI interrupts an agent can miss. Use Smites as explicit actions instead.

**Companions: Lae'zel (Fighter tank/damage), Shadowheart (Cleric healer: Healing Word, Spirit Guardians, Bless),
Astarion (Rogue: lockpicks, trap disarm, Sleight of Hand, ranged damage).** That covers every utility role: heals,
locks, traps, frontline and Charisma face. Gale is optional: he's a strong caster but brings the **orb**: feeding magic
items, a 3-rest revival trap if he dies early, and an orb-detonation game-over option. Keep him alive in camp and
satisfy his feeding quest, or include him for Gauntlet/Iron Throne teleports (Dimension Door) if the agent handles
casters well. Karlach is a strong, simple melee alternative to Lae'zel.
* **Use subclass defaults and the game's "auto-select" level-up suggestions.** The level-up UI pre-selects sensible
  spells. Prefer auto-cast-friendly kits: Shadowheart → Life Domain; Astarion → Thief (extra bonus action) with a bow;
  Lae'zel → Battle Master or Champion (Champion is simplest).
* **Approval:** to keep the core party, avoid dialogue options that are cruel to tieflings or refugees (Lae'zel and
  Astarion disapprove of different things, but neither leaves easily). Don't take Shadowheart's artefact, or Lae'zel's
  side against the Crèche, in ways that force a party split. A neutral-good, pragmatic play style keeps everyone.
* **Minimise micro-management:** auto-equip upgrades by comparing displayed AC or damage only; give everyone 3+ healing
  potions; auto-send junk to camp; avoid inventory-heavy sidequests; let one character (the Paladin) hold gold and
  quest items.

---

## 5. What a progress tracker can fairly use

| Signal | Visible to player? | Engine data | UI-only | Notes |
|---|---|---|---|---|
| Journal quests: titles, accepted/closed, unlocked step text | Yes (Journal → Quests) | Quest *definitions* in `quest_prototypes.lsx`. Quest *state* in Osiris (queries `QuestIsAccepted`, `QuestIsClosed`, `QuestIsInProgress`, events `QuestAccepted`, `QuestClosed`, `QuestUpdateUnlocked*`; names found in the native binary's strings) and synced to the client via `esv::JournalProtocol` / `ecl::JournalProtocol`. Collapse state: `eoc::quest::ModifiedJournalEntrySingletonComponent` | Journal panel (`JournalQuests`, `QuestUnlockedSteps` view-model strings) | Best signal. Export title plus unlocked step descriptions only |
| Main-quest objective (top-level "MainQuest" category) | Yes | Same; category `MainQuest` | Quest tracker widget | Drives the "next objective" line in the memory notes |
| Current level/region and subregion | Yes (minimap header, save name) | Level name (`WLD_Main_A`…), subregion IDs (`*_SUB`). The save's `SaveInfo.json` has `Current Level` and per-character `Subregion` (VERIFIED LOCALLY) | Minimap label | Translate via `Levels.lsf` / `*_Subregions.lsf` |
| Waypoints discovered | Yes (map) | `eoc::party::WaypointsComponent::m_UnlockedWaypoints` (symbol present, VERIFIED LOCALLY); names via `Waypointshrines*.lsf` | Map screen | Also tells you which acts are reachable |
| Map markers (quest markers, custom pins) | Yes | `Story/Journal/Markers/*.lsx` definitions; active state is runtime (UNVERIFIED API) | Map | Export only active markers |
| Party level, classes, XP | Yes | ECS components; also `SaveInfo.json` (`Level`, `Classes`, XP) | Character sheet | |
| Act / chapter | Implied by region | Level → act map in the DB | – | `TUT`=Prologue, `WLD/CRE`=Act 1, `SCL`=Act 2, `BGO/CTY/IRN`=Act 3, `END`=Finale, `EPI`=Epilogue |
| Quest timer countdowns | Yes (on-screen) | `*_QuestTimers.lsf` labels + runtime counter | Timer widget | Critical for Iron Throne and Netherbrain |
| Tutorials, books read, inspirations | Yes | – | Journal tabs | Low priority |
| Story flags, `DB_*` Osiris databases, dialog flags | **No** | Osiris | – | **Never export** |

**Tracker design:** a `progress.json` written by the harness after each tool call that changes state. Fields: act,
level, subregion, main-quest step text, list of open quests with their latest step, discovered waypoints, party
summary, and a "rest-sensitive" flag list. Mirror it into the Markdown memory as the "where am I / what next" header.
Between sessions the agent reads it first.

---

## 6. Save-scumming policy

Quicksave and quickload are normal player behaviour on non-Honour difficulties. Default keys are **F5 quicksave and F8
quickload** (UNVERIFIED for the Linux build; read the actual binding from the input config). The game also autosaves on
area transitions, long rests and some story beats. Saves live in
`PlayerProfiles/Public/Savegames/Story/<Name>-<id>__<label>/`. Each is an LSPK v18 `.lsv` with `SaveInfo.json`.
The harness can read the save's level, party and difficulty **without loading it**, which is useful for choosing a
recovery point. This data appears on the load screen too, so it's fair.

**Cadence:**
1. **Quicksave** before every combat you initiate, every dialogue with a visible skill check, every lockpick or trap
   attempt that matters, every jump or chasm, and every puzzle.
2. **Named hard save** (via the menu, label `AI_<act>_<short-desc>_<n>`) at each region transition, after each main-quest
   step, and **always before**: the Rosymorn trail, Shadow-Cursed Lands, Shadowfell, leaving Act 2, Iron Throne, Wyrm's
   Rock audience, Morphic Pool. Keep the last ~15 hard saves; delete older AI saves only through the game UI (never
   touch the profile directory from the harness, per the brief's no-modify rule).
3. **Never save while a character is dying, in a known-lost fight, or after an irreversible bad choice.** If the agent
   realises it made a mistake, load first and decide second.
4. **Re-roll policy:** reloading to retry a failed *dialogue* check is allowed (a normal player does it), capped at
   ~3 attempts. After that, accept the result or try another route so the run makes progress. Combat retries:
   after 2 wipes on the same fight, the agent must change something (rest first, change positioning, use consumables,
   lower difficulty, or skip the fight).

**Auto-recovery from a party wipe or game over:** the Game Over screen offers Load / Main Menu. The harness should
detect it (screenshot or UI state), load the most recent quicksave, check that it isn't a doomed state (e.g. mid-combat
with everyone downed), and otherwise fall back to the previous hard save. Then it writes a
`## Wipe log` entry to memory with the cause and the planned change. For permanent-looking traps (e.g. three rests after
Gale's death), the memory file needs a "deadline" section the agent checks before any long rest.

---

## 7. Recommendation

### 7.1 Knowledge assets to ship with the harness

1. **`bg3_lookup.sqlite`**, built from game data by a script, versioned by Steam buildid. Harness-internal only; the
   agent never queries it directly.
   * `loca(handle TEXT PK, version INT, text TEXT)`: 232,878 rows from `english.loca`.
   * `stats(name PK, type, parent, display_name, description, description_params, icon, spell_level, school, …)`,
     resolved through `using` inheritance and pak override order, with names already translated.
   * `templates(guid PK, name, type, display_name, description, stats, icon)` from every `RootTemplates/_merged.lsf`.
   * `level_objects(guid PK, level, template_guid, display_name)` from `Mods/*/{Globals,Levels}/*/{Characters,Items}/_merged.lsf`
     (only needed if the exporter sees instance GUIDs without a runtime display name).
   * `levels(id PK, name)`, `subregions(id PK, name)`, `waypoints(id PK, name)` from the `Localization/*.lsf` key
     tables.
   * `quests(quest_id PK, guid, category, title, visible)` and `quest_steps(quest_id, step_id, guid, description)`,
     excluding `*_HiddenBoosters` and dropping `DevComment` and `DialogFlagGUID`.
2. **`walkthrough.md`**: a curated, spoiler-light checklist written by us (not copied from the wiki, for licensing).
   One section per act with:
   * **Choke points** and the "finish before crossing" list (e.g. resolve the Rite of Thorns, visit Moonrise before the
     Shadowfell, Iron Throne before Foundry before Gortash).
   * **Timers** (Nautiloid 15, Iron Throne 8/6/5, Netherbrain) and **rest-sensitive deadlines**.
   * **Never-pick list** of game-over options (§3.2), worded generically.
   * **Must-keep items**: the Orphic Hammer path, the Nightsinger book, Netherstones.
   * One-line hints only for the mandatory puzzles (Umbral Gem darkness, Self-Same strip gear, Faith-Leap floor mosaic).
   Target length is about 2–3 pages, so the agent can read it at every session start.
3. **`wiki_lookup` tool** (`wiki_search`, `wiki_page` with a section selector) over bg3.wiki's Action API with
   TextExtracts. Use a descriptive User-Agent, ≤1 req/s, an on-disk cache, a page URL in every result, and no bulk
   crawling.
4. **`progress.json` + memory header** generated from journal, region and waypoint state (§5).
5. **`setup.md`**: the recommended difficulty (Explorer, Karmic Dice on), the build (Half-Orc Devotion Paladin, Lae'zel,
   Shadowheart, Astarion), the reaction settings and the save cadence (§3.3, §4, §6).

Why this mix: the DB makes state readable and stays fair because the exporter does the gating. The checklist prevents
the few irreversible failures that general knowledge and the wiki won't flag ahead of time. The wiki tool covers
everything else on demand without bundling licensed content.

### 7.2 Extraction commands (verified pieces, assembled)

```bash
set -euo pipefail
G="$HOME/.local/share/Steam/steamapps/common/Baldurs Gate 3/Data"
W=build/gamedata; mkdir -p "$W"/{raw,lsx}
uv venv .venv && uv pip install --python .venv/bin/python lz4 zstandard

# 1) loca -> TSV (pure Python)
.venv/bin/python -I tools/pak.py extract "$G/Localization/English.pak" '*/english.loca' "$W/raw"
.venv/bin/python -I tools/loca.py "$W/raw/Localization/English/english.loca" > "$W/english.tsv"

# 2) Stats, journal, localization key tables, root templates (pure Python unpack)
for P in Shared Gustav GustavX Patch8_HotFix10; do
  .venv/bin/python -I tools/pak.py extract "$G/$P.pak" 'Public/*/Stats/Generated/Data/*.txt' "$W/raw"
  .venv/bin/python -I tools/pak.py extract "$G/$P.pak" '*/Story/Journal/*_prototypes.lsx'    "$W/raw"
  .venv/bin/python -I tools/pak.py extract "$G/$P.pak" '*/Localization/*.lsf'                 "$W/raw"
  .venv/bin/python -I tools/pak.py extract "$G/$P.pak" 'Public/*/RootTemplates/_merged.lsf'   "$W/raw"
done
# (the pak glob uses fnmatch, where * also matches '/')

# 3) LSF -> LSX through LSLib.dll on .NET 8 (Divine CLI is broken on Linux, use the lsconv wrapper)
find "$W/raw" -name '*.lsf' -printf '%P\n' | while read -r f; do
  mkdir -p "$W/lsx/$(dirname "$f")"
  docker run --rm -u "$(id -u):$(id -g)" -v "$PWD/tools/lsconv/bin:/app:ro" \
    -v "$PWD/$W/raw:/in:ro" -v "$PWD/$W/lsx:/out" mcr.microsoft.com/dotnet/runtime:8.0 \
    dotnet /app/lsconv.dll res "/in/$f" "/out/${f%.lsf}.lsx"
done
# (batch this into a single container run with a directory mode in lsconv for speed)

# 4) Build SQLite: parse english.tsv, stats txt (resolve `using`), LSX TranslatedStringKey tables,
#    RootTemplates GameObjects, quest_prototypes.lsx -> bg3_lookup.sqlite  (tools/build_db.py, to write)
# 5) Record the Steam buildid in the DB; rebuild when it changes:
grep -m1 buildid "$HOME/.local/share/Steam/steamapps/appmanifest_1086940.acf"
```

Verified on this machine: pak listing and extraction, loca parsing, LSF→LSX for `Levels`, `Waypointshrines{,2}`,
`Act2_Subregions` and the Shared RootTemplates, loca→XML through LSLib, and handle resolution for spells, levels,
waypoints, subregions and quest titles. `tools/build_db.py` is the remaining small piece of work.

---

## Sources

* LSLib repo, releases and CLI: https://github.com/Norbyte/lslib, https://github.com/Norbyte/lslib/releases,
  `Divine/CLI/CommandLineArguments.cs`, `Divine/CLI/CommandLineActions.cs` (`TryToValidatePath`)
* bg3se Lua API (`Ext.Loca`, `Ext.StaticData`): https://github.com/Norbyte/bg3se/blob/main/Docs/API.md
* bg3.wiki modding page: https://bg3.wiki/wiki/Modding:PAK_files
* bg3forge: https://pypi.org/project/bg3forge/, https://github.com/crazyace/bg3-forge
* bg3rustpaklib: https://docs.rs/bg3rustpaklib
* bg3.wiki license: https://bg3.wiki/wiki/bg3wiki:Copyrights. robots: https://bg3.wiki/robots.txt
* Difficulty: https://bg3.wiki/wiki/Difficulty. Rest: https://bg3.wiki/wiki/Long_rest, https://bg3.wiki/wiki/Camp_Supplies
* Acts: https://bg3.wiki/wiki/Act_One, https://bg3.wiki/wiki/Act_Two, https://bg3.wiki/wiki/Act_Three
* Quests and places: https://bg3.wiki/wiki/Reach_Moonrise_Towers, https://bg3.wiki/wiki/Gauntlet_of_Shar,
  https://bg3.wiki/wiki/Iron_Throne, https://bg3.wiki/wiki/Rescue_the_Grand_Duke,
  https://bg3.wiki/wiki/Get_Gortash%27s_Netherstone, https://bg3.wiki/wiki/Confront_the_Elder_Brain,
  https://bg3.wiki/wiki/Defiled_Temple, https://bg3.wiki/wiki/Necromancy_of_Thay
* Game over and timing: https://bg3.wiki/wiki/Game_Over_screens, https://bg3.wiki/wiki/Time-sensitive_activities
* Patch history context: https://gamespot.com/articles/baldurs-gate-3-hotfix-8-resolves-several-bugs-shaves-the-cat/1100-6518136/,
  https://www.gamepro.de/artikel/baldurs-gate-3-hotfix-18-inhalte,3432973.html
* Local inspection: `Data/*.pak` headers and listings; `english.loca`; `quest_prototypes.lsx`; the `nm -C` and `strings`
  output of `bin/bg3`; a local save's `SaveInfo.json` (read-only).
