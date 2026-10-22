"""CLI-side auth storage for ``omnigent login``.

Persists per-server auth state in ``~/.omnigent/auth_tokens.json``
keyed by server URL. Two record shapes live side by side:

- **Session JWTs** from the browser-based OIDC / accounts login flow
  (``{"token": ..., "user_id": ..., "expires_at": ...}``).
- **Databricks Apps pointer records**
  (``{"auth_type": "databricks", "workspace_host": ...}``) written by
  ``omnigent login <apps-url>``. These deliberately store NO token:
  Databricks OAuth access tokens expire after ~1 hour, so the record
  just names the workspace whose host-keyed Databricks CLI OAuth cache
  (``databricks auth login --host <ws>``) mints fresh bearers on
  demand.

See ``designs/OIDC_AUTH.md`` §CLI Login Flow.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import shlex
import stat
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from omnigent.server_url import is_workspace_hosted_url

if TYPE_CHECKING:
    import httpx

_logger = logging.getLogger(__name__)
_TOKEN_FILE_NAME = "auth_tokens.json"

# Treat a stored token with less than this much life left as needing
# renewal. Shared by the "is it still usable" read path and the refresh
# path's already-renewed check, so a caller that decides to refresh is
# never handed back the same near-expiry token it wanted to replace.
# Comfortably longer than a WebSocket handshake, far shorter than the
# server's 1-hour access-token TTL.
REFRESH_MIN_REMAINING_SECONDS = 90.0

# Machine-readable refresh health, one record per server, beside the token
# file. Watchdogs read it; it never holds token material.
_REFRESH_STATUS_FILE_NAME = "auth_refresh_status.json"
# Command run (detached, no token material) when refresh needs a human:
# a definitive refusal, an approaching grant expiry, or a long outage.
AUTH_ALERT_COMMAND_ENV = "OMNIGENT_AUTH_ALERT_COMMAND"
# Warn this many days before the refresh grant's absolute lifetime ends.
REFRESH_EXPIRY_WARN_DAYS_ENV = "OMNIGENT_REFRESH_EXPIRY_WARN_DAYS"
_DEFAULT_REFRESH_EXPIRY_WARN_DAYS = 7.0
# The same alert for the same server repeats at most this often.
_ALERT_REPEAT_SECONDS = 6 * 3600.0
# Transient failures (connection errors, 5xx, 429) are retried in-call with
# these pauses; consecutive transient failures past the threshold alert.
_REFRESH_ATTEMPTS = 3
_REFRESH_BACKOFF_SECONDS: tuple[float, ...] = (0.5, 1.5)
_UNREACHABLE_ALERT_THRESHOLD = 20
_TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


def _token_file_path() -> Path:
    """Return the path to the auth token storage file.

    Uses the shared Omnigent state directory, honoring
    ``OMNIGENT_DATA_DIR``.

    :returns: Path to ``<data-dir>/auth_tokens.json``.
    """
    from omnigent_ui_sdk.terminal._config import state_dir

    return Path(state_dir()) / _TOKEN_FILE_NAME


def _normalize_server_url(server_url: str) -> str:
    """Normalize a server URL for use as a dict key.

    Strips trailing slashes so ``http://localhost:6767`` and
    ``http://localhost:6767/`` resolve to the same entry.

    :param server_url: The server URL to normalize.
    :returns: Normalized URL string.
    """
    return server_url.rstrip("/")


def _write_tokens_file(path: Path, data: dict[str, Any]) -> None:
    """Atomically write the auth-tokens file, never exposing it readable.

    The previous sequence was ``path.write_text(...)`` followed by ``os.chmod``.
    ``write_text`` creates a missing file at the process umask (``0o644`` on a
    typical box), so on the very first login — exactly when a session JWT is
    first persisted — the token sat world-readable on disk until the ``chmod``
    landed. ``clear_token`` did not ``chmod`` at all, relying on the mode of a
    file that may not have been created here.

    Mirrors ``claude_native_bridge._atomic_write_user_json``: write a
    ``0o600`` temp beside the target, ``fsync``, then ``os.replace``. The temp
    is created by :mod:`tempfile` with owner-only permissions before any bytes
    are written, and the rename means a crash mid-write can no longer truncate
    the file and lose every stored token.

    Hardens the parent to ``0o700`` first (``mkdir`` alone won't
    re-permission an existing ``0o755`` dir), so every writer routes
    through here — including ``clear_token``.

    :param path: Destination file, i.e. ``~/.omnigent/auth_tokens.json``.
    :param data: The full token map to serialise.
    :returns: None.
    :raises OSError: If the temp cannot be written or replaced into place.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path.parent, stat.S_IRWXU)

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()


