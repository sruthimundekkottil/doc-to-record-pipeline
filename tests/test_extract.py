"""Check structured extraction, bounded retries, and transient CLI failures."""

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest
from google.genai import errors
from pydantic import ValidationError

from app.pipeline import extract


@pytest.fixture
def model_call(monkeypatch):
    """Provide an isolated model client without network calls or real keys."""
    invoice = json.loads(
        (Path(__file__).resolve().parents[1] / "data/ground_truth/inv_001.json").read_text()
    )
    invoice["field_confidence"] = {name: 1.0 for name in invoice}
    response = SimpleNamespace(
        parsed=invoice,
        usage_metadata=SimpleNamespace(
            prompt_token_count=100, candidates_token_count=50, thoughts_token_count=10
        ),
    )
    factory = MagicMock()
    generate = factory.return_value.__enter__.return_value.models.generate_content
    generate.return_value = response
    sleep = MagicMock()
    monkeypatch.setattr(extract.genai, "Client", factory)
    monkeypatch.setattr(extract.time, "sleep", sleep)
    monkeypatch.setattr(extract.config, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(extract.config, "LLM_MODEL", "test-model")
    return generate, sleep, factory, response


def test_structured_output_preserves_decimal_and_usage(model_call):
    """Send an API schema with decimal strings and return the Pydantic object."""
    generate, _, factory, _ = model_call
    invoice, usage = extract.extract_fields("invoice text")
    assert invoice.total == Decimal("1475.00")
    assert isinstance(invoice.line_items[0].unit_price, Decimal)
    assert (usage.model, usage.prompt_tokens, usage.output_tokens) == ("test-model", 100, 60)
    options = generate.call_args.kwargs["config"]
    assert options.response_mime_type == "application/json"
    assert options.response_json_schema["$defs"]["LineItem"]["properties"]["unit_price"]["type"] == "string"
    assert factory.call_args.kwargs["http_options"].retry_options.attempts == 1


@pytest.mark.parametrize("status", [429, 500, 503, 599])
def test_transient_errors_retry_then_recover(model_call, caplog, status):
    """Recover after three retries and collect audit events without terminal logs."""
    generate, sleep, _, response = model_call
    generate.side_effect = [errors.APIError(status, {})] * 3 + [response]
    events = []
    with extract.capture_extraction_audit(events):
        invoice, _ = extract.extract_fields("invoice text")
    assert invoice.invoice_number == "INV-2026-001"
    assert generate.call_count == 4
    assert sleep.call_args_list == [call(1), call(2), call(4)]
    assert not caplog.records
    assert len(events) == 3
    assert all(event["stage"] == "extract" and event["outcome"] == "retry" for event in events)
    assert all(event["detail"]["http_status"] == status for event in events)


def test_exhausted_503_remains_transient(model_call):
    """Exhausted retries raise a temporary error without another delay."""
    generate, sleep, _, _ = model_call
    generate.side_effect = errors.APIError(503, {})
    with pytest.raises(extract.TransientExtractionError, match="HTTP 503"):
        extract.extract_fields("invoice text")
    assert generate.call_count == 4
    assert sleep.call_args_list == [call(1), call(2), call(4)]


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_permanent_http_errors_are_not_retried(model_call, status):
    """Surface configuration and permission failures immediately."""
    generate, sleep, _, _ = model_call
    generate.side_effect = errors.APIError(status, {})
    with pytest.raises(errors.APIError):
        extract.extract_fields("invoice text")
    assert generate.call_count == 1
    sleep.assert_not_called()


def test_invalid_schema_retries_once_and_counts_all_tokens(model_call, caplog):
    """Include usage from the invalid response in the extraction total."""
    generate, sleep, _, response = model_call
    generate.side_effect = [
        SimpleNamespace(parsed=None, usage_metadata=response.usage_metadata), response
    ]
    events = []
    with extract.capture_extraction_audit(events):
        _, usage = extract.extract_fields("invoice text")
    assert generate.call_count == 2
    assert (usage.prompt_tokens, usage.output_tokens) == (200, 120)
    assert events[0]["detail"]["reason"] == "invalid_schema"
    assert not caplog.records
    sleep.assert_not_called()


def test_second_invalid_schema_surfaces_validation_error(model_call):
    """Stop after the second invalid structured response."""
    generate, _, _, response = model_call
    response.parsed = {"total": "not a number"}
    with pytest.raises(ValidationError):
        extract.extract_fields("invoice text")
    assert generate.call_count == 2


def test_absent_nullable_fields_remain_none(model_call):
    """Keep an absent vendor and total nullable under the exact contract."""
    _, _, _, response = model_call
    response.parsed.update(vendor_name=None, total=None)
    invoice, _ = extract.extract_fields("invoice text")
    assert invoice.vendor_name is None
    assert invoice.total is None


def test_empty_text_never_calls_model(model_call):
    """Reject unusable text before creating a model client."""
    _, _, factory, _ = model_call
    with pytest.raises(ValueError, match="no extractable text"):
        extract.extract_fields(" \n ")
    factory.assert_not_called()


def test_cli_transient_failure_has_temporary_exit_code(model_call, monkeypatch, capsys):
    """Print no invoice when the provider is temporarily unavailable."""
    generate, _, _, _ = model_call
    generate.side_effect = errors.APIError(503, {})
    monkeypatch.setattr(extract.sys, "argv", ["extract", "example.pdf"])
    monkeypatch.setattr(extract, "extract_text", lambda path: ("invoice text", "native"))
    with pytest.raises(SystemExit) as result:
        extract.main()
    assert result.value.code == 75
    assert capsys.readouterr().out == ""
