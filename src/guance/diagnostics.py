"""Bounded structured logs and explicit local support exports."""
from __future__ import annotations

from collections import deque
from datetime import datetime,timezone
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import platform
import re
from threading import RLock
from typing import Any
import zipfile

SENSITIVE=re.compile(r'(?i)(authorization|cookie|token|secret|password|private.?key|openid|unionid|account.?id|user.?id|raw.?payload|raw.?body|cert.?pem)')
URL_QUERY=re.compile(r'(https?://[^\s?]+)\?[^\s]+')
PEM=re.compile(r'-----BEGIN[^-]*PRIVATE KEY-----.*?-----END[^-]*PRIVATE KEY-----',re.S)


def redact(value: Any, key: str='') -> Any:
    if SENSITIVE.search(key):
        return '<redacted>'
    if isinstance(value,dict):
        return {str(k):redact(v,str(k))for k,v in list(value.items())[:100]}
    if isinstance(value,(list,tuple)):
        return [redact(v)for v in value[:100]]
    if isinstance(value,bytes):
        return f'<binary {len(value)} bytes>'
    if isinstance(value,Path):
        return value.name
    if isinstance(value,str):
        value=PEM.sub('<private key redacted>',value)
        value=URL_QUERY.sub(r'\1?<redacted>',value)
        value=re.sub(r'(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+',r'\1<redacted>',value)
        return value[:1600]
    if isinstance(value,(int,float,bool))or value is None:
        return value
    return type(value).__name__


class DiagnosticLog:
    def __init__(self, directory: Path, *, max_bytes: int=8*1024*1024, backups: int=4):
        directory.mkdir(parents=True,exist_ok=True)
        self.directory=directory
        self.recent:deque[dict[str,Any]]=deque(maxlen=300)
        self._lock=RLock()
        self._logger=logging.getLogger(f'guance.{id(self)}')
        self._logger.setLevel(logging.DEBUG);self._logger.propagate=False
        handler=RotatingFileHandler(directory/'guance.jsonl',maxBytes=max_bytes,backupCount=backups,encoding='utf8')
        handler.setFormatter(logging.Formatter('%(message)s'))
        self._logger.addHandler(handler)

    def write(self, event: str, *, level: str='INFO', message: str='', **fields: Any) -> dict[str,Any]:
        item=redact({'time':datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                     'level':level,'event':event,'message':message,**fields})
        with self._lock:
            self.recent.append(item)
            try:
                self._logger.log(getattr(logging,level.upper(),logging.INFO),json.dumps(item,ensure_ascii=False))
            except OSError:
                # Retain the in-memory event even when writing diagnostics fails.
                pass
        return item

    def export(self, destination: Path, *, status: dict[str,Any]|None=None) -> Path:
        destination.parent.mkdir(parents=True,exist_ok=True)
        with self._lock, zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED)as z:
            z.writestr('manifest.json',json.dumps(redact({'schema':'guance-diagnostics-v1','platform':platform.platform(),
                'python':platform.python_version(),'status':status or {},'privacy':'No raw capture, model weights or certificate key.'}),ensure_ascii=False,indent=2))
            # Only re-serialized structured records; never zip an arbitrary user directory.
            z.writestr('recent.jsonl',''.join(json.dumps(redact(x),ensure_ascii=False)+'\n'for x in self.recent))
        return destination

    def close(self) -> None:
        for handler in list(self._logger.handlers):
            handler.flush();handler.close();self._logger.removeHandler(handler)
