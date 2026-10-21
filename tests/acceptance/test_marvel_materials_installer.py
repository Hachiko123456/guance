"""Phase 2 repair-2: trusted-material authentication + bounded-read gates.

Covers the required negative matrix:
  A. internally-consistent but untrusted archive (pin mismatch) is rejected
     before the probe; no sentinel, no target/staging, snapshot unchanged;
  B. self-consistent top-level shadow modules are rejected before execution;
  C. correct materials with any wrong expected pin are rejected before probe;
  D. zip size/bomb bounds (declared size, member/total limits, ratio,
     bounded expected+1 read) never cause large reads or writes;
  E. real frozen materials with explicit trusted pins reproduce the canonical
     runtime identity, probe positivity and 189-entry SHA256SUMS.
Every failure asserts: target absent, no staging leftovers, snapshot unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import warnings
import zipfile

import pytest

from scripts import build_marvel_materials as bmm

ZIP_PREFIX = bmm.ZIP_PREFIX
MANIFEST_NAME = "source_manifest.json"
REAL_SOURCE_ZIP = Path(r"D:\project\guandan\runs\comparison_delivery\dataset\guandan-marvel-source.zip")
REAL_SOURCE_MANIFEST = Path(r"D:\project\guandan\runs\comparison_delivery\dataset\source_manifest.json")
REAL_OVERLAY = Path(r"D:\project\guandan\runs\M08_bootstrap_observability_repair"
                    r"\notebook_bootstrap_actual_20261010a\working\guandan-marvel-source")
REAL_NATIVE = REAL_OVERLAY / "nanopokerlib" / bmm.NATIVE_FILENAME
REAL_RUNTIME = Path(r"D:\project\guance\build\acceptance\guance-model-platform-20261016"
                    r"\phase2\runtime\marvel-e5c82e7c-d7acbc8d")

FILES = {"README.md": b"# materialized runtime\n", "configs/profile.json": b"{}\n"}
NATIVE_BYTES = b"synthetic-native-bytes"


def _entry(path: str, data: bytes, origin: str = "git") -> dict:
    return {"bytes": len(data), "origin": origin, "path": path,
            "sha256": hashlib.sha256(data).hexdigest()}


def _manifest_payload(files: list[dict] | None = None, **overrides) -> dict:
    payload = {
        "schema": "guandan-marvel-source-manifest-v1",
        "source_root": "guandan-marvel-source",
        "git_commit": "e5c82e7c27fb366800e9722c9e8e4720df7db01f",
        "package_id": "installer-test-package",
        "created_at": "2026-01-01T00:00:00+00:00",
        "bundled_dependencies": [],
        "licenses": [],
        "remote_verified": False,
        "uploaded": False,
        "workspace_snapshot": False,
        "files": files if files is not None else [_entry(p, d) for p, d in FILES.items()],
    }
    payload.update(overrides)
    payload["manifest_hash"] = bmm._canonical(payload)
    return payload


def _manifest_raw(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _delivery_zip(path: Path, members: list[tuple[str, bytes]], embedded: bytes,
                  *, compression: int = zipfile.ZIP_STORED) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, "w", compression=compression) as archive:
            for name, data in members:
                archive.writestr(ZIP_PREFIX + name, data)
            archive.writestr(ZIP_PREFIX + MANIFEST_NAME, embedded)


def _make_overlay(root: Path) -> Path:
    for relative in bmm.CPP_PATHS:
        target = root.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"// {relative}\n".encode())
    return root


def _overlay_hash(root: Path) -> str:
    try:
        return bmm._raw_native_manifest(root)["hash"]
    except Exception:
        return "0" * 64


def _snapshot(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


def _install_args(tmp_path: Path, *, members: list[tuple[str, bytes]] | None = None,
                  embedded: bytes | None = None, payload: dict | None = None,
                  raw_override: bytes | None = None, overlay_root: Path | None = None,
                  native: bool = True, target_name: str = "installed",
                  compression: int = zipfile.ZIP_STORED,
                  ) -> tuple[Path, Path, argparse.Namespace]:
    inputs = tmp_path / "inputs"; inputs.mkdir(exist_ok=True)
    work = tmp_path / "work"; work.mkdir(exist_ok=True)
    payload = payload if payload is not None else _manifest_payload()
    raw = _manifest_raw(payload) if raw_override is None else raw_override
    members = members if members is not None else [(p, d) for p, d in FILES.items()]
    embedded = raw if embedded is None else embedded
    zip_path = inputs / "source.zip"
    _delivery_zip(zip_path, members, embedded, compression=compression)
    manifest_path = inputs / MANIFEST_NAME
    manifest_path.write_bytes(raw)
    if overlay_root is None:
        overlay_root = _make_overlay(inputs / "overlay")
    native_path = inputs / "native.pyd"
    if native:
        native_path.write_bytes(NATIVE_BYTES)
    target = work / target_name
    native_sha256 = hashlib.sha256(native_path.read_bytes()).hexdigest() if native else "0" * 64
    native_bytes = native_path.stat().st_size if native else 1
    args = argparse.Namespace(
        source_zip=zip_path, source_manifest=manifest_path, overlay_root=overlay_root,
        native=native_path, target=target,
        expected_source_zip_sha256=hashlib.sha256(zip_path.read_bytes()).hexdigest(),
        expected_source_manifest_sha256=hashlib.sha256(raw).hexdigest(),
        expected_source_content_manifest_hash=payload.get("manifest_hash", "0" * 64),
        expected_source_commit=payload.get("git_commit", "0" * 40),
        expected_native_sha256=native_sha256,
        expected_native_bytes=native_bytes,
        expected_overlay_manifest_hash=_overlay_hash(overlay_root),
    )
    return work, target, args


def _assert_clean_failure(work: Path, target: Path) -> None:
    assert not target.exists()
    assert [path.name for path in work.iterdir()] == []
    assert not [path for path in work.rglob("*") if ".staging" in path.name]


def _fake_probe(source_root: Path, trusted_modules: str, *, overlay_hash: str,
                matches: bool = True, extension_sha256: str | None = None) -> dict:
    native = source_root / "nanopokerlib" / bmm.NATIVE_FILENAME
    json.loads(trusted_modules)  # the fake still requires a well-formed trust payload
    return {
        "extension_path": str(native),
        "extension_filename": bmm.NATIVE_FILENAME,
        "extension_sha256": (extension_sha256 if extension_sha256 is not None
                             else hashlib.sha256(native.read_bytes()).hexdigest()),
        "embedded_source_sha256": overlay_hash,
        "current_source_manifest_hash": overlay_hash,
        "source_matches_loaded_binary": matches,
        "compiler": "11.2.0",
        "cxx_standard": 202002,
        "python_implementation": "CPython",
        "python_version": "3.12.0",
        "python_cache_tag": "cpython-312",
    }


@pytest.mark.parametrize("bad_path", [
    "../../escaped.txt",
    "..\\..\\escaped.txt",
    "C:/absolute.txt",
    "/rooted.txt",
    "sub/file.txt:ads",
    "CON",
    "dir/NUL.txt",
    "a//b.txt",
    "trail. /file.txt",
    "sub/\x01control.txt",
    "name.txt.",
])
def test_unsafe_manifest_paths_fail_before_any_write(tmp_path: Path, bad_path: str) -> None:
    payload = _manifest_payload(files=[_entry(bad_path, b"payload")])
    work, target, args = _install_args(tmp_path, payload=payload,
                                       members=[(bad_path, b"payload")])
    before = _snapshot(tmp_path)
    with pytest.raises(SystemExit):
        bmm.install_runtime(args)
    assert _snapshot(tmp_path) == before
    _assert_clean_failure(work, target)


def test_escape_repro_counterexample_writes_nothing_anywhere(tmp_path: Path) -> None:
    """Exact former repro: escaped member + missing overlay must leave no file."""
    payload = _manifest_payload(files=[_entry("../../escaped.txt", b"x")])
    work, target, args = _install_args(tmp_path, payload=payload,
                                       members=[("../../escaped.txt", b"x")],
                                       overlay_root=tmp_path / "missing-overlay")
    before = _snapshot(tmp_path)
    with pytest.raises(SystemExit):
        bmm.install_runtime(args)
    assert _snapshot(tmp_path) == before
    assert not list(tmp_path.rglob("escaped.txt"))
    _assert_clean_failure(work, target)


def test_unsafe_zip_member_relative_path_fails(tmp_path: Path) -> None:
    # The manifest is benign; only the archive member name tries to traverse.
    payload = _manifest_payload(files=[_entry("ok.txt", b"data")])
    work, target, args = _install_args(tmp_path, payload=payload,
                                       members=[("../evil.txt", b"data")])
    before = _snapshot(tmp_path)
    with pytest.raises(SystemExit):
        bmm.install_runtime(args)
    assert _snapshot(tmp_path) == before
    assert not list(tmp_path.rglob("evil.txt"))
    _assert_clean_failure(work, target)


def test_duplicate_manifest_path_fails(tmp_path: Path) -> None:
    payload = _manifest_payload(files=[_entry("a.txt", b"1"), _entry("a.txt", b"2")])
    work, target, args = _install_args(tmp_path, payload=payload,
                                       members=[("a.txt", b"1")])
    with pytest.raises(SystemExit, match="duplicate"):
        bmm.install_runtime(args)
    _assert_clean_failure(work, target)


def test_duplicate_zip_member_fails(tmp_path: Path) -> None:
    members = [(path, data) for path, data in FILES.items()]
    members.append(("README.md", FILES["README.md"]))
    work, target, args = _install_args(tmp_path, members=members)
    with pytest.raises(SystemExit, match="duplicate members"):
        bmm.install_runtime(args)
    _assert_clean_failure(work, target)


def test_zip_extra_member_fails(tmp_path: Path) -> None:
    members = [(path, data) for path, data in FILES.items()]
    members.append(("extra.txt", b"extra"))
    work, target, args = _install_args(tmp_path, members=members)
    with pytest.raises(SystemExit, match="not listed in the manifest"):
        bmm.install_runtime(args)
    _assert_clean_failure(work, target)


def test_zip_missing_member_fails(tmp_path: Path) -> None:
    members = [("README.md", FILES["README.md"])]
    work, target, args = _install_args(tmp_path, members=members)
    with pytest.raises(SystemExit, match="does not exactly match"):
        bmm.install_runtime(args)
    _assert_clean_failure(work, target)


def test_embedded_manifest_mismatch_fails(tmp_path: Path) -> None:
    modified = bytearray(_manifest_raw(_manifest_payload()))
    modified[-3] ^= 0x01  # same length, still valid JSON, different bytes
    work, target, args = _install_args(tmp_path, embedded=bytes(modified))
    with pytest.raises(SystemExit, match="embedded source_manifest.json differs"):
        bmm.install_runtime(args)
    _assert_clean_failure(work, target)


@pytest.mark.parametrize("raw", [
    b"{not json",
    b'{"schema": 1, "schema": 2}',
    b'{"value": NaN}',
])
def test_malformed_manifest_fails(tmp_path: Path, raw: bytes) -> None:
    work, target, args = _install_args(tmp_path, raw_override=raw)
    with pytest.raises(SystemExit):
        bmm.install_runtime(args)
    _assert_clean_failure(work, target)


def test_oversized_manifest_fails(tmp_path: Path) -> None:
    raw = b" " * (bmm._MAX_MANIFEST_BYTES + 1)
    work, target, args = _install_args(tmp_path, raw_override=raw)
    with pytest.raises(SystemExit, match="oversized"):
        bmm.install_runtime(args)
    _assert_clean_failure(work, target)


def test_manifest_hash_tamper_fails(tmp_path: Path) -> None:
    payload = _manifest_payload()
    payload["manifest_hash"] = "0" * 64
    work, target, args = _install_args(tmp_path, payload=payload)
    with pytest.raises(SystemExit, match="not self-consistent"):
        bmm.install_runtime(args)
    _assert_clean_failure(work, target)


def test_manifest_extra_top_key_fails(tmp_path: Path) -> None:
    payload = _manifest_payload()
    payload["unexpected"] = True
    payload["manifest_hash"] = bmm._canonical(
        {key: value for key, value in payload.items() if key != "manifest_hash"})
    work, target, args = _install_args(tmp_path, payload=payload)
    with pytest.raises(SystemExit, match="top-level keys"):
        bmm.install_runtime(args)
    _assert_clean_failure(work, target)


def test_member_bytes_hash_mismatch_fails(tmp_path: Path) -> None:
    tampered = b"Z" * len(FILES["README.md"])
    members = [("README.md", tampered), ("configs/profile.json", FILES["configs/profile.json"])]
    work, target, args = _install_args(tmp_path, members=members)
    with pytest.raises(SystemExit, match="bytes/hash mismatch"):
        bmm.install_runtime(args)
    _assert_clean_failure(work, target)


def test_overlay_missing_fails_before_writes(tmp_path: Path) -> None:
    work, target, args = _install_args(tmp_path)
    args.overlay_root.joinpath(*bmm.CPP_PATHS[1].split("/")).unlink()
    before = _snapshot(tmp_path)
    with pytest.raises(SystemExit, match="overlay input missing"):
        bmm.install_runtime(args)
    assert _snapshot(tmp_path) == before
    _assert_clean_failure(work, target)


def test_overlay_measured_mismatch_fails_and_cleans_staging(tmp_path: Path, monkeypatch) -> None:
    work, target, args = _install_args(tmp_path)
    monkeypatch.setattr(bmm, "_identity_probe",
                        lambda source_root, trust: _fake_probe(source_root, trust,
                                                               overlay_hash="0" * 64))
    with pytest.raises(SystemExit, match="overlay raw manifest differs"):
        bmm.install_runtime(args)
    _assert_clean_failure(work, target)


def test_source_matches_loaded_binary_false_fails(tmp_path: Path, monkeypatch) -> None:
    work, target, args = _install_args(tmp_path)
    overlay_hash = _overlay_hash(args.overlay_root)
    monkeypatch.setattr(bmm, "_identity_probe",
                        lambda source_root, trust: _fake_probe(source_root, trust,
                                                               overlay_hash=overlay_hash,
                                                               matches=False))
    with pytest.raises(SystemExit, match="source_matches_loaded_binary is not true"):
        bmm.install_runtime(args)
    _assert_clean_failure(work, target)


def test_native_identity_mismatch_fails(tmp_path: Path, monkeypatch) -> None:
    work, target, args = _install_args(tmp_path)
    overlay_hash = _overlay_hash(args.overlay_root)
    monkeypatch.setattr(bmm, "_identity_probe",
                        lambda source_root, trust: _fake_probe(source_root, trust,
                                                               overlay_hash=overlay_hash,
                                                               extension_sha256="f" * 64))
    with pytest.raises(SystemExit, match="loaded native extension identity mismatch"):
        bmm.install_runtime(args)
    _assert_clean_failure(work, target)


def test_native_missing_fails_before_writes(tmp_path: Path) -> None:
    work, target, args = _install_args(tmp_path, native=False)
    before = _snapshot(tmp_path)
    with pytest.raises(SystemExit, match="native extension missing"):
        bmm.install_runtime(args)
    assert _snapshot(tmp_path) == before
    _assert_clean_failure(work, target)


def test_target_already_exists_fails_without_touching(tmp_path: Path) -> None:
    work, target, args = _install_args(tmp_path)
    target.mkdir()
    sentinel = target / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    before = _snapshot(tmp_path)
    with pytest.raises(SystemExit, match="target already exists"):
        bmm.install_runtime(args)
    assert _snapshot(tmp_path) == before
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert [path.name for path in work.iterdir()] == [target.name]


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_target_parent_junction_rejected(tmp_path: Path) -> None:
    work, _target, args = _install_args(tmp_path)
    real = tmp_path / "real-parent"
    real.mkdir()
    junction = tmp_path / "junction-parent"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(real)],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.skip("junction creation unavailable")
    try:
        args.target = junction / "installed"
        before = _snapshot(tmp_path)
        with pytest.raises(SystemExit, match="reparse"):
            bmm.install_runtime(args)
        assert _snapshot(tmp_path) == before
        assert list(real.iterdir()) == []
        assert [path.name for path in work.iterdir()] == []
    finally:
        os.rmdir(junction)


def test_synthetic_install_is_atomic_and_complete(tmp_path: Path, monkeypatch) -> None:
    work, target, args = _install_args(tmp_path)
    overlay_hash = _overlay_hash(args.overlay_root)
    monkeypatch.setattr(bmm, "_identity_probe",
                        lambda source_root, trust: _fake_probe(source_root, trust,
                                                               overlay_hash=overlay_hash))
    assert bmm.install_runtime(args) == 0
    runtime = json.loads((target / "runtime.json").read_text(encoding="utf-8"))
    native_sha = hashlib.sha256(NATIVE_BYTES).hexdigest()
    assert runtime["four_condition_probe"]["source_matches_loaded_binary"] is True
    assert runtime["native"]["sha256"] == native_sha
    assert runtime["runtime_id"] == f"marvel-e5c82e7c27fb-{native_sha[:12]}"
    assert runtime["trusted_pins"]["source_zip_sha256"] == args.expected_source_zip_sha256
    assert runtime["trusted_pins"]["native_bytes"] == args.expected_native_bytes
    assert [path.name for path in work.iterdir()] == [target.name]
    bmm._verify_sums(target)


# --- A: internally-consistent but untrusted archive ------------------------------

def test_untrusted_selfconsistent_archive_rejected_before_probe(tmp_path: Path,
                                                                monkeypatch) -> None:
    sentinel = tmp_path / "sentinel.txt"
    malicious = ("from pathlib import Path\n"
                 f"Path({str(sentinel)!r}).write_text('EXECUTED', encoding='utf-8')\n").encode()
    files = {"nanopokerlib/__init__.py": b"", "nanopokerlib/guandan.py": malicious,
             "nanopokerlib/build_identity.py": b""}
    payload = _manifest_payload(files=[_entry(p, d) for p, d in files.items()])
    work, target, args = _install_args(tmp_path, payload=payload,
                                       members=[(p, d) for p, d in files.items()])
    args.expected_source_zip_sha256 = "0" * 64  # pin belongs to the trusted delivery
    calls = []
    monkeypatch.setattr(bmm, "_identity_probe", lambda *a, **k: calls.append(1))
    before = _snapshot(tmp_path)
    with pytest.raises(SystemExit, match="trusted source zip"):
        bmm.install_runtime(args)
    assert calls == []
    assert _snapshot(tmp_path) == before
    assert not sentinel.exists()
    _assert_clean_failure(work, target)


# --- B: self-consistent top-level shadow modules --------------------------------

@pytest.mark.parametrize("shadow", ["sitecustomize.py", "usercustomize.py",
                                    "platform.py", "hashlib.py"])
def test_selfconsistent_top_level_shadow_rejected_before_execution(
        tmp_path: Path, shadow: str) -> None:
    sentinel = tmp_path / "shadow-sentinel.txt"
    content = ("from pathlib import Path\n"
               f"Path({str(sentinel)!r}).write_text('EXECUTED', encoding='utf-8')\n").encode()
    files = dict(FILES); files[shadow] = content
    payload = _manifest_payload(files=[_entry(p, d) for p, d in files.items()])
    work, target, args = _install_args(tmp_path, payload=payload,
                                       members=[(p, d) for p, d in files.items()])
    before = _snapshot(tmp_path)
    with pytest.raises(SystemExit, match="forbidden top-level shadow"):
        bmm.install_runtime(args)
    assert _snapshot(tmp_path) == before
    assert not sentinel.exists()
    _assert_clean_failure(work, target)


# --- C: correct materials with wrong expected pins ------------------------------

@pytest.mark.parametrize("field,value,match", [
    ("expected_source_zip_sha256", "0" * 64, "trusted source zip"),
    ("expected_source_manifest_sha256", "0" * 64, "trusted source manifest"),
    ("expected_source_content_manifest_hash", "0" * 64, "trusted content manifest"),
    ("expected_source_commit", "0" * 40, "trusted source commit"),
    ("expected_native_sha256", "0" * 64, "trusted native"),
    ("expected_native_bytes", 12345, "trusted native"),
    ("expected_overlay_manifest_hash", "0" * 64, "trusted raw overlay"),
])
def test_wrong_expected_pins_fail_before_probe(tmp_path: Path, monkeypatch,
                                               field: str, value: object,
                                               match: str) -> None:
    work, target, args = _install_args(tmp_path)
    setattr(args, field, value)
    calls = []
    monkeypatch.setattr(bmm, "_identity_probe", lambda *a, **k: calls.append(1))
    before = _snapshot(tmp_path)
    with pytest.raises(SystemExit, match=match):
        bmm.install_runtime(args)
    assert calls == []
    assert _snapshot(tmp_path) == before
    _assert_clean_failure(work, target)


# --- D: zip bounded reads and size limits ---------------------------------------

def test_declared_member_size_mismatch_fails(tmp_path: Path) -> None:
    members = [("README.md", b"short"),
               ("configs/profile.json", FILES["configs/profile.json"])]
    work, target, args = _install_args(tmp_path, members=members)
    with pytest.raises(SystemExit, match="declared size mismatch"):
        bmm.install_runtime(args)
    _assert_clean_failure(work, target)


def test_member_over_size_limit_rejected_at_manifest(tmp_path: Path) -> None:
    entry = {"bytes": bmm._MAX_MEMBER_BYTES + 1, "origin": "git",
             "path": "big.bin", "sha256": "0" * 64}
    payload = _manifest_payload(files=[entry])
    work, target, args = _install_args(tmp_path, payload=payload,
                                       members=[("big.bin", b"x")])
    with pytest.raises(SystemExit, match="out of bounds"):
        bmm.install_runtime(args)
    _assert_clean_failure(work, target)


def test_total_uncompressed_size_limit_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(bmm, "_MAX_TOTAL_BYTES", 10)
    work, target, args = _install_args(tmp_path)
    with pytest.raises(SystemExit, match="total uncompressed"):
        bmm.install_runtime(args)
    _assert_clean_failure(work, target)


def test_compression_ratio_limit_rejected(tmp_path: Path, monkeypatch) -> None:
    data = b"a" * 5000
    payload = _manifest_payload(files=[_entry("zeros.bin", data)])
    work, target, args = _install_args(tmp_path, payload=payload,
                                       members=[("zeros.bin", data)],
                                       compression=zipfile.ZIP_DEFLATED)
    monkeypatch.setattr(bmm, "_MAX_COMPRESSION_RATIO", 2)
    with pytest.raises(SystemExit, match="compression ratio"):
        bmm.install_runtime(args)
    _assert_clean_failure(work, target)


class _FakeStream:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.payload)
        data, self.payload = self.payload[:size], self.payload[size:]
        return data

    def __enter__(self) -> "_FakeStream":
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _FakeArchive:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def open(self, info) -> _FakeStream:
        return _FakeStream(self.payload)


def test_bounded_read_never_reads_past_expected_plus_one() -> None:
    with pytest.raises(SystemExit, match="bounded read size mismatch"):
        bmm._bounded_member_bytes(_FakeArchive(b"X" * 40), object(), 16, "member")
    assert bmm._bounded_member_bytes(_FakeArchive(b"X" * 16), object(), 16, "member") == b"X" * 16


# --- E: real frozen materials with explicit trusted pins ------------------------

@pytest.mark.skipif(not (REAL_SOURCE_ZIP.is_file() and REAL_SOURCE_MANIFEST.is_file()
                         and REAL_NATIVE.is_file()
                         and (REAL_RUNTIME / "runtime.json").is_file()),
                    reason="frozen materials unavailable")
def test_real_frozen_materials_with_trusted_pins_is_deterministic(tmp_path: Path) -> None:
    trusted = json.loads((REAL_RUNTIME / "runtime.json").read_text(encoding="utf-8"))
    work = tmp_path / "runtime"; work.mkdir()
    target = work / "marvel-e5c82e7c-d7acbc8d"
    args = argparse.Namespace(
        source_zip=REAL_SOURCE_ZIP, source_manifest=REAL_SOURCE_MANIFEST,
        overlay_root=REAL_OVERLAY, native=REAL_NATIVE, target=target,
        expected_source_zip_sha256=trusted["source"]["archive_sha256"],
        expected_source_manifest_sha256=trusted["source"]["manifest_sha256"],
        expected_source_content_manifest_hash=trusted["source"]["content_manifest_hash"],
        expected_source_commit=trusted["source"]["git_commit"],
        expected_native_sha256=trusted["native"]["sha256"],
        expected_native_bytes=trusted["native"]["bytes"],
        expected_overlay_manifest_hash=trusted["native_source_overlay"]["raw_manifest_hash"])
    assert bmm.install_runtime(args) == 0
    runtime = json.loads((target / "runtime.json").read_text(encoding="utf-8"))
    assert runtime["runtime_id"] == "marvel-e5c82e7c27fb-d7acbc8d76c9"
    assert runtime["native"]["sha256"] == (
        "d7acbc8d76c9e24caea1b851b733190c068adb8530486a23d42547986344808d")
    assert runtime["source_fingerprint"]["hash"] == (
        "246775cf2ccbd6f5d2b7bb0b8da9700ad62ae6d47939dcf258f4aed90f0e98a3")
    assert runtime["four_condition_probe"]["source_matches_loaded_binary"] is True
    assert runtime["trusted_pins"]["source_zip_sha256"] == args.expected_source_zip_sha256
    assert [path.name for path in work.iterdir()] == [target.name]
    bmm._verify_sums(target)
    sums = (target / "SHA256SUMS").read_text(encoding="utf-8").strip().splitlines()
    assert len(sums) == 189


# --- build-bundle (export) gates -------------------------------------------------

def _expectation(model_version: str, training_version: str):
    from guance.infrastructure.model.marvel_v2.checkpoint import CheckpointExpectation
    from guance.infrastructure.model.marvel_v2.identity import PROTOCOL
    return CheckpointExpectation(
        filename="fake.pt", bytes=4, sha256="0" * 64,
        schema="guandan-marvel-evaluation-checkpoint-v1", algorithm="ippo", architecture="v1.3",
        protocol_version=PROTOCOL, iteration=100, classification="evaluation_only",
        resume_classification="evaluation_only", training_resume_allowed=False,
        resolved_config_hash="1" * 64, source_hash="2" * 64,
        source_parent_commit="3" * 40, training_backend_identity_hash="4" * 64,
        payload_variant="flat_not_smoothed_v1", model_version=model_version,
        training_version=training_version).validate()


def _bundle_args(tmp_path: Path, *, output_name: str = "marvel-v2-test"):
    inputs = tmp_path / "inputs"; inputs.mkdir(exist_ok=True)
    checkpoint = inputs / "fake.pt"; checkpoint.write_bytes(b"fake")
    work = tmp_path / "bundles"; work.mkdir(exist_ok=True)
    output = work / output_name
    args = argparse.Namespace(checkpoint=checkpoint, runtime=REAL_RUNTIME, output=output,
                              model_id=output_name, model_version="MV", training_version="TV",
                              dry_run=False)
    return work, output, args


_NEEDS_RUNTIME = pytest.mark.skipif(
    not (REAL_RUNTIME / "runtime.json").is_file(),
    reason="materialized runtime unavailable")


@pytest.fixture
def stub_expectation(monkeypatch):
    monkeypatch.setattr(bmm, "_checkpoint_expectation",
                        lambda checkpoint, model_version, training_version:
                        (_expectation(model_version, training_version), "0" * 64))


@_NEEDS_RUNTIME
def test_build_bundle_atomic_success(tmp_path: Path, stub_expectation) -> None:
    work, output, args = _bundle_args(tmp_path)
    assert bmm.build_bundle(args) == 0
    assert sorted(path.name for path in output.iterdir()) == [
        "SHA256SUMS", "deployment.json", "manifest.json"]
    from guance.infrastructure.model.manifest import load_bundle
    bundle = load_bundle(output / "manifest.json")
    assert bundle.manifest.model_id == "marvel-v2-test"
    descriptor = json.loads((output / "deployment.json").read_text(encoding="utf-8"))
    assert descriptor["schema"] == "guance-guandan-marvel-v2-deployment-v2"
    assert descriptor["checkpoint"]["sha256"] == "0" * 64
    assert [path.name for path in work.iterdir()] == [output.name]
    assert not [path for path in work.rglob("*") if ".staging" in path.name]


@_NEEDS_RUNTIME
def test_build_bundle_existing_output_fails_without_touching(tmp_path: Path, stub_expectation) -> None:
    work, output, args = _bundle_args(tmp_path)
    output.mkdir()
    sentinel = output / "manifest.json"
    sentinel.write_text("old", encoding="utf-8")
    before = _snapshot(tmp_path)
    with pytest.raises(SystemExit, match="output already exists"):
        bmm.build_bundle(args)
    assert _snapshot(tmp_path) == before
    assert sentinel.read_text(encoding="utf-8") == "old"


@_NEEDS_RUNTIME
def test_build_bundle_failure_leaves_no_partial_output(tmp_path: Path, stub_expectation,
                                                       monkeypatch) -> None:
    work, output, args = _bundle_args(tmp_path)
    def _boom(bundle):
        raise RuntimeError("validation failed after staging writes")
    monkeypatch.setattr(bmm, "load_descriptor", _boom)
    with pytest.raises(RuntimeError, match="validation failed"):
        bmm.build_bundle(args)
    assert not output.exists()
    assert [path.name for path in work.iterdir()] == []
    assert not [path for path in work.rglob("*") if ".staging" in path.name]


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
@_NEEDS_RUNTIME
def test_build_bundle_output_parent_junction_rejected(tmp_path: Path, stub_expectation) -> None:
    real = tmp_path / "real-bundles"
    real.mkdir()
    junction = tmp_path / "junction-bundles"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(real)],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.skip("junction creation unavailable")
    try:
        work, _output, args = _bundle_args(tmp_path)
        args.output = junction / "marvel-v2-test"
        with pytest.raises(SystemExit, match="reparse"):
            bmm.build_bundle(args)
        assert list(real.iterdir()) == []
        assert [path.name for path in work.iterdir()] == []
    finally:
        os.rmdir(junction)


# --- report-accuracy gate (repair item 6) ----------------------------------------

def test_deployment_module_kept_only_training_distinctness_pins() -> None:
    from guance.infrastructure.model.marvel_v2 import deployment
    assert not hasattr(deployment, "TRAINING_BACKEND_SHA256")
    assert not hasattr(deployment, "TRAINING_SOURCE_SHA256")
    assert "TRAINING_SOURCE_SHA256" not in deployment.__all__
    assert "TRAINING_BACKEND_SHA256" not in deployment.__all__
    assert deployment.TRAINING_NATIVE_SHA256 and deployment.TRAINING_PYTHON_TAG
