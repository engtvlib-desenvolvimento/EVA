"""Processamento e histórico da EVA. Requer FFmpeg e FFprobe no PATH."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from io import TextIOWrapper
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from xml.etree import ElementTree as ET

from media_analysis import scan_video
from integration_metadata import material_identifiers, validate_xml_settings, write_xml
from process_runner import run_transcode

HERE = (Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False)
        else Path(__file__).resolve().parent)
DB_FILE = HERE / 'historico.sqlite3'
CONFIG_FILE = HERE / 'config.json'
VALID_STATES = {'NOVO', 'REVISAR', 'APROVADO', 'PROCESSANDO', 'VALIDANDO',
                'TRANSFERINDO', 'PREPARADO', 'CONCLUIDO', 'DUPLICADO', 'ERRO', 'ANALISANDO'}
VIDEO_EXTENSIONS = {'.mxf', '.mov', '.mp4', '.avi', '.mkv', '.mpg', '.mpeg', '.mts', '.m2ts'}
SUBPROCESS_FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
ANALYSIS_REVISION = '3-independent-slate-and-tail'
ANALYSIS_DEFAULTS = {
    'black_min_sec': .24, 'black_pixel_threshold': 0., 'black_picture_ratio': 1.,
    'black_expected_sec': 5., 'black_tolerance_sec': .6,
    'black_separator_min_sec': 1., 'black_separator_max_sec': 6.,
    'opening_scan_sec': 30., 'variable_opening_scan_sec': 10.,
    'commercial_min_sec': 4.5,
    'slate_freeze_noise_db': -50., 'slate_min_sec': .5,
    'slate_start_tolerance_frames': 2., 'slate_boundary_tolerance_frames': 2.
}


class DuplicateMaterialError(FileExistsError):
    """Um nome de mídia já foi encontrado no histórico ou no destino."""


class MaterialIdentityConflict(DuplicateMaterialError):
    """Outro arquivo já reservou o CM/IDK7 neste destino."""


BLACK_RE = re.compile(r'black_start:([\d.]+)\s+black_end:([\d.]+)\s+black_duration:([\d.]+)')


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def config():
    with CONFIG_FILE.open('r', encoding='utf-8') as f:
        data = json.load(f)
    if len(data['audio_channel_map']) != 16:
        raise ValueError('audio_channel_map deve ter EXATAMENTE 16 canais')
    return data


def cut_enabled(settings):
    value = settings.get('cut_video', True)
    if not isinstance(value, bool):
        raise ValueError('cut_video deve ser true ou false no config.json.')
    return value


def analysis_signature(settings):
    effective = {key: settings.get(key, default) for key, default in ANALYSIS_DEFAULTS.items()}
    # JSON normaliza tuplas/listas; números equivalentes devem ter a mesma assinatura.
    normalized = {key: [float(x) for x in value] if isinstance(value, (list,tuple)) else float(value)
                  for key,value in effective.items()}
    encoded = json.dumps({'revision': ANALYSIS_REVISION, 'settings': normalized},
                         sort_keys=True, separators=(',',':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def save_config(settings):
    """Salva preferências sem truncar a configuração em caso de interrupção."""
    temporary = CONFIG_FILE.with_suffix('.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, CONFIG_FILE)
    finally:
        temporary.unlink(missing_ok=True)


def validate_watch_settings(settings):
    result = dict(settings)
    paths = {}
    for key, label in (('source_folder', 'origem'), ('destination_folder', 'destino')):
        value = str(result.get(key, '')).strip()
        if not value:
            raise ValueError(f'Escolha a pasta de {label}.')
        path = Path(value).expanduser().resolve()
        if not path.is_dir():
            raise NotADirectoryError(f'Pasta de {label} não encontrada: {path}')
        paths[key] = path
        result[key] = str(path)
    source, destination = paths['source_folder'], paths['destination_folder']
    work = Path(result.get('working_folder') or HERE / 'trabalho').expanduser().resolve()
    if source == destination or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError('Origem e destino devem ser pastas separadas, sem uma dentro da outra.')
    if result.get('floripa_folder') and destination == Path(result['floripa_folder']).expanduser().resolve():
        if not all(result.get(key, False) for key in ('enable_publish_to_floripa', 'xml_homologated', 'audio_layout_homologated')):
            raise ValueError('O destino é a pasta Floripa configurada. Configure o XML real e o mapa de áudio antes de habilitar as flags de publicação no config.json.')
    if work == source or work == destination or source.is_relative_to(work) or work.is_relative_to(destination):
        raise ValueError('A pasta de trabalho deve ficar separada da origem e do destino.')
    result['working_folder'] = str(work)
    for key, default, minimum in (('watch_interval_sec', 3, .05), ('stable_seconds', 10, 0),
                                   ('retry_delay_sec', 30, 0), ('max_attempts', 3, 1)):
        value = float(result.get(key, default))
        if not math.isfinite(value) or value < minimum:
            raise ValueError(f'{key} deve ser maior ou igual a {minimum}.')
        result[key] = int(value) if key == 'max_attempts' else value
    if len(result.get('audio_channel_map', [])) != 16:
        raise ValueError('audio_channel_map deve ter EXATAMENTE 16 canais.')
    result['cut_video'] = cut_enabled(result)
    limit = result.get('ui_history_limit', 500)
    if isinstance(limit, bool) or int(limit) != float(limit) or not 50 <= int(limit) <= 5000:
        raise ValueError('ui_history_limit deve ser um inteiro entre 50 e 5000.')
    result['ui_history_limit'] = int(limit)
    batch_limit=result.get('max_jobs_per_scan',10)
    if isinstance(batch_limit,bool) or int(batch_limit) != float(batch_limit) or not 1 <= int(batch_limit) <= 1000:
        raise ValueError('max_jobs_per_scan deve ser um inteiro entre 1 e 1000.')
    result['max_jobs_per_scan']=int(batch_limit)
    if result.get('delivery_retention_policy', 'require_present') not in ('require_present', 'handoff'):
        raise ValueError('delivery_retention_policy deve ser require_present ou handoff.')
    if result.get('publication_order', 'xml_first') not in ('xml_first', 'mxf_first'):
        raise ValueError('publication_order deve ser xml_first ou mxf_first, conforme o importador.')
    stall_timeout=float(result.get('ffmpeg_stall_timeout_sec',180))
    if not math.isfinite(stall_timeout) or stall_timeout < 5:
        raise ValueError('ffmpeg_stall_timeout_sec deve ser pelo menos 5 segundos.')
    result['ffmpeg_stall_timeout_sec']=stall_timeout
    analysis_timeout=float(result.get('analysis_timeout_sec',300))
    if not math.isfinite(analysis_timeout) or analysis_timeout < 5:
        raise ValueError('analysis_timeout_sec deve ser pelo menos 5 segundos.')
    result['analysis_timeout_sec']=analysis_timeout
    validate_xml_settings(result)
    if result.get('floripa_folder') and destination == Path(result['floripa_folder']).expanduser().resolve():
        if result.get('xml_profile', 'eva') != 'smartware_template':
            raise ValueError('A pasta Floripa exige XML de integração real configurado; o XML EVA não é um contrato Smartware.')
    missing = [name for name in ('ffmpeg', 'ffprobe') if not shutil.which(name)]
    if missing:
        raise FileNotFoundError('Instale FFmpeg e FFprobe no PATH. Não encontrado: ' + ', '.join(missing))
    return result


class ClosingConnection(sqlite3.Connection):
    """O context manager padrão SQLite não fecha a conexão ao sair do with."""

    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def db_connection():
    conn = sqlite3.connect(DB_FILE, timeout=30, factory=ClosingConnection)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=30000')
        return conn
    except Exception:
        conn.close()
        raise


def media_key(path):
    """Identidade do material por nome (sem caminho), ignorando maiúsculas/minúsculas.
    Para produção, o identificador deve ser refinado conforme o cadastro da Floripa.
    """
    return Path(path).stem.strip().casefold()


def matching_files(dest, stem):
    """Busca nomes existentes no destino, incluindo variantes de caixa; não considera .part."""
    if not dest.is_dir():
        raise NotADirectoryError(f'Pasta de destino não encontrada: {dest}')
    matches = []
    names = {(stem + '.mxf').casefold(), (stem + '.xml').casefold()}
    for item in dest.iterdir():
        if item.name.casefold() in names:
            matches.append(item.name)
    return matches


def init_db():
    with db_connection() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT NOT NULL UNIQUE,
                source_size INTEGER NOT NULL DEFAULT 0,
                source_mtime_ns INTEGER NOT NULL DEFAULT 0,
                duration REAL,
                fps TEXT,
                mark_in REAL,
                mark_out REAL,
                confidence TEXT,
                status TEXT NOT NULL DEFAULT 'NOVO',
                message TEXT DEFAULT '',
                output_path TEXT,
                xml_path TEXT,
                output_sha256 TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                moment TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES jobs(id)
            );
            CREATE TABLE IF NOT EXISTS media_transfers (
                media_key TEXT PRIMARY KEY COLLATE NOCASE,
                job_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                output_path TEXT,
                xml_path TEXT,
                output_sha256 TEXT,
                xml_sha256 TEXT,
                verified_at TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES jobs(id)
            );
            CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status);
            CREATE TABLE IF NOT EXISTS automatic_deliveries (
                media_key TEXT NOT NULL,
                destination_key TEXT NOT NULL,
                job_id INTEGER NOT NULL,
                source_size INTEGER NOT NULL,
                source_mtime_ns INTEGER NOT NULL,
                output_path TEXT NOT NULL,
                xml_path TEXT NOT NULL,
                output_sha256 TEXT NOT NULL,
                xml_sha256 TEXT NOT NULL,
                delivered_mxf_bytes INTEGER NOT NULL,
                delivered_xml_bytes INTEGER NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(media_key, destination_key)
            );
            CREATE TABLE IF NOT EXISTS monitor_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                moment TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS delivery_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                moment TEXT NOT NULL,
                media_key TEXT NOT NULL,
                destination_key TEXT NOT NULL,
                status TEXT NOT NULL,
                output_path TEXT NOT NULL,
                xml_path TEXT NOT NULL,
                output_sha256 TEXT NOT NULL,
                xml_sha256 TEXT NOT NULL
            );
        ''')
        # Atualização NÃO destrutiva do banco da versão anterior.
        columns = {r['name'] for r in db.execute('PRAGMA table_info(jobs)')}
        additions = {
            'media_key': 'TEXT', 'duplicate_of': 'INTEGER',
            'xml_sha256': 'TEXT', 'delivery_verified_at': 'TEXT',
            'delivered_mxf_bytes': 'INTEGER', 'delivered_xml_bytes': 'INTEGER',
            'destination_folder': 'TEXT', 'attempts': 'INTEGER NOT NULL DEFAULT 0',
            'next_retry_at': 'REAL NOT NULL DEFAULT 0', 'error_kind': 'TEXT',
            'progress': 'REAL NOT NULL DEFAULT 0', 'analysis_signature': 'TEXT',
            'black_intervals_json': 'TEXT', 'slate_intervals_json': 'TEXT',
            'cut_video': 'INTEGER NOT NULL DEFAULT 1', 'cm': 'TEXT', 'idk7': 'TEXT',
            'output_duration': 'REAL'
        }
        for name, datatype in additions.items():
            if name not in columns:
                db.execute(f'ALTER TABLE jobs ADD COLUMN {name} {datatype}')
        for table in ('automatic_deliveries', 'delivery_events'):
            available = {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
            for name,datatype in {'cut_video':'INTEGER NOT NULL DEFAULT 1','cm':'TEXT','idk7':'TEXT'}.items():
                if name not in available:
                    db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {datatype}')
        available = {row['name'] for row in db.execute('PRAGMA table_info(automatic_deliveries)')}
        if 'publication_started_at' not in available:
            db.execute('ALTER TABLE automatic_deliveries ADD COLUMN publication_started_at TEXT')
        db.execute('CREATE INDEX IF NOT EXISTS ix_jobs_media_key ON jobs(media_key)')
        db.execute('CREATE INDEX IF NOT EXISTS ix_jobs_destination ON jobs(destination_folder)')
        db.execute('CREATE INDEX IF NOT EXISTS ix_auto_destination ON automatic_deliveries(destination_key)')
        db.execute('CREATE INDEX IF NOT EXISTS ix_auto_cm ON automatic_deliveries(destination_key,cm)')
        db.execute('CREATE INDEX IF NOT EXISTS ix_auto_idk7 ON automatic_deliveries(destination_key,idk7)')
        # Completa apenas identidades ausentes, sem alterar os nomes/chaves do histórico.
        for row in db.execute('SELECT id,source_path FROM jobs WHERE cm IS NULL').fetchall():
            cm,idk7 = material_identifiers(row['source_path'])
            if cm:
                db.execute('UPDATE jobs SET cm=?,idk7=? WHERE id=?',(cm,idk7,row['id']))
        for row in db.execute('SELECT media_key,destination_key,output_path FROM automatic_deliveries WHERE cm IS NULL').fetchall():
            cm,idk7 = material_identifiers(row['output_path'])
            if cm:
                db.execute('UPDATE automatic_deliveries SET cm=?,idk7=? WHERE media_key=? AND destination_key=?',
                           (cm,idk7,row['media_key'],row['destination_key']))
        for row in db.execute('SELECT id,source_path FROM jobs WHERE media_key IS NULL').fetchall():
            db.execute('UPDATE jobs SET media_key=? WHERE id=?', (media_key(row['source_path']),row['id']))
        # Materiais já concluídos em bancos v0.1 também bloqueiam reenvios.
        # Não atesta nova integridade: marca histórico legado separadamente.
        db.execute('''INSERT OR IGNORE INTO media_transfers
            (media_key,job_id,status,output_path,xml_path,output_sha256,updated_at)
            SELECT media_key,id,'HISTORICO_LEGADO',output_path,xml_path,output_sha256,?
            FROM jobs WHERE status='CONCLUIDO' AND media_key IS NOT NULL''', (utcnow(),))


def duplicate_in_history(job, dest=None, include_other_sources=True):
    """Confere o log de transferências, outras origens com mesmo nome e pasta física."""
    key = media_key(job['source_path'])
    with db_connection() as db:
        delivery = db.execute('SELECT * FROM media_transfers WHERE media_key=?', (key,)).fetchone()
        if delivery:
            return f"Material '{key}' já consta no registro de transferências (ID {delivery['job_id']}, {delivery['status']}).", delivery['job_id']
        if include_other_sources:
            other = db.execute('''SELECT id,status FROM jobs WHERE media_key=? AND id<>?
                AND status!='DUPLICADO' ORDER BY id LIMIT 1''', (key,job['id'])).fetchone()
            if other:
                return f"Material com o mesmo nome já cadastrado no ID {other['id']} ({other['status']}).", other['id']
    if dest is not None:
        found = matching_files(Path(dest), Path(job['source_path']).stem)
        if found:
            return f"Nome já presente na pasta de destino: {', '.join(found)}.", None
    return None, None


def reject_duplicate(job_id, reason, other_id=None):
    event(job_id, 'DUPLICADO', reason, duplicate_of=other_id)
    raise DuplicateMaterialError(reason)


def claim_publication(job, dest):
    """Reserva atomicamente o nome no SQLite antes de iniciar cópia para Floripa."""
    key = media_key(job['source_path'])
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute('SELECT * FROM media_transfers WHERE media_key=?',(key,)).fetchone()
        if existing:
            raise DuplicateMaterialError(f"Nome '{key}' reservado ou entregue pelo ID {existing['job_id']} ({existing['status']}).")
        other = db.execute('''SELECT id,status FROM jobs WHERE media_key=? AND id<>?
                    AND status!='DUPLICADO' ORDER BY id LIMIT 1''', (key,job['id'])).fetchone()
        if other:
            raise DuplicateMaterialError(f"Nome '{key}' já cadastrado pelo ID {other['id']} ({other['status']}).")
        found = matching_files(dest, Path(job['source_path']).stem)
        if found:
            raise DuplicateMaterialError('Destino já contém: ' + ', '.join(found))
        db.execute('INSERT INTO media_transfers(media_key,job_id,status,updated_at) VALUES(?,?,?,?)',
                   (key,job['id'],'RESERVADO',utcnow()))


def finish_publication(job, output_path, xml_path, digest, xml_digest):
    with db_connection() as db:
        cursor=db.execute('''UPDATE media_transfers SET status='CONCLUIDO', output_path=?,
            xml_path=?,output_sha256=?,xml_sha256=?,verified_at=?,updated_at=?
            WHERE media_key=? AND job_id=? AND status='RESERVADO' ''',
            (output_path,xml_path,digest,xml_digest,utcnow(),utcnow(),
             media_key(job['source_path']),job['id']))
        if cursor.rowcount != 1:
            raise RuntimeError('Registro reservado ausente ou alterado. Exige conferência manual.')


def release_failed_publication(job, dest):
    # Reabre somente se nenhum nome final ficou na pasta. Caso contrário bloqueia.
    if matching_files(dest,Path(job['source_path']).stem):
        with db_connection() as db:
            db.execute('''UPDATE media_transfers SET status='PENDENTE_REVISAO',updated_at=?
                        WHERE media_key=? AND job_id=? AND status='RESERVADO' ''',
                        (utcnow(),media_key(job['source_path']),job['id']))
        return
    with db_connection() as db:
        db.execute('''DELETE FROM media_transfers WHERE media_key=? AND job_id=?
                    AND status='RESERVADO' ''', (media_key(job['source_path']),job['id']))


def event(job_id, status, message='', **kwargs):
    if status not in VALID_STATES:
        raise ValueError(status)
    fields = dict(status=status, message=str(message), updated_at=utcnow(), **kwargs)
    changes = ', '.join(f'{key}=?' for key in fields)
    with db_connection() as db:
        db.execute(f'UPDATE jobs SET {changes} WHERE id=?', (*fields.values(), job_id))
        db.execute('INSERT INTO events(job_id,moment,status,message) VALUES(?,?,?,?)',
                   (job_id, utcnow(), status, str(message)))


def jobs(limit=None, offset=0):
    with db_connection() as db:
        if limit is None:
            cursor = db.execute('SELECT * FROM jobs ORDER BY id DESC')
        else:
            if int(limit) < 1 or int(offset) < 0:
                raise ValueError('Limite/offset de histórico inválidos')
            cursor = db.execute('SELECT * FROM jobs ORDER BY id DESC LIMIT ? OFFSET ?', (int(limit),int(offset)))
        return [dict(row) for row in cursor]


def job_counts():
    with db_connection() as db:
        return {row['status']:row['amount'] for row in db.execute('SELECT status,COUNT(*) AS amount FROM jobs GROUP BY status')}


def get_job(job_id):
    with db_connection() as db:
        row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise KeyError(job_id)
        return dict(row)


def discover(folder: str, min_age_sec: int = 120):
    base = Path(folder).expanduser()
    if not base.is_dir():
        raise FileNotFoundError(f'Pasta de origem não encontrada: {base}')
    found, skipped = 0, 0
    now = time.time()
    for source in sorted(base.rglob('*')):
        if not source.is_file() or source.suffix.lower() != '.mxf':
            continue
        stat = source.stat()
        if stat.st_size <= 0 or now - stat.st_mtime < min_age_sec:
            skipped += 1
            continue
        key = media_key(source)
        with db_connection() as db:
            prior = db.execute('SELECT id,source_size,source_mtime_ns,status FROM jobs WHERE source_path=?',
                               (str(source),)).fetchone()
            if prior and prior['source_size'] == stat.st_size and prior['source_mtime_ns'] == stat.st_mtime_ns:
                continue
            other = db.execute('''SELECT id FROM jobs WHERE media_key=? AND source_path<>?
                        AND status!='DUPLICADO' ORDER BY id LIMIT 1''', (key,str(source))).fetchone()
            delivered = db.execute('SELECT job_id FROM media_transfers WHERE media_key=?',(key,)).fetchone()
            duplicate_id = delivered['job_id'] if delivered else (other['id'] if other else None)
            duplicate = delivered is not None or other is not None
            if prior and prior['status'] in ('CONCLUIDO','PREPARADO'):
                # Uma alteração no arquivo de origem não pode desfazer entregas concluídas.
                continue
            if prior:
                db.execute('''UPDATE jobs SET source_size=?,source_mtime_ns=?,duration=NULL,
                            mark_in=NULL,mark_out=NULL,confidence=NULL,status=?,message=?,
                            media_key=?,duplicate_of=?,output_path=NULL,xml_path=NULL,
                            output_sha256=NULL,updated_at=? WHERE id=?''',
                           (stat.st_size,stat.st_mtime_ns,
                            'DUPLICADO' if duplicate else 'NOVO',
                            f'Nome duplicado; ID original {duplicate_id}' if duplicate else 'Arquivo alterado: reanálise necessária',
                            key,duplicate_id,utcnow(),prior['id']))
            else:
                db.execute('''INSERT INTO jobs
                    (source_path,source_size,source_mtime_ns,created_at,updated_at,media_key,status,message,duplicate_of)
                    VALUES(?,?,?,?,?,?,?,?,?)''',
                   (str(source),stat.st_size,stat.st_mtime_ns,utcnow(),utcnow(),key,
                    'DUPLICADO' if duplicate else 'NOVO',
                    f'Nome duplicado; ID original {duplicate_id}' if duplicate else '',duplicate_id))
        found += 1
    return found, skipped


def run_probe(path):
    cmd = ['ffprobe','-v','error','-show_format','-show_streams','-of','json',str(path)]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace',
                       creationflags=SUBPROCESS_FLAGS, timeout=60)
    if p.returncode:
        raise RuntimeError(f'ffprobe falhou: {p.stderr[-1200:]}')
    return json.loads(p.stdout)


def duration_from_probe(probe):
    fmt_duration = probe.get('format', {}).get('duration')
    if fmt_duration is not None and float(fmt_duration) > 0:
        return float(fmt_duration)
    durations = [float(s['duration']) for s in probe.get('streams', []) if s.get('duration')]
    if durations:
        return max(durations)
    raise ValueError('Duração do MXF não identificada')


def video_duration_from_probe(probe):
    """Fim exclusivo do vídeo; áudio/container mais longos não acrescentam frames."""
    for stream in probe.get('streams', []):
        if stream.get('codec_type') != 'video':
            continue
        try:
            value = float(Fraction(stream['duration_ts']) * Fraction(stream['time_base']))
            if math.isfinite(value) and value > 0:
                return value
        except (KeyError, ValueError, TypeError, ZeroDivisionError):
            pass
        try:
            value = float(stream['duration'])
            if math.isfinite(value) and value > 0:
                return value
        except (KeyError, ValueError, TypeError):
            pass
    return duration_from_probe(probe)


def fps_from_probe(probe):
    videos = [s for s in probe['streams'] if s['codec_type'] == 'video']
    if not videos:
        raise ValueError('MXF sem vídeo')
    rate = videos[0].get('avg_frame_rate') or videos[0].get('r_frame_rate') or '30000/1001'
    if rate == '0/0':
        rate = videos[0].get('r_frame_rate', '30000/1001')
    return rate, Fraction(rate)


def black_intervals(path, duration, settings):
    return scan_video(path, duration, Fraction(settings.get('analysis_fps', '30000/1001')), settings)['black_intervals']


def detect_marks(duration, fps, intervals, settings=None, slate_intervals=None, exact_intervals=False):
    """Resolve a entrada pela claquete/black e o final pelo black no EOF.

    A duração do conteúdo e a presença de black final não validam a entrada.
    slate_intervals=None mantém compatibilidade com análise só por black;
    uma lista explícita exige evidência estática da claquete antes do black.
    """
    settings=settings or {}
    expected=float(settings.get('black_expected_sec',5.0))
    tolerance=float(settings.get('black_tolerance_sec',0.6))
    f=float(fps)
    if not math.isfinite(f) or f <= 0 or not math.isfinite(duration) or duration <= 0:
        raise ValueError('FPS ou duração inválidos')
    if not math.isfinite(expected) or not math.isfinite(tolerance) or expected <= 0 or tolerance < 0:
        raise ValueError('Duração esperada/tolerância de black inválida')
    min_commercial=float(settings.get('commercial_min_sec',4.5))
    separator_min=float(settings.get('black_separator_min_sec',1.0))
    separator_max=float(settings.get('black_separator_max_sec',6.0))
    slate_min=float(settings.get('slate_min_sec',.5))
    slate_start_tolerance=float(settings.get('slate_start_tolerance_frames',2.))/f
    slate_boundary_tolerance=float(settings.get('slate_boundary_tolerance_frames',2.))/f
    if not all(math.isfinite(value) for value in (separator_min,separator_max,min_commercial,slate_min,slate_start_tolerance,slate_boundary_tolerance)) or not (0 < separator_min <= separator_max) or min(min_commercial,slate_min) <= 0 or min(slate_start_tolerance,slate_boundary_tolerance) < 0:
        raise ValueError('Limites de black separador/claquete inválidos')
    head_limit=float(settings.get('opening_scan_sec',30.0))
    variable_head_limit=min(head_limit,float(settings.get('variable_opening_scan_sec',10.0)))
    intervals=sorted((max(0.,s),min(duration,e)) for s,e in intervals
                     if math.isfinite(s) and math.isfinite(e) and min(duration,e)>max(0.,s))
    heads=[]
    for s,e in intervals:
        if s < .10 or s > head_limit or e >= duration:
            continue
        if slate_intervals is not None:
            confirmed=any(0 <= start <= slate_start_tolerance and end-start >= slate_min
                          and abs(end-s) <= slate_boundary_tolerance
                          for start,end in slate_intervals)
            if confirmed:
                heads.append((s,e))
        elif (abs(e-s-expected)<=tolerance
              or (s <= variable_head_limit and separator_min <= e-s <= separator_max)):
            heads.append((s,e))
    # Metadados black_end distinguem saída de black de um black aberto no EOF.
    # Relatórios legados, sem metadados, usam o último PTS e precisam tolerância.
    eof_tolerance=1e-6 if exact_intervals else 1.5/f
    tails=[(s,e) for s,e in intervals
           if e >= duration-eof_tolerance]
    candidates=[]
    for a,b in heads:
        ending=[(s,e) for s,e in tails if s>=b+min_commercial]
        c,d=ending[-1] if ending else (duration,duration)
        length=c-b
        if length >= min_commercial:
            unexpected_black=any(x>=b+1/f and y<=c-1/f and y-x>=expected-tolerance
                                 for x,y in intervals)
            candidates.append((a,b,c,d,unexpected_black))
    if candidates:
        a,b,c,d,unexpected_black=min(candidates,key=lambda item:item[0])
        # blackdetect relata limites de frames com casas decimais reduzidas.
        # ceil/floor podem avançar/perder um frame por arredondamento do log.
        mark_in=round(b*f)/f
        mark_out=round(c*f)/f
        if mark_out > mark_in:
            unambiguous=len(candidates)==1 and (slate_intervals is not None or not unexpected_black)
            confidence='ALTA' if unambiguous else 'BAIXA'
            ending=(f'black final {duration-c:.2f}s removido' if d>c
                    else 'sem black final detectado')
            return (mark_in,mark_out,confidence,
                    (f'Claquete estática confirmada até {a:.3f}s' if slate_intervals is not None
                     else f'Trecho de claquete inferido até {a:.3f}s')
                    + f'; black separador {b-a:.3f}s; {ending}. '
                    + ('Pontos identificados.' if confidence=='ALTA' else 'Entrada ambígua; corte automático bloqueado.'))
    # Sem o layout esperado: NÃO dar confiança alta para outro corte.
    edges=[(0.0,0.0),*intervals,(duration,duration)]
    segments=[]
    for i in range(len(edges)-1):
        start,end=edges[i][1],edges[i+1][0]
        if end-start>=.12:
            segments.append((start,end))
    if not segments:
        return 0.,duration,'BAIXA','Sem conteúdo confiável; corte automático bloqueado.'
    start,end=max(segments,key=lambda v:v[1]-v[0])
    mark_in=math.ceil(max(0,start)*f-1e-4)/f
    mark_out=math.floor(min(duration,end)*f+1e-4)/f
    if mark_out<=mark_in:
        mark_in,mark_out=0.,duration
    details='; '.join(f'{s:.3f}–{e:.3f}s ({e-s:.3f}s)' for s,e in intervals) or 'nenhum'
    return mark_in,mark_out,'BAIXA',(
        f'Separador de claquete não confirmado; corte automático bloqueado. '
        + ('Não foi confirmada uma claquete estática terminando no início do black. '
           if slate_intervals is not None else 'Nenhum separador inequívoco na janela de abertura. ')
        + f'Intervalos: {details}.')


def analyze(job_id, settings=None):
    settings = settings or config()
    job = get_job(job_id)
    if job['status'] in ('CONCLUIDO','DUPLICADO','PREPARADO'):
        raise ValueError('Material concluído, duplicado ou já preparado; não reanalisar sem auditoria.')
    path = Path(job['source_path'])
    if not path.exists():
        raise FileNotFoundError(path)
    stat = path.stat()
    if stat.st_size != job['source_size'] or stat.st_mtime_ns != job['source_mtime_ns']:
        raise RuntimeError('Arquivo de origem foi modificado desde a descoberta. Refaça a varredura.')
    probe = run_probe(path)
    duration = video_duration_from_probe(probe)
    fps_text, fps = fps_from_probe(probe)
    scan = scan_video(path, duration, fps, settings)
    intervals = scan['black_intervals']
    slate_intervals = scan['slate_intervals']
    mark_in, mark_out, confidence, msg = detect_marks(duration, fps, intervals, settings,
        slate_intervals=slate_intervals,exact_intervals=scan.get('exact_intervals',False))
    details='; '.join(f'{s:.3f}–{e:.3f}s ({e-s:.3f}s)' for s,e in intervals)
    msg += f' Blacks={len(intervals)} [{details}]; trecho={mark_out - mark_in:.3f}s'
    event(job_id, 'REVISAR', msg, duration=duration, fps=fps_text,
          mark_in=mark_in, mark_out=mark_out, confidence=confidence,
          black_intervals_json=json.dumps(intervals),slate_intervals_json=json.dumps(slate_intervals),
          analysis_signature=analysis_signature(settings))
    return get_job(job_id)


def approve(job_id, mark_in, mark_out, automatic=False):
    job = get_job(job_id)
    if job['status'] in ('PROCESSANDO','VALIDANDO','TRANSFERINDO','PREPARADO','CONCLUIDO','DUPLICADO'):
        raise ValueError('Não é permitido aprovar neste estado')
    if job['duration'] is None:
        raise ValueError('Execute a análise primeiro')
    if automatic and job['confidence'] != 'ALTA':
        raise ValueError('Corte automático exige análise de alta confiança')
    if not (0 <= mark_in < mark_out <= job['duration'] + .02):
        raise ValueError('Mark In / Mark Out fora da duração do arquivo')
    fps = float(Fraction(job['fps']))
    if abs(mark_in*fps-round(mark_in*fps)) > .05 or abs(mark_out*fps-round(mark_out*fps)) > .05:
        raise ValueError('Pontos devem estar alinhados aos frames. Use segundos com precisão.')
    event(job_id,'APROVADO','Pontos aceitos automaticamente pela detecção de alta confiança.' if automatic
          else 'Pontos aprovados pelo operador', mark_in=mark_in, mark_out=mark_out)


def audio_input_channels(probe):
    return [(stream_idx, ch_idx) for stream_idx, stream in enumerate(
        [s for s in probe['streams'] if s['codec_type'] == 'audio'])
        for ch_idx in range(int(stream.get('channels') or 0))]


def filters_for_audio(probe, settings, length):
    """Remapeia para 16 mono, normalizando pares de saída com ganho conjunto.

    'auto' utiliza o canal de entrada correspondente ou silêncio se inexistente.
    Ex. 0:0 / 0:1 / silence; sem inventar canais de audiodescrição.
    Os pares 1/2, 3/4 etc. compartilham loudnorm mesmo quando a origem usa
    streams mono separados: o balanço L/R é preservado dentro de cada par.
    """
    streams = [s for s in probe['streams'] if s['codec_type'] == 'audio']
    map_entries = settings['audio_channel_map']
    if len(map_entries) != 16:
        raise ValueError('audio_channel_map deve ter EXATAMENTE 16 canais')
    if not math.isfinite(length) or length <= 0:
        raise ValueError('Duração de áudio inválida')
    sample_count=round(length*48000)
    refs = []
    source_channels=audio_input_channels(probe)
    for pos, item in enumerate(map_entries):
        if item == 'auto':
            refs.append(source_channels[pos] if pos < len(source_channels) else None)
            continue
        if item == 'silence':
            refs.append(None)
            continue
        match = re.fullmatch(r'(\d+):(\d+)', str(item))
        if not match:
            raise ValueError(f'Canal de áudio inválido: {item!r}')
        s, c = map(int, match.groups())
        if s >= len(streams) or c >= int(streams[s].get('channels', 0)):
            raise ValueError(f'Canal {item} não existe no MXF de origem')
        refs.append((s,c))
    filter_parts, labels = [], {}
    for s in sorted({x[0] for x in refs if x is not None}):
        n = sum(1 for x in refs if x is not None and x[0] == s)
        chain = f'[0:a:{s}]aresample=48000'
        if n > 1:
            outs = [f'[split_{s}_{i}]' for i in range(n)]
            filter_parts.append(chain + f',asplit={n}' + ''.join(outs))
            labels[s] = [f'split_{s}_{i}' for i in range(n)]
        else:
            filter_parts.append(chain + f'[split_{s}_0]')
            labels[s] = [f'split_{s}_0']
    usage = {s:0 for s in labels}
    for i, ref in enumerate(refs):
        if ref is None:
            filter_parts.append(f'anullsrc=r=48000:cl=mono,atrim=end_sample={sample_count},asetpts=N/SR/TB[raw{i}]')
        else:
            s, c = ref
            label = labels[s][usage[s]]
            usage[s] += 1
            filter_parts.append(f'[{label}]pan=mono|c0=c{c},asetpts=N/SR/TB,apad,atrim=end_sample={sample_count}[raw{i}]')
    for i in range(0,16,2):
        if settings.get('normalize_audio',False) and (refs[i] is not None or refs[i+1] is not None):
            loudness=(f"loudnorm=I={float(settings.get('lufs_target',-23))}:"
                      f"LRA={float(settings.get('lra_target',7))}:"
                      f"TP={float(settings.get('true_peak_db',-2))}")
            filter_parts.append(
                f'[raw{i}][raw{i+1}]join=inputs=2:channel_layout=stereo:map=0.0-FL|1.0-FR,'
                # loudnorm faz flush em blocos de 100ms. Uma entrada fracionária
                # (ex. 5.005s/29.97fps) pode perder amostras no flush do FFmpeg 9.
                # Completa SOMENTE além do Mark Out e recorta depois em amostras.
                f'asetnsamples=n=4800:p=1,{loudness},aresample=48000,'
                f'asetpts=N/SR/TB,apad,atrim=end_sample={sample_count},'
                f'asplit=2[pair{i}l][pair{i}r]')
            filter_parts.append(f'[pair{i}l]pan=mono|c0=c0[a{i}]')
            filter_parts.append(f'[pair{i}r]pan=mono|c0=c1[a{i+1}]')
        else:
            filter_parts.append(f'[raw{i}]anull[a{i}]')
            filter_parts.append(f'[raw{i+1}]anull[a{i+1}]')
    return ';'.join(filter_parts)


def sha256(path, callback=None):
    digest = hashlib.sha256()
    total = path.stat().st_size
    done = 0
    with path.open('rb') as f:
        for block in iter(lambda: f.read(8*1024*1024), b''):
            digest.update(block)
            done += len(block)
            if callback and total:
                callback(done/total)
    return digest.hexdigest()


def xml_for_job(job, output_probe, digest, xml_path, settings=None):
    """Gera manifesto EVA ou aplica um template real do importador configurado."""
    settings = dict(settings or config())
    return write_xml(job, video_duration_from_probe(output_probe), digest, xml_path, settings)


def copy_with_hash(src, dst, expected_hash, callback=None):
    digest = hashlib.sha256()
    total, sent = src.stat().st_size, 0
    with src.open('rb') as reader, dst.open('xb') as writer:
        for buf in iter(lambda: reader.read(4*1024*1024), b''):
            writer.write(buf)
            digest.update(buf)
            sent += len(buf)
            if callback and total:
                callback(sent/total)
        writer.flush()
        os.fsync(writer.fileno())
    if digest.hexdigest() != expected_hash or dst.stat().st_size != total:
        raise RuntimeError('Checksum SHA-256 ou tamanho divergentes na transferência')


def transfer(job, source_mxf, source_xml, settings, digest, callback, dest_override=None):
    is_floripa=dest_override is None
    if is_floripa and not settings.get('enable_publish_to_floripa',False):
        raise PermissionError('Publicação Floripa desabilitada em config.json.')
    if is_floripa and not (settings.get('xml_homologated',False)
                           and settings.get('audio_layout_homologated',False)):
        raise PermissionError('XML e ordem das 16 pistas precisam de homologação explícita.')
    dest=Path(dest_override or settings['floripa_folder']).expanduser()
    if not dest.is_dir():
        raise NotADirectoryError(f'Pasta de destino não encontrada: {dest}')
    final_mxf=dest/source_mxf.name
    final_xml=dest/(source_mxf.stem+'.xml')
    if matching_files(dest,source_mxf.stem):
        raise DuplicateMaterialError(f'Nome já existe na pasta {dest}; envio bloqueado.')
    xml_digest=sha256(source_xml)
    mxf_bytes,xml_bytes=source_mxf.stat().st_size,source_xml.stat().st_size
    handoff=settings.get('delivery_retention_policy','require_present')=='handoff'
    if is_floripa:
        claim_publication(job,dest)
    # Lock no compartilhamento impede corrida entre instâncias que executam este programa.
    lock=dest/('.'+media_key(source_mxf)+'.ingest.lock')
    partial_mxf=dest/(source_mxf.name+'.'+uuid.uuid4().hex+'.part')
    partial_xml=dest/(source_mxf.stem+'.xml.'+uuid.uuid4().hex+'.part')
    locked=False
    try:
        with lock.open('x',encoding='utf-8') as lock_file:
            lock_file.write(f"job={job['id']}\ntime={utcnow()}\npid={os.getpid()}\nhost={socket.gethostname()}\n")
            lock_file.flush()
            os.fsync(lock_file.fileno())
        locked=True
        found=matching_files(dest,source_mxf.stem)
        if found:
            raise DuplicateMaterialError('Pasta recebeu um nome duplicado: '+', '.join(found))
        copy_with_hash(source_mxf,partial_mxf,digest,
                       lambda p:callback(.76+.17*p,'Transferindo MXF (.part)'))
        copy_with_hash(source_xml,partial_xml,xml_digest,
                       lambda p:callback(.93+.02*p,'Transferindo XML (.part)'))
        ET.parse(partial_xml)
        if handoff:
            # O importador pode consumir os nomes finais imediatamente.
            # Confira bytes gravados ANTES de expor o gatilho de ingestão.
            if (partial_mxf.stat().st_size!=mxf_bytes or partial_xml.stat().st_size!=xml_bytes
                    or sha256(partial_mxf)!=digest or sha256(partial_xml)!=xml_digest):
                raise RuntimeError('Integridade dos arquivos .part divergente antes da publicação')
            with db_connection() as db:
                db.execute('''UPDATE automatic_deliveries SET publication_started_at=?,updated_at=?
                    WHERE media_key=? AND destination_key=? AND job_id=? AND status='RESERVADO' ''',
                    (utcnow(),utcnow(),media_key(job['source_path']),destination_key(dest),job['id']))
        # A ordem depende de qual nome final dispara a ingestão do importador.
        # Em NTFS (Windows) rename não sobrescreve; em Linux prefira link sem replace.
        def publish_no_replace(src,target):
            if os.name=='nt':
                os.rename(src,target)
            else:
                os.link(src,target)  # hardlink atômico; falha se destino já existe
                src.unlink()
        order = settings.get('publication_order', 'xml_first')
        if order not in ('xml_first', 'mxf_first'):
            raise ValueError('publication_order inválida')
        pairs = ((partial_xml,final_xml),(partial_mxf,final_mxf))
        if order == 'mxf_first':
            pairs = tuple(reversed(pairs))
        for partial,final in pairs:
            publish_no_replace(partial,final)
        if handoff:
            complete_automatic_delivery(job,dest)
            callback(1.,'MXF/XML verificados e publicados; confirmação do cadastro depende do importador')
        else:
            # Pastas de retenção permitem releitura após publicar os nomes finais.
            if (final_mxf.stat().st_size!=mxf_bytes or final_xml.stat().st_size!=xml_bytes
                    or sha256(final_mxf)!=digest or sha256(final_xml)!=xml_digest):
                raise RuntimeError('Integridade pós-publicação divergente (MXF/XML)')
            ET.parse(final_xml)
            callback(1.,'MXF e XML finais verificados por SHA-256')
        if is_floripa:
            finish_publication(job,str(final_mxf),str(final_xml),digest,xml_digest)
        return str(final_mxf),str(final_xml),xml_digest,mxf_bytes,xml_bytes
    except Exception:
        # Não remover arquivos FINAIS publicados: evitar duplicidade na próxima tentativa.
        # A reserva fica PENDENTE_REVISAO se existir qualquer arquivo final.
        if is_floripa:
            release_failed_publication(job,dest)
        raise
    finally:
        for p in (partial_mxf,partial_xml):
            if p.exists():
                p.unlink()
        if locked and lock.exists():
            lock.unlink()


def destination_key(dest):
    return os.path.normcase(str(Path(dest).expanduser().resolve()))


def check_material_identity(job, dest, db=None):
    """Impede outro nome de arquivo de cadastrar o mesmo CM ou IDK7 no destino."""
    cm,idk7 = material_identifiers(job['source_path'])
    if not cm:
        return
    def check(connection):
        row = connection.execute('''SELECT media_key,cm,idk7,job_id FROM automatic_deliveries
            WHERE destination_key=? AND (cm=? OR idk7=?) AND media_key<>? LIMIT 1''',
            (destination_key(dest),cm,idk7,media_key(job['source_path']))).fetchone()
        if row:
            raise MaterialIdentityConflict(f"CM/IDK7 já entregue ou reservado por outro nome: {row['cm']} / {row['idk7']} (ID {row['job_id']}).")
    if db is not None:
        check(db)
    else:
        with db_connection() as connection:
            check(connection)


def record_automatic_delivery(job, dest, source_mxf, source_xml, digest, xml_digest):
    """Registra os hashes antes de publicar para reconciliar uma interrupção."""
    with db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        check_material_identity(job, dest, db)
        existing = db.execute('SELECT status FROM automatic_deliveries WHERE media_key=? AND destination_key=?',
                              (media_key(job['source_path']), destination_key(dest))).fetchone()
        if existing:
            raise DuplicateMaterialError('Existe entrega registrada neste destino; reconcilie antes de repetir.')
        db.execute('''INSERT INTO automatic_deliveries
            (media_key,destination_key,job_id,source_size,source_mtime_ns,output_path,xml_path,
             output_sha256,xml_sha256,delivered_mxf_bytes,delivered_xml_bytes,status,updated_at,cut_video,cm,idk7)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (media_key(job['source_path']), destination_key(dest), job['id'], job['source_size'],
             job['source_mtime_ns'], str(Path(dest) / source_mxf.name), str(Path(dest) / source_xml.name),
             digest, xml_digest, source_mxf.stat().st_size, source_xml.stat().st_size, 'RESERVADO', utcnow(),
             int(job.get('cut_video',1)),*material_identifiers(job['source_path'])))


def complete_automatic_delivery(job, dest):
    with db_connection() as db:
        db.execute('''INSERT INTO delivery_events
            (job_id,moment,media_key,destination_key,status,output_path,xml_path,output_sha256,xml_sha256,cut_video,cm,idk7)
            SELECT job_id,?,media_key,destination_key,'CONCLUIDO',output_path,xml_path,output_sha256,xml_sha256,cut_video,cm,idk7
            FROM automatic_deliveries WHERE media_key=? AND destination_key=? AND status!='CONCLUIDO' ''',
                   (utcnow(),media_key(job['source_path']),destination_key(dest)))
        db.execute("UPDATE automatic_deliveries SET status='CONCLUIDO',updated_at=? WHERE media_key=? AND destination_key=?",
                   (utcnow(), media_key(job['source_path']), destination_key(dest)))


def monitor_event(status, message):
    with db_connection() as db:
        db.execute('INSERT INTO monitor_events(moment,status,message) VALUES(?,?,?)', (utcnow(),status,str(message)))


def ensure_output_capacity(work, destination, length, profile):
    """Confere espaço com margem para o MXF temporário e a cópia final."""
    bitrate = 50000000 if profile == 'xdcam_hd422' else 8000000
    estimated = math.ceil(length * (bitrate/8 + 16*48000*3) * 1.15)
    margin = 64*1024*1024
    work_free = shutil.disk_usage(work).free
    if destination.is_dir():
        same_volume = work.stat().st_dev == destination.stat().st_dev
        destination_free = shutil.disk_usage(destination).free
        required = 2*estimated+margin if same_volume else estimated+margin
        if destination_free < required:
            raise OSError(f'Espaço insuficiente no destino para gerar e entregar o MXF; necessário aproximadamente {required//1048576} MiB.')
    if work_free < estimated+margin:
        raise OSError(f'Espaço insuficiente na pasta de trabalho; necessário aproximadamente {(estimated+margin)//1048576} MiB.')


def process(job_id, callback=lambda x,y:None, publish=False, settings=None, automatic=False):
    job = get_job(job_id)
    if job['status'] != 'APROVADO':
        raise ValueError('Apenas materiais aprovados podem ser processados')
    settings = dict(settings or config())
    cut_video = cut_enabled(settings)
    validate_xml_settings(settings)
    cm,idk7 = material_identifiers(job['source_path'], required=settings.get('xml_profile','eva')=='smartware_template')
    job.update(cut_video=int(cut_video),cm=cm,idk7=idk7)
    if automatic:
        settings['local_output_folder'] = settings['destination_folder']
        publish = False
    planned_dest=Path(settings['floripa_folder'] if publish else settings['local_output_folder']).expanduser()
    # Bloqueia material repetido no cadastro, ledger de envio ou pasta.
    if automatic:
        reason,other_id = None,None
    elif planned_dest.is_dir():
        reason,other_id=duplicate_in_history(job,planned_dest)
    else:
        reason,other_id=duplicate_in_history(job)
    if reason:
        reject_duplicate(job_id,reason,other_id)
    if automatic:
        if not planned_dest.is_dir():
            raise NotADirectoryError(f'Destino indisponível antes da codificação: {planned_dest}')
        check_material_identity(job, planned_dest)
    input_path = Path(job['source_path'])
    stat = input_path.stat()
    if stat.st_size != job['source_size'] or stat.st_mtime_ns != job['source_mtime_ns']:
        raise RuntimeError('Origem modificada: nova análise e aprovação necessárias.')
    if publish and not settings.get('enable_publish_to_floripa', False):
        raise PermissionError('Publicação na Floripa está desabilitada em config.json')
    work = Path(settings['working_folder']).expanduser()
    work.mkdir(parents=True,exist_ok=True)
    # Arquivo original NUNCA é alterado.
    probe = run_probe(input_path)
    video = next(s for s in probe['streams'] if s['codec_type']=='video')
    source_duration = video_duration_from_probe(probe)
    length = job['mark_out'] - job['mark_in'] if cut_video else source_duration
    if not (job['mark_in'] is not None and job['mark_out'] is not None
            and 0 <= job['mark_in'] < job['mark_out'] <= source_duration+.0001):
        raise ValueError('Marcas fora do vídeo de origem')
    profile = settings.get('video_profile', 'xdcam_hd422')
    if profile == 'xdcam_hd422':
        if int(video.get('width',0)) != 1920 or int(video.get('height',0)) != 1080:
            raise ValueError('Perfil XDCAM HD422 exige origem 1920x1080. Use outro perfil/teste homologado.')
        rate = Fraction(job['fps'])
        if abs(float(rate) - 30000/1001) > .02:
            raise ValueError('Perfil XDCAM HD422 60i exige 30000/1001 fps.')
        if video.get('field_order') not in ('tt','tb','top','unknown',None):
            raise ValueError(f"Ordem de campo não homologada: {video.get('field_order')}")
    ensure_output_capacity(work,planned_dest,length,profile)
    copy_video = not cut_video and video.get('codec_name') == 'mpeg2video' and video.get('pix_fmt') == 'yuv422p'
    if profile == 'xdcam_hd422':
        copy_video = copy_video and int(video.get('bit_rate') or 0) == 50000000 and video.get('field_order') == 'tt'
    mode_message = ('Recortando vídeo e remapeando áudio' if cut_video else
                    'Preservando vídeo integral e remapeando áudio; marcas serão enviadas no XML')
    event(job_id,'PROCESSANDO',mode_message,cut_video=int(cut_video),cm=cm,idk7=idk7,output_duration=length)
    stem = input_path.stem
    with tempfile.TemporaryDirectory(prefix='mxf_', dir=work) as tmp:
        local_mxf = Path(tmp) / (stem + '.mxf')
        local_xml = Path(tmp) / (stem + '.xml')
        cmd = ['ffmpeg','-hide_banner','-nostdin','-y','-loglevel','error',
               '-progress','pipe:1','-stats_period','0.4']
        if cut_video:
            cmd.extend(['-ss',f"{job['mark_in']:.6f}"])
        cmd.extend(['-i',str(input_path),
               '-t',f'{length:.6f}',
               '-filter_complex',filters_for_audio(probe, settings, length),
               '-map','0:v:0'])
        for i in range(16):
            cmd.extend(['-map',f'[a{i}]'])
        if copy_video:
            cmd.extend(['-c:v','copy'])
        elif profile == 'xdcam_hd422':
            cmd.extend(['-c:v','mpeg2video','-b:v','50M','-minrate','50M',
                        '-maxrate','50M','-bufsize','17825792','-pix_fmt','yuv422p',
                        '-flags','+ildct+ilme','-vf','setfield=tff','-field_order','tt','-r','30000/1001',
                        '-aspect','16:9','-g','15','-bf','2'])
        elif profile == 'teste':
            cmd.extend(['-c:v','mpeg2video','-b:v','8M','-pix_fmt','yuv422p',
                        '-r',job['fps'],'-g','12'])
        else:
            raise ValueError('video_profile inválido')
        cmd.extend(['-c:a','pcm_s24le','-ar','48000','-ac','1',
                    '-f','mxf',str(local_mxf)])
        run_transcode(cmd,length,callback,float(settings.get('ffmpeg_stall_timeout_sec',180)))
        event(job_id,'VALIDANDO','Conferindo saída técnica')
        output_probe = run_probe(local_mxf)
        out_video = [s for s in output_probe['streams'] if s['codec_type']=='video']
        out_audio = [s for s in output_probe['streams'] if s['codec_type']=='audio']
        if len(out_video) != 1 or len(out_audio) != 16:
            raise RuntimeError(f'Saída rejeitada: {len(out_video)} vídeos, {len(out_audio)} áudios; esperado 1 + 16.')
        if any(s.get('channels') != 1 or s.get('sample_rate') != '48000' or s.get('codec_name') != 'pcm_s24le' for s in out_audio):
            raise RuntimeError('Saída de áudio não atende 16 mono PCM 24-bit/48k')
        rate = Fraction(job['fps'])
        expected_frames = round(length * float(rate))
        output_video = out_video[0]
        if output_video.get('duration_ts') is None or not output_video.get('time_base'):
            raise RuntimeError('Não foi possível verificar a quantidade de frames do MXF.')
        actual_frames = Fraction(output_video['duration_ts']) * Fraction(output_video['time_base']) * rate
        if abs(float(actual_frames)-expected_frames) > .01:
            raise RuntimeError(f'Quantidade de frames divergente: {float(actual_frames):.3f}; esperado {expected_frames}.')
        if output_video.get('codec_name') != 'mpeg2video' or output_video.get('pix_fmt') != 'yuv422p':
            raise RuntimeError('Codec/formato de vídeo divergente do perfil de saída.')
        if profile == 'xdcam_hd422' and (output_video.get('width') != 1920 or output_video.get('height') != 1080
                                          or output_video.get('field_order') != 'tt'):
            raise RuntimeError('MXF de saída não corresponde ao perfil XDCAM HD422 1080i TFF.')
        out_dur = duration_from_probe(output_probe)
        if abs(out_dur - length) > .5/float(rate):
            raise RuntimeError(f'Duração de saída não confere: {out_dur:.3f}s / {length:.3f}s')
        stat = input_path.stat()
        if stat.st_size != job['source_size'] or stat.st_mtime_ns != job['source_mtime_ns']:
            raise RuntimeError('Origem modificada durante o processamento; a saída foi descartada.')
        callback(.70,'Calculando SHA-256')
        digest = sha256(local_mxf, lambda x:callback(.70+.06*x,'Verificando arquivo'))
        xml_for_job(job, output_probe, digest, local_xml, settings)
        event(job_id,'TRANSFERINDO','Enviando arquivos .part e verificando SHA-256')
        if publish:
            target_mxf,target_xml,xml_digest,mxf_bytes,xml_bytes = transfer(job,local_mxf,local_xml,settings,digest,callback)
            event(job_id,'CONCLUIDO','Transferência íntegra na Floripa confirmada',
                  output_path=target_mxf,xml_path=target_xml,output_sha256=digest,
                  xml_sha256=xml_digest,delivery_verified_at=utcnow(),
                  delivered_mxf_bytes=mxf_bytes,delivered_xml_bytes=xml_bytes)
        else:
            local_dest = Path(settings['local_output_folder']).expanduser()
            if not automatic:
                local_dest.mkdir(parents=True,exist_ok=True)
            if automatic:
                record_automatic_delivery(job, local_dest, local_mxf, local_xml, digest, sha256(local_xml))
            target_mxf,target_xml,xml_digest,mxf_bytes,xml_bytes = transfer(job,local_mxf,local_xml,settings,digest,
                        callback,dest_override=local_dest)
            if automatic:
                complete_automatic_delivery(job, local_dest)
            event(job_id,'CONCLUIDO' if automatic else 'PREPARADO',
                  ('MXF e XML entregues e verificados; cadastro Smartware não confirmado' if settings.get('xml_profile','eva')=='smartware_template'
                   else 'MXF e XML EVA entregues e verificados no destino') if automatic else
                  'MXF e XML preparados na pasta local',
                  output_path=target_mxf,xml_path=target_xml,output_sha256=digest,
                  xml_sha256=xml_digest,
                  delivered_mxf_bytes=mxf_bytes,delivered_xml_bytes=xml_bytes,
                  delivery_verified_at=utcnow(),progress=1.)
        return get_job(job_id)


def publish_prepared(job_id, callback=lambda x,y:None):
    job = get_job(job_id)
    if job['status'] != 'PREPARADO':
        raise ValueError('Material precisa estar PREPARADO na pasta local')
    settings = config()
    if not (settings.get('enable_publish_to_floripa', False) and
            settings.get('xml_homologated', False) and
            settings.get('audio_layout_homologated', False)):
        raise PermissionError('Publicação bloqueada: XML e layout das 16 pistas ainda não homologados.')
    local_mxf = Path(job['output_path'])
    local_xml = Path(job['xml_path'])
    if not local_mxf.is_file() or not local_xml.is_file():
        raise FileNotFoundError('MXF/XML local não encontrado')
    if sha256(local_mxf, lambda p:callback(.1*p,'Revalidando arquivo local')) != job['output_sha256']:
        raise RuntimeError('SHA-256 local diverge do histórico. Recusando publicação.')
    ET.parse(local_xml)
    floripa_dest=Path(settings['floripa_folder']).expanduser()
    reason,other_id=duplicate_in_history(job,floripa_dest)
    if reason:
        reject_duplicate(job_id,reason,other_id)
    event(job_id, 'TRANSFERINDO', 'Publicando material local na pasta Floripa')
    try:
        dest_mxf,dest_xml,xml_digest,mxf_bytes,xml_bytes=transfer(job,local_mxf,local_xml,settings,
                                    job['output_sha256'],callback)
        event(job_id, 'CONCLUIDO', 'Transferência íntegra na Floripa confirmada',
              output_path=dest_mxf,xml_path=dest_xml,xml_sha256=xml_digest,
              delivery_verified_at=utcnow(),delivered_mxf_bytes=mxf_bytes,delivered_xml_bytes=xml_bytes)
    except Exception as exc:
        event(job_id, 'PREPARADO', 'Falha no envio, arquivos locais preservados: '+str(exc))
        raise
    return get_job(job_id)


def export_csv(filepath):
    columns = ['id','source_path','media_key','duplicate_of','destination_folder','duration','mark_in','mark_out',
               'confidence','status','message','output_path','xml_path','output_sha256',
               'xml_sha256','delivery_verified_at','delivered_mxf_bytes','delivered_xml_bytes',
               'cm','idk7','cut_video','output_duration','attempts','error_kind','progress','updated_at']
    with db_connection() as db, open(filepath,'w',newline='',encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f,fieldnames=columns,extrasaction='ignore',delimiter=';')
        writer.writeheader()
        cursor = db.execute('SELECT * FROM jobs ORDER BY id DESC')
        for rows in iter(lambda:cursor.fetchmany(1000),[]):
            writer.writerows(_csv_safe(dict(row)) for row in rows)


def _csv_safe(row):
    return {key: "'" + value if isinstance(value,str) and value.startswith(('=','+','-','@','\t','\r'))
            else value for key,value in row.items()}


def export_logs(filepath):
    """Snapshot consistente escrito em blocos, sem carregar o histórico em memória."""
    target = Path(filepath).expanduser().resolve()
    if target.suffix.lower() != '.zip':
        raise ValueError('A exportação de logs deve usar um arquivo .zip.')
    temporary = target.with_suffix('.'+uuid.uuid4().hex+'.tmp')
    try:
        with db_connection() as db, zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            db.execute('BEGIN')
            for name in ('jobs','events','automatic_deliveries','delivery_events','media_transfers','monitor_events'):
                columns = [row['name'] for row in db.execute(f'PRAGMA table_info({name})')]
                with archive.open(name+'.csv','w',force_zip64=True) as raw:
                    with TextIOWrapper(raw,encoding='utf-8-sig',newline='') as stream:
                        writer = csv.DictWriter(stream,fieldnames=columns,delimiter=';')
                        writer.writeheader()
                        cursor = db.execute(f'SELECT * FROM {name} ORDER BY rowid')
                        for rows in iter(lambda:cursor.fetchmany(1000),[]):
                            writer.writerows(_csv_safe(dict(row)) for row in rows)
        os.replace(temporary,target)
    finally:
        temporary.unlink(missing_ok=True)
