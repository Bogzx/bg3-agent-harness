"""Client profiles: one TOML file per model condition, one format for all four clients (claude, codex, gemini, pi).

A profile names the client, the model and the reasoning effort. For pi it also names the endpoint (provider id,
base URL, the env var that holds the key: the key itself never lives in a profile), OpenRouter's provider pin and
the full model definition, which `models_json` turns into pi's models.json so a run never depends on pi's bundled
catalog (research/arch/pi-client.md §3.3). A model goes through pi as `openai-completions` (D6 in
docs/OVERHAUL-PLAN.md) unless its endpoint says `api = "anthropic-messages"`: Claude on Anthropic's own API, where
pi's estimate from the profile's rates is the bill (below).

    client = "pi"
    model = "anthropic/claude-opus-5.5"
    effort = "high"
    [endpoint]
    provider = "openrouter"
    base_url = "https://openrouter.ai/api/v1"
    api_key_env = "OPENROUTER_API_KEY"
    [routing]                      # OpenRouter only: compat.openRouterRouting (the request's `provider` field)
    only = ["anthropic"]           # allow_fallbacks = false and require_parameters = true unless set
    [model_info]
    input = ["text", "image"]
    reasoning = true
    context_window = 1000000
    max_tokens = 32000
    thinking_level_map = { minimal = false }   # optional: pi level -> provider value, false = unsupported
    compat = { requiresReasoningContentOnAssistantMessages = true }   # optional: pi's compat flags
    cost = { input = 4.0, output = 20.0, cache_read = 0.2, cache_write = 5.0 }   # USD per M tokens (below)

cost is required with input, output and cache_read (cache_write defaults to 0): it is what a response costs when
the endpoint bills nothing (any OpenAI-compatible endpoint but OpenRouter, which reports usage.cost; DeepSeek's own
API is priced from these rates), so a profile without it would run as a $0 run no cost cap ever stops.

Anthropic's API reports tokens, not dollars: pi prices each response from the rates (uncached input, cache writes,
cache reads, output; pi asks for 5-minute cache writes, and would price a 1-hour write at 2x input). So an
anthropic-messages profile needs cache_write too, and a model priced by prompt length (Claude Haiku 5.5) lists its
higher rates as tiers, each for a request whose input (uncached + cache reads + cache writes) is above a threshold;
the highest matching tier prices the whole request, output included:

    [endpoint]
    provider = "anthropic"
    base_url = "https://api.anthropic.com"
    api = "anthropic-messages"
    api_key_env = "BG3AI_ANTHROPIC_API_KEY"
    [model_info.cost]
    input = 0.1
    output = 0.5
    cache_read = 0.01
    cache_write = 0.125
    [[model_info.cost.tiers]]
    input_tokens_above = 100000
    input = 0.5
    output = 2.5
    cache_read = 0.05
    cache_write = 0.625

A pi model definition replaces pi's catalog entry of the same id, catalog compat flags included: copy the ones the
model needs into compat (pi detects the provider's defaults from the provider id and base_url).

claude, codex and gemini take only client, model, effort (and fallback_model for claude): their endpoint is the
CLI's own, so endpoint, routing and model_info are an error there rather than silently meaning nothing.

Any profile may say how far it has been checked: status = "verified" (played live through the game), "offline"
(the default: only the offline checks have run it) or "unsupported" (kept for later, never to be run). The bench
matrix refuses an unsupported profile, and an offline one unless the matrix file allows unverified profiles. The
status is not part of digest(): promoting a profile after its first live run doesn't change what its runs played.

    uv run pytest packages/harness/tests/test_profiles.py
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

CLIENT_NAMES = ("claude", "codex", "gemini", "pi")
EFFORTS = ("low", "medium", "high", "xhigh", "max")  # play.EFFORTS
STATUSES = ("verified", "offline", "unsupported")
API = "openai-completions"  # pi's default wire format (D6)
APIS = (API, "anthropic-messages")  # anthropic-messages: Anthropic's own API (pi's native provider)
INPUTS = ("text", "image")
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")  # pi's thinkingLevelMap keys
COST_KEYS = {"input": "input", "output": "output", "cache_read": "cacheRead", "cache_write": "cacheWrite"}
COST_REQUIRED = ("input", "output", "cache_read")  # deepseek_bill.RATES: enough to price any response
TIER_ABOVE = "input_tokens_above"  # a cost tier's threshold (pi's inputTokensAbove)
ROUTING_KEYS = ("only", "allow_fallbacks", "require_parameters")
PI_ONLY = ("endpoint", "routing", "model_info")
ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
Json = dict[str, Any]  # a TOML table


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Endpoint:
    provider: str  # pi's provider id; models.json defines it (a built-in id like openrouter is overridden)
    base_url: str
    api_key_env: str  # the runner passes this env var to pi; models.json refers to it as ${NAME}
    api: str = API  # APIS: pi's wire format for this endpoint


@dataclass(frozen=True)
class ModelInfo:
    input: tuple[str, ...]
    reasoning: bool
    context_window: int
    max_tokens: int
    # pi level -> provider value (None: unsupported)
    thinking_level_map: dict[str, str | None] = field(default_factory=dict[str, str | None])
    # USD per M tokens, COST_KEYS: pi's estimate of an unbilled response
    cost: dict[str, float] = field(default_factory=dict[str, float])
    compat: Json = field(default_factory=Json)  # pi's model compat flags, as written
    # prompt-length pricing: each {TIER_ABOVE: n, <COST_KEYS>...} prices a request whose input is above n
    cost_tiers: tuple[dict[str, float], ...] = ()


@dataclass(frozen=True)
class Profile:
    client: str
    model: str
    effort: str | None = None
    fallback_model: str | None = None
    endpoint: Endpoint | None = None
    routing: Json | None = None  # compat.openRouterRouting, as sent
    model_info: ModelInfo | None = None
    status: str = "offline"  # STATUSES
    source: str = ""

    @property
    def pi_model(self) -> str:
        """pi's --model: provider/model."""
        assert self.endpoint is not None
        return f"{self.endpoint.provider}/{self.model}"


