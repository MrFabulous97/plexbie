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
        self.bound_addresses = []
        # Services whose routes actually run through add_validated_post, so the
        # startup banner can report what is really protected.
        self._registered_services = set()
        self.setup_routes()

    def add_validated_post(self, path: str, handler, service: str):
        """Register a POST webhook route behind signature validation.

        Every inbound webhook route must be registered through here. Calling
        ``self.app.router.add_post`` directly silently bypasses authentication,
        which is how /webhook/sonarr, /webhook/radarr and /webhook/plex ended up
        unauthenticated while the startup banner claimed otherwise.
        """
        self.app.router.add_post(
            path,
            create_validated_handler(handler, self.validator, service)
        )
        self._registered_services.add(service)

    def setup_routes(self):
        """Configure webhook endpoints with validation"""
        self.add_validated_post("/webhook/tautulli", self._handle_tautulli, "tautulli")
        self.add_validated_post("/webhook/overseerr", self._handle_overseerr, "overseerr")
        self.add_validated_post("/webhook/bazarr", self._handle_bazarr, "bazarr")
        self.app.router.add_get("/health", self._handle_health)

    def _bind_addresses(self):
        """Parse WEBHOOK_BIND into an ordered list of addresses."""
        raw = getattr(self.services.config, "webhook_bind", "127.0.0.1") or "127.0.0.1"
        addresses = [a.strip() for a in raw.split(",") if a.strip()]
        return addresses or ["127.0.0.1"]

    async def start(self):
        """Start the webhook server.

        Binds only the configured addresses (loopback by default) rather than
        0.0.0.0. With network_mode: host, 0.0.0.0 published every /webhook/* route
        to the entire LAN, and those routes pass unauthenticated requests through
        whenever the matching secret is unset.
        """
        port = self.services.config.webhook_port
        addresses = self._bind_addresses()

        self.runner = web.AppRunner(self.app)
        await self.runner.setup()

        bound = []
        for address in addresses:
            try:
                await web.TCPSite(self.runner, address, port).start()
                bound.append(address)
            except OSError as e:
                logger.error(f"Could not bind webhook server to {address}:{port} - {e}")

        if not bound:
            raise RuntimeError(
                f"Webhook server could not bind any of {addresses} on port {port}"
            )

        self.bound_addresses = bound
        listening = ", ".join(f"{a}:{port}" for a in bound)
        logger.info(f"Webhook server listening on {listening}")

        if any(a in ("0.0.0.0", "::") for a in bound):
            logger.warning(
                "   ⚠️  Webhook server is bound to all interfaces - every /webhook/* "
                "route is reachable from the whole network. Set WEBHOOK_BIND to "
                "restrict it (default 127.0.0.1)."
            )

        self._log_auth_status()

    def _log_auth_status(self):
        """Report per-route auth status.

        A route is only authenticated when it was registered through
        add_validated_post AND a secret is configured for it, so report the
        intersection. Reporting merely-configured secrets previously told
        operators that Sonarr/Radarr were protected when their routes did not
        run the validator at all.
        """
        protected = []
        unprotected = []

        for service in sorted(self._registered_services):
            if self.validator._get_secret(service):
                protected.append(service.title())
            else:
                unprotected.append(service.title())

        if protected:
            logger.info(f"   🔒 Webhook auth enforced for: {', '.join(protected)}")
        if unprotected:
            logger.warning(
                f"   ⚠️  Unauthenticated webhook routes (no secret set): {', '.join(unprotected)}"
            )
        if not protected:
            logger.warning("   No webhook secrets configured - all webhooks are unauthenticated")

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
