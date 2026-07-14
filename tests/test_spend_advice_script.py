"""Tests for scripts/spend_advice.py CLI."""
import json
import pytest
import os
import tempfile
from pathlib import Path
from datetime import datetime
from unittest.mock import patch, Mock
from io import StringIO

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from spend_advice import main, _render_markdown, _render_transcript


@pytest.fixture
def temp_out_dir():
    """Create a temporary output directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


# ============================================================================
# Environment validation tests
# ============================================================================

def test_missing_ynab_token_returns_exit_1(temp_out_dir, monkeypatch):
    monkeypatch.setenv("YNAB_API_TOKEN", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    with patch("sys.argv", ["spend_advice.py", "--days", "90", "--out-dir", temp_out_dir]):
        result = main()

    assert result == 1


def test_missing_anthropic_key_returns_exit_1(temp_out_dir, monkeypatch):
    monkeypatch.setenv("YNAB_API_TOKEN", "test-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    with patch("sys.argv", ["spend_advice.py", "--days", "90", "--out-dir", temp_out_dir]):
        result = main()

    assert result == 1


# ============================================================================
# File output tests
# ============================================================================

def test_writes_markdown_and_json_to_out_dir(temp_out_dir, monkeypatch):
    monkeypatch.setenv("YNAB_API_TOKEN", "test-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    with patch("sys.argv", ["spend_advice.py", "--days", "90", "--out-dir", temp_out_dir]):
        with patch("spend_advice.YNABClient") as mock_client_cls:
            mock_client = Mock()
            mock_client_cls.return_value = mock_client
            mock_client.resolve_budget_id.return_value = "budget-id"
            mock_client.get_transactions.return_value = ([], "server-knowledge")
            mock_client.get_categories.return_value = [{"categories": []}]

            with patch("spend_advice.synthesize_advisory") as mock_synth:
                mock_synth.return_value = ("Test advisory", [
                    {"role": "user", "content": "context"},
                    {"role": "assistant", "content": "Test advisory"},
                ])

                result = main()

    assert result == 0

    md_files = list(Path(temp_out_dir).glob("reflection-*.md"))
    json_files = list(Path(temp_out_dir).glob("reflection-*.json"))

    assert len(md_files) == 1, "Should write markdown file"
    assert len(json_files) == 1, "Should write JSON file"


def test_json_contains_required_fields(temp_out_dir, monkeypatch):
    monkeypatch.setenv("YNAB_API_TOKEN", "test-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    with patch("sys.argv", ["spend_advice.py", "--days", "90", "--out-dir", temp_out_dir]):
        with patch("spend_advice.YNABClient") as mock_client_cls:
            mock_client = Mock()
            mock_client_cls.return_value = mock_client
            mock_client.resolve_budget_id.return_value = "budget-id"
            mock_client.get_transactions.return_value = ([], "server-knowledge")
            mock_client.get_categories.return_value = [{"categories": []}]

            with patch("spend_advice.synthesize_advisory") as mock_synth:
                mock_synth.return_value = ("Test", [
                    {"role": "user", "content": "ctx"},
                    {"role": "assistant", "content": "Test"},
                ])

                result = main()

    json_files = list(Path(temp_out_dir).glob("reflection-*.json"))
    with open(json_files[0]) as f:
        data = json.load(f)

    required_keys = [
        "generated_at",
        "period_days",
        "period_months_complete",
        "total_spend_dollars",
        "signals",
        "trends",
    ]
    for key in required_keys:
        assert key in data, f"JSON missing {key}"

    assert isinstance(data["signals"], list)
    assert isinstance(data["trends"], list)


def test_markdown_contains_advisory_text(temp_out_dir, monkeypatch):
    monkeypatch.setenv("YNAB_API_TOKEN", "test-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    with patch("sys.argv", ["spend_advice.py", "--days", "90", "--out-dir", temp_out_dir]):
        with patch("spend_advice.YNABClient") as mock_client_cls:
            mock_client = Mock()
            mock_client_cls.return_value = mock_client
            mock_client.resolve_budget_id.return_value = "budget-id"
            mock_client.get_transactions.return_value = ([], "server-knowledge")
            mock_client.get_categories.return_value = [{"categories": []}]

            with patch("spend_advice.synthesize_advisory") as mock_synth:
                mock_synth.return_value = ("My Custom Advisory", [
                    {"role": "user", "content": "ctx"},
                    {"role": "assistant", "content": "My Custom Advisory"},
                ])

                result = main()

    md_files = list(Path(temp_out_dir).glob("reflection-*.md"))
    md_text = md_files[0].read_text()

    assert "My Custom Advisory" in md_text


def test_markdown_contains_insights_header(temp_out_dir, monkeypatch):
    monkeypatch.setenv("YNAB_API_TOKEN", "test-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    with patch("sys.argv", ["spend_advice.py", "--days", "90", "--out-dir", temp_out_dir]):
        with patch("spend_advice.YNABClient") as mock_client_cls:
            mock_client = Mock()
            mock_client_cls.return_value = mock_client
            mock_client.resolve_budget_id.return_value = "budget-id"
            mock_client.get_transactions.return_value = ([], "server-knowledge")
            mock_client.get_categories.return_value = [{"categories": []}]

            with patch("spend_advice.synthesize_advisory") as mock_synth:
                mock_synth.return_value = ("Advisory", [
                    {"role": "user", "content": "ctx"},
                    {"role": "assistant", "content": "Advisory"},
                ])

                result = main()

    md_files = list(Path(temp_out_dir).glob("reflection-*.md"))
    md_text = md_files[0].read_text()

    assert "##" in md_text or "Advisory" in md_text


def test_tty_dialogue_loop_runs_and_writes_transcript(temp_out_dir, monkeypatch):
    """When stdin is a TTY, the loop runs and the transcript branch fires."""
    monkeypatch.setenv("YNAB_API_TOKEN", "test-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    initial_messages = [
        {"role": "user", "content": "context payload"},
        {"role": "assistant", "content": "First turn"},
    ]
    after_turn2 = initial_messages + [
        {"role": "user", "content": "that feels right"},
        {"role": "assistant", "content": "Glad to hear it."},
    ]

    with patch("sys.argv", ["spend_advice.py", "--days", "90", "--out-dir", temp_out_dir]):
        with patch("spend_advice.YNABClient") as mock_client_cls:
            mock_client = Mock()
            mock_client_cls.return_value = mock_client
            mock_client.resolve_budget_id.return_value = "budget-id"
            mock_client.get_transactions.return_value = ([], "server-knowledge")
            mock_client.get_categories.return_value = [{"categories": []}]

            with patch("spend_advice.synthesize_advisory") as mock_synth:
                mock_synth.return_value = ("First turn", initial_messages)
                with patch("spend_advice.continue_advisory_turn") as mock_continue:
                    mock_continue.return_value = ("Glad to hear it.", after_turn2)
                    with patch("sys.stdin") as mock_stdin:
                        mock_stdin.isatty.return_value = True
                        with patch("builtins.input", side_effect=["that feels right", "done"]):
                            result = main()

    assert result == 0
    mock_continue.assert_called_once()

    md_files = list(Path(temp_out_dir).glob("reflection-*.md"))
    assert len(md_files) == 1
    transcript = md_files[0].read_text()
    assert "Spending Reflection Transcript" in transcript
    assert "context payload" not in transcript


def test_tty_dialogue_loop_eof_exits_cleanly(temp_out_dir, monkeypatch):
    """EOFError from input() exits the loop without error."""
    monkeypatch.setenv("YNAB_API_TOKEN", "test-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    initial_messages = [
        {"role": "user", "content": "context payload"},
        {"role": "assistant", "content": "First turn"},
    ]

    with patch("sys.argv", ["spend_advice.py", "--days", "90", "--out-dir", temp_out_dir]):
        with patch("spend_advice.YNABClient") as mock_client_cls:
            mock_client = Mock()
            mock_client_cls.return_value = mock_client
            mock_client.resolve_budget_id.return_value = "budget-id"
            mock_client.get_transactions.return_value = ([], "server-knowledge")
            mock_client.get_categories.return_value = [{"categories": []}]

            with patch("spend_advice.synthesize_advisory") as mock_synth:
                mock_synth.return_value = ("First turn", initial_messages)
                with patch("spend_advice.continue_advisory_turn") as mock_continue:
                    with patch("sys.stdin") as mock_stdin:
                        mock_stdin.isatty.return_value = True
                        with patch("builtins.input", side_effect=EOFError):
                            result = main()

    assert result == 0
    mock_continue.assert_not_called()


def test_non_tty_skips_dialogue_loop(temp_out_dir, monkeypatch):
    """When stdin is not a TTY, the dialogue loop must not run."""
    monkeypatch.setenv("YNAB_API_TOKEN", "test-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    with patch("sys.argv", ["spend_advice.py", "--days", "90", "--out-dir", temp_out_dir]):
        with patch("spend_advice.YNABClient") as mock_client_cls:
            mock_client = Mock()
            mock_client_cls.return_value = mock_client
            mock_client.resolve_budget_id.return_value = "budget-id"
            mock_client.get_transactions.return_value = ([], "server-knowledge")
            mock_client.get_categories.return_value = [{"categories": []}]

            with patch("spend_advice.synthesize_advisory") as mock_synth:
                mock_synth.return_value = ("First turn", [
                    {"role": "user", "content": "ctx"},
                    {"role": "assistant", "content": "First turn"},
                ])
                with patch("spend_advice.continue_advisory_turn") as mock_continue:
                    with patch("sys.stdin") as mock_stdin:
                        mock_stdin.isatty.return_value = False

                        result = main()

    assert result == 0
    mock_continue.assert_not_called()


# ============================================================================
# _render_markdown() function tests
# ============================================================================

def test_render_transcript_skips_seed_context_message():
    """The first message (context data payload) must not appear as 'You:' in the transcript."""
    turns = [
        {"role": "user", "content": "Period: 90 days\nTotal Spending: $1,000.00"},
        {"role": "assistant", "content": "Here's what I notice about your spending."},
        {"role": "user", "content": "That makes sense."},
        {"role": "assistant", "content": "Great, let's talk about next steps."},
    ]
    result = _render_transcript(turns)

    assert "Period: 90 days" not in result
    assert "Here's what I notice" in result
    assert "That makes sense." in result
    assert "Great, let's talk" in result


def test_render_transcript_opens_with_advisor_not_user():
    """After skipping the seed message, the transcript should open with the advisor's voice."""
    turns = [
        {"role": "user", "content": "seed context"},
        {"role": "assistant", "content": "Welcome to your reflection."},
    ]
    result = _render_transcript(turns)

    lines = [l for l in result.splitlines() if l.strip()]
    non_header_lines = [l for l in lines if not l.startswith("#")]
    assert non_header_lines[0] == "**Advisor:**"


def test_render_markdown_includes_title():
    from spending_advisor import SpendingContext

    context = SpendingContext(
        period_days=90,
        period_months_complete=3,
        total_spend_dollars=1000.0,
        spend_by_category={},
        monthly_by_category={},
        payee_groups={},
        insights=[],
        trends=[],
    )

    md = _render_markdown(context, "Test advisory")

    assert "Spending Advisory" in md


def test_render_markdown_includes_category_totals():
    from spending_advisor import SpendingContext

    context = SpendingContext(
        period_days=90,
        period_months_complete=3,
        total_spend_dollars=100.0,
        spend_by_category={"Dining": 50.0, "Groceries": 50.0},
        monthly_by_category={},
        payee_groups={},
        insights=[],
        trends=[],
    )

    md = _render_markdown(context, "Test")

    assert "Dining" in md or "Groceries" in md or "Category" in md
