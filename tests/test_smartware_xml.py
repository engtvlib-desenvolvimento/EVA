"""Contrato XML Smartware fornecido para a instalação, sem conexão ao importador."""
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from xml.etree import ElementTree as ET

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import integration_metadata as metadata


class TestSmartwareXml(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / 'material.xml'
        self.rate = Fraction(30000, 1001)
        self.duration = float(Fraction(1431, 1) / self.rate)
        self.job = dict(source_path=str(self.root / 'P0320866_ENCONTRO & Promo.mxf'),
                        fps='30000/1001', mark_in=float(Fraction(150, 1) / self.rate),
                        mark_out=float(Fraction(1345, 1) / self.rate))
        self.settings = dict(xml_profile='smartware', cut_video=False)

    def render(self, *, job=None, duration=None, **settings):
        result = metadata.write_xml(job or self.job, self.duration if duration is None else duration,
                                    'a' * 64, self.target, dict(self.settings, **settings))
        self.assertEqual(result, self.target)
        return ET.parse(self.target).getroot()

    def test_exact_layout_names_identifiers_and_exclusive_frame_marks(self):
        root = self.render()
        expected = {
            'codigo': '320866', 'arquivo': 'P0320866_ENCONTRO & Promo.mxf',
            'titulo': 'ENCONTRO & Promo', 'grupo': 'Opec', 'operador': 'EVA',
            'cm': 'P0320866', 'idk7': '320866', 'markIn': '150',
            'markOut': '1345', 'remover': '0'
        }
        self.assertEqual(root.tag, 'video')
        self.assertEqual(root.attrib, {})
        self.assertEqual(len(root), len(expected), 'Não deve haver campos extras ou duplicados')
        self.assertEqual({element.tag: element.text for element in root}, expected)
        self.assertTrue(all(not element.attrib and len(element) == 0 for element in root))

    def test_iso_8859_1_accents_xml_escaping_and_unicode_references(self):
        title = 'São João & Promo <Especial> 🎥'
        job = dict(self.job, source_path=str(self.root / ('P0320866_' + title + '.mov')))
        root = self.render(job=job)
        raw = self.target.read_bytes()
        self.assertIn(b'ISO-8859-1', raw.splitlines()[0])
        self.assertIn('São João'.encode('ISO-8859-1'), raw)
        self.assertIn(b'&amp;', raw)
        self.assertIn(b'&lt;Especial&gt;', raw)
        self.assertIn(b'&#', raw, 'Caracteres fora Latin-1 devem ser representados em XML')
        self.assertNotIn('São João'.encode('utf-8'), raw)
        self.assertEqual(root.findtext('titulo'), title)
        self.assertEqual(root.findtext('arquivo'), 'P0320866_' + title + '.mxf')

    def test_cut_video_has_no_mark_fields_or_mark_attributes(self):
        # Marcas da origem não pertencem ao cadastro de um arquivo já recortado.
        root = self.render(cut_video=True,
                           duration=self.job['mark_out'] - self.job['mark_in'])
        self.assertEqual({child.tag for child in root},
                         {'codigo','arquivo','titulo','grupo','operador','cm','idk7','remover'})
        self.assertFalse(any('mark' in element.tag.casefold() or
                             any('mark' in name.casefold() for name in element.attrib)
                             for element in root.iter()))

    def test_inclusive_mark_out_is_an_explicit_alternative(self):
        root = self.render(smartware_mark_out='inclusive')
        self.assertEqual(root.findtext('markIn'), '150')
        self.assertEqual(root.findtext('markOut'), '1344')

    def test_explicit_exclusive_mark_out_preserves_first_frame_after_content(self):
        root = self.render(smartware_mark_out='exclusive')
        self.assertEqual(root.findtext('markOut'), '1345')

    def test_no_final_black_exclusive_out_is_exact_source_eof(self):
        root = self.render(job=dict(self.job, mark_out=self.duration))
        self.assertEqual(root.findtext('markOut'), '1431')
        self.assertEqual(int(root.findtext('markOut')) - int(root.findtext('markIn')), 1281)

    def test_no_final_black_inclusive_out_is_last_source_frame(self):
        root = self.render(job=dict(self.job, mark_out=self.duration), smartware_mark_out='inclusive')
        self.assertEqual(root.findtext('markOut'), '1430')

    def test_source_eof_ignores_microsecond_container_rounding(self):
        root = self.render(job=dict(self.job, mark_out=self.duration), duration=self.duration + .000008)
        self.assertEqual(root.findtext('markOut'), '1431')

    def test_half_frame_tolerance_cannot_round_mark_out_beyond_source_eof(self):
        job = dict(self.job,fps='25',mark_in=0.,mark_out=.14)
        # Três frames em 0,12 s; round(0,14*25) produziria um quarto frame.
        with self.assertRaises(ValueError):
            self.render(job=job,duration=.12)
        self.assertFalse(self.target.exists())

    def test_custom_group_is_text_escaped_and_trimmed(self):
        root = self.render(smartware_group='  Programação & Promo  ')
        self.assertEqual(root.findtext('grupo'), 'Programação & Promo')
        self.assertIn(b'&amp;', self.target.read_bytes())

    def test_title_removes_only_initial_identifier_and_leading_separators(self):
        for filename in ('P0320866_ENCONTRO & Promo.mxf',
                         'P0320866 - ENCONTRO & Promo.mov',
                         'P0320866...___ENCONTRO & Promo.mxf'):
            with self.subTest(filename=filename):
                root = self.render(job=dict(self.job, source_path=str(self.root / filename)))
                self.assertEqual(root.findtext('titulo'), 'ENCONTRO & Promo')
                self.assertEqual(root.findtext('codigo'), '320866')

    def test_smartware_needs_no_template_file(self):
        settings = dict(self.settings, smartware_xml_template_cut=str(self.root/'missing-cut.xml'),
                        smartware_xml_template_uncut=str(self.root/'missing-uncut.xml'))
        metadata.validate_xml_settings(settings)
        metadata.write_xml(self.job, self.duration, 'a' * 64, self.target, settings)
        self.assertEqual(ET.parse(self.target).getroot().tag, 'video')

    def test_invalid_identifier_blocks_writing_without_replacing_existing_xml(self):
        sentinel = b'<existing/>'
        self.target.write_bytes(sentinel)
        for name in ('sem_codigo.mxf', '123456_TITULO.mxf', 'P_TITULO.mxf', 'P0320866SemSeparador.mxf'):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.render(job=dict(self.job, source_path=str(self.root/name)))
                self.assertEqual(self.target.read_bytes(), sentinel)

    def test_invalid_mark_out_conventions_are_rejected_before_writing(self):
        for value in ('last', 'INCLUSIVE', '', None, 1):
            with self.subTest(value=value):
                settings = dict(self.settings, smartware_mark_out=value)
                with self.assertRaises(ValueError):
                    metadata.validate_xml_settings(settings)
                with self.assertRaises(ValueError):
                    metadata.write_xml(self.job, self.duration, 'a' * 64, self.target, settings)
                self.assertFalse(self.target.exists())

    def test_empty_nontext_and_xml_invalid_groups_are_rejected(self):
        for value in ('', ' \t\n ', None, 123, False, [], {}, 'Opec\x00'):
            with self.subTest(value=value):
                settings = dict(self.settings, smartware_group=value)
                with self.assertRaises(ValueError):
                    metadata.validate_xml_settings(settings)
                with self.assertRaises(ValueError):
                    metadata.write_xml(self.job, self.duration, 'a' * 64, self.target, settings)
                self.assertFalse(self.target.exists())

    def test_outside_source_or_reversed_marks_cannot_be_serialized(self):
        for marks in ({'mark_in':-1}, {'mark_out':self.duration + 1},
                      {'mark_in':self.job['mark_out']}, {'mark_out':0}):
            with self.subTest(marks=marks):
                with self.assertRaises(ValueError):
                    self.render(job=dict(self.job, **marks))
                self.assertFalse(self.target.exists())

    def test_title_is_available_in_metadata_context_and_vendor_template(self):
        context = metadata.metadata_context(self.job,self.duration,'a' * 64,self.settings)
        self.assertEqual(context['TITLE'],'ENCONTRO & Promo')
        template = self.root / 'template.xml'
        template.write_text('<Import cm="{{CM}}" id="{{IDK7}}" file="{{FILENAME}}">'
                            '<Title>{{TITLE}}</Title></Import>',encoding='utf-8')
        root = self.render(cut_video=True, xml_profile='smartware_template',
                           smartware_xml_template_cut=str(template))
        self.assertEqual(root.findtext('Title'),'ENCONTRO & Promo')
        self.assertNotIn('{{', self.target.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
