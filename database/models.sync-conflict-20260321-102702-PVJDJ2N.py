# path: database/models.py
"""SQLAlchemy models for Plexbie

Note: Plugin-specific models are defined in their respective plugin directories.
This file contains only shared/core models.
"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Integer, String, JSON
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()


def utc_now() -> datetime:
    """Return current UTC datetime (timezone-aware)"""
    return datetime.now(timezone.utc)


class PluginConfig(Base):
    """Per-guild plugin configuration"""
    __tablename__ = "plugin_config"

    id = Column(Integer, primary_key=True)
    guild_id = Column(String(32), nullable=False)
    plugin_name = Column(String(64), nullable=False)
    enabled = Column(Boolean, default=True)
    config = Column(JSON, default=dict)
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)


# Import plugin models to register them with Base
# These imports must be at the bottom to avoid circular imports
from plugins.user_mgmt.models import PlexUser  # noqa: E402, F401
from plugins.invite_tracker.models import InviteTracker, InviteUse  # noqa: E402, F401
from plugins.watch_party.models import WatchParty, WatchPartyCredit, WatchPartyParticipant  # noqa: E402, F401