def _store_entry(server_url: str, entry: dict[str, str | float]) -> None:
    """Create or update a server's record in the auth-tokens file.

    Writes ``~/.omnigent/auth_tokens.json`` with user-only
    read/write permissions (``0o600``) — the file may hold session
    JWTs, which are sensitive.

    :param server_url: The server URL the record is keyed by, e.g.
        ``"http://localhost:6767"``.
    :param entry: The record to store, e.g.
        ``{"token": "...", "user_id": "...", "expires_at": 1750000000.0}``.
    """
    path = _token_file_path()

    data: dict[str, dict[str, str | float]] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}

    # Corrupt token files (non-dict JSON) read as empty — never crash.
    if not isinstance(data, dict):
        data = {}

    data[_normalize_server_url(server_url)] = entry

    _write_tokens_file(path, data)


def store_token(
    server_url: str,
    token: str,
    user_id: str,
    expires_at: float,
    refresh_token: str | None = None,
    refresh_expires_at: float | None = None,
) -> None:
    """Persist a session token for a server.

    :param server_url: The server URL, e.g.
        ``"http://localhost:6767"``.
    :param token: The session JWT string.
    :param user_id: The authenticated user's email, e.g.
        ``"alice@example.com"``.
    :param expires_at: Unix timestamp when the token expires.
    :param refresh_token: Login-issued refresh grant token, when the
        server handed one out. Lets :func:`refresh_stored_token` renew
        the access token past expiry without a human re-running
        ``omnigent login``.
    :param refresh_expires_at: Unix timestamp when the refresh grant's
        absolute lifetime ends, when the server reported it. Drives the
        expiry-ahead warning.
    """
    entry: dict[str, str | float] = {
        "token": token,
        "user_id": user_id,
        "expires_at": expires_at,
    }
    if refresh_token is not None:
        entry["refresh_token"] = refresh_token
        if refresh_expires_at is not None and math.isfinite(refresh_expires_at):
            entry["refresh_expires_at"] = refresh_expires_at
    _store_entry(server_url, entry)


def store_databricks_auth(
    server_url: str,
    workspace_host: str,
    user_id: str | None = None,
    org_id: str | None = None,
) -> None:
    """Persist a Databricks Apps auth pointer record for a server.

    Unlike :func:`store_token` this stores no bearer: Databricks OAuth
    access tokens expire after ~1 hour, so the record only names the
    workspace host whose ``databricks auth login --host <ws>`` OAuth
    cache the auth chain should mint fresh tokens from (see
    ``omnigent.inner.databricks_executor._resolve_databricks_auth``).

    :param server_url: The Databricks Apps server URL, e.g.
        ``"https://myapp-123.aws.databricksapps.com"``.
    :param workspace_host: The workspace that fronts the app, e.g.
        ``"https://example.databricks.com"``.
    :param user_id: The authenticated user's email when known, e.g.
        ``"alice@example.com"``. Display-only.
    :param org_id: The workspace org id when known (from the
        ``x-databricks-org-id`` response header), e.g.
        ``"2850744067564480"``. Used to build workspace web-UI links
        (the ``?o=`` query param).
    """
    entry: dict[str, str | float] = {
        "auth_type": "databricks",
        "workspace_host": workspace_host.rstrip("/"),
    }
    if user_id:
        entry["user_id"] = user_id
    if org_id:
        entry["org_id"] = org_id
    _store_entry(server_url, entry)


def _load_entry(server_url: str) -> dict[str, str | float] | None:
    """Load the raw stored record for a server, if any.

    :param server_url: The server URL, e.g.
        ``"http://localhost:6767"``.
    :returns: The stored record dict, or ``None`` when the file or
        entry is missing/unreadable.
    """
    path = _token_file_path()
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    # A token file holding valid JSON of the wrong shape (``[]``, ``null``,
    # a bare string) is corrupt, not fatal — read as "nothing stored".
    if not isinstance(data, dict):
        return None
    entry = data.get(_normalize_server_url(server_url))
    return entry if isinstance(entry, dict) else None


def load_token(server_url: str, *, min_remaining_seconds: float = 0.0) -> str | None:
    """Load a stored session token for a server.

    Returns ``None`` if no token is stored, the token has expired,
    or the file is unreadable. Databricks pointer records (which hold
    no token) also return ``None`` — resolve those via
    :func:`load_databricks_workspace_host` instead.

    :param server_url: The server URL, e.g.
        ``"http://localhost:6767"``.
    :param min_remaining_seconds: Require at least this much remaining
        lifetime. ``0`` (the default) accepts any not-yet-expired token —
        the historical behaviour. A caller that can renew passes
        :data:`REFRESH_MIN_REMAINING_SECONDS` so a token about to lapse
        mid-handshake is refreshed instead of used.
    :returns: The session JWT string, or ``None``.
    """
    entry = _load_entry(server_url)
    if entry is None:
        return None

    expires_at = entry.get("expires_at", 0)
    if isinstance(expires_at, (int, float)) and expires_at < time.time():
        _warn_expired_once(server_url, expires_at, has_refresh="refresh_token" in entry)
        return None
    # Near-expiry but still valid: decline quietly (no expiry warning — it
    # has not expired) so the caller can choose to renew.
    if (
        min_remaining_seconds > 0
        and isinstance(expires_at, (int, float))
        and expires_at - time.time() < min_remaining_seconds
    ):
        return None

    token = entry.get("token")
    return token if isinstance(token, str) else None


