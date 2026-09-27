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


def redact_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively redact sensitive data from dictionary"""
    if not isinstance(data, dict):
        return data
    
    result = {}
    sensitive_keys = {"token", "key", "secret", "password", "auth", "api_key", "apikey"}
    
    for key, value in data.items():
        if any(s in key.lower() for s in sensitive_keys):
            result[key] = "REDACTED"
        elif isinstance(value, dict):
            result[key] = redact_dict(value)
        elif isinstance(value, str):
            result[key] = redact(value)
        else:
            result[key] = value
    
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
