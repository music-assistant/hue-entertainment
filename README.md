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
| `ColorMode.RGB` *(default)* | Raw RGB; the bridge maps it to each bulb's full native gamut | A vivid, dynamic show — widest colour range, most organic fades | Same RGB can look slightly different across mixed Hue models |
| `ColorMode.XY` | Converts to the bulb's CIE xy gamut (colour-accurate, hardware-independent) | Consistent, matching colour across different Hue models | Narrower range; steadier (less organic) fades |
| `ColorMode.VIVID` | Like `XY` but stretches saturated colours to the gamut edge | Cross-model consistency without losing punch | Hue is approximate (pushed to the edge), not colour-accurate |

Per Philips' own guidance, **RGB gives the widest colour range per bulb** while **xy gives colour
consistency** across lamp types — pick by whether range/dynamics or cross-model accuracy matters.

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
- **Models** — `EntertainmentArea`, `LightChannel`, `LightColorCommand`.

`LightColorCommand` carries 16-bit RGB; the encoder emits the duplicated-byte HueStream v2
format and lets the bridge gamut-map per light.

## Development

```bash
scripts/setup.sh
pre-commit run --all-files
pytest
```

## License

[Apache 2.0](LICENSE)
