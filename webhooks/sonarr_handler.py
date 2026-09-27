# path: webhooks/sonarr_handler.py
"""Sonarr webhook handler for tracking TV show downloads"""
import json
from typing import Dict, Any

from aiohttp import web

from core.logging import get_logger
from core.media_tracking import get_media_tracker

logger = get_logger(__name__)


class SonarrWebhookHandler:
    """Handles webhooks from Sonarr"""

    def __init__(self, bot):
        self.bot = bot
        self.tracker = get_media_tracker()

    async def handle_webhook(self, request: web.Request) -> web.Response:
        """Process incoming Sonarr webhook"""
        try:
            payload = await request.json()
            event_type = payload.get("eventType")

            logger.debug(f"Received Sonarr webhook: {event_type}")

            if event_type == "Grab":
                await self._handle_grab(payload)
            elif event_type == "Download":
                await self._handle_download(payload)
            elif event_type == "SeriesAdd":
                await self._handle_series_add(payload)
            elif event_type == "Test":
                logger.info("Sonarr webhook test successful")

            return web.json_response({"status": "ok"})

        except Exception as e:
            logger.error(f"Error processing Sonarr webhook: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=400)

    async def _handle_grab(self, payload: Dict[str, Any]):
        """Handle OnGrab event - download started"""
        try:
            series = payload.get("series", {})
            episodes = payload.get("episodes", [])
            release = payload.get("release", {})
            download_id = payload.get("downloadId", "")

            if not episodes:
                logger.warning("Grab event with no episodes")
                return

            # Extract series info
            title = series.get("title", "Unknown")
            tmdb_id = None
            tvdb_id = series.get("tvdbId")
            imdb_id = series.get("imdbId")

            # Try to get TMDB ID (Sonarr doesn't always provide this directly)
            # We may need to query Sonarr API or match by title
            # For now, log what we have
            logger.info(
                f"Sonarr Grab: {title} - {len(episodes)} episode(s)"
                f" (TVDB: {tvdb_id}, IMDB: {imdb_id})"
            )

            # Get season number from first episode
            if episodes:
                season_number = episodes[0].get("seasonNumber", 0)

                # Update tracking
                # Note: We need TMDB ID for proper tracking
                # If we don't have it, try to find existing tracking by title
                tracked = None

                # Try to find by TMDB ID if we have it
                if tmdb_id:
                    self.tracker.update_from_sonarr_grab(
                        tmdb_id=tmdb_id,
                        title=title,
                        season_number=season_number,
                        episodes=episodes,
                        download_id=download_id,
                    )
                else:
                    # Try to find existing tracked media by title/season
                    # This is for cases where media was requested first
                    for tracked_media in self.tracker.tracked_media.values():
                        if (
                            tracked_media.media_type == "tv"
                            and tracked_media.title.lower() == title.lower()
                            and tracked_media.season_number == season_number
                        ):
                            # Update existing tracking with download info
                            tracked_media.download_id = download_id
                            tracked_media.is_season_pack = len(episodes) > 1
                            tracked_media.expected_episode_count = len(episodes)
                            tracked_media.download_status = "downloading"

                            # Initialize episodes if not already done
                            if not tracked_media.episodes:
                                from core.media_tracking import EpisodeProgress

                                tracked_media.episodes = [
                                    EpisodeProgress(
                                        episode_number=ep.get("episodeNumber", 0),
                                        title=ep.get("title"),
                                        air_date=ep.get("airDate"),
                                    )
                                    for ep in episodes
                                ]

                            self.tracker.save_tracking_data()
                            logger.info(
                                f"Updated existing tracking for {title} S{season_number}"
                            )
                            break

                logger.info(
                    f"Grab event processed: {title} S{season_number} "
                    f"({'season pack' if len(episodes) > 1 else 'single episode'})"
                )

        except Exception as e:
            logger.error(f"Error handling Sonarr Grab event: {e}", exc_info=True)

    async def _handle_download(self, payload: Dict[str, Any]):
        """Handle OnDownload event - episode imported successfully"""
        try:
            series = payload.get("series", {})
            episodes = payload.get("episodes", [])
            episode_file = payload.get("episodeFile", {})
            is_upgrade = payload.get("isUpgrade", False)

            title = series.get("title", "Unknown")
            tvdb_id = series.get("tvdbId")

            logger.info(
                f"Sonarr Download: {title} - {len(episodes)} episode(s) imported"
                f"{' (upgrade)' if is_upgrade else ''}"
            )

            # Episodes are now in Plex, but Plex webhook will handle the notification
            # We just log this for now
            for ep in episodes:
                logger.info(
                    f"  S{ep.get('seasonNumber', 0):02d}E{ep.get('episodeNumber', 0):02d} "
                    f"- {ep.get('title', 'Unknown')}"
                )

        except Exception as e:
            logger.error(f"Error handling Sonarr Download event: {e}", exc_info=True)

    async def _handle_series_add(self, payload: Dict[str, Any]):
        """Handle OnSeriesAdd event - new series added to Sonarr"""
        try:
            series = payload.get("series", {})
            title = series.get("title", "Unknown")

            logger.info(f"Sonarr Series Add: {title}")

        except Exception as e:
            logger.error(f"Error handling Sonarr SeriesAdd event: {e}", exc_info=True)


def register_sonarr_webhook(app: web.Application, bot):
    """Register Sonarr webhook route"""
    handler = SonarrWebhookHandler(bot)

    async def webhook_endpoint(request: web.Request) -> web.Response:
        return await handler.handle_webhook(request)

    app.router.add_post("/webhook/sonarr", webhook_endpoint)
    logger.info("✅ Registered Sonarr webhook handler at /webhook/sonarr")
