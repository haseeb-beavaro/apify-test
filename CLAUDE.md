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