# Servers already warned about an expired stored token, so a poll/retry
# loop doesn't repeat the warning every few seconds.
_warned_expired_servers: set[str] = set()


def _warn_expired_once(server_url: str, expires_at: float, *, has_refresh: bool) -> None:
    """Warn (once per process per server) that a stored token expired.

    Expiry used to be a DEBUG line, which left the host dialing
    unauthenticated into a misleading 403 with no breadcrumb — an
    operator's first actionable signal must name the cause and the
    remedy.
    """
    normalized = _normalize_server_url(server_url)
    if normalized in _warned_expired_servers:
        return
    _warned_expired_servers.add(normalized)
    expired_on = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(expires_at))
    if has_refresh:
        _logger.warning(
            "Stored login session for %s expired on %s; refresh will be "
            "attempted on the next command if possible",
            normalized,
            expired_on,
        )
    else:
        _logger.warning(
            "Stored login session for %s expired on %s and holds no refresh "
            "material. Run `omnigent login %s` to re-authenticate.",
            normalized,
            expired_on,
            normalized,
        )


def stored_token_status(server_url: str) -> str:
    """Classify the stored auth state for a server.

    Lets callers distinguish "never logged in" from "logged in but the
    session lapsed" — the difference between proceeding unauthenticated
    (header-mode servers accept that) and surfacing an actionable
    re-login message.

    :param server_url: The server URL.
    :returns: ``"ok"`` (valid token stored), ``"expired"`` (entry exists
        but the token lapsed), or ``"absent"`` (no token entry at all;
        includes Databricks pointer records, which hold no token).
    """
    entry = _load_entry(server_url)
    if entry is None or not isinstance(entry.get("token"), str):
        return "absent"
    expires_at = entry.get("expires_at", 0)
    if isinstance(expires_at, (int, float)) and expires_at < time.time():
        return "expired"
    return "ok"


@contextlib.contextmanager
def _token_file_lock() -> Iterator[None]:
    """Exclusive advisory lock over token-file read-modify-write cycles.

    Serializes concurrent refreshes on one machine (host + CLI sharing
    ``auth_tokens.json``) so only one performs the network exchange and
    the other picks up its result. Best-effort on platforms without
    ``fcntl`` (Windows): the refresh still works, only the local
    serialization is lost.

    :raises OSError: If the lock file cannot be created or opened — the
        caller degrades to "cannot refresh" (a state directory we cannot
        write is one we could not persist the result to either).
    """
    lock_path = _token_file_path().with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:
        yield
        return
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def refresh_stored_token(server_url: str, *, timeout: float = 10.0) -> str | None:
    """Renew the stored access token from its login-issued refresh grant.

    POSTs ``grant_type=refresh_token`` to the server's ``/oauth/token``,
    persists the result, and returns the fresh access token. Safe to call
    opportunistically: returns ``None`` when there is nothing to refresh
    (no entry / no refresh material) or when the server refuses (grant
    revoked, past its absolute lifetime, or an older server without the
    endpoint).

    Runs under the token-file lock and re-checks state after acquiring
    it, so of two concurrent callers only one performs the network
    refresh and the other returns the already-renewed token.

    :param server_url: The server URL, e.g. ``"http://localhost:6767"``.
    :param timeout: HTTP timeout in seconds.
    :returns: A valid access token, or ``None``.
    """
    normalized = _normalize_server_url(server_url)
    # Cheap pre-check BEFORE touching the lock file: with no refresh
    # material there is nothing to do, and creating a lock file would
    # raise on a read-only state directory — masking the caller's other
    # credential sources (e.g. the Databricks SDK fallback).
    pre = _load_entry(server_url)
    if pre is None or not isinstance(pre.get("refresh_token"), str) or not pre["refresh_token"]:
        return None
    try:
        with _token_file_lock():
            return _refresh_locked(server_url, normalized, timeout)
    except OSError as exc:
        # Cannot lock/persist (read-only or full state dir) — a refresh we
        # could not store is worse than none, so decline and let the caller
        # fall through to its other credential sources.
        _logger.debug("Token refresh for %s skipped, state dir unusable: %s", normalized, exc)
        return None


