# Apify Short-Video Discovery — V0

Terminal-only Python app that takes a niche, fetches short-form video + creator
data from TikTok, Instagram Reels, and YouTube Shorts via Apify Actors,
normalizes it into one common schema, and writes raw + normalized JSON plus a
timing summary. This is the social-data validation layer only — see "Out of
scope" below before adding anything beyond that.

## Run it

```
pip install -r requirements.txt
cp .env.example .env   # then fill in APIFY_TOKEN
python app.py --niche "AI automation" --limit 10
python app.py --limit 10                                   # prompts for niche
python app.py --niche "AI automation" --platform tiktok --limit 10
```

`--platform` accepts `tiktok`, `instagram`, `youtube`, or `all` (default).

## Git / GitHub

Remote: `https://github.com/haseeb-beavaro/apify-test.git`, branch `main`.

Git identity for this repo is set **locally, not globally**
(`git config --local user.name/user.email` → `haseeb-beavaro` /
`haseeb@beavaro.com`) — this machine has multiple GitHub accounts
(`takweentutors1`, `haseeb-012`, `haseeb-beavaro`) with credentials that Git
Credential Manager can cache, so a global identity would leak across repos.

If `git push` fails with `Permission ... denied to <some-other-account>`,
that's not a code or permissions problem — Windows Git Credential Manager
grabbed a cached/logged-in session for the wrong GitHub account. Fix: log
into `haseeb-beavaro` in the browser (log out of/switch away from the other
account first), then retry the push; GCM will pick up that session. As a
last resort, clear the stale cached credential first: `cmdkey /list` to find
the `git:https://<account>@github.com` entry, `cmdkey /delete:"<target>"` to
remove it, then retry — but that alone doesn't guarantee the *right* account
gets picked up next, since GCM can silently reuse whatever GitHub session is
active in the default browser.

## Actors used (exactly these four — do not swap or add others without asking)

| Actor | Role | Input shape used |
|---|---|---|
| `clockworks/tiktok-scraper` | TikTok video + creator search | `{searchQueries: [niche], searchSection: "/video", resultsPerPage: limit, scrapeRelatedSearchWords: false}` |
| `apify/instagram-search-scraper` | Reel discovery by niche | `{search: niche, searchType: "popular", searchLimit: limit, enhanceUserSearchWithFacebookPage: false}` |
| `apify/instagram-profile-scraper` | Bulk creator enrichment | `{usernames: [...]}` — one call for all unique usernames from the search step, never one call per creator |
| `streamers/youtube-scraper` | Shorts-only search | `{searchQueries: [niche], maxResults: 0, maxResultsShorts: limit, maxResultStreams: 0}` |

Never request video downloads, transcripts/subtitles, full comment scraping,
or follower-list scraping from any of these Actors in V0.

## Reliability: timeouts, aborts, retries

TikTok and Instagram Actor runs are unreliable — they can sit in `READY` for
minutes, run for minutes and return 0 records, or get aborted mid-run.
YouTube has been reliable in practice but goes through the same harness for
consistency, since nothing prevents it from hanging too.

`run_actor_with_retries()` in `app.py` wraps every Actor call:

- **Timeout** — `ACTOR_TIMEOUT_SECONDS = 120`. Each attempt starts the run
  with `ActorClient.start()` (async, returns immediately), then long-polls
  `RunClient.wait_for_finish(wait_duration=timedelta(seconds=120))`. If the
  run hasn't reached a terminal status (`SUCCEEDED`/`FAILED`/`TIMED-OUT`/`ABORTED`)
  by then, the attempt is marked `TIMED_OUT` and the run is aborted via
  `RunClient.abort()` — never left running in the Apify console.
- **Retries** — `MAX_ATTEMPTS = 3`. Retried on failure, unexpected abort,
  timeout, or a `SUCCEEDED` run that returned 0 records when records were
  requested. Wait 10s after attempt 1 fails, 20s after attempt 2 fails, then
  give up after attempt 3.
- **Config errors don't retry** — a 400 `InvalidRequestError` from
  `apify_client.errors` (bad/invalid Actor input) fails immediately as
  `CONFIG_ERROR` status; retrying an invalid input three times can't fix it.
- **Instagram profile enrichment** only runs if reel discovery returned at
  least one username after retries. If discovery exhausts all retries with 0
  results, Instagram is recorded `NO_RESULTS` (or `FAILED` on a hard error)
  and the run moves on to YouTube without calling
  `apify/instagram-profile-scraper`.

Every attempt (not just the final one) is recorded with platform, actor,
attempt number, run ID, dataset ID, status, timestamps, elapsed seconds, and
records returned — surfaced in `errors.json` (every non-passing attempt) and
summarized per platform in `summary.json` (`attempts`/`discovery_attempts`/
`profile_attempts`, `total_platform_seconds` including retry wait time).

