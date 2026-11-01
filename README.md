# EVA — Estação de Vídeo Automatizada

**v0.4 · Preparação de vídeos, metadados e fila automática para TV**

A EVA acompanha uma pasta de origem, identifica os materiais novos, analisa claquete/blacks, remapeia o áudio e entrega MXF + XML na pasta de destino. A interface mostra o andamento e permite exportar os logs. Não é necessário selecionar, aprovar ou gerar cada vídeo manualmente.

## Operação

1. Instale Python 3.10+ com Tkinter e coloque `ffmpeg` e `ffprobe` no `PATH`.
2. Execute `iniciar.bat` ou `python app.py`.
3. Escolha **Origem** e **Destino** na interface. As duas pastas devem existir e ficar separadas; o destino pode ser um compartilhamento de rede UNC.
4. Escolha **corte de vídeo** e clique em **Iniciar monitoramento**. Os vídeos que já estão na origem também entram na verificação.
5. Acompanhe os estados, o progresso e as mensagens por material. **Exportar logs** gera um ZIP com os relatórios CSV, inclusive tentativas que falharam.

**Parar após atual** encerra o monitoramento depois do material em processamento. Fechar a janela também aguarda essa conclusão. A interface precisa permanecer aberta para monitorar: esta versão é um aplicativo desktop, não um serviço Windows.

As pastas escolhidas ficam salvas no `config.json`. `watch_on_start: true` permite iniciar o monitoramento ao abrir o aplicativo.

Com **corte de vídeo marcado**, a EVA remove fisicamente a claquete/blacks e omite todas as marcas no XML. Com a opção **desmarcada**, preserva o vídeo integral e informa Mark In/Out no XML em coordenadas do arquivo original. Os dois modos produzem 16 pistas mono PCM 24-bit/48 kHz. Vídeo integral já compatível com o perfil MPEG-2 é copiado sem recodificar a imagem. O modo escolhido fica registrado no histórico; mudar a caixa não substitui entregas já concluídas.

## Fluxo automático e histórico

O monitoramento procura `.mxf`, `.mov`, `.mp4`, `.avi`, `.mkv`, `.mpg`, `.mpeg`, `.mts` e `.m2ts`, incluindo subpastas. Um arquivo só fica elegível depois de manter tamanho e data de alteração estáveis entre varreduras. Isso também cobre cópias que preservam a data antiga do arquivo; arquivos vazios e extensões temporárias são ignorados. Para entregas por rede, prefira copiar com extensão temporária e renomear para a extensão final quando a cópia terminar.

O SQLite registra a origem, as etapas, os cortes, as tentativas, o destino e a integridade da entrega. O nome sem extensão, ignorando maiúsculas/minúsculas, identifica o material. Nomes iguais representam o mesmo código de material; versões diferentes devem ter códigos diferentes.

- **Processado e presente no destino:** a EVA confere a entrega registrada e não gera outro arquivo. A primeira conferência usa SHA-256; conferências seguintes aproveitam o resultado enquanto a assinatura dos arquivos permanecer igual.
- **Entrega registrada, mas ausente:** a EVA retoma o processamento. Se restou apenas um dos arquivos, só remove esse remanescente para recuperação quando seu checksum corresponde ao que a própria EVA registrou.
- **Outro destino selecionado:** a entrega tem controle independente por pasta. Um registro antigo de outra pasta não bloqueia a nova entrega.
- **Nome já existente sem uma entrega comprovada:** registra conflito e preserva o arquivo existente. O programa não sobrescreve materiais de terceiros.
- **Falha de leitura, codificação ou rede:** registra o erro e segue com os outros materiais. Novas tentativas têm intervalo e limite persistidos no banco; reabrir o aplicativo não zera esse limite.
- **Cortes ambíguos:** registra falha de análise e segue a fila, sem publicar um corte estimado de baixa confiança. Uma mudança nos parâmetros ou na revisão do detector permite uma nova análise automática; reiniciar com a mesma configuração não repete indefinidamente a falha.
- **Pasta consumida pelo importador:** com `delivery_retention_policy: "handoff"`, arquivos removidos após uma entrega concluída não provocam reenvio. Se a publicação foi interrompida e existe dúvida sobre consumo pelo Smartware, a EVA preserva a reserva e registra a necessidade de confirmação externa.

Os originais são preservados. O processamento usa uma pasta de trabalho; a transferência escreve arquivos `.part`, verifica SHA-256 e publica os nomes finais sem substituição. Em `handoff`, a releitura de integridade ocorre antes de publicar, pois o importador pode consumir os nomes finais imediatamente. O histórico e os hashes permitem reconciliar uma entrega interrompida. Um lock do monitor impede duas instâncias de processarem o mesmo banco simultaneamente; locks remotos ou sem identificação suficiente exigem conferência.

