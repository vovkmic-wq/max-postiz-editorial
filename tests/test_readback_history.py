"""A compact baseline preserves historic records without runtime raw snapshots."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def readback(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("readback_history_test", scripts / "readback_max.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


@pytest.fixture
def historical_records(readback, monkeypatch):
    records = [{"id": f"historic-{i}", "state": "published", "nested": {"value": i}}
               for i in range(113)]
    hashes = sorted(hashlib.sha256(canonical(row)).hexdigest() for row in records)
    monkeypatch.setattr(readback, "HISTORICAL_RECORD_HASHES", tuple(hashes))
    monkeypatch.setattr(readback, "HISTORICAL_RECORD_HASH_LIST_SHA256",
                        hashlib.sha256(canonical(hashes)).hexdigest())
    return records


def test_original_baseline_has_113_unique_hashes_and_integrity(readback):
    hashes = readback.HISTORICAL_RECORD_HASHES
    assert len(hashes) == len(set(hashes)) == 113
    assert hashlib.sha256(canonical(sorted(hashes))).hexdigest() == readback.HISTORICAL_RECORD_HASH_LIST_SHA256


def test_new_records_and_preparation_updates_are_allowed(readback, historical_records):
    records = copy.deepcopy(historical_records)
    records.append({"id": "new", "state": "preparing"})
    readback.verify_historical_records(records)
    records[-1]["state"] = "prepared"
    readback.verify_historical_records(list(reversed(records)))


def test_removing_historical_record_fails_even_with_new_record(readback, historical_records):
    records = copy.deepcopy(historical_records[1:]) + [{"id": "new", "state": "published"}]
    with pytest.raises(ValueError, match="OLD_113_RECORDS_CHANGED"):
        readback.verify_historical_records(records)


def test_changing_historical_record_before_readback_fails(readback, historical_records):
    records = copy.deepcopy(historical_records)
    records[10]["nested"]["value"] = "modified"
    with pytest.raises(ValueError, match="OLD_113_RECORDS_CHANGED"):
        readback.verify_historical_records(records)


def test_duplicate_identifier_fails(readback, historical_records):
    records = copy.deepcopy(historical_records)
    records.append({"id": records[0]["id"], "state": "different"})
    with pytest.raises(ValueError, match="LEDGER_RECORD_IDS_NOT_UNIQUE"):
        readback.verify_historical_records(records)


def test_modified_baseline_fails_closed(readback, historical_records, monkeypatch):
    monkeypatch.setattr(readback, "HISTORICAL_RECORD_HASHES",
                        ("0" * 64, *readback.HISTORICAL_RECORD_HASHES[1:]))
    with pytest.raises(ValueError, match="HISTORICAL_HASH_BASELINE_INVALID"):
        readback.verify_historical_records(historical_records)
