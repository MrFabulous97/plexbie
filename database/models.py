# path: database/models.py
"""SQLAlchemy models for Plexbie"""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, JSON
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()


class User(Base):
    """Discord-Plex user mapping"""
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True)
    discord_id = Column(String(32), unique=True, nullable=False)
    plex_username = Column(String(255), unique=True)
    plex_email = Column(String(255))
    plex_id = Column(Integer)
    linked_at = Column(DateTime, default=datetime.utcnow)
    is_admin = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    user_metadata = Column(JSON, default=dict)


class MediaRequest(Base):
    """Media request tracking"""
    __tablename__ = "media_requests"
    
    id = Column(Integer, primary_key=True)
    discord_id = Column(String(32), nullable=False)
    request_type = Column(String(32))  # movie, tv, music
    title = Column(String(255))
    tmdb_id = Column(Integer)
    status = Column(String(32), default="pending")  # pending, approved, available
    requested_at = Column(DateTime, default=datetime.utcnow)
    approved_at = Column(DateTime)
    request_metadata = Column(JSON, default=dict)


class WatchHistory(Base):
    """User watch history cache"""
    __tablename__ = "watch_history"
    
    id = Column(Integer, primary_key=True)
    discord_id = Column(String(32), nullable=False)
    media_id = Column(String(64))
    media_type = Column(String(32))
    title = Column(String(255))
    watched_at = Column(DateTime)
    duration = Column(Integer)  # seconds
    progress = Column(Integer)  # percentage


class ServerStatus(Base):
    """Plex server status snapshots"""
    __tablename__ = "server_status"
    
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    is_online = Column(Boolean)
    active_streams = Column(Integer, default=0)
    total_bandwidth = Column(Integer, default=0)  # kbps
    library_counts = Column(JSON, default=dict)
    server_metadata = Column(JSON, default=dict)


class PluginConfig(Base):
    """Per-guild plugin configuration"""
    __tablename__ = "plugin_config"

    id = Column(Integer, primary_key=True)
    guild_id = Column(String(32), nullable=False)
    plugin_name = Column(String(64), nullable=False)
    enabled = Column(Boolean, default=True)
    config = Column(JSON, default=dict)


class KeyValueStore(Base):
    """Generic key-value store for plugin data"""
    __tablename__ = "key_value_store"

    id = Column(Integer, primary_key=True)
    namespace = Column(String(64), nullable=False, index=True)
    key = Column(String(255), nullable=False, index=True)
    value = Column(Text, nullable=False)


# Import plugin models to register them with Base
from plugins.user_mgmt.models import PlexUser  # noqa: E402
from plugins.invite_tracker.models import InviteTracker, InviteUse  # noqa: E402
