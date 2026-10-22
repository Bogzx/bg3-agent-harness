#!/usr/bin/env python3
"""Unit tests for runner/profiles.py: every example in profiles/ loads, the validation errors, and pi's
models.json against golden files in runner/fixtures/pi/ (regenerate one with BG3AI_UPDATE_GOLDEN=1 after a
deliberate change, then review the diff).

    python3 -m unittest runner/test_profiles.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profiles as PR

REPO = Path(__file__).resolve().parents[1]
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
    "model_info": {"input": ["text", "image"], "reasoning": True, "context_window": 200000, "max_tokens": 16000},
}


def pi(**changes) -> dict:
    """PI with top-level keys replaced (None removes one) and dotted keys ("endpoint.provider") set inside."""
    raw = json.loads(json.dumps(PI))
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
        self.assertEqual({PR.load(p).client for p in paths}, set(PR.CLIENT_NAMES))

    def test_examples_hold_no_key(self):
        for p in EXAMPLES.glob("*.toml"):
            text = p.read_text()
            self.assertNotRegex(text, r"sk-[A-Za-z0-9]|api_key\s*=", p.name)

    def test_golden_models_json(self):
        pi = [p for p in sorted(EXAMPLES.glob("*.toml")) if PR.load(p).client == "pi"]
        self.assertEqual(len(pi), 3)
        for p in pi:
            got = json.dumps(PR.models_json(PR.load(p)), indent=1) + "\n"
            golden = GOLDEN / f"models.{p.stem}.json"
            if os.environ.get("BG3AI_UPDATE_GOLDEN"):
                golden.write_text(got)
            self.assertEqual(got, golden.read_text(), f"{golden.name}: regenerate with BG3AI_UPDATE_GOLDEN=1")


class Parse(unittest.TestCase):
    def err(self, raw: dict, pattern: str):
        with self.assertRaisesRegex(PR.ProfileError, pattern):
            PR.parse(raw, "t.toml")

    def test_pi_profile(self):
        p = PR.parse(pi(), "t.toml")
        self.assertEqual(
            (p.client, p.model, p.effort, p.pi_model), ("pi", "vendor/model-1", "medium", "openrouter/vendor/model-1")
        )
        self.assertEqual(p.endpoint, PR.Endpoint("openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"))
        # the pin's defaults: no fallback to another provider, no endpoint that ignores a parameter
        self.assertEqual(p.routing, {"only": ["vendor"], "allow_fallbacks": False, "require_parameters": True})

    def test_other_clients(self):
        for client in ("claude", "codex", "gemini"):
            p = PR.parse({"client": client, "model": "m", "effort": "high"}, "t.toml")
            self.assertEqual((p.client, p.endpoint, p.routing, p.model_info), (client, None, None, None))
        self.assertEqual(PR.parse({"client": "claude", "model": "m", "fallback_model": "f"}).fallback_model, "f")

    def test_endpoint_keys_rejected_on_non_pi_clients(self):
        for client in ("claude", "codex", "gemini"):
            for key in PR.PI_ONLY:
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
        self.assertEqual(PR.parse(pi(endpoint__api="openai-completions")).endpoint.provider, "openrouter")

    def test_routing_validation(self):
        local = pi(endpoint__provider="bench", endpoint__base_url="http://127.0.0.1:8080/v1")
        self.err(local, "is not OpenRouter")
        local.pop("routing")
        self.assertIsNone(PR.parse(local).routing)
        self.err(pi(routing={"only": []}), "only must be a non-empty list")
        self.err(pi(routing={"order": ["a"]}), "unknown key.*order")
        self.err(pi(routing={"only": ["a"], "allow_fallbacks": "no"}), "allow_fallbacks must be true or false")
        self.assertTrue(PR.parse(pi(routing={"only": ["a"], "allow_fallbacks": True})).routing["allow_fallbacks"])

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

    def test_load_reports_bad_toml(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.toml"
            path.write_text("client = \n")
            with self.assertRaisesRegex(PR.ProfileError, "bad.toml: not valid TOML"):
                PR.load(path)


class ToolsOff(unittest.TestCase):
    def test_a_text_only_pi_model_gets_no_screenshots(self):
        text_only = PR.parse(pi(model_info__input=["text"]))
        self.assertEqual(PR.tools_off(text_only), ("screenshots",))
        self.assertEqual(PR.tool_env(text_only, {"X": "1"}), {"X": "1", "BG3AI_TOOLS_OFF": "screenshots"})
        self.assertEqual(PR.tool_env(text_only, {"BG3AI_TOOLS_OFF": "wiki"})["BG3AI_TOOLS_OFF"], "wiki,screenshots")
        self.assertEqual(PR.tool_env(text_only, {"BG3AI_TOOLS_OFF": "screenshots"})["BG3AI_TOOLS_OFF"], "screenshots")

    def test_image_models_and_the_clis_keep_every_tool(self):
        self.assertEqual(PR.tools_off(PR.parse(pi())), ())
        self.assertEqual(PR.tools_off(PR.parse({"client": "codex", "model": "m"})), ())
        self.assertEqual(PR.tool_env(PR.parse(pi()), {"BG3AI_TOOLS_OFF": "wiki"}), {"BG3AI_TOOLS_OFF": "wiki"})
        self.assertEqual(PR.tool_env(PR.parse(pi()), {}), {})


class ModelsJson(unittest.TestCase):
    def test_full_definition(self):
        raw = pi(
            model_info__thinking_level_map={"off": "none", "minimal": False},
            model_info__cost={"input": 1, "output": 2.5},
            model_info__compat={"requiresReasoningContentOnAssistantMessages": True},
        )
        self.assertEqual(
            PR.models_json(PR.parse(raw)),
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
        model = PR.models_json(PR.parse(raw))["providers"]["deepseek"]["models"][0]
        self.assertNotIn("compat", model)
        self.assertNotIn("thinkingLevelMap", model)

    def test_only_for_pi(self):
        with self.assertRaisesRegex(PR.ProfileError, "pi profiles only"):
            PR.models_json(PR.parse({"client": "codex", "model": "m"}))


if __name__ == "__main__":
    unittest.main()
