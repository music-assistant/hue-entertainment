"""Tests for DTLS handshake retry behaviour."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hue_entertainment.dtls import (
    _CT_HANDSHAKE,
    _HT_HELLO_VERIFY_REQUEST,
    _HT_SERVER_HELLO,
    _SERVER_HELLO_RESENDS,
    _DtlsConnection,
)


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


def test_handshake_without_hello_verify_request() -> None:
    """A ServerHello reply to the first ClientHello skips the cookie ClientHello."""
    conn = MagicMock(spec=_DtlsConnection)
    conn._host = "bridge.local"
    conn._port = 2100
    conn._sock = MagicMock()
    conn._recv = MagicMock(return_value=b"server-flight")
    conn._is_hello_verify_request = MagicMock(return_value=False)

    _DtlsConnection.handshake(conn)

    conn._await_server_hello.assert_not_called()
    conn._parse_server_flight.assert_called_once_with(b"server-flight")


def test_handshake_with_hello_verify_request() -> None:
    """A HelloVerifyRequest reply sends the cookie ClientHello and awaits ServerHello."""
    conn = MagicMock(spec=_DtlsConnection)
    conn._host = "bridge.local"
    conn._port = 2100
    conn._sock = MagicMock()
    conn._recv = MagicMock(return_value=b"hello-verify-request")
    conn._is_hello_verify_request = MagicMock(return_value=True)
    conn._parse_hello_verify_request = MagicMock(return_value=b"cookie")
    conn._await_server_hello = MagicMock(return_value=b"server-flight")

    _DtlsConnection.handshake(conn)

    conn._await_server_hello.assert_called_once_with(b"cookie")
    conn._parse_server_flight.assert_called_once_with(b"server-flight")


def test_is_hello_verify_request() -> None:
    """Only a handshake record carrying a HelloVerifyRequest is detected."""
    conn = MagicMock(spec=_DtlsConnection)
    hello_verify_request = bytes([_CT_HANDSHAKE]) + bytes(12) + bytes([_HT_HELLO_VERIFY_REQUEST])
    server_hello = bytes([_CT_HANDSHAKE]) + bytes(12) + bytes([_HT_SERVER_HELLO])

    assert _DtlsConnection._is_hello_verify_request(conn, hello_verify_request)
    assert not _DtlsConnection._is_hello_verify_request(conn, server_hello)
