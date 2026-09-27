# path: core/webhooks.py
"""Webhook server for external service notifications with signature validation"""
import json
from typing import Optional

from aiohttp import web

from core.logging import get_logger
from core.services import BotServices
from core.webhook_security import WebhookValidator, create_validated_handler

logger = get_logger(__name__)


class WebhookServer:
    """HTTP server for receiving webhooks from Tautulli, Overseerr, etc."""

    def __init__(self, services: BotServices):
        self.services = services
        self.app = web.Application()
        self.runner = None
        self.validator = WebhookValidator(services.config)
        self.setup_routes()

    def setup_routes(self):
        """Configure webhook endpoints with validation"""
        # Wrap handlers with signature validation
        self.app.router.add_post(
            "/webhook/tautulli",
            create_validated_handler(self._handle_tautulli, self.validator, "tautulli")
        )
        self.app.router.add_post(
            "/webhook/overseerr",
            create_validated_handler(self._handle_overseerr, self.validator, "overseerr")
        )
        self.app.router.add_post(
            "/webhook/bazarr",
            self._handle_bazarr  # Bazarr doesnt support webhook auth
        )
        self.app.router.add_get("/health", self._handle_health)

    async def start(self):
        """Start the webhook server"""
        port = self.services.config.webhook_port

        self.runner = web.AppRunner(self.app)
        await self.runner.setup()

        site = web.TCPSite(self.runner, "0.0.0.0", port)
        await site.start()

        logger.info(f"Webhook server listening on port {port}")

        # Log webhook security status
        secrets_configured = []
        if self.services.config.sonarr_webhook_secret:
            secrets_configured.append("Sonarr")
        if self.services.config.radarr_webhook_secret:
            secrets_configured.append("Radarr")
        if self.services.config.tautulli_webhook_secret:
            secrets_configured.append("Tautulli")
        if self.services.config.overseerr_webhook_secret:
            secrets_configured.append("Overseerr")

        if secrets_configured:
            services_list = ", ".join(secrets_configured)
            logger.info(f"   Webhook auth enabled for: {services_list}")
        else:
            logger.warning("   No webhook secrets configured - webhooks are unauthenticated")

    async def stop(self):
        """Stop the webhook server"""
        if self.runner:
            await self.runner.cleanup()

    async def _handle_tautulli(self, request: web.Request) -> web.Response:
        """Handle Tautulli webhook events"""
        try:
            # Use pre-validated body if available
            body = request.get("_validated_body")
            if body:
                data = json.loads(body)
            else:
                data = await request.json()

            event_type = data.get("event_type", "unknown")
            logger.info(f"Tautulli webhook received: {event_type}")

            # TODO: Process Tautulli events
            return web.json_response({"status": "ok"})
        except json.JSONDecodeError as e:
            logger.error(f"Tautulli webhook JSON error: {e}")
            return web.json_response({"error": "Invalid JSON"}, status=400)
        except Exception as e:
            logger.error(f"Tautulli webhook error: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_overseerr(self, request: web.Request) -> web.Response:
        """Handle Overseerr webhook events"""
        try:
            body = request.get("_validated_body")
            if body:
                data = json.loads(body)
            else:
                data = await request.json()

            notification_type = data.get("notification_type", "unknown")
            logger.info(f"Overseerr webhook received: {notification_type}")

            # TODO: Process Overseerr events
            return web.json_response({"status": "ok"})
        except json.JSONDecodeError as e:
            logger.error(f"Overseerr webhook JSON error: {e}")
            return web.json_response({"error": "Invalid JSON"}, status=400)
        except Exception as e:
            logger.error(f"Overseerr webhook error: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_bazarr(self, request: web.Request) -> web.Response:
        """Handle Bazarr webhook events"""
        try:
            data = await request.json()
            logger.info("Bazarr webhook received")
            # TODO: Process Bazarr events
            return web.json_response({"status": "ok"})
        except Exception as e:
            logger.error(f"Bazarr webhook error: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint"""
        return web.json_response({"status": "healthy"})
