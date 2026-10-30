import hashlib
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import core


class TestMarks(unittest.TestCase):
    def test_claquete_5s_black_e_5s_black_final(self):
        # claquete 1s, black 5s, comercial 30s, black 5s.
        mi,mo,conf,msg=core.detect_marks(41,Fraction(25,1),[(1,6),(36,41)])
        self.assertEqual((mi,mo,conf),(6,36,'ALTA'))
        self.assertIn('claquete',msg)

    def test_29_97_alinha_por_frame(self):
        fps=Fraction(30000,1001)
        mi,mo,conf,_=core.detect_marks(41,fps,[(1.001,6.006),(36.036,41)])
        self.assertAlmostEqual(mi,6.006,places=3)
        self.assertAlmostEqual(mo,36.036,places=3)
        self.assertEqual(conf,'ALTA')

    def test_black_interno_ambiguo_requer_revisao(self):
        result=core.detect_marks(41,Fraction(25),[(1,6),(15,20),(36,41)])
        self.assertEqual(result[2],'BAIXA')

    def test_black_nao_tem_cinco_segundos(self):
        result=core.detect_marks(32,Fraction(25),[(1,1.4),(27,27.4)])
        self.assertEqual(result[2],'BAIXA')

    def test_sem_black_requer_revisao(self):
        mi,mo,conf,_=core.detect_marks(30,Fraction(30000,1001),[])
        self.assertEqual(conf,'BAIXA')
        self.assertAlmostEqual(mi,0)
        self.assertGreater(mo,29)

    def test_sem_conteudo(self):
        result=core.detect_marks(5,Fraction(25),[(0,5)])
        self.assertEqual(result[2],'BAIXA')


class TestDuplicidade(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.before=core.DB_FILE
        core.DB_FILE=self.root/'db.sqlite3'
        (self.root/'origem'/'a').mkdir(parents=True)
        (self.root/'origem'/'b').mkdir(parents=True)
        (self.root/'dest').mkdir()
        core.init_db()

    def tearDown(self):
        core.DB_FILE=self.before
        self.tmp.cleanup()

    def test_same_name_different_folders_skipped(self):
        (self.root/'origem'/'a'/'spot.mxf').write_bytes(b'a')
        (self.root/'origem'/'b'/'SPOT.MXF').write_bytes(b'b')
        core.discover(str(self.root/'origem'),0)
        status={Path(j['source_path']).name:j['status'] for j in core.jobs()}
        self.assertEqual(status['spot.mxf'],'NOVO')
        self.assertEqual(status['SPOT.MXF'],'DUPLICADO')
        duplicate=next(j for j in core.jobs() if j['status']=='DUPLICADO')
        self.assertIsNotNone(duplicate['duplicate_of'])

    def test_case_insensitive_dest_folder(self):
        (self.root/'dest'/'Comercial.XML').write_text('<x/>')
        self.assertEqual(core.matching_files(self.root/'dest','comercial'),['Comercial.XML'])

    def test_db_reserved_and_completed_cannot_republish(self):
        inp=self.root/'origem'/'a'/'spot.mxf'
        inp.write_bytes(b'original')
        core.discover(str(self.root/'origem'),0)
        j=core.jobs()[0]
        core.claim_publication(j,self.root/'dest')
        with self.assertRaises(core.DuplicateMaterialError):
            core.claim_publication(j,self.root/'dest')
        core.finish_publication(j,str(self.root/'dest'/'spot.mxf'),str(self.root/'dest'/'spot.xml'),'mxfhash','xmlhash')
        reason,_=core.duplicate_in_history(j)
        self.assertIn('registro',reason)
        with core.db_connection() as db:
            found=db.execute('SELECT * FROM media_transfers').fetchone()
            self.assertEqual(found['status'],'CONCLUIDO')

    def test_complete_transfer_checksums_and_xml_and_double_send(self):
        inp=self.root/'origem'/'a'/'spot.mxf'
        inp.write_bytes(b'arquivo de teste ' * 1024)
        xml=self.root/'origem'/'a'/'spot.xml'
        xml.write_text('<?xml version="1.0"?><Material/>',encoding='utf-8')
        core.discover(str(self.root/'origem'),0)
        j=core.jobs()[0]
        digest=hashlib.sha256(inp.read_bytes()).hexdigest()
        settings={'floripa_folder':str(self.root/'dest'),'enable_publish_to_floripa':True,
                  'xml_homologated':True,'audio_layout_homologated':True}
        result=core.transfer(j,inp,xml,settings,digest,lambda *_:None)
        self.assertEqual(Path(result[0]).read_bytes(),inp.read_bytes())
        self.assertEqual(Path(result[1]).read_bytes(),xml.read_bytes())
        self.assertEqual(result[3],inp.stat().st_size)
        with self.assertRaises(core.DuplicateMaterialError):
            core.transfer(j,inp,xml,settings,digest,lambda *_:None)
        with core.db_connection() as db:
            self.assertEqual(db.execute('SELECT status FROM media_transfers').fetchone()['status'],'CONCLUIDO')

    def test_interrupted_transfer_no_final_releases_reservation(self):
        inp=self.root/'origem'/'a'/'spot.mxf'
        inp.write_bytes(b'dados')
        xml=self.root/'origem'/'a'/'spot.xml'
        xml.write_text('<Material/>')
        core.discover(str(self.root/'origem'),0)
        j=core.jobs()[0]
        settings={'floripa_folder':str(self.root/'dest'),'enable_publish_to_floripa':True,
                  'xml_homologated':True,'audio_layout_homologated':True}
        with self.assertRaises(RuntimeError):
            core.transfer(j,inp,xml,settings,'hash-invalido',lambda *_:None)
        with core.db_connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM media_transfers').fetchone()[0],0)

    def test_migration_idempotent(self):
        core.init_db()
        with core.db_connection() as db:
            columns={row['name'] for row in db.execute('PRAGMA table_info(jobs)')}
            self.assertIn('delivery_verified_at',columns)

if __name__=='__main__':
    unittest.main()
