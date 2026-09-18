"""Pydantic request/response schemas (auto-published to Swagger)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------
class DeviceBase(BaseModel):
    name: str = Field(..., max_length=128, examples=["IGD Kiri"])
    ip: str = Field(..., examples=["10.100.1.14"])
    port: int = Field(4370, ge=1, le=65535)
    password: str | None = Field(None, description="Device connection password, if any")
    location: str | None = None
    area: str | None = None
    is_active: bool = True


class DeviceCreate(DeviceBase):
    pass


class DeviceUpdate(BaseModel):
    name: str | None = None
    ip: str | None = None
    port: int | None = Field(None, ge=1, le=65535)
    password: str | None = None
    location: str | None = None
    area: str | None = None
    is_active: bool | None = None


class DeviceRead(ORMModel):
    id: int
    name: str
    ip: str
    port: int
    location: str | None = None
    area: str | None = None
    is_active: bool

    model: str | None = None
    serial_number: str | None = None
    firmware_version: str | None = None
    mac_address: str | None = None
    lock_count: int | None = None
    reader_count: int | None = None
    max_user_count: int | None = None
    personnel_count: int | None = None

    is_online: bool
    last_seen_at: datetime | None = None
    last_error: str | None = None


class DeviceInfoRead(BaseModel):
    """Result of "Get Information of Device"."""

    device_id: int
    ip: str
    reachable: bool
    serial_number: str | None = None
    firmware_version: str | None = None
    device_model: str | None = None
    mac_address: str | None = None
    lock_count: int | None = None
    reader_count: int | None = None
    max_user_count: int | None = None
    max_fingerprint_count: int | None = None
    personnel_count: int | None = None
    door_status: dict[str, str] = Field(default_factory=dict)
    raw_parameters: dict[str, str] = Field(default_factory=dict)
    error: str | None = None


class DeviceHealth(BaseModel):
    device_id: int
    name: str
    ip: str
    is_online: bool
    latency_ms: float | None = None
    error: str | None = None


class DiscoveredDevice(BaseModel):
    ip: str
    port: int
    serial_number: str | None = None
    device_name: str | None = None


# --------------------------------------------------------------------------
# Network scan (searching for panels on an IP range)
# --------------------------------------------------------------------------
class NetworkScanRequest(BaseModel):
    ranges: list[str] = Field(
        ...,
        min_length=1,
        description="CIDR / single IP / a-b range, e.g. ['10.100.1.0/24', '192.168.1.0/24']",
        examples=[["10.100.1.0/24", "192.168.1.0/24"]],
    )
    port: int = Field(4370, ge=1, le=65535)
    timeout: float = Field(1.0, ge=0.1, le=10.0, description="TCP timeout per host, seconds")
    workers: int = Field(64, ge=1, le=256, description="Parallel TCP probes")
    identify: bool = Field(
        True, description="Read serial/name/firmware from hosts whose port is open"
    )
    local_address: str | None = Field(
        None, description="Force the source address (host with several NICs)"
    )


class NetworkScanHost(BaseModel):
    ip: str
    port: int
    latency_ms: float | None = None
    serial_number: str | None = None
    device_name: str | None = None
    firmware_version: str | None = None
    lock_count: int | None = None
    model: str | None = None
    mac_address: str | None = None
    #: True when this panel is already in the database (matched by serial, then IP).
    registered: bool = False
    device_id: int | None = None
    #: The address the database holds for it. Different from `ip` = stale record.
    device_ip: str | None = None
    #: The panel's own network configuration (read-only, as reported by the panel).
    panel_ip: str | None = None
    panel_netmask: str | None = None
    panel_gateway: str | None = None
    error: str | None = None


class NetworkScanResult(BaseModel):
    ranges: list[str]
    hosts_scanned: int
    hosts_open: int
    duration_ms: float
    hosts: list[NetworkScanHost]


# --------------------------------------------------------------------------
# A panel's own network configuration (the ZKAccess "Modify IP Address" feature)
# --------------------------------------------------------------------------
class PanelNetworkRead(BaseModel):
    device_id: int
    name: str
    ip: str
    port: int
    serial_number: str | None = None
    ip_address: str | None = None
    netmask: str | None = None
    gateway: str | None = None
    mac: str | None = None
    #: False when the server has no PANEL_NETWORK_WRITE_ENABLED, i.e. nothing can be
    #: written yet (a dry run still is). Reported here so the UI can say so up front
    #: instead of only after the operator has pressed the write button.
    write_enabled: bool = False


class PanelNetworkUpdate(BaseModel):
    ip: str = Field(..., description="Alamat baru panel", examples=["10.100.1.30"])
    netmask: str = Field(..., description="NetMask panel", examples=["255.255.255.0"])
    gateway: str | None = Field(
        None,
        description=(
            "Kosongkan kalau panel tidak memakai gateway. Firmware ini tidak "
            "melaporkan gateway, jadi nilainya tidak bisa dibaca dan disalin otomatis."
        ),
    )


class PanelNetworkChangeRead(BaseModel):
    device_id: int
    name: str
    current: PanelNetworkRead
    new_ip: str
    netmask: str
    gateway: str
    #: False when PANEL_NETWORK_WRITE_ENABLED is off (nothing is ever written then).
    enabled: bool
    dry_run: bool
    written: bool
    #: None on a dry run. False means the panel did not answer on the new address.
    reachable_at_new_address: bool | None = None
    record_updated: bool
    warnings: list[str] = Field(default_factory=list)
    #: Exactly what is handed to the panel (SetDeviceParam).
    params: dict[str, str] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# Personnel
# --------------------------------------------------------------------------
class PersonnelBase(BaseModel):
    employee_id: str = Field(..., max_length=32, examples=["1001"])
    name: str = Field(..., max_length=128)
    card_number: str | None = None
    pin: str | None = None
    #: Department by name. Resolved against the department master; an unknown
    #: name is created automatically so bulk imports keep working.
    department: str | None = None
    #: Department by id (takes precedence over `department`).
    department_id: int | None = None
    is_active: bool = True
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    #: Ensure the person is a member of this access level (idempotent, additive).
    access_group_id: int | None = None
    #: Set the person's access levels to exactly this list (replaces existing).
    access_group_ids: list[int] | None = None
    device_id: int | None = None


class PersonnelCreate(PersonnelBase):
    pass


class PersonnelUpdate(BaseModel):
    #: The badge/PIN on the device. Changing it re-labels the person on every
    #: panel that reads from this master, so it is guarded against duplicates
    #: just like a create.
    employee_id: str | None = Field(None, max_length=32)
    name: str | None = None
    card_number: str | None = None
    pin: str | None = None
    #: Department by name (resolved/created against the master).
    department: str | None = None
    #: Department by id; send 0 or use department_id=null semantics via `clear_department`.
    department_id: int | None = None
    #: Set to true to detach the person from any department.
    clear_department: bool = False
    is_active: bool | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    #: Ensure membership of this access level. Additive: other memberships are
    #: kept, because a person can legitimately hold up to ~11 of them.
    access_group_id: int | None = None
    #: Replace the person's access levels with exactly this list. Use this to
    #: "move" somebody; send [] to clear all of their access levels.
    access_group_ids: list[int] | None = None
    device_id: int | None = None


class PersonnelRead(ORMModel):
    id: int
    employee_id: str
    name: str
    card_number: str | None = None
    pin: str | None = None
    #: Derived from the department master (read-only).
    department: str | None = None
    department_id: int | None = None
    is_active: bool
    access_group_id: int | None = None
    device_id: int | None = None
    #: All groups this person belongs to (authoritative, many-to-many).
    access_group_names: list[str] = Field(default_factory=list)
    #: Same membership as ids, so an edit form can tick the right boxes.
    access_group_ids: list[int] = Field(default_factory=list)


class DevicePersonnel(BaseModel):
    """A user record read back from a physical device."""

    employee_id: str | None = None
    name: str | None = None
    card_number: str | None = None
    pin: str | None = None
    privilege: str | None = None
    group: str | None = None


class DeviceDataRead(BaseModel):
    device_id: int
    table: str
    count: int
    records: list[dict]


# --------------------------------------------------------------------------
# Access groups (ZKAccess calls these "access levels")
# --------------------------------------------------------------------------
class AccessGroupCreate(BaseModel):
    name: str = Field(..., max_length=128, examples=["ICU"])
    description: str | None = None
    device_timezone_id: int = 1


class AccessGroupUpdate(BaseModel):
    name: str | None = Field(None, max_length=128)
    description: str | None = None
    device_timezone_id: int | None = None


class AccessGroupRead(ORMModel):
    id: int
    name: str
    description: str | None = None
    device_timezone_id: int
    door_count: int = 0
    member_count: int = 0
    #: Name of the access time zone this level uses ("24 Jam"), if defined.
    time_zone_name: str | None = None
    is_24_hour: bool | None = None


class AccessGroupDoorCreate(BaseModel):
    device_id: int
    door_number: int = Field(1, ge=1, le=4)


class AccessGroupDoorRead(ORMModel):
    id: int
    access_group_id: int
    device_id: int
    door_number: int
    device_name: str | None = None
    device_ip: str | None = None


class AccessGroupMemberRead(ORMModel):
    personnel_id: int
    employee_id: str
    name: str
    department: str | None = None


class AccessGroupMemberAdd(BaseModel):
    """Members can be given either by internal id or by device PIN/badge."""

    personnel_id: int | None = None
    employee_id: str | None = Field(None, description="Badgenumber / PIN on the device")
    employee_ids: list[str] | None = Field(None, description="Bulk add several badges in one call")


class AccessGroupDetail(AccessGroupRead):
    doors: list[AccessGroupDoorRead] = Field(default_factory=list)
    members: list[AccessGroupMemberRead] = Field(default_factory=list)


class AccessGroupChangeResult(BaseModel):
    access_group_id: int
    added: int = 0
    removed: int = 0
    skipped: int = 0
    message: str | None = None


# --------------------------------------------------------------------------
# Departments
# --------------------------------------------------------------------------
class DepartmentCreate(BaseModel):
    name: str = Field(..., max_length=128, examples=["Bidang Keperawatan"])
    parent_id: int | None = None
    #: ZKAccess DEPTID, kept so re-running the migration matches rows.
    legacy_id: int | None = None
    code: str | None = Field(None, max_length=32)
    description: str | None = None


class DepartmentUpdate(BaseModel):
    name: str | None = Field(None, max_length=128)
    parent_id: int | None = None
    code: str | None = Field(None, max_length=32)
    description: str | None = None
    #: Set to true to move this department to the top level.
    clear_parent: bool = False


class DepartmentRead(ORMModel):
    id: int
    name: str
    legacy_id: int | None = None
    code: str | None = None
    description: str | None = None
    parent_id: int | None = None
    personnel_count: int = 0


class DepartmentNode(BaseModel):
    """A department with its nested children, for the tree view."""

    id: int
    name: str
    legacy_id: int | None = None
    parent_id: int | None = None
    personnel_count: int = 0
    children: list[DepartmentNode] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Login / role-based access
#
# The rules (password length, which roles exist) live in
# `app/services/auth_service.py` — that is the single validator, so the API and
# the CLI/seed path cannot drift apart. These models only describe the shapes.
# --------------------------------------------------------------------------
class LoginRequest(BaseModel):
    username: str = Field(..., max_length=64, examples=["admin"])
    password: str = Field(..., max_length=256)


class UserRead(ORMModel):
    id: int
    username: str
    full_name: str | None = None
    #: `admin` = every tab and setting, `hr` = personnel/departments/access levels.
    role: str
    is_active: bool
    #: True while the account still uses the password it was seeded with. The
    #: dashboard warns about it; it never blocks a login.
    must_change_password: bool = False
    last_login_at: datetime | None = None


class UserCreate(BaseModel):
    username: str = Field(..., max_length=64)
    password: str = Field(..., max_length=256)
    role: str = Field("hr", description="admin = semua setting, hr = personel/departemen/level")
    full_name: str | None = Field(None, max_length=128)


class UserUpdate(BaseModel):
    """Every field is optional — send only what changes."""

    role: str | None = None
    full_name: str | None = None
    is_active: bool | None = None
    password: str | None = Field(None, max_length=256)
    must_change_password: bool | None = None


class PasswordChange(BaseModel):
    current_password: str = Field(..., max_length=256)
    new_password: str = Field(..., max_length=256)


class DepartmentDetail(DepartmentRead):
    full_path: str | None = None
    parent_name: str | None = None
    children: list[DepartmentRead] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Access control time zones ("jam akses")
# --------------------------------------------------------------------------
class TimeZoneSlotInput(BaseModel):
    """One start-end segment. `day_of_week` follows the device: 0 = Sunday."""

    day_of_week: int = Field(..., ge=0, le=6, description="0=Sunday .. 6=Saturday")
    slot: int = Field(1, ge=1, le=3, description="Segment number (firmware allows up to 3)")
    start_time: str = Field("00:00", pattern=r"^\d{2}:\d{2}$")
    end_time: str = Field("00:00", pattern=r"^\d{2}:\d{2}$")


class TimeZoneSlotRead(ORMModel):
    day_of_week: int
    slot: int
    start_time: str
    end_time: str

    @property
    def is_empty(self) -> bool:
        return self.start_time == self.end_time


class TimeZoneDayRead(BaseModel):
    """A weekday with only its active segments, for display."""

    day_of_week: int
    day_name: str
    segments: list[str] = Field(default_factory=list)


class TimeZoneCreate(BaseModel):
    name: str = Field(..., max_length=128, examples=["24 Jam"])
    device_timezone_id: int = Field(
        ..., ge=1, description="Slot number the panel uses (ZKAccess acc_timeseg.id)"
    )
    description: str | None = None
    is_24_hour: bool = False
    slots: list[TimeZoneSlotInput] = Field(default_factory=list)


class TimeZoneUpdate(BaseModel):
    name: str | None = Field(None, max_length=128)
    device_timezone_id: int | None = Field(None, ge=1)
    description: str | None = None
    is_24_hour: bool | None = None
    #: When given, replaces every segment of the zone.
    slots: list[TimeZoneSlotInput] | None = None


class TimeZoneRead(ORMModel):
    id: int
    name: str
    device_timezone_id: int
    description: str | None = None
    is_24_hour: bool
    slots: list[TimeZoneSlotRead] = Field(default_factory=list)


class TimeZoneDetail(TimeZoneRead):
    """Human-readable weekly view plus how many access levels use it."""

    week: list[TimeZoneDayRead] = Field(default_factory=list)
    access_group_count: int = 0


class SetGroupTimeZone(BaseModel):
    time_zone_id: int


# --------------------------------------------------------------------------
# Access logs
# --------------------------------------------------------------------------
class AccessLogRead(ORMModel):
    id: int
    device_id: int
    device_serial: str | None = None
    event_time: datetime
    event_code: int | None = None
    event_type: str | None = None
    card_number: str | None = None
    employee_id: str | None = None
    door_number: int | None = None
    verification_mode: str | None = None
    in_out_status: str | None = None


class LogPullResult(BaseModel):
    device_id: int
    fetched: int
    inserted: int
    duplicates: int
    #: Rows the panel returned that fell outside the requested date window.
    skipped: int = 0
    error: str | None = None


class RealtimeEvent(BaseModel):
    device_id: int
    record_type: str
    event_time: datetime | None = None
    event_code: int | None = None
    event_type: str | None = None
    card_number: str | None = None
    door_number: int | None = None
    raw: dict


# --------------------------------------------------------------------------
# Realtime monitoring (the Monitoring tab)
# --------------------------------------------------------------------------
class MonitorPollingState(BaseModel):
    """Whether anything is polling the panels, and how often.

    Exposed because `SCHEDULER_ENABLED=false` (the dev launcher and the API
    systemd unit) leaves the feed motionless while the panels are perfectly
    healthy — the page has to be able to say which one it is.
    """

    enabled: bool
    log_interval_seconds: int
    health_interval_seconds: int


class MonitorStats(BaseModel):
    devices: int
    online: int
    offline: int
    log_rows: int
    #: How many events fall inside the requested date window (None = no window).
    events_in_range: int | None = None
    last_event_at: datetime | None = None


class MonitorWindow(BaseModel):
    """The date window the feed is restricted to, echoed back for the page."""

    since: datetime | None = None
    until: datetime | None = None


class MonitorDeviceStatus(BaseModel):
    id: int
    name: str
    ip: str
    port: int
    location: str | None = None
    area: str | None = None
    is_active: bool
    is_online: bool
    last_seen_at: datetime | None = None
    last_log_time: datetime | None = None
    last_error: str | None = None
    personnel_count: int | None = None
    lock_count: int | None = None


class MonitorEvent(BaseModel):
    """One access event in the live feed."""

    id: int
    device_id: int
    device_name: str | None = None
    event_time: datetime
    event_code: int | None = None
    event_type: str | None = None
    card_number: str | None = None
    employee_id: str | None = None
    person_name: str | None = None
    door_number: int | None = None
    verification_mode: str | None = None
    in_out_status: str | None = None
    #: "diterima" | "ditolak" | "lain" — only EventType 0 and 27 are classified,
    #: because those are the two verified on this fleet.
    outcome: str


class MonitorSnapshot(BaseModel):
    generated_at: datetime
    polling: MonitorPollingState
    stats: MonitorStats
    devices: list[MonitorDeviceStatus]
    #: Newest first.
    events: list[MonitorEvent]
    #: Highest `AccessLog.id` in this payload; the stream continues from here.
    cursor: int
    window: MonitorWindow


# --------------------------------------------------------------------------
# Device control
# --------------------------------------------------------------------------
class OpenDoorRequest(BaseModel):
    door_number: int = Field(1, ge=1, description="Door/output number")
    duration_seconds: int = Field(5, ge=0, le=600, description="0 = toggle")


class ControlResult(BaseModel):
    device_id: int
    action: str
    success: bool
    error: str | None = None


class DoorScheduleCreate(BaseModel):
    device_id: int
    door_number: int = 1
    day_of_week: int = Field(0, ge=0, le=7)
    start_time: str = Field("00:00", pattern=r"^\d{2}:\d{2}$")
    end_time: str = Field("23:59", pattern=r"^\d{2}:\d{2}$")
    device_timezone_id: int = 1
    normal_open: bool = False
    description: str | None = None


class DoorScheduleRead(ORMModel):
    id: int
    device_id: int
    door_number: int
    day_of_week: int
    start_time: str
    end_time: str
    device_timezone_id: int
    normal_open: bool


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
class DashboardRow(BaseModel):
    """A row shaped like the ZKAccess device toolbar."""

    device_name: str
    serial_number: str | None = None
    ip_address: str
    personnel_count: int | None = None
    firmware_version: str | None = None
    status: str


class SyncSummary(BaseModel):
    job: str
    total_devices: int
    succeeded: int
    failed: int
    details: list[dict] = Field(default_factory=list)