def _refresh_locked(server_url: str, normalized: str, timeout: float) -> str | None:
    """Perform the refresh exchange; caller holds the token-file lock."""
    entry = _load_entry(server_url)
    if entry is None:
        return None
    # Another process may have refreshed while we waited on the lock. A
    # freshly minted token is far from expiry, so this cleanly separates
    # "someone already renewed" from "this is the same near-expiry token".
    expires_at = entry.get("expires_at", 0)
    token = entry.get("token")
    if (
        isinstance(token, str)
        and isinstance(expires_at, (int, float))
        and expires_at - time.time() > REFRESH_MIN_REMAINING_SECONDS
    ):
        return token
    refresh_token = entry.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        return None

    resp = _post_refresh_with_retry(normalized, refresh_token, timeout)
    if resp is None:
        # Transient: the stored pair is kept untouched for the next attempt.
        return None
    if resp.status_code != 200:
        _record_refusal(normalized, resp)
        return None
    try:
        payload = resp.json()
    except ValueError:
        _logger.warning("Token refresh against %s returned a malformed response", normalized)
        return None
    if not isinstance(payload, dict):
        _logger.warning("Token refresh against %s returned a malformed response", normalized)
        return None
    access_token = payload.get("access_token")
    new_refresh = payload.get("refresh_token")
    # Only overwrite the stored pair with genuinely usable material —
    # a null/non-string field must never clobber a working credential.
    if not isinstance(access_token, str) or not access_token:
        _logger.warning("Token refresh against %s returned no access token", normalized)
        return None
    if not isinstance(new_refresh, str) or not new_refresh:
        # A server that renews without returning refresh material keeps the
        # one we already hold (login grants deliberately do not rotate).
        new_refresh = refresh_token
    expires_in = _coerce_expires_in(payload.get("expires_in"))
    refresh_expires_at = _refresh_expires_at(payload, entry, rotated=new_refresh != refresh_token)

    user_id = entry.get("user_id")
    # Persist before handing the token out: a rotated refresh token that is
    # used but never stored would strand the grant.
    store_token(
        server_url,
        token=access_token,
        user_id=user_id if isinstance(user_id, str) else "",
        expires_at=time.time() + expires_in,
        refresh_token=new_refresh,
        refresh_expires_at=refresh_expires_at,
    )
    # A fresh token means any earlier expiry warning is stale; allow
    # a new one if this credential ever lapses again.
    _warned_expired_servers.discard(normalized)
    _logger.info("Refreshed login session for %s", normalized)
    _record_success(normalized, refresh_expires_at)
    return access_token


def _post_refresh_with_retry(
    normalized: str, refresh_token: str, timeout: float
) -> httpx.Response | None:
    """POST the refresh grant, retrying transient failures within *timeout*.

    Connection errors, timeouts, 5xx and 429 are transient (a server restart
    or DB failover): they are retried with short backoff and, if they
    persist, recorded without touching the stored credential. Any other
    response — success or a definitive refusal — is returned as-is.

    :returns: The final non-transient response, or ``None`` when every
        attempt failed transiently.
    """
    import httpx

    deadline = time.monotonic() + timeout
    last_problem = ""
    for attempt in range(_REFRESH_ATTEMPTS):
        remaining = max(1.0, deadline - time.monotonic())
        try:
            resp = httpx.post(
                f"{normalized}/oauth/token",
                data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                timeout=remaining,
            )
        except httpx.HTTPError as exc:
            last_problem = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code not in _TRANSIENT_HTTP_STATUSES:
                return resp
            last_problem = f"HTTP {resp.status_code}"
        if attempt + 1 >= _REFRESH_ATTEMPTS:
            break
        pause = _REFRESH_BACKOFF_SECONDS[min(attempt, len(_REFRESH_BACKOFF_SECONDS) - 1)]
        if time.monotonic() + pause >= deadline:
            break
        time.sleep(pause)
    _logger.warning(
        "Token refresh against %s failed transiently (%s); keeping the stored "
        "credential and retrying on the next connection attempt.",
        normalized,
        last_problem,
    )
    _record_transient_failure(normalized, last_problem)
    return None


def _refresh_expires_at(
    payload: dict[str, Any], entry: dict[str, str | float], *, rotated: bool
) -> float | None:
    """Return the refresh grant's absolute expiry from a refresh response.

    Falls back to the stored value when the server omits it and the refresh
    token did not change (same grant, same lifetime).
    """
    raw = payload.get("refresh_token_expires_in")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw):
        return time.time() + max(0.0, float(raw))
    previous = entry.get("refresh_expires_at")
    if not rotated and isinstance(previous, (int, float)) and math.isfinite(previous):
        return float(previous)
    return None


def _oauth_error_code(resp: httpx.Response) -> str:
    """Return the RFC 6749 ``error`` code from a refusal, or ``""``."""
    try:
        body = resp.json()
    except ValueError:
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, str) else ""


