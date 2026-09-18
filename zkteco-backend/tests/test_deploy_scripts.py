"""Mengunci skrip deploy Windows -> server (192.168.0.27).

`deploy/push_to_server.ps1` dan `deploy/server_*.sh` tidak dijalankan oleh tes
aplikasi mana pun, padahal kesalahan di situ mendarat langsung di host produksi
(mis. `.env` tertimpa, database server terhapus, atau PowerShell 5.1 gagal
membaca berkas karena non-ASCII). Tes ini gagal lebih dulu.
"""

from __future__ import annotations

from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEPLOY = BACKEND_ROOT / "deploy"

PUSH_PS1 = DEPLOY / "push_to_server.ps1"
DEPLOY_SH = DEPLOY / "server_deploy.sh"
STATUS_SH = DEPLOY / "server_status.sh"
SETUP_ENV_SH = DEPLOY / "server_setup_env.sh"
COMPOSE = BACKEND_ROOT / "docker-compose.yml"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def executable_lines(text: str) -> list[str]:
    """Baris yang benar-benar dijalankan (bukan komentar atau teks `echo`)."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("echo"):
            continue
        out.append(line)
    return out


def test_deploy_helpers_exist():
    for path in (PUSH_PS1, DEPLOY_SH, STATUS_SH, SETUP_ENV_SH):
        assert path.is_file(), f"{path.name} hilang"


def test_push_script_is_ascii_without_bom():
    # PowerShell 5.1 membaca berkas tanpa BOM sebagai ANSI; karakter non-ASCII
    # tampil rusak, dan BOM membuat `powershell -File` gagal parse argumen.
    data = PUSH_PS1.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf"), "push_to_server.ps1 punya BOM"
    assert data.isascii(), "push_to_server.ps1 bukan ASCII"


def test_push_script_never_ships_the_server_secrets_or_databases():
    text = read(PUSH_PS1)
    # Tanpa dua baris ini, `-czf` dari folder backend akan membawa .env (sandi
    # Postgres + token agen) dan live_check.db dari laptop ke server.
    assert "--exclude=.env" in text
    assert "--exclude=*.db" in text


def test_push_script_only_uses_powershell_5_1_syntax():
    text = read(PUSH_PS1)
    # `&&` tidak ada di PowerShell 5.1 (host ini), hanya di 7+.
    assert "&&" not in text


def test_push_script_uploads_then_runs_the_server_deploy_script():
    text = read(PUSH_PS1)
    assert "'tar'" in text and "'scp'" in text and "'ssh'" in text
    assert "zkteco-prod" in text
    assert "/home/sentral/zkteco-backend" in text
    assert "bash deploy/server_deploy.sh" in text
    # Skrip server harus ikut dikirim, kalau tidak versi lama yang jalan.
    for helper in ("deploy/server_deploy.sh", "deploy/server_status.sh"):
        assert helper in text


def test_push_script_keeps_the_remote_env_and_data():
    text = read(PUSH_PS1)
    # Yang boleh dihapus sebelum ekstraksi hanya tiga direktori kode.
    assert "rm -rf app migrations scripts" in text
    for keep in (".env", "live_check.db"):
        assert f"rm -rf {keep}" not in text


@pytest.mark.parametrize("name", ["server_deploy.sh", "server_setup_env.sh", "server_status.sh"])
def test_shell_scripts_have_no_carriage_returns(name: str):
    """`\\r` di akhir baris membuat bash membaca `set -eu\\r` sebagai nama perintah."""
    assert b"\r" not in (DEPLOY / name).read_bytes(), f"{name} punya CR"


def test_server_deploy_leaves_the_worker_off():
    """Panel C3 hanya menerima satu koneksi; dua penjadwal saling menendang."""
    script = read(DEPLOY_SH)
    running = [line for line in executable_lines(script) if "docker compose up" in line]
    assert running, "tidak ada perintah `docker compose up` di server_deploy.sh"
    assert any("db backend" in line for line in running)
    assert not any("worker" in line for line in running), "server_deploy.sh menyalakan worker"


def test_server_deploy_guards_the_data_copy():
    script = read(DEPLOY_SH)
    # Hanya boleh menimpa kalau target belum punya data sungguhan, kecuali
    # pengguna memintanya eksplisit.
    assert "sync_runs" in script  # ukuran "data sungguhan"
    assert "--force-copy" in script
    assert "nyata" in script


def test_setup_env_never_writes_an_empty_token_line():
    """Baris `PUSH_AGENT_TOKEN=` yang kosong + token asli = dua baris, membingungkan."""
    script = read(SETUP_ENV_SH)
    assert "PUSH_AGENT_URL=http://10.100.1.100:8081" in script
    assert "PUSH_AGENT_TOKEN=" not in script


def test_status_script_masks_secrets():
    script = read(STATUS_SH)
    assert "PASSWORD" in script and "TOKEN" in script
    # Jangan pernah mencetak nilai apa adanya untuk kunci rahasia.
    assert "disembunyikan" in script
    # DATABASE_URL memuat sandi di dalam URL -> harus disaring juga.
    assert "***@" in script


def test_compose_loads_env_and_pins_its_own_project_name():
    text = read(COMPOSE)
    # Tanpa `env_file`, container tidak pernah melihat PUSH_AGENT_URL/TOKEN dan
    # setiap push menjawab 503.
    assert text.count("env_file:") == 2, "backend dan worker harus memuat .env"
    # Host itu sudah menjalankan project compose lain (network 172.18/172.19).
    assert "name: zkteco" in text
    assert '"8000:8000"' in text
