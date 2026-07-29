"""Health / scrap policy unit tests."""
from drivestation.health.policy import THRESHOLDS, evaluate_health
from drivestation.hw.simulator import make_hdd, make_nvme, make_sata_ssd
from drivestation.models import HealthVerdict


def _eval(sim_drive):
    return evaluate_health(sim_drive.info, sim_drive.health_raw)


def test_nvme_good_wear():
    result = _eval(make_nvme(percentage_used=6))
    assert result.percent == 94
    assert result.verdict == HealthVerdict.GOOD


def test_nvme_at_70_is_scrap():
    # 30% used → 70% life remaining → scrap (at or below 70)
    result = _eval(make_nvme(percentage_used=30))
    assert result.percent == 70
    assert result.verdict == HealthVerdict.SCRAP


def test_nvme_media_errors_scrap():
    result = _eval(make_nvme(percentage_used=5, media_errors=3))
    assert result.verdict == HealthVerdict.SCRAP
    assert any("Media errors" in w or "media errors" in w for w in result.warnings)


def test_nvme_critical_warning_scrap():
    result = _eval(make_nvme(critical_warning=0x04))
    assert result.verdict == HealthVerdict.SCRAP


def test_nvme_percentage_used_over_100_clamps_to_scrap():
    result = _eval(make_nvme(percentage_used=140))
    assert result.percent == 0
    assert result.verdict == HealthVerdict.SCRAP


def test_sata_ssd_without_wear_attribute_is_unknown():
    result = _eval(make_sata_ssd(percent_life=None))
    assert result.verdict == HealthVerdict.UNKNOWN
    assert result.percent is None


def test_sata_ssd_71_good_70_scrap():
    assert _eval(make_sata_ssd(percent_life=71)).verdict == HealthVerdict.GOOD
    assert _eval(make_sata_ssd(percent_life=70)).verdict == HealthVerdict.SCRAP


def test_sata_ssd_smart_failure_scrap():
    result = _eval(make_sata_ssd(smart_passed=False))
    assert result.verdict == HealthVerdict.SCRAP


def test_hdd_clean():
    result = _eval(make_hdd())
    assert result.verdict == HealthVerdict.GOOD
    assert result.percent == 100


def test_hdd_five_realloc_still_good():
    # "over 5" — five is allowed
    result = _eval(make_hdd(reallocated=5))
    assert result.percent == 90
    assert result.verdict == HealthVerdict.GOOD


def test_hdd_six_realloc_is_scrap():
    result = _eval(make_hdd(reallocated=6))
    assert result.verdict == HealthVerdict.SCRAP
    assert any("over 5" in w for w in result.warnings)


def test_hdd_health_at_or_below_80_scrap():
    # 10 realloc × 2 penalty = 80% → scrap by percent rule
    result = _eval(make_hdd(reallocated=10))
    assert result.percent == 80
    assert result.verdict == HealthVerdict.SCRAP
    assert any("80%" in w for w in result.warnings)


def test_hdd_pending_sectors_scrap():
    result = _eval(make_hdd(pending=2))
    assert result.verdict == HealthVerdict.SCRAP


def test_hdd_smart_failure_scrap():
    result = _eval(make_hdd(smart_passed=False))
    assert result.verdict == HealthVerdict.SCRAP


def test_thresholds_match_operator_rules():
    assert THRESHOLDS["ssd_scrap_at_or_below"] == 70
    assert THRESHOLDS["hdd_scrap_at_or_below"] == 80
    assert THRESHOLDS["hdd_realloc_scrap_above"] == 5
