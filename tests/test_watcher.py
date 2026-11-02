"""Exercita a fila com SQLite e arquivos reais, sem substituir testes de FFmpeg."""
import csv
import io
import hashlib
import os
import socket
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core
import watcher


class TestFolderWatcher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'origem'
        self.dest = self.root / 'destino'
        self.work = self.root / 'trabalho'
        for path in (self.source, self.dest, self.work):
            path.mkdir()
        self.db_patch = patch.object(core, 'DB_FILE', self.root / 'historico.sqlite3')
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.tmp.cleanup)
        core.init_db()
        self.settings = core.config()
        self.settings.update(source_folder=str(self.source), destination_folder=str(self.dest),
                             local_output_folder=str(self.dest), working_folder=str(self.work),
                             stable_seconds=10, max_attempts=2, retry_delay_sec=0,
                             watch_interval_sec=1, video_profile='teste',
                             delivery_retention_policy='require_present')
        self.now = 1000.0
        self.clock_patch = patch.object(watcher.time, 'monotonic', side_effect=lambda: self.now)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.analysis_patch = patch.object(core, 'analyze', side_effect=self.analyze)
        self.analyzed = self.analysis_patch.start()
        self.addCleanup(self.analysis_patch.stop)
        self.processing_patch = patch.object(core, 'process', side_effect=self.process)
        self.processed = self.processing_patch.start()
        self.addCleanup(self.processing_patch.stop)
        self.notifications = []
        self.service = self.new_service()

    def new_service(self, **changes):
        return watcher.FolderWatcher(dict(self.settings, **changes),
                                     lambda kind, payload: self.notifications.append((kind, payload)))

    def video(self, name='spot.mxf', parent=None):
        path = (parent or self.source) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'conteudo sintetico')
        # Uma cópia pode preservar o horário antigo da origem.
        os.utime(path, (100, 100))
        return path

    def analyze(self, job_id, settings=None):
        core.event(job_id, 'REVISAR', 'Separador identificado', duration=41,
                   fps='25/1', mark_in=6, mark_out=36, confidence='ALTA')
        return core.get_job(job_id)

    def ambiguous_analysis(self, job_id, settings=None):
        core.event(job_id, 'REVISAR', 'Separador ambiguo', duration=41,
                   fps='25/1', mark_in=6, mark_out=36, confidence='BAIXA')
        return core.get_job(job_id)

    def process(self, job_id, callback=lambda *_: None, publish=False,
                settings=None, automatic=False):
        self.assertTrue(automatic, 'A fila deve utilizar o fluxo automático')
        job = core.get_job(job_id)
        dest = Path(settings['destination_folder'])
        output = dest / (Path(job['source_path']).stem + '.mxf')
        xml = output.with_suffix('.xml')
        output.write_bytes(b'MXF de teste')
        xml.write_text('<Material/>', encoding='utf-8')
        core.record_automatic_delivery(job, dest, output, xml, core.sha256(output),
                                       core.sha256(xml))
        core.complete_automatic_delivery(job, dest)
        core.event(job_id, 'CONCLUIDO', 'Entregue', output_path=str(output),
                   xml_path=str(xml), output_sha256=core.sha256(output),
                   xml_sha256=core.sha256(xml), delivery_verified_at=core.utcnow(),
                   delivered_mxf_bytes=output.stat().st_size,
                   delivered_xml_bytes=xml.stat().st_size)
        callback(1, 'Entregue')
        return core.get_job(job_id)

    def stable_scan(self, service=None):
        service = service or self.service
        service.scan_once()
        self.now += 11
        service.scan_once()

    def test_waits_for_unchanged_size_even_when_source_mtime_is_old(self):
        source = self.video()
        self.service.scan_once()
        self.assertEqual(self.analyzed.call_count, 0)
        self.now += 9
        source.write_bytes(b'copia ainda crescendo, com outro tamanho')
        os.utime(source, (100, 100))
        self.service.scan_once()
        self.now += 2
        self.service.scan_once()
        self.assertEqual(self.analyzed.call_count, 0)
        self.now += 9
        self.service.scan_once()
        self.assertEqual(self.processed.call_count, 1)

    def test_completed_and_present_is_skipped_after_restart(self):
        self.video()
        self.stable_scan()
        self.assertEqual(self.processed.call_count, 1)
        self.stable_scan(self.new_service())
        self.assertEqual(self.processed.call_count, 1)
        self.assertEqual(core.jobs()[0]['status'], 'CONCLUIDO')

    def test_missing_delivery_is_recreated_without_deleting_history(self):
        self.video()
        self.stable_scan()
        first_id = core.jobs()[0]['id']
        for path in self.dest.iterdir():
            path.unlink()
        self.stable_scan(self.new_service())
        self.assertEqual(self.processed.call_count, 2)
        self.assertTrue((self.dest / 'spot.mxf').is_file())
        self.assertEqual(core.jobs()[0]['id'], first_id)
        with core.db_connection() as db:
            self.assertGreaterEqual(db.execute('SELECT COUNT(*) FROM events WHERE job_id=?',
                                                (first_id,)).fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM delivery_events WHERE job_id=?',
                                        (first_id,)).fetchone()[0], 2)

    def test_handoff_does_not_resend_after_both_files_are_consumed(self):
        self.video()
        self.stable_scan(self.new_service(delivery_retention_policy='handoff'))
        for path in self.dest.iterdir():
            path.unlink()
        service = self.new_service(delivery_retention_policy='handoff')
        self.stable_scan(service)
        self.service.scan_once()
        self.assertEqual(self.processed.call_count,1)
        self.assertEqual(core.jobs()[0]['status'],'CONCLUIDO')
        self.assertIn('Cadastro externo não confirmado',core.jobs()[0]['message'])

    def test_handoff_preserves_verified_remaining_xml_without_resending(self):
        self.video()
        self.stable_scan(self.new_service(delivery_retention_policy='handoff'))
        (self.dest/'spot.mxf').unlink()
        remaining=self.dest/'spot.xml'
        expected=remaining.read_bytes()
        self.stable_scan(self.new_service(delivery_retention_policy='handoff'))
        self.assertEqual(self.processed.call_count,1)
        self.assertEqual(remaining.read_bytes(),expected)
        self.assertEqual(core.jobs()[0]['status'],'CONCLUIDO')

    def test_handoff_does_not_accept_a_changed_remaining_file(self):
        self.video()
        self.stable_scan(self.new_service(delivery_retention_policy='handoff'))
        (self.dest/'spot.mxf').unlink()
        remaining=self.dest/'spot.xml'
        remaining.write_bytes(b'<ArquivoDeTerceiro/>')
        self.stable_scan(self.new_service(delivery_retention_policy='handoff'))
        self.assertEqual(self.processed.call_count,1)
        self.assertEqual(remaining.read_bytes(),b'<ArquivoDeTerceiro/>')
        self.assertEqual(core.jobs()[0]['error_kind'],'CONFLITO')

    def test_handoff_changed_source_is_a_conflict_even_after_files_are_consumed(self):
        source=self.video()
        self.stable_scan(self.new_service(delivery_retention_policy='handoff'))
        for path in self.dest.iterdir():
            path.unlink()
        source.write_bytes(b'nova versao com o mesmo identificador')
        self.stable_scan(self.new_service(delivery_retention_policy='handoff'))
        self.assertEqual(self.processed.call_count,1)
        self.assertEqual(core.jobs()[0]['error_kind'],'CONFLITO')

    def test_handoff_does_not_mark_an_unfinished_reservation_as_delivered(self):
        self.video()
        original_process=self.process
        def processing(job_id,callback=lambda *_:None,publish=False,settings=None,automatic=False):
            if core.get_job(job_id)['attempts']==1:
                staged=self.work/'spot.mxf'
                staged_xml=self.work/'spot.xml'
                staged.write_bytes(b'MXF incompleto antes do envio')
                staged_xml.write_text('<Material/>',encoding='utf-8')
                core.record_automatic_delivery(core.get_job(job_id),self.dest,staged,staged_xml,
                    core.sha256(staged),core.sha256(staged_xml))
                raise RuntimeError('falha antes de entregar')
            return original_process(job_id,callback,publish,settings,automatic)
        self.processed.side_effect=processing
        service=self.new_service(delivery_retention_policy='handoff')
        self.stable_scan(service)
        self.assertEqual(core.jobs()[0]['status'],'ERRO')
        service.scan_once()
        self.assertEqual(self.processed.call_count,2)
        self.assertEqual(core.jobs()[0]['status'],'CONCLUIDO')
        self.assertTrue((self.dest/'spot.mxf').is_file())

    def interrupted_handoff_publication(self,remaining):
        self.video()
        mxf_data=b'MXF final verificado antes de publicar'
        xml_data=b'<Material/>'
        def processing(job_id,callback=lambda *_:None,publish=False,settings=None,automatic=False):
            staged=self.work/'spot.mxf'
            staged_xml=self.work/'spot.xml'
            staged.write_bytes(mxf_data)
            staged_xml.write_bytes(xml_data)
            core.record_automatic_delivery(core.get_job(job_id),self.dest,staged,staged_xml,
                core.sha256(staged),core.sha256(staged_xml))
            with core.db_connection() as db:
                db.execute('UPDATE automatic_deliveries SET publication_started_at=?',(core.utcnow(),))
            if remaining>=1:
                (self.dest/'spot.xml').write_bytes(xml_data)
            if remaining==2:
                (self.dest/'spot.mxf').write_bytes(mxf_data)
            raise RuntimeError('interrupcao depois de iniciar a publicacao')
        self.processed.side_effect=processing
        service=self.new_service(delivery_retention_policy='handoff')
        self.stable_scan(service)
        self.assertEqual(self.processed.call_count,1)
        self.assertEqual(core.jobs()[0]['status'],'ERRO')
        return service,mxf_data,xml_data

    def test_handoff_started_publication_with_no_files_requires_external_confirmation(self):
        service,_,_=self.interrupted_handoff_publication(0)
        for _ in range(3):
            service.scan_once()
        self.assertEqual(self.processed.call_count,1)
        job=core.jobs()[0]
        self.assertEqual(job['error_kind'],'CONFLITO')
        self.assertIn('Publicação iniciada',job['message'])
        self.assertIn('confirmação externa necessária',job['message'])
        with core.db_connection() as db:
            record=db.execute('SELECT * FROM automatic_deliveries').fetchone()
            self.assertEqual(record['status'],'RESERVADO')
            self.assertIsNotNone(record['publication_started_at'])

    def test_handoff_started_publication_preserves_a_verified_remnant(self):
        service,_,xml_data=self.interrupted_handoff_publication(1)
        service.scan_once()
        self.assertEqual(self.processed.call_count,1)
        self.assertEqual((self.dest/'spot.xml').read_bytes(),xml_data)
        self.assertEqual(core.jobs()[0]['error_kind'],'CONFLITO')
        with core.db_connection() as db:
            self.assertEqual(db.execute('SELECT status FROM automatic_deliveries').fetchone()[0],'RESERVADO')

    def test_handoff_started_publication_with_both_verified_files_is_reconciled(self):
        service,mxf_data,xml_data=self.interrupted_handoff_publication(2)
        service.scan_once()
        self.assertEqual(self.processed.call_count,1)
        self.assertEqual(core.jobs()[0]['status'],'CONCLUIDO')
        self.assertEqual((self.dest/'spot.mxf').read_bytes(),mxf_data)
        self.assertEqual((self.dest/'spot.xml').read_bytes(),xml_data)
        with core.db_connection() as db:
            self.assertEqual(db.execute('SELECT status FROM automatic_deliveries').fetchone()[0],'CONCLUIDO')

    def test_cut_mode_is_saved_for_new_job_and_delivery(self):
        self.video()
        self.stable_scan(self.new_service(cut_video=False))
        job=core.jobs()[0]
        self.assertEqual(job['cut_video'],0)
        self.assertFalse(self.processed.call_args.kwargs['settings']['cut_video'])
        with core.db_connection() as db:
            self.assertEqual(db.execute('SELECT cut_video FROM automatic_deliveries').fetchone()[0],0)

    def test_changing_cut_mode_does_not_reencode_an_existing_delivery(self):
        self.video()
        self.stable_scan(self.new_service(cut_video=True))
        self.stable_scan(self.new_service(cut_video=False))
        self.assertEqual(self.processed.call_count,1)
        self.assertEqual(core.jobs()[0]['cut_video'],1)

    def test_failed_processing_retries_once_when_cut_mode_changes(self):
        self.video()
        original_process=self.process
        def processing(job_id,callback=lambda *_:None,publish=False,settings=None,automatic=False):
            if settings.get('cut_video',True):
                raise RuntimeError('falha no processamento com corte')
            return original_process(job_id,callback,publish,settings,automatic)
        self.processed.side_effect=processing
        self.stable_scan(self.new_service(cut_video=True,max_attempts=1))
        self.assertEqual(core.jobs()[0]['status'],'ERRO')
        self.assertEqual(self.processed.call_count,1)
        service=self.new_service(cut_video=False,max_attempts=1)
        self.stable_scan(service)
        service.scan_once()
        self.assertEqual(self.processed.call_count,2)
        self.assertEqual(core.jobs()[0]['status'],'CONCLUIDO')
        self.assertEqual(core.jobs()[0]['cut_video'],0)

    def test_identity_conflict_under_another_filename_is_not_retried_indefinitely(self):
        self.video('P0320866_primeiro.mxf')
        self.video('P0320866_segundo.mxf')
        original_process=self.process
        def processing(job_id,callback=lambda *_:None,publish=False,settings=None,automatic=False):
            if Path(core.get_job(job_id)['source_path']).stem.endswith('_segundo'):
                raise core.MaterialIdentityConflict('CM/IDK7 reservado por outro nome de arquivo')
            return original_process(job_id,callback,publish,settings,automatic)
        self.processed.side_effect=processing
        self.stable_scan()
        self.assertEqual(self.processed.call_count,2)
        for _ in range(3):
            self.service.scan_once()
        with patch.object(core,'ANALYSIS_REVISION','revisao_diferente'):
            self.stable_scan(self.new_service(cut_video=False))
        self.assertEqual(self.processed.call_count,2)
        by_name={Path(job['source_path']).stem:job for job in core.jobs()}
        self.assertEqual(by_name['P0320866_segundo']['error_kind'],'IDENTIFICADOR')
        self.assertEqual(by_name['P0320866_primeiro']['status'],'CONCLUIDO')
        self.assertFalse((self.dest/'P0320866_segundo.mxf').exists())

    def test_checksum_cache_is_released_when_source_leaves_the_watched_tree(self):
        source=self.video()
        self.stable_scan()
        self.service.scan_once()
        self.assertEqual(len(self.service.verified),2)
        source.unlink()
        self.service.scan_once()
        self.assertEqual(self.service.verified,{})

    def test_idle_scan_of_thousand_delivered_files_uses_one_database_snapshot(self):
        count=1000
        mxf_data=b'MXF volume'
        xml_data=b'<Material/>'
        mxf_hash=hashlib.sha256(mxf_data).hexdigest()
        xml_hash=hashlib.sha256(xml_data).hexdigest()
        job_rows=[]
        delivery_rows=[]
        destination=str(self.service.dest)
        for index in range(count):
            name=f'volume_{index:04d}'
            source=self.source/(name+'.mxf')
            source.write_bytes(b'original')
            stat=source.stat()
            output=(self.dest/(name+'.mxf')).resolve()
            xml=(self.dest/(name+'.xml')).resolve()
            output.write_bytes(mxf_data)
            xml.write_bytes(xml_data)
            job_rows.append((str(source.resolve()),stat.st_size,stat.st_mtime_ns,name,
                core.utcnow(),core.utcnow(),destination,str(output),str(xml),mxf_hash,xml_hash))
            delivery_rows.append((name,core.destination_key(self.dest),index+1,stat.st_size,stat.st_mtime_ns,
                str(output),str(xml),mxf_hash,xml_hash,len(mxf_data),len(xml_data),core.utcnow()))
        with core.db_connection() as db:
            db.executemany('''INSERT INTO jobs
                (source_path,source_size,source_mtime_ns,media_key,status,created_at,updated_at,destination_folder,
                 output_path,xml_path,output_sha256,xml_sha256) VALUES(?,?,?,?,'CONCLUIDO',?,?,?,?,?,?,?)''',job_rows)
            db.executemany('''INSERT INTO automatic_deliveries
                (media_key,destination_key,job_id,source_size,source_mtime_ns,output_path,xml_path,
                 output_sha256,xml_sha256,delivered_mxf_bytes,delivered_xml_bytes,status,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,'CONCLUIDO',?)''',delivery_rows)
        service=self.new_service(stable_seconds=0)
        connection=core.db_connection
        with patch.object(core,'db_connection',wraps=connection) as connections:
            service.scan_once()
            self.assertEqual(connections.call_count,1)
        with patch.object(core,'db_connection',wraps=connection) as connections, \
             patch.object(core,'sha256',side_effect=AssertionError('Hash imutavel nao deve ser relido')), \
             patch.object(core,'matching_files',side_effect=AssertionError('Destino nao deve ser varrido por arquivo')):
            service.scan_once()
            self.assertEqual(connections.call_count,1)
        self.assertEqual(self.processed.call_count,0)
        self.assertEqual(self.analyzed.call_count,0)

    def test_stop_during_source_enumeration_does_not_materialize_entire_tree(self):
        source=self.video()
        visited=[]
        def sources():
            for index in range(10000):
                visited.append(index)
                if index==3:
                    self.service.stop_event.set()
                yield source
        with patch.object(self.service,'_sources',side_effect=sources):
            self.service.scan_once()
        self.assertEqual(len(visited),4)
        self.assertEqual(self.analyzed.call_count,0)

    def test_new_destination_has_independent_delivery(self):
        self.video()
        self.stable_scan()
        other_dest = self.root / 'outro_destino'
        other_dest.mkdir()
        self.stable_scan(self.new_service(destination_folder=str(other_dest),
                                          local_output_folder=str(other_dest)))
        self.assertEqual(self.processed.call_count, 2)
        self.assertTrue((other_dest / 'spot.mxf').is_file())
        self.assertTrue((self.dest / 'spot.mxf').is_file())

    def test_partial_owned_delivery_is_recovered(self):
        self.video()
        self.stable_scan()
        (self.dest / 'spot.mxf').unlink()
        self.assertTrue((self.dest / 'spot.xml').exists())
        self.stable_scan(self.new_service())
        self.assertEqual(self.processed.call_count, 2)
        self.assertTrue((self.dest / 'spot.mxf').is_file())
        self.assertTrue((self.dest / 'spot.xml').is_file())

    def test_changed_destination_file_is_preserved_as_conflict(self):
        self.video()
        self.stable_scan()
        output = self.dest / 'spot.mxf'
        output.write_bytes(b'outro conteudo que substituiu a entrega')
        self.stable_scan(self.new_service())
        self.assertEqual(self.processed.call_count, 1)
        self.assertEqual(output.read_bytes(), b'outro conteudo que substituiu a entrega')
        self.assertEqual(core.jobs()[0]['status'], 'ERRO')
        self.assertEqual(core.jobs()[0]['error_kind'], 'CONFLITO')

    def test_unrecognized_remnant_is_preserved_during_partial_recovery(self):
        self.video()
        self.stable_scan()
        (self.dest / 'spot.mxf').unlink()
        remnant = self.dest / 'spot.xml'
        remnant.write_text('<XMLDeTerceiro/>', encoding='utf-8')
        self.stable_scan(self.new_service())
        self.assertEqual(self.processed.call_count, 1)
        self.assertEqual(remnant.read_text(encoding='utf-8'), '<XMLDeTerceiro/>')
        self.assertEqual(core.jobs()[0]['error_kind'], 'CONFLITO')

    def test_duplicate_names_in_subfolders_are_delivered_once(self):
        self.video('spot.mxf', self.source / 'a')
        self.video('SPOT.MXF', self.source / 'b')
        self.stable_scan()
        self.assertEqual(self.processed.call_count, 1)
        self.assertEqual(sorted(j['status'] for j in core.jobs()), ['CONCLUIDO', 'DUPLICADO'])
        duplicate = next(j for j in core.jobs() if j['status'] == 'DUPLICADO')
        self.assertIsNotNone(duplicate['duplicate_of'])

    def test_verified_legacy_prepared_output_is_adopted_without_reencoding(self):
        self.video()
        core.discover(str(self.source), min_age_sec=0)
        job = core.jobs()[0]
        output = self.dest / 'spot.mxf'
        xml = self.dest / 'spot.xml'
        output.write_bytes(b'MXF preparado pela versao anterior')
        xml.write_text('<Material/>', encoding='utf-8')
        core.event(job['id'], 'PREPARADO', 'Entrega anterior', output_path=str(output),
                   xml_path=str(xml), output_sha256=core.sha256(output),
                   xml_sha256=core.sha256(xml), delivered_mxf_bytes=output.stat().st_size,
                   delivered_xml_bytes=xml.stat().st_size)
        self.stable_scan()
        self.assertEqual(self.processed.call_count, 0)
        self.assertEqual(core.get_job(job['id'])['status'], 'CONCLUIDO')
        with core.db_connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM automatic_deliveries').fetchone()[0], 1)

    def test_changed_source_with_existing_delivery_requires_new_name(self):
        source = self.video()
        self.stable_scan()
        source.write_bytes(b'uma nova versao para o mesmo codigo')
        self.stable_scan()
        self.assertEqual(self.processed.call_count, 1)
        self.assertEqual(core.jobs()[0]['status'], 'ERRO')
        self.assertEqual(core.jobs()[0]['error_kind'], 'CONFLITO')

    def test_incomplete_reservation_preserves_retry_delay(self):
        self.video()
        self.settings['retry_delay_sec'] = 60
        service = self.new_service()
        failed_once = False

        def processing(job_id, callback=lambda *_: None, publish=False,
                       settings=None, automatic=False):
            nonlocal failed_once
            if not failed_once:
                failed_once = True
                staged = self.work / 'spot.mxf'
                staged_xml = staged.with_suffix('.xml')
                staged.write_bytes(b'MXF pronto antes da falha de rede')
                staged_xml.write_text('<Material/>', encoding='utf-8')
                core.record_automatic_delivery(core.get_job(job_id), self.dest, staged,
                                               staged_xml, core.sha256(staged),
                                               core.sha256(staged_xml))
                raise RuntimeError('rede caiu antes de publicar arquivos finais')
            return self.process(job_id, callback, publish, settings, automatic)

        self.processed.side_effect = processing
        with patch.object(watcher.time, 'time', side_effect=lambda: self.now):
            self.stable_scan(service)
            self.assertEqual(self.processed.call_count, 1)
            self.now += 1
            service.scan_once()
            self.assertEqual(self.processed.call_count, 1,
                             'Reconciliar reserva não deve antecipar a nova tentativa')
            self.now += 60
            service.scan_once()
        self.assertEqual(self.processed.call_count, 2)
        self.assertEqual(core.jobs()[0]['status'], 'CONCLUIDO')

    def test_stop_finishes_current_file_without_starting_next(self):
        self.video('a_primeiro.mxf')
        self.video('b_segundo.mxf')

        def processing(*args, **kwargs):
            result = self.process(*args, **kwargs)
            self.service.stop_event.set()
            return result

        self.processed.side_effect = processing
        self.stable_scan()
        self.assertEqual(self.processed.call_count, 1)
        self.assertTrue((self.dest / 'a_primeiro.mxf').exists())
        self.assertFalse((self.dest / 'b_segundo.mxf').exists())

    def test_scan_budget_processes_one_and_registers_remaining_stable_jobs(self):
        self.video('a_primeiro.mxf')
        self.video('b_segundo.mxf')
        service=self.new_service(max_jobs_per_scan=1)
        self.stable_scan(service)
        self.assertEqual(self.processed.call_count,1)
        states={Path(job['source_path']).stem:job['status'] for job in core.jobs()}
        self.assertEqual(states,{'a_primeiro':'CONCLUIDO','b_segundo':'NOVO'})
        self.assertTrue((self.dest/'a_primeiro.mxf').is_file())
        self.assertFalse((self.dest/'b_segundo.mxf').exists())

    def test_new_arrival_is_observed_between_batches_without_bypassing_fifo(self):
        self.video('b_primeiro.mxf')
        self.video('c_segundo.mxf')
        service=self.new_service(max_jobs_per_scan=1,stable_seconds=0)
        service.scan_once()
        self.assertEqual(self.processed.call_count,1)
        self.video('a_novo.mxf')
        service.scan_once()
        self.assertEqual(self.processed.call_count,2)
        states={Path(job['source_path']).stem:job['status'] for job in core.jobs()}
        self.assertEqual(states['c_segundo'],'CONCLUIDO')
        self.assertEqual(states['a_novo'],'NOVO')
        service.scan_once()
        self.assertEqual(self.processed.call_count,3)
        processed_ids=[call.args[0] for call in self.processed.call_args_list]
        names=[Path(core.get_job(job_id)['source_path']).stem for job_id in processed_ids]
        self.assertEqual(names,['b_primeiro','c_segundo','a_novo'])
        service.scan_once()
        self.assertEqual(self.processed.call_count,3,'Varreduras seguintes não devem reenviar os mesmos materiais')

    def test_failed_analysis_also_consumes_one_scan_budget_attempt(self):
        self.video('a_falha.mxf')
        self.video('b_depois.mxf')
        self.analyzed.side_effect=self.ambiguous_analysis
        service=self.new_service(max_jobs_per_scan=1,stable_seconds=0)
        service.scan_once()
        self.assertEqual(self.analyzed.call_count,1)
        self.assertEqual(self.processed.call_count,0)
        states={Path(job['source_path']).stem:job['status'] for job in core.jobs()}
        self.assertEqual(states,{'a_falha':'ERRO','b_depois':'NOVO'})

    def test_external_name_conflict_is_preserved_and_not_processed(self):
        self.video()
        external = self.dest / 'SPOT.mxf'
        external.write_bytes(b'material de terceiro')
        self.stable_scan()
        self.assertEqual(self.processed.call_count, 0)
        self.assertEqual(external.read_bytes(), b'material de terceiro')
        self.assertIn(core.jobs()[0]['status'], ('DUPLICADO', 'ERRO'))
        self.assertTrue(any(kind == 'refresh' for kind, _ in self.notifications),
                        'A interface deve exibir também conflitos antes da análise')

    def test_unreadable_file_does_not_prevent_processing_other_files(self):
        blocked = self.video('a_sem_permissao.mxf')
        healthy = self.video('b_ok.mxf')
        original_stat = Path.stat

        def stat(path, *args, **kwargs):
            if path == blocked:
                raise PermissionError('acesso negado ao arquivo especifico')
            return original_stat(path, *args, **kwargs)

        with patch.object(self.service, '_sources', return_value=[blocked, healthy]), \
                patch.object(Path, 'stat', stat):
            self.stable_scan()
        self.assertEqual(self.processed.call_count, 1)
        self.assertTrue((self.dest / 'b_ok.mxf').exists())
        with core.db_connection() as db:
            self.assertTrue(db.execute("SELECT 1 FROM monitor_events WHERE message LIKE '%a_sem_permissao%'" ).fetchone())

    def test_failure_does_not_block_other_videos_and_retries_are_bounded(self):
        self.video('a_falha.mxf')
        self.video('b_ok.mp4')

        def analyze(job_id, settings=None):
            if Path(core.get_job(job_id)['source_path']).stem == 'a_falha':
                raise RuntimeError('erro transitorio de leitura')
            return self.analyze(job_id, settings)

        self.analyzed.side_effect = analyze
        self.stable_scan()
        for _ in range(5):
            self.now += 31
            self.service.scan_once()
        by_name = {Path(j['source_path']).stem: j for j in core.jobs()}
        self.assertEqual(by_name['a_falha']['status'], 'ERRO')
        self.assertEqual(by_name['b_ok']['status'], 'CONCLUIDO')
        self.assertEqual(self.processed.call_count, 1)
        failures = [call for call in self.analyzed.call_args_list
                    if call.args[0] == by_name['a_falha']['id']]
        self.assertEqual(len(failures), 2)
        count = self.analyzed.call_count
        self.stable_scan(self.new_service())
        self.assertEqual(self.analyzed.call_count, count,
                         'Reabrir o aplicativo não deve zerar o limite de tentativas')

    def test_ambiguous_marks_are_logged_and_never_cut_automatically(self):
        self.video()

        def ambiguous(job_id, settings=None):
            core.event(job_id, 'REVISAR', 'Separador ambiguo', duration=41,
                       fps='25/1', mark_in=6, mark_out=36, confidence='BAIXA')
            return core.get_job(job_id)

        self.analyzed.side_effect = ambiguous
        self.stable_scan()
        for _ in range(3):
            self.now += 31
            self.service.scan_once()
        self.assertEqual(self.processed.call_count, 0)
        self.assertEqual(self.analyzed.call_count, 1)
        self.assertEqual(core.jobs()[0]['status'], 'ERRO')

    def test_only_current_source_is_processed(self):
        previous = self.root / 'origem_anterior'
        previous.mkdir()
        self.video('anterior.mxf', previous)
        core.discover(str(previous), min_age_sec=0)
        self.video('atual.mov')
        self.stable_scan()
        self.assertEqual(self.processed.call_count, 1)
        processed_id = self.processed.call_args.args[0]
        self.assertEqual(Path(core.get_job(processed_id)['source_path']).name, 'atual.mov')

    def test_cut_failure_retries_once_when_analysis_configuration_changes(self):
        self.video()
        self.analyzed.side_effect = self.ambiguous_analysis
        self.stable_scan()
        first = core.jobs()[0]
        self.assertEqual(first['error_kind'], 'CORTE')
        self.assertEqual(first['analysis_signature'], core.analysis_signature(self.settings))

        changed = dict(black_expected_sec=3.0)
        service = self.new_service(**changed)
        self.stable_scan(service)
        self.assertEqual(self.analyzed.call_count, 2)
        for _ in range(3):
            self.now += 31
            service.scan_once()
        self.stable_scan(self.new_service(**changed))
        self.assertEqual(self.analyzed.call_count, 2,
                         'A mesma configuração não deve reabrir o corte indefinidamente')

        self.analyzed.side_effect = self.analyze
        changed['black_pixel_threshold'] = .05
        self.stable_scan(self.new_service(**changed))
        self.assertEqual(self.analyzed.call_count, 3)
        self.assertEqual(self.processed.call_count, 1)
        result = core.jobs()[0]
        self.assertEqual(result['id'], first['id'])
        self.assertEqual(result['status'], 'CONCLUIDO')
        with core.db_connection() as db:
            count = db.execute("SELECT COUNT(*) FROM events WHERE job_id=? AND message LIKE 'Corte não confirmado:%'",
                               (first['id'],)).fetchone()[0]
        self.assertEqual(count, 2, 'A reanálise deve preservar os erros anteriores')

    def test_polling_changes_and_restart_do_not_reopen_cut_failure(self):
        explicit_defaults = dict(core.ANALYSIS_DEFAULTS)
        self.assertEqual(core.analysis_signature({}), core.analysis_signature(explicit_defaults),
                         'Valores default explícitos e implícitos devem ter a mesma assinatura')
        explicit_defaults.update(black_pixel_threshold=0, black_picture_ratio=1,
                                 black_expected_sec=5, opening_scan_sec=30,
                                 slate_start_tolerance_frames=2,slate_boundary_tolerance_frames=2)
        self.assertEqual(core.analysis_signature({}), core.analysis_signature(explicit_defaults),
                         'Números equivalentes devem produzir a mesma assinatura')
        self.video()
        self.analyzed.side_effect = self.ambiguous_analysis
        self.stable_scan()
        changed = dict(watch_interval_sec=99, stable_seconds=1,
                       retry_delay_sec=120, max_attempts=100)
        self.assertEqual(core.analysis_signature(self.settings),
                         core.analysis_signature(dict(self.settings, **changed)))
        for _ in range(3):
            self.stable_scan(self.new_service(**changed))
        self.assertEqual(self.analyzed.call_count, 1)
        self.assertEqual(self.processed.call_count, 0)
        self.assertEqual(core.jobs()[0]['error_kind'], 'CORTE')

    def test_detector_revision_and_legacy_null_signature_each_retry_once(self):
        self.video()
        self.analyzed.side_effect = self.ambiguous_analysis
        self.stable_scan()
        job_id = core.jobs()[0]['id']
        with patch.object(core, 'ANALYSIS_REVISION', 'regressao_detector_novo'):
            service = self.new_service()
            self.stable_scan(service)
            self.stable_scan(self.new_service())
            self.assertEqual(self.analyzed.call_count, 2)
            self.assertEqual(core.get_job(job_id)['analysis_signature'],
                             core.analysis_signature(self.settings))

            # Bancos anteriores à assinatura não devem exigir intervenção manual.
            core.event(job_id, 'ERRO', 'Corte não confirmado: falha da versão anterior',
                       error_kind='CORTE', analysis_signature=None,
                       attempts=100, next_retry_at=watcher.time.time() + 99999)
            self.stable_scan(self.new_service())
            self.stable_scan(self.new_service())
            self.assertEqual(self.analyzed.call_count, 3)
            result = core.get_job(job_id)
            self.assertEqual(result['attempts'], 1)
            self.assertEqual(result['error_kind'], 'CORTE')
            self.assertEqual(result['analysis_signature'], core.analysis_signature(self.settings))

    def test_analysis_changes_preserve_completed_delivery_and_destination_conflict(self):
        self.video('concluido.mxf')
        self.stable_scan()
        self.video('conflito.mxf')
        external = self.dest / 'conflito.mxf'
        external.write_bytes(b'material externo deve permanecer intacto')
        self.stable_scan()
        self.assertEqual(self.analyzed.call_count, 1)
        self.assertEqual(self.processed.call_count, 1)

        with patch.object(core, 'ANALYSIS_REVISION', 'regressao_revisao_sem_sobrescrita'):
            self.stable_scan(self.new_service(black_expected_sec=3.0,
                                              black_pixel_threshold=.05))
        self.assertEqual(self.analyzed.call_count, 1)
        self.assertEqual(self.processed.call_count, 1)
        by_name = {Path(job['source_path']).stem: job for job in core.jobs()}
        self.assertEqual(by_name['concluido']['status'], 'CONCLUIDO')
        self.assertEqual(by_name['conflito']['error_kind'], 'CONFLITO')
        self.assertEqual(external.read_bytes(), b'material externo deve permanecer intacto')

    def test_export_contains_historical_failure_events(self):
        self.video()
        self.analyzed.side_effect = RuntimeError('falha exportavel especifica')
        self.stable_scan()
        archive_path = self.root / 'logs.zip'
        core.export_logs(archive_path)
        with zipfile.ZipFile(archive_path) as archive:
            csv_files = [name for name in archive.namelist() if name.endswith('.csv')]
            self.assertGreaterEqual(len(csv_files), 2)
            text = '\n'.join(archive.read(name).decode('utf-8-sig') for name in csv_files)
            self.assertIn('falha exportavel especifica', text)
            for name in csv_files:
                rows = list(csv.reader(io.StringIO(archive.read(name).decode('utf-8-sig')),
                                       delimiter=';'))
                self.assertGreaterEqual(len(rows), 1)


