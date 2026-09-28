"""Prepare the bundled mitmproxy helper for a local Windows build.

This copies only the executable and its adjacent frozen-runtime directory. The
result is ignored by Git and is regenerated on the build machine.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import shutil


def main() -> int:
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,default=Path(r'C:\Program Files\mitmproxy\bin'));parser.add_argument('--destination',type=Path,default=Path('runtime/mitmproxy'));args=parser.parse_args()
    source=args.source.resolve();destination=args.destination.resolve()
    helper=source/'mitmdump.exe';internal=source/'_internal'
    if not helper.is_file() or not internal.is_dir():
        raise SystemExit(f'mitmproxy standalone runtime not found under {source}')
    destination.mkdir(parents=True,exist_ok=True)
    shutil.copy2(helper,destination/'mitmdump.exe')
    if (destination/'_internal').exists():shutil.rmtree(destination/'_internal')
    shutil.copytree(internal,destination/'_internal')
    shutil.copy2(Path('src/guance/infrastructure/capture/addon.py'),destination/'addon.py')
    print(f'prepared {destination}')
    return 0
if __name__=='__main__':raise SystemExit(main())
