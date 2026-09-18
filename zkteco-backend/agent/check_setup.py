"""Check whether this machine can run the push agent.

Run it with the 32-bit interpreter you intend to use:

    C:\\Python313-32\\python.exe check_setup.py
    C:\\Python313-32\\python.exe check_setup.py --test 10.100.1.12

It reports what is ready and what is missing, and — most importantly — it tells
you straight away if the interpreter is 64-bit, because that is the one mistake
that cannot be worked around: a 64-bit process cannot load a 32-bit DLL.

`--test <ip>` goes further and actually opens a session with a real panel. That
is the only way to catch a DLL that loads but cannot find its transport plugins,
which otherwise shows up much later as `PullLastError=-201` on the first push.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
from pathlib import Path

from zk_push_agent import prepare_sdk_folder

OK = "  OK   "
WARN = "  !!!  "
BAD = "  X    "

DLL_NAME = "plcommpro.dll"
#: Where the Pull SDK usually ends up after a manual install or `pyzkaccess setup`.
SEARCH_HINTS = (
    Path(r"C:\PullSDK"),
    Path(r"C:\Program Files (x86)\ZKTeco"),
    Path(r"C:\Program Files (x86)\ZKAccess"),
    Path(r"C:\ZKAccess"),
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs",
    Path(sys.prefix),
    Path(__file__).resolve().parent,
    Path.cwd(),
)


def _find_dll() -> Path | None:
    declared = os.environ.get("ZK_PULLSDK_DLL", "").strip()
    if declared:
        path = Path(declared)
        return path if path.is_file() else None

    for root in SEARCH_HINTS:
        if not root or not root.exists():
            continue
        try:
            for candidate in root.rglob(DLL_NAME):
                return candidate
        except OSError:
            continue
    return None


def _test_panel(dll: Path, ip: str) -> bool:
    """Open a real session and read a device parameter. The real proof."""
    lib = ctypes.WinDLL(str(prepare_sdk_folder(str(dll))))
    lib.Connect.restype = ctypes.c_void_p
    conn = f"protocol=TCP,ipaddress={ip},port=4370,timeout=4000,passwd=".encode()
    handle = lib.Connect(conn)
    if not handle:
        print(f"{BAD} tidak bisa konek ke {ip} (PullLastError={lib.PullLastError()})")
        print(
            "         Kalau kodenya -201, DLL transport (pltcpcomm.dll dll.) tidak\n"
            "         ditemukan. Simpan SEMUA pl*.dll di SATU folder; agen memindah\n"
            "         working directory ke sana otomatis (prepare_sdk_folder)."
        )
        return False

    buf = ctypes.create_string_buffer(512)
    lib.GetDeviceParam(handle, buf, 512, b"~SerialNumber,FirmVer,LockCount")
    lib.Disconnect(handle)
    print(f"{OK} konek ke {ip}: {buf.value.decode(errors='ignore')}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", metavar="IP", help="Buka sesi sungguhan ke panel ini")
    args = parser.parse_args()

    bits = 64 if sys.maxsize > 2**32 else 32
    print("=== Python ===")
    marker = OK if bits == 32 else BAD
    print(f"{marker} Python {sys.version.split()[0]} ({bits}-bit)")
    print(f"      {sys.executable}")
    if bits != 32:
        print(
            f"{WARN} Agen TIDAK akan bisa memuat {DLL_NAME}. Install Python 32-bit ke\n"
            "         folder TERPISAH (jangan menimpa Python 64-bit yang sudah ada),\n"
            "         misalnya C:\\Python313-32, lalu jalankan script ini dengan\n"
            "         interpreter itu."
        )

    print("\n=== Pull SDK DLL ===")
    dll = _find_dll()
    if dll is None:
        print(f"{BAD} {DLL_NAME} tidak ditemukan.")
        print("         Set ZK_PULLSDK_DLL ke lokasi lengkapnya, contoh:")
        print(r"         setx ZK_PULLSDK_DLL C:\PullSDK\plcommpro.dll")
    else:
        print(f"{OK} ditemukan: {dll}")
        if bits == 32:
            try:
                lib = ctypes.WinDLL(str(dll))
            except OSError as exc:
                print(f"{BAD} gagal dimuat: {exc}")
                print(
                    "         Sering karena Visual C++ Redistributable 2015-2022 (x86) belum ada."
                )
            else:
                # Prove it is really the SDK and not a same-named unrelated file.
                for symbol in ("Connect", "SetDeviceData", "DeleteDeviceData", "GetDeviceData"):
                    marker = OK if hasattr(lib, symbol) else BAD
                    print(f"{marker} export {symbol}: {hasattr(lib, symbol)}")

    if args.test:
        print(f"\n=== Sesi sungguhan ke {args.test} ===")
        if dll is None or bits != 32:
            print(f"{BAD} dilewati: DLL belum ada atau Python bukan 32-bit")
        else:
            _test_panel(dll, args.test)

    print("\n=== Konfigurasi agen ===")
    token = os.environ.get("ZK_PUSH_AGENT_TOKEN", "")
    print(
        f"{OK if token else BAD} ZK_PUSH_AGENT_TOKEN {'terisi' if token else 'BELUM diisi'}"
        f"{f' ({len(token)} karakter)' if token else ''}"
    )
    print(f"{OK} ZK_PUSH_AGENT_PORT = {os.environ.get('ZK_PUSH_AGENT_PORT', '8081 (default)')}")
    print(
        f"{OK} ZK_PUSH_AGENT_HOST = {os.environ.get('ZK_PUSH_AGENT_HOST', '127.0.0.1 (default)')}"
    )

    print("\n=== Verdict ===")
    if bits == 32 and dll is not None and token:
        print(f"{OK} Siap. Jalankan:  python zk_push_agent.py")
        return 0
    print(f"{WARN} Belum siap. Beresi bagian yang ditandai di atas.")
    return 1


if __name__ == "__main__":
    if sys.platform != "win32":
        print("Script ini untuk Windows.")
        raise SystemExit(2)
    raise SystemExit(main())
