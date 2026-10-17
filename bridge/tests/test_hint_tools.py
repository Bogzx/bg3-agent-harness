"""Every tool a hint names exists (A2, naut2 run 3 called mcp__bg3__ToolSearch).

Refusals, NEXT lines and hints in the bridge and the mod name tools for the agent to call: `travel_to(objective=o2)`,
`trade sell=junk`, `queue_actions [interact e56, interact e57]`, `{"tool": "interact"}`, `NEXT: jump_to ...`. A renamed
tool or parameter left behind in one of them is a dead hint. This scans the agent-facing strings (Python string
constants and f-string text, the docstrings of MCP tools; Lua string literals) and checks each mention against the
registered tools and their parameters. The mod's refusals must name MCP tools, not the RPC ops behind them
(Actions.lua said "use act.talk to talk to characters": the tool is talk_to).
"""
from __future__ import annotations

import ast
import asyncio
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_turn_macros import Base  # noqa: E402,F401  (sets BG3AI_RUNTIME before importing server)
from bg3ai import server  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "bridge" / "src" / "bg3ai"
LUA = REPO / "mod" / "Mods" / "BG3AI" / "ScriptExtender" / "Lua" / "Server"
PY_FILES = ["server.py", "route.py", "turn.py", "render.py", "compact.py", "navhints.py", "macros.py", "actq.py",
            "longnav.py", "delta.py", "twostep.py", "hotbar.py", "clicker.py", "party.py", "economy.py", "saver.py",
            "spellprep.py"]
LUA_FILES = ["Guard.lua", "Actions.lua", "Navigation.lua"]
# Prose strings that name an RPC op on purpose (they answer the bridge, not the agent).
RPC_OK = {"act.note: only queue_* events"}

CALL = re.compile(r"(?<![\w.])([a-z][a-z0-9_]*)\(([^()]*)\)?")
KW_FORM = re.compile(r"(?<![\w.])([a-z][a-z0-9_]*)((?: [a-z_]+=[^ ,;)]+)+)")
STEPS = re.compile(r"\b(?:queue_actions|do_actions) \[([^\]]*)\]")
TOOL_KEY = re.compile(r"""\btool["']?\s*[:=]\s*["']([A-Za-z_]+)["']""")
NEXT = re.compile(r"NEXT: ([a-z][a-z0-9_]*)")
RPC_OP = re.compile(r"(?<![\w.])(?:act|ui|nav|state|save|hotbar|party|vis)\.[a-z_]+")
LUA_STR = re.compile(r'"((?:[^"\\\n]|\\.)*)"|\'((?:[^\'\\\n]|\\.)*)\'')


def registered() -> dict[str, set[str]]:
    return {t.name: set((t.input_schema or {}).get("properties", {}))
            for t in asyncio.run(server.mcp.list_tools())}


def _is_tool_def(node: ast.AST) -> bool:
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
        "tool" in ast.unparse(d) for d in node.decorator_list)


