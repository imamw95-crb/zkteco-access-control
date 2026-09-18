"""Locks the Windows installer for the push agent (`agent/installer/`).

The installer is the recommended way to put the agent on a Windows machine, and
almost nothing in it is exercised by the Python tests: it is Inno Setup Pascal
plus four payload scripts. A silent regression here (an environment variable the
agent reads but the installer never writes, a task that dies after three days, a
non-ASCII byte that PowerShell 5.1 reads as ANSI) would only appear on the
operator's PC, at the moment they try to push to a panel.

So these tests check the *contract* between three files that must agree:
`zk_push_agent.py` (what the agent reads), `zk_push_agent.iss` (what gets
installed) and the payload scripts (how it runs). No hardware, no admin, no
panel, no database.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
AGENT_ROOT = BACKEND_ROOT / "agent"
INSTALLER_ROOT = AGENT_ROOT / "installer"
ISS = INSTALLER_ROOT / "zk_push_agent.iss"
BUILD_SCRIPT = INSTALLER_ROOT / "build_installer.ps1"
README = INSTALLER_ROOT / "README.md"
PAYLOAD = INSTALLER_ROOT / "app"

PAYLOAD_SCRIPTS = ("run_agent.cmd", "install_task.ps1", "check_agent.ps1", "gen_token.ps1")

pytestmark = pytest.mark.skipif(not ISS.exists(), reason="installer agen tidak ada di checkout ini")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def cmd_code(text: str) -> str:
    """Baris .cmd tanpa komentar rem/:: - komentar boleh menyebut jebakannya."""
    return "\n".join(
        line for line in text.splitlines() if not line.strip().lower().startswith(("rem", "::"))
    )


def ps_code(text: str) -> str:
    """Skrip PowerShell tanpa blok <# ... #> dan baris komentar #."""
    without_block = re.sub(r"<#.*?#>", "", text, flags=re.S)
    return "\n".join(
        line for line in without_block.splitlines() if not line.lstrip().startswith("#")
    )


# --------------------------------------------------------------- the contract
def test_installer_sets_every_environment_variable_the_agent_reads():
    # Nama variabelnya hanya hidup di dua tempat: zk_push_agent.py (dibaca) dan
    # .iss (ditulis). Kalau salah satu pindah tanpa yang lain, agen berhenti
    # start dengan exit 2/3 dan itu baru terlihat di PC operator.
    agent = read(AGENT_ROOT / "zk_push_agent.py")
    wanted = set(re.findall(r'os\.environ\.get\(\s*"(ZK_[A-Z_]+)"', agent))
    assert len(wanted) >= 5, f"tidak menemukan env var agen: {sorted(wanted)}"

    written = set(re.findall(r"SetEnvValue\('(ZK_[A-Z_]+)'", read(ISS)))
    assert wanted <= written, f"installer tidak menulis: {sorted(wanted - written)}"


def test_installer_defaults_match_the_agent_defaults():
    agent = read(AGENT_ROOT / "zk_push_agent.py")
    iss = read(ISS)
    assert '"ZK_PUSH_AGENT_PORT", "8081"' in agent
    assert "DefaultPort = '8081'" in iss
    # 127.0.0.1 adalah default yang aman: agen bisa membuka pintu, jadi jangan
    # pernah terbuka ke LAN tanpa keputusan operator.
    assert '"ZK_PUSH_AGENT_HOST", "127.0.0.1"' in agent
    assert "SafeHost = '127.0.0.1'" in iss


def test_the_installer_ships_every_script_it_runs():
    shipped = set(re.findall(r'Source: "\{#PayloadDir\}\\([^"]+)"', read(ISS)))
    assert shipped == set(PAYLOAD_SCRIPTS), f"payload tidak cocok: {sorted(shipped)}"
    for name in PAYLOAD_SCRIPTS:
        assert (PAYLOAD / name).exists(), f"{name} hilang dari installer/app"


def test_the_task_points_at_the_bundled_interpreter_and_launcher():
    iss = read(ISS)
    # Agen harus jalan dari python.exe yang dibundel (32-bit), bukan Python
    # mesin - dan lewat launcher, supaya keluaran agen masuk agent.log.
    assert r"{app}\python\python.exe" in iss
    assert r"{app}\run_agent.cmd" in iss
    assert "install_task.ps1" in iss


