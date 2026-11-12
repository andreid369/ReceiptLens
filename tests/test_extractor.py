import json

import pytest

import handler


def test_parse_key():
    assert handler.parse_key("uploads/t1/d1/bon.jpg") == ("t1", "d1", "bon.jpg")


def test_parse_key_rejects_other_prefixes():
    with pytest.raises(ValueError):
        handler.parse_key("web/index.html")


@pytest.mark.parametrize("name,fmt", [("a.PDF", "pdf"), ("b.jpg", "jpeg"), ("c.png", "png")])
def test_file_format(name, fmt):
    assert handler.file_format(name) == fmt


def test_file_format_rejects_unknown():
    with pytest.raises(ValueError):
        handler.file_format("notes.docx")


def test_parse_tool_output():
    resp = {"output": {"message": {"content": [
        {"text": "ok"},
        {"toolUse": {"name": "record_expense", "input": {"total": 5}}},
    ]}}}
    assert handler.parse_tool_output(resp) == {"total": 5}


def test_parse_tool_output_without_tool_call():
    with pytest.raises(ValueError):
        handler.parse_tool_output({"output": {"message": {"content": [{"text": "hi"}]}}})


def test_textract_mapping_handles_romanian_number_format():
    result = {"ExpenseDocuments": [{
        "SummaryFields": [
            {"Type": {"Text": "VENDOR_NAME"}, "ValueDetection": {"Text": "Dedeman SRL"}},
            {"Type": {"Text": "TOTAL"}, "ValueDetection": {"Text": "1.234,56 lei"}},
            {"Type": {"Text": "INVOICE_RECEIPT_DATE"}, "ValueDetection": {"Text": "2026-11-01"}},
        ],
        "LineItemGroups": [{"LineItems": [{"LineItemExpenseFields": [
            {"Type": {"Text": "ITEM"}, "ValueDetection": {"Text": "Vopsea"}},
            {"Type": {"Text": "PRICE"}, "ValueDetection": {"Text": "1.234,56"}},
        ]}]}],
    }]}
    mapped = handler.map_textract_expense(result)
    assert mapped["total"] == 1234.56
    assert mapped["supplier_name"] == "Dedeman SRL"
    assert mapped["line_items"][0]["amount"] == 1234.56


def _event(key="uploads/t1/d1/bon.jpg"):
    body = {"detail": {"bucket": {"name": "b"}, "object": {"key": key}}}
    return {"Records": [{"messageId": "m1", "body": json.dumps(body)}]}


def test_handler_reports_failures_per_message(monkeypatch):
    def boom(bucket, key):
        raise RuntimeError("bedrock down")
    monkeypatch.setattr(handler, "process", boom)
    assert handler.lambda_handler(_event(), None) == {"batchItemFailures": [{"itemIdentifier": "m1"}]}


def test_handler_success(monkeypatch):
    seen = {}
    monkeypatch.setattr(handler, "process", lambda b, k: seen.setdefault("key", k) and "EXTRACTED")
    assert handler.lambda_handler(_event("uploads/t1/d1/bon%20nou.jpg"), None) == {"batchItemFailures": []}
    assert seen["key"] == "uploads/t1/d1/bon nou.jpg"
