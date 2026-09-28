"""CLI composition entry. Helper dispatch happens before importing Qt."""
from __future__ import annotations
import argparse
import os


def main() -> int:
    parser=argparse.ArgumentParser(prog='guance')
    parser.add_argument('--model-worker',action='store_true')
    parser.add_argument('--weights',default='')
    parser.add_argument('--threads',type=int,default=2)
    parser.add_argument('--demo',action='store_true')
    parser.add_argument('--screenshot',default='')
    parser.add_argument('--exit-after',type=float,default=0)
    parser.add_argument('--version',action='store_true')
    args,extra=parser.parse_known_args()
    for key in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
        os.environ.setdefault(key,str(max(1,min(args.threads,4))))
    if args.version:
        from guance import __version__
        print(__version__);return 0
    if args.model_worker:
        from guance.infrastructure.model.worker import run
        return run(args.weights,args.threads)
    from guance.bootstrap import run_gui
    return run_gui(args,extra)

if __name__=='__main__':
    raise SystemExit(main())
