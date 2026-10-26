"""Canonical foreground startup owns its listener before publishing a pidfile."""

from __future__ import annotations

import errno
import socket
from pathlib import Path

import pytest

from omnigent.host import local_server


def test_reserved_port_stays_exclusive_until_server_takes_it() -> None:
    """A competing startup cannot win the gap before uvicorn takes the socket."""
    with local_server.bind_local_server_socket("127.0.0.1", 0) as owned:
        port = owned.getsockname()[1]
        with socket.socket() as contender:
            contender.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with pytest.raises(OSError) as error:
                contender.bind(("127.0.0.1", port))
            assert error.value.errno == errno.EADDRINUSE
        with local_server.bind_local_server_socket("127.0.0.1", port) as second:
            assert second.getsockname()[1] != port
            assert owned.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 1
            assert second.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 1
    with socket.socket() as released:
        released.bind(("127.0.0.1", port))


def test_bind_error_other_than_contention_is_not_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid bind addresses refuse rather than silently moving elsewhere."""
    with pytest.raises(OSError) as error:
        local_server.bind_local_server_socket("192.0.2.1", 0)
    assert error.value.errno == errno.EADDRNOTAVAIL


def test_canonical_cli_hands_reserved_listener_to_uvicorn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The PID is advertised only while its listener is held through startup."""
    import uvicorn.server
    from click.testing import CliRunner

    from omnigent.cli import cli

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("OMNIGENT_AUTH_ENABLED", "0")
    monkeypatch.setattr(local_server, "local_server_url_if_healthy", lambda: None)
    real_bind = local_server.bind_local_server_socket
    bound: list[socket.socket] = []

    def bind(host: str, port: int) -> socket.socket:
        sock = real_bind(host, 0)
        bound.append(sock)
        return sock

    def register(port: int) -> None:
        assert len(bound) == 1
        assert bound[0].getsockname()[1] == port
        assert bound[0].getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 1

    def run(self: uvicorn.Server, sockets: list[socket.socket] | None = None) -> None:
        assert sockets == bound and len(bound) == 1
        assert self.config.port == bound[0].getsockname()[1]
        with socket.socket() as competing:
            competing.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with pytest.raises(OSError):
                competing.bind(bound[0].getsockname())

    monkeypatch.setattr(local_server, "bind_local_server_socket", bind)
    monkeypatch.setattr(local_server, "register_local_server", register)
    monkeypatch.setattr(local_server, "clear_local_server_record", lambda: None)
    monkeypatch.setattr(uvicorn.server.Server, "run", run)
    result = CliRunner().invoke(cli, ["server", "--no-open"])
    assert result.exit_code == 0, result.output
    assert len(bound) == 1 and bound[0].fileno() == -1
