# Integração de cadastro com SmartWare / Floripa

A EVA agora tem um perfil `smartware` baseado no XML aceito apresentado pelo usuário: declaração `ISO-8859-1`, raiz `video` e campos `codigo`, `arquivo`, `titulo`, `grupo`, `operador`, `markIn`, `markOut` e `remover`. O operador é emitido como `EVA`; `remover` permanece `0`, conforme o modelo.

Esse modelo local é a referência para a estrutura. A [documentação pública do SmartWare](https://www.floripatec.com.br/produto/smartware/) também confirma ingest automatizado com metadados e preenchimento da base por XML, mas não especifica o mapeamento de campos da instalação.

Os novos exemplos [Smartware_cortado.xml](exemplos_xml/Smartware_cortado.xml) e [Smartware_integral.xml](exemplos_xml/Smartware_integral.xml) mostram esse perfil. `codigo` recebe o IDK7 numérico. As tags adicionais `cm` e `idk7` foram autorizadas pelo usuário para carregar os dois identificadores. O preenchimento efetivo das colunas correspondentes ainda deve ser conferido no SmartWare; a geração dessas tags, sozinha, não comprova o cadastro na base.

Os arquivos [EVA_cortado.xml](exemplos_xml/EVA_cortado.xml) e [EVA_integral.xml](exemplos_xml/EVA_integral.xml) são exemplos do **rascunho EVA**, identificado por `schema="EVA-INGEST-DRAFT-1"`. Não são um layout oficial SmartWare e não comprovam cadastro na base da Floripa. O valor de SHA-256 nesses exemplos é ilustrativo.

## Identificação do material

A EVA extrai o primeiro código do nome do arquivo:

| Nome / campo | Valor no exemplo |
|---|---|
| Arquivo de origem | `P0320866_ENCONTRO.mxf` |
| CM | `P0320866` |
| IDK7 | `320866` |

O CM preserva as letras e os zeros, com letras convertidas para maiúsculas. O IDK7 é a parte numérica sem zeros à esquerda. O código deve estar no início do nome e terminar antes de um separador ou do fim do nome; a EVA não procura outro código no meio do título.

Essa conversão foi definida para este fluxo da emissora. Prefixos diferentes com a mesma parte numérica podem gerar o mesmo IDK7; a regra de unicidade e o tratamento de materiais já cadastrados precisam corresponder aos do SmartWare.

## Opção “corte de vídeo”

| Opção | Vídeo entregue | XML |
|---|---|---|
| Marcada | Contém somente o trecho entre os pontos detectados, com remapeamento de áudio para 16 canais | Contém CM/IDK7 e metadados; não contém instruções de Mark In/Mark Out |
| Desmarcada | Preserva a duração integral, com o mesmo remapeamento de áudio | Contém CM/IDK7 e as instruções de Mark In/Mark Out sobre o arquivo integral |

A detecção da claquete e dos blacks ocorre nos dois modos. O modo sem corte mantém as coordenadas do conteúdo dentro do vídeo integral; ele não converte o Mark In para zero. Internamente, a EVA usa Mark Out exclusivo. O perfil SmartWare converte esse limite para a convenção configurada no XML. Quando não existe black final, preserva-se até o último frame de vídeo.

No exemplo integral, o Mark In é o frame `150` e o Mark Out exclusivo é `1345`. O último frame de conteúdo é, portanto, `1344`. O perfil SmartWare padrão emite `markOut=1345`, conforme a confirmação do usuário de que esse campo indica o primeiro frame após o conteúdo. O FPS é `30000/1001`, equivalente a aproximadamente 29,97 frames por segundo. No modo cortado, a saída tem 1.195 frames e o XML omite `markIn` e `markOut`.

## Perfis de XML

### SmartWare baseado no modelo fornecido

```json
{
  "xml_profile": "smartware",
  "smartware_group": "Opec",
  "smartware_mark_out": "exclusive"
}
```

O perfil não exige templates externos e escreve `ISO-8859-1`. A ordem dos filhos é `codigo`, `arquivo`, `titulo`, `grupo`, `operador`, `cm`, `idk7`, marcas opcionais e `remover`. Os valores são:

| Campo | Origem |
|---|---|
| `codigo` | IDK7 numérico, como `320866` |
| `arquivo` | Nome exato do MXF entregue |
| `titulo` | Nome-base sem o CM inicial e os separadores que o seguem |
| `grupo` | `smartware_group`, com padrão `Opec` |
| `operador` | `EVA` |
| `cm` | CM com letras e zeros preservados, incluído conforme autorização do usuário |
| `idk7` | IDK7 numérico, incluído conforme autorização do usuário |
| `markIn` / `markOut` | Somente no modo sem corte, em frames conforme a convenção abaixo |
| `remover` | `0`, mantido conforme o modelo fornecido |

O usuário confirmou que `markOut` indica o primeiro frame após o conteúdo. O padrão deste fluxo é, portanto, `smartware_mark_out: "exclusive"`, com contagem em frames iniciada em zero. O exemplo apresentado com `markIn=210` e `markOut=659` representa os frames de conteúdo de `210` até `658`. A alternativa `inclusive` permanece disponível para outras instalações que exijam o último frame de conteúdo; ela emitiria `1344` no exemplo integral deste documento.

Não se deduz a política da watch-folder pelo campo `remover=0`. A movimentação e o consumo dos arquivos dependem do importador e da configuração local.

### Rascunho EVA

```json
{
  "xml_profile": "eva",
  "cut_video": true
}
```

Esse perfil serve para conferir os valores que a EVA calcula. Ele gera o formato dos exemplos deste documento. Nomes de arquivos sem CM válido podem ser usados em ensaios, mas os campos CM/IDK7 são omitidos nesses rascunhos.

### Template do importador SmartWare

O perfil `smartware_template` continua disponível quando a instalação exigir uma estrutura diferente do perfil embutido. Nesse caso, mantenha templates separados para os dois modos:

```json
{
  "xml_profile": "smartware_template",
  "smartware_xml_template_cut": "C:\\EVA\\templates\\smartware_cortado.xml",
  "smartware_xml_template_uncut": "C:\\EVA\\templates\\smartware_integral.xml",
  "publication_order": "xml_first",
  "delivery_retention_policy": "handoff"
}
```

Os caminhos acima são exemplos de configuração. Os arquivos precisam conter o XML real da instalação, com os placeholders inseridos nos campos corretos. A caixa “corte de vídeo” determina qual template será usado. A EVA valida o template selecionado antes da codificação.

A substituição ocorre em textos e atributos XML, com escape dos valores. A EVA conserva a estrutura fornecida, sem criar tags proprietárias. Placeholders desconhecidos ou incompletos são rejeitados. Os placeholders de CM, IDK7 e nome de arquivo são obrigatórios. No template integral, também são obrigatórias instruções de entrada e saída. No template cortado, placeholders de marcas e campos conhecidos de Mark In/Mark Out, mesmo com valores estáticos, são rejeitados.

## Placeholders disponíveis

| Placeholder | Conteúdo |
|---|---|
| `{{CM}}` | Código com letras e zeros preservados, como `P0320866` |
| `{{IDK7}}` | Identificador numérico sem zeros à esquerda, como `320866` |
| `{{FILENAME}}` | Nome do MXF entregue, incluindo `.mxf` |
| `{{TITLE}}` | Título derivado do nome-base sem o CM inicial e os separadores que o seguem |
| `{{DURATION_SECONDS}}` | Duração do arquivo entregue, em segundos com nove casas decimais |
| `{{SHA256}}` | SHA-256 do MXF entregue |
| `{{FPS}}` | Taxa racional, como `30000/1001` |
| `{{MARK_IN_FRAMES}}` | Primeiro frame do conteúdo, índice iniciado em zero; somente sem corte |
| `{{MARK_OUT_FRAMES}}` | Primeiro frame depois do conteúdo, fim exclusivo; somente sem corte |
| `{{MARK_OUT_LAST_FRAME}}` | Último frame do conteúdo, fim inclusivo; somente sem corte |
| `{{MARK_IN_SECONDS}}` | Posição do primeiro frame de conteúdo em segundos precisos; somente sem corte |
| `{{MARK_OUT_SECONDS}}` | Posição do fim exclusivo do conteúdo em segundos precisos; somente sem corte |

Use `{{MARK_OUT_LAST_FRAME}}` apenas se o contrato do importador exigir o último frame inclusivo. Se ele exigir fim exclusivo, use `{{MARK_OUT_FRAMES}}` ou `{{MARK_OUT_SECONDS}}`, conforme a unidade solicitada.

Este fluxo usa frames e fim exclusivo, conforme a confirmação recebida. Para templates de outras instalações que exijam timecode SMPTE, convenção drop-frame ou unidades como intervalos de 100 ns, será necessário um mapeamento específico; esses formatos não são presumidos pelos placeholders. O FPS racional sozinho não declara uma convenção de timecode.

## Ordem de publicação e pasta monitorada

A EVA prepara os arquivos temporários antes de publicar os nomes finais. A configuração `publication_order` controla qual nome final aparece primeiro:

| Valor | Ordem |
|---|---|
| `xml_first` | XML e depois MXF |
| `mxf_first` | MXF e depois XML |

Confirme o gatilho com a Floripa. Se o importador dispara ao encontrar o MXF, os metadados podem precisar estar disponíveis antes dele. Se dispara pelo XML, o vídeo pode precisar estar disponível primeiro. A existência de uma watch-folder, por si só, não determina essa ordem nem garante que a pasta esteja configurada para ler um XML acompanhante.

A forma de associar os arquivos também precisa estar definida: mesmo nome-base, campo dentro do XML, caminho relativo, código do material ou outro identificador previsto pelo importador.

## Arquivos consumidos pelo importador

`delivery_retention_policy` define o comportamento da EVA depois de uma entrega registrada:

| Valor | Uso |
|---|---|
| `require_present` | Para uma pasta em que MXF/XML devem permanecer. Uma entrega ausente pode ser recriada, conforme a conferência do histórico e dos arquivos existentes. |
| `handoff` | Para uma pasta em que o importador move ou remove os arquivos após recebê-los. Uma entrega concluída não é reenviada apenas porque os arquivos foram consumidos. |

Escolha a política conforme a operação da pasta. No modo `handoff`, a ausência posterior não distingue consumo normal de uma remoção externa inesperada; é necessário usar o histórico do importador para essa conferência.

O estado `CONCLUIDO` na EVA confirma a entrega e as verificações locais de integridade. **Não é uma confirmação de cadastro aceito pelo SmartWare.** Para atestar o preenchimento de CM, IDK7 e marcas na base, será necessário conferir o resultado no sistema ou integrar um retorno de processamento suportado pelo fornecedor.

## Informações a solicitar à Floripa

Para confirmar o cadastro automático com o modelo já fornecido, use o caso concreto `CM=P0320866`, `IDK7=320866` e confirme com o suporte:

1. Se o importador preenche as colunas corretas a partir de `codigo=320866`, `cm=P0320866` e `idk7=320866`, já definidos para este fluxo. Obtenha o XSD ou a documentação de mapeamento, se disponíveis.
2. As colunas de destino de CM e IDK7, seus tipos, regras de unicidade e comportamento quando o material já existe.
3. A reprodução entre o Mark In e o Mark Out em frames, com fim exclusivo já confirmado, conferindo o primeiro e o último frame de conteúdo no resultado importado.
4. Como associar o XML ao MXF e qual arquivo dispara a importação, incluindo a ordem de publicação exigida.
5. O comportamento da pasta após o processamento: movimentação, retenção, pasta de erro, logs e forma de consultar a confirmação de cadastro.
6. Se a mesma instalação aceita o vídeo integral com marcas lógicas e o vídeo fisicamente cortado com as marcas omitidas.

Teste o perfil embutido, ou os templates específicos quando necessários, em uma pasta de homologação com um material de cada modo. Confira as colunas CM/IDK7, o operador `EVA`, a duração exibida e a reprodução do primeiro e último frame de conteúdo.
