"""bg3harness.judge: extraction over fake events, the persona prompt, verdict parsing, the retry loop, chunking, the
same-model guard, the claude -p and HTTP clients (fake subprocess and fake HTTP: no process, no network), the cost
cap and per-call costs, and client_for over profile-shaped objects. Runner-side wiring: runner/test_judge.py."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bg3harness import judge
from bg3harness.testing.judge import FakeJudge


def verdict(score: float, per: Mapping[int, float]) -> str:
    return json.dumps(
        {
            "score": score,
            "summary": "fine",
            "decisions": [{"id": i, "score": s, "why": f"why {i}"} for i, s in per.items()],
        }
    )


@dataclass
class Ev:
    kind: str
    t: float | None = None
    call_id: str | None = None
    tool: str | None = None
    args: Any = None
    text: str = ""


def loader(evs: Sequence[Ev]) -> judge.Loader:
    return lambda base: evs


DECISIONS: list[judge.Decision] = [
    {"id": 1, "kind": "dialogue", "speaker": "A", "line": "l", "offered": ["1. x", "2. y"], "chosen": "x",
     "reason": "because", "n": 2},
    {"id": 2, "kind": "note", "chosen": "spared him", "reason": None},
]  # fmt: skip


class Extract(unittest.TestCase):
    def test_events_through_the_loader(self) -> None:
        shown = json.dumps({"result": "DIALOGUE Lae'zel: Move.\n  1. Who are you?\n  2. Lead on.\nSTOPPED"})
        evs = [
            Ev("tool_call", 1.0, "a", "dialogue_advance", {}),
            Ev("tool_result", 1.1, "a", text=shown),
            Ev("text", text="Following her, as a soldier would."),
            Ev("tool_call", 2.0, "b", "choose_dialogue", {"option": 2}),
            Ev("tool_result", 2.1, "b", text="conversation ended"),
            Ev("tool_call", 3.0, "c", "note", {"text": "x", "tag": "decision"}),
            Ev("tool_result", 3.1, "c", text="ok"),
        ]
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "S001.jsonl").write_text("")
            got, dropped = judge.extract(Path(d), None, None, loader(evs))
        self.assertEqual(
            [(x["kind"], x["chosen"], x["reason"]) for x in got],
            [("dialogue", "Lead on.", "Following her, as a soldier would."), ("note", "x", None)],
        )
        self.assertEqual((got[0]["offered"], got[0]["id"], dropped), (["1. Who are you?", "2. Lead on."], 1, 0))

    def test_cap_and_family(self) -> None:
        self.assertEqual(judge.cap([1, 2, 3], 0), ([], 3))
        self.assertTrue(judge.same_model("deepseek-flash", "deepseek/deepseek-v4.1-flash"))
        self.assertTrue(judge.same_model("anthropic/claude-opus-5.5", "claude-opus-5-5"))
        self.assertFalse(judge.same_model("deepseek-flash", "claude-sonnet-5-5"))
        self.assertIsNone(judge.family("llama-4"))


class Persona(unittest.TestCase):
    def test_prompt(self) -> None:
        p = judge.build_prompt("# Me", DECISIONS, dropped=3)
        self.assertIn("DECISIONS (2 of the run; 3 more not shown)", p)
        self.assertIn('"stated_reason": "because"', p)
        self.assertIn('"times": 2', p)
        self.assertIn('"said_to_you": "A: l"', p)

    def test_parse_verdict(self) -> None:
        v = judge.parse_verdict("Sure!\n```json\n" + verdict(6.5, {1: 7, 2: 6, 9: 1}) + "\n```", [1, 2])
        self.assertEqual(v["score"], 6.5)
        self.assertEqual(sorted(v["decisions"]), [1, 2])  # unknown id 9 dropped
        v = judge.parse_verdict('{"score": "n/a", "decisions": [{"id": 1, "score": 4}, {"id": 2, "score": 8}]}', [1, 2])
        self.assertEqual(v["score"], 6.0)  # no overall score: the mean of the decisions
        for bad in ("no json here", '{"score": 11, "decisions": []}', '{"other": 1} {"score": 1', "{ {"):
            with self.assertRaises(ValueError):
                judge.parse_verdict(bad, [1, 2])

    def test_retry_then_ok_records_each_call(self) -> None:
        fake = FakeJudge([RuntimeError("exit 1: overloaded"), verdict(7, {1: 7, 2: 7})])
        v = judge.judge_chunk(None, "# Me", DECISIONS, call=fake)
        self.assertEqual((v["score"], v["attempts"]), (7.0, 2))
        self.assertEqual(v["call_costs"], [0.0, 0.01])

    def test_failed_after_retries_keeps_the_cost_of_an_error(self) -> None:
        fake = FakeJudge(["garbage", judge.JudgeError("is_error", 0.03)])
        res = judge.judge_decisions(fake, "# Me", DECISIONS)
        self.assertEqual(res["status"], "judge_failed")
        self.assertIn("JudgeError", res["error"])
        self.assertEqual((len(fake.prompts), res["call_costs"], res["cost_usd"]), (2, [0.01, 0.03], 0.04))

    def test_chunks_weighted(self) -> None:
        ds = [dict(DECISIONS[1], id=i) for i in range(1, 4)]
        fake = FakeJudge([verdict(9, {1: 9, 2: 9}), verdict(3, {3: 3})])
        res = judge.judge_decisions(None, "# Me", ds, chunk=2, call=fake)
        self.assertEqual((res["status"], res["score"], res["calls"], res["call_costs"]), ("ok", 7.0, 2, [0.01, 0.01]))
        self.assertEqual([i["score"] for i in res["items"]], [9, 9, 3])
        p = judge.persona_field(res)
        self.assertEqual(p["out_of_character"][0].split(":")[0], "? #3 spared him")
        self.assertEqual(p["call_costs"], [0.01, 0.01])

    def test_judge_run_guards_and_never_raises(self) -> None:
        fake = FakeJudge([], model="claude-opus-5-5")
        with tempfile.TemporaryDirectory() as d:
            res = judge.judge_run(Path(d), {"model": "opus"}, client=fake, playthroughs=Path(d), load=loader([]))
            self.assertIn("model under test", res["error"])

            def boom(base: Path) -> list[Ev]:
                raise KeyError("x")

            (Path(d) / "S001.jsonl").write_text("")
            res = judge.judge_run(Path(d), None, client=fake, playthroughs=Path(d), load=boom)
        self.assertEqual((res["status"], res["error"]), ("judge_failed", "KeyError: 'x'"))


@dataclass
class Done:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class Claude(unittest.TestCase):
    def test_command_parse_and_complete(self) -> None:
        seen: list[dict[str, Any]] = []

        def run(cmd: list[str], **kw: Any) -> Done:
            seen.append(dict(kw, cmd=cmd))
            return Done(0, json.dumps({"type": "result", "result": "{}", "total_cost_usd": 0.02}))

        c = judge.ClaudeCLI("claude-sonnet-5-5", run=run)  # pyright: ignore[reportArgumentType]
        cmd = c.command()
        self.assertEqual(cmd[:6], ["claude", "-p", "--model", "claude-sonnet-5-5", "--output-format", "json"])
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertEqual(judge.call_client(c, "the prompt", 9), ("{}", {"cost_usd": 0.02}))
        self.assertEqual((seen[0]["input"], seen[0]["timeout"]), ("the prompt", 9))
        self.assertFalse(c.paid)
        with self.assertRaises(ValueError):
            c.parse(json.dumps({"is_error": True, "result": "API Error"}))
        for bad in ("not json", "[1]"):
            with self.assertRaises(ValueError):
                c.parse(bad)

    def test_nonzero_exit(self) -> None:
        c = judge.ClaudeCLI("m", run=lambda cmd, **kw: Done(1, "", "auth"))  # pyright: ignore[reportArgumentType, reportUnknownLambdaType]
        with self.assertRaisesRegex(RuntimeError, "exit 1: auth"):
            c.complete("p", 1)

    def test_bare_command_client(self) -> None:
        class Echo:
            model = "echo"

            def command(self) -> list[str]:
                return ["cat"]

            def parse(self, stdout: str) -> tuple[str, dict[str, Any]]:
                return stdout, {}

        self.assertEqual(judge.call_client(Echo(), "hi", 10), ("hi", {}))


PRICE = {"input": 1.0, "output": 4.0, "cache_read": 0.1}  # USD per million tokens


class FakeHTTP:
    def __init__(self, *answers: Any):
        self.answers = list(answers)
        self.sent: list[tuple[str, dict[str, Any], Mapping[str, str], float]] = []

    def __call__(self, url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> Any:
        self.sent.append((url, json.loads(body), headers, timeout))
        return self.answers.pop(0)


def reply(text: str, usage: dict[str, Any] | None) -> dict[str, Any]:
    return {"choices": [{"message": {"content": text}}], **({"usage": usage} if usage is not None else {})}


class HTTP(unittest.TestCase):
    def client(self, post: FakeHTTP, cap: float = 1.0, **kw: Any) -> judge.ChatCompletions:
        return judge.ChatCompletions("deepseek-flash", "https://api.example/v1/", "JUDGE_KEY", PRICE, cap,
                                 env={"JUDGE_KEY": "k"}, post=post, **kw)  # fmt: skip

    def test_request_and_cost_from_the_price_sheet(self) -> None:
        post = FakeHTTP(reply('{"score": 5}', {"prompt_tokens": 1000, "completion_tokens": 500,
                                               "prompt_tokens_details": {"cached_tokens": 400}}))  # fmt: skip
        c = self.client(post)
        text, meta = c.complete("judge this", 30)
        url, body, headers, timeout = post.sent[0]
        self.assertEqual(
            (url, headers["Authorization"], timeout), ("https://api.example/v1/chat/completions", "Bearer k", 30)
        )
        self.assertEqual(
            (body["model"], body["temperature"], body["max_tokens"]), ("deepseek-flash", 0, judge.MAX_ANSWER_TOKENS)
        )
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertNotIn("provider", body)
        self.assertEqual(text, '{"score": 5}')
        self.assertAlmostEqual(meta["cost_usd"], (600 * 1.0 + 400 * 0.1 + 500 * 4.0) / 1e6)
        self.assertAlmostEqual(c.spent_usd, meta["cost_usd"])

    def test_openrouter_cost_and_routing(self) -> None:
        post = FakeHTTP(reply("x", {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.5}))
        c = self.client(post, routing={"only": ["deepseek"]})
        self.assertEqual(c.complete("p", 1)[1]["cost_usd"], 0.5)
        self.assertEqual(
            (post.sent[0][1]["provider"], post.sent[0][1]["usage"]), ({"only": ["deepseek"]}, {"include": True})
        )

    def test_no_usage_charges_the_worst_case(self) -> None:
        c = self.client(FakeHTTP(reply("x", None)))
        self.assertEqual(c.complete("p", 1)[1]["cost_usd"], round(c.worst_case("p"), 6))

    def test_cap_refuses_a_call_that_could_cross_it(self) -> None:
        post = FakeHTTP(reply("x", {"cost": 0.009}), reply("y", {"cost": 0.009}))
        c = self.client(post, cap=0.02)  # worst case of one call: ~4000 * 4 / 1e6 = 0.016
        c.complete("p", 1)
        with self.assertRaises(judge.CostCapError):
            c.complete("p", 1)
        self.assertEqual(len(post.sent), 1)
        got = judge.ask_json(c, "p", str)
        self.assertEqual((got["attempts"], got["call_costs"]), (0, []))
        self.assertIn("CostCapError", got["error"])

    def test_errors(self) -> None:
        with self.assertRaisesRegex(judge.UnusableProfileError, "cost cap"):
            self.client(FakeHTTP(), cap=0)
        with self.assertRaisesRegex(judge.UnusableProfileError, "price sheet"):
            judge.ChatCompletions("m", "https://x", "K", {}, 1.0)
        c = judge.ChatCompletions("m", "https://x", "K", PRICE, 1.0, env={}, post=FakeHTTP())
        with self.assertRaisesRegex(RuntimeError, r"\$K is not set"):
            c.complete("p", 1)
        c = self.client(FakeHTTP({"error": "bad"}))
        with self.assertRaises(judge.JudgeError) as e:
            c.complete("p", 1)
        self.assertGreater(e.exception.cost_usd, 0)


@dataclass
class Endpoint:
    base_url: str = "https://api.deepseek.com"
    api_key_env: str = "DEEPSEEK_API_KEY"


@dataclass
class Info:
    cost: dict[str, float] = field(default_factory=lambda: dict(PRICE))


@dataclass
class Profile:
    client: str
    model: str
    endpoint: Endpoint | None = None
    model_info: Info | None = None
    routing: dict[str, Any] | None = None
    source: str = "profiles/x.toml"


class ClientFor(unittest.TestCase):
    def test_profiles(self) -> None:
        c = judge.client_for(Profile("claude", "claude-opus-5-5"))
        self.assertIsInstance(c, judge.ClaudeCLI)
        self.assertEqual(c.model, "claude-opus-5-5")
        pi = Profile("pi", "deepseek-flash", Endpoint(), Info())
        with self.assertRaisesRegex(judge.UnusableProfileError, "cost cap"):
            judge.client_for(pi)
        c = judge.client_for(pi, max_cost_usd=2.0, env={})
        assert isinstance(c, judge.ChatCompletions)
        self.assertEqual(
            (c.model, c.url, c.key_env, c.max_cost_usd, c.paid),
            ("deepseek-flash", "https://api.deepseek.com/chat/completions", "DEEPSEEK_API_KEY", 2.0, True),
        )
        with self.assertRaisesRegex(judge.UnusableProfileError, "price sheet"):
            judge.client_for(Profile("pi", "m", Endpoint(), None), max_cost_usd=1.0)
        with self.assertRaisesRegex(judge.UnusableProfileError, "not codex"):
            judge.client_for(Profile("codex", "gpt-x"))


class Transport(unittest.TestCase):
    def test_http_post_error(self) -> None:
        with self.assertRaises((RuntimeError, OSError)):
            judge.http_post("http://127.0.0.1:9/nothing", {}, b"{}", 0.5)

    def test_fake_judge_runs_dry(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "no answer left"):
            FakeJudge([]).complete("p", 0)
        self.assertIs(subprocess.run, judge.ClaudeCLI("m").run)


if __name__ == "__main__":
    unittest.main()