def _record_refusal(normalized: str, resp: httpx.Response) -> None:
    """Log, record and alert a definitive refresh refusal.

    The stored credential is left in place: a human re-login replaces it,
    and keeping it preserves evidence (e.g. the grant id) for diagnosis.
    """
    error = _oauth_error_code(resp)
    if error == "expired_token":
        cause = "the refresh grant reached its absolute lifetime"
    elif error == "invalid_grant":
        cause = "the refresh grant is unknown, revoked or was replayed"
    else:
        cause = "the server does not accept this refresh grant"
    message = (
        f"Token refresh against {normalized} refused (HTTP {resp.status_code}"
        f"{' ' + error if error else ''}): {cause}. Run `omnigent login {normalized}` "
        "to re-authenticate; hosts using this credential stay offline until then."
    )
    _logger.warning("%s", message)
    status = _update_refresh_status(
        normalized,
        state="refused",
        http_status=resp.status_code,
        error=error or None,
        message=message,
        failure=True,
    )
    _maybe_alert(normalized, "refresh_refused", message, status, log_level=logging.ERROR)


def _record_transient_failure(normalized: str, problem: str) -> None:
    status = _update_refresh_status(
        normalized,
        state="unreachable",
        http_status=None,
        error=problem[:200],
        message=f"Token refresh against {normalized} failing transiently: {problem[:200]}",
        failure=True,
    )
    failures = status.get("consecutive_failures", 0)
    if isinstance(failures, int) and failures >= _UNREACHABLE_ALERT_THRESHOLD:
        _maybe_alert(
            normalized,
            "refresh_unreachable",
            f"Token refresh against {normalized} has failed transiently {failures} times "
            f"in a row (last: {problem[:200]}); the stored credential is kept.",
            status,
            log_level=logging.WARNING,
        )


def _record_success(normalized: str, refresh_expires_at: float | None) -> None:
    warn_seconds = _refresh_expiry_warn_seconds()
    remaining = None if refresh_expires_at is None else refresh_expires_at - time.time()
    expiring = remaining is not None and remaining < warn_seconds
    message = "ok"
    if expiring and remaining is not None and refresh_expires_at is not None:
        expires_on = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(refresh_expires_at))
        message = (
            f"The refresh grant for {normalized} expires on {expires_on} "
            f"(in {remaining / 86400:.1f} days). Run `omnigent login {normalized}` "
            "before then or hosts using it go offline."
        )
    status = _update_refresh_status(
        normalized,
        state="expiring" if expiring else "ok",
        http_status=200,
        error=None,
        message=message,
        failure=False,
        refresh_expires_at=refresh_expires_at,
    )
    if expiring:
        _maybe_alert(normalized, "refresh_expiring", message, status, log_level=logging.WARNING)


def _refresh_expiry_warn_seconds() -> float:
    raw = os.environ.get(REFRESH_EXPIRY_WARN_DAYS_ENV, "").strip()
    try:
        days = float(raw) if raw else _DEFAULT_REFRESH_EXPIRY_WARN_DAYS
    except ValueError:
        days = _DEFAULT_REFRESH_EXPIRY_WARN_DAYS
    if not math.isfinite(days) or days < 0:
        days = _DEFAULT_REFRESH_EXPIRY_WARN_DAYS
    return days * 86400.0


def _refresh_status_path() -> Path:
    return _token_file_path().with_name(_REFRESH_STATUS_FILE_NAME)


def load_refresh_status(server_url: str) -> dict[str, Any] | None:
    """Return the recorded refresh health for a server, if any.

    :param server_url: The server URL.
    :returns: The status record (``state`` is ``ok``, ``expiring``,
        ``refused`` or ``unreachable``), or ``None``.
    """
    path = _refresh_status_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    record = data.get(_normalize_server_url(server_url))
    return record if isinstance(record, dict) else None


def _update_refresh_status(
    normalized: str,
    *,
    state: str,
    http_status: int | None,
    error: str | None,
    message: str,
    failure: bool,
    refresh_expires_at: float | None = None,
) -> dict[str, Any]:
    """Merge one refresh outcome into the status file; never raises."""
    path = _refresh_status_path()
    data: dict[str, Any] = {}
    try:
        loaded = json.loads(path.read_text())
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        data = {}
    previous = data.get(normalized)
    record: dict[str, Any] = dict(previous) if isinstance(previous, dict) else {}
    now = time.time()
    record.update(
        {
            "state": state,
            "updated_at": now,
            "http_status": http_status,
            "error": error,
            "message": message,
        }
    )
    if failure:
        record["last_failure_at"] = now
        failures = record.get("consecutive_failures", 0)
        record["consecutive_failures"] = (failures if isinstance(failures, int) else 0) + 1
    else:
        record["last_success_at"] = now
        record["consecutive_failures"] = 0
        record["refresh_expires_at"] = refresh_expires_at
        # A healthy refresh clears suppression for failure alerts.
        alerted = record.get("alerted")
        if isinstance(alerted, dict):
            for kind in ("refresh_refused", "refresh_unreachable"):
                alerted.pop(kind, None)
    data[normalized] = record
    try:
        _write_tokens_file(path, data)
    except OSError as exc:
        _logger.debug("Could not record refresh status for %s: %s", normalized, exc)
    return record


