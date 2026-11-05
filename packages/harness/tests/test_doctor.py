# pyright: basic
"""bg3harness.doctor on test_preflight's fixture machine (Steam libraries, manifests and a repo in a temp dir) plus fake
layers, shim, X socket, Xwayland and stubbed commands (ffmpeg, uv, node): nothing real is read or run."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import NamedTuple
from unittest import mock

from bg3harness import bg3le_prebuilt, clients, doctor
from bg3harness.preflight import FAIL, PASS, WARN
from test_preflight import Fixture


class Usage(NamedTuple):
    total: int
    used: int
    free: int


ENCODERS = " V....D libx264              libx264 H.264\n V....D h264_nvenc           NVIDIA NVENC H.264 encoder\n"


class DoctorFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.have |= {"ffmpeg", "uv", "node"}
        self.cmds.update({"uv": (0, "uv 0.12.6"), "node": (0, "v24.20.0")})
        self.encoders = (0, ENCODERS)
        self.encode: dict[str, tuple[int, str]] = {"h264_nvenc": (0, ""), "libx264": (0, "")}
        self.python = (0, "/home/someone/.local/share/uv/python/cpython-3.14.0/bin/python3.14")
        self.free_gb = 100.0
        self.layers = self.tmp / "layers"
        self.layers.mkdir()
        (self.layers / doctor.LAYER).write_text("{}")
        shim = self.repo / "build/bg3sdl/libbg3sdl.so"
        shim.parent.mkdir(parents=True)
        shim.write_bytes(b"\x7fELF")
        (self.repo / "tools/pi/node_modules/.bin").mkdir(parents=True)
        (self.repo / "tools/pi/node_modules/.bin/pi").write_text("")
        self.x11 = self.tmp / "x11"
        self.x11.mkdir()
        (self.x11 / "X1").write_text("")
        self.xauth = self.tmp / "xauth_ABC"
        self.xauth.write_text("")
        self.ps = f"1234 /usr/bin/Xwayland :1 -rootless -auth {self.xauth} -listenfd 55"
        self.build_bg3le()
        self.deploy_mod()
        self.login(offline_log=True)

    def dctx(self, **env: str) -> doctor.Ctx:
        base = self.ctx(**env)

        def run(cmd: list[str]) -> tuple[int, str]:
            name = Path(cmd[0]).name
            if name == "ffmpeg":
                return self.encoders if "-encoders" in cmd else self.encode[cmd[cmd.index("-c:v") + 1]]
            if name == "uv" and cmd[1:2] == ["python"]:
                return self.python
            return base.run(cmd)  # type: ignore[operator]

        return doctor.Ctx(
            home=base.home,
            env=base.env,
            repo=base.repo,
            steam=base.steam,
            bg3le=base.bg3le,
            run=run,
            which=base.which,
            meminfo=base.meminfo,
            disk_usage=lambda p: Usage(0, 0, int(self.free_gb * 1e9)),
            xwayland=lambda: self.ps,
            x11_dir=str(self.x11),
            layer_dirs=(self.layers,),
        )

    def checks(self, **env: str) -> dict[str, doctor.Check]:
        return {c.name: c for c in doctor.doctor(self.dctx(**env))}


class TestDoctor(DoctorFixture):
    def test_a_ready_machine(self):
        checks = doctor.doctor(self.dctx())
        got = {c.name: c.status for c in checks}
        for name in ("install", "build", "bg3le", "screenshot", "shim", "display", "ffmpeg", "record", "disk",
                     "uv", "python", "node", "pi"):  # fmt: skip
            self.assertEqual(got.get(name), PASS, (name, [c for c in checks if c.name == name]))
        self.assertTrue(doctor.report(checks)["ready"])

    def test_every_warn_and_fail_says_what_to_do(self):
        self.have = {"nvidia-smi"}  # no claude, docker, ffmpeg, uv, node
        (self.layers / doctor.LAYER).unlink()
        (self.repo / "build/bg3sdl/libbg3sdl.so").unlink()
        self.install(build="1")
        self.ps = ""
        checks = doctor.doctor(self.dctx(DISPLAY=":7"))
        bad = [c for c in checks if c.status != PASS]
        self.assertGreater(len(bad), 8)
        self.assertEqual([c.name for c in bad if not c.fix], [])
        self.assertEqual([c.name for c in checks if c.status == PASS and c.fix], [])

    def test_screenshot_layer(self):
        (self.layers / doctor.LAYER).unlink()
        c = self.checks()["screenshot"]
        self.assertEqual(c.status, FAIL)
        self.assertIn("mesa-vulkan-drivers", c.fix)
        user = self.tmp / "data/vulkan/explicit_layer.d"
        user.mkdir(parents=True)
        (user / doctor.LAYER).write_text("{}")
        self.assertEqual(self.checks(XDG_DATA_HOME=str(self.tmp / "data"))["screenshot"].status, PASS)

    def test_shim(self):
        (self.repo / "build/bg3sdl/libbg3sdl.so").write_bytes(b"not an ELF")
        c = self.checks()["shim"]
        self.assertEqual(c.status, FAIL)
        self.assertIn("native/bg3sdl/build.sh after tools/build_bg3le.sh", c.fix)
        self.assertNotIn("bg3le_prebuilt.py fetch", c.fix)
        c = self.checks(BG3AI_BG3LE_PREBUILT_URL="https://example.invalid/p.tar.gz")["shim"]
        self.assertIn("bg3le_prebuilt.py fetch (the prebuilt carries the shim)", c.fix)

    def test_display_from_the_desktop_xwayland(self):
        c = self.checks()["display"]
        self.assertEqual(c.status, PASS, c.detail)
        self.assertIn(f"XAUTHORITY={self.xauth}", c.detail)

    def test_no_display(self):
        self.ps = ""
        c = self.checks(DISPLAY=":1", XAUTHORITY=str(self.tmp / "gone"))["display"]
        self.assertEqual(c.status, FAIL)
        self.assertIn("XAUTHORITY", c.detail)
        self.assertIn("pgrep -a Xwayland", c.fix)
        c = self.checks(DISPLAY=":9")["display"]
        self.assertIn("no X server socket", c.detail)

    def test_ffmpeg(self):
        self.have.discard("ffmpeg")
        self.assertEqual(self.checks()["ffmpeg"].status, FAIL)
        self.assertNotIn("record", self.checks())
        self.have.add("ffmpeg")
        self.encoders = (0, " V....D h264_nvenc   NVENC\n")
        self.assertEqual(self.checks()["ffmpeg"].status, FAIL)

    def setting(self) -> Path:
        return self.home / ".config/bg3ai/record-encoder"

    def test_the_recording_encoder_set_must_encode(self):
        self.encode["h264_nvenc"] = (1, "[h264_nvenc] Cannot load libcuda.so.1\nError while opening encoder")
        c = self.checks(**{doctor.RECORD_ENCODER_ENV: "h264_nvenc"})["record"]
        self.assertEqual(c.status, FAIL)
        self.assertIn("Error while opening encoder", c.detail)
        self.assertIn(doctor.RECORD_ENCODER_ENV, c.fix)
        self.assertEqual(self.checks(**{doctor.RECORD_ENCODER_ENV: "libx264"})["record"].status, PASS)
        self.assertFalse(self.setting().exists())  # the variable set: nothing written
        self.setting().parent.mkdir(parents=True)
        self.setting().write_text("h264_nvenc\n")
        c = self.checks()["record"]
        self.assertEqual(c.status, FAIL)
        self.assertIn(str(self.setting()), c.detail)
        self.assertIn(f"delete {self.setting()}", c.fix)
        self.encoders = (0, " V....D libx264   x264\n")  # no NVENC in this ffmpeg build
        self.assertIn("lists no h264_nvenc", self.checks()["record"].detail)

    def test_with_no_encoder_set_the_doctor_writes_the_one_that_encodes_once(self):
        self.encode["h264_nvenc"] = (1, "[h264_nvenc] Cannot load libcuda.so.1\nError while opening encoder")  # AMD
        c = self.checks()["record"]
        self.assertEqual(c.status, PASS)
        self.assertIn(f"wrote it to {self.setting()}", c.detail)
        self.assertEqual(self.setting().read_text(), "libx264\n")
        self.encode["h264_nvenc"] = (0, "")  # once written, the setting stays whatever encodes later
        self.assertEqual(self.checks()["record"].status, PASS)
        self.assertEqual(self.setting().read_text(), "libx264\n")
        self.setting().unlink()
        self.checks()
        self.assertEqual(self.setting().read_text(), "h264_nvenc\n")  # an NVIDIA machine: the GPU's encoder

    def test_with_no_encoder_that_encodes_nothing_is_written(self):
        self.encode.update({"h264_nvenc": (1, "no libcuda"), "libx264": (1, "broken x264")})
        c = self.checks()["record"]
        self.assertEqual(c.status, FAIL)
        self.assertIn("h264_nvenc: no libcuda; libx264: broken x264", c.detail)
        self.assertFalse(self.setting().exists())

    def test_the_encoder_variable_is_the_bench_hosts(self):
        from bg3harness.runner import run_host

        self.assertEqual(doctor.RECORD_ENCODER_ENV, run_host.RECORD_ENCODER_ENV)

    def test_disk(self):
        for free, want in ((100, PASS), (10, WARN), (2, FAIL)):
            self.free_gb = free
            self.assertEqual(self.checks()["disk"].status, want, free)

    def test_uv_and_python(self):
        self.python = (2, "error: No interpreter found for Python >=3.14")
        c = self.checks()["python"]
        self.assertEqual(c.status, WARN)
        self.assertIn("uv python install 3.14", c.fix)
        self.have.discard("uv")
        got = self.checks()
        self.assertEqual(got["uv"].status, FAIL)
        self.assertNotIn("python", got)

    def test_node_and_pi(self):
        for old in ("v18.19.0", "v22.12.0"):  # pi needs 22.19
            self.cmds["node"] = (0, old)
            self.assertEqual(self.checks()["node"].status, WARN, old)
        self.cmds["node"] = (0, "v22.19.0")
        self.assertEqual(self.checks()["node"].status, PASS)
        self.cmds["node"] = (0, "v24.20.0")
        (self.repo / "tools/pi/node_modules/.bin/pi").unlink()
        c = self.checks()["pi"]
        self.assertEqual((c.status, c.fix), (WARN, "cd tools/pi && npm ci --ignore-scripts"))
        self.have.discard("node")
        got = self.checks()
        self.assertEqual(got["node"].status, WARN)
        self.assertNotIn("pi", got)

    def test_wrong_game_build_points_at_the_depot_procedure(self):
        self.install(build="99999999")
        c = self.checks()["build"]
        self.assertEqual(c.status, FAIL)
        self.assertIn("download_depot", c.fix)
        self.assertIn("docs/GAME-UPDATES.md", c.fix)

    def test_bg3le_fix_offers_the_prebuilt_when_configured(self):
        self.build_bg3le(current=False)
        self.assertNotIn("bg3le_prebuilt.py fetch", self.checks()["bg3le"].fix)
        c = self.checks(BG3AI_BG3LE_PREBUILT_URL="https://example.invalid/p.tar.gz")["bg3le"]
        self.assertEqual(c.status, FAIL)
        self.assertIn("bg3le_prebuilt.py fetch", c.fix)

    def test_a_verified_prebuilt_says_so(self):
        dist = self.repo / "build/bg3le/dist"
        lib = (dist / "libbg3le.so").read_bytes()
        info = (dist / "BUILDINFO").read_text()
        (dist / "BUILDINFO").write_text(info + f"sha256: {hashlib.sha256(lib).hexdigest()}\n")
        (dist / "bg3lua").write_text("")
        path, sha = bg3le_prebuilt.pack(dist, self.repo / "patches/bg3le", self.tmp / "pkg")
        for name in ("libbg3le.so", "bg3lua", "BUILDINFO"):
            (dist / name).unlink()
        bg3le_prebuilt.fetch(path.as_uri(), sha, dist, self.repo / "patches/bg3le")
        c = self.checks()["bg3le"]
        self.assertEqual(c.status, PASS, c.detail)
        self.assertIn("a prebuilt, verified", c.detail)

    def test_a_crashing_check_is_a_fail_not_an_abort(self):
        with mock.patch.object(doctor, "check_display", side_effect=RuntimeError("boom")):
            checks = doctor.doctor(self.dctx())
        crashed = [c for c in checks if c.name == "display"]
        self.assertEqual(crashed[0].status, FAIL)
        self.assertIn("boom", crashed[0].detail)
        self.assertIn("pi", [c.name for c in checks])  # later checks still ran

    def test_main_json_and_exit(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = doctor.main(["--json"], self.dctx())
        rep = json.loads(out.getvalue())
        self.assertEqual((rc, rep["ready"], rep["fails"]), (0, True, 0))
        self.assertEqual(set(rep["checks"][0]), {"name", "status", "detail", "fix"})
        (self.repo / "build/bg3sdl/libbg3sdl.so").unlink()
        out = io.StringIO()
        with redirect_stdout(out):
            rc = doctor.main([], self.dctx())
        self.assertEqual(rc, 1)
        self.assertIn("fix: native/bg3sdl/build.sh", out.getvalue())
        self.assertIn("NOT READY: 1 FAIL", out.getvalue())

    def test_default_ctx_is_this_checkout(self):
        ctx = doctor.default_ctx()
        self.assertTrue((ctx.repo / "runner" / "launch_game.sh").is_file())
        self.assertEqual(ctx.env.get("HOME"), os.environ.get("HOME"))


# --claude-probe: a fake `claude` (a script) prints one stream case and records its argv and cwd; no API call.
FAKE_CLAUDE = """#!{python}
import json, os, sys, time
case = {case!r}
with open({log!r}, "w") as f:
    memory = os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY")
    json.dump({{"argv": sys.argv[1:], "cwd": os.getcwd(), "memory": memory}}, f)
