"""Tests for the output limit (handout test: "Output limit")."""
from harness.output import shorten


def test_short_output_is_unchanged():
    assert shorten("all good", limit=100) == "all good"


def test_large_output_is_bounded_and_marked():
    text = "START" + "x" * 10_000 + "END"
    result = shorten(text, limit=200)

    assert result.startswith("START")
    assert result.endswith("END")
    assert "[output shortened: 9808 characters removed]" in result
    # The kept text plus the marker line stay close to the limit.
    assert len(result) < 200 + 60
