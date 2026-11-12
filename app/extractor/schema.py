"""JSON schema the model must fill, passed to Bedrock as a forced tool call."""

CATEGORIES = [
    "fuel",
    "transport",
    "meals",
    "groceries",
    "office_supplies",
    "software",
    "telecom",
    "utilities",
    "rent",
    "professional_services",
    "travel",
    "other",
]

EXPENSE_SCHEMA = {
    "type": "object",
    "properties": {
        "document_type": {"type": "string", "enum": ["receipt", "invoice", "other"]},
        "supplier_name": {"type": "string"},
        "supplier_tax_id": {
            "type": "string",
            "description": "Fiscal code exactly as printed, e.g. RO12345678 or CUI 12345678",
        },
        "invoice_number": {"type": "string"},
        "invoice_date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
        "currency": {"type": "string", "description": "ISO 4217 code, e.g. RON, EUR"},
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "quantity": {"type": "number"},
                    "unit_price": {"type": "number"},
                    "amount": {"type": "number", "description": "Line amount as printed"},
                    "vat_rate": {"type": "number", "description": "VAT percent, e.g. 21"},
                },
                "required": ["description", "amount"],
            },
        },
        "subtotal": {"type": "number", "description": "Total without VAT, if printed"},
        "vat_total": {"type": "number", "description": "Total VAT, if printed"},
        "total": {"type": "number", "description": "Amount paid, including VAT"},
        "category": {"type": "string", "enum": CATEGORIES},
    },
    "required": ["document_type", "supplier_name", "invoice_date", "currency", "total", "category"],
}

TOOL_NAME = "record_expense"

SYSTEM_PROMPT = (
    "You extract expense data from Romanian and international receipts and invoices. "
    "Read the document image exactly. Keep Romanian diacritics (ș, ț, ă, â, î). "
    "Copy numbers as printed; never compute or invent values that are not on the document. "
    "If a field is not printed, omit it. Always answer by calling the record_expense tool."
)
