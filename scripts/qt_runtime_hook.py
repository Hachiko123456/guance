"""Install deterministic bundled Qt/shiboken/CRT DLL search paths on Windows."""
import ctypes
import os
from pathlib import Path
import sys

# A frozen windowed executable defaults stdio to the ANSI code page. The model
# worker JSONL contract is UTF-8 (source mode starts the worker with ``-X utf8``),
# so enforce the same encoding here before any application import. Without this,
# non-ASCII candidate labels are emitted as GBK and the strict UTF-8 client
# rejects the result.
#
# stdin needs the same treatment: the parent writes UTF-8 JSONL, and a frozen
# worker that decodes it with the ANSI code page turns a valid request (for
# example a load source containing a Chinese directory name) into
# PROTOCOL_INVALID.  A plain (non-worker) GUI launch may expose an empty or
# ``None`` stdin, so both cases must stay harmless.
for _stream in (sys.stdout, sys.stderr, sys.stdin):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="strict")
        except (LookupError, OSError, ValueError):
            pass
del _stream

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

# The optional frozen MARVEL GPU profile vendors the already-accepted Torch/CUDA
# runtime under ``marvel_runtime/site-packages``.  Append it (never prepend) so
# bundled application/Qt modules keep priority, and register Torch's DLL
# directory before the worker imports Torch lazily.  The no-capture/FableDan
# profile does not contain this directory and is unaffected.
_marvel_site = base / "marvel_runtime" / "site-packages"
if _marvel_site.is_dir():
    if str(_marvel_site) not in sys.path:
        sys.path.append(str(_marvel_site))
    _torch_lib = _marvel_site / "torch" / "lib"
    if _torch_lib.is_dir():
        try:
            _dll_dirs.append(os.add_dll_directory(str(_torch_lib)))
        except (AttributeError, OSError):
            pass
        os.environ["PATH"] = str(_torch_lib) + os.pathsep + os.environ.get("PATH", "")

# The optional frozen DanKS CPU profile vendors its Torch runtime under
# ``danks_runtime/site-packages`` with the same append-never-prepend and DLL
# registration rules; profiles without this directory are unaffected.
_danks_site = base / "danks_runtime" / "site-packages"
if _danks_site.is_dir():
    if str(_danks_site) not in sys.path:
        sys.path.append(str(_danks_site))
    _torch_lib = _danks_site / "torch" / "lib"
    if _torch_lib.is_dir():
        try:
            _dll_dirs.append(os.add_dll_directory(str(_torch_lib)))
        except (AttributeError, OSError):
            pass
        os.environ["PATH"] = str(_torch_lib) + os.pathsep + os.environ.get("PATH", "")

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
