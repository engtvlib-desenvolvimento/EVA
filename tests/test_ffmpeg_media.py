"""Testes reais de sinal; ignorados quando FFmpeg/FFprobe não estão no PATH."""
import array
import math
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import core


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),
                     'Requer FFmpeg e FFprobe no PATH')
class TestFFmpegAudio(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='eva_audio_')
        self.root=Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def run_ffmpeg(self,args,binary=False):
        result=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin',
                               '-y',*map(str,args)],capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf-8',errors='replace'))
        return result.stdout

    def make_source(self,separate=False,duration=5):
        target=self.root/('mono.mkv' if separate else 'stereo.mkv')
        command=['-f','lavfi','-i',
                 f'aevalsrc=0.08*sin(2*PI*440*t)|0.02*sin(2*PI*440*t):s=48000:d={duration}:c=stereo']
        if separate:
            command+=['-filter_complex','[0:a]channelsplit=channel_layout=stereo[l][r]',
                      '-map','[l]','-map','[r]']
        command+=['-c:a','pcm_f32le',target]
        self.run_ffmpeg(command)
        return target

    def render(self,source,channels=None,length=5,seek=None):
        settings={'audio_channel_map':channels or ['auto']*16,'normalize_audio':True,
                  'lufs_target':-23,'lra_target':7,'true_peak_db':-2}
        probe=core.run_probe(source)
        filters=core.filters_for_audio(probe,settings,length)
        target=self.root/'treated.mkv'
        command=['-ss',str(seek)] if seek is not None else []
        command+=['-i',source,'-filter_complex',filters]
        for i in range(16):
            command+=['-map',f'[a{i}]']
        command+=['-c:a','pcm_s24le','-ar','48000','-ac','1','-t',str(length),target]
        self.run_ffmpeg(command)
        streams=core.run_probe(target)['streams']
        self.assertEqual(len(streams),16)
        for stream in streams:
            self.assertEqual(stream['channels'],1)
            self.assertEqual(stream['sample_rate'],'48000')
            self.assertEqual(stream['codec_name'],'pcm_s24le')
        return target

    def samples(self,target,stream,expected_samples=240000):
        raw=self.run_ffmpeg(['-i',target,'-map',f'0:a:{stream}',
                             '-f','f32le','-c:a','pcm_f32le','pipe:1'])
        values=array.array('f')
        values.frombytes(raw)
        if sys.byteorder!='little':
            values.byteswap()
        self.assertEqual(len(values),expected_samples)
        return values

    def rms(self,values):
        return math.sqrt(sum(value*value for value in values)/len(values))

    def test_loudnorm_preserves_stereo_balance_for_stereo_and_mono_inputs(self):
        for separate in (False,True):
            with self.subTest(separate_mono_streams=separate):
                treated=self.render(self.make_source(separate=separate))
                left=self.rms(self.samples(treated,0))
                right=self.rms(self.samples(treated,1))
                self.assertGreater(left,0.001)
                self.assertAlmostEqual(left/right,4.0,delta=0.002)
                self.assertAlmostEqual(self.rms(self.samples(treated,15)),0,delta=1e-7)

    def test_explicit_swap_duplication_and_silence(self):
        channels=['0:1','0:0','0:0','0:0']+['silence']*12
        treated=self.render(self.make_source(),channels)
        swapped_left=self.rms(self.samples(treated,0))
        swapped_right=self.rms(self.samples(treated,1))
        self.assertAlmostEqual(swapped_left/swapped_right,0.25,delta=0.002)
        self.assertEqual(self.samples(treated,2),self.samples(treated,3))
        self.assertAlmostEqual(self.rms(self.samples(treated,15)),0,delta=1e-7)

    def test_source_without_audio_yields_sixteen_silent_channels(self):
        source=self.root/'no_audio.mkv'
        self.run_ffmpeg(['-f','lavfi','-i','color=c=blue:s=32x32:r=25:d=5',
                         '-an','-c:v','ffv1',source])
        treated=self.render(source)
        for i in (0,15):
            self.assertAlmostEqual(self.rms(self.samples(treated,i)),0,delta=1e-7)

    def test_fractional_broadcast_duration_after_seek_preserves_all_samples(self):
        # 150 frames a 30000/1001 fps = 5.005s, não múltiplo de 100ms.
        # O flush dinâmico do loudnorm no FFmpeg 9 perdia 4560 amostras.
        treated=self.render(self.make_source(duration=16.016),length=5.005,seek=10.01)
        left=self.samples(treated,0,expected_samples=240240)
        right=self.samples(treated,1,expected_samples=240240)
        self.assertAlmostEqual(self.rms(left)/self.rms(right),4.0,delta=0.002)
        reference=self.rms(left[48000:52800])
        # Comprova sinal no início/fim e nos blocos onde o flush perdia áudio;
        # somente acrescentar silêncio no final não satisfaz esta regressão.
        for start in (0,96000,100800,len(left)-4800):
            with self.subTest(start_sample=start):
                self.assertAlmostEqual(self.rms(left[start:start+4800])/reference,
                                       1.0,delta=0.03)


if __name__=='__main__':
    unittest.main()