class TestWatchConfiguration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'origem').mkdir()
        (self.root / 'destino').mkdir()
        self.settings = dict(source_folder=str(self.root / 'origem'),
                             destination_folder=str(self.root / 'destino'),
                             working_folder=str(self.root / 'trabalho'),
                             audio_channel_map=['auto'] * 16)
        binaries = patch.object(core.shutil, 'which', side_effect=lambda name: name)
        binaries.start()
        self.addCleanup(binaries.stop)

    def test_same_folder_is_rejected(self):
        self.settings['destination_folder'] = self.settings['source_folder']
        with self.assertRaises(ValueError):
            core.validate_watch_settings(self.settings)

    def test_nested_destination_is_rejected(self):
        nested = self.root / 'origem' / 'saida'
        nested.mkdir()
        self.settings['destination_folder'] = str(nested)
        with self.assertRaises(ValueError):
            core.validate_watch_settings(self.settings)

    def test_missing_destination_is_not_silently_created(self):
        missing = self.root / 'servidor_indisponivel'
        self.settings['destination_folder'] = str(missing)
        with self.assertRaises(NotADirectoryError):
            core.validate_watch_settings(self.settings)
        self.assertFalse(missing.exists())

    def test_missing_ffprobe_prevents_start(self):
        with patch.object(core.shutil, 'which', side_effect=lambda name: None if name == 'ffprobe' else name):
            with self.assertRaises(FileNotFoundError):
                core.validate_watch_settings(self.settings)

    def test_configured_floripa_destination_requires_all_three_flags(self):
        self.settings['floripa_folder'] = self.settings['destination_folder']
        with self.assertRaises(ValueError):
            core.validate_watch_settings(self.settings)
        self.settings.update(enable_publish_to_floripa=True, xml_homologated=True)
        with self.assertRaises(ValueError):
            core.validate_watch_settings(self.settings)
        self.settings['audio_layout_homologated'] = True
        with self.assertRaises(ValueError):
            core.validate_watch_settings(self.settings)
        template=self.root/'template_integ.xml'
        template.write_text('<Import><CM>{{CM}}</CM><ID>{{IDK7}}</ID><Arquivo>{{FILENAME}}</Arquivo></Import>',encoding='utf-8')
        self.settings.update(xml_profile='smartware_template',smartware_xml_template_cut=str(template))
        checked = core.validate_watch_settings(self.settings)
        self.assertEqual(Path(checked['destination_folder']), Path(self.settings['destination_folder']).resolve())

    def test_floripa_destination_accepts_native_smartware_xml(self):
        self.settings.update(floripa_folder=self.settings['destination_folder'],
                             enable_publish_to_floripa=True,xml_homologated=True,
                             audio_layout_homologated=True,xml_profile='smartware')
        checked=core.validate_watch_settings(self.settings)
        self.assertEqual(checked['xml_profile'],'smartware')

    def test_save_preferences_preserves_audio_map_and_source_destination(self):
        config_path = self.root / 'config.json'
        with patch.object(core, 'CONFIG_FILE', config_path):
            core.save_config(self.settings)
            restored = core.config()
        self.assertEqual(restored, self.settings)


