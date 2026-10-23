"""Aggregate trade archive validation and point-in-time stablecoin conversion."""

import hashlib
import io
import json
import zipfile

import pytest
import requests

from lp_pilot.data import DataContractError, RequestBudget
from lp_pilot.fine_reference import convert_aggtrades, download_aggtrades, load_aggtrades


DAY = "2024-01-15"
START = 1705276800


def csv_row(identifier=1, timestamp=None, price="2500", quantity="2", **changes):
    row = [identifier, price, quantity, identifier * 2, identifier * 2 + 1,
           START * 1000 if timestamp is None else timestamp, "False", "True"]
    for index, value in changes.items():
        row[int(index)] = value
    return ",".join(map(str, row)) + "\n"


def write_csv(tmp_path, text):
    path = tmp_path / "trades.csv"
    path.write_text(text)
    return path


def zip_bytes(text):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("trades.csv", text)
    return stream.getvalue()


def quote(symbol, timestamp, price, identifier, **fields):
    return {"symbol": symbol, "timestamp": timestamp, "price": price,
            "aggregate_trade_id": identifier, **fields}


def test_aggregate_format_units_and_preserved_identity(tmp_path):
    path = write_csv(tmp_path, csv_row(timestamp=START * 1000 + 123))
    row = load_aggtrades(path, "ETHUSDT", DAY)[0]
    assert row["timestamp"] == pytest.approx(START + 0.123)
    assert row["source_timestamp_raw"] == START * 1000 + 123
    assert row["timestamp_unit"] == "milliseconds"
    assert row["aggregate_trade_id"] == 1
    assert (row["first_trade_id"], row["last_trade_id"]) == (2, 3)
    assert row["price"] == "2500"
    assert row["quantity"] == "2"
    assert row["buyer_is_maker"] is False
    assert row["is_best_match"] is True


def test_optional_official_header_and_half_open_selection(tmp_path):
    header = "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker,is_best_match\n"
    path = write_csv(tmp_path, header + csv_row() + csv_row(2, START * 1000 + 1000)
                     + csv_row(3, START * 1000 + 2000))
    selected = load_aggtrades(path, "ETHUSDT", DAY, start=START + 1, end=START + 2)
    assert [row["aggregate_trade_id"] for row in selected] == [2]


def test_post_2025_archive_uses_microseconds(tmp_path):
    start = 1735689600
    path = write_csv(tmp_path, csv_row(timestamp=start * 1_000_000 + 123456))
    row = load_aggtrades(path, "ETHUSDT", "2025-01-01")[0]
    assert row["timestamp_unit"] == "microseconds"
    assert row["timestamp"] == pytest.approx(start + 0.123456)


@pytest.mark.parametrize("timestamp", [START, START * 1_000_000,
                                       (START - 1) * 1000, (START + 86400) * 1000])
def test_wrong_unit_or_other_day_is_rejected(tmp_path, timestamp):
    path = write_csv(tmp_path, csv_row(timestamp=timestamp))
    with pytest.raises(DataContractError, match="timestamp unit or UTC archive day"):
        load_aggtrades(path, "ETHUSDT", DAY)


@pytest.mark.parametrize("text", [
    csv_row() + csv_row(),
    csv_row(2) + csv_row(1),
    csv_row(1, START * 1000 + 1) + csv_row(2, START * 1000),
])
def test_duplicate_ids_and_decreasing_identity_or_time_are_rejected(tmp_path, text):
    with pytest.raises(DataContractError):
        load_aggtrades(write_csv(tmp_path, text), "ETHUSDT", DAY)


def test_same_timestamp_trades_keep_distinct_ordered_ids(tmp_path):
    rows = load_aggtrades(write_csv(tmp_path, csv_row() + csv_row(2)), "ETHUSDT", DAY)
    assert [row["sequence"] for row in rows] == [1, 2]
    assert rows[0]["timestamp"] == rows[1]["timestamp"]


@pytest.mark.parametrize("changes", [
    {"price": "0"}, {"price": "NaN"}, {"price": "Infinity"}, {"price": "bad"},
    {"quantity": "0"}, {"quantity": "-1"}, {"quantity": "NaN"},
    {"6": "1"}, {"7": "perhaps"}, {"3": "9", "4": "3"},
])
def test_invalid_trade_fields_are_rejected(tmp_path, changes):
    with pytest.raises(DataContractError):
        load_aggtrades(write_csv(tmp_path, csv_row(**changes)), "ETHUSDT", DAY)


