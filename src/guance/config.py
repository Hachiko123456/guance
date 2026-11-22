"""Application paths and user preferences; no import-time filesystem effects."""
from __future__ import annotations

import json
import os
import tempfile
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
    models=data/'models'
    return {'data_dir':data,'config_dir':data/'config','logs_dir':data/'logs',
            # mitmproxy creates its capture CA in the same confdir used by the runtime.
            # Keep certificate discovery and helper generation on one path.
            'cert_dir':data/'config'/'mitmproxy','runtime_dir':root/'runtime',
            'exports_dir':data/'exports','models_dir':models,
            'catalog_dir':models/'catalog','active_model_file':models/'active_model.json',
            'root_dir':root}


def ensure_app_paths(paths: dict[str, Path]) -> None:
    for name in ('data_dir','config_dir','logs_dir','cert_dir','exports_dir','models_dir'):
        paths[name].mkdir(parents=True,exist_ok=True)
    catalog=paths.get('catalog_dir')
    if catalog is not None:
        catalog.mkdir(parents=True,exist_ok=True)


def default_model_source(paths: dict[str, Path], preferences: dict[str, Any] | None = None) -> Path:
    """Select an explicit source first; never fall back after a selected source fails."""
    override = os.environ.get("GUANCE_MODEL_BUNDLE") or os.environ.get("GUANCE_MODEL_PATH")
    if override:
        return Path(override)
    selected = (preferences or {}).get("model_path")
    if isinstance(selected, str) and selected.strip():
        return Path(selected)
    from guance.infrastructure.model.defaults import initial_model_source
    return initial_model_source(paths)


def default_model_path(paths: dict[str, Path]) -> Path:
    """Compatibility alias. New composition uses a model source, not a weight format."""
    return default_model_source(paths)


def load_preferences(paths: dict[str, Path]) -> dict[str, Any]:
    defaults={'theme':'system','floating':False,'floating_layout':{},'split_layout':'side','model_path':'',
              'model_selection_failed':False,'requested_model_path':'','inference_threads':2,
              'model_device':'auto',
              'advice_timeout_ms':2000,'top_n':3,'debug_capture_enabled':False}
    p=paths['config_dir']/'preferences.json'
    try:
        obj=json.loads(p.read_text('utf8'))
        if isinstance(obj,dict):
            defaults.update({k:v for k,v in obj.items()if k in defaults})
    except (OSError,ValueError):
        pass
    return defaults


def save_preferences(paths: dict[str, Path], preferences: dict[str, Any]) -> None:
    """Replace only a complete JSON document; never touch another writer's temp."""
    text = json.dumps(preferences, ensure_ascii=False, indent=2)
    directory = paths['config_dir']
    directory.mkdir(parents=True, exist_ok=True)
    owned: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory,
                                         prefix='.preferences-', suffix='.tmp', delete=False) as stream:
            owned = Path(stream.name)
            stream.write(text)
            stream.flush()
        owned.replace(directory / 'preferences.json')
    finally:
        if owned is not None:
            try:
                owned.unlink(missing_ok=True)
            except OSError:
                # Do not hide the persistence failure or delete any other file.
                pass
