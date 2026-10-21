"""Offline validation of tools/dev/acceptance.py (`acceptance.py --check`): no game, nothing written but scratch files.

What it proves without the game, so a live window isn't spent on a typo:
  - the file parses, every section is registered, every name a function reads is defined (symtable)
  - tools: every call("x")/took("x")/counted("x") names a bridge tool, its keywords are parameters of it, required
    ones are given, literal values fit the annotation (str vs number)
  - Python attributes: S.<a>.<b> on the imported bridge server, and attributes of modules imported inside sections
    (from bg3ai import hotbar as HB → HB.cast_state)
  - RPC ops: try_rpc/rpc/run_job("op", context=...) are registered by the mod in that context (Server/ = server,
    Client/ = client, Shared/ = both; RPC.register, State.lua timed(), Actions.lua reg() = act.<name>); other op-like
    strings (a tuple of ops in a loop) exist in some context
  - Lua snippets sent with lua_server()/S.game.eval(): BG3AI module fields they use exist in that context's Lua
    (Mods.BG3AI.BG3AI.<Mod>.<field>, local aliases of modules); Ext._Internal.<fn> exists in bg3le (source or
    patches); each snippet compiles (luac -p in Docker nickblah/lua:5.4, skipped without Docker)
  - saves: each save the sections load resolves the way load_game resolves it (the playthrough's folders, newest
    first), duplicates are named, and its SaveInfo.json (read from the .lsv in memory, read-only) matches what the
    section needs (level, party, a spot): acceptance.SAVE_NEEDS
Verdicts: FAIL (the live item would break), WARN (worth a look), PASS, SKIP (a check that can't run here).
"""

from __future__ import annotations

import ast
import builtins
import importlib
import inspect
import json
import math
import re
import shutil
import subprocess
import symtable
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LUA_ROOT = REPO / "mod" / "Mods" / "BG3AI" / "ScriptExtender" / "Lua"
TOOL_CALLERS = {"call", "took", "counted"}          # helpers in acceptance.py: first arg = the bridge tool's name
RPC_METHODS = {"try_rpc": "server", "rpc": "server", "run_job": "server"}  # game.py defaults
OP_LIKE = re.compile(r"^(ui|state|act|nav|sys|job|events|camp|dev|autoskip)\.[a-z_][a-z_0-9.]*$")


# ------------------------------------------------------------------------------------------------- the mod's Lua

def lua_context(path: Path) -> set[str]:
    top = path.relative_to(LUA_ROOT).parts[0]
    return {"Server": {"server"}, "Client": {"client"}}.get(top, {"server", "client"})


