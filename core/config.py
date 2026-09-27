# path: core/config.py
"""Configuration management for Plexbie"""

import os
from pathlib import Path
from typing import Optional, Any

import yaml
from pydantic import BaseModel, Field, validator

from core.logging import get_logger

logger = get_logger(__name__)


def _int_or_none(value: Any, source: Optional[str] = None) -> Optional[int]:
    """Safely coerce environment/YAML values to int or None.

    Warns when a non-empty value has to be discarded. Silently returning None
    here meant a typo'd setting (e.g. BOT_OWNER_ID=novaora instead of a Discord
    snowflake) produced a bot that started perfectly and simply never applied
    that setting, with nothing in the logs to explain why.
    """
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning(
            f"Ignoring invalid integer for {source or 'config value'}: {value!r} "
            f"- expected a numeric ID. This setting is now INACTIVE."
        )
        return None


def _env_int(name: str) -> Optional[int]:
    """Read an optional integer env var, warning if it is set but unusable."""
    return _int_or_none(os.getenv(name), source=name)


def _env_int_default(name: str, default: int) -> int:
    """Read an integer env var with a fallback, warning if set but unusable.

    Previously these used a bare int(os.getenv(...)), which raised ValueError and
    took the whole bot down on a single typo'd interval - and surfaced only as an
    opaque "Fatal error" with no mention of which variable was at fault.
    """
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning(
            f"Ignoring invalid integer for {name}: {raw!r} - falling back to {default}"
        )
        return default


