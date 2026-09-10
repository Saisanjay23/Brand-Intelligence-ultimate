# Brand Intelligence

Two-phase impersonation triage across six social platforms. **Discovery**
sweeps each platform's own search surface for candidate profiles matching an
analyst's keywords; **analysis** scrapes and scores each candidate against a
risk rubric. Both phases prefer reading the platform's own GraphQL/API
payloads over scraping rendered HTML, and discovery writes one MongoDB
document per profile.

## Run

```bash
python run.py                # sets up on first run, then starts everything
python run.py --setup        # install dependencies, browsers and the UI
python run.py --check        # verify prerequisites (Mongo, packages, sessions), exit
python run.py --dev          # serve the UI from Vite instead, hot-reloaded
python run.py --port 9000    # serve somewhere else
```

`python run.py` is the only command you need. <http://127.0.0.1:8000> is the
whole app -- one process serves the API and the built UI on one port. API
docs live at `/docs`. See `run.py`'s own docstring for the full flag list and
`backend/main.py`'s for the API's own design notes (single worker, error
shape, auth posture).

## Layout

```
backend/
  main.py                  ASGI app: mounts frontend/dist, wires routers, lifespan
  api/                      the whole HTTP surface, one router module per domain --
                            discovery.py, analysis.py, clients.py, sessions.py,
                            logos.py, media.py, reports.py, alerts.py, health.py
  discovery/runner.py       discovery job engine: sweeps, caps, session claims
  analysis/runner.py        analysis job engine: scrape + score, memory-only results
  platforms/
    registry.py             the platform catalog (id, adapter paths, auth style)
    facebook/ twitter/ instagram/ youtube/ telegram/ tiktok/
      discovery_engine.py     keywords -> candidate profile URLs
      analysis_engine.py      profile URL -> scored fields
  sessions/manager.py       pooled platform credentials: leases, health, backoff
  database/
    connection.py           the one Motor client
    repositories/            one module per collection (profiles, clients, sessions,
                             logos, evidence, analysis results, ...)
    migrations/              one-off scripts (run by hand, not on startup)
  shared/                   keywords.py (permutation matching), text.py (scoring
                            text ops), models/ (Row, Hit, the risk rubric),
                            job_store.py, live_poll.py, extraction.py, resilience.py
  services/                 avatar_cache.py, email_service.py, logo_match.py,
                            report_service.py, session_canary_service.py
  stealth/                  browser.py, human.py (pacing), fingerprint.py, ...
  tests/                    pytest, pure logic only -- see "Tests" below
frontend/src/               React + TypeScript (Vite)
  pages/                    HomeView, LiveResultsView, AnalysisView, SessionPanel,
                            SchedulerPanel, ReportsPanel, AdminPanel, ...
  components/               DiscoveryProfileGrid.tsx (the triage grid), JobMonitor,
                            PlatformIcon, ...
session/                    cookie/session files -- gitignored
runs/                       per-job report workbooks
```

Dependencies run one way: `platforms/` imports `shared/`, `sessions/` and
`stealth/`, never the reverse. `platforms/registry.py` loads each platform's
adapter classes lazily by string path, so `shared/` never imports a
platform.

## The workflow

1. **Discover** -- `POST /discovery/jobs` sweeps each requested platform's
   own search surface for the given keywords. Results are written to Mongo
   as each sweep completes (`GET /discovery/profiles` reads them back while
   the job is still running), keyed by `(client_id, platform, url)`, and
   deduped: re-running the same keywords enriches the rows already found
   rather than duplicating them.
2. **Triage** -- an analyst marks each candidate `validated` or `rejected`
   (`POST /discovery/profiles/status`). That decision is the analyst's; no
   later sweep ever overwrites it.
3. **Analyse** -- `POST /discovery/profiles/analyse` sends every validated
   profile straight to the analysis engine, or paste URLs directly into
   `POST /analysis/jobs` for an ad-hoc run with no client behind it.
   Analysis scrapes each profile and scores it (`shared/models/scoring.py`).
   Its results are **memory-only** with a retention TTL -- they never touch
   the `profiles` collection discovery owns.
4. **Export** -- `POST /analysis/export/xlsx` turns a set of rows into a
   report workbook; `POST /reports/client/{id}/send` emails one.

Re-running discovery is idempotent, so a daily sweep is safe.

