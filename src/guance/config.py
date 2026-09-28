"""Application paths and user preferences; no import-time filesystem effects."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any


def project_root() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parents[2]


def app_paths() -> dict[str, Path]:
    override=os.environ.get('GUANCE_DATA_DIR')
    data=Path(override) if override else Path(os.environ.get('LOCALAPPDATA',Path.home()/'AppData'/'Local'))/'Guance'
    root=project_root()
    return {'data_dir':data,'config_dir':data/'config','logs_dir':data/'logs',
            'cert_dir':data/'certs','runtime_dir':root/'runtime',
            'exports_dir':data/'exports','models_dir':data/'models','root_dir':root}


def ensure_app_paths(paths: dict[str, Path]) -> None:
    for name in ('data_dir','config_dir','logs_dir','cert_dir','exports_dir','models_dir'):
        paths[name].mkdir(parents=True,exist_ok=True)


def default_model_path(paths: dict[str, Path]) -> Path:
    override=os.environ.get('GUANCE_MODEL_PATH')
    if override:
        return Path(override)
    local=paths['models_dir']/'best.npz'
    if local.is_file():
        return local
    return paths['root_dir']/'data'/'profiles'/'tencent_daguandan'/'models'/'best.npz'


def load_preferences(paths: dict[str, Path]) -> dict[str, Any]:
    defaults={'theme':'system','floating':False,'model_path':'','inference_threads':2,
              'advice_timeout_ms':2000,'top_n':3}
    p=paths['config_dir']/'preferences.json'
    try:
        obj=json.loads(p.read_text('utf8'))
        if isinstance(obj,dict):
            defaults.update({k:v for k,v in obj.items()if k in defaults})
    except (OSError,ValueError):
        pass
    return defaults


def save_preferences(paths: dict[str, Path], preferences: dict[str,Any]) -> None:
    paths['config_dir'].mkdir(parents=True,exist_ok=True)
    p=paths['config_dir']/'preferences.json';tmp=p.with_suffix('.tmp')
    tmp.write_text(json.dumps(preferences,ensure_ascii=False,indent=2),encoding='utf8')
    tmp.replace(p)
