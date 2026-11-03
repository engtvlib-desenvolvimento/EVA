from __future__ import annotations

import ctypes
import os
import socket
import stat as stat_module
import time
import uuid
from pathlib import Path
from threading import Event

import core


class DeliveryConflict(RuntimeError):
    pass


def process_is_alive(pid):
    if pid <= 0:
        return True
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            # ERROR_INVALID_PARAMETER: PID não existe. Acesso negado é inconclusivo.
            return ctypes.get_last_error() != 87
        try:
            exit_code = ctypes.c_ulong()
            kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def remove_abandoned_lock(path):    
    if not path.exists():
        return
    try:
        fields = dict(line.split('=', 1) for line in path.read_text(encoding='utf-8').splitlines() if '=' in line)
        local = fields.get('host') == socket.gethostname()
        dead = local and not process_is_alive(int(fields.get('pid', '0')))
    except (OSError, ValueError):
        dead = False
    if not dead:
        raise DeliveryConflict(f'Pasta ocupada por outra execução ou lock sem identificação: {path.name}')
    path.unlink()


class FolderWatcher:
    def __init__(self, settings, notify=lambda *_: None):
        self.settings = dict(settings)
        self.source = Path(settings['source_folder']).expanduser().resolve()
        self.dest = Path(settings.get('destination_folder') or settings['local_output_folder']).expanduser().resolve()
        self.settings['destination_folder'] = str(self.dest)
        self.work = Path(settings.get('working_folder') or core.HERE / 'trabalho').expanduser().resolve()
        self.notify = notify
        self.observed = {}
        self.verified = {}
        self.stop_event = Event()
        self.last_error = None
        self.last_progress = {}
        self.source_errors = {}
        self.analysis_signature = core.analysis_signature(self.settings)
        self._jobs_by_path = None
        self._jobs_by_key = None
        self._deliveries = None
        self._destination_names = None
        self._validated_parent_pairs = set()

    @staticmethod
    def _path_key(path):
        return os.path.normcase(str(path))

    def _load_scan_snapshot(self):        
        prefix = str(self.source).rstrip('\\/') + os.sep
        escaped_prefix = prefix.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
        with core.db_connection() as db:
            rows = db.execute('''SELECT * FROM jobs
                WHERE source_path LIKE ? ESCAPE '!' OR destination_folder=?''',
                (escaped_prefix, str(self.dest))).fetchall()
            deliveries = db.execute('SELECT * FROM automatic_deliveries WHERE destination_key=?',
                                    (core.destination_key(self.dest),)).fetchall()
        self._jobs_by_path = {}
        self._jobs_by_key = {}
        for row in rows:
            self._cache_job(dict(row))
        self._deliveries = {row['media_key']:dict(row) for row in deliveries}
        self._destination_names = {}
        with os.scandir(self.dest) as entries:
            for entry in entries:
                if self.stop_event.is_set():
                    break
                self._destination_names.setdefault(entry.name.casefold(), []).append(entry.name)

    def _cache_job(self, job):
        if self._jobs_by_path is not None:
            self._jobs_by_path[self._path_key(job['source_path'])] = job
            bucket = self._jobs_by_key.setdefault(job['media_key'], {})
            bucket[job['id']] = job
        return job

    def _refresh_job(self, job_id):
        return self._cache_job(core.get_job(job_id))

    def _matching_files(self, stem):
        if self._destination_names is None:
            return core.matching_files(self.dest, stem)
        return [name for extension in ('.mxf', '.xml')
                for name in self._destination_names.get((stem + extension).casefold(), [])]

    def _remember_output_names(self, job):
        if self._destination_names is not None:
            for field in ('output_path', 'xml_path'):
                if job.get(field):
                    name = Path(job[field]).name
                    self._destination_names[name.casefold()] = [name]

    def run(self, stop_event):
        self.stop_event = stop_event
        lock = Path(core.DB_FILE).with_suffix('.monitor.lock')
        remove_abandoned_lock(lock)
        token = uuid.uuid4().hex
        with lock.open('x', encoding='utf-8') as writer:
            writer.write(f'pid={os.getpid()}\nhost={socket.gethostname()}\ntoken={token}\n')
            writer.flush()
            os.fsync(writer.fileno())
        try:
            core.monitor_event('INICIADO', f'Origem={self.source}; destino={self.dest}')
            self.notify('message', 'Monitorando a pasta; aguardando vídeos estáveis.')
            while not stop_event.is_set():
                try:
                    self.scan_once()
                    self.last_error = None
                except Exception as exc:
                    message = str(exc)
                    if message != self.last_error:
                        core.monitor_event('ERRO', message)
                        self.notify('error', message)
                        self.last_error = message
                stop_event.wait(float(self.settings.get('watch_interval_sec', 3)))
            core.monitor_event('PARADO', 'Monitoramento encerrado após o arquivo atual.')
        finally:
            # O token evita remover um lock substituído por outra execução.
            if lock.exists() and f'token={token}' in lock.read_text(encoding='utf-8'):
                lock.unlink()

    def _sources(self):
        if not self.source.is_dir():
            raise NotADirectoryError(f'Origem indisponível: {self.source}')
        if not self.dest.is_dir():
            raise NotADirectoryError(f'Destino indisponível: {self.dest}')
        def walk_error(exc):
            self._source_error(exc.filename or self.source, exc)
        for directory, subdirs, filenames in os.walk(self.source, onerror=walk_error, followlinks=False):
            if self.stop_event.is_set():
                return
            subdirs[:] = sorted(name for name in subdirs
                                if not (Path(directory) / name).is_symlink()
                                and (Path(directory) / name).resolve() not in (self.work, self.dest))
            for name in sorted(filenames):
                if self.stop_event.is_set():
                    return
                path = Path(directory) / name
                if path.suffix.lower() in core.VIDEO_EXTENSIONS and not path.is_symlink():
                    yield path

    def _source_error(self, path, exc):
        message = f'Falha ao acessar {path}: {exc}'
        if self.source_errors.get(str(path)) != message:
            core.monitor_event('ERRO', message)
            self.notify('error', message)
            self.source_errors[str(path)] = message

    def _register(self, path, stat):
        source_path = str(path)
        cached = None if self._jobs_by_path is None else self._jobs_by_path.get(self._path_key(source_path))
        if cached is not None and cached['source_size'] == stat.st_size and cached['source_mtime_ns'] == stat.st_mtime_ns and cached['destination_folder'] == str(self.dest):
            return cached
        with core.db_connection() as db:
            previous = db.execute('SELECT * FROM jobs WHERE source_path=?', (source_path,)).fetchone()
            if previous is None:
                # Versões anteriores podem ter guardado caminhos Windows 8.3.
                for candidate in db.execute('SELECT * FROM jobs WHERE media_key=?', (core.media_key(path),)):
                    if Path(candidate['source_path']).expanduser().resolve() == path.resolve():
                        previous = dict(candidate)
                        previous['source_path'] = source_path
                        db.execute('UPDATE jobs SET source_path=? WHERE id=?', (source_path,previous['id']))
                        break
            if previous is None:
                cursor = db.execute('''INSERT INTO jobs
                    (source_path,source_size,source_mtime_ns,media_key,status,created_at,updated_at,destination_folder,cut_video)
                    VALUES(?,?,?,?,'NOVO',?,?,?,?)''',
                    (source_path,stat.st_size,stat.st_mtime_ns,core.media_key(path),core.utcnow(),core.utcnow(),str(self.dest),
                     int(bool(self.settings.get('cut_video',True)))))
                job_id = cursor.lastrowid
                reason = 'Arquivo estável detectado na pasta de origem.'
            else:
                job_id = previous['id']
                changed = previous['source_size'] != stat.st_size or previous['source_mtime_ns'] != stat.st_mtime_ns
                moved = previous['destination_folder'] != str(self.dest)
                if not changed and not moved:
                    return self._cache_job(dict(previous))
                if not changed and previous['destination_folder'] is None and previous['output_path'] and Path(previous['output_path']).parent.resolve() == self.dest:
                    db.execute('UPDATE jobs SET destination_folder=? WHERE id=?',(str(self.dest),job_id))
                    updated = dict(previous)
                    updated['destination_folder'] = str(self.dest)
                    return self._cache_job(updated)
                db.execute('''UPDATE jobs SET source_size=?,source_mtime_ns=?,destination_folder=?,
                    attempts=0,next_retry_at=0,error_kind=NULL,duration=NULL,fps=NULL,mark_in=NULL,
                    mark_out=NULL,confidence=NULL,black_intervals_json=NULL,slate_intervals_json=NULL,
                    output_path=NULL,xml_path=NULL,output_sha256=NULL,
                    xml_sha256=NULL,delivery_verified_at=NULL,delivered_mxf_bytes=NULL,
                    delivered_xml_bytes=NULL,duplicate_of=NULL,progress=0,cut_video=? WHERE id=?''',
                    (stat.st_size,stat.st_mtime_ns,str(self.dest),int(bool(self.settings.get('cut_video',True))),job_id))
                reason = 'Origem alterada; verificando entrega anterior.' if changed else 'Destino alterado; verificando esta pasta.'
        core.event(job_id, 'NOVO', reason)
        self.notify('refresh', None)
        return self._refresh_job(job_id)

    def _delivery(self, job, refresh=False):
        key = core.media_key(job['source_path'])
        if self._deliveries is not None and not refresh:
            return self._deliveries.get(key)
        with core.db_connection() as db:
            row = db.execute('SELECT * FROM automatic_deliveries WHERE media_key=? AND destination_key=?',
                             (core.media_key(job['source_path']),core.destination_key(self.dest))).fetchone()
            delivery = dict(row) if row else None
        if self._deliveries is not None:
            if delivery is None:
                self._deliveries.pop(key, None)
            else:
                self._deliveries[key] = delivery
        return delivery

    def _hash_matches(self, path, digest, size, stat=None):
        stat = path.stat() if stat is None else stat
        signature = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, digest)
        if stat.st_size != size:
            return False
        key = str(path)
        if self.verified.get(key) == signature:
            return True
        if core.sha256(path) != digest:
            return False
        self.verified[key] = signature
        return True

    def _reconcile(self, job):
        """True = já entregue; False = pode processar; conflito lança exceção."""
        delivery = self._delivery(job)
        stem = Path(job['source_path']).stem
        lock = self.dest / ('.' + core.media_key(job['source_path']) + '.ingest.lock')
        if (self._destination_names is None or lock.name.casefold() in self._destination_names) and lock.exists():
            remove_abandoned_lock(lock)
            if self._destination_names is not None:
                self._destination_names.pop(lock.name.casefold(), None)
        if delivery is None:
            # Adota resultados locais de versões anteriores somente com hashes conhecidos.
            if job.get('output_path') and job.get('xml_path') and job.get('output_sha256') and job.get('xml_sha256'):
                mxf, xml = Path(job['output_path']), Path(job['xml_path'])
                if mxf.parent.resolve() == self.dest and xml.parent.resolve() == self.dest and mxf.is_file() and xml.is_file():
                    if core.sha256(mxf) == job['output_sha256'] and core.sha256(xml) == job['xml_sha256']:
                        core.record_automatic_delivery(job, self.dest, mxf, xml, job['output_sha256'], job['xml_sha256'])
                        core.complete_automatic_delivery(job, self.dest)
                        delivery = self._delivery(job, refresh=True)
                        self._remember_output_names(job)
            if delivery is None:
                found = self._matching_files(stem)
                if found:
                    raise DeliveryConflict('Nome existente sem entrega verificada no banco: ' + ', '.join(found))
                if job['error_kind'] == 'CONFLITO':
                    core.event(job['id'], 'NOVO', 'Conflito removido do destino; nova tentativa agendada.',
                               error_kind=None,attempts=0,next_retry_at=0.,progress=0.)
                    self._refresh_job(job['id'])
                return False
        mxf, xml = Path(delivery['output_path']), Path(delivery['xml_path'])
        parent_pair = (str(mxf.parent),str(xml.parent))
        if parent_pair not in self._validated_parent_pairs:
            if mxf.parent.resolve() != self.dest or xml.parent.resolve() != self.dest:
                raise DeliveryConflict('Caminhos do registro de entrega divergem do destino selecionado.')
            self._validated_parent_pairs.add(parent_pair)
        present = []
        for path, digest, size in ((mxf,delivery['output_sha256'],delivery['delivered_mxf_bytes']),
                                    (xml,delivery['xml_sha256'],delivery['delivered_xml_bytes'])):
            try:
                file_stat = path.stat()
            except FileNotFoundError:
                continue
            if not stat_module.S_ISREG(file_stat.st_mode) or not self._hash_matches(path, digest, size, stat=file_stat):
                raise DeliveryConflict(f'Arquivo no destino difere do histórico; preservado: {path.name}')
            present.append(path)
        handoff_policy = self.settings.get('delivery_retention_policy','require_present') == 'handoff'
        if (handoff_policy and delivery['status'] == 'RESERVADO'
                and delivery.get('publication_started_at') and len(present)<2):
            raise DeliveryConflict(
                'Publicação iniciada; possível consumo pelo importador; confirmação externa necessária. '
                'Reserva e arquivos remanescentes preservados para evitar novo cadastro.')
        handoff = handoff_policy and delivery['status'] == 'CONCLUIDO'
        if len(present) == 2 or handoff:
            if delivery['job_id'] == job['id'] and (delivery['source_size'] != job['source_size']
                                                   or delivery['source_mtime_ns'] != job['source_mtime_ns']):
                raise DeliveryConflict('Origem mudou, mas o nome anterior permanece no destino. Use um novo nome para a nova versão.')
            duplicate = delivery['job_id'] != job['id']
            status = 'DUPLICADO' if duplicate else 'CONCLUIDO'
            cut_video = int(bool(delivery.get('cut_video',True)))
            message = ('Entrega já realizada; arquivos removidos/consumidos após a transferência. Cadastro externo não confirmado.'
                       if handoff and len(present)<2 else 'Material já processado e íntegro no destino.')
            if delivery['status'] != 'CONCLUIDO':
                core.complete_automatic_delivery(job, self.dest)
                core.monitor_event('RECUPERADO', f'Entrega íntegra reconciliada: {mxf}')
            if job['status'] != status or int(bool(job.get('cut_video',True))) != cut_video or (handoff and len(present)<2 and job['message'] != message):
                core.event(job['id'], status, message,
                           output_path=str(mxf),xml_path=str(xml),output_sha256=delivery['output_sha256'],
                           xml_sha256=delivery['xml_sha256'],duplicate_of=delivery['job_id'] if duplicate else None,
                           delivered_mxf_bytes=delivery['delivered_mxf_bytes'],delivered_xml_bytes=delivery['delivered_xml_bytes'],
                           delivery_verified_at=core.utcnow(),progress=1.,error_kind=None,cut_video=cut_video)
                self._refresh_job(job['id'])
                self.notify('refresh', None)
            return True
        # Recupera só artefatos que os hashes deste registro comprovam pertencer à EVA.
        for path in present:
            path.unlink()
            self.verified.pop(str(path), None)
            if self._destination_names is not None:
                self._destination_names.pop(path.name.casefold(), None)
        with core.db_connection() as db:
            db.execute('DELETE FROM automatic_deliveries WHERE media_key=? AND destination_key=?',
                       (core.media_key(job['source_path']),core.destination_key(self.dest)))
        if self._deliveries is not None:
            self._deliveries.pop(core.media_key(job['source_path']), None)
        retrying_failure = delivery['status'] != 'CONCLUIDO' and job['status'] == 'ERRO'
        core.event(job['id'], 'ERRO' if retrying_failure else 'NOVO',
                   job['message'] if retrying_failure else 'Entrega ausente/incompleta; recriação automática agendada.',
                   output_path=None,xml_path=None,progress=0.,error_kind=None,
                   next_retry_at=0 if delivery['status'] == 'CONCLUIDO' else job['next_retry_at'],
                   attempts=0 if delivery['status'] == 'CONCLUIDO' else job['attempts'])
        self._refresh_job(job['id'])
        return False

    def _progress(self, job_id, progress, message):
        previous_time, previous_message = self.last_progress.get(job_id, (-float('inf'), None))
        now = time.monotonic()
        if now - previous_time >= 1 or message != previous_message or progress >= 1:
            with core.db_connection() as db:
                db.execute('UPDATE jobs SET progress=? WHERE id=?', (max(0.,min(1.,progress)),job_id))
            self.last_progress[job_id] = (now, message)
            self.notify('refresh', None)
        self.notify('progress', dict(job_id=job_id,progress=max(0.,min(1.,progress)),message=message))

    def _fail(self, job, exc, kind='TECNICO'):
        message = str(exc)
        if job['status'] == 'ERRO' and job['message'] == message and job['error_kind'] == kind:
            return
        previous = core.get_job(job['id'])
        if previous['status'] != 'ERRO' or previous['message'] != message or previous['error_kind'] != kind:
            core.event(job['id'], 'ERRO', message, error_kind=kind,progress=0.,
                       next_retry_at=time.time() + float(self.settings.get('retry_delay_sec',30)))
        else:
            return
        self._progress(job['id'], 0., message)
        self.last_progress.pop(job['id'],None)
        self._refresh_job(job['id'])

    def scan_once(self):
        if self.stop_event.is_set():
            return
        try:
            self._load_scan_snapshot()
            self._scan_once_snapshot()
        finally:
            self._jobs_by_path = None
            self._jobs_by_key = None
            self._deliveries = None
            self._destination_names = None

    def _scan_once_snapshot(self):
        now = time.monotonic()
        stable = float(self.settings.get('stable_seconds', 10))
        paths = self._sources()
        current = set()
        ready = []
        found_count = 0
        for path in paths:
            if self.stop_event.is_set():
                break
            found_count += 1
            try:
                stat = path.stat()
            except FileNotFoundError:
                self.observed.pop(str(path), None)
                continue
            except OSError as exc:
                self.observed.pop(str(path), None)
                self._source_error(path, exc)
                continue
            self.source_errors.pop(str(path), None)
            key = str(path)
            current.add(key)
            signature = (stat.st_size, stat.st_mtime_ns)
            observed = self.observed.get(key)
            if observed is None or observed[:2] != signature:
                self.observed[key] = (*signature, now)
            if stat.st_size > 0 and now - self.observed[key][2] >= stable:
                ready.append((path, stat))
        self.observed = {key:value for key,value in self.observed.items() if key in current}
        active_media_keys = {core.media_key(path) for path in current}
        retained_outputs = {delivery[field] for key in active_media_keys
                            for delivery in [self._deliveries.get(key)] if delivery is not None
                            for field in ('output_path','xml_path')}
        self.verified = {path:signature for path,signature in self.verified.items() if path in retained_outputs}
        queued = []
        for path, stat in ready:
            if self.stop_event.is_set():
                break
            job = self._register(path, stat)
            queued.append((job['id'],path,job))
        # Registra o lote inteiro para acompanhamento, mas volta a observar a
        # origem entre grupos pequenos. A ordem de cadastro mantém FIFO quando
        # chega um nome alfabeticamente anterior aos vídeos que já aguardavam.
        attempts_started = 0
        budget = max(1,int(self.settings.get('max_jobs_per_scan',10)))
        for _,path,queued_job in sorted(queued,key=lambda item:item[0]):
            if self.stop_event.is_set() or attempts_started >= budget:
                break
            job = self._jobs_by_path.get(self._path_key(path),queued_job)
            did_work = False
            try:
                if self._reconcile(job):
                    continue
                job = self._jobs_by_path.get(self._path_key(job['source_path']),job)
                if job['error_kind'] == 'CORTE' and job['analysis_signature'] != self.analysis_signature:
                    core.event(job['id'], 'NOVO', 'Detector ou configuração de análise alterado; nova análise automática.',
                               attempts=0,next_retry_at=0.,error_kind=None,progress=0.)
                    job = self._refresh_job(job['id'])
                if job['error_kind'] == 'TECNICO' and bool(job.get('cut_video',True)) != bool(self.settings.get('cut_video',True)):
                    core.event(job['id'], 'NOVO', 'Modo de corte alterado; nova tentativa de processamento.',
                               attempts=0,next_retry_at=0.,error_kind=None,progress=0.,
                               cut_video=int(bool(self.settings.get('cut_video',True))))
                    job = self._refresh_job(job['id'])
                if job['error_kind'] in ('CORTE', 'CONFLITO', 'IDENTIFICADOR'):
                    continue
                if job['attempts'] >= int(self.settings.get('max_attempts',3)):
                    if job['status'] != 'ERRO':
                        self._fail(job, 'Limite de tentativas atingido após processamento interrompido.')
                    continue
                if job['next_retry_at'] > time.time():
                    continue
                # Retoma estágios interrompidos por crash, sem herdar aprovações antigas.
                did_work = True
                attempts_started += 1
                core.event(job['id'], 'ANALISANDO', 'Análise automática de black e pontos de corte.',
                           attempts=job['attempts']+1,progress=0.,error_kind=None,
                           analysis_signature=self.analysis_signature,
                           cut_video=int(bool(self.settings.get('cut_video',True))))
                self._progress(job['id'], .02, 'Analisando black / claquete')
                analyzed = core.analyze(job['id'], self.settings)
                if analyzed['confidence'] != 'ALTA':
                    self._fail(job, 'Corte não confirmado: ' + analyzed['message'], 'CORTE')
                    continue
                core.approve(job['id'], analyzed['mark_in'], analyzed['mark_out'], automatic=True)
                core.process(job['id'], callback=lambda p,m,jid=job['id']:self._progress(jid,.1+.9*p,m),
                             settings=self.settings, automatic=True)
                core.event(job['id'], 'CONCLUIDO', 'Material entregue e verificado no destino.',progress=1.,
                           error_kind=None,next_retry_at=0.)
                self._progress(job['id'], 1., 'Concluído')
            except DeliveryConflict as exc:
                self._fail(job, exc, 'CONFLITO')
            except core.MaterialIdentityConflict as exc:
                self._fail(job, exc, 'IDENTIFICADOR')
            except Exception as exc:
                self._fail(job, exc)
            finally:
                if did_work:
                    final_job = self._refresh_job(job['id'])
                    self._delivery(final_job,refresh=True)
                    self._remember_output_names(final_job)
                    self.last_progress.pop(job['id'],None)
                    self.notify('refresh', None)
        self.notify('message', f'Monitorando — {found_count} vídeo(s); {found_count-len(ready)} aguardando estabilidade da cópia.')