class TestMonitorLocks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / '.monitor.lock'

    def test_dead_local_process_lock_can_be_removed(self):
        self.path.write_text(f'pid=123456\nhost={socket.gethostname()}\ntoken=antigo\n', encoding='utf-8')
        with patch.object(watcher, 'process_is_alive', return_value=False):
            watcher.remove_abandoned_lock(self.path)
        self.assertFalse(self.path.exists())

    def test_active_process_lock_is_preserved(self):
        data = f'pid=123456\nhost={socket.gethostname()}\ntoken=ativo\n'
        self.path.write_text(data, encoding='utf-8')
        with patch.object(watcher, 'process_is_alive', return_value=True):
            with self.assertRaises(watcher.DeliveryConflict):
                watcher.remove_abandoned_lock(self.path)
        self.assertEqual(self.path.read_text(encoding='utf-8'), data)

    def test_unknown_and_remote_locks_are_preserved(self):
        for data in ('lock legado sem pid', 'pid=123456\nhost=outro-computador-na-rede\n'):
            with self.subTest(data=data):
                self.path.write_text(data, encoding='utf-8')
                with patch.object(watcher, 'process_is_alive', return_value=False):
                    with self.assertRaises(watcher.DeliveryConflict):
                        watcher.remove_abandoned_lock(self.path)
                self.assertEqual(self.path.read_text(encoding='utf-8'), data)


if __name__ == '__main__':
    unittest.main()