This does not fix the upstream TikTok/Instagram flakiness — it bounds how
long the app waits on it and makes failures visible and recoverable instead
of hanging forever.

## Field mapping (source → normalized)

**TikTok** (`app.py::normalize_tiktok_item`)
- creator: `authorMeta.id/name/profileUrl/fans` → `id/handle/profile_url/follower_count`
- video: `id`, `webVideoUrl`, `text`, `playCount`, `diggCount`, `commentCount`, `createTimeISO`, `videoMeta.duration`, `videoMeta.coverUrl`

**YouTube** (`normalize_youtube_item`)
- creator: `channelId/channelUsername|channelName/channelUrl/numberOfSubscribers`
- video: `id`, `url`, `title`, `viewCount`, `likes`, `commentsCount`, `date`, `duration` (text `"1:05"`/`"1:02:03"` parsed to seconds via `parse_duration_to_seconds`), `thumbnailUrl`

**Instagram** (`normalize_instagram_item`) — verified against live output 2026-08-20
- Search-scraper and profile-scraper field names weren't pinned down in the original spec, so the code tries several candidate keys per field via `get_first()` (e.g. views try `videoPlayCount` → `videoViewCount` → `viewCount` → `playsCount`). Confirmed live: `ownerUsername`, `videoPlayCount`, `likesCount`, `commentsCount`, `timestamp`, `caption`, `displayUrl` on the reel side; `followersCount`, `id`, `url` on the profile side all populate correctly. If a future run shows unexpected `null`s, check `raw/instagram_reels.json` / `raw/instagram_profiles.json` first — Instagram Actor output has been observed to vary (e.g. `post-details: timeline metrics rate limited` warnings can leave some metrics missing on a given run).

**Universal rule:** a field the platform didn't return is `null`, never `0`.
`0` means the platform explicitly reported zero. Don't "fix" a `null` by
defaulting it — trace it back to the raw JSON first (see below).

## Why raw + normalized are both saved

Every run writes untouched Actor output to `raw/` and mapped output to
`normalized/`. If a normalized field is unexpectedly `null`, check the raw
file before touching the mapping code — it tells you whether Apify didn't
return the field or the mapping is wrong. Don't delete or skip writing the
raw files to "save space"; they're the debugging source of truth.

## Output layout

```
data/<timestamp>_<niche-slug>/
├── raw/{tiktok,instagram_reels,instagram_profiles,youtube_shorts}.json
├── normalized/{tiktok,instagram,youtube,all_results}.json
├── errors.json
└── summary.json
```

## Soak test (supplier reliability monitor)

A separate, standalone monitoring layer — **not** part of the V0 CLI, and
`app.py` is untouched by it. Measures TikTok/Instagram/YouTube's real Actor
reliability (failure rate, block rate, timeout rate, first-attempt vs
recovered-after-retry) over a 3-day window instead of trusting one run.

- `soak_test.py` — one monitoring cycle. Fires `CONCURRENT_USERS` (currently
  **3**) simultaneous workers per platform via `ThreadPoolExecutor` — real
  concurrent load against the vendor, not sequential calls, to catch
  rate-limiting/blocking that only triggers under simultaneous requests.
  Each worker independently requests `LIMIT` results/platform (currently
  **5**) and runs its own retry-with-timeout cycle, reusing `app.py`'s Actor
  IDs/constants/`normalize_*_item()` functions but implementing its own
  retry loop instrumented with: queue-vs-execution time split (via
  `run.stats.run_time_secs` vs total wall time), blocking-signal detection
  (scans `RunClient.log().get()` text for `429`/`403`/`captcha`/
  `proxy retry`/etc.), required-field presence checks, and
  first-attempt-vs-final outcome tracking. Each platform's logged record
  keeps worker 0's result at the top level (backward compatible with the
  pre-concurrency log format) plus a `concurrent` summary (successes/
  failures/blocked count across all workers) and the full
  `concurrent_workers` list. Appends one JSON line to
  `data/soak_test/log.jsonl`.