def _maybe_alert(
    normalized: str, kind: str, message: str, status: dict[str, Any], *, log_level: int
) -> None:
    """Emit an operator alert for *kind*, at most once per repeat interval."""
    alerted = status.get("alerted")
    if not isinstance(alerted, dict):
        alerted = {}
    last = alerted.get(kind)
    now = time.time()
    if isinstance(last, (int, float)) and now - last < _ALERT_REPEAT_SECONDS:
        return
    alerted[kind] = now
    status["alerted"] = alerted
    _logger.log(log_level, "omnigent auth alert kind=%s server=%s: %s", kind, normalized, message)
    _persist_alert_marker(normalized, alerted)
    _run_alert_hook(kind, normalized, message)


def _persist_alert_marker(normalized: str, alerted: dict[str, Any]) -> None:
    path = _refresh_status_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    if not isinstance(data, dict) or not isinstance(data.get(normalized), dict):
        return
    data[normalized]["alerted"] = alerted
    with contextlib.suppress(OSError):
        _write_tokens_file(path, data)


def _run_alert_hook(kind: str, normalized: str, message: str) -> None:
    """Start :data:`AUTH_ALERT_COMMAND_ENV` detached, if configured.

    The hook gets the alert in ``OMNIGENT_AUTH_ALERT_KIND``/``_SERVER``/
    ``_MESSAGE``; it never receives token material. Failures only log.
    """
    command = os.environ.get(AUTH_ALERT_COMMAND_ENV, "").strip()
    if not command:
        return
    try:
        argv = shlex.split(command)
        if not argv:
            return
        env = {
            **os.environ,
            "OMNIGENT_AUTH_ALERT_KIND": kind,
            "OMNIGENT_AUTH_ALERT_SERVER": normalized,
            "OMNIGENT_AUTH_ALERT_MESSAGE": message,
        }
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    except (OSError, ValueError) as exc:
        _logger.warning("Auth alert hook %s could not start: %s", AUTH_ALERT_COMMAND_ENV, exc)


def _coerce_expires_in(raw: object) -> float:
    """Return a sane access-token lifetime in seconds from *raw*.

    Falls back to one hour for anything missing, non-numeric, or
    non-finite — ``float("NaN")``/``float("Infinity")`` parse happily but
    would yield an expiry that never compares as expired, pinning a dead
    token forever.
    """
    default = 3600.0
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or value <= 0:
        return default
    return value


def load_databricks_workspace_host(server_url: str) -> str | None:
    """Load the workspace host from a Databricks Apps pointer record.

    :param server_url: The server URL, e.g.
        ``"https://myapp-123.aws.databricksapps.com"``.
    :returns: The workspace host, e.g.
        ``"https://example.databricks.com"``, or ``None`` when the
        stored record (if any) is not a Databricks pointer record.
    """
    entry = _load_entry(server_url)
    if entry is None or entry.get("auth_type") != "databricks":
        return None
    host = entry.get("workspace_host")
    return host if isinstance(host, str) and host else None


def load_databricks_org_id(server_url: str) -> str | None:
    """Load the workspace org id from a Databricks pointer record.

    :param server_url: The server URL, e.g.
        ``"https://example.databricks.com/api/2.0/omnigent"``.
    :returns: The org id, e.g. ``"2850744067564480"``, or ``None``
        when the stored record (if any) is not a Databricks pointer
        record or carries no org id.
    """
    entry = _load_entry(server_url)
    if entry is None or entry.get("auth_type") != "databricks":
        return None
    org_id = entry.get("org_id")
    return org_id if isinstance(org_id, str) and org_id else None


# Workspace-routing header. When a Databricks host fronts many workspaces
# under one hostname, the bare host is the account; the API proxy routes a
# workspace request by this header (equivalently to the ``?o=`` query param).
DATABRICKS_ORG_ID_HEADER = "X-Databricks-Org-Id"

# Replica-routing header for a host-sharded deployment. The sharding layer
# routes requests by this value — else the default fallback — so a host's
# control tunnel, its runners' tunnels, and their session traffic all land on
# one replica when they carry the same key (the host_id). Omitted for an
# unsharded / single-replica deployment, which needs no sticky routing.
OMNIGENT_SLICE_KEY_HEADER = "X-Databricks-Omnigent-Slice-Key"

