# hackathon-radar

Watches hackathons, tech meetups, and networking events, scores each new one against
your interests with Claude, and posts matches to a Telegram channel — so your phone
buzzes when something worth joining appears.

**Sources:** Devpost (JSON API), MLH season pages (schema.org microdata), Luma city
discover feed (`lu.ma/singapore`) for networking events and meetups.
**Scope:** Singapore in-person + online/global events (configurable in `config.toml`).
**Scoring:** `claude-haiku-4-5` rates each event 0–10 against the interest profile in
`config.toml` — scores and reasons stay in the database (backend-only); the channel
cards show factual event info, so they read fine in a public channel. New Devpost
events are also enriched from their pages: team size, registration deadline, and a
collapsed tap-to-expand challenge brief. Without Anthropic credentials everything
degrades to keyword matching (no enrichment).

## Setup (one-time, ~5 minutes)

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

## Run on a schedule (every 6 hours)

Pick **one** of the two options — each keeps its own seen-events DB, so running both
means duplicate notifications.

### Option A (recommended): GitHub Actions — runs even when your Mac is off

The workflow in `.github/workflows/radar.yml` runs twice daily on GitHub's servers.
One-time setup after pushing the repo — add the three secrets (each command prompts
you to paste the value, so nothing lands in shell history):

```sh
gh secret set TELEGRAM_BOT_TOKEN
gh secret set TELEGRAM_CHAT_ID
gh secret set ANTHROPIC_API_KEY

gh workflow run radar.yml          # trigger a test run now
gh run watch                       # watch it
```

The seen-events DB is persisted between runs via the Actions cache. Quirks: scheduled
runs can start a few minutes late, and GitHub pauses schedules on repos with no
commits for 60 days (any commit re-enables). If the cache is ever evicted, the next
run re-notifies up to `max_per_run` events once.

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
3. **Per-run cap** — `max_per_run` (10)
4. **Rolling daily cap** — `max_per_day` (15) across runs, counted from the DB, so
   even a lost/evicted DB produces one bounded batch, not a flood
5. **Repeat-title guard** — titles matching anything notified in the last
   `duplicate_title_days` (14) are skipped; catches the same event cross-posted on
   two sources and recurring weekly events that get fresh ids
6. **Quiet hours** — posts between `quiet_start` and `quiet_end` (23:00–08:00 SGT)
   deliver silently: they appear in the channel without pinging your phone
7. **Fail-safe sends** — if Telegram errors mid-run, unsent events stay unrecorded
   and are retried next run instead of being lost or hammered

## Tuning

Everything about *what you get notified for* lives in `config.toml`:

- `interests.profile` — free text; this is what Claude scores against
- `interests.min_score` — raise to 7–8 if the channel gets noisy
- `scope.mode` — `sg_plus_online` / `sg_only` / `global`
- `sources.*.enabled` — turn sources on/off

## Development

```sh
uv run pytest
```

Parser tests run against saved fixtures in `tests/fixtures/` (real Devpost/MLH
responses from 2026-07). If a site changes its markup, refresh the fixture and fix
the parser.
