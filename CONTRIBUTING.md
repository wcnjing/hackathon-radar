# Contributing

Read [PROJECT.md](PROJECT.md) first — vision, architecture, and the known-gaps
list (good source of starter tasks). [README.md](README.md) has the operational
detail.

**The one thing to understand before you start:** `master` is deployed. The
GitHub Actions workflow posts to a live Telegram channel with real subscribers
within 30 minutes of a merge. Work on a branch, open a PR.

## Setup (~5 minutes)

```sh
brew install uv gh          # uv manages Python 3.14 itself; no pyenv needed
gh repo clone wcnjing/hackathon-radar && cd hackathon-radar
uv sync                     # deps + dev group
uv run pytest               # must be green before you change anything
```

No Docker, no database to provision, no services — SQLite is a file, and every
test runs offline against fixtures and mocks.

## Your sandbox credentials

Copy `.env.example` to `.env` and fill in **your own** values. Never put
production credentials in a local `.env`.

| Variable | What you use |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your own bot from [@BotFather](https://t.me/BotFather) (`/newbot`) |
| `TELEGRAM_CHAT_ID` | Your own **private test channel**, with your bot added as an admin with "Post messages". Run `uv run radar get-chat-id` to find the id |
| `ANTHROPIC_API_KEY` | Your own key (set a spend limit). Optional — leave it unset and the keyword scorer runs instead |
| `EMAIL_ADDRESS`, `EMAIL_APP_PASSWORD` | **Leave empty.** The email source skips itself cleanly |

Verify with `uv run radar test-telegram` — a message should land in *your* test
channel, never @EventSonar.

## Rules that keep production safe

1. **Never run `radar run` against production credentials.** Use `--dry-run`, or
   a fully sandboxed `.env`.
2. **`--dry-run` is not side-effect free.** It writes nothing to the events DB,
   but the watchlist and email fetchers still advance their own state (page
   content-hashes, IMAP UID) — pointed at production, a dry run silently
   consumes events the live channel then never posts. This is a known gap; see
   PROJECT.md.
3. **Never add a `pull_request` trigger to `radar.yml`.** That would give branch
   code access to the live secrets. `ci.yml` runs PR tests and holds no secrets.
4. **Don't commit `.env`,** and don't paste tokens into issues, screenshots, or
   chat. Error paths and httpx logs are deliberately sanitized because a bot
   token once leaked through a traceback.

## Workflow

- Branch from `master`: `feat/short-description` or `fix/short-description`
- Conventional commits: `feat:`, `fix:`, `chore:`, `docs:`
- Open a PR — CI runs `pytest` and must pass; `master` takes no direct pushes
- Keep the diff focused; if you find something unrelated, note it rather than
  folding it in

## Testing conventions

- **Tests must run offline.** No network, no API keys, no live IMAP. Use the
  saved fixtures in `tests/fixtures/` (real captured API/HTML responses) or
  `monkeypatch`.
- A bug fix gets a regression test that fails before the fix.
- Parser changes: refresh the fixture from the live source and update the
  parser together, so the test proves it against real markup.

## Code map

| Path | Responsibility |
|---|---|
| `sources/*.py` | One fetcher per source; each returns `list[Event]` and must not raise past `fetch_all` |
| `models.py` | The `Event` dataclass — **changing fields affects queued payloads in production** (see PROJECT.md gaps) |
| `scoring.py` | Claude scoring + classification (kind, experience level), keyword fallback |
| `enrich.py` | Page fetch + extraction for events about to post; also shared HTML→text |
| `filtering.py` | Scope rules, title normalization for dedupe |
| `store.py` | SQLite: seen-events, the notification queue, pacing metadata |
| `notify.py` | Telegram formatting and delivery |
| `cli.py` | Orchestration: collect → select → enrich → queue → drip |
| `config.toml` | All tuning (interests, thresholds, sources, caps) — prefer config over code |

Cost note: the owner is cost-sensitive about API spend. Scoring is batched and
enrichment runs **only** for events that will actually post — preserve that
ordering, and don't add per-event API calls to the ingest path.
