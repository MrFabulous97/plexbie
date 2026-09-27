# path: webhooks/radarr_handler.py
"""Radarr webhook handler for tracking movie downloads"""
import json
from typing import Dict, Any

from aiohttp import web

from core.logging import get_logger
from core.media_tracking import get_media_tracker

logger = get_logger(__name__)


class RadarrWebhookHandler:
    """Handles webhooks from Radarr"""

    def __init__(self, bot):
        self.bot = bot
        self.tracker = get_media_tracker()

    async def handle_webhook(self, request: web.Request) -> web.Response:
        """Process incoming Radarr webhook"""
        try:
            payload = await request.json()
            event_type = payload.get("eventType")

            logger.debug(f"Received Radarr webhook: {event_type}")

            if event_type == "Grab":
                await self._handle_grab(payload)
            elif event_type == "Download":
                await self._handle_download(payload)
            elif event_type == "MovieAdded":
                await self._handle_movie_add(payload)
            elif event_type == "Test":
                logger.info("Radarr webhook test successful")

            return web.json_response({"status": "ok"})

        except Exception as e:
            logger.error(f"Error processing Radarr webhook: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=400)

    async def _handle_grab(self, payload: Dict[str, Any]):
        """Handle OnGrab event - download started"""
        try:
            movie = payload.get("movie", {})
            release = payload.get("release", {})
            download_id = payload.get("downloadId", "")

            title = movie.get("title", "Unknown")
            tmdb_id = movie.get("tmdbId")
            year = movie.get("year")

            logger.info(
                f"Radarr Grab: {title} ({year}) - TMDB: {tmdb_id}"
            )

            # For movies, we mainly just log this
            # The Plex webhook will handle the actual notification
            # But we can update tracking if the movie was requested

            if tmdb_id:
                tracked = self.tracker.get_tracked_media(tmdb_id)
                if tracked:
                    tracked.download_id = download_id
                    tracked.download_status = "downloading"
                    self.tracker.save_tracking_data()
                    logger.info(f"Updated tracking for requested movie: {title}")

        except Exception as e:
            logger.error(f"Error handling Radarr Grab event: {e}", exc_info=True)

    async def _handle_download(self, payload: Dict[str, Any]):
        """Handle OnDownload event - movie imported successfully"""
        try:
            movie = payload.get("movie", {})
            movie_file = payload.get("movieFile", {})
            is_upgrade = payload.get("isUpgrade", False)

            title = movie.get("title", "Unknown")
            tmdb_id = movie.get("tmdbId")

            logger.info(
                f"Radarr Download: {title} imported"
                f"{' (upgrade)' if is_upgrade else ''}"
            )

            # Movie is now in Plex, but Plex webhook will handle the notification

        except Exception as e:
            logger.error(f"Error handling Radarr Download event: {e}", exc_info=True)

    async def _handle_movie_add(self, payload: Dict[str, Any]):
        """Handle OnMovieAdded event - new movie added to Radarr"""
        try:
            movie = payload.get("movie", {})
            title = movie.get("title", "Unknown")

            logger.info(f"Radarr Movie Add: {title}")

        except Exception as e:
            logger.error(f"Error handling Radarr MovieAdded event: {e}", exc_info=True)


def register_radarr_webhook(webhook_server, bot):
    """Register Radarr webhook route behind signature validation.

    Takes the WebhookServer (not its raw aiohttp app) so the route goes through
    add_validated_post. See register_sonarr_webhook.
    """
    handler = RadarrWebhookHandler(bot)

    async def webhook_endpoint(request: web.Request) -> web.Response:
        return await handler.handle_webhook(request)

    webhook_server.add_validated_post("/webhook/radarr", webhook_endpoint, "radarr")
    logger.info("✅ Registered Radarr webhook handler at /webhook/radarr")