def test_outside_selected_period_is_still_validated(tmp_path):
    path = write_csv(tmp_path, csv_row() + csv_row(1, START * 1000 + 2000))
    with pytest.raises(DataContractError, match="Duplicate"):
        load_aggtrades(path, "ETHUSDT", DAY, end=START + 1)


def test_conversion_uses_backward_usdc_trade_without_parity_assumption():
    eth = [quote("ETHUSDT", 10, "2500", 10)]
    usdc = [quote("USDCUSDT", 8, "0.998", 20), quote("USDCUSDT", 11, "1.2", 21)]
    rows, diagnostics = convert_aggtrades(eth, usdc)
    row = rows[0]
    assert row["price_usdc_per_eth"] == pytest.approx(2500 / 0.998)
    assert row["eth_timestamp"] == 10
    assert row["usdc_timestamp"] == 8
    assert row["eth_available_at"] == row["available_at"] == 11
    assert row["usdc_available_at"] == 9
    assert row["eth_age_seconds"] == 0
    assert row["usdc_age_seconds"] == 2
    assert row["usdc_aggregate_trade_id"] == 20
    assert row["sequence"] == 10
    assert "ASYNCHRONOUS_REFERENCE_LEGS" in row["reference_flags"]
    assert diagnostics["converted_count"] == 1
    assert diagnostics["asynchronous_leg_count"] == 1


def test_both_prices_available_before_converted_quote_is_known():
    eth = [quote("ETHUSDT", 10, "2500", 10)]
    usdc = [quote("USDCUSDT", 9, "1", 20, available_at=12)]
    rows, diagnostics = convert_aggtrades(eth, usdc, delay_seconds=1)
    assert rows == []
    assert diagnostics["unavailable_usdc_count"] == 1
    rows, _ = convert_aggtrades(eth, usdc, delay_seconds=3)
    assert rows[0]["available_at"] == 13
    assert rows[0]["usdc_available_at"] == 12


def test_conversion_may_use_older_available_usdc_but_records_its_age():
    eth = [quote("ETHUSDT", 10, "2500", 10)]
    usdc = [quote("USDCUSDT", 7, "0.99", 20),
            quote("USDCUSDT", 9, "1", 21, available_at=30)]
    rows, _ = convert_aggtrades(eth, usdc)
    assert rows[0]["usdc_timestamp"] == 7
    assert rows[0]["usdc_age_seconds"] == 3
    assert rows[0]["price_usdc_per_eth"] == pytest.approx(2500 / 0.99)


def test_missing_and_stale_conversion_prices_are_explicit_exclusions():
    eth = [quote("ETHUSDT", -1, "2500", 1),
           quote("ETHUSDT", 120, "2500", 2), quote("ETHUSDT", 120.001, "2500", 3)]
    usdc = [quote("USDCUSDT", 0, "1", 20)]
    rows, diagnostics = convert_aggtrades(eth, usdc)
    assert [row["eth_aggregate_trade_id"] for row in rows] == [2]
    assert rows[0]["usdc_age_seconds"] == 120
    assert diagnostics["missing_usdc_count"] == 1
    assert diagnostics["stale_usdc_count"] == 1


def test_same_time_stablecoin_trade_tie_uses_last_id():
    eth = [quote("ETHUSDT", 10, "2500", 10)]
    usdc = [quote("USDCUSDT", 10, "0.9", 20), quote("USDCUSDT", 10, "1", 21)]
    rows, _ = convert_aggtrades(eth, usdc)
    assert rows[0]["usdc_aggregate_trade_id"] == 21
    assert rows[0]["price_usdc_per_eth"] == 2500


def test_future_trade_changes_leave_earlier_conversion_unchanged():
    eth = [quote("ETHUSDT", 10, "2500", 10)]
    usdc = [quote("USDCUSDT", 9, "0.99", 20), quote("USDCUSDT", 11, "1", 21)]
    before, _ = convert_aggtrades(eth, usdc)
    usdc[-1]["price"] = "500"
    after, _ = convert_aggtrades(eth, usdc)
    assert before == after


@pytest.mark.parametrize("options", [{"delay_seconds": -1}, {"delay_seconds": float("nan")},
                                      {"max_age_seconds": -1}])
