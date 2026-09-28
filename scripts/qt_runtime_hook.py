"""Ensure bundled Qt and shiboken DLL directories are discoverable on Windows."""
import os
from pathlib import Path
import sys

base=Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
paths=[base / "PySide6", base / "shiboken6", base]
_dll_dirs=[]
for path in paths:
    if path.is_dir():
        try: _dll_dirs.append(os.add_dll_directory(str(path)))
        except (AttributeError, OSError): pass
os.environ["PATH"] = os.pathsep.join(str(p) for p in paths if p.is_dir()) + os.pathsep + os.environ.get("PATH", "")
