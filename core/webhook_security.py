# path: core/webhook_security.py
"""Webhook signature validation for secure webhook handling"""
import hashlib
import hmac
from typing import Optional, Tuple

from aiohttp import web

from core.logging import get_logger

logger = get_logger(__name__)


def secure_equals(provided: Optional[str], expected: Optional[str]) -> bool:
    """Constant-time comparison of two secrets supplied as text.

    hmac.compare_digest refuses str arguments containing non-ASCII characters
    (it raises TypeError), so a request carrying a single non-ASCII byte in an
    auth header would otherwise crash the handler and return 500 instead of 401.
    Compare the UTF-8 bytes instead, which is defined for all input.
    """
    if provided is None or expected is None:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


class WebhookValidator:
    """Validates webhook signatures from various services"""

    def __init__(self, config):
        self.config = config

    async def validate_request(
        self,
        request: web.Request,
        service: str
    ) -> Tuple[bool, Optional[str], Optional[bytes]]:
        """
        Validate a webhook request signature.
        
        Returns:
            Tuple of (is_valid, error_message, body_bytes)
            - If secret not configured, returns (True, None, body) to allow passthrough
            - If validation fails, returns (False, error_message, None)
            - If validation succeeds, returns (True, None, body)
        """
        # Get the secret for this service
        secret = self._get_secret(service)
        
        # If no secret configured, allow passthrough but log warning
        if not secret:
            logger.debug(f"No webhook secret configured for {service} - skipping validation")
            body = await request.read()
            return True, None, body

        # Read body
        body = await request.read()
        
        # Validate based on service type
        if service in ("sonarr", "radarr"):
            return self._validate_arr_signature(request, body, secret, service)
        elif service == "tautulli":
            return self._validate_tautulli_signature(request, body, secret)
        elif service == "overseerr":
            return self._validate_overseerr_signature(request, body, secret)
        elif service == "plex":
            return self._validate_plex_signature(request, body, secret)
        elif service == "bazarr":
            return self._validate_shared_secret(request, body, secret, service)
        else:
            # A secret is configured but we have no validator for this service.
            # Fail closed: silently accepting would defeat the operator's intent.
            logger.error(
                f"Webhook secret configured for unknown service '{service}' - "
                f"rejecting request because it cannot be validated"
            )
            return False, "Unvalidatable service", None

    def _get_secret(self, service: str) -> Optional[str]:
        """Get webhook secret for a service from config"""
        secret_map = {
            "sonarr": getattr(self.config, "sonarr_webhook_secret", None),
            "radarr": getattr(self.config, "radarr_webhook_secret", None),
            "tautulli": getattr(self.config, "tautulli_webhook_secret", None),
            "overseerr": getattr(self.config, "overseerr_webhook_secret", None),
            "plex": getattr(self.config, "plex_webhook_secret", None),
            "bazarr": getattr(self.config, "bazarr_webhook_secret", None),
        }
        return secret_map.get(service)

    def _validate_shared_secret(
        self,
        request: web.Request,
        body: bytes,
        secret: str,
        service: str
    ) -> Tuple[bool, Optional[str], Optional[bytes]]:
        """Validate a service that has no native webhook auth.

        Such services (Bazarr) can only carry a secret in the webhook URL or a
        static header, so accept either. Configure the notification URL as
        ``/webhook/<service>?secret=<value>``.
        """
        auth_header = request.headers.get("X-Webhook-Secret") or request.headers.get("Authorization")
        if auth_header:
            if auth_header.startswith("Bearer "):
                auth_header = auth_header[7:]
            if secure_equals(auth_header, secret):
                return True, None, body
            logger.warning(f"{service.title()} webhook: Invalid secret header")
            return False, "Invalid secret", None

        query_secret = request.query.get("secret")
        if query_secret:
            if secure_equals(query_secret, secret):
                return True, None, body
            logger.warning(f"{service.title()} webhook: Invalid query secret")
            return False, "Invalid secret", None

        logger.warning(f"{service.title()} webhook: No authentication found")
        return False, "Missing authentication", None

    def _validate_arr_signature(
        self,
        request: web.Request,
        body: bytes,
        secret: str,
        service: str
    ) -> Tuple[bool, Optional[str], Optional[bytes]]:
        """
        Validate Sonarr/Radarr webhook signature.
        
        Sonarr/Radarr use X-Api-Key header for authentication.
        """
        # Check X-Api-Key header (standard Sonarr/Radarr auth)
        api_key = request.headers.get("X-Api-Key")
        
        if api_key:
            if secure_equals(api_key, secret):
                return True, None, body
            else:
                logger.warning(f"{service.title()} webhook: Invalid API key")
                return False, "Invalid API key", None

        # Check custom signature header (if configured)
        signature = request.headers.get("X-Webhook-Signature") or request.headers.get("X-Signature-256")
        
        if signature:
            expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            sig_value = signature.replace("sha256=", "")
            
            if secure_equals(sig_value, expected):
                return True, None, body
            else:
                logger.warning(f"{service.title()} webhook: Invalid signature")
                return False, "Invalid signature", None

        # No auth header found
        logger.warning(f"{service.title()} webhook: No authentication header found")
        return False, "Missing authentication", None

    def _validate_tautulli_signature(
        self,
        request: web.Request,
        body: bytes,
        secret: str
    ) -> Tuple[bool, Optional[str], Optional[bytes]]:
        """
        Validate Tautulli webhook signature.
        
        Tautulli can send a custom header with a shared secret.
        Configure in Tautulli: Settings > Notifications > Webhook > Headers
        """
        # Check for custom auth header
        auth_header = (
            request.headers.get("X-Tautulli-Signature") or
            request.headers.get("X-Webhook-Secret") or
            request.headers.get("Authorization")
        )
        
        if auth_header:
            # Handle Bearer token format
            if auth_header.startswith("Bearer "):
                auth_header = auth_header[7:]
            
            if secure_equals(auth_header, secret):
                return True, None, body
            else:
                logger.warning("Tautulli webhook: Invalid signature/secret")
                return False, "Invalid signature", None

        # Check query parameter as fallback
        query_secret = request.query.get("secret")
        if query_secret:
            if secure_equals(query_secret, secret):
                return True, None, body
            else:
                logger.warning("Tautulli webhook: Invalid query secret")
                return False, "Invalid secret", None

        logger.warning("Tautulli webhook: No authentication found")
        return False, "Missing authentication", None

    def _validate_overseerr_signature(
        self,
        request: web.Request,
        body: bytes,
        secret: str
    ) -> Tuple[bool, Optional[str], Optional[bytes]]:
        """
        Validate Overseerr webhook signature.
        
        Overseerr supports webhook authentication via Authorization header.
        """
        auth_header = request.headers.get("Authorization")
        
        if auth_header:
            # Handle Bearer token format
            if auth_header.startswith("Bearer "):
                token = auth_header[7:]
            else:
                token = auth_header
            
            if secure_equals(token, secret):
                return True, None, body
            else:
                logger.warning("Overseerr webhook: Invalid authorization")
                return False, "Invalid authorization", None

        logger.warning("Overseerr webhook: No authorization header")
        return False, "Missing authorization", None

    def _validate_plex_signature(
        self,
        request: web.Request,
        body: bytes,
        secret: str
    ) -> Tuple[bool, Optional[str], Optional[bytes]]:
        """
        Validate Plex webhook.
        
        Plex webhooks include the Plex token, which we can validate.
        """
        plex_token = request.query.get("token") or request.headers.get("X-Plex-Token")

        if plex_token:
            if secure_equals(plex_token, secret):
                return True, None, body
            else:
                logger.warning("Plex webhook: Invalid token")
                return False, "Invalid token", None

        # Fail closed. This branch is only reached when a secret IS configured,
        # and previously returned True - meaning an attacker bypassed validation
        # simply by omitting the token. If no secret is configured,
        # validate_request short-circuits before ever calling this.
        logger.warning("Plex webhook: No token provided but a secret is configured")
        return False, "Missing token", None


def create_validated_handler(original_handler, validator: WebhookValidator, service: str):
    """
    Decorator factory to wrap webhook handlers with signature validation.
    
    Usage:
        validated_handler = create_validated_handler(my_handler, validator, "sonarr")
    """
    async def validated_wrapper(request: web.Request) -> web.Response:
        is_valid, error, body = await validator.validate_request(request, service)
        
        if not is_valid:
            logger.warning(f"Rejected {service} webhook: {error}")
            return web.json_response(
                {"error": error, "service": service},
                status=401
            )
        
        # Store the pre-read body for the handler to use
        request["_validated_body"] = body
        
        return await original_handler(request)
    
    return validated_wrapper
