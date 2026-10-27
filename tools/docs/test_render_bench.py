"""render_bench on a small synthetic analysis file shaped like research/reports/bench-diverse-2026-10-25.json."""

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import render_bench


def job(model: str, scenario: str, **kw: Any) -> dict[str, Any]:
    base = {"model": model, "scenario": scenario, "matrix": "B3 diverse-2026-10-25", "label": None, "minutes": 10.0}
    if scenario.endswith("gold_max"):
        base |= {"outcome": "score", "score": 100}
    else:
        base |= {"outcome": "pass", "t_pass_min": 9.0}
    return base | kw


def timeline(model: str, scenario: str, **kw: Any) -> dict[str, Any]:
    tl = {"model": model, "scenario": scenario, "counted": True, "points": [[0.3, 100], [5.0, 200]]}
    return tl | {"score_counted": 100, "markers": []} | kw


def data() -> dict[str, Any]:
    jobs = [job(m, s) for m, _ in render_bench.MODELS for s, _, _ in render_bench.SCENARIOS]
    jobs[0] = job("Opus 5.5", "crash_site_grove", outcome="fail", label="cost cap $0.35", minutes=41.3)
    jobs.append(job("Haiku 5.5", "fight_act1_windmill", matrix="B4 rerun", t_pass_min=5.5))
    opus_act1 = next(i for i, j in enumerate(jobs) if (j["model"], j["scenario"]) == ("Opus 5.5", "act1_gold_max"))
    jobs[opus_act1] = job("Opus 5.5", "act1_gold_max", score=808, minutes=45.1, label="score to the wipe")
    timelines = [timeline(m, s) for m, _ in render_bench.MODELS for s in ("act1_gold_max", "act3_gold_max")]
    timelines[0] = timeline(
        "Opus 5.5",
        "act1_gold_max",
        points=[[0.3, 133], [27.6, 941], [43.8, 986]],
        score_counted=808,
        markers=[{"t_min": 41.72, "gold": 941}],
    )
    timelines.append(timeline("Opus 5.5", "act1_gold_max", counted=False, score_counted=999))
    models = {
        m: {"cost_usd": 1.25 * (i + 1), "cost_bench_recorded": 99.0} for i, (m, _) in enumerate(render_bench.MODELS)
    }
    return {"jobs": jobs, "gold_timelines": timelines, "models": models}


class RenderBenchTest(unittest.TestCase):
    def test_grid_uses_the_counted_job_and_the_score_at_the_wipe(self) -> None:
        svg = render_bench.grid_svg(data(), "light")
        self.assertIn("+808 gold", svg)
        self.assertIn("wiped at 41.7 min (4)", svg)
        self.assertIn("5.5 min (1)", svg)  # the B4 rerun replaces Haiku's B3 windmill cell
        self.assertNotIn("9.0 min (1)", svg)
        self.assertIn("cost cap at 41.3 min", svg)

    def test_grid_costs_come_from_cost_usd(self) -> None:
        svg = render_bench.grid_svg(data(), "dark")
        self.assertIn("$1.25 (Opus), $2.50 (Sonnet) and $3.75 (Haiku)", svg)
        self.assertIn("billed $5.00", svg)
        self.assertNotIn("99.00", svg)

    def test_gold_line_is_dashed_after_the_wipe_and_labelled_with_the_counted_score(self) -> None:
        svg = render_bench.gold_svg(data(), "light")
        self.assertEqual(svg.count('stroke-dasharray="3 4"'), 1)
        self.assertIn("+808, wiped at 41.7 min", svg)
        self.assertNotIn("+999", svg)  # an attempt that doesn't count is not drawn

    def test_main_writes_both_charts_in_both_themes(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "report.json"
            src.write_text(json.dumps(data()))
            self.assertEqual(render_bench.main(["render_bench.py", str(src), tmp]), 0)
            names = sorted(p.name for p in Path(tmp).glob("*.svg"))
        self.assertEqual(
            names,
            [
                "bench-pilot-gold-dark.svg",
                "bench-pilot-gold-light.svg",
                "bench-pilot-grid-dark.svg",
                "bench-pilot-grid-light.svg",
            ],
        )
        self.assertEqual(render_bench.main(["render_bench.py"]), 2)


if __name__ == "__main__":
    unittest.main()