# Opaque extra request headers for dev/test: a JSON object of header name→value
# in :data:`DATABRICKS_EXTRA_HEADERS_ENV_VAR`. Databricks deployments use it to
# carry request-routing selector headers so a request pins to a specific server
# instance/replica instead of the default one. Folded into
# :func:`databricks_request_headers` below so it travels with every
# client→server connection built through that one helper — a per-call-site
# bearer that skips this helper misses the selectors. Unset in prod.
DATABRICKS_EXTRA_HEADERS_ENV_VAR = "OMNIGENT_DATABRICKS_EXTRA_HEADERS"


def _databricks_extra_headers() -> dict[str, str]:
    """Return the opaque extra request headers when configured, else ``{}``.

    Reads :data:`DATABRICKS_EXTRA_HEADERS_ENV_VAR`, a JSON object of header
    name→value. Missing or malformed (unset, not JSON, or not an object) →
    ``{}``, so production and local runs are unaffected.

    :returns: A header dict parsed from the env var, or an empty dict.
    """
    raw = os.environ.get(DATABRICKS_EXTRA_HEADERS_ENV_VAR, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): str(value) for key, value in parsed.items()}


def databricks_request_headers(
    server_url: str,
    *,
    bearer_token: str | None = None,
    host_id: str | None = None,
) -> dict[str, str]:
    """Build the headers for a request to a Databricks-fronted server.

    The single source of truth for server-request headers. It always
    includes the :data:`DATABRICKS_ORG_ID_HEADER` workspace-routing header
    when ``omnigent login https://<host>/?o=<id>`` recorded a selector, and
    adds ``Authorization`` when a bearer is supplied. Folding both into one
    builder makes routing travel with auth: a caller that has a token gets
    routing for free, and a caller whose credential is set elsewhere (an
    httpx ``Auth`` that mints per request, or the managed-host token header)
    omits the token and still gets routing.

    Both values are omitted when absent, so single-workspace and
    local-unauthenticated callers get ``{}`` and are unaffected.

    Also folds in any opaque dev/test headers from
    :data:`DATABRICKS_EXTRA_HEADERS_ENV_VAR` (request-routing selectors set by
    some Databricks deployments) so every chokepoint that builds headers through
    this one helper carries them when set.

    :param server_url: The server URL, e.g.
        ``"https://example.databricks.com/api/2.0/omnigent"``.
    :param bearer_token: The workspace bearer token, or ``None`` when the
        credential is supplied by a separate mechanism (or there is none).
    :param host_id: The host a request is scoped to (its control tunnel, its
        runners, and their session traffic all name it so they co-locate on one
        replica). Pass it unconditionally: it is emitted (as the
        :data:`OMNIGENT_SLICE_KEY_HEADER` routing header) only when *server_url*
        is a host-sharded mount, since that is the only deployment with the
        sharding layer that reads it. ``None`` defaults to the runner's own
        host_id inside a runner process (via ``OMNIGENT_RUNNER_SLICE_KEY``) and
        otherwise leaves routing to the default.
    :returns: A header dict carrying ``Authorization``, ``X-Databricks-Org-Id``,
        ``X-Databricks-Omnigent-Slice-Key``, and/or the configured extra headers
        as available, possibly empty.
    """
    headers: dict[str, str] = {}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    org_id = load_databricks_org_id(server_url)
    if org_id:
        headers[DATABRICKS_ORG_ID_HEADER] = org_id
    # Resolve the slice-key host_id when the caller names none, so every
    # request still carries a key (routed by the sharding layer rather than
    # depending on its default). Two ordered fallbacks, both host_ids:
    #   1. In a runner process, the runner's own host_id (exported at launch as
    #      OMNIGENT_RUNNER_SLICE_KEY) — keys the runner's server traffic
    #      (transcript posts, uploads, policy checks) onto its host's replica,
    #      co-located with its tunnel.
    #   2. Otherwise, on the CLI, this machine's OWN host_id if it already has a
    #      host identity — a host-less CLI request (session list, /me-adjacent
    #      reads, export) then keys to the replica holding this CLI's own hosts'
    #      tunnels. Read-only (never mints an identity), so a non-host machine
    #      stays unkeyed (→ default). Gated on the host-sharded mount below so
    #      the file read only happens for requests that could use it.
    # Only a host-sharded deployment runs the sharding layer that reads the
    # header; an unsharded server is single-replica and would just log a header
    # it ignores — so gate emission (and the CLI identity lookup) on the mount.
    # Callers never reason about the deployment; a new RPC routed through this
    # builder is keyed automatically.
    on_workspace_mount = is_workspace_hosted_url(server_url)
    if host_id is None:
        from omnigent.runner.identity import RUNNER_SLICE_KEY_ENV_VAR

        host_id = os.environ.get(RUNNER_SLICE_KEY_ENV_VAR)
    if host_id is None and on_workspace_mount:
        from omnigent.host.identity import load_host_identity_if_present

        identity = load_host_identity_if_present()
        if identity is not None:
            host_id = identity.host_id
    # Kill switch: slice-key emission is ON by default; export
    # ``OMNIGENT_HOST_SLICE_KEY_ENABLED=0`` to turn it off and fall back to the
    # server's default (workspace-id) routing with no redeploy — a per-process
    # escape hatch for a bad rollout, since this emits from sidecar-less
    # processes (laptop CLI, managed sandbox host, spawned runner) that can't
    # evaluate a server-side flag. Only the exact value "0" disables it; unset,
    # "1", or anything else leaves emission on.
    slice_key_enabled = os.environ.get("OMNIGENT_HOST_SLICE_KEY_ENABLED", "1") != "0"
    if host_id and on_workspace_mount and slice_key_enabled:
        headers[OMNIGENT_SLICE_KEY_HEADER] = host_id
    # Opaque dev/test extra headers (request-routing selectors); no-op in prod
    # (env unset).
    headers.update(_databricks_extra_headers())
    return headers


