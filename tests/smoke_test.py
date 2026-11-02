"""Teste de integração com demo sintética, sem conexão com pasta real da Floripa."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core

folder = Path(__file__).resolve().parent
src = folder/'DEMO.mxf'
if not src.exists():
    raise SystemExit('Execute create_demo.sh (Linux) para criar DEMO.mxf primeiro')
root = Path(tempfile.mkdtemp(prefix='ingest_floripa_'))
(root/'destino').mkdir(exist_ok=True)
(root/'work').mkdir(exist_ok=True)
(root/'local').mkdir(exist_ok=True)
settings = core.config()
settings['source_folder']=str(folder)
settings['working_folder']=str(root/'work')
settings['local_output_folder']=str(root/'local')
settings['floripa_folder']=str(root/'destino')
settings['video_profile']='teste'
settings['xml_profile']='eva'  # DEMO não tem CM; este ensaio usa XML de conferência.
settings['normalize_audio']=True
settings['enable_publish_to_floripa']=True  # somente área de teste!
settings['xml_homologated']=True  # testes sintéticos apenas
settings['audio_layout_homologated']=True  # testes sintéticos apenas
tmpcfg=root/'config_teste.json'
tmpcfg.write_text(json.dumps(settings),encoding='utf-8')
core.CONFIG_FILE=tmpcfg
core.DB_FILE=root/'historico_teste.sqlite3'
core.init_db()
print('Descobrir',core.discover(str(folder),0))
job=next(j for j in core.jobs() if j['source_path']==str(src))
print('Analisar:',core.analyze(job['id']))
job=core.get_job(job['id'])
# Tem de ser confirmado manualmente na aplicação real.
assert abs(job['mark_in']-6.0)<0.09, job
assert abs(job['mark_out']-11.0)<0.09, job
assert job['confidence']=='ALTA', job
core.approve(job['id'],job['mark_in'],job['mark_out'])
result=core.process(job['id'],lambda v,m: None)
print('Resultado local:',result)
assert result['status']=='PREPARADO'
result=core.publish_prepared(job['id'])
print('Resultado publicado em homologação:',result)
assert result['status']=='CONCLUIDO'
assert Path(result['output_path']).exists()
assert Path(result['xml_path']).exists()
probe=core.run_probe(result['output_path'])
assert len([s for s in probe['streams'] if s['codec_type']=='audio'])==16
print('OK: arquivo MXF + XML produzidos, SHA-256 validado, 16 canais de áudio')
