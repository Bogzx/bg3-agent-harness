"""bg3harness.scenario_file: a resolved scenario splits into the v1 scenario and the run's expected settings; the
settings check tells contradictions from settings the game can't report."""

from __future__ import annotations

import unittest

from bg3harness import scenario_file as S  # noqa: N812

WANT = {"difficulty": "Balanced", "karmic_dice": False, "timing": "realtime"}
V1 = {"name": "beach", "start": {"kind": "new_game"}, "until": "nautiloid_escaped", "caps": {"run_hours": 1}}


class Split(unittest.TestCase):
    def test_v1_alone_passes_through(self) -> None:
        v1, want, errs = S.split(dict(V1))
        self.assertEqual((v1, errs), (V1, []))
        self.assertEqual(want, {"difficulty": None, "karmic_dice": None, "timing": None})

    def test_extension_keys_leave_the_v1_part(self) -> None:
        sc = dict(V1, game_settings={"seed": 7, "difficulty": "Balanced", "karmic_dice": False}, timing="realtime")
        v1, want, errs = S.split(sc)
        self.assertEqual(errs, [])
        self.assertEqual(v1, dict(V1, game_settings={"seed": 7}))
        self.assertEqual(want, {"difficulty": "Balanced", "karmic_dice": False, "timing": "realtime"})

    def test_no_seed_no_game_settings(self) -> None:
        v1, want, _ = S.split(dict(V1, game_settings={"karmic_dice": True}))
        self.assertNotIn("game_settings", v1)
        self.assertTrue(want["karmic_dice"])

    def test_bad_values_are_refused(self) -> None:
        sc = dict(V1, game_settings={"difficulty": "Hard", "karmic_dice": "no", "resolution": None}, timing="paused")
        errs = S.split(sc)[2]
        self.assertEqual(len(errs), 4, errs)
        self.assertIn("unknown keys resolution", errs[0])

    def test_not_an_object(self) -> None:
        self.assertEqual(S.split([1])[2], ["a scenario file holds a JSON object"])
        self.assertEqual(S.split(dict(V1, game_settings=3))[2], ["game_settings must be an object"])


class Mismatches(unittest.TestCase):
    def test_matching_game(self) -> None:
        self.assertEqual(S.mismatches(WANT, {"difficulty": "Balanced", "karmic_dice": False}), ([], []))

    def test_karmic_dice_on_is_a_contradiction(self) -> None:
        bad, unknown = S.mismatches(WANT, {"difficulty": None, "karmic_dice": True})
        self.assertEqual(bad, ["karmic_dice is True, the scenario needs False"])
        self.assertEqual(unknown, ["difficulty"])  # the readout has no difficulty yet

    def test_unfixed_settings_are_not_checked(self) -> None:
        self.assertEqual(S.mismatches({"difficulty": None, "karmic_dice": None}, {"karmic_dice": True}), ([], []))


if __name__ == "__main__":
    unittest.main()
