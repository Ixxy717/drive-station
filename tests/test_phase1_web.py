"""Phase 1 web layer: certificates, QR codes, batch/lot tagging."""
import os
import tempfile

os.environ.setdefault("DRIVESTATION_DB",
                      os.path.join(tempfile.mkdtemp(), "web-test.db"))

from fastapi.testclient import TestClient  # noqa: E402

from drivestation.web import app as webapp  # noqa: E402

client = TestClient(webapp.app)


def _seed_record(serial: str = "CERT123", batch=None, result: str = "PASSED"):
    job_id = webapp.joblog.start_job(
        slot="SATA-1", manufacturer="Samsung", model="870 EVO",
        serial=serial, capacity_bytes=1_000_204_886_016,
        drive_type="SATA_SSD", health_percent=95, health_verdict="GOOD",
        health_warnings=[], wipe_method="ATA_SECURE_ERASE_ENHANCED",
        batch=batch)
    webapp.joblog.finish_job(job_id, result)
    return job_id


def test_cert_page_serves_for_known_serial():
    _seed_record("CERTOK1")
    res = client.get("/cert/CERTOK1")
    assert res.status_code == 200
    assert "Certificate of Data Destruction" in res.text


def test_cert_page_404_for_unknown_serial():
    assert client.get("/cert/NOPE-NOT-A-SERIAL").status_code == 404


def test_qr_svg_links_to_drive_record():
    _seed_record("QR9000")
    res = client.get("/api/records/QR9000/qr.svg")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in res.content


def test_qr_404_for_unknown_serial():
    assert client.get("/api/records/NOQR/qr.svg").status_code == 404


def test_batch_set_and_stamped_on_config():
    res = client.post("/api/batch", json={"batch": "LOT-ACME-01"})
    assert res.json()["batch"] == "LOT-ACME-01"
    assert client.get("/api/config").json()["batch"] == "LOT-ACME-01"

    # Clearing works too
    res = client.post("/api/batch", json={"batch": "  "})
    assert res.json()["batch"] is None


def test_records_filter_by_batch():
    _seed_record("BATCHD1", batch="LOT-FILTER")
    _seed_record("BATCHD2", batch="LOT-FILTER")
    _seed_record("OTHER1", batch="LOT-OTHER")

    rows = client.get("/api/records", params={"batch": "LOT-FILTER"}).json()["records"]
    serials = {r["serial"] for r in rows}
    assert serials == {"BATCHD1", "BATCHD2"}


def test_station_stamps_active_batch(station, backend, joblog):
    from drivestation.hw.simulator import make_sata_ssd
    from drivestation.models import SlotStatus

    from .conftest import wait_for

    station.batch = "LOT-LIVE"
    backend.insert_drive("SATA-1", make_sata_ssd(serial="LIVE1"))
    slot = station.slots["SATA-1"]
    wait_for(lambda: slot.status == SlotStatus.READY, message="READY")
    station.confirm_wipe("SATA-1")
    wait_for(lambda: slot.status == SlotStatus.PASSED, message="PASSED")

    rec = joblog.by_serial("LIVE1")[0]
    assert rec["batch"] == "LOT-LIVE"
