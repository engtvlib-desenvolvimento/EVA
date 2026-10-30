"""Cortes automáticos e estrutura do remapeamento de áudio."""
import math
import sys
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import core


class TestAutomaticMarks(unittest.TestCase):
    def test_last_picture_after_black_is_preserved_with_exact_metadata(self):
        mi,mo,confidence,_=core.detect_marks(40,Fraction(25),[(3,5),(38,39.96)],
            slate_intervals=[(0,3)],exact_intervals=True)
        self.assertEqual((mi,mo,confidence),(5,40,'ALTA'))

    def test_real_chef_variable_separator_and_fractional_duration(self):
        fps=Fraction(30000,1001)
        mi,mo,confidence,_=core.detect_marks(55.3553,fps,
            [(3.003,5.238567),(50.617233,55.321933)])
        self.assertEqual(round(mi*fps),157)
        self.assertEqual(round(mo*fps),1517)
        self.assertEqual(confidence,'ALTA')
        self.assertAlmostEqual(mo-mi,45.3786666667,places=6)

    def test_variable_separator_without_final_black_preserves_content_to_eof(self):
        mi,mo,confidence,message=core.detect_marks(40.32,Fraction(25),[(3,5.32)])
        self.assertEqual((mi,mo,confidence),(5.32,40.32,'ALTA'))
        self.assertIn('sem black final',message)

    def test_variable_separator_accepts_unconventional_content_duration(self):
        result=core.detect_marks(53,Fraction(25),[(3,5.32),(48,53)])
        self.assertEqual(result[:3],(5.32,48,'ALTA'))

    def test_variable_separator_accepts_unconventional_duration_without_final_black(self):
        result=core.detect_marks(48,Fraction(25),[(3,5.32)])
        self.assertEqual(result[:3],(5.32,48,'ALTA'))

    def test_twelve_real_source_layouts_have_independent_entry_and_exit(self):
        # Durações, blacks e frames medidos no lote real. O título/duração
        # nominal do comercial não deve determinar ou impedir o corte.
        cases=[
            (47.747708,[(3.003,5.005),(44.878167,47.714333)],150,1345),
            (35.4354,[(3.003,5.005)],150,1062),
            (35.268567,[(3.003,5.005)],150,1057),
            (40.04,[(3.003,5.005),(35.5355,40.006633)],150,1065),
            (40.04,[(3.003,5.005),(35.5355,40.006633)],150,1065),
            (40.373667,[(2.969633,4.971633),(36.036,40.3403)],149,1080),
            (40.407042,[(2.969633,4.971633),(35.5355,40.373667)],149,1065),
            (55.121733,[(3.003,5.005),(50.5505,55.088367)],150,1515),
            (55.3553,[(3.003,5.238567),(50.617233,55.321933)],157,1517),
            (45.045,[(3.003,5.005),(40.04,45.011633)],150,1200),
            (45.045,[(3.003,5.005),(40.04,45.011633)],150,1200),
            (38.505133,[(4.004,6.006),(36.503133,38.471767)],180,1094),
        ]
        fps=Fraction(30000,1001)
        for index,(duration,intervals,first,last) in enumerate(cases,1):
            with self.subTest(source=index):
                mi,mo,confidence,_=core.detect_marks(duration,fps,intervals)
                self.assertEqual(confidence,'ALTA')
                self.assertAlmostEqual(mi,float(first/fps),places=6)
                self.assertAlmostEqual(mo,float(last/fps),places=6)
                self.assertEqual(round((mo-mi)*fps),last-first)

    def test_variable_separator_outside_opening_window_is_not_automatic(self):
        result=core.detect_marks(52.32,Fraction(25),[(15,17.32),(47.32,52.32)])
        self.assertEqual(result[2],'BAIXA')

    def test_static_slate_confirms_variable_entry_without_final_black(self):
        result=core.detect_marks(40.32,Fraction(25),[(3,5.32)],
                                 slate_intervals=[(0,3)])
        self.assertEqual(result[:3],(5.32,40.32,'ALTA'))

    def test_explicitly_missing_slate_evidence_blocks_automatic_cut(self):
        result=core.detect_marks(40,Fraction(25),[(3,5)],slate_intervals=[])
        self.assertEqual(result[2],'BAIXA')

    def test_freeze_must_cover_initial_slate_and_reach_separator(self):
        for freezes in ([(1,3)],[(0,1),(2,3)],[(0,2.8)]):
            with self.subTest(freezes=freezes):
                result=core.detect_marks(40,Fraction(25),[(3,5)],slate_intervals=freezes)
                self.assertEqual(result[2],'BAIXA')

    def test_slate_evidence_tolerates_frame_rounding(self):
        result=core.detect_marks(40,Fraction(25),[(3,5)],
                                 slate_intervals=[(.04,2.96)])
        self.assertEqual(result[:3],(5,40,'ALTA'))

    def test_slate_evidence_beyond_two_frame_tolerance_is_rejected(self):
        for freezes in ([ (.12,3) ],[ (0,2.88) ]):
            with self.subTest(freezes=freezes):
                result=core.detect_marks(40,Fraction(25),[(3,5)],slate_intervals=freezes)
                self.assertEqual(result[2],'BAIXA')

    def test_slate_evidence_selects_entry_among_multiple_black_intervals(self):
        result=core.detect_marks(45,Fraction(25),[(3,5),(10,15),(40,45)],
                                 slate_intervals=[(0,3)])
        self.assertEqual(result[:3],(5,40,'ALTA'))

    def test_confirmed_entry_preserves_long_internal_black(self):
        result=core.detect_marks(45,Fraction(25),[(3,5),(20,25),(40,45)],
                                 slate_intervals=[(0,3)])
        self.assertEqual(result[:3],(5,40,'ALTA'))
        self.assertEqual(result[1]-result[0],35)

    def test_slate_evidence_does_not_create_a_missing_black_separator(self):
        result=core.detect_marks(40,Fraction(25),[],slate_intervals=[(0,3)])
        self.assertEqual(result[2],'BAIXA')

    def test_short_prelude_is_not_a_confirmed_static_slate(self):
        result=core.detect_marks(40,Fraction(25),[(.2,2.2)],
                                 slate_intervals=[(0,.2)])
        self.assertEqual(result[2],'BAIXA')

    def test_rounded_ffmpeg_timestamps_do_not_discard_a_frame(self):
        fps=Fraction(30000,1001)
        # FFmpeg usa seis algarismos significativos no relatório de black.
        mi,mo,confidence,_=core.detect_marks(47.1805,fps,[(11.1445,16.1495),(46.1795,47.1471)])
        self.assertEqual(round(mi*fps),484)
        self.assertEqual(round(mo*fps),1384)
        self.assertEqual(confidence,'ALTA')

    def test_no_final_black_preserves_full_commercial(self):
        mi,mo,confidence,message=core.detect_marks(40,Fraction(25),[(5,10)])
        self.assertEqual((mi,mo,confidence),(10,40,'ALTA'))
        self.assertIn('sem black final',message)

    def test_short_final_black_is_removed(self):
        mi,mo,confidence,_=core.detect_marks(41,Fraction(25),[(5,10),(40,40.96)])
        self.assertEqual((mi,mo,confidence),(10,40,'ALTA'))

    def test_one_frame_final_black_is_removed_when_detected(self):
        result=core.detect_marks(40.04,Fraction(25),[(3,5),(40,40.04)])
        self.assertEqual(result[:3],(5,40,'ALTA'))

    def test_long_final_black_is_removed(self):
        self.assertEqual(core.detect_marks(52,Fraction(25),[(5,10),(40,52)])[:3],
                         (10,40,'ALTA'))

    def test_nonzero_picture_after_black_is_preserved(self):
        # An internal dark scene ends 0.4 seconds before the final picture.
        result=core.detect_marks(40,Fraction(25),[(5,10),(38,39.6)])
        self.assertEqual(result[:3],(10,40,'ALTA'))

    def test_black_inside_commercial_is_ambiguous(self):
        result=core.detect_marks(40,Fraction(25),[(5,10),(20,25)])
        self.assertEqual(result[2],'BAIXA')

    def test_ambiguous_opening_candidates_are_not_automatic(self):
        result=core.detect_marks(55,Fraction(25),[(1,6),(10,15),(45,55)])
        self.assertEqual(result[2],'BAIXA')

    def test_no_blind_five_second_slate_cut(self):
        result=core.detect_marks(35,Fraction(25),[])
        self.assertEqual(result[0],0)
        self.assertEqual(result[2],'BAIXA')

    def test_source_only_black_is_not_a_commercial(self):
        self.assertEqual(core.detect_marks(10,Fraction(25),[(0,10)])[2],'BAIXA')

    def test_invalid_duration_and_rate(self):
        for duration,fps in ((0,25),(30,0),(math.nan,25),(30,math.inf)):
            with self.subTest(duration=duration,fps=fps):
                with self.assertRaises(ValueError):
                    core.detect_marks(duration,fps,[])

    @patch('core.subprocess.run')
    def test_blackdetect_uses_relative_timestamps(self,run):
        run.return_value=SimpleNamespace(returncode=0,stderr=(
            'black_start:5 black_end:10 black_duration:5\n'
            'black_start:40 black_end:41 black_duration:1'))
        self.assertEqual(core.black_intervals('a.mxf',41,{}),[(5,10),(40,41)])
        command=run.call_args.args[0]
        self.assertIn('setpts=PTS-STARTPTS,blackdetect=',command[command.index('-vf')+1])