def lua_index(root: Path = LUA_ROOT) -> dict:
    """ops: op -> contexts; modules: name -> {ctx, defs (fields defined), refs (fields referenced)}; root: BG3AI.x."""
    ops: dict[str, set] = {}
    mods: dict[str, dict] = {}
    root_fields: set[str] = set()
    for f in sorted(root.rglob("*.lua")):
        ctx = lua_context(f) if root == LUA_ROOT else {"server", "client"}
        txt = f.read_text(errors="replace")
        for m in re.finditer(r'RPC\.register\("([\w.]+)"', txt):
            ops.setdefault(m.group(1), set()).update(ctx)
        if f.name.startswith("State"):  # State.lua and its parts
            for m in re.finditer(r'^\s*timed\("([\w.]+)"', txt, re.M):
                ops.setdefault(m.group(1), set()).update(ctx)
        if f.name.startswith("Actions"):  # Actions.lua and its parts
            for m in re.finditer(r'^\s*reg\("([\w.]+)"', txt, re.M):
                ops.setdefault("act." + m.group(1), set()).update(ctx)
        aliases: dict[str, str] = {}
        for m in re.finditer(r"\blocal\s+(\w+)\s*=\s*BG3AI\.(\w+)\b(?!\s*[.(\[:])", txt):
            aliases[m.group(1)] = m.group(2)
        for m in re.finditer(r"^\s*BG3AI\.(\w+)\s*=\s*(\w+)\s*$", txt, re.M):
            if m.group(2) not in ("nil", "true", "false"):
                aliases[m.group(2)] = m.group(1)
        for m in re.finditer(r"^\s*(?:function\s+BG3AI\.(\w+)|BG3AI\.(\w+)\s*=(?!=))", txt, re.M):
            root_fields.add(m.group(1) or m.group(2))
        for alias, mod in aliases.items():
            d = mods.setdefault(mod, {"ctx": set(), "defs": set(), "refs": set()})
            if re.search(rf"^\s*(?:local\s+)?{re.escape(alias)}\s*=\s*(?:BG3AI\.{mod}\s+or\s+)?\{{", txt, re.M) or \
                    re.search(rf"^\s*BG3AI\.{mod}\s*=\s*{re.escape(alias)}\s*$", txt, re.M):
                d["ctx"].update(ctx)  # the file that creates the module table
            a = re.escape(alias)
            d["defs"].update(re.findall(rf"\bfunction\s+{a}[.:](\w+)", txt))
            d["defs"].update(re.findall(rf"(?<![\w.]){a}\.(\w+)\s*=(?!=)", txt))
            for line in re.findall(rf"^\s*({a}\.\w+(?:\s*,\s*{a}\.\w+)+)\s*=(?!=)", txt, re.M):
                d["defs"].update(re.findall(rf"{a}\.(\w+)", line))
            d["refs"].update(re.findall(rf"(?<![\w.]){a}[.:](\w+)", txt))
    for mod in mods.values():
        if not mod["ctx"]:
            mod["ctx"] = {"server", "client"}
    return {"ops": ops, "modules": mods, "root": root_fields}


def bg3le_internals() -> set[str]:
    names: set[str] = set()
    for base in (REPO / "third_party" / "bg3le" / "src", REPO / "patches" / "bg3le"):
        if not base.is_dir():
            continue
        for f in base.rglob("*"):
            if f.suffix in (".inl", ".cpp", ".h", ".hpp", ".patch", ".lua") and f.is_file():
                try:
                    names.update(re.findall(r'"(\w+)"', f.read_text(errors="replace")))
                except OSError:
                    pass
    return names


def lua_refs(code: str) -> list[tuple[str, str | None]]:
    """(module, field) pairs a snippet uses: Mods.BG3AI.BG3AI.Mod.field, aliases of the root and of modules.
    (module, None) = only the module itself is named. ("", field) = a root field (Mods.BG3AI.BG3AI.rpc)."""
    root = r"Mods\.BG3AI\.BG3AI"
    roots = set(re.findall(rf"\blocal\s+(\w+)\s*=\s*{root}\b(?!\s*\.)", code))
    alias: dict[str, str] = {}
    for m in re.finditer(rf"\blocal\s+(\w+)\s*=\s*{root}\.(\w+)\b(?!\s*[.(\[:])", code):
        alias[m.group(1)] = m.group(2)
    for r in roots:
        for m in re.finditer(rf"\blocal\s+(\w+)\s*=\s*{re.escape(r)}\.(\w+)\b(?!\s*[.(\[:])", code):
            alias[m.group(1)] = m.group(2)
    out: list[tuple[str, str | None]] = []
    heads = [root] + [re.escape(r) for r in roots]
    for h in heads:
        for m in re.finditer(rf"(?<![\w.]){h}\.(\w+)(?:\s*\.\s*(\w+)|(?:\s*\[\s*\"([\w.]+)\"\s*\]))?", code):
            first, second = m.group(1), m.group(2)
            if first.startswith("__"):
                continue
            if first[0].isupper():
                out.append((first, second))
            else:
                out.append(("", first))
    for a, mod in alias.items():
        for m in re.finditer(rf"(?<![\w.]){re.escape(a)}[.:](\w+)", code):
            out.append((mod, m.group(1)))
        out.append((mod, None))
    return out


