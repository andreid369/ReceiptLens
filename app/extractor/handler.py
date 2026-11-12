"""SQS-triggered extractor.

Flow per document:
  1. S3 "Object Created" event (via EventBridge -> SQS) names the uploaded file.
  2. Bedrock reads the image/PDF and returns JSON through a forced tool call.
  3. Business rules validate totals, VAT and dates.
  4. If validation fails, Textract AnalyzeExpense runs as a fallback.
  5. The result is written to DynamoDB as EXTRACTED or NEEDS_REVIEW.

Failures are reported per message (ReportBatchItemFailures), so one broken
file is retried and eventually lands in the DLQ without blocking the batch.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from schema import EXPENSE_SCHEMA, SYSTEM_PROMPT, TOOL_NAME
from validation import validate_expense

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

TABLE_NAME = os.environ.get("TABLE_NAME", "")
MODEL_ID = os.environ.get("MODEL_ID", "")
TEXTRACT_FALLBACK = os.environ.get("TEXTRACT_FALLBACK", "true") == "true"
MAX_BYTES = int(os.environ.get("MAX_DOCUMENT_BYTES", str(3_750_000)))
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "ReceiptLens")

_retry = Config(retries={"max_attempts": 4, "mode": "adaptive"})
s3 = boto3.client("s3")
bedrock = boto3.client("bedrock-runtime", config=_retry)
textract = boto3.client("textract", config=_retry)
table = boto3.resource("dynamodb").Table(TABLE_NAME) if TABLE_NAME else None

IMAGE_FORMATS = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}


# ---------- helpers ----------------------------------------------------------

def parse_key(key: str) -> tuple[str, str, str]:
    """uploads/<tenant_id>/<doc_id>/<filename> -> (tenant_id, doc_id, filename)."""
    parts = key.split("/", 3)
    if len(parts) != 4 or parts[0] != "uploads":
        raise ValueError(f"unexpected object key: {key}")
    return parts[1], parts[2], parts[3]


def file_format(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "pdf":
        return "pdf"
    if ext in IMAGE_FORMATS:
        return IMAGE_FORMATS[ext]
    raise ValueError(f"unsupported file type: {filename}")


def emit_metrics(**values: float) -> None:
    """CloudWatch Embedded Metric Format: one log line, no extra API calls."""
    print(json.dumps({
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": METRIC_NAMESPACE,
                "Dimensions": [[]],
                "Metrics": [{"Name": k, "Unit": "Count"} for k in values],
            }],
        },
        **values,
    }))


def to_dynamo(value):
    """DynamoDB needs Decimal, not float."""
    return json.loads(json.dumps(value), parse_float=Decimal)


# ---------- extraction -------------------------------------------------------

def build_content(fmt: str, data: bytes) -> dict:
    if fmt == "pdf":
        return {"document": {"format": "pdf", "name": "document", "source": {"bytes": data}}}
    return {"image": {"format": fmt, "source": {"bytes": data}}}


def extract_with_bedrock(fmt: str, data: bytes) -> tuple[dict, dict]:
    response = bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": SYSTEM_PROMPT}],
        messages=[{
            "role": "user",
            "content": [build_content(fmt, data), {"text": "Extract this expense."}],
        }],
        toolConfig={
            "tools": [{"toolSpec": {
                "name": TOOL_NAME,
                "description": "Record the structured expense read from the document.",
                "inputSchema": {"json": EXPENSE_SCHEMA},
            }}],
            "toolChoice": {"tool": {"name": TOOL_NAME}},
        },
        inferenceConfig={"temperature": 0, "maxTokens": 2000},
    )
    return parse_tool_output(response), response.get("usage", {})


def parse_tool_output(response: dict) -> dict:
    for block in response["output"]["message"]["content"]:
        tool_use = block.get("toolUse")
        if tool_use and tool_use.get("name") == TOOL_NAME:
            return tool_use["input"]
    raise ValueError("model did not call the extraction tool")


def _amount(text: str | None):
    if not text:
        return None
    cleaned = "".join(ch for ch in text if ch.isdigit() or ch in ",.-")
    if cleaned.count(",") and cleaned.count("."):
        cleaned = cleaned.replace(".", "").replace(",", ".")  # 1.234,56
    else:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def map_textract_expense(result: dict) -> dict:
    """Map AnalyzeExpense output onto our schema (summary fields + line items)."""
    doc = (result.get("ExpenseDocuments") or [{}])[0]
    fields = {}
    for f in doc.get("SummaryFields", []):
        ftype = f.get("Type", {}).get("Text")
        fields.setdefault(ftype, f.get("ValueDetection", {}).get("Text"))

    lines = []
    for group in doc.get("LineItemGroups", []):
        for item in group.get("LineItems", []):
            cols = {c.get("Type", {}).get("Text"): c.get("ValueDetection", {}).get("Text")
                    for c in item.get("LineItemExpenseFields", [])}
            amount = _amount(cols.get("PRICE"))
            if amount is not None:
                lines.append({"description": cols.get("ITEM") or "", "amount": amount})

    expense = {
        "document_type": "invoice" if fields.get("INVOICE_RECEIPT_ID") else "receipt",
        "supplier_name": fields.get("VENDOR_NAME"),
        "supplier_tax_id": fields.get("TAX_PAYER_ID"),
        "invoice_number": fields.get("INVOICE_RECEIPT_ID"),
        "invoice_date": fields.get("INVOICE_RECEIPT_DATE"),
        "subtotal": _amount(fields.get("SUBTOTAL")),
        "vat_total": _amount(fields.get("TAX")),
        "total": _amount(fields.get("TOTAL")),
        "line_items": lines,
    }
    return {k: v for k, v in expense.items() if v not in (None, "", [])}


def extract_with_textract(bucket: str, key: str) -> dict:
    result = textract.analyze_expense(Document={"S3Object": {"Bucket": bucket, "Name": key}})
    return map_textract_expense(result)


# ---------- persistence ------------------------------------------------------

def already_done(tenant_id: str, doc_id: str) -> bool:
    item = table.get_item(Key={"tenant_id": tenant_id, "doc_id": doc_id}).get("Item")
    return bool(item and item.get("status") in {"EXTRACTED", "NEEDS_REVIEW"})


def save(tenant_id: str, doc_id: str, **attrs) -> None:
    attrs["updated_at"] = datetime.now(timezone.utc).isoformat()
    names = {f"#{k}": k for k in attrs}
    values = {f":{k}": to_dynamo(v) for k, v in attrs.items()}
    table.update_item(
        Key={"tenant_id": tenant_id, "doc_id": doc_id},
        UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in attrs),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


# ---------- entry point ------------------------------------------------------

def process(bucket: str, key: str) -> str:
    tenant_id, doc_id, filename = parse_key(key)
    if already_done(tenant_id, doc_id):
        log.info("skip duplicate delivery doc_id=%s", doc_id)  # SQS is at-least-once
        return "SKIPPED"

    fmt = file_format(filename)
    head = s3.head_object(Bucket=bucket, Key=key)
    if head["ContentLength"] > MAX_BYTES:
        save(tenant_id, doc_id, status="NEEDS_REVIEW", issues=["file too large for extraction"])
        return "NEEDS_REVIEW"

    data = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    save(tenant_id, doc_id, status="PROCESSING", filename=filename)

    expense, usage = extract_with_bedrock(fmt, data)
    issues = validate_expense(expense)
    source = "bedrock"
    metrics = {
        "DocumentsProcessed": 1,
        "BedrockInputTokens": usage.get("inputTokens", 0),
        "BedrockOutputTokens": usage.get("outputTokens", 0),
        "TextractFallbacks": 0,
    }

    if issues and TEXTRACT_FALLBACK:
        log.info("validation failed, using Textract fallback doc_id=%s issues=%s", doc_id, issues)
        try:
            fallback = extract_with_textract(bucket, key)
            fallback_issues = validate_expense({**expense, **fallback})
            metrics["TextractFallbacks"] = 1
            if len(fallback_issues) < len(issues):
                expense, issues, source = {**expense, **fallback}, fallback_issues, "textract"
        except ClientError as err:
            log.warning("textract fallback failed: %s", err)

    status = "EXTRACTED" if not issues else "NEEDS_REVIEW"
    metrics["NeedsReview"] = int(bool(issues))
    save(tenant_id, doc_id, status=status, expense=expense, issues=issues, source=source,
         model_id=MODEL_ID)
    emit_metrics(**metrics)
    return status


def lambda_handler(event, context):
    failures = []
    for record in event.get("Records", []):
        try:
            detail = json.loads(record["body"])["detail"]
            bucket = detail["bucket"]["name"]
            key = urllib.parse.unquote_plus(detail["object"]["key"])
            status = process(bucket, key)
            log.info("processed key=%s status=%s", key, status)
        except Exception:  # noqa: BLE001 - report and let SQS retry / DLQ
            log.exception("failed message_id=%s", record.get("messageId"))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
