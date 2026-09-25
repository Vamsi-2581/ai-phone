"""Every model the wizard offers must really load and answer on this Mac. An entry that cannot load or cannot
generate does not belong in the catalog.

This test never downloads on its own: an entry whose weights are not in the Hugging Face cache is skipped, named,
so a run says which entries are still unverified rather than quietly fetching gigabytes. Set
`AI_PHONE_TEST_DOWNLOAD=1` to let it fetch the missing ones — that is the only thing in the suite allowed to
download, and it lifts offline mode for these tests only and puts it back afterwards.

    HF_HUB_OFFLINE=1 timeout 3600 uv run pytest tests/integration/test_llm_catalog.py -q -m integration -p no:cacheprovider
    AI_PHONE_TEST_DOWNLOAD=1 timeout 3600 uv run pytest tests/integration/test_llm_catalog.py -q -m integration -p no:cacheprovider
"""
import os
import time

import pytest

from ai_phone import models

pytestmark = pytest.mark.integration
SYS = "You are ai-phone, a phone assistant. Answer in one short sentence."
REPLY_BUDGET_SEC = 30
MAY_DOWNLOAD = os.environ.get("AI_PHONE_TEST_DOWNLOAD") == "1"


@pytest.fixture
def downloading_allowed(monkeypatch):
    """Lift offline mode, but only when the run asked to download. huggingface_hub reads HF_HUB_OFFLINE once, into
    a module constant, so the env var alone would not lift it (another integration module may already have set it);
    monkeypatch puts both back after the test."""
    if not MAY_DOWNLOAD:
        return
    from huggingface_hub import constants

    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    monkeypatch.setattr(constants, "HF_HUB_OFFLINE", False)


@pytest.mark.parametrize("entry", list(models.CATALOG.values()), ids=list(models.CATALOG))
def test_every_catalog_model_loads_and_answers(entry, downloading_allowed, record_property):
    from ai_phone.services.backends.mlx_lm_backend import make_llm

    if models.local_path(entry.repo) is None:
        if not MAY_DOWNLOAD:
            pytest.skip(f"not downloaded: {entry.repo} (AI_PHONE_TEST_DOWNLOAD=1 fetches it)")
        from huggingface_hub import snapshot_download

        snapshot_download(entry.repo)

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