def luac_check(snippets: list[tuple[str, str]]) -> tuple[str, list[str]]:
    """Compile each snippet with luac -p (Lua 5.4, Docker). -> (status, errors); status 'skip: why' when it can't run."""
    if not shutil.which("docker"):
        return "skip: no docker", []
    with tempfile.TemporaryDirectory(prefix="acc-luac-") as d:
        names = []
        for i, (label, code) in enumerate(snippets):
            p = Path(d) / f"s{i:03d}.lua"
            p.write_text(code + "\n")
            names.append((p.name, label))
        script = "for f in /s/*.lua; do luac -p \"$f\" 2>&1 || true; done"
        try:
            r = subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", f"{d}:/s:ro", "nickblah/lua:5.4",
                                "sh", "-c", script], capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"skip: docker run failed ({e})", []
        if r.returncode != 0 and not r.stdout.strip():
            return f"skip: docker run failed ({r.stderr.strip()[:120]})", []
        errs = []
        lab = dict(names)
        for line in r.stdout.splitlines():
            m = re.search(r"/s/(s\d{3}\.lua)", line)
            if m:
                errs.append(f"{lab.get(m.group(1), m.group(1))}: {line.split(m.group(1), 1)[1].lstrip(':')[:160]}")
        return "ok", errs


# ------------------------------------------------------------------------------------------------- the Python file

class _Fn:
    """String constants a function assigns to names (g = "local G=..." then lua_server(g + '...')), by line: the
    last assignment before a use wins, so a name rebound to a dict reads as not a string."""

    def __init__(self, node: ast.AST, module_consts: dict[str, object]):
        self.module = dict(module_consts)
        self.assigns: dict[str, list[tuple[int, str | None]]] = {}
        for n in ast.walk(node):
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                self.assigns.setdefault(n.targets[0].id, []).append((n.lineno, None))
        for name, rows in self.assigns.items():
            rows.sort()
        for n in ast.walk(node):  # values second: a string built from an earlier string name resolves too
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                rows = self.assigns[n.targets[0].id]
                i = rows.index((n.lineno, None)) if (n.lineno, None) in rows else -1
                if i >= 0:
                    rows[i] = (n.lineno, const_str(n.value, self.at(n.lineno)))

    def at(self, line: int) -> dict[str, object]:
        """Names -> their string value at this line (non-strings map to None)."""
        out = dict(self.module)
        for name, rows in self.assigns.items():
            before = [v for ln, v in rows if ln < line]
            if before:
                out[name] = before[-1]
        return out


