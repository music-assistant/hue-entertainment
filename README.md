# hue-entertainment

Async Python client for the **Philips Hue Entertainment** streaming API.

A small, dependency-light library that pairs with a Hue bridge, discovers entertainment
areas, and streams color frames to the lights over **DTLS 1.2 PSK** using the `HueStream`
protocol — with **as little latency as possible**. Works with both the Hue **V2**
("square") bridge and the new Hue **Pro** bridge.

The DTLS handshake and record layer are implemented in pure Python on top of
[`cryptography`](https://cryptography.io/) — no `openssl` subprocess, no C bindings, no
other DTLS dependency. Blocking socket I/O is confined to a dedicated sender thread so it
never blocks your asyncio event loop.

> Extracted from [Music Assistant](https://github.com/music-assistant/server)'s
> `hue_entertainment` provider so it can be shared across projects (e.g. Music Assistant
> and [ambilight-hue-pro-bridge](https://github.com/marcelveldt/ambilight-hue-pro-bridge)).

## Status

Early standalone release. The core (pairing, area discovery, DTLS streaming, HueStream v2
encoding) is the same code that is *"working and tested on Hue Bridge V2 and Hue Bridge
Pro"* in Music Assistant. The async `EntertainmentSession` lifecycle wrapper is new.

## Install

```bash
pip install hue-entertainment
```

Requires Python 3.11+. Runtime dependencies: `aiohttp`, `cryptography`.

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

## API

- **`HueEntertainmentAPI(host, app_key=None)`** — low-level CLIP v2 wrapper:
  `pair()`, `get_entertainment_areas()`, `start_entertainment(area_id)`,
  `stop_entertainment(area_id)`, `get_bridge_id()`, `close()`.
- **`HueDtlsStreamer()`** — low-level DTLS/HueStream streamer:
  `connect(host, username, clientkey, area_id)` (blocking — run in an executor),
  `send_colors([...])`, `disconnect()`, `is_connected`.
- **`EntertainmentSession(host, app_key, client_key, *, idle_timeout=10.0)`** — the
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
