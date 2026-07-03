"""QUANT — read-only PoE2 currency decision-support. Stdlib only."""
from pathlib import Path

# Single source of truth: the root VERSION file the self-updater compares against.
# Deriving __version__ from it means the two can never drift apart (they did once,
# which silently pinned the updater to an old version while the code moved on).
try:
    __version__ = (Path(__file__).resolve().parent.parent / "VERSION").read_text(
        encoding="utf-8").strip() or "0.0.0"
except OSError:
    __version__ = "0.0.0"

MODEL_V = "m3"  # bumped whenever forecast math changes; stored on every prediction
