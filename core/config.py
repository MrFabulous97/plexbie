# path: core/config.py
"""Configuration management for Plexbie"""

import os
from pathlib import Path
from typing import Optional, Any

import yaml
from pydantic import BaseModel, Field, validator


def _int_or_none(value: Optional[str]) -> Optional[int]:
    """Safely coerce environment/YAML values to int or None."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class Config(BaseModel):
    """Bot configuration loaded from environment variables, then overlaid by config/config.yml if present."""

    # Discord
    discord_bot_token: str = Field(default_factory=lambda: os.getenv("DISCORD_BOT_TOKEN", ""))
    guild_id: Optional[int] = Field(default_factory=lambda: _int_or_none(os.getenv("GUILD_ID")))
    bot_owner_id: Optional[int] = Field(default_factory=lambda: _int_or_none(os.getenv("BOT_OWNER_ID")))

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
    bookshelf_settle_seconds: int = Field(default_factory=lambda: int(os.getenv("BOOKSHELF_SETTLE_SECONDS", "120")))

    # Redis
    redis_url: Optional[str] = Field(default_factory=lambda: os.getenv("REDIS_URL"))

    # Database
    db_url: str = Field(default_factory=lambda: os.getenv("DB_URL", "sqlite:///config/plexbie.db"))

    # Logging
    log_level: str = Field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    # Webhook server (for inbound events)
    webhook_port: int = Field(default_factory=lambda: int(os.getenv("WEBHOOK_PORT", "8080")))
    webhook_path: str = Field(default_factory=lambda: os.getenv("WEBHOOK_PATH", "/webhook"))
    sonarr_webhook_secret: Optional[str] = Field(default_factory=lambda: os.getenv("SONARR_WEBHOOK_SECRET"))
    radarr_webhook_secret: Optional[str] = Field(default_factory=lambda: os.getenv("RADARR_WEBHOOK_SECRET"))
    tautulli_webhook_secret: Optional[str] = Field(default_factory=lambda: os.getenv("TAUTULLI_WEBHOOK_SECRET"))
    overseerr_webhook_secret: Optional[str] = Field(default_factory=lambda: os.getenv("OVERSEERR_WEBHOOK_SECRET"))

    # ---- Additional config matching original bot features ----
    # Discord resource IDs (stored as int for discord.py)
    admin_channel_id: Optional[int] = Field(default_factory=lambda: _int_or_none(os.getenv("ADMIN_CHANNEL_ID")))
    stats_channel_id: Optional[int] = Field(default_factory=lambda: _int_or_none(os.getenv("STATS_CHANNEL_ID")))
    updates_channel_id: Optional[int] = Field(default_factory=lambda: _int_or_none(os.getenv("UPDATES_CHANNEL_ID")))
    plex_member_role_id: Optional[int] = Field(default_factory=lambda: _int_or_none(os.getenv("PLEX_MEMBER_ROLE_ID")))
    required_role_id: Optional[int] = Field(default_factory=lambda: _int_or_none(os.getenv("DISCORD_REQUIRED_ROLE_ID")))
    webhook_notification_channel: Optional[int] = Field(default_factory=lambda: _int_or_none(os.getenv("WEBHOOK_NOTIFICATION_CHANNEL")))
    admin_role_id: Optional[int] = Field(default_factory=lambda: _int_or_none(os.getenv("ADMIN_ROLE_ID")))
    homies_role_id: Optional[int] = Field(default_factory=lambda: _int_or_none(os.getenv("HOMIES_ROLE_ID")))
    nerd_role_id: Optional[int] = Field(default_factory=lambda: _int_or_none(os.getenv("NERD_ROLE_ID")))
    dumb_role_id: Optional[int] = Field(default_factory=lambda: _int_or_none(os.getenv("DUMB_ROLE_ID")))
    welcome_channel_id: Optional[int] = Field(default_factory=lambda: _int_or_none(os.getenv("WELCOME_CHANNEL_ID")))
    nerd_message_id: Optional[int] = Field(default_factory=lambda: _int_or_none(os.getenv("NERD_MESSAGE_ID")))
    dumb_cat_emoji_id: Optional[int] = Field(default_factory=lambda: _int_or_none(os.getenv("DUMB_CAT_EMOJI_ID")))
    nerd_cat_emoji_id: Optional[int] = Field(default_factory=lambda: _int_or_none(os.getenv("NERD_CAT_EMOJI_ID")))
    you_are_dumb_channel_id: Optional[int] = Field(default_factory=lambda: _int_or_none(os.getenv("YOU_ARE_DUMB_CHANNEL_ID")))
    forever_dumb_role_id: Optional[int] = Field(default_factory=lambda: _int_or_none(os.getenv("FOREVER_DUMB_ROLE_ID")))
    forever_dumb_message_id: Optional[int] = Field(default_factory=lambda: _int_or_none(os.getenv("FOREVER_DUMB_MESSAGE_ID")))
    dumb_family_message_id: Optional[int] = Field(default_factory=lambda: _int_or_none(os.getenv("DUMB_FAMILY_MESSAGE_ID")))
    nerds_but_dumb_role_id: Optional[int] = Field(default_factory=lambda: _int_or_none(os.getenv("NERDS_BUT_DUMB_ROLE_ID")))

    # Watch tracking persistent message IDs
    now_watching_message_id: Optional[int] = Field(default_factory=lambda: _int_or_none(os.getenv("NOW_WATCHING_MESSAGE_ID")))
    watch_streak_message_id: Optional[int] = Field(default_factory=lambda: _int_or_none(os.getenv("WATCH_STREAK_MESSAGE_ID")))
    leaderboard_message_id: Optional[int] = Field(default_factory=lambda: _int_or_none(os.getenv("LEADERBOARD_MESSAGE_ID")))

    # External APIs
    tmdb_api_key: Optional[str] = Field(default_factory=lambda: os.getenv("TMDB_API_KEY"))

    # Plugin-specific config
    watch_party_channel_id: Optional[int] = Field(default_factory=lambda: _int_or_none(os.getenv("WATCH_PARTY_CHANNEL_ID")))
    watch_party_credit_interval: int = Field(default_factory=lambda: int(os.getenv("WATCH_PARTY_CREDIT_INTERVAL", "300")))
    health_check_interval: int = Field(default_factory=lambda: int(os.getenv("HEALTH_CHECK_INTERVAL", "300")))
    inactivity_warning_days: int = Field(default_factory=lambda: int(os.getenv("INACTIVITY_WARNING_DAYS", "25")))
    inactivity_removal_days: int = Field(default_factory=lambda: int(os.getenv("INACTIVITY_REMOVAL_DAYS", "30")))

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
                    coerced = _int_or_none(value) if key != "webhook_port" else int(value)
                    setattr(self, key, coerced)
                else:
                    setattr(self, key, value)

    @validator("discord_bot_token")
    def validate_discord_token(cls, v: str) -> str:
        if not v:
            raise ValueError("DISCORD_BOT_TOKEN is required")
        return v