sys.stderr.write("stderr of " + case + "\\n")
mcp = "failed" if case == "mcp" else "connected"
lines = [{{"type": "system", "subtype": "init", "session_id": "s", "model": "claude-haiku",
          "tools": ["ToolSearch", "mcp__bg3__session_clock"], "mcp_servers": [{{"name": "bg3", "status": mcp}}]}}]
call = {{"type": "assistant", "message": {{"id": "m1", "content": [
    {{"type": "tool_use", "id": "t1", "name": "mcp__bg3__session_clock", "input": {{}}}}]}}}}
denied = "Claude requested permissions to use mcp__bg3__session_clock, but you haven't granted it yet."
ok = {{"type": "result", "subtype": "success", "is_error": False, "result": "No session deadline set.",
      "total_cost_usd": 0.0012, "num_turns": 2, "permission_denials": []}}
if case == "login":
    sys.stderr.write("auth: no credentials\\n")
    lines.append({{"type": "result", "subtype": "success", "is_error": True,
                  "result": "Not logged in · Please run /login", "total_cost_usd": 0, "permission_denials": []}})
elif case == "crash":
    sys.stderr.write("Error: unknown option '--frobnicate'\\n")
    print("not json")
    sys.exit(1)
elif case == "timeout":
    sys.stderr.write("waiting for the MCP server\\n")
    sys.stderr.flush()
    time.sleep(30)