O banco existente é atualizado sem apagar seus dados. Preserve `historico.sqlite3` ao atualizar a aplicação. Faça backup com a EVA fechada.

## Claquete e black

O padrão de referência é:

```text
CLAQUETE → BLACK separador → CONTEÚDO → BLACK final, se houver
```

Uma única passada de FFmpeg combina [`freezedetect`](https://ffmpeg.org/ffmpeg-filters.html#freezedetect) e [`blackdetect`](https://ffmpeg.org/ffmpeg-filters.html#blackdetect). A imagem estática desde o começo do arquivo até o início do black confirma o trecho de claquete. O **Mark In** é o primeiro frame após esse black. A duração do separador é medida no arquivo, sem exigir cinco segundos.

O **Mark Out** é analisado separadamente: quando o último segmento é black, fica no primeiro frame desse segmento; quando o arquivo termina com imagem, fica no fim exclusivo do último frame de vídeo. Metadados por frame permitem remover até um único frame de black final e preservar uma última imagem após um black interno. O fim é obtido pelo stream de vídeo, mesmo se o áudio ou o container tiver duração maior.

O conteúdo pode ter duração diferente de 15, 30, 45 ou 60 segundos. Não há aprovação baseada nessa lista e não é necessário black final para confirmar a entrada. Blacks internos são preservados quando a claquete de entrada está confirmada.

Os limiares padrão são `black_pixel_threshold: 0.0` e `black_picture_ratio: 1.0`, para preservar imagens escuras e o início de fades. Materiais com preto ruidoso podem exigir ajuste. Os logs registram início, fim e duração de cada intervalo detectado, além do motivo de um bloqueio.

Essa análise confirma a estrutura de uma claquete estática seguida por black; não faz OCR nem interpreta o texto do cartão. Uma claquete animada ou uma entrada sem essa estrutura pode exigir ajuste do detector. Em caso de ambiguidade, o motivo fica registrado e o programa continua com o próximo material.

Os parâmetros usados na análise são `black_min_sec`, `black_pixel_threshold`, `black_picture_ratio`, `opening_scan_sec`, `commercial_min_sec`, `slate_freeze_noise_db`, `slate_min_sec`, `slate_start_tolerance_frames` e `slate_boundary_tolerance_frames`. Os campos antigos de duração esperada do separador permanecem para compatibilidade com a API que recebe somente intervalos de black.

## Áudio e perfil de vídeo

A saída contém **16 streams mono PCM de 24 bits a 48 kHz**. O mapa `audio_channel_map` tem exatamente 16 posições:

| Valor | Origem do canal de saída |
|---|---|
| `"auto"` | Canal de entrada correspondente, em sequência; silêncio se estiver ausente |
| `"0:0"` | Canal 1 da primeira stream de áudio |
| `"0:1"` | Canal 2 da primeira stream de áudio |
| `"1:0"` | Canal 1 da segunda stream de áudio |
| `"silence"` | Silêncio |

Exemplo: `"0:0"` e `"0:1"` nas duas primeiras posições enviam o estéreo da primeira stream para as duas primeiras pistas mono de saída. Os canais SAP, audiodescrição, PGM e reservas dependem do mapa da emissora: o preset `auto` preserva a ordem e não cria conteúdos ausentes.

`normalize_audio` habilita `loudnorm` em uma passada, com metas configuráveis em `lufs_target`, `lra_target` e `true_peak_db`. Os pares de saída 1/2, 3/4 e assim por diante são normalizados juntos para preservar o balanço, inclusive quando a origem contém streams mono separadas. A saída é conferida quanto à quantidade de streams, codec, amostragem, duração e quantidade exata de frames; isso não constitui certificação de loudness broadcast.

O perfil padrão `xdcam_hd422` gera MPEG-2 HD422 de 50 Mb/s em MXF e exige origem 1920×1080 a 30000/1001 fps, com ordem de campos compatível. Arquivos incompatíveis são registrados como falha; esta versão não escolhe automaticamente uma conversão de resolução ou cadência. O perfil `teste` serve para ensaios sintéticos em baixa resolução.

## Configuração de monitoramento

Além das pastas salvas pela interface, estes parâmetros podem ser ajustados no `config.json` com a EVA fechada:

| Parâmetro | Padrão | Função |
|---|---:|---|
| `watch_interval_sec` | 3 | Intervalo entre varreduras, em segundos |
| `stable_seconds` | 10 | Tempo mínimo sem mudança de tamanho/data antes de processar |
| `max_attempts` | 3 | Limite de tentativas por material no destino atual |
| `retry_delay_sec` | 30 | Intervalo entre tentativas após falha |
| `watch_on_start` | `false` | Inicia o monitor ao abrir a interface |
| `working_folder` | Pasta de trabalho configurada | Arquivos temporários de codificação |
| `cut_video` | `true` | Corte físico; `false` mantém o vídeo e envia as marcas no XML |
| `ui_history_limit` | 500 | Linhas recentes exibidas; contadores e exportação incluem todo o histórico |
| `max_jobs_per_scan` | 10 | Máximo de tentativas antes de voltar a observar arquivos novos/alterados |
| `ffmpeg_stall_timeout_sec` | 180 | Interrompe codificação sem avanço e libera a fila |
| `analysis_timeout_sec` | 300 | Tempo máximo de análise por material; ajustar para vídeos longos |
| `delivery_retention_policy` | `require_present` | Usar `handoff` quando o importador move/consome a entrega |
| `publication_order` | `xml_first` | Ou `mxf_first`, conforme o gatilho documentado do importador |

A varredura usa snapshot do banco e índice do destino, sem abrir uma conexão por vídeo conhecido. A interface agrupa atualizações e limita o histórico exibido. Logs são exportados em blocos diretamente para ZIP. A fila permanece sequencial para limitar CPU, memória e tráfego; o espaço livre é conferido antes de codificar. Essas medidas não equivalem a um ensaio de capacidade na rede de produção.

## Destino futuro: Floripa

O [Smartware documenta ingest com metadados XML](https://www.floripatec.com.br/produto/smartware/), mas o contrato de campos depende da instalação. A EVA extrai `CM=P0320866` e `IDK7=320866` do nome `P0320866_...mxf`, preservando o CM e retirando letras/zeros à esquerda do IDK7. Identidades repetidas por outro nome de arquivo são bloqueadas no destino para evitar duplicidade de cadastro.

O perfil padrão `xml_profile: "eva"` produz um rascunho explícito (`EVA-INGEST-DRAFT-1`), que não deve ser confundido com XML aceito pelo Smartware. O perfil `smartware_template` preenche templates reais separados para vídeo cortado e integral, configurados em `smartware_xml_template_cut` e `smartware_xml_template_uncut`. É necessário obter um modelo aceito, as unidades das marcas e a configuração de importação. Os exemplos e o procedimento estão em [INTEGRACAO_SMARTWARE.md](INTEGRACAO_SMARTWARE.md).

Uma pasta de destino comum pode ser usada para preparar e conferir os materiais. Quando o destino escolhido coincide com `floripa_folder`, o envio exige as três configurações existentes: `enable_publish_to_floripa`, `xml_homologated` e `audio_layout_homologated`, além do perfil com template real. Essas flags declaram que a integração foi validada. `CONCLUIDO` significa entrega verificada; cadastro efetivo no Smartware exige confirmação do importador, que não é fornecida pelo simples envio ao watch-folder.

## Logs, testes e executável

**Exportar logs** reúne `jobs.csv`, `events.csv`, `automatic_deliveries.csv`, `delivery_events.csv`, `media_transfers.csv` e `monitor_events.csv`. Os relatórios usam UTF-8 com BOM e separador `;`, para facilitar a abertura no Excel. Os eventos preservam falhas anteriores mesmo quando uma tentativa posterior termina com sucesso; o histórico de entregas mantém os resultados originais quando uma saída precisa ser recriada.

Execute os testes de regressão com:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

Os testes da fila usam arquivos e SQLite temporários. Com FFmpeg e FFprobe disponíveis, a mesma execução também testa sinais de áudio reais e o fluxo completo ao colocar vídeos sintéticos na origem: MXF, MOV sem black final e XDCAM HD422 1080i a 29,97 fps. Confere cortes, duração, canais e integridade sem conectar à Floripa. Os testes de mídia são ignorados quando as ferramentas não estão no PATH.

Para conferir um lote real inteiro sem usar o banco e o destino de operação:

```powershell
python tests/validate_real_batch.py --source "C:\pasta\origem" --report validacao_lote.json
```

Esse ensaio cria saídas temporárias, decodifica e conta os frames produzidos, confere as 16 pistas de áudio e compara os checksums dos originais antes e depois. Ao terminar, mantém somente o relatório solicitado.

Para gerar o aplicativo Windows, execute `criar_exe_windows.bat`. O resultado fica em `dist\EVA\EVA.exe`; FFmpeg e FFprobe continuam sendo dependências externas.