# ------------------------------------------------------------ task scheduling
def test_the_task_is_boot_scoped_system_and_restarts_by_itself():
    text = read(PAYLOAD / "install_task.ps1")
    assert "-AtStartup" in text
    assert "New-ScheduledTaskPrincipal -UserId 'SYSTEM'" in text
    assert "-LogonType ServiceAccount" in text
    assert "-RestartCount $RestartCount" in text
    assert "-RestartInterval (New-TimeSpan -Minutes $RestartMinutes)" in text
    # Batas waktu default Task Scheduler adalah 3 hari: agen akan dibunuh diam-diam.
    assert "-ExecutionTimeLimit (New-TimeSpan -Seconds 0)" in text
    # Kalau tidak IgnoreNew, pengulangan restart menumpuk instance yang gagal bind.
    assert "-MultipleInstances IgnoreNew" in text


def test_task_registration_refuses_a_64_bit_python():
    # Kesalahan #1 di README agen: DLL 32-bit tidak bisa dimuat proses 64-bit.
    # Lebih baik gagal saat pemasangan daripada gagal saat push pertama.
    text = read(PAYLOAD / "install_task.ps1")
    assert "calcsize('P') * 8" in text
    assert "$bits -ne '32'" in text
    assert "exit 4" in text


# -------------------------------------------------------------- payload style
def test_payload_scripts_are_ascii_without_bom():
    # cmd.exe dan PowerShell 5.1 membaca berkas tanpa BOM sebagai ANSI.
    for path in [PAYLOAD / name for name in PAYLOAD_SCRIPTS] + [
        BUILD_SCRIPT,
        INSTALLER_ROOT / "smoke_test.ps1",
    ]:
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{path.name} punya BOM"
        try:
            raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise AssertionError(f"{path.name} mengandung karakter non-ASCII: {exc}") from exc


def test_the_launcher_avoids_the_cmd_parsing_traps():
    text = cmd_code(read(PAYLOAD / "run_agent.cmd"))
    assert "&&" not in text  # cmd tidak punya &&
    for line in text.splitlines():
        if line.strip().lower().startswith("echo "):
            assert "(" not in line and ")" not in line, f"tanda kurung di echo: {line}"
    # Tanpa pemutaran, agent.log tumbuh tanpa batas di PC yang hidup 24/7.
    assert "move /y" in text


def test_the_generated_token_is_cryptographic_and_copy_paste_safe():
    text = ps_code(read(PAYLOAD / "gen_token.ps1"))
    # Get-Random bukan CSPRNG; token ini setara kunci pintu.
    assert "RandomNumberGenerator" in text
    assert "Get-Random" not in text
    assert "0123456789abcdefghijklmnopqrstuvwxyz" in text


# ------------------------------------------------------------------- safety
def test_pascal_comments_do_not_nest_braces():
    # Komentar Pascal berakhir di } pertama, jadi menulis {app} di dalamnya
    # memutus komentar di tengah: sisanya dianggap kode dan ISCC mati dengan
    # "String error" / "Unknown identifier" di baris itu. Komentar blok di .iss
    # ini selalu dimulai di awal baris, jadi pindai sebagai blok.
    inside = False
    for number, line in enumerate(read(ISS).splitlines(), start=1):
        stripped = line.strip()
        if not inside:
            if stripped.startswith("{"):
                inside = True
            else:
                continue
        if stripped.count("{") > (1 if stripped.startswith("{") else 0):
            raise AssertionError(f"baris {number}: kurung buka kedua di dalam komentar: {stripped}")
        if "}" in stripped:
            assert stripped.endswith("}"), (
                f"baris {number}: kurung tutup di tengah komentar memutus komentar: {stripped}"
            )
            assert stripped.count("}") == 1, f"baris {number}: kurung tutup ganda: {stripped}"
            inside = False
    assert not inside, "komentar Pascal tidak pernah ditutup"
    # Komentar yang memuat {app} pernah benar-benar lolos ke .iss dan mematikan
    # kompilasi - pastikan pencariannya masuk akal pada berkas ini.
    assert "ExpandConstant('{app}" in read(ISS)


