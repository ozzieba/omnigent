"""Closed provider-error metadata for native failure status edges."""

from __future__ import annotations

# Claude Code StopFailure.error values; textual details never enter this field.
NATIVE_FAILURE_CODES = frozenset(
    {
        "rate_limit",
        "overloaded",
        "authentication_failed",
        "oauth_org_not_allowed",
        "account_on_hold",
        "billing_error",
        "invalid_request",
        "model_not_found",
        "server_error",
        "max_output_tokens",
        "cloud_credential_error",
    }
)


def native_failure_code(value: object) -> str | None:
    """Return a recognized actionable code, preserving generic unknown fallback."""
    return value if isinstance(value, str) and value in NATIVE_FAILURE_CODES else None
