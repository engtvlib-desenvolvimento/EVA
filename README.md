# EVA — Estação de Vídeo Automatizada

**v0.3 · Fila automática de preparação de vídeos para TV**

A EVA acompanha uma pasta de origem, identifica os materiais novos, remove claquete e blacks quando consegue determinar os cortes, remapeia o áudio e entrega MXF + XML na pasta de destino. A interface mostra o andamento e permite exportar os logs. Não é necessário selecionar, aprovar ou gerar cada vídeo manualmente.

## Operação

1. Instale Python 3.10+ com Tkinter e coloque `ffmpeg` e `ffprobe` no `PATH`.
2. Execute `iniciar.bat` ou `python app.py`.
3. Escolha **Origem** e **Destino** na interface. As duas pastas devem existir e ficar separadas; o destino pode ser um compartilhamento de rede UNC.
4. Clique em **Iniciar monitoramento**. Os vídeos que já estão na origem também entram na verificação.
5. Acompanhe os estados, o progresso e as mensagens por material. **Exportar logs** gera um ZIP com os relatórios CSV, inclusive tentativas que falharam.

**Parar após atual** encerra o monitoramento depois do material em processamento. Fechar a janela também aguarda essa conclusão. A interface precisa permanecer aberta para monitorar: esta versão é um aplicativo desktop, não um serviço Windows.

As pastas escolhidas ficam salvas no `config.json`. `watch_on_start: true` permite iniciar o monitoramento ao abrir o aplicativo.

## Fluxo automático e histórico

O monitoramento procura `.mxf`, `.mov`, `.mp4`, `.avi`, `.mkv`, `.mpg`, `.mpeg`, `.mts` e `.m2ts`, incluindo subpastas. Um arquivo só fica elegível depois de manter tamanho e data de alteração estáveis entre varreduras. Isso também cobre cópias que preservam a data antiga do arquivo; arquivos vazios e extensões temporárias são ignorados. Para entregas por rede, prefira copiar com extensão temporária e renomear para a extensão final quando a cópia terminar.

O SQLite registra a origem, as etapas, os cortes, as tentativas, o destino e a integridade da entrega. O nome sem extensão, ignorando maiúsculas/minúsculas, identifica o material. Nomes iguais representam o mesmo código de material; versões diferentes devem ter códigos diferentes.

- **Processado e presente no destino:** a EVA confere a entrega registrada e não gera outro arquivo. A primeira conferência usa SHA-256; conferências seguintes aproveitam o resultado enquanto a assinatura dos arquivos permanecer igual.
- **Entrega registrada, mas ausente:** a EVA retoma o processamento. Se restou apenas um dos arquivos, só remove esse remanescente para recuperação quando seu checksum corresponde ao que a própria EVA registrou.
- **Outro destino selecionado:** a entrega tem controle independente por pasta. Um registro antigo de outra pasta não bloqueia a nova entrega.
- **Nome já existente sem uma entrega comprovada:** registra conflito e preserva o arquivo existente. O programa não sobrescreve materiais de terceiros.
- **Falha de leitura, codificação ou rede:** registra o erro e segue com os outros materiais. Novas tentativas têm intervalo e limite persistidos no banco; reabrir o aplicativo não zera esse limite.
- **Cortes ambíguos:** registra falha de análise e segue a fila, sem publicar um corte estimado de baixa confiança. Uma mudança nos parâmetros ou na revisão do detector permite uma nova análise automática; reiniciar com a mesma configuração não repete indefinidamente a falha.

Os originais são preservados. O processamento usa uma pasta de trabalho; a transferência escreve arquivos `.part`, verifica SHA-256 e publica os nomes finais sem substituição. O histórico e os hashes permitem reconciliar uma entrega interrompida. Um lock do monitor impede duas instâncias de processarem o mesmo banco simultaneamente; locks remotos ou sem identificação suficiente exigem conferência.

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

## Destino futuro: Floripa

Hoje o XML produzido por `core.xml_for_job()` é demonstrativo (`EXEMPLO-NAO-FLORIPA`). Para integrar com a Floripa, será necessário adaptar esse XML ao schema aceito, confirmar o mapa das 16 pistas e verificar a ordem de publicação do par XML/MXF e o gatilho de ingestão.

Uma pasta de destino comum pode ser usada para preparar e conferir os materiais. Quando o destino escolhido coincide com `floripa_folder`, o envio exige as três configurações existentes: `enable_publish_to_floripa`, `xml_homologated` e `audio_layout_homologated`. Essas flags declaram que a integração foi validada; não transformam o XML demonstrativo em um XML compatível.

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