def test_early_wizard_code_does_not_expand_the_app_constant():
    # {app} belum punya nilai selama InitializeWizard (halaman folder belum
    # dibuat). Inno melempar runtime error dan installer mati sebelum halaman
    # pertama muncul - persis yang pernah terjadi di FindSdkFolder().
    iss = read(ISS)
    assert "ExpandConstant('{app}" in iss, "pola pencarian tidak cocok dengan berkas"

    def body(start: str, end: str) -> str:
        begin = iss.index(start)
        return iss[begin : iss.index(end, begin)]

    early = body("function FindSdkFolder()", "function MissingTransportDlls(")
    early += body("procedure InitializeWizard()", "function ShouldSkipPage(")
    assert "ExpandConstant('{app}" not in early, "{app} dipakai sebelum halaman folder dibuat"


def test_the_installer_never_writes_to_a_panel():
    # Installer hanya boleh membuktikan jalur HTTP-nya hidup (GET /health) dan
    # membaca satu panel secara baca-saja lewat check_setup.py --test.
    iss = read(ISS)
    for forbidden in ("tables/", "SetDeviceData", "DeleteDeviceData", "/control", "params="):
        assert forbidden not in iss, f"installer menyentuh jalur tulis panel: {forbidden}"

    check = read(PAYLOAD / "check_agent.ps1")
    assert "$AgentDir 'check_setup.py'" in check
    assert "'--test', $PanelIp" in check
    assert "/health" in check


def test_the_zkteco_sdk_is_not_redistributed():
    # Lisensi SDK ZKTeco tidak memungkinkan kita mengemas pl*.dll; operator harus
    # menunjuk foldernya sendiri dan installer menyalinnya dari mesin operator.
    iss = read(ISS)
    sources = re.findall(r'^Source: "([^"]+)"', iss, re.M)
    assert sources, "tidak menemukan [Files] Source di .iss"
    assert not any("plcommpro" in source or "pl*.dll" in source for source in sources)
    assert "CopyFolder(SdkFolder, SdkTarget)" in iss, "SDK tidak disalin dari mesin operator"
    # Yang boleh ikut dikemas hanya runtime Microsoft dan Python resmi.
    assert "vc_redist.x86.exe" in iss
    assert "embed-win32" not in iss  # payload Python ditunjuk lewat build script
    assert "{param:SDK|}" in iss


def test_the_health_check_forces_the_installed_configuration():
    # Shell yang menjalankan installer bisa punya ZK_* sendiri - `setx` menulis
    # level User, dan level User MENANG atas level Machine. Tanpa blok ini, agen
    # yang dinyalakan untuk pemeriksaan memakai port/DLL/token milik setup lama.
    # Terbukti: agen uji pernah start di port 8081 dengan DLL C:\PullSDK padahal
    # installer memasang port 8099 dan DLL di folder aplikasi, lalu laporan
    # menyalahkan payload yang sebenarnya sehat.
    text = read(PAYLOAD / "check_agent.ps1")
    for name in (
        "ZK_PULLSDK_DLL",
        "ZK_PUSH_AGENT_TOKEN",
        "ZK_PUSH_AGENT_HOST",
        "ZK_PUSH_AGENT_PORT",
    ):
        assert f"$env:{name} = $" in text, f"{name} tidak dipaksa ke env anak proses"
    assert "'User', 'Machine'" in text, "nilai ZK_* usang tidak dilaporkan"


def test_the_smoke_harness_clears_ambient_agent_env():
    # Kalau tidak, smoke test menguji konfigurasi milik shell developer.
    text = read(INSTALLER_ROOT / "smoke_test.ps1")
    assert "ZK_PULLSDK_DLL" in text
    assert 'Remove-Item "Env:$name"' in text


