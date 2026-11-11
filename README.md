# hue-entertainment

Async Python client for the **Philips Hue Entertainment** streaming API.

It pairs with a Hue bridge, discovers entertainment areas, and streams colour frames to the
lights over **DTLS 1.2 PSK** using the `HueStream` protocol with minimal latency. It works
with both the Hue **V2** ("square") bridge and the Hue **Pro** bridge.

The DTLS 1.2 PSK handshake, record layer and HueStream encoder are pure Python on top of
[`cryptography`](https://cryptography.io/) — no `openssl` subprocess, no C bindings, no
other DTLS dependency. Blocking socket I/O is confined to a dedicated sender thread, so it
never blocks your asyncio event loop.

It powers the Hue Entertainment plugin (the Sendspin bridge) in
[Music Assistant](https://github.com/music-assistant/server) and the
[ambilight-hue-pro-bridge](https://github.com/marcelveldt/ambilight-hue-pro-bridge).

## What it provides

- **Bridge discovery** — find Hue bridges on the LAN via mDNS (`discover_bridges`).
- **Pairing** — create an application key and the DTLS client key (`HueEntertainmentAPI.pair`).
- **Areas** — list a bridge's entertainment configurations with their channels and positions.
- **Streaming** — start/stop an entertainment stream and push per-channel colours at up to ~50 Hz.
- **`EntertainmentSession`** — a high-level async facade that opens the stream on demand, runs
  the blocking DTLS work in an executor, enforces the bridge's single-active-stream constraint,
  and tears the stream down after inactivity.
- Works with the V2 and Pro bridges; dependencies are `aiohttp`, `cryptography` and `zeroconf`.

## What you can build with it

Anything that drives Hue lights from a fast colour source — screen / Ambilight sync, music
visualisers, games, rich notifications, or your own virtual bridge — without the Hue Sync
hardware.

## Planned extensions

- HueStream **v1** framing (currently HueStream v2). The **CIE xy** colour space is supported
  via colour modes — see below.

## Install

```bash
pip install hue-entertainment
```

Requires Python 3.11+. Runtime dependencies: `aiohttp`, `cryptography`, `zeroconf`.

## Usage

```python
import asyncio

from hue_entertainment import EntertainmentSession, HueEntertainmentAPI, LightColorCommand


async def main() -> None:
    host = "192.168.1.50"

    # One-time pairing (press the bridge link button first):
    api = HueEntertainmentAPI(host)
    creds = await api.pair()  # {"username": ..., "clientkey": ...}
    await api.close()

    # Stream to an entertainment area:
    session = EntertainmentSession(host, creds["username"], creds["clientkey"])
    areas = await session.get_entertainment_areas()
    area = areas[0]

    await session.start(area.id)  # activates the area + opens the DTLS stream
    try:
        # Drive frames at your own rate (~25-50 Hz). One command per channel.
        for _ in range(250):
            session.send(
                [LightColorCommand(channel_id=ch.channel_id, red=65535) for ch in area.channels]
            )
            await asyncio.sleep(1 / 50)
    finally:
        await session.aclose()


asyncio.run(main())
```

## Colour modes

`EntertainmentSession(..., color_mode=...)` (or `streamer.set_color_mode(...)`) chooses how your
RGB colours are encoded into the stream:

| Mode | What it does | When it makes sense | Trade-off |
| --- | --- | --- | --- |
| `ColorMode.RGB` *(default)* | Raw RGB; the bridge maps it to each bulb's full native gamut | A vivid, dynamic show — widest colour range | Same RGB can look slightly different across mixed Hue models |
| `ColorMode.XY` | Converts to the bulb's CIE xy gamut (colour-accurate, hardware-independent) | Consistent, matching colour across different Hue models | Narrower range: colours outside a bulb's gamut are clamped to its edge |
| `ColorMode.VIVID` | Maps RGB onto the bulb's own gamut (primaries on the gamut corners, white on D65) | Saturated colour from each bulb's full gamut, with whites kept neutral | Not colour-accurate; bulbs with different gamuts render the same RGB differently |

The xy modes use each light's colour gamut as reported by the bridge (exposed as
`LightChannel.gamut`); a light with an unknown gamut is treated as gamut C. Gamut B does not
contain D65 white, so `VIVID` falls back to `XY` for those lights.

Per Philips' own guidance, **RGB gives the widest colour range per bulb** while **xy gives colour
consistency** across lamp types — pick by whether range/dynamics or cross-model accuracy matters.

## Zigbee (Ziggurat) transport

`hue_entertainment.zigbee` streams the same frames straight onto a Zigbee network run by
[Ziggurat](https://github.com/zigpy/ziggurat) (the Zigbee stack behind Home Assistant's ZHA),
without a Hue bridge. It connects to `ziggurat-server` as an extra websocket client next to ZHA
and speaks the Hue Zigbee entertainment protocol: compact frames to a proxy light (one per update,
or several beyond 10 channels), which relays them to the other lights.

```python
from hue_entertainment import LightColorCommand, ZigbeeEntertainmentSession, ZigbeeLight

lights = [
    ZigbeeLight(channel_id=0, ieee="00:17:88:01:0c:bf:17:c8", nwk=0x1A2B, name="Left"),
    ZigbeeLight(channel_id=1, ieee="00:17:88:01:0c:bf:17:d4", nwk=0x3C4D, name="Right"),
]
session = ZigbeeEntertainmentSession("ws://homeassistant.local:9999", lights)
await session.start()  # captures each light's state, then starts streaming
session.send([LightColorCommand(channel_id=0, red=65535), LightColorCommand(channel_id=1, blue=65535)])
await session.stop()  # restores the captured state
```

- Hue lights within radio range of each other; the best-heard light is the proxy.
- Frames go out at up to 25 Hz (the Hue bridge's own rate), or `max_rate_hz`; the lights fade over
  the time between frames, and when the network is busy the newest frame wins. A frame holds 10
  channels, so an update to more channels goes out as several frames, back to back.
- Gradient lights (Signe, gradient lightstrips) can show several zones: one entry per zone with the
  light's `ieee` and `nwk` and `zone=0..n-1`, zone 0 at the first segment (the bottom of a Signe).
  The segments are spread evenly over the zones; a light without segments streams the colour of
  its zone 0. `read_segment_counts(url, nwks)` tells beforehand which lights are gradients:

  ```python
  lights = [
      ZigbeeLight(channel_id=0, ieee="00:17:88:01:0c:bf:17:c8", nwk=0x1A2B, name="Signe", zone=0),
      ZigbeeLight(channel_id=1, ieee="00:17:88:01:0c:bf:17:c8", nwk=0x1A2B, name="Signe", zone=1),
      ZigbeeLight(channel_id=2, ieee="00:17:88:01:0c:bf:17:c8", nwk=0x1A2B, name="Signe", zone=2),
  ]
  ```
- `stop()` gives each light back its on/off state, brightness and colour, and a gradient light
  the gradient it showed before streaming.
- A light follows one streamer at a time. While another client (another app, or a second session)
  streams to one of the lights, the first `start()` raises `LightsBusyError` without touching them;
  a light frees up a few seconds after that client stops, so the caller can simply try again later.
- The client can only send unicast frames, at background priority so ZHA's traffic always goes
  first; it never reconfigures the stack or the network.
- When the stream is lost (ZHA restarts the stack, the connection drops, frames stop arriving),
  `is_streaming` turns False and the caller restarts or stops, as with `EntertainmentSession`.

## API

- **`HueEntertainmentAPI(host, app_key=None)`** — low-level CLIP v2 wrapper:
  `pair()`, `get_entertainment_areas()`, `start_entertainment(area_id)`,
  `stop_entertainment(area_id)`, `get_bridge_id()`, `close()`.
- **`HueDtlsStreamer()`** — low-level DTLS/HueStream streamer:
  `connect(host, username, clientkey, area_id)` (blocking — run in an executor),
  `send_colors([...])`, `disconnect()`, `is_connected`.
- **`EntertainmentSession(host, app_key, client_key, *, idle_timeout=10.0, color_mode=ColorMode.RGB)`** — the
  recommended high-level facade: on-demand `start(area_id)`, non-blocking `send(...)`,
  `stop()` / `aclose()`, automatic idle teardown, and enforcement of the bridge's
  single-active-stream constraint. The blocking DTLS work runs in an executor for you.
- **`ZigbeeEntertainmentSession(url, lights, *, idle_timeout=10.0, max_rate_hz=25.0)`** — the Zigbee transport:
  `start()`, non-blocking `send(...)`, `stop()` / `aclose()`, `is_streaming`, and `max_rate_hz`
  (can be changed while streaming, without a restart); raises `ZigguratError` when
  ziggurat-server is unreachable or its Zigbee stack is down, and `LightsBusyError` when another
  client streams to one of the lights.
- **`hue_entertainment.zigbee.read_segment_counts(url, nwks)`** — the gradient segment count per
  light (1 for a light without segments; lights that do not answer are left out).
- **Models** — `EntertainmentArea`, `LightChannel`, `LightColorCommand`, `ZigbeeLight`.

`LightColorCommand` carries 16-bit RGB; in the default RGB mode the encoder emits the
duplicated-byte HueStream v2 format and lets the bridge gamut-map per light.

## Development

```bash
scripts/setup.sh
pre-commit run --all-files
pytest
```

## License

[Apache 2.0](LICENSE)
