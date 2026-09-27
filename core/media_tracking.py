# path: core/media_tracking.py
"""Centralized media tracking system for coordinating between plugins"""
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from enum import Enum

from core.logging import get_logger

logger = get_logger(__name__)

# Storage file for tracked media
TRACKING_FILE = Path("config/media_tracking.json")


class DownloadStatus(Enum):
    """Status of a tracked download"""
    PENDING = "pending"  # Approved in Overseerr, not yet grabbed
    DOWNLOADING = "downloading"  # Grabbed by arr, in download client
    IMPORTING = "importing"  # Downloaded, being imported by arr
    PARTIAL = "partial"  # Some episodes/seasons available
    COMPLETE = "complete"  # All expected content available
    FAILED = "failed"  # Download or import failed


@dataclass
class EpisodeProgress:
    """Track progress of individual episodes"""
    episode_number: int
    title: Optional[str] = None
    downloaded: bool = False
    available_in_plex: bool = False
    air_date: Optional[str] = None


@dataclass
class TrackedMedia:
    """Represents media being tracked across the request → download → import pipeline"""

    # Identification
    tmdb_id: int
    media_type: str  # "movie" or "tv"
    title: str

    # For TV shows
    season_number: Optional[int] = None
    expected_episode_count: Optional[int] = None
    episodes: Optional[List[EpisodeProgress]] = None

    # Request tracking
    requester_user_id: Optional[int] = None
    monitor: bool = False  # For ongoing series
    requested_seasons: Optional[List[int]] = None  # Which seasons were requested

    # Download tracking
    download_id: Optional[str] = None  # Sonarr/Radarr downloadId (SABnzbd nzo_id)
    is_season_pack: bool = False
    download_status: str = DownloadStatus.PENDING.value

    # Discord tracking
    notification_message_id: Optional[int] = None  # Message ID in updates channel
    notification_channel_id: Optional[int] = None

    # Metadata
    request_timestamp: str = None  # When user requested
    download_start_timestamp: Optional[str] = None  # When download started
    first_episode_timestamp: Optional[str] = None  # When first episode arrived in Plex
    completion_timestamp: Optional[str] = None  # When all content arrived

    # Additional data
    poster_url: Optional[str] = None
    overview: Optional[str] = None
    requester_notification_sent: bool = False
    requester_notification_sent_at: Optional[str] = None
    requester_notification_reason: Optional[str] = None

    def __post_init__(self):
        if self.request_timestamp is None:
            self.request_timestamp = datetime.now(timezone.utc).isoformat()
        if self.episodes is None and self.expected_episode_count:
            self.episodes = [
                EpisodeProgress(episode_number=i)
                for i in range(1, self.expected_episode_count + 1)
            ]

    def get_available_episode_count(self) -> int:
        """Count how many episodes are available in Plex"""
        if not self.episodes:
            return 0
        return sum(1 for ep in self.episodes if ep.available_in_plex)

    def get_download_progress_percentage(self) -> float:
        """Calculate download progress as percentage"""
        if not self.expected_episode_count or self.expected_episode_count == 0:
            return 0.0
        return (self.get_available_episode_count() / self.expected_episode_count) * 100

    def requested_season_numbers(self) -> Optional[List[int]]:
        """Return requested seasons as normalized ints, or None for non-season-specific requests."""
        if self.requested_seasons == "all":
            return None
        if isinstance(self.requested_seasons, list):
            normalized = []
            for season in self.requested_seasons:
                try:
                    normalized.append(int(season))
                except (TypeError, ValueError):
                    continue
            return sorted(set(normalized)) or None
        if self.season_number is not None:
            return [int(self.season_number)]
        return None

    def should_notify_for_movie_arrival(self) -> bool:
        """Whether a requester DM should be sent for a movie arrival."""
        return (
            self.media_type == "movie"
            and bool(self.requester_user_id)
            and not self.requester_notification_sent
        )

    def should_notify_for_episode_arrival(self, season_number: int, episode_number: int) -> bool:
        """Whether a requester DM should be sent for this episode arrival."""
        if self.media_type != "tv" or not self.requester_user_id or self.requester_notification_sent:
            return False
        if episode_number != 1:
            return False

        requested_seasons = self.requested_season_numbers()
        if requested_seasons:
            return season_number == min(requested_seasons)

        if self.season_number is not None:
            return season_number == int(self.season_number)

        return True

    def mark_requester_notified(self, reason: str):
        """Persist that the requester has already been notified about Plex availability."""
        self.requester_notification_sent = True
        self.requester_notification_reason = reason
        if not self.requester_notification_sent_at:
            self.requester_notification_sent_at = datetime.now(timezone.utc).isoformat()

    def is_complete(self) -> bool:
        """Check if all expected content has arrived"""
        if self.media_type == "movie":
            return self.download_status == DownloadStatus.COMPLETE.value

        if not self.expected_episode_count:
            return False

        return self.get_available_episode_count() >= self.expected_episode_count

    def mark_episode_available(self, episode_number: int, title: Optional[str] = None):
        """Mark an episode as available in Plex"""
        if not self.episodes:
            return

        for ep in self.episodes:
            if ep.episode_number == episode_number:
                ep.available_in_plex = True
                if title:
                    ep.title = title
                break

        # Update status
        if self.is_complete():
            self.download_status = DownloadStatus.COMPLETE.value
            if not self.completion_timestamp:
                self.completion_timestamp = datetime.now(timezone.utc).isoformat()
        elif self.get_available_episode_count() > 0:
            self.download_status = DownloadStatus.PARTIAL.value
            if not self.first_episode_timestamp:
                self.first_episode_timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization"""
        data = asdict(self)
        # Convert EpisodeProgress objects to dicts
        if data.get('episodes'):
            data['episodes'] = [
                asdict(ep) if isinstance(ep, EpisodeProgress) else ep
                for ep in data['episodes']
            ]
        return data

    @classmethod
    def from_dict(cls, data: Dict) -> "TrackedMedia":
        """Create from dictionary"""
        # Convert episode dicts to EpisodeProgress objects
        if data.get('episodes'):
            data['episodes'] = [
                EpisodeProgress(**ep) if isinstance(ep, dict) else ep
                for ep in data['episodes']
            ]
        return cls(**data)


class MediaTrackingManager:
    """Manages tracked media across the request → download → import pipeline"""

    def __init__(self):
        self.tracked_media: Dict[str, TrackedMedia] = {}
        self.load_tracking_data()

    def _get_tracking_key(self, tmdb_id: int, season_number: Optional[int] = None) -> str:
        """Generate unique tracking key"""
        if season_number is not None:
            return f"tv:{tmdb_id}:s{season_number}"
        return f"movie:{tmdb_id}" if tmdb_id else f"tv:{tmdb_id}"

    def load_tracking_data(self):
        """Load tracking data from file"""
        if TRACKING_FILE.exists():
            try:
                with open(TRACKING_FILE) as f:
                    data = json.load(f)
                    for key, media_data in data.items():
                        self.tracked_media[key] = TrackedMedia.from_dict(media_data)
                logger.info(f"Loaded {len(self.tracked_media)} tracked media items")
            except Exception as e:
                logger.error(f"Error loading media tracking data: {e}")

    def save_tracking_data(self):
        """Save tracking data to file atomically.

        Writes to a temp file in the same directory then os.replace()s it into
        place, which is atomic on POSIX. A plain open(path, "w") truncates first,
        so a crash or container restart mid-write (this is called from the Sonarr
        and Radarr webhook request paths) left invalid JSON - and
        load_tracking_data then silently started from an empty dict, losing every
        requester mapping.
        """
        try:
            TRACKING_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {key: media.to_dict() for key, media in self.tracked_media.items()}
            tmp_path = TRACKING_FILE.with_suffix(TRACKING_FILE.suffix + ".tmp")
            with open(tmp_path, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, TRACKING_FILE)
        except Exception as e:
            logger.error(f"Error saving media tracking data: {e}")

    def register_request(
        self,
        tmdb_id: int,
        media_type: str,
        title: str,
        requester_user_id: int,
        season_number: Optional[int] = None,
        requested_seasons: Optional[List[int]] = None,
        monitor: bool = False,
        poster_url: Optional[str] = None,
        overview: Optional[str] = None,
    ) -> TrackedMedia:
        """Register a new media request from media_requests plugin"""
        key = self._get_tracking_key(tmdb_id, season_number)

        tracked = TrackedMedia(
            tmdb_id=tmdb_id,
            media_type=media_type,
            title=title,
            season_number=season_number,
            requester_user_id=requester_user_id,
            requested_seasons=requested_seasons,
            monitor=monitor,
            poster_url=poster_url,
            overview=overview,
        )

        self.tracked_media[key] = tracked
        self.save_tracking_data()

        logger.info(f"Registered request tracking for {title} (TMDB: {tmdb_id})")
        return tracked

    def update_from_sonarr_grab(
        self,
        tmdb_id: int,
        title: str,
        season_number: int,
        episodes: List[Dict[str, Any]],
        download_id: str,
    ):
        """Update tracking when Sonarr grabs a download (OnGrab webhook)"""
        key = self._get_tracking_key(tmdb_id, season_number)

        # Get or create tracking entry
        if key not in self.tracked_media:
            tracked = TrackedMedia(
                tmdb_id=tmdb_id,
                media_type="tv",
                title=title,
                season_number=season_number,
            )
            self.tracked_media[key] = tracked
        else:
            tracked = self.tracked_media[key]

        # Update with download info
        tracked.download_id = download_id
        tracked.is_season_pack = len(episodes) > 1
        tracked.expected_episode_count = len(episodes)
        tracked.download_status = DownloadStatus.DOWNLOADING.value
        tracked.download_start_timestamp = datetime.now(timezone.utc).isoformat()

        # Initialize episode list
        tracked.episodes = [
            EpisodeProgress(
                episode_number=ep.get('episodeNumber', 0),
                title=ep.get('title'),
                air_date=ep.get('airDate'),
            )
            for ep in episodes
        ]

        self.save_tracking_data()
        logger.info(f"Updated from Sonarr grab: {title} S{season_number} - {len(episodes)} episodes")

    def is_monitored(self, tmdb_id: int, season_number: Optional[int] = None) -> bool:
        """Check if media is being tracked"""
        key = self._get_tracking_key(tmdb_id, season_number)
        return key in self.tracked_media

    def get_tracked_media(
        self, tmdb_id: int, season_number: Optional[int] = None
    ) -> Optional[TrackedMedia]:
        """Get tracked media by TMDB ID and season"""
        key = self._get_tracking_key(tmdb_id, season_number)
        tracked = self.tracked_media.get(key)
        if tracked:
            return tracked

        if season_number is not None:
            return self.tracked_media.get(f"tv:{tmdb_id}")

        return None

    def get_by_download_id(self, download_id: str) -> Optional[TrackedMedia]:
        """Find tracked media by download ID"""
        for tracked in self.tracked_media.values():
            if tracked.download_id == download_id:
                return tracked
        return None

    def mark_episode_available(
        self,
        tmdb_id: int,
        season_number: int,
        episode_number: int,
        episode_title: Optional[str] = None,
    ) -> Optional[TrackedMedia]:
        """Mark an episode as available in Plex and check if notification should be sent"""
        tracked = self.get_tracked_media(tmdb_id, season_number)

        if not tracked:
            return None

        # Check completion status before marking
        was_complete = tracked.is_complete()

        tracked.mark_episode_available(episode_number, episode_title)
        self.save_tracking_data()

        logger.info(
            f"Marked {tracked.title} S{season_number}E{episode_number:02d} as available "
            f"({tracked.get_available_episode_count()}/{tracked.expected_episode_count})"
        )

        # Check if this episode completed the media
        if not was_complete and tracked.is_complete():
            logger.info(f"🎉 Media complete: {tracked.title} - ready for notification")

        return tracked

    def update_notification_message(
        self,
        tmdb_id: int,
        season_number: Optional[int],
        message_id: int,
        channel_id: int,
    ):
        """Store Discord message ID for updates"""
        key = self._get_tracking_key(tmdb_id, season_number)
        tracked = self.tracked_media.get(key)

        if tracked:
            tracked.notification_message_id = message_id
            tracked.notification_channel_id = channel_id
            self.save_tracking_data()

    def cleanup_old_completed(self, days: int = 7):
        """Remove completed tracking entries older than specified days"""

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        to_remove = []

        for key, tracked in self.tracked_media.items():
            if tracked.completion_timestamp:
                completion = datetime.fromisoformat(tracked.completion_timestamp)
                if completion < cutoff:
                    to_remove.append(key)

        for key in to_remove:
            del self.tracked_media[key]

        if to_remove:
            self.save_tracking_data()
            logger.info(f"Cleaned up {len(to_remove)} old completed tracking entries")


# Global instance
_tracking_manager: Optional[MediaTrackingManager] = None


def get_media_tracker() -> MediaTrackingManager:
    """Get the global media tracking manager instance"""
    global _tracking_manager
    if _tracking_manager is None:
        _tracking_manager = MediaTrackingManager()
    return _tracking_manager