# Sentinel for the ``timeout`` argument of :func:`open_server_client`. ``None``
# is a meaningful httpx value ("disable timeout"), so it can't stand in for
# "unset". When the caller passes nothing we omit ``timeout`` entirely and let
# httpx apply its own default, rather than silently changing it.
_TIMEOUT_UNSET: Any = object()


def open_server_client(
    server_url: str,
    *,
    auth: httpx.Auth | None = None,
    bearer_token: str | None = None,
    headers: dict[str, str] | None = None,
    timeout: Any = _TIMEOUT_UNSET,
    follow_redirects: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
    host_id: str | None = None,
) -> httpx.AsyncClient:
    """Open an :class:`httpx.AsyncClient` to an Omnigent server, keyed for routing.

    The one way to open a client to the server. It folds
    :func:`databricks_request_headers` in for you, so a request to a host-sharded
    mount automatically carries the org-id and slice-key routing headers (and any
    dev/test selectors) — and a request to an unsharded server carries none.
    Callers never reason about the deployment; a new server RPC opened through
    this factory is routed correctly by construction, which is why it exists
    rather than each site building headers by hand.

    :param server_url: The server base URL, e.g.
        ``"https://example.databricks.com/api/2.0/omnigent"``. Both the client's
        ``base_url`` and the input to the routing-header builder.
    :param auth: An httpx ``Auth`` when the credential is minted per request
        (e.g. :class:`_RunnerDatabricksAuth`, which re-injects a fresh bearer and
        the routing headers on the OAuth-redirect retry). Mutually exclusive with
        *bearer_token* in practice: pass one or the other, not both.
    :param bearer_token: A static workspace bearer, folded in as
        ``Authorization``. Leave ``None`` when *auth* supplies the credential or
        there is none.
    :param headers: Extra request headers (e.g. the runner ``Origin`` sentinel).
        Merged *under* the routing headers, so routing always wins over a
        caller-supplied collision.
    :param timeout: An httpx timeout (``httpx.Timeout``, ``float``, or ``None``
        to disable). Omitted by default so httpx applies its own default rather
        than this factory silently overriding it.
    :param follow_redirects: Passed through to httpx. Defaults to ``False``
        (httpx's own default); callers relying on seeing a 3xx — e.g. an auth
        flow that re-mints on the Databricks Apps OAuth login redirect — keep it
        ``False``.
    :param transport: An httpx ``AsyncBaseTransport`` to substitute for the
        default network transport. Its one production-adjacent use is injecting a
        test transport (e.g. ``httpx.MockTransport``); ``None`` uses the default.
    :param host_id: The host a request is scoped to, forwarded to
        :func:`databricks_request_headers` (which emits it as the slice-key only
        on a host-sharded mount and otherwise falls back to the runner's own
        host_id). Pass it unconditionally when known.
    :returns: A configured :class:`httpx.AsyncClient`.
    """
    import httpx
    from omnigent_client._http import is_loopback_url

    pinned = {
        **(headers or {}),
        **databricks_request_headers(server_url, bearer_token=bearer_token, host_id=host_id),
    }
    kwargs: dict[str, Any] = {}
    if timeout is not _TIMEOUT_UNSET:
        kwargs["timeout"] = timeout
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.AsyncClient(
        base_url=server_url,
        headers=pinned,
        auth=auth,
        follow_redirects=follow_redirects,
        # A proxy cannot reach a loopback server, so local targets bypass it.
        trust_env=not is_loopback_url(server_url),
        **kwargs,
    )


def clear_token(server_url: str) -> None:
    """Remove a stored token for a server.

    No-op if no token is stored or the file doesn't exist.

    :param server_url: The server URL, e.g.
        ``"http://localhost:6767"``.
    """
    path = _token_file_path()
    if not path.exists():
        return

    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return

    key = _normalize_server_url(server_url)
    if key in data:
        del data[key]
        _write_tokens_file(path, data)
