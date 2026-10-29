# Garmin Connect Personal API backend

`mcp-garmin-connect` can read Garmin data two ways. This page covers the official
one; the other is the older email/password flow via the `garminconnect` package.

| | **personal** (official) | **unofficial** (email/password) |
|---|---|---|
| Auth | API key → 24 h bearer token | Garmin SSO login, cached tokens |
| Stability | Documented endpoints, rate-limit friendly | Breaks when SSO/Cloudflare changes |
| Raw activity file (.FIT) | ✅ `download_activity_file` | ❌ |
| Sleep score, HRV, RHR, stress, body battery, SpO2, weight, hydration, steps, HR zones, fitness age | ✅ | ✅ |
| Training status / training load | ❌ | ✅ |
| Training readiness | ❌ | ✅ |
| VO2 Max, race predictions, cycling FTP | ❌ | ✅ |
| Personal records | ❌ | ✅ |
| Write/push (workouts, courses) | ❌ (not in any self-serve tier) | ❌ |

> Since October 2026 Garmin strips some fields it computes (VO2 Max, recovery
> time, performance condition) from files delivered through its API, so a .FIT
> fetched with `download_activity_file` is not byte-identical to a manual
> download from Garmin Connect.

## 1. Create a key

1. Log in to [Garmin Connect](https://connect.garmin.com) in a browser.
2. Open <https://connect.garmin.com/app/settings/personalApiToken>.
3. **Create Key** → accept the terms → choose an expiry (30 / 90 / 180 / 365 days).
4. Copy the key immediately: Garmin shows it **once**. Treat it like a password.

There is no application or approval step. Revoke or rotate at the same page.

## 2. Configure

```env
GARMIN_AUTH_MODE=personal
GARMIN_PERSONAL_API_KEY=your-key-here
```

Store it in `.env` (git-ignored) or your secret manager — never commit it.

`GARMIN_AUTH_MODE` choices:

- `auto` (default) — use `personal` when a key is present, else `unofficial`.
- `personal` — official API only; derived metrics come back empty with an
  explanatory `errors` entry.
- `unofficial` — ignore the key and use email/password.
- `hybrid` — run **both** (see below).

## Hybrid mode: both backends in one process

`GARMIN_AUTH_MODE=hybrid` needs both credential sets and routes every call to the
backend that actually owns that data:

| Reads | Backend |
|---|---|
| sleep, HRV, resting HR, stress, body battery | personal |
| activities, activity detail, splits, activity file (.FIT) | personal |
| weight/body composition, SpO2, hydration, steps, fitness age | personal |
| training status / load, training readiness | unofficial |
| VO2 Max, race predictions, cycling FTP, personal records | unofficial |

If the preferred backend fails for a read, the call falls back to the other one,
so a Cloudflare hiccup on the email/password side still lets wellness data flow
and vice versa. Auth errors (`PersonalApiAuthError`, expired key) are **never**
masked by the fallback — they surface so you can rotate the key.

```env
GARMIN_AUTH_MODE=hybrid
GARMIN_PERSONAL_API_KEY=your-key-here
GARMIN_EMAIL=you@example.com
GARMIN_PASSWORD=your-password
```

Hybrid mode is visible in the output rather than silent: `get_full_snapshot`
tags every section with `data_source` (`personal`, `unofficial`, or
`personal+unofficial`), keeps a `source_detail` map of endpoint → backend for
mixed sections, lists `hybrid_fallbacks`, and reports `auth_backend` plus
`section_sources` at the top level. Attribution survives the endpoint cache, so
a section served from cache still says which backend filled it.

Check it with:

```bash
garmin-mcp doctor --live          # reports the hybrid backend and both live paths
garmin-mcp serve --auth-mode hybrid   # one-off override without touching .env
```

## 3. Verify

```bash
garmin-mcp personal-api --check          # token + one sample per core endpoint
garmin-mcp personal-api --refresh-token  # discard the cached bearer token
garmin-mcp doctor --live                 # includes personal-api-live
```

`--check` prints one line per endpoint so a partially available account is
obvious. To inspect a raw payload (useful when Garmin changes a schema):

```bash
garmin-mcp personal-api --probe /sleep-service/sleep/dailySleep --params date=2026-10-26
```

## 4. How auth works

```
POST https://personal.connectapi.garmin.com/personalapitoken-service/personal-api/personal-api-key/token
Authorization: ApiKey <your-key>
→ { "access_token": "<JWT, 24 h>" }
```

- No refresh tokens: when a token is close to expiring the server simply asks for
  a new one.
- The bearer token is cached in `GARMIN_PERSONAL_TOKEN_CACHE`
  (default `~/.garminconnect/personal_api_token.json`, mode `0600`). The cache is
  keyed by a hash of the API key, so rotating the key cannot serve a stale token.
- A `401` on a data call forces one token refresh and retries once.
- Key expiry produces `PersonalApiAuthError` with a pointer back to the key page.

## 5. Tools and their backend

Personal-only (return `"error": "personal api required"` on the unofficial
backend):

- `get_body_composition` — weight, BMI, body fat/muscle/water
- `get_spo2` — daily blood oxygen
- `get_hydration` — logged fluid intake
- `get_steps_history` — daily steps vs goal
- `get_fitness_age` — Garmin fitness age
- `download_activity_file` — original .FIT (ZIP) per activity

Everything else works on both backends, except the derived metrics listed in the
table at the top: on the personal backend those fields come back `null` with an
`errors` entry naming the missing metric, instead of failing the whole call.

## 6. Endpoints used

| Tool(s) | Endpoint |
|---|---|
| sleep, recovery, health summary | `/sleep-service/sleep/dailySleep?date=` |
| HRV, recovery | `/hrv-service/hrv/{date}` |
| resting HR | `/usersummary-service/v2/dailysummaries?startDate=&endDate=` |
| stress | `/usersummary-service/stats/stress/daily/{date}/{date}` |
| body battery | `/wellness-service/wellness/bodyBattery/reports/daily` |
| activities | `/activitylist-service/activities/search/activities` |
| activity detail | `/activity-service/activity/{id}` |
| per-km splits | `/activity-service/activity/{id}/details` (rebuilt from the sample stream; Garmin's own splits endpoint is 404 on this tier) |
| activity file | `/download-service/files/activity/{id}` |
| weight / body composition | `/weight-service/weight/range/{start}/{end}` |
| SpO2 | `/wellness-service/wellness/daily/spo2acclimation/{date}` |
| hydration | `/usersummary-service/stats/hydration/daily/{start}/{end}` |
| steps | `/usersummary-service/stats/steps/daily/{start}/{end}` |
| fitness age | `/fitnessage-service/fitnessage/v2/{date}` |
| HR zones | `/biometric-service/heartRateZones/v2` |
| profile | `/userprofile-service/userprofile/settings` |

Full reference: <https://developer.garmin.com/gc-developer-program/personal/swagger-docs/>.

## Rate limits and batching

The Personal API is rate-limited hard: 12 parallel requests return `429` with
`Retry-After: 14`. The server therefore:

- caps its own concurrency (`GARMIN_PERSONAL_MAX_CONCURRENCY`, default 2);
- honours `Retry-After` and retries twice before raising;
- reads **one range per endpoint** instead of one call per day, cached in memory
  for `GARMIN_PERSONAL_BATCH_TTL_SECONDS` (default 900).

Range length is capped by Garmin, differently per endpoint — sleeping past the
limit returns `400`, not a smaller result:

| Endpoint | Verified max |
|---|---|
| `/sleep-service/sleep/dailySleepSummariesByDate` | 31 days |
| `/usersummary-service/stats/stress/daily/{from}/{to}` | 28 days |
| `/usersummary-service/v2/dailysummaries` | 35 days |
| `/hrv-service/hrv/daily/{from}/{to}` | 35 days |

`GARMIN_PERSONAL_BATCH_DAYS` defaults to 28, which fits all four. Days older than
the window fall back to single-day calls.

## Verified response shapes (2026-10-28)

The Swagger pages describe fields but not the envelope, and three of them bite:

| Endpoint | What actually arrives |
|---|---|
| token exchange | **bare JWT string**, `content-type: text/plain` (not JSON) |
| `/usersummary-service/v2/dailysummaries` | `{"data": [{...}]}`; resting HR at `data[0].heartRate.restingValue`, stress at `data[0].stress.{avgLevel,maxLevel}` |
| `/usersummary-service/stats/stress/daily/{d}/{d}` | `[{"values": {...}}]` — durations only, no maximum |
| `/activity-service/activity/{id}/splits` | **404**; `splitSummaries` in the activity payload is a single whole-activity lap |
| `/activity-service/activity/{id}/details` | per-sample stream (`sumDistance`, `sumDuration`, `directHeartRate`, `directDoubleCadence`, `directElevation`) — laps are rebuilt from it |
| `/weight-service/weight/range/...` | `dailyWeightSummaries` is empty unless you log weight in Garmin Connect |

Re-check any of these with:

```bash
garmin-mcp personal-api --probe /usersummary-service/v2/dailysummaries \
  --params startDate=2026-10-27,endDate=2026-10-27
```
