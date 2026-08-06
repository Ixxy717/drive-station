from drivestation.models import UsageSnapshot
from drivestation.station import _prior_wipe_info, _usage_looks_clean


def test_usage_looks_clean_empty():
    u = UsageSnapshot(256_000_000_000, 0, False, "Empty / 256GB", "")
    assert _usage_looks_clean(u) is True


def test_usage_looks_clean_rejects_data():
    u = UsageSnapshot(256_000_000_000, None, False, "Data present / 256GB", "")
    assert _usage_looks_clean(u) is False


def test_prior_wipe_still_clean():
    jobs = [{
        "result": "PASSED",
        "wipe_finished_at": "2026-08-06T20:28:05+00:00",
        "wipe_method": "ATA_SECURE_ERASE_ENHANCED",
        "slot": "M2-1",
        "created_at": "2026-08-06T20:27:33+00:00",
    }]
    u = UsageSnapshot(256_000_000_000, 0, False, "Empty / 256GB", "")
    info = _prior_wipe_info(jobs, u)
    assert info is not None
    assert info["still_clean"] is True
    assert info["had_passed"] is True


def test_prior_wipe_needs_rewipe_when_dirty():
    jobs = [{
        "result": "PASSED",
        "wipe_finished_at": "2026-08-06T20:28:05+00:00",
        "wipe_method": "ZERO_OVERWRITE",
        "slot": "NVME-C1",
        "created_at": "2026-08-06T20:27:33+00:00",
    }]
    u = UsageSnapshot(256_000_000_000, 50_000_000_000, True,
                      "50GB / 256GB used", "")
    info = _prior_wipe_info(jobs, u)
    assert info is not None
    assert info["still_clean"] is False
    assert info["had_passed"] is True