- `soak_test_report.py` — aggregates `log.jsonl` into per-platform failure
  rate, zero-result rate, timeout rate, blocked-run count, first-attempt
  success rate, recovered-after-retry count, and `median`/`p95`/`max`
  seconds. Runnable anytime, not just after the full 3 days. **Caveat at the
  current cadence:** only ~6 samples per platform over 72h — enough to catch
  a real, sustained failure, but any single-run percentage (e.g. "33%
  failure rate" from 2 of 6) is statistically noisy. Don't over-read small
  differences; look for a run that's actually broken, not for a precise rate.
- `.github/workflows/soak-test.yml` — runs `python soak_test.py` on
  `workflow_dispatch` (manual smoke test) and on a schedule
  (`cron: "7 */12 * * *"`, i.e. **every 12 hours**, ~6 cycles over 72h) on
  GitHub's own runners, then commits `data/soak_test/` back to the repo.
  Deliberately spaced out (changed from an original every-20-minutes plan)
  to catch failures that only surface after hours of sustained running,
  and because 6 cycles at `LIMIT=5` × `CONCURRENT_USERS=3` costs an
  estimated ~$1.2-2 total — comfortably inside the $3.50 cap even with
  concurrency tripling the per-checkpoint cost. Needs `APIFY_TOKEN` added as a GitHub
  Actions secret (Settings → Secrets and variables → Actions) — can't be set
  from the CLI without `gh` installed and authenticated, so this was a
  manual one-time step (already done).
- **Budget gate, not a run-count guarantee**: before any Actor calls,
  `soak_test.py` checks `client.user().monthly_usage()` against a baseline
  snapshot taken on its first-ever run (stored in
  `data/soak_test/budget_state.json`) and skips the cycle entirely
  (`status: "SKIPPED_BUDGET_CAP"`, zero Actor calls) once soak-test spend
  hits `BUDGET_CAP_USD` (currently $3.50). At the current every-12h/`LIMIT=5`/
  `CONCURRENT_USERS=3` cadence this is still not a binding constraint
  (~$1.2-2 total expected spend, against a $4.69 account balance at last
  check) — it was a hard constraint back when the cadence was every 20
  minutes (~216 cycles), not now.
- `soak_test_checkpoint_report.py` — prints one row per (checkpoint, vendor)
  in the exact columns the tracking sheet wants (Requests made / Requests
  failed / Failure rate / Average response time (sec) / What went wrong),
  tab-separated for pasting directly into the sheet. Grouped into three
  blocks (TikTok/Instagram/YouTube) since the sheet template has one
  "Vendor" column — needs one copy of the sheet per vendor. "Requests made"
  sums actor-call attempts (including retries) across every concurrent
  worker for that checkpoint, not items fetched — falls back to the single
  top-level result for older log entries logged before `CONCURRENT_USERS`
  existed (no `concurrent_workers` key present).
- `.gitignore` special-cases this: `data/*` stays ignored (real per-run
  scrape output from `app.py`) but `!data/soak_test/` is tracked, since the
  soak test's `log.jsonl`/`budget_state.json` need to survive between
  GitHub Actions runs via git, not local disk.
- **Before trusting the schedule**: run the workflow once manually via
  `workflow_dispatch` and confirm it wires up correctly end-to-end (token
  works, all 3 platforms get called, `log.jsonl` gets committed) — that's
  the smoke test. Only then let the cron schedule run unattended.

## apify-client version note

`apify-client>=3.1` returns typed objects, not dicts. `client.actor(id).start(...)`
returns a `Run` object (use `run.id` / `run.default_dataset_id`, not dict
subscripting), `client.run(run_id).wait_for_finish(wait_duration=...)` returns
a `Run | None` (non-`None` even if the run hasn't reached a terminal status —
check `.status` against `TERMINAL_RUN_STATUSES`, don't assume it means
finished), and `client.dataset(id).list_items()` returns a `DatasetItemsPage`
whose `.items` is the plain list of dict records. `InvalidRequestError`
(HTTP 400) lives at `apify_client.errors.InvalidRequestError`, not the
top-level `apify_client` package. If `pip install` ever resolves an older 2.x
`apify-client`, these return shapes and import paths will differ and
`run_actor_with_retries()` in `app.py` will break — keep the version pin in
`requirements.txt` in sync with the API shape the code expects.

## Conventions to preserve when editing

- One platform failing must never abort the others — each `run_*` function
  catches its own errors internally (via `run_actor_with_retries()`) and
  returns a result dict with `status` one of `PASS`/`FAILED`/`NO_RESULTS`/
  `CONFIG_ERROR`; `main()` keeps going regardless and collects every failed
  attempt into `errors.json`.
- Timing uses `time.monotonic()`, per Actor call, not wall-clock `time.time()`.
- Instagram's timing is two numbers (`search_seconds` + `profile_seconds`),
  each the elapsed time of the *successful* attempt for that stage — keep
  both when reporting, don't collapse early. `total_platform_seconds` (in
  `summary.json`) is larger than their sum: it also includes every failed/
  timed-out attempt's elapsed time and the retry wait time actually spent,
  since that's real wall-clock cost of running that platform.
- `average_platform_seconds` is computed only over platforms that succeeded.
- Every normalized record carries `fetched_at` (UTC, `now_iso()`), even
  though V0 doesn't yet do anything with historical comparison.
