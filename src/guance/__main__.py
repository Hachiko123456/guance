"""CLI composition entry. Helper dispatch happens before importing Qt."""
from __future__ import annotations

import argparse
import os


def main() -> int:
    parser = argparse.ArgumentParser(prog="guance")
    parser.add_argument("--model-worker", action="store_true")
    parser.add_argument("--model-bundle", default="")
    parser.add_argument("--weights", default="", help="legacy model-source alias")
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--screenshot", default="")
    parser.add_argument("--exit-after", type=float, default=0)
    parser.add_argument("--version", action="store_true")
    parser.add_argument(
        "--frozen-smoke", action="store_true",
        help="run a bounded no-network Qt/QML startup smoke for package acceptance",
    )
    parser.add_argument("--smoke-report", default="")
    parser.add_argument(
        "--smoke-state", choices=("default", "demo", "unavailable", "error"),
        default="default",
    )
    parser.add_argument(
        "--smoke-theme", choices=("system", "light", "dark"), default="system",
    )
    args, extra = parser.parse_known_args()
    for key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ.setdefault(key, str(max(1, min(args.threads, 4))))
    if args.version:
        from guance import __version__
        print(__version__)
        return 0
    if args.model_worker:
        from guance.infrastructure.model.worker import run
        return run(args.model_bundle or args.weights, args.threads,
                   requested_device=args.device)
    from guance.bootstrap import run_gui
    return run_gui(args, extra)


if __name__ == "__main__":
    # Frozen-package multiprocessing: the DanKS V3 partition precompute spawns
    # worker processes; freeze_support intercepts the spawned child bootstrap
    # before the GUI/worker entry runs (no-op unfrozen and on non-Windows).
    import multiprocessing

    multiprocessing.freeze_support()
    raise SystemExit(main())