def const_str(node: ast.AST, names: dict[str, object], dynamic: list | None = None) -> str | None:
    """A string expression's value, with f-string fields as 0 (dynamic gets their source). None = not a string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                out.append(str(v.value))
            else:
                if dynamic is not None:
                    dynamic.append(ast.unparse(v.value))
                out.append("0")
        return "".join(out)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = const_str(node.left, names, dynamic), const_str(node.right, names, dynamic)
        return a + b if a is not None and b is not None else None
    if isinstance(node, ast.Name) and isinstance(names.get(node.id), str):
        return names[node.id]
    return None


def attr_chain(node: ast.AST) -> list[str] | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return [node.id] + parts[::-1]
    return None


def kw_const(call: ast.Call, name: str):
    for k in call.keywords:
        if k.arg == name and isinstance(k.value, ast.Constant):
            return k.value.value
    return None


def literal_type_ok(ann, value) -> bool:
    if isinstance(ann, str):
        a = ann.replace(" ", "")
    else:
        a = getattr(ann, "__name__", str(ann))
    alts = set(a.split("|"))
    if "Any" in alts or "object" in alts or not alts:
        return True
    if value is None:
        return "None" in alts or "Optional" in a
    if isinstance(value, bool):
        return alts != {"str"}
    if isinstance(value, (int, float)):
        return bool(alts & {"int", "float", "bool"})
    if isinstance(value, str):
        return "str" in alts or any(x.startswith(("dict", "list")) for x in alts)
    return True


def check(acc_path: Path, S, acc_module=None, lua: dict | None = None, internals: set[str] | None = None,
          run_luac: bool = True) -> list[tuple[str, str, str]]:
    """-> [(verdict, check, evidence)]."""
    res: list[tuple[str, str, str]] = []

    def add(verdict: str, what: str, ev: str) -> None:
        res.append((verdict, what, ev))

    src = acc_path.read_text()
    try:
        tree = ast.parse(src, str(acc_path))
    except SyntaxError as e:
        add("FAIL", "parse", f"{e}")
        return res
    add("PASS", "parse", f"{acc_path.name}: {len(src.splitlines())} lines")
    lua = lua if lua is not None else lua_index()
    internals = internals if internals is not None else bg3le_internals()

    # --- sections registered
    if acc_module is not None:
        secs, funcs = list(getattr(acc_module, "SECTIONS", [])), dict(getattr(acc_module, "FUNCS", {}))
        missing = [s for s in secs if s not in funcs]
        extra = [s for s in funcs if s not in secs]
        add("FAIL" if missing else ("WARN" if extra else "PASS"), "sections registered",
            f"SECTIONS={secs}; no function: {missing or 'none'}; in FUNCS but not run: {extra or 'none'}")

    # --- undefined names (symtable: implicit globals that the module doesn't define)
    mod_names = {n for n in dir(builtins)} | {"__file__", "__name__", "__doc__"}
    for node in tree.body:
        for t in (ast.walk(node) if not isinstance(node, (ast.FunctionDef, ast.ClassDef)) else [node]):
            if isinstance(t, (ast.FunctionDef, ast.ClassDef)):
                mod_names.add(t.name)
            elif isinstance(t, ast.Name) and isinstance(t.ctx, ast.Store):
                mod_names.add(t.id)
            elif isinstance(t, (ast.Import, ast.ImportFrom)):
                mod_names.update((a.asname or a.name).split(".")[0] for a in t.names)
            elif isinstance(t, ast.Global):
                mod_names.update(t.names)
    undefined: list[str] = []

    def walk_st(t: symtable.SymbolTable) -> None:
        for s in t.get_symbols():
            if t.get_type() != "module" and s.is_referenced() and s.is_global() and not s.is_declared_global() \
                    and s.get_name() not in mod_names:
                undefined.append(f"{t.get_name()}:{s.get_name()}")
        for c in t.get_children():
            walk_st(c)

    walk_st(symtable.symtable(src, str(acc_path), "exec"))
    add("FAIL" if undefined else "PASS", "names defined", ", ".join(sorted(set(undefined))) or "every name a function reads exists")

    # --- per function: tools, attributes, RPC ops, Lua snippets
    module_consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            v = const_str(node.value, {})
            if v is not None:
                module_consts[node.targets[0].id] = v
    tool_bad, tool_ok, attr_bad, rpc_bad, rpc_ok, lua_bad, lua_warn = [], set(), [], [], set(), [], []
    snippets: list[tuple[str, str]] = []
    nrefs = 0
    ncalls = 0
    imported: dict[str, object] = {}
    hasattr_guarded: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "hasattr" and len(n.args) == 2 \
                and isinstance(n.args[1], ast.Constant):
            hasattr_guarded.add(str(n.args[1].value))
    sys.path.insert(0, str(REPO / "runner"))
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        F = _Fn(fn, module_consts)
        where = fn.name
        for n in ast.walk(fn):
            if isinstance(n, ast.ImportFrom) and n.module:
                for a in n.names:
                    full = f"{n.module}.{a.name}"
                    try:
                        imported[a.asname or a.name] = importlib.import_module(full)
                    except ImportError:
                        try:
                            imported[a.asname or a.name] = getattr(importlib.import_module(n.module), a.name)
                        except (ImportError, AttributeError) as e:
                            attr_bad.append(f"{where}: from {n.module} import {a.name} ({e})")
            elif isinstance(n, ast.Import):
                for a in n.names:
                    try:
                        imported[a.asname or a.name] = importlib.import_module(a.name)
                    except ImportError as e:
                        attr_bad.append(f"{where}: import {a.name} ({e})")
        for n in ast.walk(fn):
            if not isinstance(n, ast.Call):
                continue
            # tool calls
            if isinstance(n.func, ast.Name) and n.func.id in TOOL_CALLERS and n.args:
                name = const_str(n.args[0], F.at(n.lineno))
                if name is None:
                    continue
                tool = getattr(S, name, None)
                if tool is None or not callable(tool):
                    tool_bad.append(f"{where}: {n.func.id}({name!r}): no such bridge tool")
                    continue
                f = getattr(tool, "fn", tool)
                try:
                    sig = inspect.signature(f)
                except (TypeError, ValueError):
                    continue
                params = sig.parameters
                var_kw = any(p.kind == p.VAR_KEYWORD for p in params.values())
                given = {k.arg for k in n.keywords if k.arg}
                for k in n.keywords:
                    if k.arg is None:
                        continue
                    if k.arg not in params and not var_kw:
                        tool_bad.append(f"{where}: {name}({k.arg}=…): no such parameter (has {', '.join(params)})")
                    elif k.arg in params and isinstance(k.value, ast.Constant):
                        ann = params[k.arg].annotation
                        if ann is not inspect.Parameter.empty and not literal_type_ok(ann, k.value.value):
                            tool_bad.append(f"{where}: {name}({k.arg}={k.value.value!r}): annotated {ann}")
                need = [p for p, v in params.items() if v.default is inspect.Parameter.empty
                        and v.kind in (v.POSITIONAL_OR_KEYWORD, v.KEYWORD_ONLY) and p not in given]
                if need and not any(k.arg is None for k in n.keywords):
                    tool_bad.append(f"{where}: {name}(): required {need} not given")
                tool_ok.add(name)
            # RPC ops
            if isinstance(n.func, ast.Attribute) and n.func.attr in RPC_METHODS and n.args:
                op = const_str(n.args[0], F.at(n.lineno))
                if op is None:
                    continue
                ctx = kw_const(n, "context") or RPC_METHODS[n.func.attr]
                have = lua["ops"].get(op)
                if not have:
                    rpc_bad.append(f"{where}: {n.func.attr}({op!r}): no such op in the mod")
                elif ctx not in have:
                    rpc_bad.append(f"{where}: {n.func.attr}({op!r}, context={ctx!r}): registered only in {sorted(have)}")
                else:
                    rpc_ok.add(op)
            # Lua snippets
            code_node, ctx = None, None
            if isinstance(n.func, ast.Name) and n.func.id == "lua_server" and n.args:
                code_node, ctx = n.args[0], "server"
            elif isinstance(n.func, ast.Attribute) and n.func.attr == "eval" and n.args:
                code_node, ctx = n.args[0], kw_const(n, "context") or "client"
            if code_node is not None:
                dyn: list[str] = []
                here = F.at(n.lineno)
                code = const_str(code_node, here, dyn)
                if code is None and isinstance(code_node, ast.Name) and code_node.id not in here:
                    continue  # a parameter passed through (def lua_server(code), lambda code, ctx: ...)
                if code is None:
                    names = [x.id for x in ast.walk(code_node) if isinstance(x, ast.Name)]
                    bad = [x for x in names if x in here and here[x] is None]
                    lua_bad.append(f"{where} line {n.lineno}: Lua code built from non-strings {bad}" if bad else
                                   f"{where} line {n.lineno}: Lua code not resolvable offline ({ast.unparse(code_node)[:60]})")
                    continue
                label = f"{where}:{n.lineno} ({ctx})"
                snippets.append((label, code))
                if "Osi." in code and ctx == "client":
                    lua_bad.append(f"{label}: Osiris call in the client context (eval defaults to client)")
                refs = lua_refs(code)
                nrefs += len(refs)
                for mod, field in refs:
                    if mod == "":
                        if field not in lua["root"] and field not in lua["modules"]:
                            lua_bad.append(f"{label}: Mods.BG3AI.BG3AI.{field} is not defined by the mod")
                        continue
                    m = lua["modules"].get(mod)
                    if m is None:
                        lua_bad.append(f"{label}: no BG3AI.{mod} module")
                    elif ctx not in m["ctx"]:
                        lua_bad.append(f"{label}: BG3AI.{mod} lives in {sorted(m['ctx'])}, not {ctx}")
                    elif field and field not in m["defs"]:
                        (lua_warn if field in m["refs"] else lua_bad).append(
                            f"{label}: {mod}.{field} " + ("used by the mod but no definition found"
                                                         if field in m["refs"] else "is not defined by the mod"))
                for x in re.findall(r"Ext\._Internal\.(\w+)", code):
                    if x not in internals:
                        lua_bad.append(f"{label}: Ext._Internal.{x} not in bg3le's source or patches")
            # calls of S.f(...) / module.f(...): the arguments bind to f's signature (count, keyword names)
            ch = attr_chain(n.func) if isinstance(n.func, ast.Attribute) else None
            if ch and (ch[0] == "S" or ch[0] in imported) and not any(isinstance(a, ast.Starred) for a in n.args) \
                    and all(k.arg for k in n.keywords):
                obj = S if ch[0] == "S" else imported[ch[0]]
                for part in ch[1:]:
                    obj = getattr(obj, part, None)
                    if obj is None:
                        break
                if obj is not None and callable(obj) and not inspect.isclass(obj):
                    target = getattr(obj, "fn", obj)
                    try:
                        sig = inspect.signature(target)
                    except (TypeError, ValueError):
                        sig = None
                    if sig is not None:
                        try:
                            sig.bind(*[None] * len(n.args), **{k.arg: None for k in n.keywords})
                        except TypeError as e:
                            attr_bad.append(f"{where} line {n.lineno}: {'.'.join(ch)}(...): {e}")
                        else:
                            ncalls += 1
        for n in ast.walk(fn):
            if not isinstance(n, ast.Attribute) or not isinstance(n.ctx, ast.Load):
                continue
            ch = attr_chain(n)
            if not ch or len(ch) < 2:
                continue
            head = ch[0]
            if head == "S":
                obj = S
            elif head in imported:
                obj = imported[head]
            else:
                continue
            path = head
            for part in ch[1:]:
                if not hasattr(obj, part):
                    if part not in hasattr_guarded:
                        attr_bad.append(f"{where}: {path}.{part}")
                    break
                obj = getattr(obj, part)
                path += "." + part
                if not inspect.ismodule(obj) and part != "game" and part != "H" and part != "render":
                    break  # stop at values (functions, dicts, objects built at run time)
    # op-like strings outside direct calls (a tuple of ops in a loop)
    loose = sorted({c.value for c in ast.walk(tree) if isinstance(c, ast.Constant) and isinstance(c.value, str)
                    and OP_LIKE.match(c.value) and c.value not in rpc_ok})
    for op in loose:
        if op not in lua["ops"]:
            rpc_bad.append(f"op-like string {op!r}: no such op in the mod")
    attr_bad = sorted(set(attr_bad))
    add("FAIL" if tool_bad else "PASS", "bridge tools + keywords",
        "; ".join(tool_bad) or f"{len(tool_ok)} tools, every keyword a parameter: {', '.join(sorted(tool_ok))}")
    add("FAIL" if attr_bad else "PASS", "Python attributes (S.*, imported modules)",
        "; ".join(attr_bad) or f"every S.x / module.x the sections read exists; {ncalls} calls bind to their signatures")
    add("FAIL" if rpc_bad else "PASS", "RPC ops + context",
        "; ".join(rpc_bad) or f"{len(rpc_ok)} ops, each registered in the context called: {', '.join(sorted(rpc_ok))}")
    add("FAIL" if lua_bad else ("WARN" if lua_warn else "PASS"), "Lua snippets: mod fields + context",
        "; ".join(lua_bad + lua_warn) or f"{len(snippets)} snippets, {nrefs} BG3AI module/field uses, all defined in their context")
    if run_luac:
        st, errs = luac_check(snippets)
        if st != "ok":
            add("SKIP", "Lua snippets compile (luac 5.4)", st)
        else:
            add("FAIL" if errs else "PASS", "Lua snippets compile (luac 5.4)",
                "; ".join(errs) or f"{len(snippets)} snippets compile")
    return res


# ------------------------------------------------------------------------------------------------- saves

def save_info(folder: Path) -> dict | None:
    """SaveInfo.json from a save's .lsv, read in memory (nothing extracted). None when lz4/zstandard are missing."""
    sys.path.insert(0, str(REPO / "tools"))
    try:
        import pak  # needs lz4 (+ zstandard for some entries)
    except ImportError:
        return None
    from bg3ai import saves as SV
    p = pak.Package(SV.save_file(folder))
    e = p.find("SaveInfo.json")
    return json.loads(p.read(e[0]).decode("utf-8", "replace")) if e else {}


