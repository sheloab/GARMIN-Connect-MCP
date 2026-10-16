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

This project extends that foundation with richer sleep data (naps, stages, overnight vitals), 20 new tools, and activity weather plus time in HR and power zones. The full original history lives in kavakoss's repository linked above.

## What's New In This Version

- **Richer `get_sleep`** — naps (time, length, Garmin feedback), deep/light/REM/awake stages with ratings, bedtime and wake time, overnight HRV, SpO2, respiration, sleep stress, Body Battery recharge, skin temperature, and Garmin's sleep need with recommended bedtime.
- **20 new tools** — per-km splits from the raw recording, run/walk/stop segments, night-by-night sleep timeline, hour-by-hour day timeline, 90-day HRV trend, 52-week trends, daily activity, Body Battery, daily vitals, training readiness history, performance metrics, race prediction trend, running tolerance, saved workouts, yearly totals, badges, gear, devices, and body metrics (see [MCP Tools](#mcp-tools)).
- **Richer existing tools** — real HR and power zone ranges in `get_zones`, shoe usage in `get_gear`, watch alarms in `get_devices`.
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

- **36 Garmin tools and 4 prompt templates** for training, recovery and sleep analysis.
- **Demo mode** — `serve --demo` runs on fictional sample data with no credentials, for try-outs and recordings.
- **Local MCP server** over `stdio` or Streamable HTTP (`/mcp`).
- **Endpoint-level TTL cache with single-flight factories** — concurrent tool calls share one Garmin request instead of stampeding the API.
- **Global request concurrency cap** so parallel snapshots stay within a safe request rate.
- **Partial-error reporting** — composite tools include an `errors` array when an endpoint fails, instead of silently returning nulls.
- **Metric units** — Garmin's imperial weather values and gram-based weights are converted for you.
- **Provider demo bridge** for `deepseek`, `openai`, `openrouter`, and `gemini`.
- **Explicit opt-in** before Garmin health/activity data is sent to any external LLM provider.
- **Mocked test suite** plus optional live Garmin smoke tests.

## How It Works

[MCP (Model Context Protocol)](https://modelcontextprotocol.io/) is an open standard that lets AI assistants call local "tools". This project is an MCP server: it runs on your own computer, logs in to Garmin Connect with your account, and exposes your data as tools your AI client can call when you ask a question.

```text
Garmin watch ──sync──▶ Garmin Connect app ──▶ Garmin Connect (cloud)
                                                     │
                                         GARMIN Connect MCP (runs locally)
                                                     │  MCP tools
                                                     ▼
                                 Your AI client (Cursor, Codex CLI, Gemini CLI, opencode, …)
```

Your Garmin password and login tokens stay on your machine. The AI client only sees the tool results it asks for.

## Getting Started (Step by Step)

### 1. Install the prerequisites

| You need | Why | How |
|---|---|---|
| **Git** | Download this repository | [git-scm.com/downloads](https://git-scm.com/downloads) |
| **uv** | Installs Python 3.11+ and all dependencies for you | macOS/Linux: `curl -LsSf https://astral.sh/uv/install.sh \| sh`<br>Windows (PowerShell): `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` |
| **A Garmin Connect account** | The data source (not needed for demo mode) | Your watch should be syncing to the Garmin Connect app |
| **An MCP client** | The AI you will chat with | Any client from [Works With Any MCP Harness](#works-with-any-mcp-harness) |

You do not need to install Python yourself; `uv` downloads a suitable version automatically.

### 2. Download the code

```bash
git clone https://github.com/sheloab/GARMIN-Connect-MCP
cd GARMIN-Connect-MCP
uv sync
```

### 3. Try it without a Garmin account (optional)

Demo mode serves **fictional** sample data, so you can check everything works before entering any credentials:

```bash
uv run garmin-mcp tools          # lists all tools and prompts
uv run garmin-mcp serve --demo   # starts the server on fictional data (Ctrl+C to stop)
```

To try demo mode inside your AI client, add `"--demo"` to the end of the `args` list in the client config shown in [MCP Client Setup](#mcp-client-setup).

### 4. Add your Garmin credentials

Copy the example settings file and fill in your Garmin Connect email and password:

```bash
cp .env.example .env              # Windows PowerShell: Copy-Item .env.example .env
```

```env
GARMIN_EMAIL=you@example.com
GARMIN_PASSWORD=your-password
```

`.env` is listed in `.gitignore`, so it is never committed. Leave the provider keys empty unless you plan to use the [provider demo agents](#provider-demo-agents).

### 5. Log in once

```bash
uv run garmin-mcp login
```

Run this in a normal terminal: if your account uses two-factor authentication, Garmin sends a code and the command asks you to type it. The login tokens are saved in `~/.garminconnect` (change with `GARMIN_TOKEN_STORE`), so later starts do not need to log in again.

### 6. Check the setup

```bash
uv run garmin-mcp doctor --live
```

Every required line should say `OK`. `WARN` lines for provider keys are fine if you do not use the provider demo agents.

### 7. Connect your AI client

Add the server to your client's MCP configuration (see [MCP Client Setup](#mcp-client-setup) below), replacing `/path/to/GARMIN-Connect-MCP` with the folder you cloned. Restart the client, and the Garmin tools appear in its tool list.

You can also run the server yourself, over stdio or HTTP:

```bash
uv run garmin-mcp serve --transport stdio
uv run garmin-mcp serve --transport http --host 127.0.0.1 --port 8765
# HTTP clients connect to http://127.0.0.1:8765/mcp
```

### 8. Ask your first questions

Just ask in plain language; the AI picks the right tools. For example:

| Ask | Tools it will likely use |
|---|---|
| "How recovered am I today? Should I train hard?" | `get_recovery`, `get_training_readiness_history` |
| "Analyze last night's sleep. Why was my REM low?" | `get_sleep`, `get_sleep_detail` |
| "Break my last run down per kilometre." | `get_recent_activities`, `get_activity_distance_splits` |
| "Did I stop or walk during my last run?" | `get_activity_segments` |
| "What drained my Body Battery yesterday?" | `get_body_battery`, `get_daily_timeline` |
| "How has my HRV trended over the last 3 months?" | `get_hrv_trend`, `get_long_term_trends` |
| "Am I increasing my running load too fast?" | `get_running_tolerance`, `get_training_load` |
| "Plan an easy run for tonight in my real HR zones." | `get_zones`, `get_recovery` |
| "How many km have my shoes done?" | `get_gear` |
| "How much have I run this year?" | `get_yearly_totals` |

### Keeping the data fresh

The server reads what is in Garmin Connect, not what is on your watch. **Sync your watch in the Garmin Connect app before asking**, otherwise you get the data from the last sync (`get_devices` shows the last upload time). Responses are cached for 2 minutes (`GARMIN_CACHE_TTL_SECONDS`), so after syncing, wait a moment before asking again.

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
| `get_sleep_detail` | **New.** One night in detail: stage-by-stage timeline with clock times, hourly HR/HRV/SpO2/stress/Body Battery, minutes of SpO2 below 90%, restless moments, breathing disruptions |
| `get_hrv_trend` | **New.** Up to 90 nights of HRV with 5-minute high, status, personal baseline range, and nights below baseline |
| `get_daily_timeline` | **New.** Hour-by-hour HR, stress, Body Battery, SpO2 and respiration for any day, plus auto-detected activities |
| `get_health_summary` | Compact recovery, sleep, RHR, and stress snapshot |

### Activity and training

| Tool | Purpose |
|---|---|
| `get_daily_activity` | **New.** Steps vs goal, distance, floors, calories, intensity minutes, active/sedentary time, and weekly intensity minutes vs goal |
| `get_recent_activities` | Recent normalized activity summaries |
| `get_activity_detail` | One activity with training effect, weather, and time in HR and power zones |
| `get_activity_splits` | Per-lap splits with pace, HR, cadence, elevation and HR drift |
| `get_activity_distance_splits` | **New.** Per-km (or custom distance) splits computed from the second-by-second recording, independent of auto-lap: pace, grade-adjusted pace, HR, cadence, stride, ground contact, power, elevation, split pattern and aerobic decoupling |
| `get_activity_segments` | **New.** Garmin's run/walk/stand detection within an activity, with paused time |
| `get_recent_load` | Volume by sport based on recent activities |
| `get_training_load` | Native Garmin Training Load when the device/account exposes it |
| `get_running_tolerance` | **New.** Weekly running impact load as a percentage of Garmin's running tolerance |
| `get_running_summary` | Running summary with pace, HR, longest and fastest run |
| `get_monthly_running_stats` | Monthly running breakdown |
| `get_yearly_totals` | **New.** Year-to-date totals per sport: count, distance, time, average and longest |
| `get_workouts` | **New.** Saved workouts in your Garmin Connect library |
| `get_workout_detail` | **New.** Step-by-step structure of a saved workout with pace (min/km), HR, power and cadence targets |

### Performance

| Tool | Purpose |
|---|---|
| `get_fitness` | VO2 Max, FTP, and race predictions |
| `get_performance_metrics` | **New.** Endurance score and level, hill score, fitness age, lactate threshold (HR, pace, running power), heat/altitude acclimation, VO2 Max trend |
| `get_race_prediction_trend` | **New.** Daily 5K/10K/half/marathon predictions and how they changed |
| `get_zones` | Heart-rate zone ranges (bpm) per sport with max/resting/threshold HR, running power zones, and thresholds |
| `get_long_term_trends` | **New.** Up to 52 weeks of weekly steps, stress, HRV, resting HR, sleep, SpO2 and respiration with recent-vs-earlier comparison |
| `get_personal_records` | Garmin personal records |

### Gear, devices and body

| Tool | Purpose |
|---|---|
| `get_gear` | **New.** Shoes and other gear: distance, activity count, percent of max distance and km remaining, default activity types, last use, last-30-day usage |
| `get_devices` | **New.** Registered Garmin devices, firmware version, last upload, and alarms |
| `get_badges` | **New.** Earned badges and points, badges in progress, and joined challenges |
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
- Activity tools never return GPS coordinates, and workout/badge tools drop your Garmin display name and profile image, so routes and identity stay out of LLM conversations.

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
- **Today's data looks old** — sync the watch in the Garmin Connect app, wait a minute, and ask again; `get_devices` shows when the watch last uploaded.
- **The AI client does not show the Garmin tools** — check the `--directory` path in the client config points to your clone, then fully restart the client.

## Garmin Disclaimer

This project uses the community `garminconnect` Python package and Garmin Connect endpoints. Garmin Connect is not a public API for individual open-source projects, and authentication or endpoint behavior may change without notice. This project is not affiliated with or endorsed by Garmin.

Some metrics depend on Garmin device capabilities. For example, a device may expose VO2 Max and Training Effect but not native Garmin Training Load or Training Status. Zone availability depends on what the Garmin user settings endpoint exposes for your account.

## Contributing

Contributions are welcome. To add or change a tool:

1. Write the tool function in `src/mcp_garmin_connect/tools.py`. Fetch Garmin data through `_endpoint(...)` so it is cached and failures are reported in an `errors` array instead of crashing.
2. Register it in `TOOL_SPECS` at the bottom of the same file, with a clear docstring (it becomes the tool description the AI reads) and a JSON schema for its parameters.
3. Add a test in `tests/test_tools.py` using the `FakeGarmin` client with **fictional** values, and update the tool count in `tests/test_server.py`.
4. Run `uv run ruff check .` and `uv run pytest` before opening a pull request.

Please keep personal data out of contributions: no real Garmin IDs, GPS coordinates, health values or credentials in code, tests or issues.

## License

This project is licensed under the [MIT License](LICENSE).

Original work © 2026 [kavakoss](https://github.com/kavakoss) — [kavakoss/mcp-garmin-connect](https://github.com/kavakoss/mcp-garmin-connect). Extensions © 2026 [sheloab](https://github.com/sheloab).
