"""The docs name the model catalog's keys in three places. Nothing but a reader's attention keeps those lists in step
with `models.CATALOG`, so these tests do it instead: adding or renaming a model fails here until the docs follow."""
from __future__ import annotations

import re
from pathlib import Path

from ai_phone import models

ROOT = Path(__file__).resolve().parents[2]
KEY = r"[a-z][a-z0-9]*"

# one pattern per place a reader is told which models exist; group 1 is the list, however it is punctuated there
KEY_LISTS = {
    "config.example.yaml": re.compile(rf"#\s+({KEY}(?: \| {KEY})+) —"),       # "# t4 | t9 | … — or any"
    "README.md": re.compile(rf"one of ({KEY}(?:, {KEY})+) \(the catalog"),
    "architecture.md": re.compile(rf"made calls with it: (`{KEY}`(?:, `{KEY}`)+)\."),
}
MOVED = "if the wording moved, move this test's pattern with it"


def keys_named_in(name: str) -> list[str]:
    found = KEY_LISTS[name].findall((ROOT / name).read_text())
    assert len(found) == 1, f"{name}: expected one list of model keys, found {len(found)} ({MOVED})"
    return re.findall(KEY, found[0])


def test_every_doc_names_exactly_the_catalog_in_its_own_order():
    for name in KEY_LISTS:
        assert keys_named_in(name) == list(models.CATALOG), f"{name} is out of step with models.CATALOG"


def test_the_docs_agree_with_each_other():
    """Belt and braces: if the three patterns were ever all pointed at stale text, they would still disagree."""
    assert len({tuple(keys_named_in(name)) for name in KEY_LISTS}) == 1


def test_the_catalog_keys_look_like_what_the_patterns_can_find():
    """A key with a capital or an underscore would be half-matched above, so the keys must stay plain."""
    assert all(re.fullmatch(KEY, key) for key in models.CATALOG)
    assert len(models.CATALOG) >= 2


def test_what_auto_picks_is_written_the_way_the_code_decides_it():
    big, small = models.BIG_MEMORY_GB, models.BIG_MEMORY_GB - 1
    over, under = models.recommended_tier(big), models.recommended_tier(small)
    sentences = {"config.example.yaml": f"auto ({over} with {big} GB or more, else {under})",
                 "architecture.md": f"`auto` picks by total memory: {big} GB or more → {over}, else {under}"}
    for name, sentence in sentences.items():
        assert sentence in (ROOT / name).read_text(), f"{name}: expected {sentence!r} ({MOVED})"
