# scenarios/: fixed start points for benchmark series

A scenario is one `scenarios/<name>.json`: where a run starts (a new game, or a start save), the milestone it ends
at, the caps, the character profile and the per-run resets. `play.py run --scenario NAME [--runs N]` plays it as a
`--runs` series and freezes it into `runtime/sessions/<series>/series.json`, so every run of the series (and every
model compared on it) starts from the same point under the same rules.

```bash
python3 runner/play.py scenarios                              # each scenario: ready / needs a start save
python3 runner/play.py run --scenario nautiloid --hours 12    # 3 runs (the scenario's N), series "nautiloid"
python3 runner/play.py run --scenario crash_site_grove --runs 5 --playthrough grove-opus
python3 runner/play.py runs --playthrough nautiloid           # the runs table, as for any series
```

| Scenario | Start | Until | Caps | Status |
|---|---|---|---|---|
| `nautiloid` | new game (phase 0 from the brief) | `nautiloid_escaped` | 4 h | ready (the launch-bar series) |
| `crash_site_grove` | the beach after the crash | `grove` | 2 h, $50 | needs a start save |
| `act1_underdark` | goblin camp leaders dealt with | `underdark` (unverified, below) | 3 h, $75 | needs a start save |
| `act2_last_light` | just arrived in the Shadow-Cursed Lands | `last_light` | 2 h, $50 | needs a start save |

The drafts' caps are guesses from claude-run-2's pace (crash site → grove took it ~40 min of play, the Shadow-Cursed
Lands → Last Light ~50 min) at ~$25 per hour (the launch-bar runs: ~$4.5 per 12-min run). Tune them once, before a
series starts: a series refuses a changed scenario file.

## Format

```json
{
 "name": "crash_site_grove",                 // = the file name (a-z 0-9 _ -)
 "title": "Crash site → Emerald Grove",      // shown by play.py scenarios
 "about": "...",                             // for people; never shown to the agent
 "start": {"kind": "new_game"}               // or:
 "start": {"kind": "save",
           "save": "Claude-<id>__SCN-crash_site_grove",   // the save folder; null = placeholder (needs a start save)
           "save_sha1": null,                // optional: sha1 of the folder's .lsv (<folder>/<label>.lsv); a changed save is refused
           "note": {"where": "...", "objective": "...", "party": ["..."], "journal": ["..."]}},
 "until": "grove",                           // a milestone id of bridge/src/bg3ai/milestones.py
 "runs": 3,                                  // the default N (--runs overrides it)
 "caps": {"run_hours": 2, "max_cost": 50, "max_tokens": null},   // run_hours required
 "profile": {"character": {"name": "Claude", "brief": "...", "roleplay": "strict"}, "difficulty": "Explorer"},
 "reset": {...}                              // optional; if given, must be the standard one (below)
}
```

Validation (`runs.validate_scenario`, run by `play.py scenarios` and `run --scenario`): the milestone exists, caps
are positive (run_hours set), the profile has a brief (and a valid roleplay/difficulty); a save start needs
`profile.character.name` equal to the save's character (the folder's owner part: the save fixes the character, so
the profile must match it), a start note with `where` and `objective`, and a save label that isn't an agent `Sxxx-`
label (re-save it as `SCN-<name>`, see below).

**The flags.** `run --scenario` takes `--until`, `--character-brief`, `--roleplay`, `--difficulty`, `--save-owner`
only when they equal the scenario's (otherwise exit 2: the scenario fixes them); the caps follow the series' caps
rule (`--run-hours/--max-cost/--max-tokens` different from the scenario's: refused unless `--allow-cap-change`,
recorded). `--playthrough` names the series (default: the scenario's name), `--runs` the number of runs (default:
the scenario's), `--hours` the whole series' budget. A scenario that isn't ready is refused.

**Frozen per series** (series.json `scenario`: `name`, `digest`, `start_save_sha1`, `spec` = the whole scenario):
a later run is refused when the scenario file changed (digest), the start save's `.lsv` changed (sha1), or the
series belongs to another scenario or none. Run records carry `scenario`.

## Per-run resets

The same for every scenario (`runs.RESETS`; not tunable, so runs stay comparable):

| State | new game | start save |
|---|---|---|
| saves | only saves written after the run start are the run's (`saves_after`) | the same, plus the start save itself: the runner loads it for the first session (it is older than the run) and the agent's first own save supersedes it |
| NAV-3 kill list and breadcrumbs (`nav_guard.json`, `nav_<uuid>.json`) | moved aside into `runtime/sessions/<run>/reset/` | same |
| `play/knowledge/` | snapshot; the agent's edits are put back after the run | same |
| memory | fresh from `memory-template/` | fresh from `memory-template/`, plus the start note as `HANDOFF.md` |
| the game process | stopped between runs | same |

**Memory for a mid-game start.** Never a copy of another playthrough's memory (claude-run-2's memory is Claude's
own play history: giving it to any model, Claude included, contaminates the benchmark). A run gets the empty
template plus a short, neutral start note that the harness writes: `runs.start_note(scenario)`, a pure function of
the scenario's `start.note` (fixed text: where the party is, the current objective, the party, the journal's quest
titles). Chosen over reading the game at load because the note is then byte-identical for every run and every
model, is reviewable before a series starts, and needs no game before the memory exists. The validator refuses a
note that mentions sessions (`S012`), playthroughs or memory paths, or is longer than 1500 characters. Write it from
the game's own screens (the journal, the map) when you make the save: only what a player sees there.

