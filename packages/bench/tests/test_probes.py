# pyright: basic
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from bg3bench import fairness_probes

REPO = Path(__file__).resolve().parents[3]
LIST_TOOLS = "import asyncio, json; from bg3ai import server; print(json.dumps(sorted(t.name for t in asyncio.run(server.mcp.list_tools()))))"


@pytest.fixture(scope="module")
def server_tools(tmp_path_factory):
    """The bg3 server's tool registry, offline: import bg3ai.server in the bridge's own env, in a subprocess."""
    if shutil.which("uv") is None:
        pytest.skip("uv is needed to run the bridge's env")
    env = {**os.environ, "BG3AI_RUNTIME": str(tmp_path_factory.mktemp("bg3ai-runtime"))}
    out = subprocess.run(
        ["uv", "run", "--project", str(REPO / "bridge"), "python", "-c", LIST_TOOLS],
        capture_output=True, text=True, env=env, timeout=300, check=True,
    )  # fmt: skip
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.integration
def test_every_server_tool_has_a_probe_or_an_exemption(server_tools):
    table = fairness_probes.load()
    assert len(server_tools) > 50
    assert fairness_probes.check(table, server_tools) == []


def test_the_table_is_well_formed():
    table = fairness_probes.load()
    rows = table["tools"]
    assert [r["tool"] for r in rows] == sorted(r["tool"] for r in rows)
    probed = {r["tool"] for r in rows if "probes" in r}
    # tools that act on an entity by id must refuse a hidden one
    for tool in ("look_at", "move_to", "interact", "pick_up", "talk_to", "pickpocket", "use_ability"):
        assert tool in probed and any(
            p["expect"] == "refusal" for p in next(r for r in rows if r["tool"] == tool)["probes"]
        )
    # the main observation tools are probed for every kind of hidden thing they could show
    look = next(r for r in rows if r["tool"] == "look")
    assert {p["hidden"] for p in look["probes"]} == {"creature", "object", "place"}
    assert "load_game" not in probed and "wiki_page" not in probed


BAD = [
    ({"schema": 2}, "not a v1"),
    ({"hidden_kinds": {}}, "hidden_kinds"),
    ({"tools": [{"probes": []}]}, "no tool name"),
    ({"tools": [{"tool": "x"}]}, "not both or neither"),
    ({"tools": [{"tool": "x", "exempt": "r", "probes": []}]}, "not both or neither"),
    ({"tools": [{"tool": "x", "exempt": " "}]}, "needs a reason"),
    ({"tools": [{"tool": "x", "probes": []}]}, "non-empty list"),
    ({"tools": [{"tool": "x", "probes": [{"args": {}}]}]}, "exactly args"),
    ({"tools": [{"tool": "x", "probes": [{"args": [], "hidden": "creature", "expect": "absent"}]}]}, "args must be"),
    ({"tools": [{"tool": "x", "probes": [{"args": {}, "hidden": "ghost", "expect": "absent"}]}]}, "unknown hidden"),
    ({"tools": [{"tool": "x", "probes": [{"args": {}, "hidden": "creature", "expect": "maybe"}]}]}, "expect must"),
    (
        {"tools": [{"tool": "x", "probes": [{"args": {"t": "e1"}, "hidden": "creature", "expect": "refusal"}]}]},
        "aim at",
    ),
    ({"tools": [{"tool": "x", "exempt": "r"}, {"tool": "x", "exempt": "r"}]}, "listed twice"),
]


@pytest.mark.parametrize("change,msg", BAD, ids=[m for _, m in BAD])
def test_check_finds_problems(change, msg):
    table = {**fairness_probes.load(), **change}
    assert any(msg in e for e in fairness_probes.check(table)), fairness_probes.check(table)


def test_coverage_against_a_tool_list(tmp_path):
    table = fairness_probes.load()
    names = [r["tool"] for r in table["tools"]]
    assert fairness_probes.check(table, names) == []
    errs = fairness_probes.check(table, [*names[1:], "new_tool"])
    assert any("without a probe or an exemption: new_tool" in e for e in errs)
    assert any(f"doesn't have: {names[0]}" in e for e in errs)
    (tmp_path / "t.json").write_text(json.dumps({**table, "schema": 9}))
    with pytest.raises(ValueError, match="not a v1"):
        fairness_probes.load(tmp_path / "t.json")


def test_expand():
    plan = '{"action":[{"target":"{hidden}"}]}'
    assert fairness_probes.expand({"plan": plan, "n": 3, "l": ["{visible}"]}, "e9", "e2") == {
        "plan": '{"action":[{"target":"e9"}]}',
        "n": 3,
        "l": ["e2"],
    }