class Config(BaseModel):
    """Bot configuration loaded from environment variables, then overlaid by config/config.yml if present."""

    # Discord
    discord_bot_token: str = Field(default_factory=lambda: os.getenv("DISCORD_BOT_TOKEN", ""))
    guild_id: Optional[int] = Field(default_factory=lambda: _env_int("GUILD_ID"))
    bot_owner_id: Optional[int] = Field(default_factory=lambda: _env_int("BOT_OWNER_ID"))

    # Plex
    plex_url: str = Field(default_factory=lambda: os.getenv("PLEX_URL", "http://localhost:32400"))
    plex_token: str = Field(default_factory=lambda: os.getenv("PLEX_TOKEN", ""))
    plex_username: Optional[str] = Field(default_factory=lambda: os.getenv("PLEX_USERNAME"))
    plex_password: Optional[str] = Field(default_factory=lambda: os.getenv("PLEX_PASSWORD"))

    # Overseerr
    overseerr_url: str = Field(default_factory=lambda: os.getenv("OVERSEERR_URL", ""))
    overseerr_token: str = Field(default_factory=lambda: os.getenv("OVERSEERR_TOKEN", ""))

    # Tautulli
    tautulli_url: str = Field(default_factory=lambda: os.getenv("TAUTULLI_URL", ""))
    tautulli_token: str = Field(default_factory=lambda: os.getenv("TAUTULLI_TOKEN", ""))

    # Sonarr/Radarr/Lidarr
    sonarr_url: str = Field(default_factory=lambda: os.getenv("SONARR_URL", ""))
    sonarr_token: str = Field(default_factory=lambda: os.getenv("SONARR_TOKEN", ""))
    radarr_url: str = Field(default_factory=lambda: os.getenv("RADARR_URL", ""))
    radarr_token: str = Field(default_factory=lambda: os.getenv("RADARR_TOKEN", ""))
    lidarr_url: str = Field(default_factory=lambda: os.getenv("LIDARR_URL", ""))
    lidarr_token: str = Field(default_factory=lambda: os.getenv("LIDARR_TOKEN", ""))

    # Bazarr
    bazarr_url: str = Field(default_factory=lambda: os.getenv("BAZARR_URL", ""))
    bazarr_token: str = Field(default_factory=lambda: os.getenv("BAZARR_TOKEN", ""))

    # Readarr
    readarr_url: str = Field(default_factory=lambda: os.getenv("READARR_URL", ""))
    readarr_token: str = Field(default_factory=lambda: os.getenv("READARR_TOKEN", ""))

    # NZBHydra
    nzbhydra_url: str = Field(default_factory=lambda: os.getenv("NZBHYDRA_URL", ""))
    nzbhydra_api_key: str = Field(default_factory=lambda: os.getenv("NZBHYDRA_API_KEY", ""))

    # SABnzbd
    sabnzbd_url: str = Field(default_factory=lambda: os.getenv("SABNZBD_URL", ""))
    sabnzbd_api_key: str = Field(default_factory=lambda: os.getenv("SABNZBD_API_KEY", ""))

    # Bookshelf Processor
    bookshelf_audiobook_watch: str = Field(default_factory=lambda: os.getenv("BOOKSHELF_AUDIOBOOK_WATCH", "/watch/audiobooks"))
    bookshelf_ebook_watch: str = Field(default_factory=lambda: os.getenv("BOOKSHELF_EBOOK_WATCH", "/watch/ebooks"))
    bookshelf_audiobook_library: str = Field(default_factory=lambda: os.getenv("BOOKSHELF_AUDIOBOOK_LIBRARY", "/library/audiobooks"))
    bookshelf_ebook_library: str = Field(default_factory=lambda: os.getenv("BOOKSHELF_EBOOK_LIBRARY", "/library/ebooks"))
    bookshelf_settle_seconds: int = Field(default_factory=lambda: _env_int_default("BOOKSHELF_SETTLE_SECONDS", 120))

    # Redis
    redis_url: Optional[str] = Field(default_factory=lambda: os.getenv("REDIS_URL"))

    # Database
    db_url: str = Field(default_factory=lambda: os.getenv("DB_URL", "sqlite:///config/plexbie.db"))

    # Logging
    log_level: str = Field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    # Webhook server (for inbound events)
    webhook_port: int = Field(default_factory=lambda: _env_int_default("WEBHOOK_PORT", 8080))
    # Addresses to bind the webhook server to, comma-separated.
    #
    # Defaults to loopback only. The server previously bound 0.0.0.0, which with
    # network_mode: host exposed every /webhook/* route to the whole LAN - and the
    # routes accept unauthenticated requests whenever a secret is unset.
    #
    # Senders on the host network (Plex) reach the bot on 127.0.0.1. A sender in a
    # bridge-networked container (Sonarr, Radarr, Tautulli, Overseerr) cannot, and
    # needs the bridge gateway added here (e.g. "127.0.0.1,172.17.0.1") plus its
    # notification URL pointed at that address.
    webhook_bind: str = Field(default_factory=lambda: os.getenv("WEBHOOK_BIND", "127.0.0.1"))
    webhook_path: str = Field(default_factory=lambda: os.getenv("WEBHOOK_PATH", "/webhook"))
    sonarr_webhook_secret: Optional[str] = Field(default_factory=lambda: os.getenv("SONARR_WEBHOOK_SECRET"))
    radarr_webhook_secret: Optional[str] = Field(default_factory=lambda: os.getenv("RADARR_WEBHOOK_SECRET"))
    tautulli_webhook_secret: Optional[str] = Field(default_factory=lambda: os.getenv("TAUTULLI_WEBHOOK_SECRET"))
    overseerr_webhook_secret: Optional[str] = Field(default_factory=lambda: os.getenv("OVERSEERR_WEBHOOK_SECRET"))
    # Plex sends no auth of its own; append ?token=<value> to the webhook URL.
    plex_webhook_secret: Optional[str] = Field(default_factory=lambda: os.getenv("PLEX_WEBHOOK_SECRET"))
    # Bazarr has no webhook auth; append ?secret=<value> to the webhook URL.
    bazarr_webhook_secret: Optional[str] = Field(default_factory=lambda: os.getenv("BAZARR_WEBHOOK_SECRET"))

    # ---- Additional config matching original bot features ----
    # Discord resource IDs (stored as int for discord.py)
    admin_channel_id: Optional[int] = Field(default_factory=lambda: _env_int("ADMIN_CHANNEL_ID"))
    stats_channel_id: Optional[int] = Field(default_factory=lambda: _env_int("STATS_CHANNEL_ID"))
    updates_channel_id: Optional[int] = Field(default_factory=lambda: _env_int("UPDATES_CHANNEL_ID"))
    plex_member_role_id: Optional[int] = Field(default_factory=lambda: _env_int("PLEX_MEMBER_ROLE_ID"))
    required_role_id: Optional[int] = Field(default_factory=lambda: _env_int("DISCORD_REQUIRED_ROLE_ID"))
    webhook_notification_channel: Optional[int] = Field(default_factory=lambda: _env_int("WEBHOOK_NOTIFICATION_CHANNEL"))
    admin_role_id: Optional[int] = Field(default_factory=lambda: _env_int("ADMIN_ROLE_ID"))
    homies_role_id: Optional[int] = Field(default_factory=lambda: _env_int("HOMIES_ROLE_ID"))
    nerd_role_id: Optional[int] = Field(default_factory=lambda: _env_int("NERD_ROLE_ID"))
    dumb_role_id: Optional[int] = Field(default_factory=lambda: _env_int("DUMB_ROLE_ID"))
    welcome_channel_id: Optional[int] = Field(default_factory=lambda: _env_int("WELCOME_CHANNEL_ID"))
    nerd_message_id: Optional[int] = Field(default_factory=lambda: _env_int("NERD_MESSAGE_ID"))
    dumb_cat_emoji_id: Optional[int] = Field(default_factory=lambda: _env_int("DUMB_CAT_EMOJI_ID"))
    nerd_cat_emoji_id: Optional[int] = Field(default_factory=lambda: _env_int("NERD_CAT_EMOJI_ID"))
    you_are_dumb_channel_id: Optional[int] = Field(default_factory=lambda: _env_int("YOU_ARE_DUMB_CHANNEL_ID"))
    forever_dumb_role_id: Optional[int] = Field(default_factory=lambda: _env_int("FOREVER_DUMB_ROLE_ID"))
    forever_dumb_message_id: Optional[int] = Field(default_factory=lambda: _env_int("FOREVER_DUMB_MESSAGE_ID"))
    dumb_family_message_id: Optional[int] = Field(default_factory=lambda: _env_int("DUMB_FAMILY_MESSAGE_ID"))
    nerds_but_dumb_role_id: Optional[int] = Field(default_factory=lambda: _env_int("NERDS_BUT_DUMB_ROLE_ID"))

    # Watch tracking persistent message IDs
    now_watching_message_id: Optional[int] = Field(default_factory=lambda: _env_int("NOW_WATCHING_MESSAGE_ID"))
    watch_streak_message_id: Optional[int] = Field(default_factory=lambda: _env_int("WATCH_STREAK_MESSAGE_ID"))
    leaderboard_message_id: Optional[int] = Field(default_factory=lambda: _env_int("LEADERBOARD_MESSAGE_ID"))

    # External APIs
    tmdb_api_key: Optional[str] = Field(default_factory=lambda: os.getenv("TMDB_API_KEY"))

    # Plugin-specific config
    watch_party_channel_id: Optional[int] = Field(default_factory=lambda: _env_int("WATCH_PARTY_CHANNEL_ID"))
    watch_party_credit_interval: int = Field(default_factory=lambda: _env_int_default("WATCH_PARTY_CREDIT_INTERVAL", 300))
    health_check_interval: int = Field(default_factory=lambda: _env_int_default("HEALTH_CHECK_INTERVAL", 300))
    # Consecutive failed checks before the first outage alert is sent. Referenced
    # by service_health but previously never declared, so the failure path raised
    # AttributeError and killed the monitor at the moment of an outage.
    health_max_failures: int = Field(default_factory=lambda: _env_int_default("HEALTH_MAX_FAILURES", 3))
    # Minimum seconds between repeat alerts for the same still-down service.
    health_alert_cooldown: int = Field(default_factory=lambda: _env_int_default("HEALTH_ALERT_COOLDOWN", 1800))
    inactivity_warning_days: int = Field(default_factory=lambda: _env_int_default("INACTIVITY_WARNING_DAYS", 25))
    inactivity_removal_days: int = Field(default_factory=lambda: _env_int_default("INACTIVITY_REMOVAL_DAYS", 30))

    class Config:  # pydantic model config (not the same as this module's class)
        # NOTE: env_file is only respected by pydantic BaseSettings,
        # but we keep it here in case you swap later.
        env_file = ".env"
        env_file_encoding = "utf-8"

    def __init__(self, **data: Any):
        """
        Load from env first (via default_factory), then overlay with YAML if present.
        YAML values that are "truthy" will override env values.
        """
        super().__init__(**data)

        config_path = Path("config/config.yml")
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                yaml_config = yaml.safe_load(f) or {}

            # Apply YAML overrides, coercing numeric Discord IDs to int where relevant
            for key, value in yaml_config.items():
                if value is None or value == "":
                    continue
                if not hasattr(self, key):
                    continue

                # Coerce selected keys to int
                if key in {
                    "guild_id",
                    "bot_owner_id",
                    "admin_channel_id",
                    "stats_channel_id",
                    "updates_channel_id",
                    "plex_member_role_id",
                    "required_role_id",
                    "webhook_notification_channel",
                    "webhook_port",
                    "admin_role_id",
                    "homies_role_id",
                    "nerd_role_id",
                    "dumb_role_id",
                    "welcome_channel_id",
                    "nerd_message_id",
                    "dumb_cat_emoji_id",
                    "nerd_cat_emoji_id",
                    "you_are_dumb_channel_id",
                    "forever_dumb_role_id",
                    "forever_dumb_message_id",
                    "dumb_family_message_id",
                    "nerds_but_dumb_role_id",
                    "now_watching_message_id",
                    "watch_streak_message_id",
                    "leaderboard_message_id",
                }:
                    coerced = _int_or_none(value, source=f"config.yml:{key}")
                    if coerced is None and key == "webhook_port":
                        # webhook_port is non-Optional; keep the env/default value
                        # rather than writing None and failing later at bind time.
                        logger.warning(
                            f"config.yml:webhook_port is not a number ({value!r}) - "
                            f"keeping {self.webhook_port}"
                        )
                        continue
                    setattr(self, key, coerced)
                else:
                    setattr(self, key, value)

    @validator("discord_bot_token")
    def validate_discord_token(cls, v: str) -> str:
        if not v:
            raise ValueError("DISCORD_BOT_TOKEN is required")
        return v