def py_strings(path: Path) -> list[tuple[int, str]]:
    """String constants and f-string text; docstrings only of MCP tools (other docstrings name Python functions)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and not _is_tool_def(n):
            body = n.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                skip.add(id(body[0].value))
    out: list[tuple[int, str]] = []
    in_fstring: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.JoinedStr):
            parts = []
            for v in n.values:
                in_fstring.add(id(v))
                parts.append(v.value if isinstance(v, ast.Constant) else "{}")
            out.append((n.lineno, "".join(parts)))
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip | in_fstring:
            out.append((n.lineno, n.value))
    return out


def lua_strings(path: Path) -> list[tuple[int, str]]:
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("--"):
            continue
        for m in LUA_STR.finditer(line):
            out.append((i, m.group(1) if m.group(1) is not None else m.group(2)))
    return out


def _regex(s: str) -> bool:
    return s.startswith("^") or "(?" in s or "\\s" in s or "\\d" in s


def dead_mentions(where: str, s: str, tools: dict[str, set[str]], lua: bool = False) -> list[str]:
    """Problems in one string: tool names that don't exist, parameters a tool doesn't take, RPC ops in prose."""
    if _regex(s):
        return []
    bad = []

    def kws(name: str, text: str, pat: str) -> None:
        for k in re.findall(pat, text):
            if k not in tools[name]:
                bad.append(f"{where}: {name} has no parameter {k!r}: {s[:120]!r}")

    for m in CALL.finditer(s):
        name = m.group(1)
        if name in tools:
            kws(name, m.group(2) or "", r"(?<![\w.])([a-z_]+)\s*=")
        elif "_" in name:
            bad.append(f"{where}: {name}( is not a tool: {s[:120]!r}")
    for m in KW_FORM.finditer(s):
        name = m.group(1)
        if name in tools:
            kws(name, m.group(2), r" ([a-z_]+)=")
        elif "_" in name and not lua:
            bad.append(f"{where}: {name} k=v is not a tool: {s[:120]!r}")
    for m in STEPS.finditer(s):
        for step in m.group(1).split(","):
            w = step.strip().split(" ")[0].strip("\"'")
            if w and w not in ("...", "…") and w not in tools:
                bad.append(f"{where}: step {w!r} is not a tool: {s[:120]!r}")
    for m in TOOL_KEY.finditer(s):
        if m.group(1) not in tools:
            bad.append(f"{where}: tool {m.group(1)!r} is not a tool: {s[:120]!r}")
    for m in NEXT.finditer(s):
        if "_" in m.group(1) and m.group(1) not in tools:
            bad.append(f"{where}: NEXT {m.group(1)!r} is not a tool: {s[:120]!r}")
    if lua and " " in s and not s.startswith("[BG3AI]") and s not in RPC_OK and RPC_OP.search(s):
        bad.append(f"{where}: names an RPC op, not an MCP tool: {s[:120]!r}")
    return bad


class HintTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = registered()

    def test_bridge_hints_name_real_tools_and_params(self):
        bad = []
        for f in PY_FILES:
            for ln, s in py_strings(SRC / f):
                bad += dead_mentions(f"{f}:{ln}", s, self.tools)
        self.assertEqual(bad, [], "\n".join(bad))

    def test_mod_hints_name_real_tools(self):
        bad = []
        for f in LUA_FILES:
            for ln, s in lua_strings(LUA / f):
                bad += dead_mentions(f"{f}:{ln}", s, self.tools, lua=True)
        self.assertEqual(bad, [], "\n".join(bad))

    def test_the_checker_catches_dead_hints(self):
        t = self.tools
        self.assertTrue(dead_mentions("x", "then travel_to(objective=o2)", t) == [])
        self.assertTrue(dead_mentions("x", "then travel_too(objective=o2)", t))      # renamed tool
        self.assertTrue(dead_mentions("x", "then travel_to(goal=o2)", t))            # renamed parameter
        self.assertTrue(dead_mentions("x", "(sell junk: trade sell_all=junk)", t))   # k=v form
        self.assertTrue(dead_mentions("x", "queue_actions [interact e56, climb e57]", t))
        self.assertTrue(dead_mentions("x", '{"tool": "use_item", "target": "e5"}', t))
        self.assertTrue(dead_mentions("x", "NEXT: jump_too (1,2,3)", t))
        self.assertTrue(dead_mentions("x", "use act.talk to talk to characters", t, lua=True))
        self.assertEqual(dead_mentions("x", "use talk_to to talk to characters", t, lua=True), [])

    def test_tool_docstrings_are_scanned(self):
        # an MCP tool's docstring is its description: it is in the scan; other functions' docstrings are not
        strings = [s for _, s in py_strings(SRC / "server.py")]
        self.assertTrue(any(s.startswith("Party control.") for s in strings))
        self.assertFalse(any("make_executor(who)" in s for _, s in py_strings(SRC / "actq.py")))


if __name__ == "__main__":
    unittest.main()
