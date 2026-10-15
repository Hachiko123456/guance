"""Offline migration of existing authorized recordings; never network replay."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
from pathlib import Path
from typing import Iterator, Any

from guance.contracts import SourceFrame
from guance.domain.session import SessionEngine
from guance.infrastructure.protocol.live import GAME_HOST, parse_aggregate

from .collector import TrainingDataCollector
from .recorder import TrainingDataRecorder
from .schema import SCHEMA_VERSION, canonical_json, integer, finite_number
from .validation import validate_decision

MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_PAYLOAD_BYTES = 1024 * 1024


class LegacyInputError(ValueError):
    def __init__(self, code: str, line: int = 0):
        super().__init__(code)
        self.code, self.line = code, line


def source_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def iter_jsonl_frames(path: Path, counters: dict[str, int]) -> Iterator[SourceFrame]:
    """Validate target-frame metadata/hash, preserve order, never infer direction."""
    with path.open('rb') as stream:
        number = 0
        while True:
            line = stream.readline(MAX_LINE_BYTES + 1)
            if not line:
                return
            number += 1
            counters['input_rows'] += 1
            if len(line) > MAX_LINE_BYTES:
                raise LegacyInputError('line_too_large', number)
            try:
                record = json.loads(line.decode('utf-8-sig'))
            except (UnicodeError, ValueError):
                raise LegacyInputError('invalid_json_or_truncated_line', number) from None
            if type(record) is not dict:
                raise LegacyInputError('invalid_envelope', number)
            if record.get('host') != GAME_HOST:
                counters['ignored_non_target'] += 1
                continue
            direction = record.get('direction')
            if direction not in {'client', 'server'}:
                raise LegacyInputError('frame_direction_unknown', number)
            if record.get('dropped') or record.get('injected'):
                raise LegacyInputError('modified_or_dropped_frame', number)
            try:
                if type(record.get('schema_version')) is not int or record['schema_version'] != 1:
                    raise ValueError()
                sequence = integer(record.get('sequence'), 0, 2**63-1)
                captured_at = finite_number(record.get('received_at'))
                connection = record.get('connection_id')
                if type(connection) is not str or len(connection) > 4096:
                    raise ValueError()
            except (ValueError, TypeError, OverflowError):
                raise LegacyInputError('invalid_frame_metadata_or_base64', number) from None
            if direction == 'client':
                # Preserve transport ordering without retaining, parsing, or replaying
                # client request bytes. SessionEngine consumes only this bounded
                # metadata and deliberately ignores the empty client payload.
                counters['ignored_client'] += 1
                yield SourceFrame(host=GAME_HOST, payload=b'', received_at=captured_at,
                                  sequence=sequence, connection_id=connection, direction='client')
                continue
            try:
                count = integer(record.get('payload_bytes'), 0, MAX_PAYLOAD_BYTES)
                encoded = record.get('payload_b64')
                if type(encoded) is not str:
                    raise ValueError()
                payload = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError, binascii.Error, OverflowError):
                raise LegacyInputError('invalid_frame_metadata_or_base64', number) from None
            if len(payload) != count or hashlib.sha256(payload).hexdigest() != record.get('payload_sha256'):
                raise LegacyInputError('payload_integrity_mismatch', number)
            counters['server_frames'] += 1
            yield SourceFrame(host=GAME_HOST, payload=payload, received_at=captured_at,
                              sequence=sequence, connection_id=connection, direction='server')


def read_legacy_episodes(path: str | Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read-only deterministic reconstruction. Return canonical episodes + safe report.

    No automatic reset after an error. Omitted directions are not server data.
    The report contains only hashes/counts/enumerated reasons, not source paths.
    """
    path = Path(path)
    digest = source_sha256(path)
    source_id = 'sha256:' + digest
    collector = TrainingDataCollector(source='legacy', source_id=source_id,
                                      rule_profile='tencent_daguandan', rule_revision='guance-observed-v1')
    engine = SessionEngine('legacy-' + digest[:24], aggregate_parser=parse_aggregate, accepted_host=GAME_HOST)
    counters = {'input_rows': 0, 'server_frames': 0, 'ignored_client': 0, 'ignored_non_target': 0}
    errors: list[dict[str, Any]] = []
    latest: dict[str, dict[str, Any]] = {}
    def take(batches):
        for episode in batches:
            latest[episode['episode_id']] = episode
    try:
        if path.suffix.lower() != '.jsonl':
            # The existing .mitm reader deliberately has a looser direction
            # contract; do not use it as a training-data trust boundary.
            raise LegacyInputError('unsupported_input_format')
        frames = iter_jsonl_frames(path, counters)
        for frame in frames:
            for update in engine.feed(frame):
                if update.kind == 'error':
                    take(collector.invalidate_current('state_error'))
                    # Engine codes are defined in project code; never serialize the message.
                    safe_codes = {'state_invalid','state_out_of_order','protocol_rejected','frame_direction_unknown',
                                  'engine_paused','capture_paused','manual_seat_conflict','seat_evidence_conflict','event_unsupported'}
                    errors.append({'code': update.error_code if update.error_code in safe_codes else 'engine_rejected'})
                    break
                take(collector.observe_update(update))
            if engine.paused:
                break
    except LegacyInputError as exc:
        errors.append({'code': exc.code, 'line': exc.line})
        take(collector.invalidate_current('external_loss'))
    except (ValueError, TypeError, OSError, EOFError):
        errors.append({'code': 'legacy_read_failed'})
        take(collector.invalidate_current('external_loss'))
    if source_sha256(path) != digest:
        errors.append({'code': 'source_changed_during_read'})
        take(collector.invalidate_current('external_loss'))
        # Previously sealed games are also unverifiable against a stable input.
        for item in latest.values():
            item['status'] = 'quarantine'
            item['reasons'] = ['source_changed_during_read']
            item['lost'] = True
            for record in item['records']:
                record['status'] = 'quarantine'
                record['quality'] = {'status':'quarantine','reasons':['source_changed_during_read'],'lost':True}
                record['outcome'] = None
    take(collector.close())
    episodes = list(latest.values())
    records = [record for episode in episodes for record in episode['records']]
    for record in records:
        validate_decision(record)
    report = {'record_type':'conversion_report','schema_version':SCHEMA_VERSION,
              'source_sha256':digest,'source_format':path.suffix.lower().lstrip('.'),
              'status':'rejected' if errors else 'converted', 'counters':counters,
              'episodes':len(episodes),'decisions':len(records),
              'accepted_candidates':sum(r['status']=='accepted_candidate' for r in records),
              'partial':sum(r['status']=='partial' for r in records),
              'quarantined':sum(r['status']=='quarantine' for r in records),'errors':errors}
    return episodes, report


def convert_legacy(path: str | Path, output_root: str | Path) -> dict[str, Any]:
    """Output follows live writer layout: output_root/training_data. Idempotent IDs."""
    source = Path(path).resolve()
    root = Path(output_root).resolve() / 'training_data'
    # Never write within a raw capture directory.
    if root == source.parent or source.parent in root.parents:
        raise ValueError('output_must_be_outside_source_directory')
    episodes, report = read_legacy_episodes(source)
    entries = []
    for episode in episodes:
        relative = 'episodes/' + episode['episode_id'] + '.jsonl'
        TrainingDataRecorder._atomic_write(root / relative, ''.join(canonical_json(r)+'\n' for r in episode['records']))
        entries.append({k:v for k,v in episode.items() if k!='records'} | {'file':relative,'record_count':len(episode['records'])})
    manifest = {'record_type':'manifest','schema_version':SCHEMA_VERSION,'source':'legacy',
                'source_id':'sha256:'+report['source_sha256'],'status':'degraded' if report['errors'] else 'closed',
                'episodes':entries,'conversion':report}
    TrainingDataRecorder._atomic_write(root / 'manifests' / ('legacy-'+report['source_sha256']+'.json'),canonical_json(manifest)+'\n')
    return report
