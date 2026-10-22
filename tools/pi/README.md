# tools/pi: the pinned pi agent

The harness runs non-Claude models (and Claude on the ranked track, D6 in docs/OVERHAUL-PLAN.md) through pi
(`@earendil-works/pi-coding-agent`), pinned to 1.3.0 here and installed per repo, never globally:

    cd tools/pi && npm ci --ignore-scripts

- `bg3.ts`: the only extension pi loads (`pi -ne -na -e bg3.ts`). It connects to the bg3 MCP server itself and
  registers every tool as `mcp__bg3__<name>` with `executionMode: "sequential"` (pi has no other way to run tool
  calls one at a time), sets the system text verbatim from a file, and records each response's id, served
  provider and raw usage (OpenRouter's billed `usage.cost`) as a `bg3.billing` session entry.
- `conformance/`: offline checks of that setup against a stub model and a stub MCP server (no key, no game). Marked
  `e2e`: CI's e2e job installs tools/pi and runs it; it skips without node_modules, except under CI where it fails.
- `test_pi_install.py`: the pinned version.

The design and the evidence behind it: research/arch/pi-client.md (local). The runner side is `bg3harness/pi_client.py`
and the profiles `bg3harness/profiles.py` + `profiles/*.toml`.

    python3 -m unittest tools/pi/test_pi_install.py
    python3 -m unittest tools/pi/conformance/test_pi_conformance.py
    cd tools/pi && npx tsc --noEmit
