"""Modos físico/lógico com FFmpeg real, integridade e tempo de execução limitado."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import core
from process_runner import run_transcode


class TestProcessRunner(unittest.TestCase):
    def test_stalled_child_is_stopped(self):
        command=[sys.executable,'-u','-c',"import time; print('out_time=00:00:00.000000',flush=True); time.sleep(10)"]
        with self.assertRaisesRegex(TimeoutError,'sem avanço'):
            run_transcode(command,1,lambda *_:None,stall_timeout=.2)

    def test_child_errors_and_progress_are_reported(self):
        progress=[]
        run_transcode([sys.executable,'-u','-c',"print('out_time=00:00:00.500000'); print('progress=end')"],
                      1,lambda value,message:progress.append(value),stall_timeout=5)
        self.assertEqual(progress,[.34])
        with self.assertRaisesRegex(RuntimeError,'falha controlada'):
            run_transcode([sys.executable,'-c',"import sys; print('falha controlada',file=sys.stderr); sys.exit(3)"],
                          1,lambda *_:None,stall_timeout=5)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg/FFprobe fora do PATH')
class TestProcessingModes(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='eva_modos_')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.source=self.root/'P0320866_TESTE.mxf'
        self.destination=self.root/'destino'
        self.destination.mkdir()
        database=patch.object(core,'DB_FILE',self.root/'historico.sqlite3')
        database.start()
        self.addCleanup(database.stop)
        core.init_db()
        self.settings=core.config()
        self.settings.update(video_profile='teste',working_folder=str(self.root/'work'),
                             source_folder=str(self.root),destination_folder=str(self.destination),
                             xml_profile='eva')
        graph=('color=red:s=320x180:r=25:d=3[a];color=black:s=320x180:r=25:d=2[b];'
               'testsrc2=s=320x180:r=25:d=7.32[c];color=black:s=320x180:r=25:d=1[d];'
               '[a][b][c][d]concat=n=4:v=1:a=0[out0]')
        result=subprocess.run(['ffmpeg','-hide_banner','-nostdin','-loglevel','error','-f','lavfi','-i',graph,
                               '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=13.32',
                               '-c:v','mpeg2video','-b:v','8M','-pix_fmt','yuv422p','-c:a','pcm_s24le',
                               '-ar','48000','-ac','2',str(self.source)],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        core.discover(str(self.root),0)
        self.job_id=core.jobs()[0]['id']

    def packets(self,path):
        result=subprocess.run(['ffprobe','-v','error','-select_streams','v:0','-show_packets',
                               '-show_data_hash','sha256','-show_entries','packet=data_hash','-of','json',str(path)],
                              capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        return [packet['data_hash'] for packet in json.loads(result.stdout)['packets']]

    def exercise(self,cut,xml_profile='eva'):
        self.settings['cut_video']=cut
        self.settings['xml_profile']=xml_profile
        source_hash=core.sha256(self.source)
        job=core.analyze(self.job_id,self.settings)
        self.assertEqual(job['confidence'],'ALTA',job)
        self.assertEqual((job['mark_in'],job['mark_out']),(5.,12.32))
        core.approve(self.job_id,job['mark_in'],job['mark_out'],automatic=True)
        result=core.process(self.job_id,settings=self.settings,automatic=True)
        probe=core.run_probe(result['output_path'])
        video=next(stream for stream in probe['streams'] if stream['codec_type']=='video')
        self.assertEqual(video['duration_ts'],183 if cut else 333)
        audio=[stream for stream in probe['streams'] if stream['codec_type']=='audio']
        self.assertEqual(len(audio),16)
        self.assertTrue(all(stream['codec_name']=='pcm_s24le' and stream['channels']==1
                            and stream['sample_rate']=='48000' for stream in audio))
        xml=core.ET.parse(result['xml_path'])
        native = xml_profile == 'smartware'
        self.assertEqual((xml.findtext('cm' if native else 'CM'),xml.findtext('idk7' if native else 'IDK7')),
                         ('P0320866','320866'))
        if native:
            self.assertEqual(xml.getroot().tag,'video')
            self.assertEqual(xml.findtext('codigo'),'320866')
            self.assertEqual(xml.findtext('operador'),'EVA')
            self.assertEqual(xml.findtext('arquivo'),Path(result['output_path']).name)
        if cut:
            self.assertFalse(any('mark' in element.tag.casefold() for element in xml.iter()))
        else:
            self.assertEqual((xml.findtext('markIn' if native else 'MarkIn'),
                              xml.findtext('markOut' if native else 'MarkOut')),('125','308'))
            if not native:
                self.assertEqual(xml.find('MarkOut').get('end'),'exclusive')
            self.assertEqual(self.packets(self.source),self.packets(result['output_path']),
                             'O vídeo integral compatível deve conservar os pacotes de imagem sem recodificação')
        self.assertEqual(result['cut_video'],int(cut))
        self.assertEqual(core.sha256(self.source),source_hash)
        with core.db_connection() as db:
            ledger=db.execute('SELECT * FROM automatic_deliveries').fetchone()
            self.assertEqual((ledger['cut_video'],ledger['cm'],ledger['idk7']),(int(cut),'P0320866','320866'))

    def test_physical_cut_omits_marks(self):
        self.exercise(True)

    def test_full_video_remaps_audio_and_writes_original_marks(self):
        self.exercise(False)

    def test_smartware_physical_cut_has_identifiers_operator_and_no_marks(self):
        self.exercise(True,xml_profile='smartware')

    def test_smartware_full_video_has_source_marks_with_exclusive_out(self):
        self.exercise(False,xml_profile='smartware')

    def test_smartware_invalid_identifier_fails_before_transcoding(self):
        core.event(self.job_id,'APROVADO','Teste de cadastro',fps='25/1',mark_in=5,mark_out=12.32,
                   source_path=str(self.root/'SEM_CODIGO.mxf'))
        self.settings['xml_profile']='smartware'
        with patch.object(core,'run_probe') as probe, patch.object(core,'run_transcode') as transcode:
            with self.assertRaisesRegex(ValueError,'CM/IDK7'):
                core.process(self.job_id,settings=self.settings,automatic=True)
            probe.assert_not_called()
            transcode.assert_not_called()


if __name__=='__main__':
    unittest.main()
