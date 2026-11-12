"""Business-rule checks on an extracted expense.

Pure functions only (no AWS calls) so they are cheap to unit test.
An expense that fails validation is retried through the fallback path
or stored with status NEEDS_REVIEW, never silently accepted.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

# Romanian VAT rates. 21% / 11% apply from 1 Aug 2025; 19% / 9% / 5% are kept
# because older receipts and invoices are still uploaded for past periods.
ALLOWED_VAT_RATES = {Decimal(r) for r in ("0", "5", "9", "11", "19", "21")}


def _dec(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def tolerance(total: Decimal) -> Decimal:
    """Rounding slack: at least 5 bani, or 0.5% of the total."""
    return max(Decimal("0.05"), (abs(total) * Decimal("0.005")).quantize(Decimal("0.01")))


def validate_expense(expense: dict, today: date | None = None) -> list[str]:
    """Return a list of human-readable issues. Empty list = valid."""
    issues: list[str] = []
    today = today or datetime.now(timezone.utc).date()

    total = _dec(expense.get("total"))
    if total is None:
        issues.append("missing total")
        return issues
    if total <= 0:
        issues.append("total must be positive")

    if not expense.get("supplier_name"):
        issues.append("missing supplier name")

    raw_date = expense.get("invoice_date")
    if not raw_date:
        issues.append("missing date")
    else:
        try:
            parsed = date.fromisoformat(str(raw_date))
            if parsed > today:
                issues.append(f"date {parsed} is in the future")
        except ValueError:
            issues.append(f"date '{raw_date}' is not ISO YYYY-MM-DD")

    tol = tolerance(total)
    subtotal = _dec(expense.get("subtotal"))
    vat_total = _dec(expense.get("vat_total"))
    if subtotal is not None and vat_total is not None:
        if abs(subtotal + vat_total - total) > tol:
            issues.append(f"subtotal {subtotal} + VAT {vat_total} != total {total}")

    lines = expense.get("line_items") or []
    amounts = [_dec(li.get("amount")) for li in lines]
    if lines and all(a is not None for a in amounts):
        line_sum = sum(amounts, Decimal("0"))
        # Receipts list gross line amounts, invoices often list net ones.
        matches_total = abs(line_sum - total) <= tol
        matches_subtotal = subtotal is not None and abs(line_sum - subtotal) <= tol
        if not (matches_total or matches_subtotal):
            issues.append(f"line items sum {line_sum} matches neither total nor subtotal")

    for i, li in enumerate(lines):
        rate = _dec(li.get("vat_rate"))
        if rate is not None and rate not in ALLOWED_VAT_RATES:
            issues.append(f"line {i + 1}: VAT rate {rate}% is not a Romanian rate")

    return issues
