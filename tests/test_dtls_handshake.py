"""Tests for DTLS handshake retry behaviour."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hue_entertainment.dtls import _SERVER_HELLO_RESENDS, _DtlsConnection


def test_send_cookie_client_hello_numbering() -> None:
    """The cookie ClientHello is message 1 and keeps the record sequence running."""
    conn = MagicMock(spec=_DtlsConnection)
    conn._msg_seq = 7
    conn._send_seq = 3

    _DtlsConnection._send_cookie_client_hello(conn, b"cookie")

    assert conn._msg_seq == 1
    assert conn._send_seq == 3
    conn._build_client_hello.assert_called_once_with(cookie=b"cookie")


def test_await_server_hello_resends_after_timeout() -> None:
    """A single ServerHello timeout triggers one cookie ClientHello resend."""
    conn = MagicMock(spec=_DtlsConnection)
    conn._host = "bridge.local"
    conn._port = 2100
    conn._recv = MagicMock(side_effect=[TimeoutError(), b"server-flight"])
    cookie = b"\x01\x02"

    data = _DtlsConnection._await_server_hello(conn, cookie)

    assert data == b"server-flight"
    assert conn._send_cookie_client_hello.call_count == 2
    conn._send_cookie_client_hello.assert_any_call(cookie)


def test_await_server_hello_raises_after_exhausted_resends() -> None:
    """All ServerHello attempts timing out re-raises the last TimeoutError."""
    conn = MagicMock(spec=_DtlsConnection)
    conn._host = "bridge.local"
    conn._port = 2100
    conn._recv = MagicMock(side_effect=TimeoutError())

    with pytest.raises(TimeoutError):
        _DtlsConnection._await_server_hello(conn, b"cookie")

    assert conn._send_cookie_client_hello.call_count == _SERVER_HELLO_RESENDS + 1
