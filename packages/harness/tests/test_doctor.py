# pyright: basic
"""bg3harness.doctor on test_preflight's fixture machine (Steam libraries, manifests and a repo in a temp dir) plus fake
layers, shim, X socket, Xwayland and stubbed commands (ffmpeg, uv, node): nothing real is read or run."""

from __future__ import annotations

import hashlib
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
from typing import NamedTuple
from unittest import mock

from bg3harness import bg3le_prebuilt, doctor
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

    def test_the_recording_encoder_must_encode(self):
        self.encode["h264_nvenc"] = (1, "[h264_nvenc] Cannot load libcuda.so.1\nError while opening encoder")
        c = self.checks()["record"]
        self.assertEqual(c.status, FAIL)
        self.assertIn("Error while opening encoder", c.detail)
        self.assertIn(f"{doctor.RECORD_ENCODER_ENV}=libx264", c.fix)
        self.assertEqual(self.checks(**{doctor.RECORD_ENCODER_ENV: "libx264"})["record"].status, PASS)
        self.encoders = (0, " V....D libx264   x264\n")  # no NVENC in this ffmpeg build
        self.assertIn("lists no h264_nvenc", self.checks()["record"].detail)

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
