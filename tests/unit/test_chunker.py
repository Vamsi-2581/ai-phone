from ai_phone.pipeline.chunker import ClauseChunker


def run(toks, **kw):
    c = ClauseChunker(**kw)
    out = []
    for t in toks:
        out += c.feed(t)
    return out + c.flush()


def test_sentence_punctuation_always_emits():
    assert run(["Hi", "."]) == ["Hi."]


def test_clause_punctuation_needs_min_tokens():
    # first unit: clause break allowed from 4 tokens; later units need 6
    assert run(["Yes", ",", " I", " can", " do", " that", ",", " sure", "."]) == ["Yes, I can do that,", "sure."]
    assert run(["Well", ",", " yes", ",", " I", " can", " do", " that", "."]) == ["Well, yes,", "I can do that."]


def test_max_tokens_emits_without_punctuation():
    toks = [f" w{i}" for i in range(24)]
    out = run(toks, max_tokens=12, first_max_tokens=8)
    assert out[0].split() == [f"w{i}" for i in range(8)]           # first unit: 8 tokens
    assert out[1].split() == [f"w{i}" for i in range(8, 20)]       # later units: 12 tokens
    assert len(out) == 3


def test_flush_returns_remainder_and_empties():
    c = ClauseChunker()
    c.feed("Hello")
    c.feed(" there")
    assert c.flush() == ["Hello there"] and c.flush() == []


def test_whitespace_only_is_dropped():
    assert run([" ", "\n"]) == []
