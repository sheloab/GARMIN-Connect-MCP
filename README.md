![GARMIN Connect MCP](docs/assets/banner.svg)

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-compatible-6D28D9)](https://modelcontextprotocol.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/sheloab/GARMIN-Connect-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/sheloab/GARMIN-Connect-MCP/actions/workflows/ci.yml)

# GARMIN Connect MCP

**LLM-agnostic Garmin Connect MCP server** for sleep and naps, recovery, Body Battery, training readiness, training load, running tolerance, VO2 Max, endurance and hill scores, race predictions, gear mileage, and running summaries.

Use the same local Garmin tools from any MCP client — Cursor, Continue, Codex CLI, Gemini CLI, Hermes, opencode — or from provider-backed demo agents using DeepSeek, OpenAI, OpenRouter, or Gemini.

## Tribute & Thanks

GARMIN Connect MCP is built on **[kavakoss/mcp-garmin-connect](https://github.com/kavakoss/mcp-garmin-connect)**. The original MCP server, Garmin client, caching layer, running tools, demo mode and test suite are the work of **[kavakoss](https://github.com/kavakoss)**. Huge thanks for building it and sharing it as open source under the MIT License. 🙏

This project extends that foundation with richer sleep data (naps, stages, overnight vitals), 10 new tools, and activity weather plus time in HR and power zones. kavakoss's original commit history is preserved in this repository, and their original project was in turn inspired by [Jack-Abyss/claude-garmin](https://github.com/Jack-Abyss/claude-garmin).

## What's New In This Version

- **Richer `get_sleep`** — naps (time, length, Garmin feedback), deep/light/REM/awake stages with ratings, bedtime and wake time, overnight HRV, SpO2, respiration, sleep stress, Body Battery recharge, skin temperature, and Garmin's sleep need with recommended bedtime.
- **10 new tools** — daily activity, Body Battery, daily vitals, training readiness history, performance metrics, race prediction trend, running tolerance, gear, devices, and body metrics (see [MCP Tools](#mcp-tools)).
- **Richer `get_activity_detail`** — weather during the activity (°C, km/h) and minutes in each heart-rate and power zone.
- **Fix** — `get_recovery` reports recovery time in hours (Garmin returns minutes).

## See It In Action

![opencode checking Garmin recovery through the MCP server](docs/assets/demo.gif)

Recorded with `garmin-mcp serve --demo`, which serves deterministic **fictional** sample data — no Garmin credentials and no real health data involved:

```bash
uv run garmin-mcp serve --demo
```

## Works With Any MCP Harness

This server implements standard MCP over `stdio` and Streamable HTTP, so there is no harness-specific adapter to maintain. If your tool speaks MCP, it works:

| Harness | Connection |
|---|---|
| Codex CLI | MCP server config |
| Gemini CLI | MCP server config |
| Cursor, Windsurf, Zed, VS Code Copilot, Continue | MCP server settings (stdio or HTTP) |
| opencode | `mcp` block in `opencode.json` |
| Hermes | stdio or HTTP |
| Other desktop and CLI clients | `mcpServers` stdio config |
| Custom agents, web bots, scripts | Streamable HTTP at `http://127.0.0.1:8765/mcp` |

Any client that supports MCP over `stdio` or Streamable HTTP can use every tool and prompt in this server with no extra code.

## Why This Exists

Most fitness advice from LLMs is generic. This server lets an LLM inspect your real Garmin Connect data first, then answer with context from your activities, recovery markers, HRV, sleep, naps, Body Battery, stress, VO2 Max, and running history.

The core server is provider-neutral. DeepSeek, OpenAI, OpenRouter, and Gemini integrations are optional demo bridges for clients that do not speak MCP directly.

## Features

- **26 Garmin tools and 4 prompt templates** for training, recovery and sleep analysis.
- **Demo mode** — `serve --demo` runs on fictional sample data with no credentials, for try-outs and recordings.
- **Local MCP server** over `stdio` or Streamable HTTP (`/mcp`).
- **Endpoint-level TTL cache with single-flight factories** — concurrent tool calls share one Garmin request instead of stampeding the API.
- **Global request concurrency cap** so parallel snapshots stay within a safe request rate.
- **Partial-error reporting** — composite tools include an `errors` array when an endpoint fails, instead of silently returning nulls.
- **Metric units** — Garmin's imperial weather values and gram-based weights are converted for you.
- **Provider demo bridge** for `deepseek`, `openai`, `openrouter`, and `gemini`.
- **Explicit opt-in** before Garmin health/activity data is sent to any external LLM provider.
- **Mocked test suite** plus optional live Garmin smoke tests.

## Quick Start

```bash
git clone https://github.com/sheloab/GARMIN-Connect-MCP
cd GARMIN-Connect-MCP
uv sync
cp .env.example .env
```

Set Garmin credentials in `.env`:

```env
GARMIN_EMAIL=you@example.com
GARMIN_PASSWORD=your-password
```

Authenticate once (interactive MFA supported):

```bash
uv run garmin-mcp login
uv run garmin-mcp doctor --live
```

List available MCP tools, then run the server:

```bash
uv run garmin-mcp tools
uv run garmin-mcp serve --transport stdio
```

HTTP transport:

```bash
uv run garmin-mcp serve --transport http --host 127.0.0.1 --port 8765
# clients connect to http://127.0.0.1:8765/mcp
```

> Windows (PowerShell) equivalents: `uv sync`, `Copy-Item .env.example .env`, `notepad .env`, then the same `uv run garmin-mcp ...` commands.

## MCP Client Setup

Generic `stdio` config for a local checkout:

```json
{
  "mcpServers": {
    "garmin": {
      "command": "uv",
      "args": [
        "--directory",
        "/path/to/GARMIN-Connect-MCP",
        "run",
        "garmin-mcp",
        "serve",
        "--transport",
        "stdio"
      ]
    }
  }
}
```

Credentials are read from the repo's `.env` file, so no `env` block is needed.

[opencode](https://opencode.ai) (`opencode.json`):

```json
{
  "mcp": {
    "garmin": {
      "type": "local",
      "command": [
        "uv",
        "--directory",
        "/path/to/GARMIN-Connect-MCP",
        "run",
        "garmin-mcp",
        "serve",
        "--transport",
        "stdio"
      ],
      "enabled": true
    }
  }
}
```

Per-client examples (Cursor, Continue, Hermes, generic stdio/HTTP) live in [docs/clients](docs/clients/README.md).

## MCP Tools

### Recovery and sleep

| Tool | Purpose |
|---|---|
| `get_recovery` | Readiness, HRV, sleep, body battery, resting HR, training status if available |
| `get_sleep` | Sleep duration and score, naps, stages, bedtime/wake time, overnight HRV/SpO2/respiration/stress, and sleep need |
| `get_training_readiness_history` | **New.** Daily readiness score with each factor's contribution: sleep, sleep history, recovery time, acute load, stress history, HRV |
| `get_body_battery` | **New.** Charged, drained, high, low, level at wake, sleep recharge, latest level, and the events (sleep, naps, activities) that moved it |
| `get_resting_heart_rate` | Daily RHR with 7-day and 30-day trend summaries, up to 30 days |
| `get_stress` | Daily stress buckets and average stress |
| `get_daily_vitals` | **New.** Daily HR (min/max/resting), SpO2, respiration, time in rest/low/medium/high stress, and altitude |
| `get_health_summary` | Compact recovery, sleep, RHR, and stress snapshot |

### Activity and training

| Tool | Purpose |
|---|---|
| `get_daily_activity` | **New.** Steps vs goal, distance, floors, calories, intensity minutes, active/sedentary time, and weekly intensity minutes vs goal |
| `get_recent_activities` | Recent normalized activity summaries |
| `get_activity_detail` | One activity with training effect, weather, and time in HR and power zones |
| `get_activity_splits` | Per-lap splits with pace, HR, cadence, elevation and HR drift |
| `get_recent_load` | Volume by sport based on recent activities |
| `get_training_load` | Native Garmin Training Load when the device/account exposes it |
| `get_running_tolerance` | **New.** Weekly running impact load as a percentage of Garmin's running tolerance |
| `get_running_summary` | Running summary with pace, HR, longest and fastest run |
| `get_monthly_running_stats` | Monthly running breakdown |

### Performance

| Tool | Purpose |
|---|---|
| `get_fitness` | VO2 Max, FTP, and race predictions |
| `get_performance_metrics` | **New.** Endurance score and level, hill score, fitness age, lactate threshold (HR, pace, running power), heat/altitude acclimation, VO2 Max trend |
| `get_race_prediction_trend` | **New.** Daily 5K/10K/half/marathon predictions and how they changed |
| `get_zones` | Heart-rate zones and thresholds from Garmin user settings |
| `get_personal_records` | Garmin personal records |

### Gear, devices and body

| Tool | Purpose |
|---|---|
| `get_gear` | **New.** Shoes and other gear with distance, activity count, and percent of max distance used |
| `get_devices` | **New.** Registered Garmin devices, firmware version, and last upload |
| `get_body_metrics` | **New.** Weigh-ins and body composition, blood pressure readings, and today's hydration |
| `get_full_snapshot` | Broad multi-section Garmin snapshot |

## MCP Prompts

- `recovery_check`
- `weekly_training_review`
- `activity_analysis`
- `race_plan_context`

## Configuration

All settings come from environment variables (`.env` is loaded automatically).

| Variable | Default | Purpose |
|---|---|---|
| `GARMIN_EMAIL` | — | Garmin Connect account email (required) |
| `GARMIN_PASSWORD` | — | Garmin Connect password (required) |
| `GARMIN_TOKEN_STORE` | `~/.garminconnect` | Directory for the OAuth token cache |
| `GARMIN_CACHE_TTL_SECONDS` | `120` | TTL for cached Garmin endpoint responses |
| `GARMIN_MAX_CONCURRENCY` | `6` | Global cap on in-flight Garmin requests |
| `GARMIN_DEMO` | unset | `1` serves fictional sample data (same as `serve --demo`) |

Provider bridge variables (optional): `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, plus the matching `*_BASE_URL` and `*_MODEL` overrides.

## How It Stays Fast And Quiet

- **Endpoint-level cache.** Sleeping, stress, RHR, and activity fetches are cached per day/limit and shared across every tool that needs them — a `get_full_snapshot` burst reuses the same responses instead of refetching.
- **Single-flight.** If two tool calls ask for the same missing key, only one hits Garmin; the other waits and reuses the result.
- **Failures are never cached.** A failed endpoint is retried on the next call.
- **Concurrency cap.** All outbound Garmin calls pass through one bounded semaphore (`GARMIN_MAX_CONCURRENCY`) so parallel sections cannot flood the API.
- **Visible partial failures.** When an optional endpoint fails, composite tools add an `errors` array (source + exception) to their output.

## Provider Demo Agents

Provider API access may require a paid account or credits even if the provider's web app has a free tier.

```env
DEEPSEEK_API_KEY=sk-...
OPENAI_API_KEY=sk-...
OPENROUTER_API_KEY=sk-or-...
GEMINI_API_KEY=...
```

| Provider | Env key | Default model | API style |
|---|---|---|---|
| DeepSeek | `DEEPSEEK_API_KEY` | `deepseek-v4-pro` | OpenAI-compatible Chat Completions |
| OpenAI | `OPENAI_API_KEY` | `gpt-5` | OpenAI Chat Completions |
| OpenRouter | `OPENROUTER_API_KEY` | `google/gemini-3-flash-preview` | OpenAI-compatible Chat Completions |
| Gemini | `GEMINI_API_KEY` | `gemini-3.6-flash` | Gemini Generate Content API |

```bash
uv run garmin-mcp ask "How is my recovery today?" --provider deepseek --allow-external-health-data
uv run garmin-mcp ask "Summarize my running for 90 days." --provider openai --allow-external-health-data
uv run garmin-mcp ask "What should I focus on this week?" --provider gemini --allow-external-health-data
```

The `--allow-external-health-data` flag is required because Garmin tool results can be sent to the selected provider.

## Privacy And Safety

- Your `.env` file and Garmin token cache stay local and are ignored by git.
- MCP clients that run locally can call Garmin tools without sending provider API keys to this project.
- Provider demo commands such as `garmin-mcp ask` can send Garmin health/activity results to the selected LLM provider, so the CLI requires `--allow-external-health-data`.
- `get_zones` returns only zone/threshold fields from the Garmin profile, not the raw profile (gender, birth date, weight, and similar PII are trimmed).

## Testing

```bash
uv run garmin-mcp doctor
uv run garmin-mcp doctor --live
uv run garmin-mcp tools
uv run ruff check .
uv run pytest
```

CI runs on Linux (Python 3.11, 3.12, 3.13) and Windows (3.12). Optional live tests are gated and never run in CI:

```powershell
$env:GARMIN_LIVE_TEST = "1"
uv run pytest tests/live
```

## Troubleshooting

- **`Garmin rate-limited the request`** — lower `GARMIN_MAX_CONCURRENCY` (for example to `3`) and retry after a few minutes.
- **`Garmin authentication failed`** — check `GARMIN_EMAIL`/`GARMIN_PASSWORD`, then run `uv run garmin-mcp login` from a real terminal (MFA prompts need interactive input).
- **A tool returns `errors`** — the array names the endpoint that failed; the rest of the payload is still valid partial data.
- **A tool returns empty lists** (for example `get_body_metrics`) — nothing was logged in Garmin Connect for that period.

## Garmin Disclaimer

This project uses the community `garminconnect` Python package and Garmin Connect endpoints. Garmin Connect is not a public API for individual open-source projects, and authentication or endpoint behavior may change without notice. This project is not affiliated with or endorsed by Garmin.

Some metrics depend on Garmin device capabilities. For example, a device may expose VO2 Max and Training Effect but not native Garmin Training Load or Training Status. Zone availability depends on what the Garmin user settings endpoint exposes for your account.

## License

This project is licensed under the [MIT License](LICENSE).

Original work © 2026 [kavakoss](https://github.com/kavakoss) — [kavakoss/mcp-garmin-connect](https://github.com/kavakoss/mcp-garmin-connect). Extensions © 2026 [sheloab](https://github.com/sheloab).
