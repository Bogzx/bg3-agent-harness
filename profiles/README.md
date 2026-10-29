# profiles

One TOML file per model condition, for `play.py run --profile` and the bench matrix's `models`. The format is in
`packages/harness/src/bg3harness/profiles.py` (its docstring); [docs/QUICKSTART.md](../docs/QUICKSTART.md) shows how
to write your own.

| Profile | Client | Status | Cost a run counts |
|---|---|---|---|
| `deepseek-flash.toml` | pi | verified | DeepSeek's bill, computed from its usage fields (`deepseek_bill.py`) |
| `deepseek-v4-pro.toml` | pi | offline | DeepSeek's bill, computed from its usage fields (`deepseek_bill.py`) |
| `deepseek-v4.1-flash-pi.toml` | pi | offline | OpenRouter's billed `usage.cost` |
| `opus-5.5-pi.toml` | pi | offline | OpenRouter's billed `usage.cost` |
| `claude-opus-5-5.toml` | pi | offline | the profile's `cost` table (Anthropic's API reports tokens, not dollars) |
| `claude-sonnet-5-5.toml` | pi | offline | the profile's `cost` table (Anthropic's API reports tokens, not dollars) |
| `claude-haiku-5-5.toml` | pi | offline | the profile's `cost` table (Anthropic's API reports tokens, not dollars) and its over-100K-token tier |
| `opus-5.5-claude-code.toml` | claude | verified | none: a subscription; `total_cost_usd` is recorded, never charged |
| `sonnet-5.5-claude-code.toml` | claude | offline | none: a subscription; `total_cost_usd` is recorded, never charged |
| `haiku-5.5-claude-code.toml` | claude | offline | none: a subscription; `total_cost_usd` is recorded, never charged |
| `codex.toml` | codex | offline | none: a subscription; an estimate from `clients.CODEX_PRICES`, unknown for a model not in it |
| `gemini-cli.toml` | gemini | unsupported | none: a subscription; an estimate from `clients.GEMINI_PRICES` |

**Status** (the profile's `status` key): `verified` has played live through the game; `offline` has passed only
the offline checks (conformance against stub models); `unsupported` is kept for later and not run. The bench matrix
refuses an unsupported profile, and an offline one unless the matrix file sets `allow_unverified = true`.

**Cost.** Only pi spends money (`spend.PAID_CLIENTS`): its runs need `--max-cost` and `runtime/budget.json`. A pi
response that its endpoint doesn't bill (any endpoint but OpenRouter and DeepSeek, or an OpenRouter response
without `usage.cost`) is priced from the profile's `cost` table, which every pi profile must have. The run's
`served` record (manifest `model.served`) names the providers that answered.

**Effort** (`effort`: low, medium, high, xhigh, max) as each client gets it:

| Client | Flag | Mapping |
|---|---|---|
| claude | `--effort` | as is |
| codex | `model_reasoning_effort` | as is, `max` becomes `xhigh` |
| gemini | none | ignored: the CLI has no effort setting |
| pi | `--thinking` | as is; pi clamps a level the model's `thinking_level_map` drops to the next one up (else down), and the manifest's `model.served.effort` records what it sent |
