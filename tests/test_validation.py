from datetime import date

from validation import validate_expense

TODAY = date(2026, 11, 10)


def base(**over):
    exp = {
        "supplier_name": "OMV Petrom",
        "invoice_date": "2026-11-02",
        "subtotal": 100.0,
        "vat_total": 21.0,
        "total": 121.0,
        "line_items": [{"description": "Motorină", "amount": 121.0, "vat_rate": 21}],
    }
    exp.update(over)
    return exp


def test_valid_receipt_has_no_issues():
    assert validate_expense(base(), TODAY) == []


def test_missing_total_stops_early():
    assert validate_expense(base(total=None), TODAY) == ["missing total"]


def test_vat_mismatch_is_reported():
    issues = validate_expense(base(vat_total=30.0), TODAY)
    assert any("subtotal" in i for i in issues)


def test_rounding_within_tolerance_is_accepted():
    assert validate_expense(base(vat_total=21.03), TODAY) == []


def test_net_line_items_match_subtotal():
    lines = [{"description": "Consultanță", "amount": 60.0}, {"description": "Licență", "amount": 40.0}]
    assert validate_expense(base(line_items=lines), TODAY) == []


def test_line_items_matching_nothing_fail():
    lines = [{"description": "x", "amount": 10.0}]
    assert any("line items" in i for i in validate_expense(base(line_items=lines), TODAY))


def test_unknown_vat_rate_flagged():
    lines = [{"description": "x", "amount": 121.0, "vat_rate": 24}]
    assert any("24" in i for i in validate_expense(base(line_items=lines), TODAY))


def test_old_19_percent_rate_still_allowed():
    lines = [{"description": "x", "amount": 121.0, "vat_rate": 19}]
    assert validate_expense(base(line_items=lines), TODAY) == []


def test_future_date_flagged():
    assert any("future" in i for i in validate_expense(base(invoice_date="2027-01-01"), TODAY))


def test_bad_date_format_flagged():
    assert any("ISO" in i for i in validate_expense(base(invoice_date="02.11.2026"), TODAY))
