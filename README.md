# hackathon-radar

Watches hackathons, tech meetups, and networking events, scores each new one against
your interests with Claude, and posts matches to a Telegram channel — so your phone
buzzes when something worth joining appears.

**Sources:** Devpost (JSON API), MLH season pages (schema.org microdata), Luma city
discover feed (`lu.ma/singapore`) for networking events and meetups.
**Scope:** Singapore in-person + online/global events (configurable in `config.toml`).
**Scoring:** `claude-haiku-4-5` rates each event 0–10 against the interest profile in
`config.toml` and writes a one-line "why you'd care". New Devpost events are also
enriched from their pages: team size, registration deadline, and a collapsed
tap-to-expand challenge brief. Without Anthropic credentials everything degrades to
keyword matching (no enrichment).

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

## Run on a schedule (9:03 and 21:03 daily)

```sh
cp launchd/com.wenjing.hackathon-radar.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.wenjing.hackathon-radar.plist
```

Logs go to `logs/radar.log`. To stop: `launchctl unload ~/Library/LaunchAgents/com.wenjing.hackathon-radar.plist`.

Note launchd only fires while the Mac is awake; it catches up on wake if a time was
missed. For a machine-independent schedule, move the same command to GitHub Actions
cron later.

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