def test_invalid_availability_assumptions_are_rejected(options):
    with pytest.raises(DataContractError):
        convert_aggtrades([], [], **options)


def test_conversion_rejects_duplicate_input_without_silent_sorting():
    eth = [quote("ETHUSDT", 10, "2500", 10)] * 2
    with pytest.raises(DataContractError, match="without duplicates"):
        convert_aggtrades(eth, [])


class Response:
    status_code = 200

    def __init__(self, content):
        self.content = content
        self.headers = {"Content-Length": str(len(content))}

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        yield self.content

    def close(self):
        pass


class Session:
    def __init__(self, archive, *, fail_archive=False):
        self.archive = archive
        self.calls = []
        self.fail_archive = fail_archive

    def get(self, url, **options):
        self.calls.append(url)
        assert options["allow_redirects"] is False
        if url.endswith(".CHECKSUM"):
            filename = url.rsplit("/", 1)[-1].removesuffix(".CHECKSUM")
            return Response(f"{hashlib.sha256(self.archive).hexdigest()}  {filename}\n".encode())
        if self.fail_archive:
            raise requests.Timeout("synthetic failure")
        return Response(self.archive)


def test_download_checksums_provenance_and_cache_make_no_extra_requests(tmp_path):
    session = Session(zip_bytes(csv_row()))
    budget = RequestBudget(max_requests=2, max_bytes=10000)
    first = download_aggtrades("ETHUSDT", DAY, tmp_path, budget=budget, session=session)
    assert budget.used_requests == len(session.calls) == 2
    assert first["row_count"] == 1
    assert first["checksum_verified"] is True
    assert first["timestamp_unit"] == "milliseconds"
    assert first["coverage_start"] == START
    assert first["coverage_end"] == START + 86400
    assert first["retrieved_at"]
    cached = download_aggtrades("ETHUSDT", DAY, tmp_path,
                               budget=RequestBudget(max_requests=0), session=session)
    assert cached["cached"] is True
    assert cached["retrieved_at"] == first["retrieved_at"]
    assert cached["sha256"] == first["sha256"]
    assert len(session.calls) == 2
    manifest = json.loads((tmp_path / "ETHUSDT-aggTrades-2024-01-15.zip.manifest.json").read_text())
    assert manifest == first
    assert manifest["retrieved_at"] == cached["retrieved_at"]


def test_request_budget_is_shared_across_downloads(tmp_path):
    session = Session(zip_bytes(csv_row()))
    budget = RequestBudget(max_requests=1)
    with pytest.raises(DataContractError, match="request budget"):
        download_aggtrades("ETHUSDT", DAY, tmp_path, budget=budget, session=session)
    assert len(session.calls) == 1
    assert not list(tmp_path.iterdir())


def test_byte_budget_rejects_advertised_oversize_without_reading_body(tmp_path):
    session = Session(zip_bytes(csv_row()))
    budget = RequestBudget(max_requests=2, max_bytes=120)
    with pytest.raises(DataContractError, match="remaining download byte budget"):
        download_aggtrades("ETHUSDT", DAY, tmp_path, budget=budget, session=session)
    assert budget.used_bytes < budget.max_bytes
    assert not list(tmp_path.iterdir())


def test_network_failure_leaves_no_partial_final_files(tmp_path):
    session = Session(zip_bytes(csv_row()), fail_archive=True)
    with pytest.raises(DataContractError, match="Timeout"):
        download_aggtrades("ETHUSDT", DAY, tmp_path, session=session)
    assert not list(tmp_path.iterdir())


def test_existing_corrupt_archive_is_not_replaced(tmp_path):
    filename = "ETHUSDT-aggTrades-2024-01-15.zip"
    (tmp_path / filename).write_bytes(b"preserve corrupt evidence")
    session = Session(zip_bytes(csv_row()))
    with pytest.raises(DataContractError, match="checksum mismatch"):
        download_aggtrades("ETHUSDT", DAY, tmp_path, session=session)
    assert (tmp_path / filename).read_bytes() == b"preserve corrupt evidence"
    assert len(session.calls) == 1


def test_checksum_valid_but_malformed_csv_is_not_published(tmp_path):
    session = Session(zip_bytes(csv_row(price="NaN")))
    with pytest.raises(DataContractError, match="price"):
        download_aggtrades("ETHUSDT", DAY, tmp_path, session=session)
    assert not list(tmp_path.iterdir())
