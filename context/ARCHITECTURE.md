# ARCHITECTURE.md — TikTok × Roblox Live Engine

## estado atual

O repositório ainda não possui código. A arquitetura abaixo é uma proposta de fundação, não uma descrição de componentes existentes.

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
AGGREGATOR (agrupa quando reduz custo sem destruir a experiência)
   ↓
LOCAL_API (expõe fila processada pra quem consome)
   ↓
ROBLOX_BRIDGE (Luau consumindo a API local via HttpService)
   ↓
GAME_ENGINE (spawn, física, efeito, duração, cleanup)
   ↓
OBS_INTEGRATION (reage ao estado da live)

OBSERVABILITY atravessa todas as camadas (métricas + logs + watchdog)
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
Payload bruto da lib de TikTok Live, sem alteração. O normalizer é o único lugar que conhece o formato específico da lib escolhida.

### NORMALIZER → EVENT_ENGINE
Evento no schema interno padronizado (ver `EVENT_SCHEMA.md`). A partir daqui, nada mais no pipeline sabe que a origem foi TikTok.

### EVENT_ENGINE → QUEUE
Mesmo schema interno, já com `priority` resolvida e `status` inicial definido. Eventos descartados aqui (dedupe, cooldown) não entram na fila — mas ficam registrados no log JSONL pra debug.

### QUEUE → LOCAL_API
A API local expõe um endpoint de leitura (pull, não push) pro Roblox. Contrato mínimo: `GET /events?since=<cursor>&limit=<n>` retornando lista de eventos prontos pra execução, mais um `GET /health` separado (health check não expõe nem aceita payload de evento).

### LOCAL_API → ROBLOX_BRIDGE
JSON puro via HTTP. O bridge em Luau faz polling nesse endpoint respeitando o limite de requests do HttpService (ver risco #2 em `PROJECT_SPEC.md`).

### ROBLOX_BRIDGE → GAME_ENGINE
Tradução de `event` (string) pra função Luau correspondente — nunca `if event == "gift_rose" then`. O mapping de gift pra efeito de jogo mora fora do código de execução (Gift Mapping Engine configurável).

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

- **Integração:** biblioteca TikTok, limites do Roblox HttpService e alcance de localhost podem divergir entre Studio e produção.
- **Performance:** flood de comentários pode superar o consumidor; fila infinita apenas esconde o problema.
- **Confiabilidade:** reconexão agressiva pode causar loops, duplicatas e carga desnecessária.
- **Memória:** entidades e cache de avatar podem se acumular em lives longas.
- **Segurança:** localhost não é automaticamente seguro; endpoints sensíveis precisam de validação, limite e eventualmente autenticação.
- **Privacidade:** logs podem capturar usernames e identificadores além do necessário.
- **Produto:** batching pode reduzir requests, mas introduz latência; não deve ser tratado como latência zero.

---

## estrutura de diretórios proposta

```
src/
  app.py                 # composição da aplicação e ciclo de vida
  config.py              # configuração validada, sem segredos hardcoded
  domain/
    events.py            # tipos e invariantes do evento interno
    commands.py          # comandos abstratos para consumidores
    policies.py          # prioridade, dedupe, cooldown, overflow
  ingestion/
    base.py              # protocolo de source adapter
    tiktok.py            # adaptador TikTok, após validação da biblioteca
  engine/
    normalizer.py
    processor.py
    queue.py
    aggregator.py
  adapters/
    local_api.py
    roblox.py
    obs.py
  observability/
    metrics.py
    recording.py
    health.py
  security/
    validation.py
  tests/
    unit/
    integration/
    failure/
    load/
roblox/
  src/                   # scripts Luau e módulos do jogo
configs/
  gift-mappings.example.yaml
events/                  # logs JSONL por dia
context/                 # documentação de contexto do projeto
```

A estrutura pode ser ajustada ao stack escolhido. Ela não justifica adicionar frameworks antes de um protótipo mínimo.
