"""Locks the Windows dev launcher (the "tombol" that keeps the backend running).

The launcher lives outside the backend package: `jalankan-backend.bat` /
`hentikan-backend.bat` / `status-backend.bat` at the workspace root call
`deploy/serve_backend.ps1`. None of that is exercised by the app tests, so a
silent break (wrong venv, a command-line regression that makes the supervisor
hang, a non-ASCII byte that PowerShell 5.1 reads as ANSI) would only show up
when somebody presses the button. These tests fail instead.

Skipped when the backend folder is deployed without the workspace root around
it (the deployment bundle contains only the backend).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = Path(__file__).resolve().parents[1]

SCRIPT = BACKEND_ROOT / "deploy" / "serve_backend.ps1"
SHORTCUTS = ("jalankan-backend.bat", "hentikan-backend.bat", "status-backend.bat")
TASKS_JSON = WORKSPACE_ROOT / ".vscode" / "tasks.json"

pytestmark = pytest.mark.skipif(
    not (WORKSPACE_ROOT / SHORTCUTS[0]).exists(),
    reason="workspace root launcher tidak ada (backend dijalankan terpisah)",
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_every_shortcut_calls_the_supervisor_script():
    for name in SHORTCUTS:
        shortcut = WORKSPACE_ROOT / name
        assert shortcut.exists(), f"{name} hilang"
        text = read(shortcut)
        assert r"zkteco-backend\deploy\serve_backend.ps1" in text
        # `goto` + label, bukan blok `if (...)` berkurung: tanda kurung di dalam
        # teks echo membuat cmd salah parse ("... was unexpected at this time").
        assert "goto :tidak_ada" in text
        assert ":tidak_ada" in text


def test_shortcuts_and_script_are_ascii():
    # cmd.exe dan PowerShell 5.1 membaca berkas tanpa BOM sebagai ANSI; karakter
    # non-ASCII akan tampil rusak atau menggagalkan parsing.
    for name in SHORTCUTS:
        assert read(WORKSPACE_ROOT / name).isascii(), f"{name} bukan ASCII"
    assert read(SCRIPT).isascii(), "serve_backend.ps1 bukan ASCII"


def test_supervisor_waits_on_python_and_never_on_a_shell_wrapper():
    script = read(SCRIPT)
    # Menunggu pembungkus cmd.exe bisa menggantung selamanya setelah anaknya
    # dibunuh, sehingga restart tidak pernah terjadi. Yang ditunggu harus proses
    # python-nya, dan taskkill harus senyap (kalau tidak, EA=Stop mematikan
    # skrip begitu ada PID yang sudah tidak ada).
    assert "ComSpec" not in script
    assert "WaitForExit()" in script
    assert "function Stop-ProcessTree" in script
    assert "function Get-StrayUvicornProcesses" in script


def test_supervisor_uses_the_project_venv_and_does_not_run_the_scheduler():
    script = read(SCRIPT)
    assert r".venv\Scripts\python.exe" in script
    assert "$env:SCHEDULER_ENABLED = 'false'" in script
    assert "--reload-include" in script
    assert "app.main:app" in script


def test_launcher_reads_persisted_agent_settings_when_the_shell_env_is_stale():
    # Shell task VS Code mewarisi environment yang sudah tersnapot saat VS Code
    # dinyalakan, sedangkan setx dan installer agen menulis ke registry. Tanpa
    # cadangan ini launcher DIAM-DIAM memakai alamat agen yang lama, sehingga
    # push menuju agen yang salah tanpa satu pun pesan (terjadi 2026-09-18
    # waktu backend dipindah ke agen di PC lain).
    script = read(SCRIPT)
    assert "function Get-PersistedSetting" in script
    assert "foreach ($scope in 'User', 'Machine')" in script
    assert "[Environment]::GetEnvironmentVariable($Name, $scope)" in script

    body = script.split("function Set-BackendEnvironment", 1)[1].split("\nfunction ", 1)[0]
    # Urutannya: parameter, lalu registry, baru environment proses.
    assert "$storedUrl = Get-PersistedSetting 'PUSH_AGENT_URL'" in body
    assert "$storedToken = Get-PersistedSetting 'ZK_PUSH_AGENT_TOKEN'" in body
    assert "$storedToken = Get-PersistedSetting 'PUSH_AGENT_TOKEN'" in body
    assert body.index("$PushAgentUrl) {") < body.index("$storedUrl) {")
    assert body.index("$storedUrl) {") < body.index("$envUrl) {")
    # ZK_PUSH_AGENT_TOKEN menang atas PUSH_AGENT_TOKEN: 2026-09-18 nilai lama
    # 36 karakter di PUSH_AGENT_TOKEN membuat SEMUA panel menjawab
    # "Authorization: Bearer <token> wajib" walau ZK_* sudah benar.
    assert body.index("Get-PersistedSetting 'ZK_PUSH_AGENT_TOKEN'") < body.index(
        "Get-PersistedSetting 'PUSH_AGENT_TOKEN'"
    )
    # Nilai yang diabaikan harus kelihatan: itu satu-satunya cara operator tahu
    # agen mana yang dipakai (dan kapan push menuju agen lain).
    assert "push agent: $env:PUSH_AGENT_URL ($script:PushAgentUrlSource)" in script
    assert "token agen: $script:PushAgentTokenSource" in script
    assert "'DEFAULT - PUSH_AGENT_URL tidak diset'" in script
    assert "PUSH_AGENT_URL proses ($envUrl) diabaikan" in script
    assert "'KOSONG - push akan menjawab 503'" in script


def test_launcher_reports_a_token_the_agent_rejects():
    # 401 dari agen hanya muncul sebagai "push gagal" di dashboard kalau token
    # salah; memeriksanya saat start membuat penyebabnya terbaca lebih dulu.
    script = read(SCRIPT)
    assert "function Test-PushAgentToken" in script
    assert "'DITOLAK (401) - token backend tidak sama dengan token agen'" in script
    assert 'Write-Note "agen: $agentCheck" $agentColour' in script
    # Pemeriksaan ini tidak boleh menghentikan backend; agen bisa dinyalakan
    # setelah backend jalan.
    check = script.split("function Test-PushAgentToken", 1)[1].split("\nfunction ", 1)[0]
    assert "try {" in check and "catch" in check
    assert "throw" not in check


def test_stray_cleanup_never_touches_the_push_agent():
    # Agent push memakai python 32-bit dan bisa jalan bersamaan; prosesnya tidak
    # boleh ikut dibunuh oleh pembersihan proses yatim.
    script = read(SCRIPT)
    assert "zk_push_agent" in script
    stray = script.split("function Get-StrayUvicornProcesses", 1)[1]
    assert "*zk_push_agent*" in stray.split("function ")[0]


def test_vscode_tasks_offer_start_status_and_stop():
    assert TASKS_JSON.exists(), "tasks.json hilang"
    # Komentar // tidak boleh kembali: berkas ini dibaca sebagai JSON.
    raw = read(TASKS_JSON)
    assert "//" not in raw, "tasks.json harus JSON murni, bukan JSONC"
    tasks = json.loads(raw)["tasks"]
    labels = {task["label"] for task in tasks}
    assert any("Jalankan backend" in label for label in labels)
    assert any("Hentikan backend" in label for label in labels)
    assert any("Status backend" in label for label in labels)

    for task in tasks:
        assert "${workspaceFolder}" in " ".join(task["args"])

    start = next(task for task in tasks if "Jalankan backend" in task["label"])
    assert start["args"][-1] == "start"
    assert start["group"] == {"kind": "build", "isDefault": True}