def test_no_dialog_can_block_a_silent_install():
    # Dengan /VERYSILENT tidak ada yang bisa menekan OK. MsgBox yang tidak dijaga
    # WizardSilent membuat pemasangan massal menggantung di setiap PC begitu
    # verdictnya bukan OK - terbukti saat smoke test: setup tidak pernah keluar.
    iss = read(ISS)
    assert "if (not WizardSilent) and (Copy(VerdictLine" in iss
    # Harness harus punya batas waktu, supaya hang terlihat sebagai kegagalan.
    smoke = read(INSTALLER_ROOT / "smoke_test.ps1")
    assert "AddSeconds(300)" in smoke
    assert "Stop-Smoke 'installer tidak keluar" in smoke


def test_the_installer_proves_itself_and_leaves_a_report():
    iss = read(ISS)
    check = read(PAYLOAD / "check_agent.ps1")
    assert "CHECK-REPORT.txt" in iss
    assert "check_agent.ps1" in iss
    assert "-Restart" in iss  # agen harus membaca env yang baru ditulis
    assert "VERDICT:" in check
    # Laporan harus ASCII: dibaca ulang Inno Setup dan dibuka di Notepad.
    assert "-Encoding Ascii" in check


# -------------------------------------------------------------------- build
def test_the_build_script_keeps_the_payload_import_path_fix():
    # Interpreter embeddable TIDAK menambahkan folder skrip ke sys.path - berkas
    # ._pth menggantikannya. check_setup.py mengimpor zk_push_agent dari folder
    # aplikasi, jadi ".." wajib ada di ._pth; tanpa itu impornya gagal.
    text = read(BUILD_SCRIPT)
    assert "*._pth" in text
    assert "'..'" in text
    # Dan kegagalan itu harus ketahuan saat build, bukan di PC operator.
    assert "check_setup.py" in text
    assert "sys.path" in text


def test_the_build_script_verifies_bytes_and_syntax_before_compiling():
    text = read(BUILD_SCRIPT)
    assert "calcsize('P') * 8" in text  # bukti payload 32-bit
    assert "ParseFile" in text  # sintaks PowerShell payload
    assert "0xEF" in text  # deteksi BOM
    assert "Stop-Build" in text


def test_the_smoke_harness_exists_and_patches_only_three_things():
    # Tanpa hak admin, smoke test adalah satu-satunya cara kode [Code] Inno
    # benar-benar dijalankan. Tambalannya harus tetap sempit dan terlihat.
    text = read(INSTALLER_ROOT / "smoke_test.ps1")
    assert "PrivilegesRequired=admin" in text and "PrivilegesRequired=lowest" in text
    assert "if NeedVcRedist() then" in text and "if False then" in text
    assert "ZkPushAgent-Smoke" in text
    assert "GET /health" in text and "unins000.exe" in text


def test_the_build_verification_runs_with_a_clean_environment():
    # Shell developer bisa saja sedang menjalankan agen manual. Kalau variabel
    # ZK_* itu ikut terbaca, pemeriksaan payload melaporkan "DLL ditemukan" /
    # token "terisi" dari mesin dev - dan build lulus tanpa membuktikan apa pun.
    text = read(BUILD_SCRIPT)
    assert "$agentEnvNames" in text
    assert "Env:$name" in text
    assert "BELUM diisi" in text


def test_the_build_script_is_reproducible_from_a_fresh_checkout():
    text = read(BUILD_SCRIPT)
    # python.org + aka.ms: satu-satunya dua unduhan, keduanya boleh di-cache.
    assert "python.org/ftp/python" in text
    assert "aka.ms/vs/17/release/vc_redist.x86.exe" in text
    assert "embed-win32.zip" in text


# --------------------------------------------------------------- documentation
def test_every_silent_install_parameter_is_documented():
    params = set(re.findall(r"\{param:([A-Z]+)\|", read(ISS)))
    assert len(params) >= 5, f"parameter senyap tidak terbaca: {sorted(params)}"
    doc = read(README).lower()
    for name in sorted(params):
        assert f"/{name.lower()}=" in doc, f"parameter /{name} tidak didokumentasikan di README"


def test_the_docs_send_operators_to_the_installer():
    for path in (AGENT_ROOT / "README.md", BACKEND_ROOT / "docs" / "INSTALL.md"):
        if not path.exists():
            continue
        assert "ZkPushAgent-Setup" in read(path), f"{path.name} belum menyebut installer"
