# path: core/security.py
"""Security utilities for token handling and redaction"""
import re
from typing import Any, Dict


# Patterns for sensitive data
SENSITIVE_PATTERNS = [
    (re.compile(r"(token|key|secret|password|auth)[\"\']?\s*[:=]\s*[\"\']?([^\s\"\']+)", re.IGNORECASE), r"\1=REDACTED"),
    (re.compile(r"[a-zA-Z0-9+/]{32,}={0,2}"), lambda m: m.group()[:3] + "..." + m.group()[-3:]),  # Base64 tokens
    (re.compile(r"X-Plex-Token=([^&\s]+)"), r"X-Plex-Token=REDACTED"),
]


def redact(text: str) -> str:
    """Redact sensitive information from text"""
    if not text:
        return text
    
    result = text
    for pattern, replacement in SENSITIVE_PATTERNS:
        result = pattern.sub(replacement, result)
    
    return result


SENSITIVE_KEYS = {"token", "key", "secret", "password", "auth", "api_key", "apikey"}


def _redact_value(value: Any) -> Any:
    """Redact a value of any shape, recursing through containers.

    Lists were previously returned untouched, so a payload like
    ``{"users": [{"token": "abc"}]}`` - the shape every *arr and Overseerr webhook
    uses - logged its secrets in full.
    """
    if isinstance(value, dict):
        return redact_dict(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    if isinstance(value, str):
        return redact(value)
    return value


def redact_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively redact sensitive data from a dictionary.

    Keys whose name looks sensitive are replaced wholesale; everything else is
    walked, including lists and tuples of nested dictionaries.
    """
    if not isinstance(data, dict):
        return data

    result = {}
    for key, value in data.items():
        if any(s in str(key).lower() for s in SENSITIVE_KEYS):
            result[key] = "REDACTED"
        else:
            result[key] = _redact_value(value)

    return result


def validate_token(token: str, token_type: str = "discord") -> bool:
    """Basic token validation"""
    if not token:
        return False
    
    if token_type == "discord":
        # Discord tokens are typically 59+ characters
        return len(token) >= 59
    elif token_type == "plex":
        # Plex tokens are typically 20 characters
        return len(token) == 20
    
    # Generic validation
    return len(token) >= 16