## Keyword matching

An analyst curates a **parent** keyword (what a hit is filed under and
exported as -- "Gautam Adani") and, optionally, **child** permutations that
are what actually gets searched ("gautamadani", "adani gautam", ...). A
parent with children is never searched under its own name; a parent with no
children searches itself. The High/Medium/Low match badge is graded against
whichever child (or the parent) actually produced the hit, not always the
parent -- see `backend/shared/keywords.py`, which is the design doc for this
as much as the implementation.

## Data model

One document per `(client_id, platform, url)` in the `profiles` collection.
Discovery and analysis write **disjoint field sets**, so a re-sweep can
enrich a scored profile without ever blanking it, and an analyst's
`status` (validated/rejected) is never touched by either phase. `keywords`
is a `$addToSet` array, so one profile found under three keywords stays one
row.

`sources` records where each field came from (`name=graphql`,
`logo=dom-avatar`, etc.) -- treat `dom-`/`-loose` sources as weaker evidence
than `graphql`. **A blank field means "not visible to this session", not
"absent."**

## Extraction

Network interception first, always. A profile's own GraphQL/API entity --
matched by id -- is the only unambiguous source on a page that also carries
unrelated payloads (suggestions, sponsored content, notifications). DOM
reads exist only as a labelled fallback, used when the intercepted payload
came back empty; see `backend/shared/extraction.py`, which also tracks which
strategy answered so a platform's payload shape rotating (Facebook's GraphQL
doc ids do this) shows up as a documented gap in the results instead of a
silent zero.

## Stealth

**This is not ban-proof, and nothing is.** It is a low-detectability
posture, in `backend/stealth/`:

- **Read-only.** No writes, likes, friend requests or messages.
- **Minimal patching**, not `playwright-stealth` or canvas/WebGL spoofing --
  those are detectable in themselves.
- **Stable identity.** Same UA, viewport, locale and timezone every run.
- **Pacing over everything.** `human.py` applies jitter, fatigue and a
  circadian multiplier to request timing; this is the lever that actually
  matters.
- **Stop on challenge.** The first checkpoint aborts the run and quarantines
  the session with a growing backoff (`sessions/manager.py`).

No proxies, by design -- one stable identity per session rather than IP
rotation.

## Tests

```bash
pytest backend/tests          # pure logic: scoring, matching, caps, session
                              # leasing, cooperative stop, job-store eviction, ...
cd frontend && npm test       # Vitest: business logic in services/hooks/utils
cd frontend && npm run test:e2e   # Playwright: one read-only smoke test
```

`backend/tests` deliberately never touches Mongo, the network or a real
browser (see `pytest.ini`) -- the platform scraping engines themselves are
verified against live platforms separately, not by this suite. The
Playwright spec is intentionally read-only: it never launches a real
discovery/analysis job, to avoid firing scrapes against live logged-in
accounts in CI.

## Adding a platform

1. `backend/platforms/<name>/discovery_engine.py` + `analysis_engine.py`,
   mirroring an existing platform (`facebook/` is the most complete
   example).
2. One entry in `PLATFORMS` in `backend/platforms/registry.py` (adapter
   class paths, cookie domain and required cookies or API-key env var).
3. Its credentials land in the `sessions` Mongo collection through the UI's
   Sessions tab (`POST /sessions/{platform}/cookies` etc.) -- not a file on
   disk, except Telegram's MTProto session, which genuinely has to be one.

Keep field extraction in pure functions of `(payload) -> fields`, no browser
and no network, so it stays testable against a saved payload fixture.

## Debugging

- `logs/` -- structured run logs (see `backend/shared/logging.py`).
- `sources` on any profile document -- which extractor answered each field.
- `GET /health/ready` -- is Mongo actually reachable.
- `python run.py --check` -- prerequisites and every platform's session
  state, without starting the server.
- A field blank across *every* profile on one platform usually means a key
  moved: look in that platform's `discovery_engine.py`/`analysis_engine.py`
  first.
- **Windows + `--reload`**: breaks every browser-based platform (Facebook,
  Instagram, Twitter, TikTok fail with an empty `NotImplementedError`) while
  YouTube/Telegram keep working, which reads as a platform bug rather than
  the flag. Use plain `python run.py`; `--dev` (frontend hot reload) is
  unaffected.
