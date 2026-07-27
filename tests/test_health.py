"""Health policy unit tests. Thresholds are placeholders; these tests pin the
mechanics (mapping raw data to verdicts), not final business rules."""
from drivestation.health.policy import evaluate_health
from drivestation.hw.simulator import make_hdd, make_nvme, make_sata_ssd
from drivestation.models import HealthVerdict


def _eval(sim_drive):
    return evaluate_health(sim_drive.info, sim_drive.health_raw)


def test_nvme_percentage_used_maps_to_health():
    result = _eval(make_nvme(percentage_used=6))
    assert result.percent == 94
    assert result.verdict == HealthVerdict.GOOD


def test_nvme_media_errors_fail():
    result = _eval(make_nvme(percentage_used=5, media_errors=3))
    assert result.verdict == HealthVerdict.FAIL
    assert any("Media errors" in w for w in result.warnings)


def test_nvme_critical_warning_fails():
    result = _eval(make_nvme(critical_warning=0x04))
    assert result.verdict == HealthVerdict.FAIL


def test_nvme_percentage_used_over_100_clamps():
    result = _eval(make_nvme(percentage_used=140))
    assert result.percent == 0
    assert result.verdict == HealthVerdict.FAIL


def test_sata_ssd_without_wear_attribute_is_not_failed():
    result = _eval(make_sata_ssd(percent_life=None))
    assert result.verdict == HealthVerdict.GOOD
    assert result.percent is None
    assert any("unavailable" in w for w in result.warnings)


def test_sata_ssd_smart_failure():
    result = _eval(make_sata_ssd(smart_passed=False))
    assert result.verdict == HealthVerdict.FAIL


def test_hdd_clean():
    result = _eval(make_hdd())
    assert result.verdict == HealthVerdict.GOOD
    assert result.percent == 100


def test_hdd_reallocated_sectors_warn():
    result = _eval(make_hdd(reallocated=8))
    assert result.verdict == HealthVerdict.WARNING
    assert result.percent == 84


def test_hdd_pending_sectors_fail():
    result = _eval(make_hdd(pending=2))
    assert result.verdict == HealthVerdict.FAIL


def test_hdd_smart_failure():
    result = _eval(make_hdd(smart_passed=False))
    assert result.verdict == HealthVerdict.FAIL
    assert result.warnings == ["SMART failure"]
