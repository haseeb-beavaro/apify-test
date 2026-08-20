# Business plan reference — Apify vendor data, reliability, and unit economics

Consolidates everything measured against the real Apify setup this session:
what the vendors actually cost, how reliable they actually are, and what
that means for the Pro/Visionary/Titan pricing plans. Every number here is
either pulled live from the Apify account/API or explicitly marked as an
unverified placeholder — nothing here is guessed silently.

Last updated: 2026-08-20.

---

## 1. Vendor: Apify (all 4 Actors)

Real per-event pricing, pulled from `client.run(id).get().pricing_info` on
this account:

| Actor | Charged event | Price |
|---|---|---|
| `clockworks/tiktok-scraper` (TikTok) | `actor-start` (flat, one-time) + `result` | $0.001 + $0.0037/result |
| `apify/instagram-search-scraper` (IG discovery) | `result` | $0.0027/result |
| `apify/instagram-profile-scraper` (IG enrichment) | `profile` | $0.0026/profile |
| `streamers/youtube-scraper` (YouTube) | `result` | $0.004/result |

**Cost to refresh 1 creator across all 3 platforms, 1 item each:**
$0.0047 (TikTok) + $0.0027 (IG search) + $0.0026 (IG profile) + $0.004 (YouTube)
= **$0.014/creator-refresh**. This is a measured number, not a theoretical
one — confirmed against real account spend (`client.user().monthly_usage()`)
across multiple live test runs.

Account plan: Apify Free tier, **$5.00/month** total usage credit.

---

## 2. Vendor reliability (from the 72-hour soak test — in progress)

Live-measured via `soak_test.py` + `soak_test_report.py`
(`data/soak_test/log.jsonl`), every 12 hours, `LIMIT=5` results/platform,
`CONCURRENT_USERS=3` simultaneous requests per platform per checkpoint.
**This is still running — numbers below are from 2 of 6 planned checkpoints
and will keep updating.** Re-run `python soak_test_report.py` for the
current state.

| Platform | Tests | Success rate | Blocked (recovered) | Median time | P95 | Max |
|---|---|---|---|---|---|---|
| TikTok | 2 | 100% | 0/2 | 7.3s | 8.1s | 8.1s |
| Instagram | 2 | 100% | 2/2 | 29.2s | 30.0s | 30.1s |
| YouTube | 2 | 100% | 0/2 | 49.6s | 78.9s | 82.1s |

**Read with caution — only 2 samples so far.** Any percentage here is
statistically noisy; treat it as "nothing has actually broken yet" and
"Instagram shows friction under concurrent load every time so far," not as
a precise long-term failure rate. Wait for more checkpoints before using
this in a hard financial or reliability commitment.

**What "blocked (recovered)" means:** the vendor's own scraper hit
rate-limiting/bot-detection mid-run (real log evidence, e.g. "Blocked.
Received blocking reason: Sign in to confirm you're not a bot") but its own
internal retry logic recovered before the run finished — so the run still
returned complete, real data. Not a failure, but a real friction signal
worth watching if it escalates to actual failures later in the 72h window.

---

## 3. Unit economics — Pro / Visionary / Titan

Full detail in `unit_economics.csv` (same structure as Task 6's template).
Summary:

| | Pro ($39/mo) | Visionary ($79/mo) | Titan ($399/mo) |
|---|---|---|---|
| Creators tracked | 150 | 250 | 500 |
| Refresh interval | 12h | 6h | 1h |
| Fetches/month | 9,000 | 30,000 | 360,000 |
| Data cost @ real $0.014/fetch | $126.00 | $420.00 | $5,040.00 |
| Total cost (data + transcript + AI + infra) | $133.10 | $435.50 | $5,125.50 |
| Gross margin | **−241%** | **−451%** | **−1,185%** |
| Verdict | Does not work | Does not work | Does not work |

**All three plans lose money on data cost alone at current scope.** The
template's placeholder rate ($0.0009/fetch) was 15.6x cheaper than what
Apify actually charges — using the real rate flips every plan from
"tight but maybe workable" to "loses money on every single customer."

**Two cost lines in this model are unverified placeholders**, not measured
data — flagged explicitly, not silently assumed real:
- Transcript cost ($0.006/transcript) — V0 doesn't fetch transcripts (out
  of scope per `CLAUDE.md`), so there's no real usage data yet.
- AI credit cost ($0.05/credit) — V0 does no AI analysis (also out of
  scope), same situation.
- Infrastructure cost ($1.50/user) — a business estimate, not something
  Apify data can inform at all.

Only the **data fetch cost** ($0.014) is a real, live-verified number.

---

## 4. Key open question that changes everything

The $0.014/fetch rate assumes **1 fetch = refreshing a creator across all
3 platforms** (TikTok + Instagram + YouTube), 1 item each. If the real
product only tracks a creator on whichever single platform they're
actually active on (not always all 3), real cost drops to roughly
**$0.004–0.005/fetch** — still 4-5x the placeholder, but Pro's plan
becomes survivable again. **This needs a real product-scope answer before
any repricing decision is final.**

---

## 5. Decision status

**Not yet decided.** Per the unit economics finding, the realistic paths
are:
1. **Reprice** — Pro would need to be ~$130+/month just to break even at
   current scope, before any healthy margin.
2. **Reduce scope** — Titan's 1-hour refresh interval is the single
   biggest cost driver (360,000 fetches/month); cutting refresh frequency
   or creator counts has outsized impact.
3. **Resolve the open question above** — confirming "1 fetch = 1 platform"
   instead of "1 fetch = 3 platforms" could make Pro workable without
   repricing at all.

Fill in the final decision, new pricing/scope, decision-maker, and date in
`unit_economics.csv`'s "DECISION" section once resolved.

---

## Files this references

- `unit_economics.csv` — full Task 6 spreadsheet (Shared Assumptions → Per
  Plan Calculation → placeholder-vs-real comparison → decision section)
- `data/soak_test/log.jsonl` — raw soak test checkpoint data
- `soak_test_report.py` — aggregate reliability stats (run anytime)
- `soak_test_checkpoint_report.py` — sheet-ready rows per checkpoint per
  vendor (Task 5's tracking sheet)
- `CLAUDE.md` — full technical documentation of the Apify integration,
  reliability harness, and soak test implementation
