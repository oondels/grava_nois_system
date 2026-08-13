# Raspberry Pi Pico: modelos V1 e V2

Este documento define os dois firmwares Pico suportados pelo `grava_nois_system`.
Eles compartilham os tokens legados, mas possuem objetivos e sinalizacao diferentes.
Nao misture a interpretacao dos LEDs ou os gestos de um modelo com o outro.

## Escolha do modelo

| Modelo | Arquivo no repositorio | Quando usar |
|---|---|---|
| V1 legado | `raspberry_pico/main.py` | Instalacoes existentes que precisam apenas de triggers, restart, pull e um LED de handshake. |
| V2 operacional | `raspberry_pico/main_operational_v2.py` | Novas instalacoes que precisam de diagnostico local, dois LEDs, manutencao, watchdog e desligamento confirmado. |

O arquivo escolhido deve ser instalado no Pico com o nome `main.py`. O V1 permanece
versionado como fallback. Atualize primeiro o edge para uma imagem que entenda o
protocolo V2 e somente depois troque o firmware do Pico.

## Modelo V1 legado

### Pinagem V1

| Funcao | GPIO | Eletrica |
|---|---:|---|
| Botao dedicado 1 | 2 | Entrada com pull-up; evento no release `0 -> 1`. |
| Botao dedicado 2 | 3 | Entrada com pull-up; evento no release `0 -> 1`. |
| Botao Docker | 15 | Entrada com pull-up; pressionado em nivel baixo. |
| LED unico | 14 | Saida ativa em nivel baixo. |

Use resistor adequado em serie com o LED. Os botoes devem respeitar a pinagem e a
logica eletrica acima; nao aplique 5 V aos GPIOs do Pico.

### Gestos V1

| Entrada | Token | Resultado no edge |
|---|---|---|
| Botao 1 | `BTN_1` | Trigger da camera associada ao token. |
| Botao 2 | `BTN_2` | Trigger da camera associada ao token. |
| 5 cliques no botao Docker | `RESTART_DOCKER` | Regenera `config.json` e recria o container sem pull. |
| Hold de pelo menos 5 segundos | `PULL_DOCKER` | Regenera `config.json`, baixa a imagem e recria o container. |

O V1 envia `ACK_GRN_STARTED` ao receber `GRN_STARTED`. Seu LED fica aceso depois
desse handshake. Ele indica apenas que o processo edge abriu a serial; nao representa
saude da camera, MQTT, upload ou locacao.

## Modelo V2 operacional

### Pinagem V2

| Funcao | GPIO | Eletrica |
|---|---:|---|
| Botao dedicado 1 | 2 | Entrada com pull-up; evento no release `0 -> 1`. |
| Botao dedicado 2 | 3 | Entrada com pull-up; evento no release `0 -> 1`. |
| Botao administrativo | 15 | Entrada com pull-up; pressionado em nivel baixo, normalmente ligado ao GND. |
| LED de atividade | 13 | Saida ativa em nivel baixo. |
| LED de sistema | 14 | Saida ativa em nivel baixo. |

Os dois LEDs precisam de resistores em serie. O firmware aplica debounce ao botao
administrativo e limita eventos repetidos dos botoes dedicados.

### Gestos V2

Os cliques sao classificados depois do release. A sequencia fecha apos 700 ms sem
novo clique.

| Gesto no botao administrativo | Token | Comportamento |
|---|---|---|
| 2 cliques | `REQUEST_DIAGNOSTIC` | Diagnostico de serial, FFmpeg e buffer. |
| 3 cliques | `TOGGLE_MAINTENANCE` | Ativa ou desativa manutencao por ate 15 minutos. |
| 5 cliques | `RESTART_DOCKER` | Regenera config e recria o container sem pull. |
| Hold de 2 a 3 segundos | `RUN_SELF_TEST` | Executa o self-test tecnico local. |
| Hold de 4 a 5 segundos | `TRIGGER_GLOBAL` | Solicita captura em fan-out global. |
| Hold de 8 a 10 segundos | `ARM_SHUTDOWN` | Abre janela de confirmacao de poweroff por 5 segundos. |
| 1 clique durante a confirmacao | `SHUTDOWN_HOST` | Desliga o host quando o recurso estiver habilitado. |
| Hold de 12 segundos ou mais | `PULL_DOCKER` | Regenera config, baixa a imagem e recria o container. |

