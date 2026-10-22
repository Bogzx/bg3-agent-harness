#!/usr/bin/env python3
"""Unit tests for bg3harness.profiles: every example in profiles/ loads, the validation errors, and pi's
models.json against golden files in fixtures/pi/ (regenerate one with BG3AI_UPDATE_GOLDEN=1 after a
deliberate change, then review the diff).

    uv run pytest packages/harness/tests/test_profiles.py
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from bg3harness import profiles as pr

REPO = Path(__file__).resolve().parents[3]
EXAMPLES = REPO / "profiles"
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "pi"

PI = {
    "client": "pi",
    "model": "vendor/model-1",
    "effort": "medium",
    "endpoint": {
        "provider": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
    },
    "routing": {"only": ["vendor"]},
    "model_info": {
        "input": ["text", "image"],
        "reasoning": True,
        "context_window": 200000,
        "max_tokens": 16000,
        "cost": {"input": 1.0, "output": 4.0, "cache_read": 0.1},
    },
}


def pi(**changes: Any) -> dict[str, Any]:
    """PI with top-level keys replaced (None removes one) and dotted keys ("endpoint.provider") set inside."""
    raw: dict[str, Any] = json.loads(json.dumps(PI))
    for k, v in changes.items():
        table, _, key = k.replace("__", ".").rpartition(".")
        target = raw[table] if table else raw
        if v is None:
            target.pop(key, None)
        else:
            target[key] = v
    return raw


class Examples(unittest.TestCase):
    def test_every_example_loads(self):
        paths = sorted(EXAMPLES.glob("*.toml"))
        self.assertEqual({pr.load(p).client for p in paths}, set(pr.CLIENT_NAMES))

    def test_examples_hold_no_key(self):
        for p in EXAMPLES.glob("*.toml"):
            text = p.read_text()
            self.assertNotRegex(text, r"sk-[A-Za-z0-9]|api_key\s*=", p.name)

    def test_golden_models_json(self):
        pi = [p for p in sorted(EXAMPLES.glob("*.toml")) if pr.load(p).client == "pi"]
        self.assertEqual(len(pi), 4)
        for p in pi:
            got = json.dumps(pr.models_json(pr.load(p)), indent=1) + "\n"
            golden = GOLDEN / f"models.{p.stem}.json"
            if os.environ.get("BG3AI_UPDATE_GOLDEN"):
                golden.write_text(got)
            self.assertEqual(got, golden.read_text(), f"{golden.name}: regenerate with BG3AI_UPDATE_GOLDEN=1")


class Parse(unittest.TestCase):
    def err(self, raw: dict[str, Any], pattern: str) -> None:
        with self.assertRaisesRegex(pr.ProfileError, pattern):
            pr.parse(raw, "t.toml")

    def test_pi_profile(self):
        p = pr.parse(pi(), "t.toml")
        self.assertEqual(
            (p.client, p.model, p.effort, p.pi_model), ("pi", "vendor/model-1", "medium", "openrouter/vendor/model-1")
        )
        self.assertEqual(p.endpoint, pr.Endpoint("openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"))
        # the pin's defaults: no fallback to another provider, no endpoint that ignores a parameter
        self.assertEqual(p.routing, {"only": ["vendor"], "allow_fallbacks": False, "require_parameters": True})

    def test_other_clients(self):
        for client in ("claude", "codex", "gemini"):
            p = pr.parse({"client": client, "model": "m", "effort": "high"}, "t.toml")
            self.assertEqual((p.client, p.endpoint, p.routing, p.model_info), (client, None, None, None))
        self.assertEqual(pr.parse({"client": "claude", "model": "m", "fallback_model": "f"}).fallback_model, "f")

    def test_endpoint_keys_rejected_on_non_pi_clients(self):
        for client in ("claude", "codex", "gemini"):
            for key in pr.PI_ONLY:
                self.err({"client": client, "model": "m", key: PI[key]}, f'{key} only apply to client "pi"')

    def test_fallback_model_is_claude_only(self):
        self.err({"client": "codex", "model": "m", "fallback_model": "f"}, "unknown key.*fallback_model")
        self.err(pi(fallback_model="f"), "unknown key.*fallback_model")

    def test_basic_fields(self):
        self.err({"model": "m"}, "client must be one of")
        self.err({"client": "llama", "model": "m"}, "client must be one of")
        self.err({"client": "claude"}, "model must be a non-empty string")
        self.err({"client": "claude", "model": "m", "effort": "ultra"}, "effort must be one of")
        self.err({"client": "claude", "model": "m", "temperature": 1}, "unknown key.*temperature")

    def test_endpoint_validation(self):
        self.err(pi(endpoint=None), r"pi needs an \[endpoint\]")
        self.err(pi(endpoint__api="anthropic-messages"), "api must be 'openai-completions'")
        self.err(pi(endpoint__api_key="sk-x"), "unknown key.*api_key")
        self.err(pi(endpoint__api_key_env="sk-or-v1-abc"), "must name an environment variable")
        self.err(pi(endpoint__base_url="openrouter.ai"), "not an http")
        self.err(pi(endpoint__provider="Open Router"), "not a pi provider id")
        # pi turns on Anthropic cache_control for provider id "openrouter" only
        self.err(pi(endpoint__provider="bench"), 'OpenRouter base_url needs provider = "openrouter"')
        self.assertEqual(pr.parse(pi(endpoint__api="openai-completions")).endpoint, pr.parse(pi()).endpoint)

    def test_routing_validation(self):
        local = pi(endpoint__provider="bench", endpoint__base_url="http://127.0.0.1:8080/v1")
        self.err(local, "is not OpenRouter")
        local.pop("routing")
        self.assertIsNone(pr.parse(local).routing)
        self.err(pi(routing={"only": []}), "only must be a non-empty list")
        self.err(pi(routing={"order": ["a"]}), "unknown key.*order")
        self.err(pi(routing={"only": ["a"], "allow_fallbacks": "no"}), "allow_fallbacks must be true or false")
        routing = pr.parse(pi(routing={"only": ["a"], "allow_fallbacks": True})).routing
        self.assertTrue(routing is not None and routing["allow_fallbacks"])

    def test_model_info_validation(self):
        self.err(pi(model_info=None), r"pi needs a \[model_info\]")
        self.err(pi(model_info__input=["image"]), 'include "text"')
        self.err(pi(model_info__input=["text", "audio"]), "input must list")
        self.err(pi(model_info__reasoning="yes"), "reasoning must be true or false")
        self.err(pi(model_info__context_window=0), "context_window must be a positive integer")
        self.err(pi(model_info__max_tokens=True), "max_tokens must be a positive integer")
        self.err(pi(model_info__max_tokens=200000), "must be below context_window")
        self.err(pi(model_info__thinking_level_map={"turbo": "x"}), "unknown key.*turbo")
        self.err(pi(model_info__thinking_level_map={"low": 1}), "strings, or false")
        self.err(pi(model_info__cost={"input": -1}), "non-negative")
        self.err(pi(model_info__cost={"reasoning": 1}), "unknown key.*reasoning")
        self.err(pi(model_info__compat={"openRouterRouting": {}}), r"provider pin goes in \[routing\]")

    def test_a_pi_profile_needs_the_rates_to_price_an_unbilled_response(self):
        # an endpoint that bills nothing (not OpenRouter, not DeepSeek's API) would otherwise run as $0, uncapped
        other = {"provider": "acme", "base_url": "https://llm.acme.example/v1", "api_key_env": "ACME_KEY"}
        self.err(pi(routing=None, endpoint=other, model_info__cost=None), "cost needs input, output, cache_read")
        self.err(pi(model_info__cost={"input": 1, "output": 2}), "cost needs cache_read")
        mi = pr.parse(pi(model_info__cost={"input": 1, "output": 2, "cache_read": 0})).model_info
        assert mi is not None
        self.assertEqual(mi.cost, {"input": 1.0, "output": 2.0, "cache_read": 0.0})  # cache_write: 0 in models.json

    def test_load_reports_bad_toml(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.toml"
            path.write_text("client = \n")
            with self.assertRaisesRegex(pr.ProfileError, "bad.toml: not valid TOML"):
                pr.load(path)


class Status(unittest.TestCase):
    def test_offline_unless_the_profile_says(self):
        self.assertEqual(pr.parse(pi()).status, "offline")
        for client in ("claude", "pi"):
            raw = pi(status="verified") if client == "pi" else {"client": client, "model": "m", "status": "verified"}
            self.assertEqual(pr.parse(raw).status, "verified")
        with self.assertRaisesRegex(pr.ProfileError, "t.toml: status must be one of verified, offline, unsupported"):
            pr.parse(pi(status="tested"), "t.toml")

    def test_the_examples_say_what_has_run_live(self):
        got = {p.name: pr.load(p).status for p in EXAMPLES.glob("*.toml")}
        self.assertEqual(got["deepseek-flash.toml"], "verified")  # research/ovh live3, live4
        self.assertEqual(got["opus-5.5-claude-code.toml"], "verified")
        self.assertEqual(got["gemini-cli.toml"], "unsupported")  # D14
        self.assertEqual({got[n] for n in got if n.startswith(("codex", "deepseek-v4", "opus-5.5-pi"))}, {"offline"})


class OffRoute(unittest.TestCase):
    def test_providers_outside_the_pin_by_name_or_slug(self):
        pinned = pr.parse(pi(routing={"only": ["deepseek", "amazon-bedrock"]}))
        served = ["DeepSeek", "Amazon Bedrock", "deepseek", "Novita", "amazon_bedrock"]
        self.assertEqual(pr.off_route(pinned, served), ["Novita"])
        self.assertEqual(pr.off_route(pr.parse(pi(routing=None)), ["Novita"]), [])  # no pin: nothing is off it


class Readme(unittest.TestCase):
    def test_the_table_lists_every_profile_with_its_client_and_status(self):
        rows = {}
        for line in (EXAMPLES / "README.md").read_text().splitlines():
            cells = [c.strip(" `") for c in line.strip("|").split("|")]
            if cells[0].endswith(".toml"):
                rows[cells[0]] = (cells[1], cells[2])
        want = {p.name: (pr.load(p).client, pr.load(p).status) for p in EXAMPLES.glob("*.toml")}
        self.assertEqual(rows, want)


class Digest(unittest.TestCase):
    def test_what_a_profile_sets_not_where_it_came_from(self):
        base = pr.digest(pr.parse(pi(), "a.toml"))
        self.assertRegex(base, r"^[0-9a-f]{64}$")
        self.assertEqual(pr.digest(pr.parse(pi(), "elsewhere/b.toml")), base)
        for changed in (pi(model_info__cost={"input": 1.0, "output": 4.0, "cache_read": 0.2}), pi(effort="low")):
            self.assertNotEqual(pr.digest(pr.parse(changed)), base)
        self.assertEqual(pr.digest(pr.parse(pi(status="verified"))), base)  # what it played, not how far it's checked


class ToolsOff(unittest.TestCase):
    def test_a_text_only_pi_model_gets_no_screenshots(self):
        text_only = pr.parse(pi(model_info__input=["text"]))
        self.assertEqual(pr.tools_off(text_only), ("screenshots",))
        self.assertEqual(pr.tool_env(text_only, {"X": "1"}), {"X": "1", "BG3AI_TOOLS_OFF": "screenshots"})
        self.assertEqual(pr.tool_env(text_only, {"BG3AI_TOOLS_OFF": "wiki"})["BG3AI_TOOLS_OFF"], "wiki,screenshots")
        self.assertEqual(pr.tool_env(text_only, {"BG3AI_TOOLS_OFF": "screenshots"})["BG3AI_TOOLS_OFF"], "screenshots")

    def test_image_models_and_the_clis_keep_every_tool(self):
        self.assertEqual(pr.tools_off(pr.parse(pi())), ())
        self.assertEqual(pr.tools_off(pr.parse({"client": "codex", "model": "m"})), ())
        self.assertEqual(pr.tool_env(pr.parse(pi()), {"BG3AI_TOOLS_OFF": "wiki"}), {"BG3AI_TOOLS_OFF": "wiki"})
        self.assertEqual(pr.tool_env(pr.parse(pi()), {}), {})


class ModelsJson(unittest.TestCase):
    def test_full_definition(self):
        raw = pi(
            model_info__thinking_level_map={"off": "none", "minimal": False},
            model_info__cost={"input": 1, "output": 2.5, "cache_read": 0},
            model_info__compat={"requiresReasoningContentOnAssistantMessages": True},
        )
        self.assertEqual(
            pr.models_json(pr.parse(raw)),
            {
                "providers": {
                    "openrouter": {
                        "baseUrl": "https://openrouter.ai/api/v1",
                        "api": "openai-completions",
                        "apiKey": "${OPENROUTER_API_KEY}",  # an env reference: the key is never written
                        "models": [
                            {
                                "id": "vendor/model-1",
                                "name": "vendor/model-1",
                                "api": "openai-completions",
                                "reasoning": True,
                                "input": ["text", "image"],
                                "contextWindow": 200000,
                                "maxTokens": 16000,
                                "cost": {"input": 1.0, "output": 2.5, "cacheRead": 0.0, "cacheWrite": 0.0},
                                "thinkingLevelMap": {"off": "none", "minimal": None},
                                "compat": {
                                    "requiresReasoningContentOnAssistantMessages": True,
                                    "openRouterRouting": {
                                        "only": ["vendor"],
                                        "allow_fallbacks": False,
                                        "require_parameters": True,
                                    },
                                },
                            }
                        ],
                    }
                }
            },
        )

    def test_no_routing_no_compat(self):
        raw = pi(endpoint__provider="deepseek", endpoint__base_url="https://api.deepseek.com", routing=None)
        model = pr.models_json(pr.parse(raw))["providers"]["deepseek"]["models"][0]
        self.assertNotIn("compat", model)
        self.assertNotIn("thinkingLevelMap", model)

    def test_only_for_pi(self):
        with self.assertRaisesRegex(pr.ProfileError, "pi profiles only"):
            pr.models_json(pr.parse({"client": "codex", "model": "m"}))


if __name__ == "__main__":
    unittest.main()
