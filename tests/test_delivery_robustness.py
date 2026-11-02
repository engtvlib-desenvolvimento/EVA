import csv
import io
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import core


class TestDeliveryRobustness(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='eva_entrega_')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.source=self.root/'origem'
        self.dest=self.root/'destino'
        self.source.mkdir()
        self.dest.mkdir()
        database=patch.object(core,'DB_FILE',self.root/'db.sqlite3')
        database.start()
        self.addCleanup(database.stop)
        core.init_db()
        self.mxf=self.source/'P0320866_chamada.mxf'
        self.xml=self.mxf.with_suffix('.xml')
        self.mxf.write_bytes(b'midia de teste'*1000)
        self.xml.write_text('<Material/>',encoding='utf-8')
        core.discover(str(self.source),0)
        self.job=core.jobs()[0]
        self.digest=core.sha256(self.mxf)
        self.settings={'delivery_retention_policy':'handoff','publication_order':'xml_first'}

    def reserve(self):
        core.record_automatic_delivery(self.job,self.dest,self.mxf,self.xml,self.digest,core.sha256(self.xml))

    @unittest.skipUnless(os.name=='nt','Simula rename sem substituição no Windows')
    def test_immediate_importer_consumption_does_not_fail_or_republish(self):
        self.reserve()
        rename=os.rename
        consumed=[]
        def consume(source,target):
            rename(source,target)
            target=Path(target)
            consumed.append(target.name)
            target.unlink()
        with patch.object(core.os,'rename',side_effect=consume):
            result=core.transfer(self.job,self.mxf,self.xml,self.settings,self.digest,lambda *_:None,dest_override=self.dest)
        self.assertEqual(consumed,[self.xml.name,self.mxf.name])
        self.assertEqual(result[3:],(self.mxf.stat().st_size,self.xml.stat().st_size))
        self.assertFalse(list(self.dest.iterdir()))
        with core.db_connection() as db:
            delivery=db.execute('SELECT * FROM automatic_deliveries').fetchone()
            self.assertEqual(delivery['status'],'CONCLUIDO')
            self.assertIsNotNone(delivery['publication_started_at'])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM delivery_events').fetchone()[0],1)

    @unittest.skipUnless(os.name=='nt','Simula rename sem substituição no Windows')
    def test_interruption_after_visibility_keeps_ambiguous_delivery_record(self):
        self.reserve()
        rename=os.rename
        def fail_after_publish(source,target):
            rename(source,target)
            Path(target).unlink()
            raise RuntimeError('interrupção após publicar')
        with patch.object(core.os,'rename',side_effect=fail_after_publish):
            with self.assertRaisesRegex(RuntimeError,'após publicar'):
                core.transfer(self.job,self.mxf,self.xml,self.settings,self.digest,lambda *_:None,dest_override=self.dest)
        with core.db_connection() as db:
            delivery=db.execute('SELECT * FROM automatic_deliveries').fetchone()
            self.assertEqual(delivery['status'],'RESERVADO')
            self.assertIsNotNone(delivery['publication_started_at'])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM delivery_events').fetchone()[0],0)

    def test_same_cm_or_numeric_id_from_another_filename_is_rejected(self):
        self.reserve()
        for name in ('P0320866_outro_titulo.mxf','C0320866_outro_prefixo.mxf'):
            path=self.source/name
            path.write_bytes(b'outra origem')
            core.discover(str(self.source),0)
            job=next(row for row in core.jobs() if Path(row['source_path']).name==name)
            with self.assertRaises(core.MaterialIdentityConflict):
                core.check_material_identity(job,self.dest)

    def test_logs_stream_directly_and_keep_identity_mode_and_excel_safe_text(self):
        with core.db_connection() as db:
            db.execute('UPDATE jobs SET cm=?,idk7=?,cut_video=0 WHERE id=?',('P0320866','320866',self.job['id']))
            db.executemany('INSERT INTO events(job_id,moment,status,message) VALUES(?,?,?,?)',
                ((self.job['id'],core.utcnow(),'ERRO','=texto de teste') for _ in range(5000)))
        archive=self.root/'logs.zip'
        with patch.object(core,'jobs',side_effect=AssertionError('Exportação não pode carregar jobs() inteiro')):
            core.export_logs(archive)
        with zipfile.ZipFile(archive) as package:
            events=list(csv.DictReader(io.StringIO(package.read('events.csv').decode('utf-8-sig')),delimiter=';'))
            self.assertEqual(len(events),5000)
            self.assertEqual(events[0]['message'],"'=texto de teste")
            jobs=list(csv.DictReader(io.StringIO(package.read('jobs.csv').decode('utf-8-sig')),delimiter=';'))
            self.assertEqual((jobs[0]['cm'],jobs[0]['idk7'],jobs[0]['cut_video']),('P0320866','320866','0'))

    def test_capacity_is_checked_before_encoding(self):
        with patch.object(core.shutil,'disk_usage',return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(OSError,'Espaço insuficiente'):
                core.ensure_output_capacity(self.source,self.dest,45,'xdcam_hd422')


if __name__=='__main__':
    unittest.main()