Um clique isolado, quatro cliques e holds fora das faixas sao cancelados e recebem
feedback de erro. Atualmente `REQUEST_DIAGNOSTIC` e `RUN_SELF_TEST` validam o mesmo
conjunto tecnico: heartbeat serial, todas as cameras com FFmpeg vivo e buffers
recentes. MQTT desconectado aparece no LED, mas nao reprova esse teste local.

### Estados dos LEDs V2

| Estado | LED de sistema (GPIO 14) | LED de atividade (GPIO 13) |
|---|---|---|
| Inicializando camera | Pulso lento | Conforme MQTT/upload. |
| Todas as cameras prontas | Aceso | Conforme MQTT/upload. |
| Parte das cameras pronta | Dois pulsos | Conforme MQTT/upload. |
| Camera indisponivel | Tres pulsos rapidos | Conforme MQTT/upload. |
| MQTT conectado | Conforme camera | Aceso. |
| MQTT desconectado/desabilitado | Conforme camera | Apagado. |
| Upload ou retry pendente | Conforme camera | Pisca a cada 250 ms. |
| Manutencao | Alterna a cada 250 ms | Alterna com o LED de sistema. |
| Watchdog sem heartbeat por 10 s | Alterna rapidamente | Alterna com o LED de sistema. |
| Diagnostico em execucao | Pisca | Apagado. |
| Diagnostico/acao aceita | Aceso por 2 s | Aceso por 2 s. |
| Rejeicao ou falha | Pisca rapidamente por 5 s | Pisca junto por 5 s. |
| Restart | 5 flashes | Apagado. |
| Pull/recreate | 2 flashes | Apagado. |

A prioridade visual e: watchdog, confirmacao de acao, diagnostico/rejeicao,
manutencao e estado normal. O watchdog apenas sinaliza; ele nao reinicia o host nem
o container automaticamente.

### Manutencao V2

Enquanto a manutencao estiver ativa, triggers locais por ENTER, GPIO, botoes
dedicados e trigger global do Pico sao bloqueados. Camera, FFmpeg, buffer, workers,
retries e uploads continuam funcionando. O modo termina por novo toggle, por timeout
de 15 minutos ou por restart do edge.

### Rental no V2

O V2 ainda nao mantem um estado continuo de "locacao ativa", porque o device rental
deve continuar capturando sem internet e ainda nao possui manifesto local assinado da
agenda. Rejeicoes definitivas da API, como locacao inexistente, grace period expirado
ou janela invalida, produzem feedback visual de falha quando chegam ao worker.

## Protocolo serial

| Direcao | Mensagem | Finalidade |
|---|---|---|
| Edge -> Pico | `GRN_STARTED` | Handshake compativel com V1. |
| Pico -> Edge | `ACK_GRN_STARTED` | Confirma recebimento do handshake. |
| Pico -> Edge | `PICO_CAPS:2:DUAL_LED,HEARTBEAT,ACTIONS` | Negocia capacidades V2. |
| Edge -> Pico | `PING:<seq>` | Heartbeat a cada 2 segundos. |
| Pico -> Edge | `PONG:<seq>` | Confirma heartbeat. |
| Edge -> Pico | `STATE:CAMERA:<estado>` | `STARTING`, `READY`, `DEGRADED` ou `ERROR`. |
| Edge -> Pico | `STATE:MQTT:<estado>` | `CONNECTED`, `DISCONNECTED` ou `DISABLED`. |
| Edge -> Pico | `STATE:UPLOAD:<estado>` | `IDLE` ou `PENDING`. |
| Edge -> Pico | `STATE:MAINTENANCE:<estado>` | `ON` ou `OFF`. |
| Edge -> Pico | `FEEDBACK:<categoria>:<resultado>` | Resultado temporario de diagnostico, trigger ou acao. |

O `PicoSerialController` e o unico proprietario da porta no edge. Nenhum outro
processo deve abrir a mesma serial durante a operacao normal.

## Restart, pull e configuracao

`RESTART_DOCKER` e `PULL_DOCKER` nao executam Docker dentro do container. O edge
grava uma intent no runtime persistente, e o runner systemd do host:

1. regenera atomicamente `config.json` a partir de `/opt/.grn/config/.env`;
2. aborta a acao se a conversao falhar;
3. executa recreate, com pull previo somente para `PULL_DOCKER`;
4. registra resultado e etapa da falha nos arquivos de runtime.

Consequencia: nessas duas acoes, o `.env` e a fonte autoritativa para os campos
operacionais convertidos. Uma configuracao aplicada apenas em `config.json` por MQTT
sera substituida pelos valores equivalentes do `.env` no proximo restart/pull.
