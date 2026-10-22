# Launch gate

Nothing goes public (repo visibility, a GitHub release, a Hugging Face dataset, the leaderboard site, videos) until
the owner has settled all four items below (OVERHAUL-PLAN.md Phase F2.4). This page lists what is known so far for
each one. It takes no decisions: every box is the owner's to tick.

Sources: research/arch/game.md §2.5-2.6 and research/arch/bench.md "Licence" and open question 3 (research done
2026-10-21; the links were read then, not re-checked).

## 1. Larian's generative-AI clause

- [ ] Decided whether the benchmark may send BG3 screenshots and game text to models, and publish what comes back.

Known:
- Larian's general fan-content policy §4.5 (https://larian.com/fan-content-policy, updated 2025-07-31): "Do not
  prompt or train generative AI tools using Larian Studios IP", and no extracting game audio for machine learning.
- The same page says BG3 fan content falls under the separate BG3 Fan Content Terms
  (https://baldursgate3.game/bg3-fan-content-terms/), and those don't repeat the clause.
- Whether an agent sending screenshots to a model counts as "prompting with Larian IP" is a plausible reading, not
  a confirmed one. The research calls it the largest legal ambiguity for a public benchmark.
- Related: the Steam EULA §4(f) (no third-party program that reads the game's memory) is in tension with the Script
  Extender, which the whole modding community uses; single-player offline use is the community norm.
- Open: nobody has asked Larian. Text-only trajectories and `facts.jsonl` are the lower-risk part; videos,
  screenshots and start saves are the part to check.

## 2. Publishing start saves

- [ ] Decided whether start saves (`.lsv`) are published, where (release assets, a dataset, not git), and what
  replaces them if not.

Known:
- Larian's Fan Content Policy, the BG3 Fan Content Terms and the Steam EULA don't mention save files. Nexus hosts
  save uploads; CivBench ships its saves. Both are precedents, not permissions.
- A save embeds the mod list (BG3AI, possibly bg3le), the game version, the profile folder and character names.
  `tools/saves/inspect.py` lists what one embeds (Steam IDs, e-mail addresses, user paths, names) and runs on a
  copy only. No published-candidate save has been inspected yet.
- Saves need the same game build and mods to load, and can break across patches (docs/GAME-UPDATES.md).
- Fallback if saves are not published: a scripted recipe (new game, fixed build, route to the scenario point;
  `tools/saves/make_start_save.py`, `tools/dev/test_new_game.py`). Slow and fragile on story timing.
- State: only `nautiloid` starts from a new game; the other scenarios need start saves that are not made yet
  (docs/BENCHMARK.md, research/ovh/bench.md "Start saves to make").

## 3. The benchmark's name

- [ ] Picked the public name.

Known:
- The working names are the package names `bg3bench` and `bg3harness`.
- Wizards' Fan Content Policy (https://company.wizards.com/en/legal/fancontentpolicy) and Larian's IP FAQ both say
  not to use their logos or trademarks without permission. Wizards suggests a notice: "[Title] is unofficial Fan
  Content permitted under the Fan Content Policy. Not approved/endorsed by Wizards. Portions of the materials used
  are property of Wizards of the Coast. ©Wizards of the Coast LLC."
- Other game benchmarks use coined or shortened names (BALROG, VideoGameBench, CivBench, SpireBench) and name the
  game only in the text. The research recommends keeping "Baldur's Gate" out of the name and adding the notice and
  a "not affiliated with Larian" line. Wizards sent (and later withdrew) a DMCA notice against a BG-themed mod in
  2025.
- Open: whether "BG3" in `bg3bench` counts as using the trademark.

## 4. Scrub of personal paths and keys

- [ ] The tracked tree scans clean (exit 0) on the commit to publish.
- [ ] Every release folder scans clean before upload (docs/RELEASING.md step 3's `<out_dir>`).
- [ ] The git history is checked too, or published squashed: the scrub reads the work tree, not old commits.

The check is `tools/ci/scrub.py`. It reads every tracked file (path and contents) and, with `--artifacts`, a release
folder, and reports `path:line: kind` for home paths, e-mail addresses outside a noreply list, API-key shapes,
`.env` files, values of a real `.env` found verbatim, key assignments, Tailscale names and addresses, Steam IDs,
and personal terms (machine names, the owner's name and handles). Personal terms come from the local git user,
`$USER`, the hostname and `~/.config/bg3ai/scrub-terms.txt`, so the tool doesn't publish the list it looks for.
False positives and authorship lines go in `tools/ci/scrub_allowlist.txt` with a reason each.

```bash
uv run python tools/ci/scrub.py --env-file <your .env>                              # the tree
uv run python tools/ci/scrub.py --artifacts <out_dir> --env-file <your .env>         # plus a release
```

Exit 0: clean. 1: hits, listed on stdout (key and `.env` hits show the kind only, never the value). 2: a usage or
config error.

Known (2026-10-22, on `overhaul` 2174f13): 16 hits, all listed in research/ovh/mod.md "r14". No key, `.env` value,
e-mail or personal home path; the hits are the machine name in docs and tests, the owner's name in two word lists,
and a Tailscale-shaped test address. The allowlist treats LICENSE and CITATION.cff as authorship lines. Whether the
name may stay there is the owner's call.
