# ARCHITECTURE.md — TikTok × Roblox Live Engine

## estado atual

Domínio, connector de TikTok (`TikTokLive 7.0.1`), Event Engine (fila com prioridade, dedupe, agregação, dispatcher), Roblox Bridge (Local API + buffer + consumidor Luau), Rule Engine (Fase 6), Observabilidade (Fase 7), OBS Integration (Fase 8) e MQTT / ESP32 IoT Adapter (Fase 9) existem e têm testes automatizados passando. Ver `context/MQTT_INTEGRATION.md` para a especificação completa do MQTT Adapter. **Ainda não existe** composição end-to-end (`app.py` ligando TikTok + engine + bridge num processo só).

---

## pipeline conceitual

```
SOURCE (TikTok Live)
   ↓
INGESTION (conexão, reconexão, backoff)
   ↓
NORMALIZER (evento externo → schema interno)
   ↓
EVENT_ENGINE (classifica, prioriza, dedupe, agrega, decide o que segue)
   ↓
QUEUE (bounded, priority-aware, com política de overflow)
   ↓
DISPATCHER.dispatch(event)
   ├── InteractionConsumer → InteractionRuleEngine
   │     ├── RobloxBridge → Local API → Luau Client
   │     └── MQTTAdapter.publish_game_event(game_event)              [Phase 9]
   │             ↓ (non-blocking: queue, rate limit, TTL, dedup)
   │         MQTT Broker (Mosquitto/EMQX)
   │             ↓ Topics: liveengine/v1/device/{device_id}/command
   │         ESP32 Microcontrollers / Safe Physical Actuators
   │             ↑ Heartbeat & Telemetry (inbound)
   └── OBSAdapter.handle(event)                                      [Phase 8]
           ↓ (non-blocking: enqueue only)
       OBSPriorityQueue (bounded, priority, dedupe, expiration)
           ↓ (background worker task)
       OBS Studio WebSocket 5.x (port 4455)
```

Cada seta acima é um contrato. O objetivo é que cada camada só precise conhecer o contrato da vizinha, nunca a implementação interna dela.

---

## responsabilidades por camada

| Camada | Responsabilidade | Não deve fazer |
|---|---|---|
| Source adapter | falar com uma origem externa e produzir dados brutos | decidir mecânicas do Roblox |
| Ingestion | conexão, reconexão limitada, encerramento e captura | espalhar regras de negócio |
| Normalizer | converter formatos externos para o contrato interno | chamar APIs do Roblox |
| Event engine | validar, classificar, deduplicar, aplicar cooldown e decidir tratamento | depender de detalhes da UI |
| Queue | impor limite, prioridade, backpressure e overflow | crescer indefinidamente |
| Aggregator | agrupar eventos de baixo valor quando o trade-off for aceitável | agrupar controle crítico ou presentes sem regra |
| Local API | expor comandos/eventos com contrato e autenticação apropriados | virar o lugar de toda a lógica do produto |
| Roblox bridge | traduzir comandos internos para transporte consumível pelo Roblox | interpretar TikTok diretamente |
| Game runtime | executar spawn, efeitos, tempo de vida e cleanup | decidir prioridade da origem |
| Observability | métricas, logs, estado de saúde e diagnósticos | registrar segredos ou dados pessoais desnecessários |

---

## por que essa separação

- **NORMALIZER isola a origem**: se amanhã entrar Twitch, YouTube Live, comando manual via webhook — tudo vira o mesmo formato interno antes de chegar no EVENT_ENGINE. O resto do pipeline nem sabe que mudou a fonte.
- **EVENT_ENGINE isola prioridade**: a prioridade nasce aqui, não no Roblox. O Roblox só executa o que chega, na ordem/urgência que chega.
- **QUEUE isola throughput**: a fila é o ponto que absorve o descompasso entre "quantos eventos chegam" e "quantos o Roblox consegue processar". É aqui que backpressure e drop policy vivem — não espalhado pelo código.
- **ROBLOX_BRIDGE isola runtime do Roblox**: Luau não fala a língua interna do event engine. Ele só entende comandos de jogo já traduzidos.
- **OBSERVABILITY evita falhas silenciosas**: Módulos desacoplados coletam métricas e audit logs sem bloquear o Event Loop ou atrapalhar os workers.
- **`OperationalSnapshot`**: Coleta throughput, queue depth, health dos componentes via API. (Em tempo real).
- **`Watchdog`**: Monitora o stall silencioso (deadlocks) marcando componentes como DEGRADED caso fiquem "idle" além do limite de timeout.
- **`ResilienceMetrics`**: Rastrea estatísticas de recuperação, retries e drops para telemetria fina de resiliência.

---

