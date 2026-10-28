"""Offline tests for bg3harness.preflight (CLI: tools/bench_preflight.py): fixture Steam libraries, manifests,
loginusers.vdf and repo trees in a temp dir; the commands (pgrep, claude, docker, nvidia-smi) stubbed. Nothing real
is read or run. The checks run with the harness's real tools/steam_paths.py and tools/bg3le_buildinfo.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bg3harness import preflight as P

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tools"))  # the harness's helpers, not packaged yet
import bg3le_buildinfo
import steam_paths

ACF = """"AppState"
{{
\t"appid"\t\t"1086940"
\t"StateFlags"\t\t"{flags}"
\t"installdir"\t\t"Baldurs Gate 3"
\t"buildid"\t\t"{build}"
\t"TargetBuildID"\t\t"{target}"
\t"AutoUpdateBehavior"\t\t"{behavior}"
}}
"""

LOGINUSERS = """"users"
{{
\t"76561190000000001"
\t{{
\t\t"AccountName"\t\t"someone"
\t\t"MostRecent"\t\t"1"
\t\t"WantsOfflineMode"\t\t"{offline}"
\t}}
}}
"""

META = '<save><node id="ModuleInfo"><attribute id="Version64" type="int64" value="36028797018963968"/></node></save>'


class Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.repo = self.tmp / "repo"
        self.steam = self.home / ".local/share/Steam"
        self.lib = self.tmp / "SteamLibrary"
        # steam_paths reads these from os.environ; the real machine's must not leak into the fixture
        env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        for k in ("STEAM_ROOT", "BG3AI_STEAM_LOGIN_LOG"):
            os.environ.pop(k, None)
        self.addCleanup(env.stop)
        self.addCleanup(self._tmp.cleanup)
        self.cmds: dict[str, tuple[int, str]] = {
            "pgrep": (0, "1234"),
            "claude": (0, "2.1.301 (Claude Code)"),
            "docker": (0, "29.7.2"),
            "nvidia-smi": (0, "NVIDIA GeForce RTX 3050 Laptop GPU, 4096, 3888"),
        }
        self.have = {"claude", "docker", "nvidia-smi"}
        self.write_meminfo(16, 12)
        self.install()

    # ---- fixture builders
    def install(self, build="25605617", flags=4, target=None, behavior="0", native=True, sniper=True):
        (self.steam / "steamapps").mkdir(parents=True, exist_ok=True)
        (self.steam / "config").mkdir(parents=True, exist_ok=True)
        (self.steam / "config/libraryfolders.vdf").write_text(
            f'"libraryfolders"\n{{\n "0"\n {{\n  "path" "{self.lib}"\n  "apps"\n  {{\n   "1086940" "1"\n  }}\n }}\n}}\n'
        )
        apps = self.lib / "steamapps"
        game = apps / "common/Baldurs Gate 3"
        (game / "bin").mkdir(parents=True, exist_ok=True)
        (apps / "appmanifest_1086940.acf").write_text(
            ACF.format(flags=flags, build=build, target=build if target is None else target, behavior=behavior)
        )
        (game / "bin/bg3").write_bytes(b"\x7fELF\x02\x01\x01" if native else b"MZ\x90\x00")
        if sniper:
            (apps / "common/SteamLinuxRuntime_sniper").mkdir(parents=True, exist_ok=True)
            (apps / "common/SteamLinuxRuntime_sniper/run").write_text("#!/bin/sh\n")
        return game

    def login(self, offline_log: bool, wants: str | None = "1"):
        (self.steam / "logs").mkdir(parents=True, exist_ok=True)
        line = "[2026-10-16 10:00:00] Start offline - 1" if offline_log else "[2026-10-16 10:00:00] Starting login"
        (self.steam / "logs/steamui_login.txt").write_text(line + "\n")
        if wants is not None:
            (self.steam / "config/loginusers.vdf").write_text(LOGINUSERS.format(offline=wants))

    def write_meminfo(self, total_gib, avail_gib):
        self.meminfo = self.tmp / "meminfo"
        self.meminfo.write_text(f"MemTotal: {total_gib * 2**20} kB\nMemAvailable: {avail_gib * 2**20} kB\n")

    def build_bg3le(self, current=True):
        patches = self.repo / "patches/bg3le"
        patches.mkdir(parents=True, exist_ok=True)
        (patches / "a.patch").write_bytes(b"diff a")
        dist = self.repo / "build/bg3le/dist"
        dist.mkdir(parents=True, exist_ok=True)
        (dist / "libbg3le.so").write_bytes(b"\x7fELF")
        h = hashlib.sha256(b"diff a" if current else b"old").hexdigest()[:16]
        (dist / "BUILDINFO").write_text(f"built: 2026-10-16\npatch: a.patch {h}\n")

    def deploy_mod(self):
        mod = self.repo / "mod/Mods/BG3AI"
        (mod / "ScriptExtender").mkdir(parents=True, exist_ok=True)
        (mod / "meta.lsx").write_text(META)

    def ctx(self, **env) -> P.Ctx:
        def run(cmd):
            return self.cmds.get(Path(cmd[0]).name, (127, "not found"))

        return P.Ctx(
            home=self.home,
            env=env,
            repo=self.repo,
            steam=steam_paths,
            bg3le=bg3le_buildinfo,
            run=run,
            meminfo=self.meminfo,
            which=lambda n: f"/usr/bin/{n}" if n in self.have else None,
        )

    def statuses(self, lines, name=None):
        return [s for s, n, _ in lines if name is None or n == name]


class TestInstall(Fixture):
    def test_installed(self):
        self.assertEqual(self.statuses(P.check_install(self.ctx())), [P.PASS])

    def test_no_manifest_fails(self):
        (self.lib / "steamapps/appmanifest_1086940.acf").unlink()
        ((s, _, d),) = P.check_install(self.ctx())
        self.assertEqual(s, P.FAIL)
        self.assertIn("isn't installed through Steam", d)

    def test_still_downloading_fails(self):
        self.install(flags=4 | 256)
        self.assertEqual(self.statuses(P.check_install(self.ctx())), [P.FAIL])
        self.install(flags=1026 & ~4)
        self.assertEqual(self.statuses(P.check_install(self.ctx())), [P.FAIL])

    def test_pending_update_warns(self):
        self.install(target="26000000")
        ((s, _, d),) = P.check_install(self.ctx())
        self.assertEqual(s, P.WARN)
        self.assertIn("TargetBuildID 26000000", d)
        self.install(flags=4 | 2)
        self.assertEqual(self.statuses(P.check_install(self.ctx())), [P.WARN])


class TestNative(Fixture):
    def test_elf_and_sniper(self):
        self.assertEqual(self.statuses(P.check_native(self.ctx())), [P.PASS, P.PASS])

    def test_not_elf_fails(self):
        self.install(native=False)
        self.assertEqual(self.statuses(P.check_native(self.ctx()), "native"), [P.FAIL])

    def test_no_sniper_fails(self):
        (self.lib / "steamapps/common/SteamLinuxRuntime_sniper/run").unlink()
        self.assertEqual(self.statuses(P.check_native(self.ctx()), "sniper"), [P.FAIL])

    def test_proton_install_named(self):
        game = self.install()
        (game / "bin/bg3").unlink()
        (game / "bin/bg3_dx11.exe").write_bytes(b"MZ")
        (s, _, d), _ = P.check_native(self.ctx())
        self.assertEqual(s, P.FAIL)
        self.assertIn("bg3_dx11.exe", d)

    def test_bg3_dir_env_is_followed(self):
        other = self.tmp / "elsewhere/Baldurs Gate 3"
        (other / "bin").mkdir(parents=True)
        (s, _, d), _ = P.check_native(self.ctx(BG3_DIR=str(other)))
        self.assertEqual(s, P.FAIL)
        self.assertIn(str(other), d)


class TestBuild(Fixture):
    def test_pinned_from_first_launch(self):
        (self.repo / "tools/dev").mkdir(parents=True)
        (self.repo / "tools/dev/first_launch.sh").write_text("#!/bin/bash\nWANT_BUILD=111\n")
        self.assertEqual(P.pinned_build(self.repo), "111")
        self.assertEqual(self.statuses(P.check_build(self.ctx())), [P.FAIL])
        self.install(build="111")
        self.assertEqual(self.statuses(P.check_build(self.ctx())), [P.PASS])

    def test_fallback_pin(self):
        self.assertEqual(P.pinned_build(self.repo), P.PINNED_BUILD)
        self.assertEqual(self.statuses(P.check_build(self.ctx())), [P.PASS])

    def test_real_first_launch_matches_fallback(self):
        self.assertEqual(P.pinned_build(REPO), P.PINNED_BUILD)

    def test_no_buildid_fails(self):
        self.install(build="")
        self.assertEqual(P.check_build(self.ctx()), [(P.FAIL, "build", "no buildid (no manifest)")])

    def series(self, pins):
        d = self.repo / "runtime/sessions/s1"
        d.mkdir(parents=True)
        (d / "series.json").write_text(json.dumps({"series": "s1", **({"pins": pins} if pins else {})}))

    def test_series_pins(self):
        self.series({"game_build": "25605617"})
        self.assertEqual(self.statuses(P.check_build(self.ctx(), "s1")), [P.PASS, P.PASS])

    def test_series_drift_fails(self):
        self.series({"game_build": "1"})
        lines = P.check_build(self.ctx(), "s1")
        self.assertEqual(self.statuses(lines), [P.PASS, P.FAIL])
        self.assertIn("frozen on 1", lines[1][2])

    def test_series_without_pins_or_missing_warns(self):
        self.series(None)
        self.assertEqual(self.statuses(P.check_build(self.ctx(), "s1")), [P.PASS, P.WARN])
        self.assertEqual(self.statuses(P.check_build(self.ctx(), "nope")), [P.PASS, P.WARN])


class TestUpdates(Fixture):
    def test_offline_passes_and_reports_setting(self):
        self.login(offline_log=True, wants="1")
        ((s, _, d),) = P.check_updates(self.ctx())
        self.assertEqual(s, P.PASS)
        self.assertIn("AutoUpdateBehavior 0 (always keep", d)
        self.assertIn("WantsOfflineMode 1", d)

    def test_online_warns_for_every_behavior(self):
        self.login(offline_log=False, wants="0")
        for b, word in (("0", "between"), ("1", "launched through Steam"), ("2", "between")):
            self.install(behavior=b)
            ((s, _, d),) = P.check_updates(self.ctx())
            self.assertEqual(s, P.WARN, b)
            self.assertIn(word, d)
            self.assertIn(P.UPDATE_BEHAVIOR[b], d)

    def test_no_logs_is_not_offline(self):
        self.assertEqual(self.statuses(P.check_updates(self.ctx())), [P.WARN])

    def test_wants_offline_most_recent_user(self):
        self.login(offline_log=True, wants="0")
        self.assertFalse(P.wants_offline(self.ctx()))
        (self.steam / "config/loginusers.vdf").write_text(
            '"users"\n{\n "1"\n {\n  "WantsOfflineMode" "1"\n }\n "2"\n {\n  "WantsOfflineMode" "0"\n }\n}\n'
        )
        self.assertIsNone(P.wants_offline(self.ctx()))  # two users, none marked MostRecent
        (self.steam / "config/loginusers.vdf").unlink()
        self.assertIsNone(P.wants_offline(self.ctx()))

    def test_unknown_behavior_value(self):
        self.install(behavior="7")
        self.login(offline_log=True)
        self.assertIn("unknown value", P.check_updates(self.ctx())[0][2])

    def test_never_writes(self):
        self.login(offline_log=False)
        self.build_bg3le()
        self.deploy_mod()
        before = {p: p.stat().st_mtime_ns for p in self.tmp.rglob("*")}
        P.preflight(self.ctx())
        self.assertEqual({p: p.stat().st_mtime_ns for p in self.tmp.rglob("*")}, before)


class TestRepoChecks(Fixture):
    def test_bg3le(self):
        self.assertEqual(self.statuses(P.check_bg3le(self.ctx())), [P.FAIL])  # not built
        self.build_bg3le(current=False)
        ((s, _, d),) = P.check_bg3le(self.ctx())
        self.assertEqual(s, P.FAIL)
        self.assertIn("changed a.patch", d)
        self.build_bg3le(current=True)
        self.assertEqual(self.statuses(P.check_bg3le(self.ctx())), [P.PASS])

    def test_mod(self):
        self.assertEqual(self.statuses(P.check_mod(self.ctx())), [P.FAIL])
        self.deploy_mod()
        ((s, _, d),) = P.check_mod(self.ctx())
        self.assertEqual(s, P.PASS)
        self.assertIn("version 1.0.0.0", d)

    def test_mod_version_unknown_or_unreadable(self):
        self.deploy_mod()
        meta = self.repo / "mod/Mods/BG3AI/meta.lsx"
        meta.write_text("<save/>")  # no Version64
        ((s, _, d),) = P.check_mod(self.ctx())
        self.assertEqual((s, "version ?" in d), (P.PASS, True))
        if os.geteuid():  # root reads a mode-000 file
            meta.chmod(0)
            self.addCleanup(meta.chmod, 0o644)
            ((s, _, d),) = P.check_mod(self.ctx())
            self.assertEqual((s, "version ?" in d), (P.PASS, True))

    def test_mod_path_env(self):
        self.deploy_mod()
        self.assertEqual(self.statuses(P.check_mod(self.ctx(BG3AI_MOD_PATH=str(self.tmp / "x")))), [P.FAIL])
        self.assertEqual(self.statuses(P.check_mod(self.ctx(BG3AI_MOD_PATH=f"{self.tmp}/x:{self.repo}/mod"))), [P.PASS])


class TestTools(Fixture):
    def test_claude(self):
        self.assertEqual(self.statuses(P.check_claude(self.ctx())), [P.PASS])
        self.cmds["claude"] = (1, "boom")
        self.assertEqual(self.statuses(P.check_claude(self.ctx())), [P.FAIL])
        self.have.discard("claude")
        self.assertEqual(self.statuses(P.check_claude(self.ctx())), [P.FAIL])

    def test_docker_never_fails(self):
        self.assertEqual(self.statuses(P.check_docker(self.ctx())), [P.PASS])
        self.cmds["docker"] = (1, "permission denied")
        self.assertEqual(self.statuses(P.check_docker(self.ctx())), [P.WARN])
        self.have.discard("docker")
        self.assertEqual(self.statuses(P.check_docker(self.ctx())), [P.WARN])

    def test_steam_running(self):
        self.assertEqual(self.statuses(P.check_steam_running(self.ctx())), [P.PASS])
        self.cmds["pgrep"] = (1, "")
        self.assertEqual(self.statuses(P.check_steam_running(self.ctx())), [P.WARN])

    def test_run_returns_the_output_or_127(self):
        self.assertEqual(P._run([sys.executable, "-c", "print(' out ')"]), (0, "out"))
        self.assertEqual(P._run([sys.executable, "-c", "import sys; sys.exit('err')"]), (1, "err"))
        rc, out = P._run([str(self.tmp / "no-such-command")])
        self.assertEqual(rc, 127)
        self.assertIn("no-such-command", out)


class TestMemory(Fixture):
    def test_parse_size(self):
        self.assertEqual(P.parse_size("10G"), 10 * 2**30)
        self.assertEqual(P.parse_size("512m"), 512 * 2**20)
        self.assertEqual(P.parse_size("1024"), 1024)
        self.assertIsNone(P.parse_size("infinity"))

    def test_ram(self):
        self.assertEqual(self.statuses(P.check_ram(self.ctx())), [P.PASS])
        self.write_meminfo(16, 8)
        self.assertEqual(self.statuses(P.check_ram(self.ctx())), [P.WARN])
        self.write_meminfo(8, 6)
        self.assertEqual(self.statuses(P.check_ram(self.ctx())), [P.FAIL])
        self.assertEqual(self.statuses(P.check_ram(self.ctx(BG3AI_GAME_MEMORY_MAX="6G"))), [P.PASS])

    def test_ram_unknown_warns(self):
        self.meminfo.write_text("MemTotal: lots\nHugePages_Total: 0\n")  # no kB values
        self.assertEqual(self.statuses(P.check_ram(self.ctx())), [P.WARN])
        self.meminfo.unlink()
        ((s, _, d),) = P.check_ram(self.ctx())
        self.assertEqual((s, "can't read" in d), (P.WARN, True))

    def test_vram(self):
        ((s, _, d),) = P.check_vram(self.ctx())
        self.assertEqual(s, P.PASS)
        self.assertIn("minimum", d)
        self.cmds["nvidia-smi"] = (0, "Small GPU, 2048, 2000\nBig GPU, 8192, 8000")
        ((s, _, d),) = P.check_vram(self.ctx())
        self.assertEqual((s, "Big GPU" in d, "minimum" in d), (P.PASS, True, False))
        self.cmds["nvidia-smi"] = (0, "Small GPU, 2048, 2000")
        self.assertEqual(self.statuses(P.check_vram(self.ctx())), [P.WARN])
        self.cmds["nvidia-smi"] = (9, "NVIDIA-SMI has failed")
        self.assertEqual(self.statuses(P.check_vram(self.ctx())), [P.WARN])
        self.have.discard("nvidia-smi")
        self.assertEqual(self.statuses(P.check_vram(self.ctx())), [P.WARN])

    def test_vram_unreadable_memory_warns(self):
        self.cmds["nvidia-smi"] = (0, "Some GPU, [N/A], [N/A]")
        ((s, _, d),) = P.check_vram(self.ctx())
        self.assertEqual((s, "can't read its memory" in d), (P.WARN, True))


class TestPreflight(Fixture):
    def test_all_pass_and_exit_code(self):
        self.login(offline_log=True)
        self.build_bg3le()
        self.deploy_mod()
        lines = P.preflight(self.ctx())
        self.assertEqual(set(self.statuses(lines)), {P.PASS}, lines)
        names = [n for _, n, _ in lines]
        for n in (
            "install",
            "native",
            "sniper",
            "build",
            "updates",
            "steam",
            "bg3le",
            "mod",
            "claude",
            "docker",
            "ram",
            "vram",
        ):
            self.assertIn(n, names)

    def test_crashing_check_is_a_fail_not_an_abort(self):
        with mock.patch.object(P, "check_mod", side_effect=RuntimeError("x")):
            lines = P.preflight(self.ctx())
        self.assertIn(P.FAIL, self.statuses(lines))
        self.assertIn("vram", [n for _, n, _ in lines])  # later checks still ran

    def test_main_exit(self):
        with (
            mock.patch.object(P, "preflight", return_value=[(P.PASS, "a", "x"), (P.WARN, "b", "y")]),
            mock.patch("sys.stdout"),
        ):
            self.assertEqual(P.main([], self.ctx()), 0)
        with mock.patch.object(P, "preflight", return_value=[(P.FAIL, "a", "x")]), mock.patch("sys.stdout"):
            self.assertEqual(P.main([], self.ctx()), 1)

    def test_the_old_path_is_the_cli_and_the_module(self):
        import bench_preflight

        self.assertIs(bench_preflight, P)
        out = subprocess.run(
            [sys.executable, str(REPO / "tools" / "bench_preflight.py"), "--help"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("--series", out.stdout)


if __name__ == "__main__":
    unittest.main()