class TestAudioMapping(unittest.TestCase):
    def settings(self,**extra):
        return {'audio_channel_map':['auto']*16,'normalize_audio':True,**extra}

    def test_stereo_pair_is_normalized_together(self):
        probe={'streams':[{'codec_type':'audio','channels':2}]}
        filters=core.filters_for_audio(probe,self.settings(),5)
        self.assertEqual(filters.count('loudnorm='),1)
        self.assertIn('join=inputs=2:channel_layout=stereo',filters)
        self.assertEqual(filters.count('anullsrc='),14)

    def test_mono_streams_share_gain_with_partner(self):
        probe={'streams':[{'codec_type':'audio','channels':1} for _ in range(16)]}
        filters=core.filters_for_audio(probe,self.settings(),5)
        self.assertEqual(filters.count('loudnorm='),8)
        self.assertNotIn('[0:a:0]loudnorm',filters)
        self.assertNotIn('anullsrc=',filters)

    def test_explicit_mapping_can_duplicate_and_swap_channels(self):
        probe={'streams':[{'codec_type':'audio','channels':2}]}
        channels=['0:1','0:0','0:0','0:1']+['silence']*12
        filters=core.filters_for_audio(probe,self.settings(audio_channel_map=channels),5)
        self.assertIn('asplit=4',filters)
        self.assertEqual(filters.count('loudnorm='),2)
        self.assertIn('[a15]',filters)

    def test_no_audio_creates_silence_without_normalization(self):
        filters=core.filters_for_audio({'streams':[]},self.settings(),5)
        self.assertEqual(filters.count('anullsrc='),16)
        self.assertNotIn('loudnorm=',filters)

    def test_invalid_mapping_rejected(self):
        probe={'streams':[{'codec_type':'audio','channels':2}]}
        for channels in (['auto']*15,['0:2']+['auto']*15,['bad']+['auto']*15):
            with self.subTest(channels=channels):
                with self.assertRaises(ValueError):
                    core.filters_for_audio(probe,self.settings(audio_channel_map=channels),5)