elif case == "silent":
    lines += [{{"type": "assistant", "message": {{"id": "m1", "content": [{{"type": "text", "text": "Sure."}}]}}}},
              ok]
else:
    lines.append(call)
    if case == "denied":
        lines.append({{"type": "user", "message": {{"content": [
            {{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": denied}}]}}}})
        lines.append({{**ok, "permission_denials": [{{"tool_name": "mcp__bg3__session_clock", "tool_use_id": "t1",
                                                     "tool_input": {{}}}}]}})
    elif case == "error":
        lines.append({{"type": "user", "message": {{"content": [
            {{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "bridge: boom"}}]}}}})
        lines.append(ok)
    else:
        text = [{{"type": "text", "text": "No session deadline set."}}]
        lines.append({{"type": "user", "message": {{"content": [
            {{"type": "tool_result", "tool_use_id": "t1", "content": text}}]}}}})
        lines.append(ok)
for ev in lines:
    print(json.dumps(ev), flush=True)
"""


class TestClaudeProbe(DoctorFixture):
    def setUp(self):
        super().setUp()
        play = self.repo / "play"
        (play / ".claude").mkdir(parents=True)
        real = Path(__file__).resolve().parents[3] / "play"
        for name in (".mcp.json", ".claude/settings.json"):
            (play / name).write_text((real / name).read_text())
        self.fake = self.tmp / "bin" / "claude"
        self.fake.parent.mkdir()
        self.log = self.tmp / "claude-args.json"

    def probe(self, case: str, timeout: float = 20) -> doctor.Check:
        self.fake.write_text(FAKE_CLAUDE.format(python=sys.executable, case=case, log=str(self.log)))
        self.fake.chmod(0o755)
        ctx = self.dctx()
        ctx.which = lambda n: str(self.fake) if n == "claude" else None
        return doctor.check_claude_probe(ctx, timeout=timeout)

    def test_pass_runs_the_real_play_command_in_play(self):
        c = self.probe("ok")
        self.assertEqual(c.status, PASS, c)
        self.assertIn("session_clock: 'No session deadline set.' ($0.0012", c.detail)
        got = json.loads(self.log.read_text())
        self.assertEqual(Path(got["cwd"]).resolve(), (self.repo / "play").resolve())
        self.assertEqual(got["memory"], "1")  # ClaudeClient.env
        argv = got["argv"]
        want = clients.ClaudeClient(mcp_config=self.repo / "play/.mcp.json", binary=str(self.fake)).command(
            clients.Request(prompt=doctor.PROBE_PROMPT, model="haiku", session_dir=self.repo, session="x")
        )[1:]
        self.assertEqual(argv[: argv.index("--session-id")], want[: want.index("--session-id")])
        self.assertIn("mcp__bg3__*", json.loads(argv[argv.index("--settings") + 1])["permissions"]["allow"])

    def test_each_failing_stream_is_a_fail_with_claudes_stderr(self):
        cases = {
            "denied": ("Claude Code denied mcp__bg3__session_clock", "must allow mcp__bg3__*"),
            "error": ("a tool call failed: bridge: boom", "see the error"),
            "mcp": ("the bg3 MCP server didn't connect (init: ['bg3:failed'])", "uv sync in bridge/"),
            "login": ("claude isn't logged in: Not logged in", "/login"),
            "crash": ("claude exited 1 without its init event", "/login"),
            "silent": ("haiku never called mcp__bg3__session_clock (calls 0, results 0): No session", "rerun"),
        }
        for case, (detail, fix) in cases.items():
            with self.subTest(case):
                c = self.probe(case)
                self.assertEqual(c.status, FAIL, c)
                self.assertIn(detail, c.detail)
                self.assertIn(fix, c.fix)
                self.assertIn(f"(claude's stderr: stderr of {case}", c.fix)
        self.assertIn("unknown option '--frobnicate'", self.probe("crash").fix)

    def test_timeout_kills_it_and_fails(self):
        c = self.probe("timeout", timeout=1)
        self.assertEqual(c.status, FAIL, c)
        self.assertIn("didn't finish in 1 s", c.detail)
        self.assertIn("waiting for the MCP server", c.fix)

    def test_no_claude(self):
        ctx = self.dctx()
        ctx.which = lambda n: None
        c = doctor.check_claude_probe(ctx)
        self.assertEqual((c.status, c.detail), (FAIL, "no claude on PATH"))
        self.assertIn("log in", c.fix)

    def test_only_with_the_flag(self):
        self.fake.write_text(FAKE_CLAUDE.format(python=sys.executable, case="denied", log=str(self.log)))
        self.fake.chmod(0o755)
        ctx = self.dctx()
        ctx.which = lambda n: str(self.fake) if n == "claude" else f"/usr/bin/{n}" if n in self.have else None
        with redirect_stdout(io.StringIO()):
            self.assertEqual(doctor.main([], ctx), 0)
        self.assertFalse(self.log.exists())  # no claude run, no spend
        self.assertNotIn(doctor.PROBE, [c.name for c in doctor.doctor(ctx)])
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(doctor.main(["--claude-probe"], ctx), 1)
        self.assertIn("FAIL  claude-probe", out.getvalue())
        self.assertIn("spends about $0.005", self.help())

    def help(self) -> str:
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            doctor.main(["--help"])
        return " ".join(out.getvalue().split())