The first session's kickoff also gets a `SCENARIO START` runner note (`play.scenario_start_note`): this is a fixed
save, not a new game; the character is the save's; read it back with get_status/get_party and write CHARACTER.md
(persona from the profile's brief, header lines from the game) before playing on.

## Making a start save (live)

Candidates from the existing `Claude-` saves (read-only listing, 2026-10-16; all claude-run-2, character `Claude`,
an Oath of Devotion paladin, Explorer). Times are when claude-run-2 first reached the area (its progress events):

| Scenario | Candidate save folder | Saved | Why |
|---|---|---|---|
| `crash_site_grove` | `Claude-33912620918__S003-beach` | 10-09 23:33 | surface reached 23:31, beach 23:34; grove not until 00:13 |
| | `Claude-39912620940__S003-end` (alt.) | 10-09 23:39 | a few minutes later, still before the grove |
| `act1_underdark` | `Claude-28101261955__S018-end` | 10-10 04:28 | goblin camp leaders dead, Halsin joined; claude-run-2 never entered the Underdark (it took the mountain pass), so `underdark` is unseen |
| | `Claude-51101260954__S018-leaders-dead` (alt.) | 10-10 03:51 | right after the leaders |
| `act2_last_light` | `Claude-40101264933__S024-shadowlands-arrived` | 10-10 07:40 | Shadow-Cursed Lands reached 07:40; Last Light not until 08:27 |
| | `Claude-36101264951__S024-pre-shadowlands` (alt.) | 10-10 07:36 | just before the arrival |

Caveats: these saves carry claude-run-2's choices in the game state (party, approval, decisions, gear). Every model
gets the same state, so the comparison stays fair, but it is Claude's party, not a neutral one. A cleaner set later:
a dedicated character with its own name (not `Claude`), played to each point once and saved as `SCN-<name>`. Also:
a Claude scenario's runs make `Claude-…__Sxxx-…` saves, and claude-run-2 (owner `Claude`, no `saves_after`) takes the
newest `Sxxx-` save of `Claude`: don't resume claude-run-2 after a Claude scenario series without pointing it at its
own save first.

Steps for each scenario (the runner stopped; Steam offline as always; don't touch the candidate save):

1. Launch the game straight into the candidate: `runner/launch_game.sh --load <candidate folder>` and wait for it
   to load (`python3 runner/play.py status` shows `state Running`).
2. Check the state against the scenario: in the right area (the minimap name), out of combat, no dialogue open,
   the party as in `start.note.party`; and the `until` milestone not reached yet (`grove`, `underdark`,
   `last_light` must not be on the screen's area names or in the journal as done). If it isn't a clean moment, walk
   a few steps to one (no story progress).
3. Save it under the scenario's own label (the agent's tools, from the repo root):
   ```bash
   cd bridge/src && BG3AI_PLAYTHROUGH=scn-setup BG3AI_SAVE_PREFIX=Claude- BG3AI_SESSION_DEADLINE=1 \
     uv run --project .. python -c "from bg3ai import server as S; print(S.save_game('SCN-<name>'))"
   ```
   `BG3AI_PLAYTHROUGH` opens the save gates (`save_game` refuses outside an AI playthrough); a session deadline in
   the past makes the save synchronous (otherwise it runs in a background thread that dies with the `python -c`
   process). It prints the outcome once the .lsv is on disk (`SAVED …` / `done`, or `FAILED — …`; side effect:
   `runtime/saves/scn-setup.txt` records it). Check: `ls -t "$HOME/.local/share/Larian Studios/Baldur's Gate 3/PlayerProfiles/Public/Savegames/Story" | grep SCN-<name>`.
   The same env works for the read tools of step 4 (`S.get_status()`, `S.get_party()`, `S.get_journal()`).
4. Write the start note from the game's screens into the scenario's `start.note`: `where` (area, out of combat),
   `objective` (the journal's current main objective, as worded there), `party` (names and classes),
   `journal` (the active quest titles). The drafts hold claude-run-2's journal titles at those times as a starting
   point: correct them to what the save shows.
5. Put the folder into `start.save`, and optionally `save_sha1`:
   `sha1sum ".../Story/<folder>/SCN-<name>.lsv"` (the game names the .lsv after the label, next to a `.WebP`). Check the profile name equals the folder's owner (`Claude`).
6. `python3 runner/play.py scenarios` shows it `ready`. Quit the game (the runner launches its own).
7. First series: `python3 runner/play.py run --scenario <name> --runs 1 --session-minutes 30` supervised; check the
   first session's kickoff has the SCENARIO START note, the runner log says `loading save <folder>`, the
   agent's first save supersedes it, and the run ends at the milestone.

## Not live-tested yet

- `act1_underdark`'s until: the `underdark` milestone is an area pattern, and the Underdark has no region name
  (it is inside `WLD_Main_A`), so it fires only if a minimap area name contains "Underdark". No run has been
  there yet. Before its first series, walk into the Underdark once from the start save, read the area name
  (`get_status`), reload without saving; if no area says "Underdark", the milestone needs another fact first.

- Loading a start save older than the run in a series (`ui.menu` load of a save the runner didn't make; the Load
  page index comes from all of the owner's saves).
- The MCP server keeping the start save visible (6e473a6, offline): the runner passes `BG3AI_START_SAVE=<folder>`
  and `_my_saves` keeps that folder although it predates the run, so `load_game("<folder>")` works for an agent
  wiped before its first own save (`"latest"` takes `Sxxx-` saves only, so not an `SCN-` one).
- Whether `progress`'s silent baseline at the start save counts milestones the save already has (it should: a
  milestone already true at the first poll is "reached" with no time). That's why step 2 checks `until` is unseen.
