# Sonar

A Telegram bot that finds tech events worth an undergrad's time — hackathons,
buildathons, datathons, competitions, fellowships, workshops — scores each new one
with Claude, and drip-posts the good ones to the channel [**@EventSonar**](https://t.me/EventSonar).
So nobody hears about a hackathon three days after registration closed.

Built for Singapore CS/engineering students, self-taught devs, and student builders.
Events where you **build, compete, or apply** rank above turn-up-and-mingle ones:
networking sessions and tech talks have to clear a higher bar (score 8 vs 6) to earn
a post at all.

*(Vision, roadmap, and engineering record: [PROJECT.md](PROJECT.md).)*

> The bot and channel are **Sonar**; the repo, Python package, and CLI are still named
> `radar` — commands below are `uv run radar ...`.

**Sources (5 pipelines):** Devpost (JSON API), MLH season pages (schema.org
microdata), the Luma city discover feed (`lu.ma/singapore`), a **watchlist** of
arbitrary organizer pages (school clubs like NUS Hackers and NTUOSS, company
hackathon sites) — add any URL to `sources.watchlist.pages` and Claude extracts
events whenever the page's content changes — and a dedicated **email inbox**
subscribed to event newsletters, where Claude reads message bodies and attachments
(.ics, PDF posters, images).
Cards link to the most informative public event page available, not the deepest
signup form; the goal is to help a student decide before they commit.

**Scope:** Singapore in-person + online/global events (configurable in `config.toml`).

**Scoring:** `claude-haiku-4-5` rates each event 0–10 against the interest profile in
`config.toml` — scores and reasons stay in the database (backend-only); the channel
cards show factual event info, so they read fine in a public channel. Events are
also tagged by kind (🛠 build / 🤝 network / 🚀 program) and experience level
(🌱 beginner / ⚡ intermediate / 🔥 advanced). Devpost events about to be posted are
enriched from their pages: team size, registration deadline, and a collapsed
tap-to-expand challenge brief. Without Anthropic credentials everything degrades to
keyword matching (no enrichment).

## Setup (one-time, ~5 minutes)

To run your own instance (a different channel, another city, your own interest
profile), you need your own bot and channel:

1. **Create the bot** — in Telegram, message [@BotFather](https://t.me/BotFather) →
   `/newbot` → pick a name. Copy the token it gives you.
2. **Create the channel** — new Telegram channel (private is fine), then add your bot
   as an **admin with "Post messages"** permission.
3. **Configure** —

   ```sh
   cp .env.example .env
   # paste TELEGRAM_BOT_TOKEN (and ANTHROPIC_API_KEY) into .env
   ```

4. **Find the chat id** — post any message in the channel, then:

   ```sh
   uv run radar get-chat-id     # prints the -100... id; put it in .env as TELEGRAM_CHAT_ID
   ```

   (Public channel? Just use `@yourchannelhandle` as the chat id.)
5. **Verify** —

   ```sh
   uv run radar test-telegram   # should post a hello message in the channel
   ```

## Usage

```sh
uv run radar run --dry-run    # fetch + score, print instead of posting
uv run radar run              # the real thing
uv run radar run --max-notify 5
```

Seen events are remembered in `data/radar.db`, so you're only notified once per event.
Events scoring below `min_score` (config.toml) are recorded but not posted.

## Run on a schedule (drip-fed)

The workflow fires **every 30 minutes**; each firing drips at most one queued post.
Sources are only re-fetched every `fetch_every_hours` (6) — in-between runs are
short queue-drain checks that cost seconds of runner time and no API calls.

Pick **one** of the two options — each keeps its own seen-events DB, so running both
means duplicate notifications.

### Option A (recommended): GitHub Actions — runs even when your Mac is off

The workflow in `.github/workflows/radar.yml` fires every 30 minutes on GitHub's
servers (drip pacing; sources fetched every 6h). One-time setup after pushing the
repo — add the five secrets (each command prompts you to paste the value, so
nothing lands in shell history):

```sh
gh secret set TELEGRAM_BOT_TOKEN
gh secret set TELEGRAM_CHAT_ID
gh secret set ANTHROPIC_API_KEY
gh secret set EMAIL_ADDRESS        # feed-inbox source (optional but recommended)
gh secret set EMAIL_APP_PASSWORD

gh workflow run radar.yml                       # trigger a run now
gh workflow run radar.yml -f force_fetch=true   # ...and sweep sources immediately
gh run watch                                    # watch it
```

The seen-events DB is persisted between runs via the Actions cache. Quirks: scheduled
runs can start a few minutes late, and GitHub pauses schedules on repos with no
commits for 60 days (the workflow's keepalive step counters this). If the cache is
ever evicted, the next ingest re-queues up to `max_per_day` previously-seen events,
which then drip out normally.

### Option B: launchd — local, only while the Mac is awake

```sh
cp launchd/com.wenjing.hackathon-radar.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.wenjing.hackathon-radar.plist
```

Logs go to `logs/radar.log`; unload with `launchctl unload` on the same path.

## Anti-spam guardrails

Layered so no failure mode floods the channel (all knobs in `[notify]`):

1. **Only-new events** — the SQLite DB remembers everything ever seen
2. **Score threshold** — below `min_score` is recorded silently
3. **Drip pacing** — notification-worthy events go into a queue, and each run posts
   at most ONE, so consecutive posts are always ≥ `send_interval_seconds` (30 min)
   apart — never a burst
4. **Rolling daily cap** — `max_per_day` (15), counted from the DB, so even a
   lost/evicted DB produces one bounded day, not a flood
5. **Repeat-title guard** — titles matching anything notified in the last
   `duplicate_title_days` (60) are skipped; catches the same event cross-posted on
   two sources and recurring weekly events that get fresh ids. The same check also
   runs immediately before sending, so stale duplicate payloads already in the queue
   are dropped instead of posted.
6. **Quiet hours** — posts between `quiet_start` and `quiet_end` (23:00–08:00 SGT)
   deliver silently: they appear in the channel without pinging your phone
7. **Fail-safe sends** — a failed send stays queued and is retried next run
   instead of being lost or hammered

## Tuning

Everything about *what the channel gets notified for* lives in `config.toml`:

- `interests.profile` — free text describing the audience; this is what Claude
  scores against. Currently: "tech undergraduate students in Singapore who want to
  level up", with build/compete/apply events ranked above talks and mixers
- `interests.min_score` — raise to 7–8 if the channel gets noisy
- `interests.min_score_by_kind` — per-kind overrides; `networking = 8` is what keeps
  generic meetups and founders' breakfasts out
- `scope.mode` — `sg_plus_online` / `sg_only` / `global`
- `sources.*.enabled` — turn sources on/off; `sources.watchlist.pages` is where you
  add a new club or company page

## Cost

Uses `claude-haiku-4-5` (the cheapest model). Two things call the API, both **only
for new events** — a run that finds nothing new (the common case) costs $0:

- **Scoring** — one batched call per ~20 new events. Cheap.
- **Enrichment** — one page-fetching call per event, but **only for events actually
  being posted** (after scoring + threshold + cap). This is the main cost driver, so
  it's deliberately the last and narrowest step.

Steady state is a few new events a day → fractions of a cent. Costs spike only when
you **clear the dedupe cache and re-run**, which reprocesses the whole event pool —
treat that as a paid setup action, not routine. To cut spend to the bare minimum, set
`enrich.enabled = false` in `config.toml` (cards then show source data only, no brief
or extracted deadline/team-size).

## Development

```sh
uv run pytest
```

Parser tests run against saved fixtures in `tests/fixtures/` (real Devpost/MLH
responses from 2026-07). If a site changes its markup, refresh the fixture and fix
the parser.
