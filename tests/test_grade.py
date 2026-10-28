"""Unit tests for the eval grader. No LLM needed."""
import pytest

from bench_agent import db
from evals.run_evals import contains_number, grade


@pytest.mark.parametrize("answer", ["1040x", "1,040 times", "1040.5×", "about 1040.50 times faster"])
def test_number_matches_formatting_variants(answer):
    assert contains_number(answer, "1040")


@pytest.mark.parametrize("answer", ["10400", "21040", "104.0", "1.040"])
def test_number_rejects_other_numbers(answer):
    assert not contains_number(answer, "1040")


def test_decimal_allows_more_precision_but_not_other_numbers():
    assert contains_number("1.121x faster", "1.12")
    assert not contains_number("11.12x", "1.12")
    assert not contains_number("1.1x", "1.12")


@pytest.mark.parametrize("answer", [
    "The data does not include H100 results; only the RTX 4060 laptop GPU was measured.",
    "I can't answer that: the benchmark doesn't cover an H100.",
    "There is no H100 data available.",
])
def test_refusal_detected(answer):
    assert grade({"refusal": True}, answer, [], db.connect()) == []


def test_made_up_number_is_not_a_refusal():
    assert grade({"refusal": True}, "On the H100 it was 3500x faster.", [], db.connect())


def test_expect_tool_skipped_for_v0():
    case = {"must_contain": ["1040"], "expect_tool": "compare_versions"}
    assert grade(case, "1040x", [], db.connect(), v0=True) == []
    assert grade(case, "1040x", [], db.connect()) == ["did not call compare_versions"]


def test_data_unchanged():
    conn = db.connect()
    conn.execute("DELETE FROM results WHERE version = 'cpu'")
    assert grade({"data_unchanged": True}, "", [], conn) == ["results has 5 rows, expected 6"]
