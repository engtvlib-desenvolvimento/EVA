"""Cadastro e instruções XML sem presumir o schema proprietário do SmartWare."""
import math
import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import integration_metadata as metadata


class TestMaterialIdentifiers(unittest.TestCase):
    def test_cm_and_numeric_idk7(self):
        self.assertEqual(metadata.material_identifiers('P0320866_ENCONTRO 30-10.mxf'),
                         ('P0320866', '320866'))
        self.assertEqual(metadata.material_identifiers('p0000042 Chamada.mov'), ('P0000042', '42'))
        self.assertEqual(metadata.material_identifiers('CM00001234.mxf'), ('CM00001234', '1234'))
        self.assertEqual(metadata.material_identifiers('P0000.mxf'), ('P0000', '0'))

    def test_identifier_must_be_first_complete_token(self):
        for name in ('chamada P0320866.mxf', 'P0320866ALTAS.mxf', '0320866.mxf',
                     'P_0320866.mxf', 'sem_codigo.mxf'):
            with self.subTest(name=name):
                self.assertEqual(metadata.material_identifiers(name), (None, None))
                with self.assertRaises(ValueError):
                    metadata.material_identifiers(name, required=True)

    def test_only_first_token_supplies_identifier(self):
        self.assertEqual(metadata.material_identifiers('P0320866_P9999999.mxf'),
                         ('P0320866', '320866'))


