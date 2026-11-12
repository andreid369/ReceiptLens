import json

import api_handler


def _event(route, body=None, sub="user-1"):
    return {
        "routeKey": route,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"authorizer": {"jwt": {"claims": {"sub": sub}}}},
    }


class FakeTable:
    def __init__(self):
        self.items = []

    def put_item(self, Item):
        self.items.append(Item)

    def query(self, **kwargs):
        return {"Items": list(self.items)}


def test_safe_filename():
    assert api_handler.safe_filename("../../bon fiscal (1).JPG", "jpg") == "bon-fiscal-1.jpg"


def test_rejects_unsupported_type(monkeypatch):
    monkeypatch.setattr(api_handler, "table", FakeTable())
    resp = api_handler.lambda_handler(_event("POST /uploads", {"content_type": "text/html"}), None)
    assert resp["statusCode"] == 400


def test_upload_key_uses_token_tenant(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(api_handler, "table", table)
    captured = {}

    def fake_post(**kwargs):
        captured.update(kwargs)
        return {"url": "https://example", "fields": {}}

    monkeypatch.setattr(api_handler.s3, "generate_presigned_post", fake_post)
    resp = api_handler.lambda_handler(
        _event("POST /uploads", {"filename": "bon.jpg", "content_type": "image/jpeg"}, sub="abc"), None)
    assert resp["statusCode"] == 201
    assert captured["Key"].startswith("uploads/abc/")
    assert ["content-length-range", 1, api_handler.MAX_BYTES] in captured["Conditions"]
    assert table.items[0]["tenant_id"] == "abc"


def test_csv_export_skips_pending(monkeypatch):
    table = FakeTable()
    table.items = [
        {"doc_id": "1", "status": "EXTRACTED", "created_at": "2",
         "expense": {"supplier_name": "Mega Image", "total": 42}},
        {"doc_id": "2", "status": "PENDING_UPLOAD", "created_at": "1"},
    ]
    monkeypatch.setattr(api_handler, "table", table)
    resp = api_handler.lambda_handler(_event("GET /export"), None)
    lines = resp["body"].strip().splitlines()
    assert len(lines) == 2 and "Mega Image" in lines[1]
