# Recording demos (opencode + Claude Code)

The demo runs `mcp-garmin-connect` in **demo mode** (`serve --demo`): a deterministic client with fictional sample data (no Garmin credentials, no real health data). Everything in this folder is wired for that:

| File | For |
|---|---|
| [`opencode.json`](opencode.json) | opencode project config (overrides `garmin` MCP with the demo instance) |
| [`.mcp.json`](.mcp.json) | Claude Code project-scoped MCP config (same demo instance) |
| [`.claude/settings.json`](.claude/settings.json) | Pre-allows the 15 Garmin tools so Claude Code never stops for permission prompts while recording |
| [`demo.tape`](demo.tape) | VHS script used for the current `docs/assets/demo.gif` |

Demo facts on screen: athlete `demo-athlete`, RHR 58, HRV 44 ms BALANCED, sleep 7.4 h / score 82, readiness 78 MODERATE, VO2max 47, PR 5K 28:10.

## 0. Prep checklist (once, ~5 min)

- [ ] `cd demo && uv run --directory .. garmin-mcp doctor` shows `[OK] demo-mode`
- [ ] opencode: `opencode run "List the Garmin tools you can call. Do not call them."` → lists `get_recovery`, `get_sleep`, …
- [ ] Claude Code: `claude` inside `demo/` → approve the folder trust prompt and the project MCP server `garmin` once → `/mcp` shows `garmin` connected
- [ ] Terminal: font ≥ 15px, window ~1100×620, dark theme, prompt without long paths
- [ ] Close notifications / other windows

## 1. Pick one prompt per GIF

One question, one answer. Keep each recording 15–40 s. Prompts that work well with the demo data:

| # | Prompt | Shows off |
|---|---|---|
| 1 | `My legs feel tired today. Check my Garmin recovery and tell me: easy day or intervals?` | `get_recovery`, `get_health_summary` — fastest, best opener |
| 2 | `Review my last 7 days of training: volume, intensity, recovery. What should I change next week?` | `get_running_summary`, `get_recent_load`, `get_training_load` |
| 3 | `My sleep has been messy this week. Check sleep, stress and RHR trends and tell me what's hurting my recovery most.` | `get_sleep`, `get_stress`, `get_resting_heart_rate` |
| 4 | `I want a sub-30 5K. Check my fitness, race predictions and recent runs — am I on track?` | `get_fitness`, `get_running_summary`, `get_personal_records` |
| 5 | `Run a full checkup on my training and recovery and give me the top 3 things to fix.` | `get_full_snapshot` (longest, use only if you want a 40 s demo) |

Expected on screen: the typed prompt → tool calls with JSON args → an answer that quotes the demo numbers. That combination is what proves it's real data, not a prompt template.

## 2. Recording — opencode

**Automated (VHS).** `demo.tape` is already tuned; rerun or edit it:

```bash
cd demo && vhs demo.tape     # writes ../docs/assets/demo.gif
```

Tweak in the tape: `PlaybackSpeed` (higher = shorter GIF), `TypingSpeed`, `Set WaitTimeout` (raise if your model is slow), and the prompt on the `Type "opencode run '…'"` line.

**Manual.** `opencode run '<prompt>'` in a terminal, capture the window (ScreenToGif / Xbox Game Bar `Win+G`), trim to command → answer, speed up 1.5–2×.

## 3. Recording — Claude Code

Launch from this folder so both `.mcp.json` and the pre-approved tool permissions apply:

```bash
cd demo && claude
```

- First run only: approve the folder and the project MCP server `garmin` when asked.
- Then type your prompt (same list as above) and let it call the tools. `/mcp` should show `garmin` connected; the server command contains `--demo`.
- If your user-level `garmin` MCP (real data) takes precedence, force this config instead:

```bash
claude --strict-mcp-config --mcp-config .mcp.json \
  --allowedTools "mcp__garmin__get_recovery" "mcp__garmin__get_sleep" "mcp__garmin__get_health_summary" ...
```

Headless smoke test (no recording): 

```bash
claude -p "Use the garmin tool get_recovery and reply with only the resting_heart_rate_bpm value." \
  --strict-mcp-config --mcp-config .mcp.json --allowedTools "mcp__garmin__get_recovery"
```

Record the interactive session manually (ScreenToGif / Game Bar) — the TUI shows tool calls live, which is exactly what you want on screen.

## 4. Exporting a small GIF

If you recorded MP4:

```bash
ffmpeg -i recording.mp4 \
  -vf "fps=12,scale=1000:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse" \
  -loop 0 demo-claude.gif
```

Target: width ~1000–1100 px, 15–40 s, **under 5 MB** (GitHub loads it inline above that it gets slow).

## 5. Publish

- Main GIF: replace `docs/assets/demo.gif` (referenced by the main [README](../README.md#see-it-in-action))
- Second GIF: save as `docs/assets/demo-claude.gif` and add next to the first one in the README
- Commit + push, then re-share the repo link (social preview + GIFs do the heavy lifting)