class TestMetadataXML(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.target = self.root / 'output.xml'
        self.job = dict(source_path='P0320866_ENCONTRO & Promo.mxf',fps='30000/1001',
                        mark_in=157*1001/30000,mark_out=1517*1001/30000)

    def template_settings(self, xml, cut=True):
        template = self.root / ('cut.xml' if cut else 'uncut.xml')
        template.write_text(xml, encoding='utf-8')
        return dict(xml_profile='smartware_template',cut_video=cut,
                    **{'smartware_xml_template_cut' if cut else 'smartware_xml_template_uncut':str(template)})

    def render(self, settings=None, job=None, duration=55.3553, digest='a'*64):
        metadata.write_xml(job or self.job,duration,digest,self.target,settings or {})
        return ET.parse(self.target).getroot()

    def test_cut_draft_contains_identifiers_and_no_mark_fields(self):
        job = dict(source_path=self.job['source_path'], fps=self.job['fps'])
        root = self.render(job=job,duration=45.378666667)
        self.assertEqual(root.get('schema'),'EVA-INGEST-DRAFT-1')
        self.assertEqual(root.findtext('CM'),'P0320866')
        self.assertEqual(root.findtext('IDK7'),'320866')
        self.assertEqual(root.findtext('Arquivo'),'P0320866_ENCONTRO & Promo.mxf')
        self.assertEqual(root.findtext('CorteAplicado'),'true')
        self.assertFalse(any('markin' in str(element.tag).lower() or 'markout' in str(element.tag).lower()
                             for element in root.iter()))
        self.assertNotIn('MarkIn',self.target.read_text(encoding='utf-8'))
        self.assertNotIn('MarkOut',self.target.read_text(encoding='utf-8'))

    def test_uncut_draft_uses_original_frames_and_exclusive_out(self):
        root = self.render({'cut_video':False})
        self.assertEqual(root.findtext('MarkIn'),'157')
        self.assertEqual(root.findtext('MarkOut'),'1517')
        self.assertEqual(root.find('MarkIn').attrib,{'unit':'frames','base':'0'})
        self.assertEqual(root.find('MarkOut').attrib,{'unit':'frames','base':'0','end':'exclusive'})
        self.assertEqual(root.findtext('MarkInSegundos'),'5.238566667')
        self.assertEqual(root.findtext('MarkOutSegundos'),'50.617233333')
        self.assertEqual(root.findtext('FPS'),'30000/1001')
        self.assertEqual(root.findtext('DuracaoSegundos'),'55.355300000')

    def test_invalid_identifier_is_omitted_only_in_draft(self):
        root = self.render(job=dict(self.job,source_path='DEMO.mxf'))
        self.assertIsNone(root.find('CM'))
        self.assertIsNone(root.find('IDK7'))
        settings=self.template_settings('<Ingest cm="{{CM}}" id="{{IDK7}}" file="{{FILENAME}}"/>')
        with self.assertRaises(ValueError):
            self.render(settings,job=dict(self.job,source_path='DEMO.mxf'))

    def test_vendor_template_escapes_values_and_preserves_namespace(self):
        settings=self.template_settings('<v:Import xmlns:v="urn:installation" cm="{{CM}}" id="{{IDK7}}">'
                                        '<v:File>{{FILENAME}}</v:File><v:Digest>{{SHA256}}</v:Digest></v:Import>')
        root=self.render(settings,digest='test<&>"')
        self.assertEqual(root.tag,'{urn:installation}Import')
        self.assertEqual(root.get('cm'),'P0320866')
        self.assertEqual(root.get('id'),'320866')
        self.assertEqual(root.findtext('{urn:installation}File'),'P0320866_ENCONTRO & Promo.mxf')
        self.assertEqual(root.findtext('{urn:installation}Digest'),'test<&>"')
        self.assertIn('&amp;',self.target.read_text(encoding='utf-8'))

    def test_uncut_vendor_template_supports_inclusive_last_frame_explicitly(self):
        settings=self.template_settings('<Import cm="{{CM}}" id="{{IDK7}}" file="{{FILENAME}}">'
                                        '<Start>{{MARK_IN_FRAMES}}</Start><Last>{{MARK_OUT_LAST_FRAME}}</Last>'
                                        '<OutSeconds>{{MARK_OUT_SECONDS}}</OutSeconds><Rate>{{FPS}}</Rate></Import>',cut=False)
        root=self.render(settings)
        self.assertEqual(root.findtext('Start'),'157')
        self.assertEqual(root.findtext('Last'),'1516')
        self.assertEqual(root.findtext('OutSeconds'),'50.617233333')
        self.assertEqual(root.findtext('Rate'),'30000/1001')

    def test_only_selected_template_is_required(self):
        settings=self.template_settings('<Import cm="{{CM}}" id="{{IDK7}}" file="{{FILENAME}}"/>')
        settings['smartware_xml_template_uncut']=str(self.root/'does-not-exist.xml')
        metadata.validate_xml_settings(settings)
        settings['cut_video']=False
        with self.assertRaises(FileNotFoundError):
            metadata.validate_xml_settings(settings)

    def test_unknown_or_unresolved_template_tokens_are_rejected(self):
        for token in ('{{UNKNOWN}}','{{CM.invalid}}','{{CM','CM}}','{{MARK_IN_FRAMES}}'):
            settings=self.template_settings('<Import cm="{{CM}}" id="{{IDK7}}" file="{{FILENAME}}">'
                                            '<Extra>'+token+'</Extra></Import>')
            with self.subTest(token=token),self.assertRaises(ValueError):
                metadata.validate_xml_settings(settings)

    def test_vendor_template_requires_identifiers_and_filename(self):
        for body in ('<Import/>','<Import cm="{{CM}}" file="{{FILENAME}}"/>',
                     '<Import id="{{IDK7}}" file="{{FILENAME}}"/>',
                     '<Import cm="{{CM}}" id="{{IDK7}}"/>'):
            with self.subTest(body=body),self.assertRaises(ValueError):
                metadata.validate_xml_settings(self.template_settings(body))

    def test_cut_template_rejects_static_mark_elements_attributes_and_named_fields(self):
        fields=('<MarkIn>0</MarkIn>','<MarkOutFrames>1500</MarkOutFrames>',
                '<Entry MarkOut="1500"/>','<Field name="MarkIn" value="0"/>',
                '<Field id="MarkOut" value="1500"/>','<Field field_name="MarkIn" value="0"/>',
                '<ns:MarkOut xmlns:ns="urn:legacy">100</ns:MarkOut>',
                '<InPoint>0</InPoint>','<StartFrame>0</StartFrame>')
        for field in fields:
            settings=self.template_settings('<Import cm="{{CM}}" id="{{IDK7}}" file="{{FILENAME}}">'
                                            +field+'</Import>')
            with self.subTest(field=field),self.assertRaises(ValueError):
                metadata.validate_xml_settings(settings)

    def test_uncut_template_requires_both_mark_directions(self):
        for extra in ('','<Start>{{MARK_IN_FRAMES}}</Start>','<End>{{MARK_OUT_SECONDS}}</End>'):
            settings=self.template_settings('<Import cm="{{CM}}" id="{{IDK7}}" file="{{FILENAME}}">'
                                            +extra+'</Import>',cut=False)
            with self.subTest(extra=extra),self.assertRaises(ValueError):
                metadata.validate_xml_settings(settings)

    def test_invalid_profile_mode_template_and_xml_are_rejected(self):
        cases=({'xml_profile':'invented'},{'cut_video':'false'},
               {'xml_profile':'smartware_template'},self.template_settings('<invalid>'))
        for settings in cases:
            with self.subTest(settings=settings),self.assertRaises(ValueError):
                metadata.validate_xml_settings(settings)

    def test_template_dtd_and_entities_are_rejected(self):
        settings=self.template_settings('<!DOCTYPE Import [<!ENTITY a "value">]>'
                                        '<Import cm="{{CM}}" id="{{IDK7}}" file="{{FILENAME}}"/>')
        with self.assertRaises(ValueError):
            metadata.validate_xml_settings(settings)

    def test_uncut_marks_must_fit_full_video(self):
        for values in (dict(mark_in=-1),dict(mark_out=1000),dict(mark_out=math.nan),dict(mark_in=10,mark_out=5)):
            with self.subTest(values=values),self.assertRaises(ValueError):
                self.render({'cut_video':False},job=dict(self.job,**values))

    def test_nonfinite_duration_and_invalid_fps_are_rejected(self):
        for duration in (0,-1,math.nan,math.inf):
            with self.subTest(duration=duration),self.assertRaises(ValueError):
                self.render(duration=duration)
        for rate in ('0/0','0','bad'):
            with self.subTest(fps=rate),self.assertRaises(ValueError):
                self.render(job=dict(self.job,fps=rate))


if __name__=='__main__':
    unittest.main()
