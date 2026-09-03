"""Keep a loaded MLX model resident in memory.

macOS pages idle memory out aggressively: after a quiet spell on a busy host the model weights of an
idle worker are no longer resident and the first inference pays seconds of page-ins before it runs.
On a phone call that looks like "it does not hear me". MLX (macOS 15+) can wire buffers so the pager
leaves them alone; we wire exactly what the model needs plus a small working set, never more than the
GPU's recommended working set, and report the numbers so `doctor`/logs can show them.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

HEADROOM_BYTES = 512 * 1024 * 1024
_limit = 0  # the wired limit this process currently holds (MLX has no getter; set_wired_limit returns the previous one)


def _enabled(enabled: bool) -> bool:
    return enabled and os.environ.get("AI_PHONE_WIRE_MEMORY", "1") != "0"


def _set_limit(limit: int) -> int:
    global _limit
    import mlx.core as mx

    mx.set_wired_limit(limit)
    _limit = limit
    return limit


def begin_wiring(enabled: bool = True) -> int:
    """Call BEFORE loading a model. MLX wires buffers as they are allocated, up to the limit; a limit set after the
    load leaves the weights unwired (measured: 2.5 GB of a 2.4 GB model compressed within a minute of warm-up).
    Opens the limit to the GPU's recommended working set so every buffer the load allocates is kept resident;
    `wire_resident` reports the model's real size afterwards. Returns the limit, 0 when off or unsupported."""
    if not _enabled(enabled):
        return 0
    try:
        import mlx.core as mx

        cap = int(mx.device_info().get("max_recommended_working_set_size", 0))
        return _set_limit(cap) if cap else 0
    except Exception as e:
        log.warning("could not open the wired-memory limit (%s); replies after an idle spell may be slow", e)
        return 0


def model_bytes(model) -> int:
    """Bytes held by the model's arrays (an `nn.Module` or any pytree of `mx.array`s)."""
    import mlx.core as mx
    from mlx.utils import tree_reduce

    tree = model.parameters() if hasattr(model, "parameters") else model
    return int(tree_reduce(lambda acc, x: acc + x.nbytes if isinstance(x, mx.array) else acc, tree, 0))


def wire_resident(model, enabled: bool = True) -> dict:
    """Wire the model's memory. Returns {"model_bytes", "wired_limit_bytes"}; wired_limit_bytes is 0 when
    wiring is off or unavailable (Linux, older macOS, or AI_PHONE_WIRE_MEMORY=0)."""
    nbytes = 0
    try:
        nbytes = model_bytes(model)
    except Exception:  # not an MLX model; nothing to wire
        return {"model_bytes": 0, "wired_limit_bytes": 0}
    if nbytes == 0:
        return {"model_bytes": 0, "wired_limit_bytes": 0}
    if not _enabled(enabled):
        return {"model_bytes": nbytes, "wired_limit_bytes": 0}
    try:
        import mlx.core as mx

        cap = int(mx.device_info().get("max_recommended_working_set_size", 0))
        limit = nbytes + HEADROOM_BYTES
        if cap:
            limit = min(limit, cap)
        if _limit >= limit:  # begin_wiring already opened a wider limit; shrinking it would drop buffers from the set
            return {"model_bytes": nbytes, "wired_limit_bytes": _limit}
        _set_limit(limit)
        return {"model_bytes": nbytes, "wired_limit_bytes": limit}
    except Exception as e:  # wiring is an optimisation; the worker still runs without it
        log.warning("could not wire model memory (%s); replies after an idle spell may be slow", e)
        return {"model_bytes": nbytes, "wired_limit_bytes": 0}
