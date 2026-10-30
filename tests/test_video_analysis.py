"""Precisão dos eventos de black no EOF e evidência da claquete estática."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import media_analysis


class TestVideoAnalysisParser(unittest.TestCase):
    def parse(self,text,duration=10,minimum=.24):
        return media_analysis.parse_scan_output(text,duration,Fraction(25),{'black_min_sec':minimum})

    def test_eof_closes_black_at_exclusive_duration(self):
        result=self.parse('lavfi.black_start=8\nblack_start:8 black_end:9.96 black_duration:1.96')
        self.assertEqual(result['black_intervals'],[(8.,10)])

    def test_last_picture_does_not_turn_preceding_black_into_eof(self):
        result=self.parse('lavfi.black_start=8\nlavfi.black_end=9.96\n'
                          'black_start:8 black_end:9.96 black_duration:1.96')
        self.assertEqual(result['black_intervals'],[(8.,9.96)])
        self.assertTrue(result['exact_intervals'])

    def test_single_frame_final_black_is_retained(self):
        result=self.parse('lavfi.black_start=9.96\nblack_start:9.96 black_end:9.96 black_duration:0')
        self.assertEqual(result['black_intervals'],[(9.96,10)])

    def test_short_internal_black_is_filtered(self):
        result=self.parse('lavfi.black_start=3\nlavfi.black_end=3.04\n'
                          'lavfi.black_start=9.96')
        self.assertEqual(result['black_intervals'],[(9.96,10)])

    def test_a_picture_frame_between_blacks_is_preserved(self):
        result=media_analysis.parse_scan_output(
            'lavfi.black_start=1\nlavfi.black_end=2\n'
            'lavfi.black_start=2.0166833\nlavfi.black_end=3\n',
            10,Fraction(60000,1001),{'black_min_sec':.24})
        self.assertEqual(len(result['black_intervals']),2)

    def test_freeze_metadata_is_not_duplicated_by_log(self):
        result=self.parse('lavfi.freezedetect.freeze_start: 0\n'
                          'lavfi.freezedetect.freeze_start=0\n'
                          'lavfi.freezedetect.freeze_end: 3.003\n'
                          'lavfi.freezedetect.freeze_end=3.003\n')
        self.assertEqual(result['slate_intervals'],[(0.,3.003)])

    def test_summary_only_fallback(self):
        result=self.parse('black_start:1 black_end:2 black_duration:1\n'
                          'black_start:8 black_end:9.96 black_duration:1.96')
        self.assertEqual(result['black_intervals'],[(1.,2.),(8.,9.96)])
        self.assertFalse(result['exact_intervals'])

    def test_slate_and_adjacent_black_remain_distinct_freezes(self):
        result=self.parse('lavfi.freezedetect.freeze_start=0\n'
                          'lavfi.freezedetect.freeze_end=3\n'
                          'lavfi.freezedetect.freeze_start=3\n'
                          'lavfi.freezedetect.freeze_end=5\n')
        self.assertEqual(result['slate_intervals'],[(0.,3.),(3.,5.)])

    def test_no_black_is_exact_empty_result(self):
        result=self.parse('frame=250 time=10.00')
        self.assertEqual(result['black_intervals'],[])
        self.assertTrue(result['exact_intervals'])

    @patch('media_analysis.subprocess.run')
    def test_scan_uses_metadata_and_relative_timestamps(self,run):
        run.return_value=SimpleNamespace(returncode=0,stderr='lavfi.black_start=9.96')
        result=media_analysis.scan_video('sample.mxf',10,Fraction(25),{})
        self.assertEqual(result['black_intervals'],[(9.96,10)])
        command=run.call_args.args[0]
        filters=command[command.index('-vf')+1]
        self.assertIn('setpts=PTS-STARTPTS,blackdetect=',filters)
        self.assertIn('blackdetect=d=0:',filters)
        self.assertIn('freezedetect=',filters)
        self.assertIn('metadata=mode=print',filters)


@unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg fora do PATH')
class TestVideoAnalysisFFmpeg(unittest.TestCase):
    def scan(self,graph,duration):
        with tempfile.TemporaryDirectory(prefix='eva_scan_') as directory:
            source=Path(directory)/'source.mkv'
            rendered=subprocess.run(['ffmpeg','-hide_banner','-nostdin','-loglevel','error',
                                     '-f','lavfi','-i',graph,'-c:v','ffv1',str(source)],
                                    capture_output=True,text=True,timeout=20)
            self.assertEqual(rendered.returncode,0,rendered.stderr)
            return media_analysis.scan_video(source,duration,Fraction(25),{})

    def test_true_one_frame_final_black(self):
        result=self.scan('color=red:s=320x180:r=25:d=1[r];'
                         'color=black:s=320x180:r=25:d=0.04[b];'
                         '[r][b]concat=n=2:v=1:a=0[out0]',1.04)
        self.assertEqual(result['black_intervals'],[(1.,1.04)])
        self.assertEqual(result['slate_intervals'],[(0.,1.)])

    def test_picture_after_black_on_last_frame_is_preserved(self):
        result=self.scan('color=red:s=320x180:r=25:d=1[r];'
                         'color=black:s=320x180:r=25:d=1[b];'
                         'color=blue:s=320x180:r=25:d=0.04[p];'
                         '[r][b][p]concat=n=3:v=1:a=0[out0]',2.04)
        self.assertEqual(result['black_intervals'],[(1.,2.)])


if __name__=='__main__':
    unittest.main()
