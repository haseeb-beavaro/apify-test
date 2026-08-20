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

**This pricing model is modeled directly on a real product: sandcastles.ai**
(confirmed live 2026-08-20) — same platforms (TikTok/Instagram/YouTube
Shorts), same "select a niche, track competitors" mechanic, same
Pro/Visionary/Titan names and $39/$79/$399 prices, same credit counts, same
refresh intervals. **One correction to the original template:**
sandcastles.ai's Titan tier tracks **250 competitors, not 500** — Titan and
Visionary both cap at 250 creators; Titan differentiates on refresh speed
(1h vs 6h), credits (1500 vs 250), and API access, not creator count. All
numbers below use the corrected 250.

Full detail in `unit_economics.csv` (same structure as Task 6's template).
Summary, using the **dashboard-confirmed** rate (exact Apify per-run costs
at the current `LIMIT=5` config — TikTok $0.02 + IG search $0.01 + IG
profile $0.01 + YouTube $0.02 = **$0.06 per creator-refresh**, not an
estimate):

| | Pro ($39/mo) | Visionary ($79/mo) | Titan ($399/mo) |
|---|---|---|---|
| Creators tracked | 150 | 250 | 250 (corrected from 500) |
| Refresh interval | 12h | 6h | 1h |
| Fetches/month | 9,000 | 30,000 | 180,000 |
| Data cost @ dashboard rate $0.06/fetch | $540.00 | $1,800.00 | $10,800.00 |
| Total cost (data + transcript + AI + infra) | $547.10 | $1,815.50 | $10,885.50 |
| Gross margin | **−1,303%** | **−2,198%** | **−2,628%** |
| Verdict | Does not work | Does not work | Does not work |

**Three cost-per-fetch scenarios, from least to most real:**

| Basis | $/fetch | Pro data cost |
|---|---|---|
| Original template placeholder | $0.0009 | $8.10 |
| Event-price theoretical floor (1 item/platform) | $0.014 | $126.00 |
| **Dashboard-confirmed (5 items/platform, current config)** | **$0.06** | **$540.00** |

**All three plans lose money on data cost alone, catastrophically, at
current scope.** The gap between the theoretical $0.014 and the actual
$0.06 comes down to items-per-fetch: our current test setup pulls 5 items
per platform per refresh, and Apify charges per item returned — the
dashboard number is what's actually being billed right now, so it's the
most defensible number to plan around unless the real product only ever
needs 1 item per platform (in which case $0.014 applies instead).

**Two cost lines in this model are unverified placeholders**, not measured
data — flagged explicitly, not silently assumed real:
- Transcript cost ($0.006/transcript) — V0 doesn't fetch transcripts (out
  of scope per `CLAUDE.md`), so there's no real usage data yet.
- AI credit cost ($0.05/credit) — V0 does no AI analysis (also out of
  scope), same situation.
- Infrastructure cost ($1.50/user) — a business estimate, not something
  Apify data can inform at all.

Only the **data fetch cost** ($0.06, or $0.014 under the 1-item
assumption) is a real, live-verified number.

---

## 4. Key open question that changes everything

The $0.06/fetch rate assumes **1 fetch = refreshing a creator across all
3 platforms (TikTok + Instagram + YouTube), 5 items each** — the current
test config, and what the dashboard is actually charging right now. Two
separate product decisions each change this a lot:

| If the real product needs... | Cost/fetch | Pro data cost |
|---|---|---|
| All 3 platforms, 5 items each (current basis) | $0.06 | $540.00 |
| All 3 platforms, 1 item each | $0.014 | $126.00 |
| 1 platform only, 5 items | ~$0.02 | ~$180.00 |
| 1 platform only, 1 item | ~$0.005 | ~$45.00 |

Even the cheapest realistic scenario (1 platform, 1 item) still exceeds
Pro's $39 price on data cost alone. **This needs a real product-scope
answer — both platform count and items-per-refresh — before any repricing
decision is final.**

---

## 5. Decision status

**Not yet decided.** Per the unit economics finding, the realistic paths
are:
1. **Reprice** — Pro would need to be ~$550+/month just to break even at
   current scope (5 items/platform, all 3 platforms), before any healthy
   margin. Even the cheapest realistic scope (1 platform, 1 item) still
   needs Pro above $45/month to break even.
2. **Reduce scope** — items-per-fetch and platforms-per-fetch are both
   large levers (see the table in section 4); Titan's 1-hour refresh
   interval is also a major cost driver (360,000 fetches/month) regardless
   of which basis is used.
3. **Resolve the open questions above** — both "how many platforms per
   fetch" and "how many items per fetch" need real answers before any
   repricing decision is final; the gap between best and worst case is
   over 10x.

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
