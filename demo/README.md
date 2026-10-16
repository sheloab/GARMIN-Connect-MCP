# Recording the README demo GIF

The demo runs `mcp-garmin-connect` in **demo mode** (`serve --demo`): a deterministic client with fictional sample data (no Garmin credentials, no real health data). Config for this folder lives in [`opencode.json`](opencode.json), which overrides the `garmin` MCP entry with the demo instance.

## What to record (shot list)

One question, one answer — 20–35 seconds after speeding up:

1. **(2s)** Terminal prompt `demo/` with the command typed
2. **(2s)** `opencode run "My legs feel tired today. Check my Garmin recovery and tell me: easy day or intervals?"`
3. **(10–20s)** Tool calls appear: `get_recovery`, `get_sleep`, `get_resting_heart_rate`, `get_recent_load` — this is the proof it reads real API data
4. **(5–10s)** Final answer with specifics: readiness 78 (moderate), RHR 58, HRV 44 balanced, sleep 7.4h → recommendation

Target: < 5 MB, dark theme, no visible personal data (demo data is fictional: athlete `demo-athlete`, PR 5K 28:10).

## Option 1 — VHS (automated, reproducible)

```bash
# once
curl -fsSL -o ~/.local/bin/ttyd https://github.com/tsl0922/ttyd/releases/latest/download/ttyd.x86_64
chmod +x ~/.local/bin/ttyd
GOBIN=$HOME/.local/bin go install github.com/charmbracelet/vhs@latest

# every time
cd demo && vhs demo.tape
```

`demo.tape` records `docs/assets/demo.gif`. Tweak `TypingSpeed`, `PlaybackSpeed`, and the final `Sleep` to taste.

## Option 2 — manual screen recording

1. Smoke-test the demo first: `cd demo && opencode run "hello, which Garmin tools do you have?"`
2. Record the terminal window (Windows: [ScreenToGif](https://www.screentogif.com/) or Xbox Game Bar `Win+G`; Linux: Peek/Kooha)
3. Run the shot list command above; stop recording when the answer finishes
4. Convert/compress if you have MP4:

```bash
ffmpeg -i recording.mp4 \
  -vf "fps=12,scale=1000:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse" \
  -loop 0 docs/assets/demo.gif
```

## Where it goes

The main [README](../README.md) embeds `docs/assets/demo.gif` in the "See It In Action" section. Keep the GIF under 5 MB so GitHub loads it fast.
