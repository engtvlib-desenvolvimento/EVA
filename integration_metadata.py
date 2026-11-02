"""Identificadores e XML de cadastro, conforme o modelo Floripa fornecido.

``smartware`` gera o layout video em ISO-8859-1; ``smartware_template`` permite
adaptar o mapeamento da instalação. ``eva`` mantém o rascunho de conferência.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from xml.etree import ElementTree as ET


CM_RE = re.compile(r'^([A-Za-z]+)([0-9]+)(?=$|[\s_.-])')
TOKEN_RE = re.compile(r'\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}')
BASE_TOKENS = {'CM', 'IDK7', 'FILENAME', 'TITLE', 'DURATION_SECONDS', 'SHA256', 'FPS'}
IN_TOKENS = {'MARK_IN_FRAMES', 'MARK_IN_SECONDS'}
OUT_TOKENS = {'MARK_OUT_FRAMES', 'MARK_OUT_LAST_FRAME', 'MARK_OUT_SECONDS'}
MARK_TOKENS = IN_TOKENS | OUT_TOKENS
MAX_TEMPLATE_BYTES = 2 * 1024 * 1024


def material_identifiers(path, required=False):
    """Lê o primeiro token: P0320866 → ('P0320866', '320866').

    Não procura códigos no meio do título. Nomes sem identificador válido retornam
    (None, None), exceto quando ``required=True``.
    """
    name = Path(path).stem
    match = CM_RE.match(name)
    if match:
        letters, digits = match.groups()
        return letters.upper() + digits, digits.lstrip('0') or '0'
    if required:
        raise ValueError(f'CM/IDK7 não identificado no início do nome do arquivo: {Path(path).name}')
    return None, None


def _cut_enabled(settings):
    value = settings.get('cut_video', True)
    if not isinstance(value, bool):
        raise ValueError('cut_video deve ser true ou false.')
    return value


def _profile(settings):
    value = settings.get('xml_profile', 'eva')
    if value not in ('eva', 'smartware', 'smartware_template'):
        raise ValueError('xml_profile deve ser eva, smartware ou smartware_template.')
    return value


def _smartware_options(settings):
    group = settings.get('smartware_group', 'Opec')
    if not isinstance(group, str) or not group.strip():
        raise ValueError('smartware_group deve conter o grupo de cadastro, por exemplo Opec.')
    if any(not (ord(c) in (9, 10, 13) or 0x20 <= ord(c) <= 0xD7FF
                or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF)
           for c in group):
        raise ValueError('smartware_group contém caracteres inválidos para XML.')
    mark_out = settings.get('smartware_mark_out', 'exclusive')
    if mark_out not in ('inclusive', 'exclusive'):
        raise ValueError('smartware_mark_out deve ser inclusive ou exclusive.')
    return group.strip(), mark_out


def _local_name(name):
    return str(name).rsplit('}', 1)[-1].rsplit(':', 1)[-1]


def _is_mark_name(name):
    normalized = re.sub(r'[^a-z0-9]', '', _local_name(name).casefold())
    return (normalized.startswith(('markin', 'markout')) or normalized in {
        'inpoint', 'outpoint', 'inframe', 'outframe', 'inframes', 'outframes',
        'tcin', 'tcout', 'intc', 'outtc', 'timecodein', 'timecodeout',
        'startframe', 'endframe',
        'pontoentrada', 'pontosaida', 'pontodeentrada', 'pontodesaida'
    })


def _template_fields(root):
    fields = []
    for element in root.iter():
        if element.text is not None:
            fields.append(element.text)
        if element.tail is not None:
            fields.append(element.tail)
        fields.extend(element.attrib.values())
    return fields


def _load_template(settings):
    cut = _cut_enabled(settings)
    key = 'smartware_xml_template_cut' if cut else 'smartware_xml_template_uncut'
    configured = settings.get(key)
    if not isinstance(configured, str) or not configured.strip():
        raise ValueError(f'Configure {key} com um XML aceito pelo importador SmartWare.')
    path = Path(configured).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f'Template XML não encontrado: {path}')
    if path.stat().st_size > MAX_TEMPLATE_BYTES:
        raise ValueError('Template XML excede o limite de 2 MiB.')
    content = path.read_bytes()
    if b'<!DOCTYPE' in content.upper() or b'<!ENTITY' in content.upper():
        raise ValueError('Template XML não deve declarar DTD ou entidades.')
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError(f'Template XML inválido: {path}: {exc}') from exc
    fields = _template_fields(root)
    allowed = BASE_TOKENS | (set() if cut else MARK_TOKENS)
    tokens = set()
    for value in fields:
        for match in TOKEN_RE.finditer(value):
            token = match.group(1)
            if token not in allowed:
                raise ValueError(f'Placeholder XML desconhecido ou incompatível com o modo: {{{{{token}}}}}')
            tokens.add(token)
        remainder = TOKEN_RE.sub('', value)
        if '{{' in remainder or '}}' in remainder:
            raise ValueError('Placeholder XML incompleto ou inválido.')
    missing = {'CM', 'IDK7', 'FILENAME'} - tokens
    if missing:
        raise ValueError('Template XML precisa dos placeholders: ' + ', '.join(sorted(missing)))
    if cut:
        for element in root.iter():
            if _is_mark_name(element.tag) or any(_is_mark_name(name) for name in element.attrib):
                raise ValueError('Template de vídeo cortado não pode conter campos Mark In/Mark Out.')
            for name, value in element.attrib.items():
                attribute = re.sub(r'[^a-z0-9]', '', _local_name(name).casefold())
                if attribute in ('name', 'field', 'fieldname', 'key', 'column', 'columnname', 'property', 'propertyname', 'id') and _is_mark_name(value):
                    raise ValueError('Template de vídeo cortado não pode conter campos Mark In/Mark Out.')
    elif not (tokens & IN_TOKENS and tokens & OUT_TOKENS):
        raise ValueError('Template sem corte precisa de placeholders de Mark In e Mark Out.')
    return root


def validate_xml_settings(settings):
    """Valida o perfil e o template selecionado antes de iniciar a codificação."""
    _cut_enabled(settings)
    profile = _profile(settings)
    if profile == 'smartware':
        _smartware_options(settings)
    elif profile == 'smartware_template':
        _load_template(settings)


def metadata_context(job, output_duration, digest, settings):
    """Valores de cadastro; marcas usam frames base zero e fim exclusivo."""
    cut = _cut_enabled(settings)
    required = _profile(settings) != 'eva'
    source = Path(job['source_path'])
    cm, idk7 = material_identifiers(source, required=required)
    identifier = CM_RE.match(source.stem)
    title = re.sub(r'^[\s_.-]+', '', source.stem[identifier.end():]) if identifier else source.stem
    title = title or cm or source.stem
    duration = float(output_duration)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Duração de saída inválida para o XML.')
    try:
        rate = Fraction(str(job['fps']))
    except (KeyError, ValueError, TypeError, ZeroDivisionError) as exc:
        raise ValueError('FPS inválido para o XML.') from exc
    if rate <= 0:
        raise ValueError('FPS inválido para o XML.')
    values = {
        'CM': cm, 'IDK7': idk7, 'FILENAME': source.stem + '.mxf',
        'TITLE': title,
        'DURATION_SECONDS': f'{duration:.9f}', 'SHA256': str(digest),
        'FPS': f'{rate.numerator}/{rate.denominator}'
    }
    if not cut:
        try:
            mark_in = float(job['mark_in'])
            mark_out = float(job['mark_out'])
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError('Mark In/Mark Out ausentes ou inválidos para o XML sem corte.') from exc
        if not math.isfinite(mark_in) or not math.isfinite(mark_out) or not (0 <= mark_in < mark_out <= duration + .5/float(rate)):
            raise ValueError('Mark In/Mark Out fora da duração do vídeo integral.')
        first = round(mark_in * rate)
        end = round(mark_out * rate)
        if first < 0 or end <= first:
            raise ValueError('Mark In/Mark Out não delimitam frames válidos.')
        if end > round(duration * rate):
            raise ValueError('Mark Out ultrapassa o fim do vídeo integral em frames.')
        values.update(MARK_IN_FRAMES=str(first), MARK_OUT_FRAMES=str(end),
                      MARK_OUT_LAST_FRAME=str(end-1),
                      MARK_IN_SECONDS=f'{float(Fraction(first, 1)/rate):.9f}',
                      MARK_OUT_SECONDS=f'{float(Fraction(end, 1)/rate):.9f}')
    return values


def _draft_xml(context, cut):
    root = ET.Element('Material', {'schema': 'EVA-INGEST-DRAFT-1'})
    values = {
        'CM': context['CM'], 'IDK7': context['IDK7'], 'Arquivo': context['FILENAME'],
        'DuracaoSegundos': context['DURATION_SECONDS'], 'FPS': context['FPS'],
        'SHA256': context['SHA256'], 'CorteAplicado': 'true' if cut else 'false',
        'CriadoUTC': datetime.now(timezone.utc).isoformat(timespec='seconds')
    }
    for name, value in values.items():
        if value is not None:
            ET.SubElement(root, name).text = value
    if not cut:
        ET.SubElement(root, 'MarkIn', {'unit': 'frames', 'base': '0'}).text = context['MARK_IN_FRAMES']
        ET.SubElement(root, 'MarkOut', {'unit': 'frames', 'base': '0', 'end': 'exclusive'}).text = context['MARK_OUT_FRAMES']
        ET.SubElement(root, 'MarkInSegundos').text = context['MARK_IN_SECONDS']
        ET.SubElement(root, 'MarkOutSegundos', {'end': 'exclusive'}).text = context['MARK_OUT_SECONDS']
    return root


def _smartware_xml(context, cut, settings):
    """O campo codigo usa o IDK7; cm/idk7 explicitam o cadastro solicitado.

    O markOut é o primeiro frame após o conteúdo, conforme confirmado para esta
    instalação. A opção inclusive atende importadores que usam o último frame.
    """
    group, out_convention = _smartware_options(settings)
    root = ET.Element('video')
    fields = {
        'codigo': context['IDK7'], 'arquivo': context['FILENAME'],
        'titulo': context['TITLE'], 'grupo': group, 'operador': 'EVA',
        'cm': context['CM'], 'idk7': context['IDK7']
    }
    if not cut:
        fields['markIn'] = context['MARK_IN_FRAMES']
        fields['markOut'] = context['MARK_OUT_LAST_FRAME' if out_convention == 'inclusive'
                                    else 'MARK_OUT_FRAMES']
    fields['remover'] = '0'
    for name, value in fields.items():
        ET.SubElement(root, name).text = value
    return root


def write_xml(job, output_duration, digest, xml_path, settings):
    """Gera o XML Floripa, um rascunho EVA ou preenche o template configurado."""
    validate_xml_settings(settings)
    cut = _cut_enabled(settings)
    context = metadata_context(job, output_duration, digest, settings)
    profile = _profile(settings)
    if profile == 'eva':
        root = _draft_xml(context, cut)
    elif profile == 'smartware':
        root = _smartware_xml(context, cut, settings)
    else:
        root = _load_template(settings)
        def replace(value):
            return TOKEN_RE.sub(lambda match: context[match.group(1)], value)
        for element in root.iter():
            if element.text is not None:
                element.text = replace(element.text)
            if element.tail is not None:
                element.tail = replace(element.tail)
            for name, value in list(element.attrib.items()):
                element.set(name, replace(value))
    tree = ET.ElementTree(root)
    ET.indent(tree, space='  ')
    target = Path(xml_path)
    tree.write(target, encoding='ISO-8859-1' if profile == 'smartware' else 'utf-8',
               xml_declaration=True)
    return target