class TestVideoDuration(unittest.TestCase):
    def test_video_edit_units_take_precedence_over_longer_audio_and_container(self):
        probe={'format':{'duration':'10.0'},'streams':[
            {'codec_type':'video','duration_ts':225,'time_base':'1/25','duration':'9.5'},
            {'codec_type':'audio','duration':'10.0'}]}
        self.assertEqual(core.video_duration_from_probe(probe),9.)

    def test_video_stream_duration_precedes_container_when_edit_units_are_missing(self):
        probe={'format':{'duration':'10.0'},'streams':[
            {'codec_type':'video','duration':'9.0'},
            {'codec_type':'audio','duration':'10.0'}]}
        self.assertEqual(core.video_duration_from_probe(probe),9.)

    def test_container_duration_is_fallback_when_video_has_no_duration(self):
        probe={'format':{'duration':'10.0'},'streams':[{'codec_type':'video'}]}
        self.assertEqual(core.video_duration_from_probe(probe),10.)

    def test_fractional_broadcast_video_duration_preserves_last_frame(self):
        probe={'format':{'duration':'36.0'},'streams':[
            {'codec_type':'video','duration_ts':1057,'time_base':'1001/30000'}]}
        self.assertAlmostEqual(core.video_duration_from_probe(probe),1057*1001/30000,places=8)


if __name__=='__main__':
    unittest.main()
