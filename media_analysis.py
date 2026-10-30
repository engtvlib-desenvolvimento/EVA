"""Uma leitura do vídeo para medir black e confirmar a imagem estática inicial.

Os limites de black vêm dos metadados por frame. O resumo emitido no EOF pelo
FFmpeg aponta para o último PTS, não para o fim exclusivo do último frame.
"""
from __future__ import annotations

import math
import re
import subprocess


NUMBER = r'([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)'
BLACK_METADATA_RE = re.compile(r'\blavfi\.black_(start|end)=' + NUMBER)
BLACK_SUMMARY_RE = re.compile(
    r'black_start:' + NUMBER + r'\s+black_end:' + NUMBER + r'\s+black_duration:' + NUMBER)
FREEZE_METADATA_RE = re.compile(r'\blavfi\.freezedetect\.freeze_(start|end)=' + NUMBER)
FREEZE_LOG_RE = re.compile(r'\blavfi\.freezedetect\.freeze_(start|end):\s*' + NUMBER)
SUBPROCESS_FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def _merge(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + 1e-7:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _paired_intervals(events, duration):
    """Retorna (início,fim,aberto_no_EOF), mantendo o fim exclusivo."""
    intervals = []
    pending = None
    for kind, text in events:
        value = float(text)
        if not math.isfinite(value):
            continue
        value = min(duration, max(0., value))
        if kind == 'start':
            # Uma repetição do mesmo metadado não reinicia o segmento.
            if pending is None:
                pending = value
        elif pending is not None:
            if value > pending:
                intervals.append((pending, value, False))
            pending = None
    if pending is not None and duration > pending:
        intervals.append((pending, duration, True))
    return intervals


def parse_scan_output(stderr, duration, fps, settings):
    """Separa os eventos sem inferir EOF a partir de uma tolerância de PTS."""
    if not math.isfinite(duration) or duration <= 0 or not math.isfinite(float(fps)) or float(fps) <= 0:
        raise ValueError('Duração ou FPS inválidos para análise do vídeo')
    minimum = float(settings.get('black_min_sec', .24))
    opening = float(settings.get('opening_scan_sec', 30.))
    if not math.isfinite(minimum) or minimum < 0 or not math.isfinite(opening) or opening <= 0:
        raise ValueError('Parâmetros de duração de análise inválidos')
    metadata = BLACK_METADATA_RE.findall(stderr)
    summary = BLACK_SUMMARY_RE.findall(stderr)
    if metadata:
        measured = _paired_intervals(metadata, duration)
    else:
        # Compatibilidade com saídas antigas/mocks que só contêm o resumo.
        measured = []
        for start, end, _ in summary:
            a, b = max(0., float(start)), min(duration, float(end))
            if b > a:
                measured.append((a, b, False))
    blacks = [(start,end) for start,end,at_eof in measured
              if at_eof or end-start+1e-7 >= minimum]
    freeze_events = FREEZE_METADATA_RE.findall(stderr) or FREEZE_LOG_RE.findall(stderr)
    freezes = [(start,end) for start,end,_ in _paired_intervals(freeze_events, duration)
               if start <= opening]
    # Cartão estático e black são freezes adjacentes de imagens diferentes;
    # uni-los apagaria justamente a transição usada para confirmar a claquete.
    return {'black_intervals': _merge(blacks), 'slate_intervals': sorted(set(freezes)),
            'exact_intervals': bool(metadata) or not bool(summary)}


def scan_video(path, duration, fps, settings):
    """Decodifica uma vez e conserva inclusive black de um frame no final."""
    threshold = float(settings.get('black_pixel_threshold', 0.))
    ratio = float(settings.get('black_picture_ratio', 1.))
    freeze_noise = float(settings.get('slate_freeze_noise_db', -50.))
    freeze_duration = float(settings.get('slate_min_sec', .5))
    if not all(math.isfinite(value) for value in (threshold,ratio,freeze_noise,freeze_duration)):
        raise ValueError('Parâmetros de detecção não finitos')
    if not (0 <= threshold <= 1 and 0 < ratio <= 1 and freeze_duration > 0):
        raise ValueError('Limiares de black/claquete inválidos')
    filters = (f'setpts=PTS-STARTPTS,blackdetect=d=0:pix_th={threshold}:pic_th={ratio},'
               f'freezedetect=n={freeze_noise}dB:d={freeze_duration},metadata=mode=print')
    command = ['ffmpeg','-hide_banner','-nostdin','-nostats','-loglevel','info',
               '-i',str(path),'-map','0:v:0','-vf',filters,'-an','-f','null','-']
    result = subprocess.run(command,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
                            text=True,encoding='utf-8',errors='replace',
                            creationflags=SUBPROCESS_FLAGS)
    if result.returncode:
        raise RuntimeError('Falha ao analisar black/claquete: ' + result.stderr[-2000:])
    return parse_scan_output(result.stderr,duration,fps,settings)
