"""Tests for the device service layer (device doubles, no hardware)."""

from __future__ import annotations

import pytest

from app.schemas import DeviceCreate, DeviceUpdate
from app.services.device_client import DeviceUnreachable
from app.services.device_service import (
    DeviceNotFoundError,
    DeviceService,
    DuplicateDeviceError,
)


def test_create_and_list_device(db):
    service = DeviceService(db)
    service.create(DeviceCreate(name="IGD Kiri", ip="10.100.1.14", location="IGD"))

    devices = service.list()
    assert [d.name for d in devices] == ["IGD Kiri"]
    assert devices[0].port == 4370
    assert devices[0].is_online is False


def test_duplicate_ip_port_rejected(db):
    service = DeviceService(db)
    service.create(DeviceCreate(name="A", ip="10.100.1.14"))
    with pytest.raises(DuplicateDeviceError):
        service.create(DeviceCreate(name="B", ip="10.100.1.14"))


def test_same_ip_different_port_allowed(db):
    service = DeviceService(db)
    service.create(DeviceCreate(name="A", ip="10.100.1.14", port=4370))
    service.create(DeviceCreate(name="B", ip="10.100.1.14", port=4371))
    assert len(service.list()) == 2


def test_update_and_delete_device(db):
    service = DeviceService(db)
    device = service.create(DeviceCreate(name="A", ip="10.100.1.14"))

    updated = service.update(device.id, DeviceUpdate(name="IGD Kanan"))
    assert updated.name == "IGD Kanan"

    service.delete(device.id)
    assert service.list() == []


def test_get_missing_device_raises(db):
    with pytest.raises(DeviceNotFoundError):
        DeviceService(db).get(999)


def test_refresh_info_caches_device_details(db, fake_clients):
    service = DeviceService(db)
    device = service.create(DeviceCreate(name="IGD", ip="10.100.1.14"))
    fake_clients.users["10.100.1.14"] = [{"UID": str(i)} for i in range(1, 8)]

    info = service.refresh_info(device.id)

    assert info.serial_number == "TEST00000014"
    assert info.personnel_count == 7
    assert info.model == "C3-100"
    assert info.door_status  # door status was read from the panel

    db.refresh(device)
    assert device.is_online is True
    assert device.personnel_count == 7
    assert device.last_seen_at is not None
    assert device.last_error is None


def test_refresh_info_marks_device_offline_on_failure(db, fake_clients):
    service = DeviceService(db)
    device = service.create(DeviceCreate(name="Down", ip="10.100.1.99"))
    fake_clients.online["10.100.1.99"] = False
    fake_clients.unreachable_errors["10.100.1.99"] = "port 4370 diblokir"

    with pytest.raises(DeviceUnreachable):
        service.refresh_info(device.id)

    db.refresh(device)
    assert device.is_online is False
    assert "port 4370 diblokir" in (device.last_error or "")


def test_health_check_online_and_offline(db, fake_clients):
    service = DeviceService(db)
    up = service.create(DeviceCreate(name="Up", ip="10.100.1.10"))
    down = service.create(DeviceCreate(name="Down", ip="10.100.1.11"))
    fake_clients.online["10.100.1.10"] = True
    fake_clients.online["10.100.1.11"] = False

    assert service.check_health(up).online is True
    assert service.check_health(down).online is False

    db.refresh(up)
    db.refresh(down)
    assert up.is_online is True
    assert down.is_online is False


def test_discover_flags_registered_devices(db):
    service = DeviceService(db)
    service.create(DeviceCreate(name="Known", ip="10.100.1.90"))

    found = {item["ip"]: item for item in service.discover()}

    assert found["10.100.1.90"]["registered"] is True
    assert found["10.100.1.91"]["registered"] is False


def test_dashboard_rows_shape(db, fake_clients):
    service = DeviceService(db)
    device = service.create(DeviceCreate(name="IGD Kiri", ip="10.100.1.14"))
    fake_clients.users["10.100.1.14"] = [{"UID": "1"}, {"UID": "2"}]
    service.refresh_info(device.id)

    row = service.dashboard_rows()[0]

    assert row == {
        "device_name": "IGD Kiri",
        "serial_number": "TEST00000014",
        "ip_address": "10.100.1.14",
        "personnel_count": 2,
        "firmware_version": "AC Ver 5.4.3.2001 Sep 25 2019",
        "status": "online",
    }
