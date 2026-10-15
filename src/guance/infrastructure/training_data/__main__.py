"""Offline training data tooling; imports no Qt and never starts capture."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .legacy import convert_legacy
from .recorder import TrainingDataRecorder
from .schema import canonical_json
from .validation import validate_decision


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='python -m guance.infrastructure.training_data')
    sub = parser.add_subparsers(dest='command', required=True)
    convert = sub.add_parser('convert', help='Convert local frames.jsonl, offline only')
    convert.add_argument('inputs', nargs='+', type=Path)
    convert.add_argument('--output-root', required=True, type=Path)
    validate = sub.add_parser('validate', help='Validate canonical decision JSONL')
    validate.add_argument('input', type=Path)
    derive = sub.add_parser('derive', help='Regenerate pinned FableDan inputs; never load a model')
    derive.add_argument('input', type=Path)
    derive.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == 'convert':
        reports=[]
        for path in args.inputs:
            try:
                reports.append(convert_legacy(path,args.output_root))
            except (OSError, ValueError, TypeError):
                reports.append({'status':'rejected','errors':[{'code':'conversion_failed'}]})
        print(canonical_json({'reports':reports}))
        return 2 if any(r['status']=='rejected' for r in reports) else 0
    compiled=[]
    count=0
    errors=[]
    try:
        if args.command == 'derive' and args.input.resolve() == args.output.resolve():
            raise ValueError('input_output_collision')
        with args.input.open(encoding='utf-8-sig') as stream:
            for number,line in enumerate(stream,1):
                try:
                    value=json.loads(line)
                    validate_decision(value)
                    if args.command == 'derive':
                        from .derive import derive_fabledan
                        compiled.append(derive_fabledan(value))
                    count+=1
                except (ValueError,TypeError,KeyError,IndexError):
                    errors.append({'line':number,'code':'unsupported_or_invalid_record'})
        if args.command == 'derive':
            TrainingDataRecorder._atomic_write(args.output,''.join(canonical_json(v)+'\n' for v in compiled))
    except (OSError,ValueError,TypeError):
        errors.append({'code':'file_operation_failed'})
    print(canonical_json({'command':args.command,'valid':count,'rejected':len(errors),'errors':errors}))
    return 2 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
