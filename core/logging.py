# path: core/logging.py
"""Structured logging configuration"""
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from core.security import redact_dict


class JSONFormatter(logging.Formatter):
    """JSON log formatter with secret redaction"""
    
    def format(self, record):
        log_obj = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno
        }
        
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        
        # Redact sensitive data
        log_obj = redact_dict(log_obj)
        
        return json.dumps(log_obj)


def setup_logging(log_level: str = "INFO", enable_http_debug: bool = False):
    """Configure logging for the application

    Args:
        log_level: Logging level (INFO, DEBUG, WARNING, etc.)
        enable_http_debug: If True, log Discord HTTP requests and rate limits
    """
    # Create logs directory
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Console handler (JSON to stdout)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(JSONFormatter())
    root_logger.addHandler(console_handler)

    # File handler
    file_handler = logging.FileHandler(log_dir / "plexbie.log")
    file_handler.setFormatter(JSONFormatter())
    root_logger.addHandler(file_handler)

    # Reduce noise from libraries
    if enable_http_debug:
        # Enable HTTP request logging to monitor rate limits
        logging.getLogger("discord.http").setLevel(logging.DEBUG)
        logging.getLogger("discord.gateway").setLevel(logging.INFO)
    else:
        logging.getLogger("discord").setLevel(logging.WARNING)

    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("plexapi").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance"""
    return logging.getLogger(name)
