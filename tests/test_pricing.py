from datetime import date
from decimal import Decimal

import pytest

from evcc_chargeback.pricing import Tariff, amount, parse_tariffs, tariff_for

TARIFFS = [Tariff(date(2027, 1, 1), Decimal("0.31")), Tariff(date(2028, 1, 1), Decimal("0.29"))]


def test_parses_and_returns_decimals():
    assert parse_tariffs("2027-01-01:0.31,2028-01-01:0.29") == TARIFFS
    assert all(isinstance(t.price, Decimal) for t in parse_tariffs("2027-01-01:0.31"))


def test_sorts_unsorted_input():
    assert parse_tariffs("2028-01-01:0.29,2027-01-01:0.31") == TARIFFS


def test_allows_whitespace_and_trailing_or_empty_entries():
    assert parse_tariffs(" 2027-01-01 : 0.31 , , 2028-01-01:0.29 ,") == TARIFFS


def test_zero_price_is_allowed():
    assert parse_tariffs("2027-01-01:0")[0].price == Decimal("0")


def test_five_decimals_are_allowed():
    assert parse_tariffs("2027-01-01:0.12345")[0].price == Decimal("0.12345")


@pytest.mark.parametrize("text", ["", "   ", ",", " , ,"])
def test_rejects_empty_input(text):
    with pytest.raises(ValueError, match="no tariff entries"):
        parse_tariffs(text)


@pytest.mark.parametrize("entry", ["2027-13-01:0.31", "2027-02-30:0.31", "01-01-2027:0.31", "soon:0.31", ":0.31"])
def test_rejects_bad_date_and_names_entry(entry):
    with pytest.raises(ValueError, match="bad date") as error:
        parse_tariffs(f"2026-01-01:0.30,{entry}")
    assert entry in str(error.value)


def test_rejects_entry_without_colon():
    with pytest.raises(ValueError, match="bad tariff entry 'abc'"):
        parse_tariffs("abc")


@pytest.mark.parametrize("price", ["abc", "", "NaN", "Infinity", "-Infinity", "1e3x", "0.3.1"])
def test_rejects_bad_or_non_finite_price(price):
    with pytest.raises(ValueError, match="bad price") as error:
        parse_tariffs(f"2027-01-01:{price}")
    assert f"2027-01-01:{price}" in str(error.value)


def test_rejects_decimal_comma_with_clear_message():
    with pytest.raises(ValueError, match="decimal point, not a comma") as error:
        parse_tariffs("2027-01-01:0,31")
    assert "'31'" in str(error.value)


def test_rejects_negative_price():
    with pytest.raises(ValueError, match="negative price") as error:
        parse_tariffs("2027-01-01:-0.31")
    assert "2027-01-01:-0.31" in str(error.value)


def test_rejects_more_than_five_decimals():
    with pytest.raises(ValueError, match="more than 5 decimal places") as error:
        parse_tariffs("2027-01-01:0.123456")
    assert "2027-01-01:0.123456" in str(error.value)


def test_rejects_duplicate_start_date():
    with pytest.raises(ValueError, match="duplicate start date 2027-01-01") as error:
        parse_tariffs("2027-01-01:0.31,2028-01-01:0.29,2027-01-01:0.30")
    assert "2027-01-01:0.30" in str(error.value)


def test_tariff_starts_on_its_start_day():
    assert tariff_for(TARIFFS, date(2027, 1, 1)) == TARIFFS[0]


def test_tariff_does_not_apply_before_its_start_day():
    with pytest.raises(ValueError, match="2026-12-31") as error:
        tariff_for(TARIFFS, date(2026, 12, 31))
    assert "2027-01-01" in str(error.value)


def test_latest_started_tariff_applies():
    assert tariff_for(TARIFFS, date(2027, 12, 31)) == TARIFFS[0]
    assert tariff_for(TARIFFS, date(2028, 1, 1)) == TARIFFS[1]
    assert tariff_for(TARIFFS, date(2030, 6, 1)) == TARIFFS[1]


def test_tariff_for_works_on_unsorted_list():
    assert tariff_for(list(reversed(TARIFFS)), date(2027, 6, 1)) == TARIFFS[0]


def test_tariff_for_rejects_empty_list():
    with pytest.raises(ValueError, match="no tariffs"):
        tariff_for([], date(2027, 1, 1))


def test_amount_rounds_half_a_cent_up():
    assert amount(Decimal("10.5"), Decimal("0.31")) == Decimal("3.26")
    assert amount(Decimal("10.1"), Decimal("0.05")) == Decimal("0.51")  # 0.505


def test_amount_rounds_below_half_down_and_keeps_two_places():
    assert amount(Decimal("10.4"), Decimal("0.31")) == Decimal("3.22")  # 3.224
    assert str(amount(Decimal("10"), Decimal("0.3"))) == "3.00"
    assert str(amount(Decimal("0"), Decimal("0.31"))) == "0.00"
