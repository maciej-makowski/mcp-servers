# Fixtures — live garmin MCP captures (2026-11-13)

House convention: recorded fixtures, no network in tests. Every file here is
a real response captured through the deployed garmin server's own tools
(`mcp__garmin__*`, gateway → 127.0.0.1:8017 → the official Personal API),
paced in small batches, zero 429/403 observed. They seed the store in tests
so the §5.2 read path is provable against the exact shapes the tools serve
today (design doc §2.3, §4.5 last bullet, §7.4).

| file | capture | window | notes |
|---|---|---|---|
| api_status_live.json | garmin_api_status | - | the pre-store envelope (mode live-read-v1); the store block is additive on top of exactly these fields |
| search_activities_page1.json | garmin_recent_activities(limit=5) | newest 5 | the capture returned 5 full search records (~35 KB); this fixture keeps record 1 verbatim and trims records 2-5 (same shape, walking/running) to keep the repo lean — the store test seeds only this record |
| activity_detail_24997020730.json | garmin_activity(24997020730) | - | detail DTO shape (differs from the search record shape) |
| activity_workouts_24997020730.json | garmin_workout_usage(24997020730) | - | the empty-list shape (a real response) |
| activity_file_summary.json | garmin_activity_file(24997020730) | - | the summary-only envelope; sha256 6b9f6a46…, 106357 B (matches P7) |
| daily_steps.json | garmin_daily 2026-10-15..2026-11-12 | 29 d | TWO live chunks: [10-15..11-11] = the 28-day cap window exactly, plus the [11-12..11-12] remainder — exercises the cap split |
| daily_hrv.json | garmin_daily 2026-11-11..2026-11-12 | 2 d | the dict shape ({hrvSummaries: [...], userProfilePk}); a 30-day pull (10-14..11-12, exactly the 30-day cap) was verified live as ONE chunk before this lean capture |
| daily_sleep.json | garmin_daily 2026-11-10..2026-11-12 | 3 d | list-of-day-records shape with sleepNeed/nextSleepNeed sub-objects |
| daily_stress.json | garmin_daily 2026-11-10..2026-11-12 | 3 d | list of {calendarDate, values{…}} records |
| daily_hydration.json | garmin_daily 2026-11-10..2026-11-12 | 3 d | the EMPTY response shape (no hydration data on this account; present: false) |
| daily_weight.json | garmin_daily 2026-11-10..2026-11-12 | 3 d | dict shape with an empty dailyWeightSummaries list + range aggregates (totalAverage/previousDateWeight) — deliberately NOT date-sliceable: the read path replays it raw only when the request window equals the stored chunk window, and falls back live otherwise |
| daily_body_battery.json | garmin_daily 2026-11-11..2026-11-12 | 2 d | list records keyed by "date" (not calendarDate); the 31-day auto-split (2 chunks) was verified live on 2026-11-08 per the design's smoke evidence |
| daily_daily.json | garmin_daily 2026-11-11..2026-11-12 | 2 d | dict shape {"data": [day records]} |
| daily_perday_spo2.json | garmin_daily 2026-11-11..2026-11-12 | 2 d | per-day family: each day's data IS the raw single-date response |
| daily_perday_fitness_age.json | garmin_daily 2026-11-11..2026-11-12 | 2 d | per-day family |
| daily_perday_heart_rate.json | garmin_daily 2026-11-11..2026-11-12 | 2 d | per-day family (intraday series) |

Windows were chosen small (2-3 days; steps at the full 28-day cap window)
rather than a full 30-day pull per family so the fixtures stay lean; one
30-day pull per ranged family is unnecessary for shape fidelity because one
ranged call returns one chunk piece either way. The families without a
retained raw response in the design's §7.4 list (stress, hydration, weight,
daily summaries, wellness activity summaries) are all covered here by REAL
captures taken at implementation time, per §4.5/§7.

NOT captured (no MCP tool reaches them — the seven deployed tools do not
expose these endpoints, P3): weight goals, weight/all, wellness activity
summaries, sleep/HRV per-day detail, nutrition, menstrual endpoints, and the
three point reads (profile settings, HR zones, legacy daily). The sync
engine's fetchers for those families (client.fetch_ranged_archive /
fetch_perday_archive / fetch_point) are monkeypatched in tests; no tool
reads them, so no envelope can drift.