def _table(v: object) -> Json | None:
    return cast(Json, v) if isinstance(v, dict) else None


def _take(table: Json, where: str, allowed: tuple[str, ...]) -> Json:
    extra = sorted(set(table) - set(allowed))
    if extra:
        raise ProfileError(f"{where}: unknown key(s) {', '.join(extra)} (allowed: {', '.join(allowed)})")
    return table


def _str(table: Json, key: str, where: str) -> str:
    v = table.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ProfileError(f"{where}: {key} must be a non-empty string")
    return v


def _int(table: Json, key: str, where: str) -> int:
    v = table.get(key)
    if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
        raise ProfileError(f"{where}: {key} must be a positive integer")
    return v


def _endpoint(raw: object, where: str) -> Endpoint:
    t = _table(raw)
    if t is None:
        raise ProfileError(f"{where}: pi needs an [endpoint] table (provider, base_url, api_key_env)")
    w = f"{where} [endpoint]"
    _take(t, w, ("provider", "base_url", "api", "api_key_env"))
    api = t.get("api", API)
    if api not in APIS:
        raise ProfileError(f"{w}: api must be one of {', '.join(map(repr, APIS))} (got {api!r})")
    provider, base_url, key = (_str(t, k, w) for k in ("provider", "base_url", "api_key_env"))
    if not PROVIDER_RE.match(provider):
        raise ProfileError(f"{w}: provider {provider!r} is not a pi provider id (lower case, digits, . _ -)")
    if urlparse(base_url).scheme not in ("http", "https") or not urlparse(base_url).netloc:
        raise ProfileError(f"{w}: base_url {base_url!r} is not an http(s) URL")
    if not ENV_RE.match(key):
        raise ProfileError(f"{w}: api_key_env must name an environment variable (the key never goes in a profile)")
    if is_openrouter(base_url) and provider != "openrouter":
        # pi turns on OpenRouter's Anthropic cache_control by the provider id, not the URL
        raise ProfileError(f'{w}: an OpenRouter base_url needs provider = "openrouter" (got {provider!r})')
    return Endpoint(provider, base_url, key, api)


def is_openrouter(base_url: str) -> bool:
    host = urlparse(base_url).hostname or ""
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def _routing(raw: object, where: str, endpoint: Endpoint) -> Json:
    w = f"{where} [routing]"
    t = _table(raw)
    if t is None:
        raise ProfileError(f"{w}: must be a table")
    _take(t, w, ROUTING_KEYS)
    if not is_openrouter(endpoint.base_url):
        raise ProfileError(
            f"{w}: routing is OpenRouter's provider pin; base_url {endpoint.base_url!r} is not OpenRouter"
        )
    only = _strings(t.get("only"))
    if not only or not all(only):
        raise ProfileError(f"{w}: only must be a non-empty list of provider names")
    out: Json = {
        "only": only,
        "allow_fallbacks": t.get("allow_fallbacks", False),
        "require_parameters": t.get("require_parameters", True),
    }
    for k in ("allow_fallbacks", "require_parameters"):
        if not isinstance(out[k], bool):
            raise ProfileError(f"{w}: {k} must be true or false")
    return out


