"""Integração real do watch-folder; todos os arquivos ficam em área temporária."""
import shutil
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core
from watcher import FolderWatcher


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg/FFprobe fora do PATH')
class TestRealPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='eva_pipeline_')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'entrada'
        self.dest = self.root / 'saida'
        self.source.mkdir()
        self.dest.mkdir()
        database = patch.object(core, 'DB_FILE', self.root / 'historico.sqlite3')
        database.start()
        self.addCleanup(database.stop)
        core.init_db()
        self.settings = core.config()
        self.settings.update(source_folder=str(self.source),destination_folder=str(self.dest),
                             working_folder=str(self.root/'work'),video_profile='teste',
                             stable_seconds=.1,watch_interval_sec=.05,max_attempts=1)

    def create_video(self, path, final_black, broadcast=False):
        # Claquete 5s + black separador 5s + comercial 5s + black final opcional.
        size, rate = ('1920x1080', '30000/1001') if broadcast else ('640x360', '25')
        parts = [f'color=red:s={size}:r={rate}:d=5[v0]',
                 f'color=black:s={size}:r={rate}:d=5[v1]',
                 f'testsrc2=s={size}:r={rate}:d=5[v2]']
        if final_black:
            parts.append(f'color=black:s={size}:r={rate}:d={final_black}[v3]')
        count = 4 if final_black else 3
        parts.append(''.join(f'[v{i}]' for i in range(count))+f'concat=n={count}:v=1:a=0[out0]')
        cmd = ['ffmpeg','-hide_banner','-nostdin','-loglevel','error','-f','lavfi','-i',';'.join(parts),
               '-f','lavfi','-i',f'sine=frequency=440:sample_rate=48000:duration={15+final_black}',
               '-map','0:v','-map','1:a','-c:v','mpeg2video','-b:v','8M','-pix_fmt','yuv422p',
               '-c:a','pcm_s24le','-ar','48000','-ac','2']
        if broadcast:
            cmd.extend(['-flags','+ildct+ilme','-vf','setfield=tff','-field_order','tt','-r',rate])
        cmd.append(str(path))
        result = subprocess.run(cmd,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)

    def exercise_drop(self, final_black, extension, broadcast=False):
        original = self.root / ('original' + extension)
        self.create_video(original,final_black,broadcast=broadcast)
        digest = core.sha256(original)
        done = threading.Event()
        failures = []
        stop = threading.Event()
        def notify(kind,payload):
            if kind == 'progress' and payload['progress'] == 1:
                done.set()
            if kind == 'error':
                failures.append(payload)
                done.set()
            if kind == 'refresh' and any(job['status']=='ERRO' for job in core.jobs()):
                done.set()
        watcher = FolderWatcher(self.settings,notify)
        def run():
            try:
                watcher.run(stop)
            except Exception as exc:
                failures.append(str(exc))
                done.set()
        worker = threading.Thread(target=run)
        worker.start()
        try:
            shutil.copyfile(original,self.source / ('comercial' + extension))
            self.assertTrue(done.wait(45),core.jobs())
        finally:
            stop.set()
            worker.join(15)
        self.assertFalse(worker.is_alive())
        self.assertFalse(failures,failures)
        job = core.jobs()[0]
        self.assertEqual(job['status'],'CONCLUIDO',job)
        second = 1.001 if broadcast else 1.
        self.assertAlmostEqual(job['mark_in'],10.*second,places=3)
        self.assertAlmostEqual(job['mark_out'],15.*second,places=3)
        probe = core.run_probe(job['output_path'])
        self.assertAlmostEqual(core.duration_from_probe(probe),5.*second,places=2)
        counted = subprocess.run(['ffprobe','-v','error','-count_frames','-select_streams','v:0',
                                  '-show_entries','stream=nb_read_frames','-of','json',job['output_path']],
                                 capture_output=True,text=True,timeout=30)
        self.assertEqual(counted.returncode,0,counted.stderr)
        self.assertEqual(int(json.loads(counted.stdout)['streams'][0]['nb_read_frames']),150 if broadcast else 125)
        if broadcast:
            video = next(s for s in probe['streams'] if s['codec_type']=='video')
            self.assertEqual((video['width'],video['height'],video['pix_fmt']),(1920,1080,'yuv422p'))
            self.assertEqual(video['r_frame_rate'],'30000/1001')
            self.assertEqual(video['field_order'],'tt')
        audio = [s for s in probe['streams'] if s['codec_type']=='audio']
        self.assertEqual(len(audio),16)
        self.assertTrue(all(s['codec_name']=='pcm_s24le' and s['channels']==1 and s['sample_rate']=='48000' for s in audio))
        self.assertEqual(core.sha256(original),digest)
        self.assertEqual(core.sha256(Path(job['output_path'])),job['output_sha256'])
        xml = core.ET.parse(job['xml_path'])
        self.assertEqual(xml.findtext('Arquivo'),'comercial.mxf')
        # Uma nova varredura não pode gerar outro evento de entrega.
        retry = FolderWatcher(self.settings)
        retry.settings['stable_seconds']=0
        retry.scan_once()
        with core.db_connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM delivery_events').fetchone()[0],1)

    def test_mxf_with_short_final_black(self):
        self.exercise_drop(1.2,'.mxf')

    def test_mov_without_final_black(self):
        self.exercise_drop(0,'.mov')

    def test_xdcam_hd422_broadcast_profile(self):
        self.settings['video_profile']='xdcam_hd422'
        self.exercise_drop(1.001,'.mxf',broadcast=True)


if __name__ == '__main__':
    unittest.main()