## 4. Gerenciamento de Estado (Efêmero)
Para garantir recuperação de crash extremamente limpa e evitar bloqueios em IO complexo (Banco de Dados), **nenhum estado transacional persistente é guardado**.
1. Caches (`DeduplicationCache`, `InteractionState`): Ficam na memória; em caso de falha/reinício o estado volta zerado, com a desvantagem tolerável de repetir um re-play pontual de TikTok durante um brief window de inicialização.
2. Filas (Queues): Em memória. Perdem-se os eventos que estavam roteados para as pontes externas. Apenas o TikTok buffer absorverá os novos do chat e reacenderá as integrações.

---

## fluxos

### fluxo normal

1. O adaptador recebe um payload externo.
2. A ingestion associa metadados de conexão e timestamp.
3. O normalizer converte o payload para um evento interno ou rejeita com motivo.
4. O event engine valida, calcula prioridade e aplica deduplicação/cooldowns.
5. A fila limitada aceita, agrega ou descarta segundo a política.
6. O dispatcher entrega um comando abstrato ao bridge.
7. O Roblox executa a ação e limpa entidades expiradas.
8. O recorder e as métricas registram o resultado sem dados sensíveis desnecessários.

### falha e recuperação

Desconexões devem entrar em estados explícitos: `connected`, `disconnected`, `reconnecting`, `ended`, `failed`. O backoff inicial proposto é 2, 4, 8, 16, 30, 30, 30 segundos, com limite de tentativas e condição de encerramento quando a LIVE acabar. A implementação precisa distinguir erro transitório de encerramento normal.

---

## contratos entre camadas

### INGESTION → NORMALIZER
O `TikTokLiveConnector` registra listeners da biblioteca externa para `CommentEvent`, `GiftEvent`, `FollowEvent`, `ConnectEvent`, `DisconnectEvent` e `LiveEndEvent`. O normalizer é o único lugar que conhece os campos específicos da lib escolhida. Eventos entram em um buffer bounded e não são guardados em uma lista infinita.

### NORMALIZER → EVENT_ENGINE
Evento no schema interno padronizado (ver `EVENT_SCHEMA.md`). A partir daqui, nada mais no pipeline sabe que a origem foi TikTok.

### EVENT_ENGINE → QUEUE
Mesmo schema interno, já com `priority` resolvida e `status` inicial definido. Eventos descartados aqui (dedupe, cooldown) não entram na fila — mas ficam registrados no log JSONL pra debug.

### QUEUE -> EVENT_ENGINE_DISPATCH -> ROBLOXBRIDGE
O `Dispatcher` (fase 3) despacha cada `Event`/`AggregatedEvent` processado pra todo `EventConsumer` registrado. `RobloxBridge` (`src/adapters/roblox.py`) é um desses consumers: traduz o evento num `GameEventEnvelope` (schema_version 1.0) e guarda num buffer bounded em memória (deque com eviction FIFO), atribuindo um `sequence_number` monotônico.

### ROBLOXBRIDGE -> LOCAL_API (implementado, fase 4)
A Local API (`src/adapters/local_api.py`, FastAPI) expõe o buffer do bridge: `GET /events?since=<cursor>&limit=<n>` (pull, cursor-based, nunca remove do buffer), `POST /ack` (observabilidade, não controla retenção) e `GET /health` (não expõe nem aceita payload de evento).


### LOCAL_API -> BRIDGECLIENT.LUA (implementado, fase 4)
JSON puro via HTTP. `roblox/src/BridgeClient.lua` faz polling com `HttpService:RequestAsync`, backoff exponencial em falha, dedupe por `event_id` e rejeição segura de `schema_version` desconhecida.

### BRIDGECLIENT.LUA -> GAME_ENGINE (implementado, fase 5)
`GameEventRouter` no `roblox/src` recebe o evento decodificado, valida e faz routing para os handlers específicos da fase 5 (`AvatarService`, `EffectService`, etc). O runtime lida com cache TTL/LRU, lookup concorrente, spawn e limites de instância.

---

## decisão pendente: onde mora a Gift Mapping Engine

Duas opções, a decidir e registrar em `DECISIONS.md`:

- **no lado Python** (event engine traduz `gift → SPAWN_AVATAR com effect=X`) — Roblox recebe já pronto, mapping fácil de versionar/testar fora do Studio.
- **no lado Luau** (event engine manda `GIFT` bruto normalizado, Roblox decide o efeito a partir de uma tabela de config) — evita republish de config do lado Python toda vez que muda um efeito visual.

Isso é uma decisão real de produto, não só técnica — impacta quem edita o mapping no dia a dia (o creator, não o dev).

---

## decisões de capacidade iniciais

Os valores abaixo são apenas baseline para experimento: comentários até 60 segundos, presentes até 5 minutos, no máximo 100 entidades ativas. A métrica deve permitir ajustar esses valores. Eventos P0/P1 não podem ser silenciosamente expulsos por comentários P3/P4.

