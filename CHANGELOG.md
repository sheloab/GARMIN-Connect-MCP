## 0.4.0 — 2026-10-28

The Personal API is rate-limited far harder than the undocumented endpoints: a
burst of 12 parallel requests comes back as `429` with `Retry-After: 14`. The
per-day tool loops (30 calls for 30 days of RHR) blew that budget instantly, and
`get_full_snapshot` silently fell back to email/password for half its sections.

### Added
- **Batched range reads.** Sleep, HRV, resting HR and stress are fetched once per
  rolling window (`/dailySleepSummariesByDate`, `/hrv/daily/{from}/{to}`,
  `/v2/dailysummaries`, `/stats/stress/daily/{from}/{to}`) and served from an
  in-memory window with a 15-minute TTL. Measured on a live account: **74
  day-reads cost 8 requests instead of 74**, and a full snapshot costs 15.
- Per-endpoint range caps, because Garmin's maximum differs per endpoint
  (verified: sleep 31 days OK / 35 rejected, stress 28 OK / 31 rejected,
  dailysummaries and HRV 35 OK). `GARMIN_PERSONAL_BATCH_DAYS` (default 28),
  `GARMIN_PERSONAL_BATCH_TTL_SECONDS` (900), `GARMIN_PERSONAL_MAX_CONCURRENCY` (2).
- `429` handling honours `Retry-After` and retries twice before giving up, and the
  Personal API client caps its own concurrency.

### Changed
- `supports_derived_metrics` capability flag: on the Personal API, derived-metric
  lookups stop after one clear "not exposed by this tier" instead of probing ~40
  activity dates and collecting an identical error from each. Snapshot error
  entries dropped from ~60 to 6 on a live account.

### Verified
- Sleep hours/score, resting HR and stress match the email/password backend
  **exactly**, day for day, over 5 days.

## 0.3.2 — 2026-10-28

### Fixed
- **An expired Personal API key no longer takes the server down.** In hybrid mode
  a failed Personal API login replaced the whole client, so `kava-life` and any
  MCP client lost all Garmin data until the key was rotated. Hybrid now degrades to
  email/password with a logged warning; shared reads fall back and personal-only
  tools report the reason instead of killing startup.

# Changelog

## 0.3.1 — 2026-10-28

Fixes from testing 0.3.0 against a real Garmin account — the Personal API does
not answer the way its Swagger prose suggests.

### Fixed
- **Token exchange rejected the real response.** Garmin returns the bearer token
  as a **bare string with `content-type: text/plain`**, not JSON, so every
  authenticated call failed with "Personal API returned a non-JSON token
  response". The parser now accepts a plain-text token, a JSON body and a JSON
  string, and rejects HTML/error pages instead of treating them as a token.
- **Resting HR was always null.** The value is at
  `data[0].heartRate.restingValue` in `/usersummary-service/v2/dailysummaries`,
  not at the top level. Falls back to `restingHeartRate` and to the metrics map.
- **Stress was always null.** `/stats/stress/daily` nests durations under
  `values` and has no maximum; `/dailysummaries` has average + maximum but no
  durations. Both are merged now, and the missing activity bucket is derived from
  the tracked-day proportion.
- **`get_activity_splits` returned one whole-activity lap.** The splits endpoint
  is 404 on this tier and `splitSummaries` collapse to a single entry. Per-km
  laps are now rebuilt from the `/details` sample stream (boundary-interpolated
  pace, HR mean/max per lap, cadence and elevation), matching Garmin's own lap
  output to within a second per km. Falls back to the collapsed summary clearly
  labelled, never passed off as laps.
- Cadence averaged every cadence key together (double cadence 165 spm plus
  per-leg 82 spm reported as 123); elevation summed raw sample noise (24 m for an
  11 m climb). Both fixed: one cadence key per sample, 0.5 m elevation deadband.

### Notes
- `get_body_composition` only reports what is logged in Garmin Connect. An
  account that never enters weight there gets an empty series (not an error).
- Shapes above were verified against a live account on 2026-10-28; `--probe`
  stays the way to re-check if Garmin changes them again.

## 0.3.0 — 2026-10-27

### Added
- **Official Garmin Connect Personal API backend.** Set `GARMIN_PERSONAL_API_KEY`
  (create one at <https://connect.garmin.com/app/settings/personalApiToken>) and
  `GARMIN_AUTH_MODE=auto|personal|unofficial`. Keys are exchanged for a 24-hour
  bearer token, cached at `~/.garminconnect/personal_api_token.json` (mode 0600),
  keyed by a hash of the API key, with one automatic refresh on `401`.
  See `docs/personal-api.md`.
- **`GARMIN_AUTH_MODE=hybrid`** runs both backends in one process: wellness and
  raw-file reads go to the official API, Garmin-derived metrics (training
  status/load, readiness, VO2 Max, race predictions, PRs) go to email/password,
  with automatic fallback when a read fails. Auth errors are never masked.
  `serve --auth-mode hybrid` overrides it for a single run.
- `get_full_snapshot` now tags each section with `data_source` (plus
  `source_detail` for mixed sections), lists `hybrid_fallbacks`, and exposes
  `auth_backend` / `section_sources`. Attribution survives the endpoint cache and
  the multi-threaded day loops, so a fallback is visible instead of silent.
- Six new tools, available on the Personal API backend: `get_body_composition`,
  `get_spo2`, `get_hydration`, `get_steps_history`, `get_fitness_age`, and
  `download_activity_file` (original `.FIT` inside a ZIP).
- `garmin-mcp personal-api --check | --refresh-token | --probe PATH` to validate
  the key, rotate the cached token, and dump raw endpoint payloads.
- `doctor` reports the active backend, key presence and token cache, plus
  `personal-api-live` with `--live`.

### Changed
- Metrics the official API does not expose (training status/load, readiness,
  VO2 Max, race predictions, personal records) now degrade to `null` with an
  explanatory `errors` entry instead of failing the whole call.
- README tool count corrected (it said 15 while 18 were registered) and the tool
  table now marks Personal-API-only tools.

### Notes
- Garmin strips some computed fields (VO2 Max, recovery time, performance
  condition) from files delivered through its API, so `download_activity_file`
  is not byte-identical to a manual Garmin Connect download.

## 0.2.0 — 2026-09-29

- `get_activity_splits` with per-lap pace, HR, cadence, elevation and HR drift.
- Offline demo mode (`serve --demo`) with fictional data.
- Paged activity history, configurable caps, `recovery_time_hours` unit fix (#3).
- Explicit `start_date`/`end_date` windows for activity queries.
- `get_hrv_history` and `get_body_battery_range` for longer trend windows.
