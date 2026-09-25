"""Every model the wizard offers must really load and answer on this Mac. This is the one test that downloads:
an entry whose weights are not cached is fetched here, so offline mode is lifted — for this file only, and put
back afterwards, so the other integration tests keep their "nothing is downloaded" guarantee. An entry that
cannot load or cannot generate does not belong in the catalog.

    timeout 3600 uv run pytest tests/integration/test_llm_catalog.py -q -m integration -p no:cacheprovider
"""
import time

import pytest

from ai_phone import models

pytestmark = pytest.mark.integration
SYS = "You are ai-phone, a phone assistant. Answer in one short sentence."
REPLY_BUDGET_SEC = 30


@pytest.fixture
def may_download(monkeypatch):
    """huggingface_hub reads HF_HUB_OFFLINE once, into a module constant, so the env var alone would not lift it
    (another integration module may already have set it). monkeypatch puts both back after the test."""
    from huggingface_hub import constants

    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    monkeypatch.setattr(constants, "HF_HUB_OFFLINE", False)


@pytest.mark.parametrize("entry", list(models.CATALOG.values()), ids=list(models.CATALOG))
def test_every_catalog_model_loads_and_answers(entry, may_download, record_property):
    from huggingface_hub import snapshot_download

    from ai_phone.services.backends.mlx_lm_backend import make_llm

    if models.local_path(entry.repo) is None:
        snapshot_download(entry.repo)  # the one download this suite is allowed to make

    llm = make_llm(entry.repo)
    warm = llm.warmup(SYS)
    assert warm["warm_ms"] > 0

    t = time.monotonic()
    text = "".join(llm.stream(1, [{"role": "system", "content": SYS},
                                  {"role": "user", "content": "Say hello in five words."}], 40))
    took = time.monotonic() - t
    assert text.strip(), f"{entry.key} ({entry.repo}) generated nothing"
    assert "<think>" not in text, f"{entry.key} leaked a thinking block: {text[:120]!r}"
    assert took < REPLY_BUDGET_SEC, f"{entry.key} took {took:.1f}s to answer"
    record_property("warm_ms", round(warm["warm_ms"]))
    record_property("first_token_ms", round(llm.last_timings["first_token_ms"]))
    print(f"\n{entry.key:10s} {entry.repo:46s} warm {warm['warm_ms'] / 1000:5.1f}s  "
          f"first token {llm.last_timings['first_token_ms']:6.0f} ms  reply {text.strip()[:60]!r}")
