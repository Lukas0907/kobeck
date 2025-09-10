"""Shared logging utilities for sanitizing sensitive data."""

import re


def sanitize_sensitive_data(data):
    """Mask sensitive data in logs while preserving structure for debugging."""
    if isinstance(data, dict):
        sanitized = {}
        for key, value in data.items():
            key_lower = key.lower()
            # Special handling for Authorization header
            if key_lower == "authorization" and isinstance(value, str):
                if value.startswith("Bearer ") and len(value) > 12:
                    sanitized[key] = f"{value[:11]}..."
                else:
                    sanitized[key] = "***MASKED***"
            else:
                sanitized[key] = sanitize_sensitive_data(value)
        return sanitized
    elif isinstance(data, str):
        # Mask tokens in request body strings
        patterns = [
            (
                r'"access_token"\s*:\s*"([^"]{8,})"',
                lambda m: f'"access_token": "{m.group(1)[:4]}...{m.group(1)[-4:]}"',
            ),
            (
                r"access_token=([A-Za-z0-9+/=]{8,})([&\s]|$)",
                lambda m: f"access_token={m.group(1)[:4]}...{m.group(1)[-4:]}{m.group(2)}",
            ),
        ]
        for pattern, replacement in patterns:
            data = re.sub(pattern, replacement, data)
        return data
    elif isinstance(data, list):
        return [sanitize_sensitive_data(item) for item in data]
    else:
        return data
