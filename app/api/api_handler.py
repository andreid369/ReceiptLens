"""HTTP API behind a Cognito JWT authorizer.

Routes:
  POST /uploads            -> presigned S3 POST (size-limited) for one document
  GET  /expenses           -> the caller's documents and extraction results
  GET  /expenses/{doc_id}  -> one document
  GET  /export             -> CSV of extracted expenses

The tenant is always taken from the verified token (sub claim), never from
the request, so one user can never read another user's documents.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config

BUCKET = os.environ.get("BUCKET_NAME", "")
TABLE_NAME = os.environ.get("TABLE_NAME", "")
MAX_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(3_750_000)))
URL_TTL = int(os.environ.get("UPLOAD_URL_TTL_SECONDS", "300"))

ALLOWED_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "application/pdf": "pdf",
}

REGION = os.environ.get("AWS_REGION", "eu-central-1")

# Regional virtual-hosted endpoint: avoids the 307 redirect a new bucket outside
# us-east-1 can return on the global endpoint, which would break browser CORS.
s3 = boto3.client(
    "s3",
    region_name=REGION,
    endpoint_url=f"https://s3.{REGION}.amazonaws.com",
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)
table = boto3.resource("dynamodb").Table(TABLE_NAME) if TABLE_NAME else None


def _json_default(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(type(value).__name__)


def response(status: int, body, headers: dict | None = None) -> dict:
    is_text = isinstance(body, str)
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", **(headers or {})},
        "body": body if is_text else json.dumps(body, default=_json_default),
    }


def safe_filename(name: str, ext: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name.rsplit(".", 1)[0]).strip("-.")[:60] or "document"
    return f"{stem}.{ext}"


def tenant_of(event: dict) -> str:
    return event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]


def create_upload(tenant_id: str, body: dict) -> dict:
    content_type = body.get("content_type", "")
    if content_type not in ALLOWED_TYPES:
        return response(400, {"error": f"content_type must be one of {sorted(ALLOWED_TYPES)}"})

    doc_id = uuid.uuid4().hex
    filename = safe_filename(body.get("filename", "document"), ALLOWED_TYPES[content_type])
    key = f"uploads/{tenant_id}/{doc_id}/{filename}"

    post = s3.generate_presigned_post(
        Bucket=BUCKET,
        Key=key,
        Fields={"Content-Type": content_type},
        Conditions=[
            {"Content-Type": content_type},
            ["content-length-range", 1, MAX_BYTES],
        ],
        ExpiresIn=URL_TTL,
    )
    table.put_item(Item={
        "tenant_id": tenant_id,
        "doc_id": doc_id,
        "status": "PENDING_UPLOAD",
        "filename": filename,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return response(201, {"doc_id": doc_id, "upload": post, "max_bytes": MAX_BYTES})


def list_expenses(tenant_id: str) -> list[dict]:
    items, kwargs = [], {"KeyConditionExpression": Key("tenant_id").eq(tenant_id)}
    while True:
        page = table.query(**kwargs)
        items.extend(page["Items"])
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    items.sort(key=lambda i: i.get("created_at", ""), reverse=True)
    return items


CSV_COLUMNS = ["doc_id", "status", "invoice_date", "supplier_name", "supplier_tax_id",
               "invoice_number", "category", "currency", "subtotal", "vat_total", "total"]


def to_csv(items: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for item in items:
        if item.get("status") not in {"EXTRACTED", "NEEDS_REVIEW"}:
            continue
        expense = item.get("expense", {})
        writer.writerow({"doc_id": item["doc_id"], "status": item["status"], **expense})
    return buf.getvalue()


def lambda_handler(event, context):
    route = event.get("routeKey", "")
    tenant_id = tenant_of(event)

    if route == "POST /uploads":
        try:
            body = json.loads(event.get("body") or "{}")
        except json.JSONDecodeError:
            return response(400, {"error": "body must be JSON"})
        return create_upload(tenant_id, body)

    if route == "GET /expenses":
        return response(200, {"items": list_expenses(tenant_id)})

    if route == "GET /expenses/{doc_id}":
        doc_id = event["pathParameters"]["doc_id"]
        item = table.get_item(Key={"tenant_id": tenant_id, "doc_id": doc_id}).get("Item")
        return response(200, item) if item else response(404, {"error": "not found"})

    if route == "GET /export":
        return response(200, to_csv(list_expenses(tenant_id)), {
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": 'attachment; filename="receiptlens-expenses.csv"',
        })

    return response(404, {"error": f"no route {route}"})
