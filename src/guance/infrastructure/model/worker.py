"""Persistent model subprocess. JSON-lines in/out; diagnostics only on stderr."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys

MAX_LINE = 2 * 1024 * 1024


def run(weights_path: str, threads: int=2) -> int:
    threads=max(1,min(threads,4))
    for key in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
        os.environ[key]=str(threads)
    from guance.contracts import GameObservation
    from .advisor import FableDanAdvisor
    def emit(value):
        sys.stdout.write(json.dumps(value,ensure_ascii=False,allow_nan=False)+'\n')
        sys.stdout.flush()
    advisor=FableDanAdvisor(Path(weights_path))
    try:
        emit({'type':'loaded','metadata':advisor.load()})
    except Exception as exc:
        emit({'type':'load_error','message':str(exc)[:300]})
        return 2
    while True:
        line=sys.stdin.readline(MAX_LINE+1)
        if not line:
            return 0
        if len(line)>MAX_LINE:
            emit({'type':'protocol_error','message':'消息超过大小限制'})
            return 3
        try:
            item=json.loads(line)
            if item.get('type')=='stop':
                return 0
            if item.get('type')!='recommend':
                raise ValueError('unknown message type')
            request_id=str(item.get('request_id',''))[:100]
            observation=GameObservation.from_dict(item['observation'])
            result=advisor.recommend(observation,request_id)
            emit({'type':'result','result':result.to_dict()})
        except Exception:
            emit({'type':'request_error','request_id':str(item.get('request_id',''))[:100] if isinstance(locals().get('item'),dict) else '',
                  'message':'模型请求格式无效'})