- `APIFY_TOKEN` comes from `.env` via `python-dotenv` only. Never hardcode it,
  never print it, never let it land in a committed file (`.env` is
  gitignored — `.env.example` is the only version-controlled template).
- Keep the four Actor IDs and their input shapes exactly as documented above
  unless the user explicitly asks to change providers/inputs — this project
  exists specifically to benchmark these four.

## Out of scope for V0 — do not add without explicit request

Transcripts/captions, AI hook analysis, script generation, outlier scoring,
creator watchlists, creator video-history scraping, any database (Postgres,
vector/embeddings), a frontend, auth, scheduled jobs, full comment scraping,
follower-list scraping, video downloading. These are later phases; adding
them now would break the "V0 validates the data layer only" goal.

## Testing changes

No live-Actor test suite exists (V0 talks to real paid Apify Actors, so
tests are manual). Smoke-test without spending credits by exercising the
pure functions in a REPL: `slugify`, `parse_duration_to_seconds`, and the
three `normalize_*_item` functions all take plain dicts and have no Apify
dependency. Only run `python app.py ...` for real against Apify when the
user asks for a live run — start with `--limit 3` on one `--platform`
before requesting larger batches.

## Session log

**2026-08-20 — Reliability harness built, live-validated, repo pushed to GitHub**
- Built the timeout/retry/abort harness described in "Reliability" above
  (`run_actor_with_retries()`), replacing the old fixed-4-minute
  `client.actor(id).call(wait_duration=...)` approach. Verified against the
  installed `apify-client==3.1.3` source directly (not guessed) — confirmed
  `ActorClient.start()` + `RunClient.wait_for_finish(wait_duration=...)` +
  `RunClient.abort()` is the correct pattern, and that
  `apify_client.errors.InvalidRequestError` (HTTP 400) is the right signal
  for "don't retry, it's a config error."
- Tested with a mocked `ApifyClient` (no live cost) covering: timeout → abort
  → retry → success, zero-records → retry → success, config-error → no
  retry, and exhaust-all-3-attempts → `FAILED`. Then live-tested for real
  (`--platform all --limit 1` and `--platform youtube --limit 1`) — both
  passed on attempt 1, `errors.json` empty, all `raw/`/`normalized/` files
  written correctly.
- Live testing so far cost **$0.2179** of the account's **$5.00/month** free
  Apify credit (~$4.78 left). Pulled exact per-event pricing via
  `client.run(id).get().pricing_info` — TikTok: $0.001 flat start +
  $0.0037/result; Instagram search: $0.0027/result; Instagram profile:
  $0.0026/profile; YouTube: $0.004/result. A full `--limit 1` all-platform
  pass costs ~$0.014 in pure event charges; observed real spend runs a bit
  higher, so budget ~$0.05–0.15/run when planning further live tests.
- One live run showed YouTube taking 82.2s total when its own container log
  only spanned 17.4s — pulled `run.started_at`/`finished_at`/`stats` and
  confirmed the ~65s gap was Apify queueing the run before the container
  even started (not a bug in this code). Not something we can fix from the
  client side; the timeout harness already bounds it at `ACTOR_TIMEOUT_SECONDS`.
- Initialized git in this directory (previously not a repo), set repo-local
  identity, and pushed the initial commit to GitHub (see "Git / GitHub"
  above) — hit and resolved a Git Credential Manager conflict from multiple
  cached GitHub accounts on this machine along the way.
- Built the 3-day soak test described in "Soak test" below: `soak_test.py`,
  `soak_test_report.py`, `.github/workflows/soak-test.yml`. Cadence changed
  from the originally-planned every-3-hours to **every 20 minutes**
  (`cron: "7,27,47 * * * *"`, ~216 runs over 3 days) per a more detailed spec
  — tracks queue-vs-execution time split, blocking-signal log scanning
  (403/429/captcha/proxy-retry text matches), required-field presence, and
  first-attempt-vs-recovered-after-retry outcomes, not just pass/fail.
  Verified with a mocked `ApifyClient` (timeout/block/recovery/budget-cap
  scenarios) — not yet live-tested or scheduled. **Flagged but unresolved:**
  216 runs assumes the ~$0.014/run theoretical event-price floor; the
  $3.50 cap will likely be hit well before 3 days elapse if real cost tracks
  the ~$0.05–0.22/run observed in earlier manual testing instead — this is
  by design (the budget gate protects spend over completing the schedule),
  not a bug, but means "216 runs" is an upper bound, not a guarantee.
  Remaining manual steps before it can run for real: add `APIFY_TOKEN` as a
  GitHub Actions secret, then manually trigger the workflow once
  (`workflow_dispatch`) as a smoke test before trusting the cron schedule.
