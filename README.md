# Plexbie

A Discord bot for running a shared Plex server: it handles access requests, tracks
who is still watching, posts what has been added, files media requests, organises
audiobook and ebook downloads, and retires accounts that have gone quiet.

It is built as a plugin host. Each feature lives in its own directory under
`plugins/`, declares itself in a `plugin.json`, and can be turned off without
touching anything else.

---

## Contents

- [What it does](#what-it-does)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Commands](#commands)
- [Webhooks](#webhooks)
- [Plugins](#plugins)
- [Architecture](#architecture)
- [Development](#development)
- [Operations](#operations)
- [Security](#security)
- [Limitations](#limitations)

---

## What it does

**Plex access**

- `/join-plex` collects an email and raises an approval request in an admin
  channel. Approving it invites the account and DMs the user.
- Linked accounts are tracked in a local database, so Discord members and Plex
  accounts stay associated even when the display names differ.
- Accounts are warned after a configurable period of inactivity and removed
  later, with the warning always delivered a full pass before any removal.

**Watching**

- A stats channel carries three self-updating messages: who is watching right
  now, an all-time leaderboard, and daily watch streaks.
- Watch parties: when someone streams Plex over Discord Go Live, everyone in the
  voice channel accrues watch credit, which folds into the leaderboard.

**Media**

- `/request` walks a user through requesting a TV show, film, audiobook or ebook.
- New additions are announced from Plex webhooks, enriched with TMDB metadata,
  and episodes arriving in a batch are collapsed into a single updating message.
- Unwatched media can be reported and, optionally, deleted after a configurable
  period, with an exemption list and a dry-run mode.
- Audiobook and ebook downloads are watched, waited on until they stop changing,
  then renamed and filed into an Audiobookshelf-shaped library.

**Housekeeping**

- Plex and Tautulli are health-checked on a timer, with alerts and recovery
  notices to an admin channel.
- Discord API usage is sampled and logged, per route.
- Invite attribution is recorded, so you can see who brought whom.

---

## Requirements

| | |
|---|---|
| Python | 3.11 |
| Discord | A bot application with the **Server Members** and **Message Content** privileged intents |
| Plex | A server plus an auth token |
| Optional | Tautulli, Overseerr, Redis, a TMDB API key |

Python dependencies are pinned loosely in `requirements.txt`: `discord.py`,
`PlexAPI`, `SQLAlchemy` (asyncio) with `aiosqlite`, `aiohttp`, `pydantic`,
`redis`, `PyYAML`, `python-dotenv`, `mutagen`.

Redis is genuinely optional — without it the bot falls back to an in-process
cache with the same interface.

---

## Quick start

### Docker (recommended)

```bash
git clone https://github.com/MrFabulous97/plexbie.git
cd plexbie
cp config/.env.example config/.env
```

Fill in `config/.env` — at minimum `DISCORD_BOT_TOKEN`, `GUILD_ID`, `PLEX_URL`
and `PLEX_TOKEN`. Then:

```bash
docker build -t plexbie:latest .
```

The committed `docker-compose.yml` uses relative paths, so it works from a fresh
clone as-is:

```bash
docker compose up -d
docker compose logs -f plexbie
```

It mounts `./config` (your `.env` and the SQLite database) and `./logs`. The four
media mounts are only needed for the `bookshelf_processor` plugin — point them at
your download client's completed directories and the library you want built, or
delete them if you do not use it.

`network_mode: host` is deliberate: the webhook listener binds to `127.0.0.1` by
default, which only keeps it off the network if the container shares the host's
network namespace. See [Security](#security) if you change this.

### Without Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config/.env.example config/.env   # then edit it
python -u bot.py
```

The bot reads `config/.env` relative to its working directory, so run it from the
repository root.

### First run

On startup you should see:

```
Redis: Connected (caching enabled)
Plex: Connected to http://...
Plugins: Loaded 15/22 available plugins
Webhook server listening on 127.0.0.1:8080
Commands: 13 synced to guild ...
PLEXBIE DISCORD BOT - READY
```

Slash commands are synced to the guild named by `GUILD_ID`, which takes effect
immediately. If `GUILD_ID` is unset they are registered globally instead, which
can take up to an hour to appear.

---

## Configuration

Everything is read from environment variables, usually via `config/.env`. The
full list lives in `config/.env.example`; this section covers what matters.

### Required

| Variable | Notes |
|---|---|
| `DISCORD_BOT_TOKEN` | Bot token |
| `PLEX_URL` | e.g. `http://192.168.1.50:32400`. Default `http://localhost:32400` |
| `PLEX_TOKEN` | Plex auth token |
| `GUILD_ID` | Without it, commands register globally |

### Strongly recommended

| Variable | Why |
|---|---|
| `BOT_OWNER_ID` | Always counts as an administrator |
| `ADMIN_ROLE_ID` | A role that counts as an administrator |
| `ADMIN_CHANNEL_ID` | Where approval requests and health alerts go |
| `PLEX_USERNAME`, `PLEX_PASSWORD` | Required to *remove* Plex users; invites and reads work without them |

Authorization accepts **any** of: the Discord `administrator` permission, the
`ADMIN_ROLE_ID` role, or `BOT_OWNER_ID`.

### Behaviour

| Variable | Default | Meaning |
|---|---|---|
| `INACTIVITY_WARNING_DAYS` | `25` | Days idle before a warning DM |
| `INACTIVITY_REMOVAL_DAYS` | `30` | Days idle before removal |
| `HEALTH_CHECK_INTERVAL` | `300` | Seconds between service probes |
| `HEALTH_MAX_FAILURES` | `3` | Consecutive failures before alerting |
| `HEALTH_ALERT_COOLDOWN` | `1800` | Seconds between repeat alerts |
| `WATCH_PARTY_CREDIT_INTERVAL` | `300` | Seconds per credit tick |
| `BOOKSHELF_SETTLE_SECONDS` | `120` | How long a download must stop changing before processing |
| `LOG_LEVEL` | `INFO` | |
| `DB_URL` | `sqlite:///config/plexbie.db` | |
| `REDIS_URL` | unset | Falls back to an in-process cache |

> The `@tasks.loop(...)` decorators in the source are **not** the effective
> intervals for the health check or watch party loops — both are overridden from
> config at startup. Read the values above, not the decorators.

### Channels, roles and messages

Many plugins need a channel, role, or the ID of an existing message to keep
updating. Each is optional; the plugin that needs it logs a warning and stands
down if it is missing. The ones you are most likely to want:

`STATS_CHANNEL_ID`, `NOW_WATCHING_MESSAGE_ID`, `LEADERBOARD_MESSAGE_ID`,
`WATCH_STREAK_MESSAGE_ID`, `UPDATES_CHANNEL_ID`, `PLEX_MEMBER_ROLE_ID`,
`WATCH_PARTY_CHANNEL_ID`, `TMDB_API_KEY`.

The three stats messages must already exist — post a placeholder in the channel
and put its ID in the matching variable. The bot only ever edits them; it never
creates them, so it cannot litter the channel if a variable is wrong.

A malformed numeric setting is logged and ignored rather than crashing the bot:

```
Ignoring invalid integer for BOT_OWNER_ID: 'novaora' - expected a numeric ID.
This setting is now INACTIVE.
```

---

## Commands

13 commands are live with the default plugin set. **8 are hidden from ordinary
members** by Discord itself, so a regular user sees 5. Every command is
guild-only and every reply is ephemeral unless stated otherwise.

### Everyone

| Command | Arguments | Description |
|---|---|---|
| `/join-plex` | | Request access to the Plex server |
| `/request` | | Request a TV show, film, audiobook or ebook |
| `/recent` | | Browse recently added media |
| `/status` | | Plex server status, library counts, active streams |
| `/watchparty-stats` | | Your own watch party credit |

### Administrators

| Command | Arguments | Description |
|---|---|---|
| `/cleanup panel` | | Interactive cleanup control panel |
| `/cleanup config` | `inactivity_days?` `notification_channel?` | Change cleanup thresholds |
| `/cleanup exempt add` | `title` `media_type?` | Never clean up this title |
| `/cleanup exempt remove` | `title` | Drop an exemption |
| `/cleanup exempt list` | | Show the exemption list |
| `/list-plex-users` | `show?` | Plex accounts; `show: Only malformed accounts` filters to those needing removal |
| `/list-tracked-users` | | Database view, flagging accounts no longer on Plex |
| `/manage-links` | | Link or unlink a Discord member and a Plex account |
| `/remove-user` | `plex_username` | Remove from Plex, notify, and drop the tracking row |
| `/who-invited` | `member` | Who invited this member |
| `/watchparty-active` | | The watch party in progress |
| `/say` | `channel` `message` | Send a message as the bot (logged) |

Admin visibility is enforced twice: `default_member_permissions` keeps the
command out of the picker, and the handler re-checks at runtime. The second check
is the one that matters — a guild administrator can override the first in server
settings.

Commands from disabled plugins (`/sonarr`, `/radarr`, `/lidarr`, `/stats`,
`/overseerr`, `/search`) are listed under [Plugins](#plugins).

---

## Webhooks

An `aiohttp` listener serves these routes:

| Route | Source | Secret |
|---|---|---|
| `POST /webhook/plex` | Plex | `PLEX_WEBHOOK_SECRET` |
| `POST /webhook/sonarr` | Sonarr | `SONARR_WEBHOOK_SECRET` |
| `POST /webhook/radarr` | Radarr | `RADARR_WEBHOOK_SECRET` |
| `POST /webhook/tautulli` | Tautulli | `TAUTULLI_WEBHOOK_SECRET` |
| `POST /webhook/overseerr` | Overseerr | `OVERSEERR_WEBHOOK_SECRET` |
| `POST /webhook/bazarr` | Bazarr | `BAZARR_WEBHOOK_SECRET` |
| `GET /health` | | Liveness probe, no auth |

`WEBHOOK_BIND` defaults to `127.0.0.1` and `WEBHOOK_PORT` to `8080`.

**If a service's secret is unset, that route accepts unauthenticated requests.**
The bot says so at startup:

```
Unauthenticated webhook routes (no secret set): Bazarr, Overseerr, Plex, ...
```

Loopback binding is what makes that tolerable. Set the secrets before exposing
the listener to anything wider.

How each secret is presented differs by what the service can send:

- **Sonarr / Radarr** send `X-Api-Key`; the value must equal the secret.
- **Tautulli** accepts either an `Authorization: Bearer <secret>` header or
  `?token=<secret>` on the URL.
- **Plex** sends no auth of its own, so append `?token=<secret>` to the webhook
  URL you configure in Plex.
- **Overseerr** must send `Authorization`.
- **Bazarr** takes the secret as a query parameter.

Validation **fails closed**: an unrecognised service with a secret configured is
rejected rather than waved through.

---

## Plugins

A plugin is a directory under `plugins/` containing `cog.py` and `plugin.json`:

```json
{
    "name": "my_plugin",
    "version": "1.0.0",
    "description": "What it does",
    "author": "You",
    "enabled": true,
    "dependencies": [],
    "commands": ["/my-command - What it does"]
}
```

`cog.py` must define a class named from the directory in PascalCase plus `Cog`
(`my_plugin` → `MyPluginCog`) taking `(bot, services)`. The loader imports the
module, instantiates that class and registers it — it does **not** call a
`setup()` function, so anything a discord.py extension would put there belongs in
`__init__` or `cog_load` instead.

Persistent views must be registered in `cog_load`, not `setup`.

To turn a plugin off, set `"enabled": false` and restart. Top-level command names
must be unique across **all** plugins, enabled or not; if two collide, one fails
to load and the log names the command.

### Active by default

| Plugin | Purpose |
|---|---|
| `user_invites` | `/join-plex` and the approval flow |
| `user_mgmt` | Account linking, inactivity tracking, removal |
| `media_requests` | `/request` for TV, film, audiobook, ebook |
| `media_cleanup` | Report and optionally delete unwatched media |
| `new_media_added` | Announce additions from Plex webhooks, with TMDB metadata |
| `recently_added` | `/recent` |
| `watch_tracking` | Now Watching, leaderboard and streak displays |
| `watch_party` | Credit for Discord Go Live streams |
| `bookshelf_processor` | File audiobook and ebook downloads into a library |
| `service_health` | Probe Plex and Tautulli, alert on failure |
| `status` | `/status` and `/say` |
| `invite_tracker` | Invite attribution and an auto-role |
| `self_roles` | Self-assignable role from a button |
| `forever_dumb` | A joke role with nickname enforcement |
| `rate_limit_monitor` | Log Discord API usage per route |

### Stubs, disabled

These are **scaffolding**. Every command answers *"not yet implemented"*. They
are shipped because the configuration and webhook plumbing around them is real,
and because they show the intended shape of an integration.

| Plugin | Commands |
|---|---|
| `sonarr` | `/sonarr search`, `/sonarr add` |
| `radarr` | `/radarr search`, `/radarr queue` |
| `lidarr` | `/lidarr search`, `/lidarr add` |
| `tautulli` | `/stats history`, `/stats top`, `/stats now` |
| `overseerr` | `/overseerr movie`, `/overseerr show`, `/overseerr status` |
| `search` | `/search` |
| `bazarr` | none — receives webhooks only |

Sonarr and Radarr *webhook* handling is implemented (`webhooks/`), independently
of these command stubs.

---

## Architecture

```
bot.py                 entry point, command sync, global error handling
core/
  services.py          dependency container; shared Plex, HTTP, Redis, DB access
  config.py            environment to typed settings
  plugin_manager.py    discovery, load, unload, reload
  permissions.py       is_bot_admin / require_admin / AdminOnlyView
  blocking.py          run_blocking() - the only sanctioned way to call sync I/O
  webhooks.py          aiohttp listener
  webhook_security.py  per-service request validation
  logging.py           JSON logging with size-based rotation
  rate_limit.py        token buckets per external service
  security.py          redaction for logs
database/
  models.py session.py kv_store.py
utils/
  embeds.py formatting.py validators.py
plugins/<name>/        cog.py + plugin.json
webhooks/              Sonarr and Radarr webhook handlers
tests/                 293 tests, standard library only
```

Three conventions matter if you contribute:

**1. Nothing blocking on the event loop.** `plexapi` is entirely synchronous, and
some of its attributes perform HTTP on access — `LibrarySection.totalSize` is a
cached property that issues a request. Wrap a *coarse* unit of work in
`run_blocking()` and return plain data from it; returning a lazy plexapi object
just moves the blocking call back onto the loop. `tests/test_no_blocking_plex_calls.py`
enforces this.

**2. No database transaction across network I/O.** The SQLite database runs in
`delete` journal mode, so a read transaction holds a shared lock for its whole
lifetime and blocks every other writer. Read, close, do the slow work, then write.
`tests/test_efficiency.py` enforces this.

**3. `tasks.loop` stops forever on an unhandled exception.** A loop that watches
something must guard each step, or the first error silently ends the monitoring.

Shared state uses `database/kv_store.py`, a namespaced key-value store with an
atomic upsert. Use `kv_set_many` for more than one key — each `kv_set` is its own
transaction, and a commit on SQLite is an fsync.

---

## Development

```bash
pip install -r requirements-dev.txt
python tests/run_all.py                 # everything
python tests/run_all.py config kv       # only matching modules
pytest tests/                           # also works
```

`tests/run_all.py` uses only the standard library, so the suite runs inside the
production image with no dev dependencies:

```bash
docker run --rm -v "$PWD:/src" -w /src -e PYTHONDONTWRITEBYTECODE=1 \
  plexbie:latest python tests/run_all.py
```

293 tests across 22 modules. A good number are **pattern tests** rather than
tests of one function: they assert a property of the whole codebase, because
several bugs here recurred by being fixed in one place and missed in its
siblings. Those will fail if you reintroduce the shape:

| Module | Asserts |
|---|---|
| `test_codebase_patterns` | Views are gated; DMs are sent in the right order |
| `test_no_blocking_plex_calls` | Synchronous Plex calls go through `run_blocking` |
| `test_efficiency` | No fetch-then-overwrite, no transaction across network I/O, no per-key bulk writes |
| `test_commands` | Command names are unique, admin commands are hidden, `plugin.json` matches reality |

Tests read the real discord.py objects off each Cog where they can, rather than
parsing source, so they describe what Discord would actually be sent.

---

## Operations

**Logs** are JSON, one object per line, to stdout and `logs/plexbie.log`, rotated
at 20 MB with 5 backups (`LOG_MAX_BYTES`, `LOG_BACKUP_COUNT`).

```bash
docker logs -f plexbie
docker exec plexbie sh -c 'grep "\"level\": \"ERROR\"" /app/logs/plexbie.log | tail'
```

**Health**: `curl http://127.0.0.1:8080/health`

**Deploying a change**: rebuild the image and recreate the container. Tag the
previous image first so there is a way back.

```bash
docker tag plexbie:latest plexbie:rollback
docker build -t plexbie:latest .
docker compose up -d --no-deps --force-recreate plexbie
```

`rebuild.sh` does this, waits for the ready line, and prints a startup summary —
but its paths are the maintainer's, so adapt it before use.

**Stale global commands**: if commands ever appear twice in the picker, they are
registered in both the global and guild scopes and Discord merges the two. The
bot clears the global scope on startup when `GUILD_ID` is set, logging
`removing N stale global command(s)`.

---

## Security

- **The webhook listener binds to loopback by default.** Widening `WEBHOOK_BIND`
  exposes every `/webhook/*` route, and a route whose secret is unset accepts
  unauthenticated requests. Set the secrets first.
- **Secrets are compared with `hmac.compare_digest`** on UTF-8 bytes, so a
  non-ASCII secret cannot crash the comparison.
- **Tokens are redacted** from log output, including nested structures.
- **Authorization is checked in the handler**, not inferred from an ephemeral
  reply or from a button being hidden. Persistent views outlive their message and
  re-check on every interaction.
- **Never commit `config/.env`.** It is gitignored, along with `config/*.db` and
  `logs/`.
- `/say` lets an administrator send a message as the bot. Every use is logged
  with the invoking user and target channel. Remove the plugin if you would
  rather not have it.

---

## Limitations

Worth knowing before you rely on this:

- **Seven plugins are stubs.** See [Plugins](#plugins). Their commands answer
  *"not yet implemented"*.
- **Single guild.** Commands sync to one `GUILD_ID`, and channel and role
  settings are single-valued. It is not built to be a multi-server bot.
- **SQLite only.** `DB_URL` is passed to SQLAlchemy, but the schema migration and
  the upsert path are written against SQLite. Keep the database off any
  file-syncing tool — syncing a live SQLite file can corrupt it.
- **Removing a Plex user needs `PLEX_USERNAME` and `PLEX_PASSWORD`**, because it
  goes through plex.tv rather than the local server. Without them the bot keeps
  the tracking row and retries, rather than dropping a user it could not remove.
- **Some Plex system accounts cannot be deleted through the API at all.**
  `/list-plex-users show: Only malformed accounts` reports them with manual
  removal steps.
- **No CI.** Run `tests/run_all.py` yourself.

---

## Licence

MIT - see [LICENSE](LICENSE). Do what you like with it; there is no warranty.


## Contributing

Pull requests are welcome. Please:

1. Run `python tests/run_all.py` — all 293 should pass.
2. Add a test that fails before your change and passes after.
3. Respect the three conventions in [Architecture](#architecture). The pattern
   tests will tell you if you have not.
4. Explain *why* in comments where the reason is not obvious from the code. Much
   of this codebase carries notes about the failure that motivated a given shape;
   keeping those is more useful than tidying them away.
