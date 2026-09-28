"""One persistent inference process, a latest-only queue, and stale-result rejection."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from typing import Callable, Any
import uuid

from guance.contracts import GameObservation


class InferenceClient:
    def __init__(self, weights_path: Path, *, on_result: Callable[[dict],None],
                 on_status: Callable[[dict],None], timeout_seconds: float=2.0, threads: int=2):
        self.weights_path=Path(weights_path)
        self.on_result=on_result;self.on_status=on_status
        self.timeout=max(0.2,timeout_seconds);self.threads=max(1,min(threads,4))
        self._requests:queue.Queue[GameObservation|None]=queue.Queue(maxsize=1)
        self._responses:queue.Queue[dict]=queue.Queue(maxsize=16)
        self._stop=threading.Event();self._process:subprocess.Popen|None=None
        self._thread:threading.Thread|None=None
        self._latest_key:tuple[str,str,int]|None=None
        self._lock=threading.RLock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread=threading.Thread(target=self._run,name='guance-model',daemon=True)
        self._thread.start()

    def submit(self, observation: GameObservation) -> None:
        with self._lock:
            self._latest_key=observation.key
            try:self._requests.get_nowait()
            except queue.Empty:pass
            self._requests.put_nowait(observation)
        self.start()

    def invalidate(self) -> None:
        with self._lock:
            self._latest_key=None
            try:self._requests.get_nowait()
            except queue.Empty:pass

    def _launch(self) -> bool:
        if self._stop.is_set():return False
        self.on_status({'model_status':'loading','message':'正在加载本地模型'})
        env=os.environ.copy();env['PYTHONUTF8']='1';env['PYTHONIOENCODING']='utf-8'
        for key in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
            env[key]=str(self.threads)
        cmd=[sys.executable]
        if not getattr(sys,'frozen',False):
            cmd+=['-m','guance']
            source=Path(__file__).resolve().parents[3]
            env['PYTHONPATH']=str(source)+os.pathsep+env.get('PYTHONPATH','')
        cmd+=['--model-worker','--weights',str(self.weights_path),'--threads',str(self.threads)]
        flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
        self._responses=queue.Queue(maxsize=16)
        try:
            process=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                text=True,encoding='utf8',errors='replace',bufsize=1,env=env,creationflags=flags)
            self._process=process
            response_queue=self._responses
            def read():
                try:
                    assert process.stdout is not None
                    while True:
                        line=process.stdout.readline(2*1024*1024+1)
                        if not line:break
                        if len(line)>2*1024*1024:raise ValueError('oversized worker response')
                        obj=json.loads(line)
                        response_queue.put(obj,timeout=1)
                except Exception:
                    pass
                finally:
                    try:response_queue.put_nowait({'type':'exited'})
                    except queue.Full:pass
            threading.Thread(target=read,name='guance-model-reader',daemon=True).start()
            first=response_queue.get(timeout=12)
            if first.get('type')!='loaded':
                self.on_status({'model_status':'error','message':first.get('message','模型未能启动')})
                self._terminate();return False
            self.on_status({'model_status':'ready','message':'本地模型已就绪','metadata':first.get('metadata',{})})
            return True
        except Exception:
            self.on_status({'model_status':'error','message':'模型进程启动失败，请查看诊断'})
            self._terminate();return False

    def _run(self) -> None:
        if not self._launch():return
        while not self._stop.is_set():
            try:obs=self._requests.get(timeout=.2)
            except queue.Empty:continue
            if obs is None:break
            with self._lock:
                if obs.key!=self._latest_key:continue
            request_id=uuid.uuid4().hex
            try:
                if self._process is None or self._process.poll() is not None:
                    if not self._launch():break
                assert self._process is not None and self._process.stdin is not None
                self._process.stdin.write(json.dumps({'type':'recommend','request_id':request_id,'observation':obs.to_dict()},ensure_ascii=False)+'\n')
                self._process.stdin.flush()
                started=time.monotonic()
                reply=self._responses.get(timeout=self.timeout)
                result=reply.get('result',{})
                if reply.get('type')!='result' or result.get('request_id')!=request_id:
                    raise RuntimeError('worker response mismatch')
                with self._lock:
                    current=(obs.key==self._latest_key)
                if current and time.monotonic()-started<=self.timeout:
                    self.on_result(result)
                else:
                    self.on_status({'event':'stale_advice_dropped','message':'已丢弃过期推荐'})
            except queue.Empty:
                self.on_status({'model_status':'timeout','message':'本次计算超时，已暂停这条建议'})
                self._terminate()
            except Exception:
                self.on_status({'model_status':'error','message':'模型进程异常，正在等待重新计算'})
                self._terminate()
        self._terminate()

    def _terminate(self) -> None:
        process=self._process;self._process=None
        if process is None:return
        try:
            if process.poll()is None:
                process.terminate()
                try:process.wait(timeout=.8)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=.8)
        except (OSError,subprocess.TimeoutExpired):pass
        for stream in (process.stdin,process.stdout):
            if stream:
                try:stream.close()
                except OSError:pass

    def close(self) -> None:
        self._stop.set();self.invalidate()
        try:self._requests.put_nowait(None)
        except queue.Full:pass
        self._terminate()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
