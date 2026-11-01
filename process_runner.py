"""Execução de FFmpeg com detecção de falta de progresso e encerramento do filho."""
from __future__ import annotations

import math
import queue
import subprocess
import tempfile
import threading
import time


def run_transcode(command, duration, callback, stall_timeout=180):
    if not math.isfinite(float(stall_timeout)) or stall_timeout <= 0 or duration <= 0:
        raise ValueError('Duração/timeout de codificação inválidos')
    updates = queue.Queue()
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command,stdout=subprocess.PIPE,stderr=errors,
            text=True,encoding='utf-8',errors='replace',bufsize=1,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        def read_progress():
            try:
                for line in process.stdout:
                    updates.put(line)
            finally:
                updates.put(None)
        reader = threading.Thread(target=read_progress,name='EVA-ffmpeg-progress',daemon=True)
        reader.start()
        progressed_at = time.monotonic()
        latest_seconds = -1.
        try:
            while True:
                try:
                    line = updates.get(timeout=min(1.,stall_timeout))
                except queue.Empty:
                    line = ''
                if line is None:
                    break
                if line.startswith('out_time='):
                    stamp = line.partition('=')[2].strip()
                    try:
                        hours,minutes,seconds = stamp.split(':')
                        position = int(hours)*3600+int(minutes)*60+float(seconds)
                    except ValueError:
                        continue
                    if position > latest_seconds:
                        latest_seconds = position
                        progressed_at = time.monotonic()
                    callback(min(.68,max(0.,.68*position/duration)),'Codificando MXF')
                elif line.strip() == 'progress=end':
                    progressed_at = time.monotonic()
                if time.monotonic()-progressed_at > stall_timeout:
                    raise TimeoutError(f'FFmpeg sem avanço por {stall_timeout:g}s; processo interrompido para liberar a fila.')
            code = process.wait(timeout=max(5.,min(30.,stall_timeout)))
            if code:
                errors.seek(0,2)
                size=errors.tell()
                errors.seek(max(0,size-8000))
                raise RuntimeError('ffmpeg falhou: '+errors.read().decode('utf-8',errors='replace')[-4000:])
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            reader.join(timeout=5)
            process.stdout.close()
