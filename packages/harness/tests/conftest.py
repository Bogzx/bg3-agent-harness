"""Before any test module here imports bg3harness.runner: the game's profile folders are a temp dir for the process
(bg3harness.testing.profile). Without it a runner test that ran after test_run_host.py or test_game_host.py in one
pytest process saw the real Larian saves and Script Extender dirs (research/ovh/bench.md round 20)."""

from bg3harness.testing.profile import temp_profile

temp_profile()