def check_saves(S, needs: dict[str, dict]) -> list[tuple[str, str, str]]:
    """Each save label resolved the way load_game resolves it, and its SaveInfo checked against needs[label]."""
    res = []
    mine = S._my_saves()
    for label, need in needs.items():
        hits = [d for d in mine if d.name == label or d.name.endswith("__" + label)]
        if not hits:
            res.append(("FAIL", f"save {label}", f"not among this playthrough's {len(mine)} save folders in "
                        f"{S.SAVE_DIR} (prefix {S.SAVE_PREFIX!r}); used by {need.get('used_by')}"))
            continue
        d = hits[0]  # load_game takes the newest match (mine is newest first)
        dup = f"; {len(hits)} folders end in __{label}, load_game takes the newest" if len(hits) > 1 else ""
        try:
            info = save_info(d)
        except Exception as e:  # noqa: BLE001 - a bad .lsv is a result
            res.append(("FAIL", f"save {label}", f"{d.name}: SaveInfo unreadable ({e})"))
            continue
        if info is None:
            res.append(("WARN" if dup else "PASS", f"save {label}",
                        f"{d.name}{dup} (SaveInfo not read: run with --with lz4 --with zstandard)"))
            continue
        chars = (info.get("Active Party") or {}).get("Characters") or []
        names = ["Claude" if c.get("Origin") == "Generic" else str(c.get("Origin")) for c in chars]
        lead = (chars[0].get("Position") if chars else None) or [0, 0, 0]
        bad = []
        if need.get("level") and info.get("Current Level") != need["level"]:
            bad.append(f"level {info.get('Current Level')} (want {need['level']})")
        for who in need.get("party_has", []):
            if who not in names:
                bad.append(f"{who} not in the party")
        for who in need.get("party_not", []):
            if who in names:
                bad.append(f"{who} in the party (wanted in camp)")
        if len(chars) < need.get("party_min", 0):
            bad.append(f"party of {len(chars)} (want {need['party_min']}+)")
        if need.get("near"):
            spot, r = need["near"]
            dist = math.dist(lead, spot)
            if dist > r:
                bad.append(f"lead at {[round(x, 1) for x in lead]}, {dist:.0f} m from {spot} (want <= {r} m)")
        ev = (f"{d.name}: {info.get('Current Level')} {chars[0].get('Subregion') if chars else ''} party={names} "
              f"lead=({', '.join(f'{x:.1f}' for x in lead)}){dup}; used by {need.get('used_by')}")
        res.append(("FAIL" if bad else ("WARN" if dup and not need.get("near") else "PASS"), f"save {label}",
                    ("; ".join(bad) + " | " if bad else "") + ev))
    return res


def main_check(acc_module) -> int:
    """acceptance.py --check: prints the table, returns the number of FAILs."""
    import os
    sys.path.insert(0, str(REPO / "bridge" / "src"))
    os.environ.setdefault("BG3AI_PLAYTHROUGH", "claude-run-2")
    os.environ.setdefault("BG3AI_SAVE_PREFIX", "Claude-")
    scratch = Path(tempfile.mkdtemp(prefix="acc-check-"))
    os.environ["BG3AI_RUNTIME"] = str(scratch / "rt")
    os.environ["BG3AI_MEMORY_DIR"] = str(scratch / "memory")
    from server_view import S  # noqa: E402 - no game needed to import
    rows = check(Path(acc_module.__file__), S, acc_module)
    rows += check_saves(S, getattr(acc_module, "SAVE_NEEDS", {}))
    shutil.rmtree(scratch, ignore_errors=True)
    w = max(len(r[1]) for r in rows)
    for v, what, ev in rows:
        print(f"{v:4}  {what:<{w}}  {ev}")
    fails = sum(r[0] == "FAIL" for r in rows)
    print(f"\n--check: {len(rows)} checks, {fails} FAIL, {sum(r[0] == 'WARN' for r in rows)} WARN")
    return fails
