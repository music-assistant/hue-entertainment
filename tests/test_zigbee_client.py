"""Tests for the minimal Ziggurat websocket client."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from hue_entertainment.zigbee.frames import CLUSTER_ON_OFF, read_attributes
from hue_entertainment.zigbee.ziggurat import (
    SEND_CANCELLED,
    SEND_SUCCESS,
    STATUS_NOT_CONFIGURED,
    DeviceJoined,
    ReceivedAps,
    ZigguratClient,
    ZigguratError,
)

from .fake_ziggurat import SEND_NWK_NO_ACK, FakeLight, wait_until

if TYPE_CHECKING:
    from .fake_ziggurat import FakeZiggurat


async def test_connect_waits_for_hello(ziggurat: FakeZiggurat) -> None:
    """connect() succeeds once a v2 Hello arrives."""
    client = ZigguratClient(ziggurat.url)
    await client.connect()
    assert client.connected
    await client.close()
    assert not client.connected


async def test_connect_rejects_other_protocol_version(ziggurat: FakeZiggurat) -> None:
    """A server speaking another protocol version is refused."""
    ziggurat.version = 3
    client = ZigguratClient(ziggurat.url)
    with pytest.raises(ZigguratError, match="protocol version 3"):
        await client.connect()
    assert not client.connected


async def test_connect_unreachable_server_raises(ziggurat: FakeZiggurat) -> None:
    """A server that is gone surfaces as ZigguratError."""
    url = ziggurat.url
    await ziggurat.close()
    client = ZigguratClient(url)
    with pytest.raises(ZigguratError, match="Cannot connect"):
        await client.connect()


async def test_send_unicast_resolves_confirms(ziggurat: FakeZiggurat) -> None:
    """An accepted unicast resolves its first-hop and APS ack verdicts."""
    ziggurat.add_light(FakeLight(0x1234, "00:17:88:01:00:00:00:01"))
    client = ZigguratClient(ziggurat.url)
    await client.connect()
    send = await client.send_unicast(0x1234, CLUSTER_ON_OFF, b"\x11\x01\x00", aps_ack=True)
    assert send.acked is not None
    assert await send.confirmed == SEND_SUCCESS
    assert await send.acked == SEND_SUCCESS
    req = ziggurat.requests[-1]
    assert (req.nwk, req.cluster, req.dst_ep, req.aps_ack) == (0x1234, CLUSTER_ON_OFF, 0x0B, True)
    assert req.asdu == b"\x11\x01\x00"
    await client.close()


async def test_send_unicast_rejection_carries_status(ziggurat: FakeZiggurat) -> None:
    """A rejected request raises with the server's status."""
    ziggurat.status_for = lambda _req: STATUS_NOT_CONFIGURED
    client = ZigguratClient(ziggurat.url)
    await client.connect()
    with pytest.raises(ZigguratError, match="NotConfigured") as err:
        await client.send_unicast(0x1234, CLUSTER_ON_OFF, b"\x11\x01\x00", aps_ack=False)
    assert err.value.status == STATUS_NOT_CONFIGURED
    assert err.value.stack_down
    await client.close()


async def test_failed_first_hop_also_resolves_aps_ack(ziggurat: FakeZiggurat) -> None:
    """No ApsAckConfirm follows a failed SendConfirm, so the ack verdict takes its status."""
    ziggurat.confirm_for = lambda _req: SEND_NWK_NO_ACK
    client = ZigguratClient(ziggurat.url)
    await client.connect()
    send = await client.send_unicast(0x1234, CLUSTER_ON_OFF, b"\x11\x01\x00", aps_ack=True)
    assert send.acked is not None
    assert await asyncio.wait_for(send.acked, 1) == SEND_NWK_NO_ACK
    await client.close()


async def test_send_unicast_refuses_unsafe_destinations(ziggurat: FakeZiggurat) -> None:
    """The ZDO endpoint and broadcast addresses can never be addressed."""
    client = ZigguratClient(ziggurat.url)
    with pytest.raises(ZigguratError, match="Not connected"):
        await client.send_unicast(0x1234, CLUSTER_ON_OFF, b"", aps_ack=False)
    await client.connect()
    with pytest.raises(ValueError, match="ZDO"):
        await client.send_unicast(0x1234, 0x0034, b"", aps_ack=False, dst_ep=0)
    with pytest.raises(ValueError, match="unicast"):
        await client.send_unicast(0xFFFD, CLUSTER_ON_OFF, b"", aps_ack=False)
    assert ziggurat.requests == []
    await client.close()


async def test_disconnect_cancels_pending_and_notifies(ziggurat: FakeZiggurat) -> None:
    """A dropped connection resolves pending verdicts as cancelled and fires on_disconnect."""
    ziggurat.confirm_for = lambda _req: None
    disconnects: list[bool] = []
    client = ZigguratClient(ziggurat.url, on_disconnect=lambda: disconnects.append(True))
    await client.connect()
    send = await client.send_unicast(0x1234, CLUSTER_ON_OFF, b"\x11\x01\x00", aps_ack=False)
    await ziggurat.drop()
    assert await asyncio.wait_for(send.confirmed, 1) == SEND_CANCELLED
    assert await wait_until(lambda: disconnects == [True])
    assert not client.connected
    await client.close()


async def test_close_does_not_fire_on_disconnect(ziggurat: FakeZiggurat) -> None:
    """An intentional close is not reported as a lost connection."""
    disconnects: list[bool] = []
    client = ZigguratClient(ziggurat.url, on_disconnect=lambda: disconnects.append(True))
    await client.connect()
    await client.close()
    await asyncio.sleep(0.05)
    assert disconnects == []


async def test_notifications_reach_callbacks(ziggurat: FakeZiggurat) -> None:
    """ReceivedAps and DeviceJoined (IEEE in display byte order) are decoded."""
    ziggurat.add_light(FakeLight(0x1234, "00:17:88:01:00:00:00:01", rssi=-55))
    received: list[ReceivedAps] = []
    joined: list[DeviceJoined] = []
    client = ZigguratClient(ziggurat.url, on_received=received.append, on_joined=joined.append)
    await client.connect()
    await client.send_unicast(0x1234, CLUSTER_ON_OFF, read_attributes(1, [0x0000]), aps_ack=True)
    await ziggurat.inject_joined(0x4321, "00:17:88:01:0c:bf:17:c8")
    assert await wait_until(lambda: bool(received) and bool(joined))
    assert (received[0].source, received[0].cluster, received[0].rssi) == (0x1234, 0x0006, -55)
    assert received[0].data[2] == 0x01  # Read Attributes Response
    assert joined[0] == DeviceJoined(0x4321, "00:17:88:01:0c:bf:17:c8", 0x0000)
    await client.close()
