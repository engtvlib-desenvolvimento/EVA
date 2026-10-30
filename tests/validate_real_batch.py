"""Valida um lote real em banco e destinos temporários, preservando a origem.

Uso: python tests/validate_real_batch.py --source C:\\pasta --report resultado.json
Não utiliza o banco de produção nem a pasta de destino configurada na interface.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core


def decoded_video_frames(path):
    command = ['ffprobe', '-v', 'error', '-count_frames', '-select_streams', 'v:0',
               '-show_entries', 'stream=nb_read_frames', '-of', 'json', str(path)]
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                            errors='replace', timeout=180, creationflags=core.SUBPROCESS_FLAGS)
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:])
    return int(json.loads(result.stdout)['streams'][0]['nb_read_frames'])


def validate_file(source, destination, settings):
    before = source.stat()
    source_hash = core.sha256(source)
    source_probe = core.run_probe(source)
    source_video = next(s for s in source_probe['streams'] if s['codec_type'] == 'video')
    with core.db_connection() as db:
        cursor = db.execute('''INSERT INTO jobs
            (source_path,source_size,source_mtime_ns,media_key,status,created_at,updated_at,destination_folder)
            VALUES(?,?,?,?,'NOVO',?,?,?)''',
            (str(source), before.st_size, before.st_mtime_ns, core.media_key(source),
             core.utcnow(), core.utcnow(), str(destination)))
        job_id = cursor.lastrowid
    analyzed = core.analyze(job_id, settings)
    if analyzed['confidence'] != 'ALTA':
        raise RuntimeError('Análise rejeitada: ' + analyzed['message'])
    rate = Fraction(analyzed['fps'])
    mark_in_frame = round(analyzed['mark_in'] * float(rate))
    mark_out_frame = round(analyzed['mark_out'] * float(rate))
    expected_frames = mark_out_frame - mark_in_frame
    expected_samples = round(float(Fraction(expected_frames, 1) / rate) * 48000)
    if mark_in_frame < 0 or mark_out_frame <= mark_in_frame:
        raise AssertionError('Cortes inválidos')
    source_frames = round(Fraction(source_video['duration_ts']) * Fraction(source_video['time_base']) * rate)
    if mark_out_frame > source_frames:
        raise AssertionError('Mark Out ultrapassa o último frame da origem')
    print('ANALISADO ' + json.dumps({'file': source.name, 'mark_in': analyzed['mark_in'],
          'mark_out': analyzed['mark_out'], 'expected_frames': expected_frames,
          'source_frames': source_frames, 'message': analyzed['message']}, ensure_ascii=False), flush=True)
    core.approve(job_id, analyzed['mark_in'], analyzed['mark_out'], automatic=True)
    result = core.process(job_id, settings=settings, automatic=True)
    if result['status'] != 'CONCLUIDO':
        raise AssertionError('Material não concluiu o processamento')
    output = Path(result['output_path'])
    probe = core.run_probe(output)
    video = next(s for s in probe['streams'] if s['codec_type'] == 'video')
    audio = [s for s in probe['streams'] if s['codec_type'] == 'audio']
    decoded_frames = decoded_video_frames(output)
    if decoded_frames != expected_frames:
        raise AssertionError(f'Frames decodificados {decoded_frames}; esperado {expected_frames}')
    if (video['width'], video['height'], video['pix_fmt'], video['field_order']) != (1920, 1080, 'yuv422p', 'tt'):
        raise AssertionError('Perfil de vídeo divergente')
    if len(audio) != 16 or not all(s['channels'] == 1 and s['codec_name'] == 'pcm_s24le'
                                  and s['sample_rate'] == '48000' for s in audio):
        raise AssertionError('Layout técnico de áudio divergente')
    for index, stream in enumerate(audio, 1):
        samples = round(Fraction(stream['duration_ts']) * Fraction(stream['time_base']) * 48000)
        if abs(samples - expected_samples) > 2:
            raise AssertionError(f'Pista {index}: {samples} amostras; esperado {expected_samples}')
    if core.sha256(output) != result['output_sha256']:
        raise AssertionError('Hash da saída diverge do histórico')
    if core.sha256(Path(result['xml_path'])) != result['xml_sha256']:
        raise AssertionError('Hash do XML diverge do histórico')
    core.ET.parse(result['xml_path'])
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or core.sha256(source) != source_hash:
        raise AssertionError('Arquivo original alterado')
    return dict(file=source.name, status='CONCLUIDO', mark_in=analyzed['mark_in'],
                mark_out=analyzed['mark_out'], duration=core.duration_from_probe(probe),
                decoded_frames=decoded_frames, audio_streams=len(audio),
                expected_audio_samples=expected_samples, original_preserved=True,
                original_sha256=source_hash, output_sha256=result['output_sha256'],
                analysis_message=analyzed['message'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--report', type=Path, help='Salva somente o relatório JSON; arquivos de mídia continuam temporários.')
    args = parser.parse_args()
    source = args.source.expanduser().resolve()
    if not source.is_dir():
        parser.error('Pasta de origem inexistente')
    files = sorted(path for path in source.rglob('*') if path.is_file() and path.suffix.lower() in core.VIDEO_EXTENSIONS)
    settings = core.config()
    records = []
    production_db = core.DB_FILE
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix='eva_validacao_lote_') as temporary:
            root = Path(temporary).resolve()
            destination = root / 'saida'
            destination.mkdir()
            settings.update(source_folder=str(source), destination_folder=str(destination),
                            working_folder=str(root / 'trabalho'), video_profile='xdcam_hd422')
            core.DB_FILE = root / 'validacao.sqlite3'
            core.init_db()
            for position, path in enumerate(files, 1):
                print(f'INICIANDO {position}/{len(files)} {path.name}', flush=True)
                item_started = time.monotonic()
                try:
                    record = validate_file(path, destination, settings)
                except Exception as exc:
                    record = dict(file=path.name, status='ERRO', error=str(exc))
                record['elapsed_seconds'] = round(time.monotonic() - item_started, 3)
                records.append(record)
                print('RESULTADO ' + json.dumps(record, ensure_ascii=False), flush=True)
                # Mantém o uso de disco limitado a um material por vez.
                for output in destination.iterdir():
                    if output.is_file():
                        output.unlink()
    finally:
        core.DB_FILE = production_db
    report = dict(source=str(source), production_destination_used=False, production_database_used=False,
                  total=len(records), passed=sum(row['status'] == 'CONCLUIDO' for row in records),
                  elapsed_seconds=round(time.monotonic() - started, 3), results=records)
    if args.report:
        args.report.expanduser().resolve().write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('RESUMO ' + json.dumps({key: value for key, value in report.items() if key != 'results'}, ensure_ascii=False), flush=True)
    return 0 if records and report['passed'] == len(records) else 1


if __name__ == '__main__':
    raise SystemExit(main())
