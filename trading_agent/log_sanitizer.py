"""Log sanitization utilities to prevent credential leakage."""

import logging
import re


# Patterns that match potential API keys, secrets, and credentials
_SECRET_PATTERNS = [
    # Long hex strings (32+ chars) — typical API keys
    re.compile(r'[0-9a-fA-F]{32,}'),
    # Base64 blocks (40+ chars)
    re.compile(r'[A-Za-z0-9+/]{40,}={0,2}'),
    # Explicit key=value patterns
    re.compile(r'(?i)(api[_-]?key|api[_-]?secret|secret[_-]?key|password|token|credential)\s*[=:]\s*\S+'),
]

REDACTED = "***REDACTED***"


def sanitize_string(text: str) -> str:
    """Strip or mask any string that matches API key / secret patterns."""
    if not isinstance(text, str):
        return text
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(REDACTED, result)
    return result


class SanitizingFormatter(logging.Formatter):
    """Logging formatter that redacts secrets from log messages."""

    def format(self, record: logging.LogRecord) -> str:
        original = super().format(record)
        return sanitize_string(original)


def setup_sanitized_logging(
    level: int = logging.INFO,
    fmt: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
) -> None:
    """Configure root logger with sanitizing formatter."""
    handler = logging.StreamHandler()
    handler.setFormatter(SanitizingFormatter(fmt))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
