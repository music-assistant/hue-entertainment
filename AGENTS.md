# AGENTS.md

`hue-entertainment` is a standalone async Python client for the Philips Hue Entertainment
streaming API: bridge discovery (mDNS), pairing, entertainment-area discovery, and
low-latency DTLS-PSK / HueStream streaming to a Hue V2 or Pro bridge.

## Behaviour

- NEVER automatically reply on GitHub (PRs or Discussions) without explicit consent from the developer.

## Development Commands

- `scripts/setup.sh` - Initial setup (venv, dependencies, pre-commit hooks). Re-run after pulling latest code.
- `pytest` - Run all tests
- `pre-commit run --all-files` - Run all pre-commit hooks
- Requires Python 3.11+.

Always run `pre-commit run --all-files` after a code change to ensure it adheres to standards.

## Code Style

### Comments

Only use comments to explain complex, multi-line blocks of code. Do not comment obvious
operations. Inline comments explain code that needs explaining; respect existing comments
from authors — they had a reason to write them, don't remove them unless needed.

### Docstring Format

Use Sphinx-style docstrings with `:param:` syntax. For simple functions, a single-line
docstring is fine. Don't explain inner workings in docstrings (use inline comments for
that); the docstring provides clarity to the caller, not a technical explanation. Use the
multi-line form where the summary starts on the next line:

```python
def my_function(param1: str, param2: int, param3: bool = False) -> str:
    """
    Brief one-line description of the function.

    :param param1: Description of what param1 is used for.
    :param param2: Description of what param2 is used for.
    :param param3: Description of what param3 is used for.
    """
```

Do **not** use Google-style (`Args:`) or bullet-style (`- param:`) docstrings.

### File structure

- Private methods at the bottom of the file/class, public at the top.
- Split into multiple files/modules where it improves clarity.
- Prefer dataclasses (and `mashumaro` for (de)serialization) for data models.
- No blocking I/O on the event loop — the blocking DTLS socket work runs on a dedicated thread.

## Layout

- `api.py` - CLIP v2 REST wrapper (pairing, area discovery, start/stop).
- `dtls.py` - pure-Python DTLS 1.2 PSK streamer + HueStream v2 encoder.
- `session.py` - `EntertainmentSession`: high-level async lifecycle facade.
- `models.py` / `constants.py` - data models and protocol constants.
