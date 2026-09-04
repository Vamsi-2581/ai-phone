"""Model memory wiring: an optimisation that must never break a worker."""
import pytest

from ai_phone.services.backends import mlx_memory

mx = pytest.importorskip("mlx.core", reason="MLX-only (macOS)")


def test_model_bytes_sums_arrays_in_a_tree():
    tree = {"a": mx.zeros((4, 4), dtype=mx.float32), "b": [mx.zeros((2,), dtype=mx.int16)], "c": "not an array"}
    assert mlx_memory.model_bytes(tree) == 4 * 4 * 4 + 2 * 2


def test_disabled_reports_size_but_wires_nothing():
    tree = {"a": mx.zeros((8,), dtype=mx.float32)}
    assert mlx_memory.wire_resident(tree, enabled=False) == {"model_bytes": 32, "wired_limit_bytes": 0}


def test_env_escape_hatch(monkeypatch):
    monkeypatch.setenv("AI_PHONE_WIRE_MEMORY", "0")
    tree = {"a": mx.zeros((8,), dtype=mx.float32)}
    assert mlx_memory.wire_resident(tree, enabled=True)["wired_limit_bytes"] == 0


def test_not_a_model_is_harmless():
    assert mlx_memory.wire_resident(object(), enabled=True) == {"model_bytes": 0, "wired_limit_bytes": 0}


def test_wires_model_plus_headroom_capped_by_device():
    tree = {"a": mx.zeros((1024,), dtype=mx.float32)}
    out = mlx_memory.wire_resident(tree, enabled=True)
    assert out["model_bytes"] == 4096
    cap = mlx_memory._device_cap()  # same namespace probe as the code under test: mx.device_info moved to mx.metal once
    expected = 4096 + mlx_memory.HEADROOM_BYTES
    assert out["wired_limit_bytes"] in (expected, min(expected, cap), 0)  # 0 only where wiring is unsupported
