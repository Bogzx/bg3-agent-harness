#!/usr/bin/env bash
# The offline Lua suites (TARGET-ARCHITECTURE.md "Tooling"): every tools/dev/test_*.sh and tools/vis/test_*.sh, each
# running its .lua tests in Lua 5.3 and 5.4 under Docker (nickblah/lua images). Prints one line per suite (and a
# failing suite's output) and exits 1 when any suite fails. On GitHub Actions every suite's output is a collapsed
# group and a failure is an error annotation.
#
#   bash tools/ci/lua_suites.sh            # all suites
#   bash tools/ci/lua_suites.sh <suite.sh>...
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 2

# Known broken, with the reason; a suite leaves this list when it passes again.
declare -A BROKEN=()

if [ $# -gt 0 ]; then suites=("$@"); else suites=(tools/dev/test_*.sh tools/vis/test_*.sh); fi
fail=0
for t in "${suites[@]}"; do
  if [ $# -eq 0 ] && [ -n "${BROKEN[$t]:-}" ]; then
    echo "SKIP $t (${BROKEN[$t]})"
    continue
  fi
  out=$(bash "$t" 2>&1)
  rc=$?
  if [ -n "${GITHUB_ACTIONS:-}" ]; then printf '::group::%s\n%s\n::endgroup::\n' "$t" "$out"; fi
  if [ $rc -eq 0 ]; then
    echo "PASS $t"
  else
    [ -z "${GITHUB_ACTIONS:-}" ] && printf '%s\n' "$out"
    echo "FAIL $t (exit $rc)"
    [ -n "${GITHUB_ACTIONS:-}" ] && echo "::error::$t failed"
    fail=1
  fi
done
exit $fail
