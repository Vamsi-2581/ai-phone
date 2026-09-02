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
    assert run(["Yes", ",", " I", " can", " do", " that", ",", " sure", "."]) == ["Yes, I can do that,", "sure."]


def test_max_tokens_emits_without_punctuation():
    toks = [f" w{i}" for i in range(15)]
    out = run(toks, max_tokens=12)
    assert out[0].split() == [f"w{i}" for i in range(12)] and len(out) == 2


def test_flush_returns_remainder_and_empties():
    c = ClauseChunker()
    c.feed("Hello")
    c.feed(" there")
    assert c.flush() == ["Hello there"] and c.flush() == []


def test_whitespace_only_is_dropped():
    assert run([" ", "\n"]) == []