---

## riscos principais

- **Integração:** confirmado (fase 4, via documentação oficial) que `localhost` pode funcionar em Studio (servidor roda na máquina do creator) mas nunca alcança o creator numa experiência publicada (servidor roda na nuvem da Roblox) — ver `ROBLOX_BRIDGE.md`. Falta validação manual real em Studio.
- **Performance:** flood de comentários pode superar o consumidor; fila infinita apenas esconde o problema.
- **Confiabilidade:** reconexão agressiva pode causar loops, duplicatas e carga desnecessária.
- **Memória:** entidades e cache de avatar podem se acumular em lives longas.
- **Segurança:** localhost não é automaticamente seguro; endpoints sensíveis precisam de validação, limite e eventualmente autenticação.
- **Privacidade:** logs podem capturar usernames e identificadores além do necessário.
- **Produto:** batching pode reduzir requests, mas introduz latência; não deve ser tratado como latência zero.

---

## estrutura de diretórios (atualizada — o que existe de verdade após a fase 4)

```
src/
  app.py                 # AINDA NÃO EXISTE — composição/ciclo de vida (ver DECISIONS.md)
  config.py              # implementado (fase 1) — Settings via variável de ambiente
  errors.py              # implementado (fase 1)
  logging.py             # implementado (fase 1)
  domain/
    events.py            # implementado — Event, AggregatedEvent, EventUser, EventType/Status
    commands.py          # implementado — Command, CommandType (gameplay real é fase 5)
    priorities.py        # implementado — Priority, DEFAULT_PRIORITY_BY_EVENT_TYPE
    errors.py             # implementado
  ingestion/
    base.py               # implementado (fase 2)
    buffer.py              # implementado (fase 2)
    normalizer.py           # implementado (fase 2) — TikTokLive → Event
    tiktok.py                # implementado (fase 2) — lifecycle, reconnect, shutdown
  engine/
    queue.py                  # implementado (fase 3) — PriorityQueueSet, WRR
    dedup.py                   # implementado (fase 3)
    aggregator.py                # implementado (fase 3)
    dispatcher.py                 # implementado (fase 3) — EventConsumer Protocol
    processor.py                   # implementado (fase 3) — orquestrador do pipeline
    config.py                       # implementado (fase 3) — EngineConfig
    metrics.py                       # implementado (fase 3)
	  adapters/
	    roblox.py                        # implementado (fase 4) - RobloxBridge, GameEventEnvelope
	    local_api.py                      # implementado (fase 4) - FastAPI: /health /events /ack
	    obs.py                             # AINDA NÃO EXISTE
	    obs.py                             # implementado (fase 8) - OBSAdapter, OBSPriorityQueue, OBSActionValidator
	  interaction/
	    models.py                          # implementado (fase 6) - regras, actions e GameEvent
	    state.py                           # implementado (fase 6) - cooldown, dedupe, rate limit, agregação
	    engine.py                          # implementado (fase 6) - matching e GameEventFactory
	    consumer.py                        # implementado (fase 6) - publicação no RobloxBridge
	  observability/
	    health.py                          # implementado (fase 7) - Watchdog, ComponentHealth
	    metrics.py                         # implementado (fase 7) - OperationalSnapshot
	    audit.py                           # implementado (fase 7) - EventAuditLogger
roblox/
  ROBLOX_RUNTIME.md       # APIs verificadas, limites e lifecycle
  src/
    LiveRuntime.lua        # composição, idempotência e shutdown
    GameEventRouter.lua    # allowlist e roteamento
    AvatarService.lua      # lookup, cache, fallback e spawn
    AvatarCache.lua        # TTL + LRU bounded
    CleanupManager.lua     # active instances e expiração
    EffectService.lua      # efeitos allowlisted e cleanup
    BridgeClient.lua       # consumidor Luau: polling, backoff, dedupe, router-esqueleto
  tests/
    unit/                              # implementado — domínio, engine, adapters
    load/                               # implementado (fase 3)
    integration/                         # AINDA NÃO EXISTE
    failure/                              # AINDA NÃO EXISTE como pasta própria (casos cobertos em unit/)
roblox/
  src/
    BridgeClient.lua                       # implementado (fase 4) — polling, backoff, dedupe, router-esqueleto
	configs/
	  interaction_rules.json                     # implementado (fase 6) — regras declarativas validadas
events/                                       # AINDA NÃO EXISTE — log JSONL por dia
context/                                       # PROJECT_SPEC, ARCHITECTURE, EVENT_SCHEMA, DECISIONS, TEST_PLAN, ROBLOX_BRIDGE
```

A estrutura foi ajustada conforme cada fase precisou de verdade — nenhuma pasta acima foi criada só pra parecer completa antes de ter conteúdo real.
