"""Build a portable Windows onedir package after runtime preparation."""
from __future__ import annotations
import argparse
from pathlib import Path
import shutil
import subprocess
import sys


def main() -> int:
    parser=argparse.ArgumentParser();parser.add_argument('--clean',action='store_true');args=parser.parse_args()
    root=Path(__file__).resolve().parents[1];dist=root/'release';work=root/'build'/'pyinstaller'
    if args.clean:
        for p in (dist,work):
            if p.exists():shutil.rmtree(p)
    subprocess.run([sys.executable,'scripts/prepare_runtime.py'],cwd=root,check=True)
    runtime=root/'runtime'/'mitmproxy';qml=root/'src'/'guance'/'presentation'/'qml';model=root/'data'/'profiles'/'tencent_daguandan'/'models'/'best.npz'
    if not model.is_file():raise SystemExit('best.npz is missing; copy the trained model before building')
    cmd=[sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onedir','--windowed','--name','Guance',
         '--paths','src','--distpath',str(dist),'--workpath',str(work),
         '--add-data',f'{qml};guance/presentation/qml',
         '--add-data',f'{runtime};runtime/mitmproxy',
         '--add-data',f'{model};data/profiles/tencent_daguandan/models',
         '--collect-all','PySide6','--collect-all','shiboken6','--runtime-hook','scripts/qt_runtime_hook.py',
         'src/guance/__main__.py']
    subprocess.run(cmd,cwd=root,check=True)
    # Some shared Python environments bring a Poppler ICU DLL into the
    # PyInstaller tree. Qt on Windows should use the OS ICU implementation;
    # the Poppler DLL exports a different ABI and breaks QtCore loading.
    internal=dist/'Guance'/'_internal'
    for name in ('icuuc.dll','icudt78.dll'):
        candidate=internal/name
        if candidate.exists(): candidate.unlink()
    print(f'build complete: {dist/"Guance"}')
    return 0
if __name__=='__main__':raise SystemExit(main())