def _strings(v: object) -> list[str] | None:
    """v as a list of strings, None when it is not one."""
    if not isinstance(v, list):
        return None
    items = cast(list[object], v)
    return [x for x in items if isinstance(x, str)] if all(isinstance(x, str) for x in items) else None


def _rates(cost: Json, where: str, required: tuple[str, ...]) -> dict[str, float]:
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 for v in cost.values()):
        raise ProfileError(f"{where}: cost values must be non-negative numbers (USD per million tokens)")
    missing = [k for k in required if k not in cost]
    if missing:
        raise ProfileError(
            f"{where}: cost needs {', '.join(missing)} (USD per million tokens): a response the endpoint doesn't bill "
            "is priced from it, and a run without it would count $0 against its cost cap"
        )
    return {k: float(v) for k, v in cost.items()}


def _tiers(raw: object, where: str) -> tuple[dict[str, float], ...]:
    """cost.tiers: every tier sets all four rates (it replaces the base rates for its whole request)."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ProfileError(f"{where}: cost tiers must be a list of tables")
    out: list[dict[str, float]] = []
    for i, item in enumerate(cast(list[object], raw)):
        w = f"{where} cost tiers[{i}]"
        t = _take(_table(item) or {}, w, (TIER_ABOVE, *COST_KEYS))
        above = _int(t, TIER_ABOVE, w)
        rates = _rates({k: v for k, v in t.items() if k != TIER_ABOVE}, w, tuple(COST_KEYS))
        if out and above <= out[-1][TIER_ABOVE]:
            raise ProfileError(f"{w}: {TIER_ABOVE} must rise from tier to tier")
        out.append({TIER_ABOVE: above, **rates})
    return tuple(out)


def _model_info(raw: object, where: str, endpoint: Endpoint) -> ModelInfo:
    w = f"{where} [model_info]"
    t = _table(raw)
    if t is None:
        raise ProfileError(f"{where}: pi needs a [model_info] table (input, reasoning, context_window, max_tokens)")
    _take(t, w, ("input", "reasoning", "context_window", "max_tokens", "thinking_level_map", "cost", "compat"))
    inp = _strings(t.get("input"))
    if inp is None or "text" not in inp or not set(inp) <= set(INPUTS) or len(set(inp)) != len(inp):
        raise ProfileError(f'{w}: input must list {INPUTS} without repeats and include "text"')
    if not isinstance(t.get("reasoning"), bool):
        raise ProfileError(f"{w}: reasoning must be true or false")
    ctx, max_tokens = _int(t, "context_window", w), _int(t, "max_tokens", w)
    if max_tokens >= ctx:
        raise ProfileError(f"{w}: max_tokens ({max_tokens}) must be below context_window ({ctx})")
    tlm = _take(_table(t.get("thinking_level_map")) or {}, f"{w} thinking_level_map", THINKING_LEVELS)
    if not all(isinstance(v, str) or v is False for v in tlm.values()):
        raise ProfileError(f"{w}: thinking_level_map values must be strings, or false for an unsupported level")
    cost = dict(_take(_table(t.get("cost")) or {}, f"{w} cost", (*COST_KEYS, "tiers")))
    tiers = _tiers(cost.pop("tiers", None), w)
    # Anthropic bills cache writes (pi marks the prompt for caching): a missing rate would price them at $0
    anthropic = endpoint.api == "anthropic-messages"
    rates = _rates(cost, w, (*COST_REQUIRED, "cache_write") if anthropic else COST_REQUIRED)
    compat = _table(t.get("compat") or {})
    if compat is None or "openRouterRouting" in compat:
        raise ProfileError(f"{w}: compat must be a table of pi compat flags; the provider pin goes in [routing]")
    return ModelInfo(
        tuple(inp),
        t["reasoning"],
        ctx,
        max_tokens,
        {k: None if v is False else v for k, v in tlm.items()},
        rates,
        dict(compat),
        tiers,
    )


def parse(raw: Json, source: str = "<profile>") -> Profile:
    """A profile from its TOML table; ProfileError says what is wrong and where."""
    client = raw.get("client")
    if client not in CLIENT_NAMES:
        raise ProfileError(f"{source}: client must be one of {', '.join(CLIENT_NAMES)} (got {client!r})")
    allowed = ("client", "model", "effort", "status") + (("fallback_model",) if client == "claude" else ())
    if client != "pi":
        given = [k for k in PI_ONLY if k in raw]
        if given:
            raise ProfileError(
                f'{source}: {", ".join(given)} only apply to client "pi"; {client} uses its CLI\'s '
                f"own endpoint and model catalog"
            )
    _take(raw, source, allowed + (PI_ONLY if client == "pi" else ()))
    model = _str(raw, "model", source)
    effort = raw.get("effort")
    if effort is not None and effort not in EFFORTS:
        raise ProfileError(f"{source}: effort must be one of {', '.join(EFFORTS)} (got {effort!r})")
    fallback = None if raw.get("fallback_model") is None else _str(raw, "fallback_model", source)
    status = raw.get("status", "offline")
    if status not in STATUSES:
        raise ProfileError(f"{source}: status must be one of {', '.join(STATUSES)} (got {status!r})")
    if client != "pi":
        return Profile(client, model, effort, fallback, status=status, source=source)
    endpoint = _endpoint(raw.get("endpoint"), source)
    routing = _routing(raw["routing"], source, endpoint) if "routing" in raw else None
    return Profile(
        client,
        model,
        effort,
        None,
        endpoint,
        routing,
        _model_info(raw.get("model_info"), source, endpoint),
        status=status,
        source=source,
    )


def load(path: Path | str) -> Profile:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ProfileError(f"{path}: not valid TOML: {e}") from None
    return parse(raw, str(path))


def digest(p: Profile) -> str:
    """The sha256 of what a profile sets (its parsed fields as canonical JSON, not its file: comments and key order
    don't count, a changed rate, pin or thinking map does; nor does the status), for the run manifest."""
    fields = asdict(p)
    del fields["source"], fields["status"]
    # fields added later count only when set, so a profile that doesn't use them keeps the digest its runs recorded
    if fields["endpoint"] is not None and fields["endpoint"]["api"] == API:
        del fields["endpoint"]["api"]
    if fields["model_info"] is not None and not fields["model_info"]["cost_tiers"]:
        del fields["model_info"]["cost_tiers"]
    return hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _provider_key(name: str) -> str:
    return re.sub(r"[\s_]+", "-", name.strip().lower())


def off_route(p: Profile, providers: list[str]) -> list[str]:
    """The served providers outside the profile's [routing].only (OpenRouter names them as "DeepSeek" or "Amazon
    Bedrock", the pin as slugs: compared lower-case, spaces as dashes); [] when it pins none."""
    only = {_provider_key(s) for s in _strings((p.routing or {}).get("only")) or ()}
    return [x for x in providers if only and _provider_key(x) not in only]


def tools_off(p: Profile) -> tuple[str, ...]:
    """The bg3 tool groups (bridge capabilities.py) a profile's model can't use: screenshots for a pi model without
    image input (pi would hand it "(see attached image)" and no picture). The CLIs (claude, codex, gemini) show
    images themselves."""
    return ("screenshots",) if p.model_info is not None and "image" not in p.model_info.input else ()


def tool_env(p: Profile, env: Mapping[str, str]) -> dict[str, str]:
    """`env` with the profile's tools_off added to BG3AI_TOOLS_OFF (the server, the tool count check and the run
    manifest all read it from there)."""
    names = [n.strip() for n in (env.get("BG3AI_TOOLS_OFF") or "").split(",") if n.strip()]
    names += [g for g in tools_off(p) if g not in names]
    return {**env, "BG3AI_TOOLS_OFF": ",".join(names)} if names else dict(env)


def models_json(p: Profile) -> Json:
    """pi's models.json for a pi profile: one provider with the profile's endpoint and one full model definition
    (it replaces a catalog model of the same id). The key stays an env reference."""
    if p.client != "pi" or p.endpoint is None or p.model_info is None:
        raise ProfileError(f"{p.source}: models.json is for pi profiles only")
    mi, api = p.model_info, p.endpoint.api
    cost: Json = {COST_KEYS[k]: mi.cost.get(k, 0.0) for k in COST_KEYS}
    if mi.cost_tiers:
        cost["tiers"] = [
            {"inputTokensAbove": int(t[TIER_ABOVE]), **{COST_KEYS[k]: t[k] for k in COST_KEYS}} for t in mi.cost_tiers
        ]
    model: Json = {
        "id": p.model,
        "name": p.model,
        "api": api,
        "reasoning": mi.reasoning,
        "input": list(mi.input),
        "contextWindow": mi.context_window,
        "maxTokens": mi.max_tokens,
        "cost": cost,
    }
    if mi.thinking_level_map:
        model["thinkingLevelMap"] = dict(mi.thinking_level_map)
    compat = dict(mi.compat) | ({"openRouterRouting": dict(p.routing)} if p.routing is not None else {})
    if compat:
        model["compat"] = compat
    return {
        "providers": {
            p.endpoint.provider: {
                "baseUrl": p.endpoint.base_url,
                "api": api,
                "apiKey": "${" + p.endpoint.api_key_env + "}",
                "models": [model],
            }
        }
    }
