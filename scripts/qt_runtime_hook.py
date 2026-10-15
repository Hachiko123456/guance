"""Install deterministic bundled Qt/shiboken/CRT DLL search paths on Windows."""
import ctypes
import os
from pathlib import Path
import sys

base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
paths = [base, base / "PySide6", base / "shiboken6"]
_dll_dirs = []
for path in paths:
    if path.is_dir():
        try:
            _dll_dirs.append(os.add_dll_directory(str(path)))
        except (AttributeError, OSError):
            pass
os.environ["PATH"] = os.pathsep.join(str(p) for p in paths if p.is_dir()) + os.pathsep + os.environ.get("PATH", "")

# Pin the controlled VC runtime and binding support DLLs before any Qt Python
# extension imports.  This avoids AddDllDirectory's intentionally unspecified
# directory ordering when multiple wheels contain same-named CRT files.
if os.name == "nt":
    for name in (
        "VCRUNTIME140.dll", "VCRUNTIME140_1.dll", "MSVCP140.dll",
        "MSVCP140_1.dll", "MSVCP140_2.dll", "concrt140.dll",
    ):
        target = base / name
        if target.is_file():
            ctypes.WinDLL(str(target))
    for target in (
        base / "shiboken6" / "shiboken6.abi3.dll",
        base / "PySide6" / "Qt6Core.dll",
        base / "PySide6" / "pyside6.abi3.dll",
    ):
        if target.is_file():
            ctypes.WinDLL(str(target))
