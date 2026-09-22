# DECISIONS.md — decisões arquiteturais

## como ler este arquivo

Decisões marcadas como **EM ABERTO** ainda não foram implementadas nem validadas em execução. Decisões marcadas como **DECIDIDO** foram tomadas e justificadas. Nada aqui é "provavelmente vamos fazer assim" — se ainda não foi decidido, fica em aberto, não vira fato.

---

## RESOLVIDA — biblioteca de conexão com TikTok Live

Foi escolhida e verificada antes do código de ingestão. A biblioteca não é tratada como decisão permanente: se quebrar ou deixar de atender, a fronteira `TikTokSource` deve permitir substituição sem contaminar o domínio.

## DECIDIDO — TikTokLive 7.0.1 como connector local da v1

A fase 2 usa `TikTokLive==7.0.1`, verificado no PyPI, no repositório upstream e por inspeção dos exports instalados. A versão expõe `TikTokLiveClient`, `start()`, `connect()`, `disconnect()`, `add_listener()` e os eventos necessários para esta fase: `CommentEvent`, `GiftEvent`, `FollowEvent`, `ConnectEvent`, `DisconnectEvent` e `LiveEndEvent`.

A biblioteca é um projeto de engenharia reversa, declara licença Modified AGPL-3.0 e alerta que não é uma API de produção. Isso é aceito para o uso local e open source previsto nesta versão. A dependência está pinada para evitar que uma atualização mude silenciosamente os campos usados pelo normalizer.

O projeto usa `tiktoklive-engine` como nome de distribuição no `pyproject.toml`, porque o nome normalizado `tiktoklive` colide com a distribuição externa `TikTokLive` no resolvedor do Python. Os imports internos continuam em `src.*`.

## RESOLVIDA — FastAPI + Uvicorn pra API local (fase 4)

Confirmado sem atrito com `TikTokLive` (ambos usam `httpx`/`websockets`, sem conflito de versão observado). FastAPI roda no mesmo loop asyncio do Event Engine — uma API sync (Flask puro) exigiria thread separada ou bloquearia o loop. Validação de request via Pydantic cobre `payload_validation`/`local_api_security` sem reinventar. `fastapi>=0.115,<1.0` e `uvicorn>=0.30,<1.0` adicionados a `pyproject.toml`; versões instaladas e verificadas nesta fase: `fastapi 0.141.1`, `uvicorn 0.53.0`.

## EM ABERTO — onde mora a Gift Mapping Engine

Ver `ARCHITECTURE.md`, seção "decisão pendente". Impacta workflow do creator, não só arquitetura. Trade-off: Python-side mantém o Roblox mais burro e o mapping mais fácil de versionar/testar fora do Studio; Luau-side evita republish de config toda vez que muda um efeito visual.

## EM ABERTO — resolução de identidade TikTok → Roblox

Como (e se) o sistema associa uma conta do TikTok a um usuário/avatar do Roblox. Sem isso definido, o campo `user_id` do evento pós-engine fica sem contrato real.

## PARCIALMENTE RESOLVIDA — localhost em Roblox Studio vs jogo publicado (fase 4)

Risco #1 do `PROJECT_SPEC.md`. Verificado contra a documentação oficial atual da Roblox (não apenas suposição):

- **Studio (Play Solo/Team Test)**: o servidor da experiência roda na máquina de quem testa. A documentação oficial de `HttpService` traz um exemplo de código atual conectando a `http://localhost:11434` (um servidor Ollama local) — evidência de que `localhost` pode funcionar em Studio. Relatos mais antigos de erro (`Trust check failed`) parecem ligados à forma da URL (IP puro sem protocolo), não a um bloqueio universal.
- **Experiência publicada**: o servidor roda na infraestrutura da Roblox — uma máquina diferente da do creator. `localhost` nunca alcança a máquina do creator nesse cenário; isso não depende de configuração, é estrutural.
- Decisão de arquitetura: **v1 usa `localhost` como default de desenvolvimento em Studio**; qualquer uso além disso (compartilhar teste, publicar) exige expor a Local API via túnel HTTPS (ngrok, Cloudflare Tunnel, etc.) — documentado em `ROBLOX_BRIDGE.md`.
- Ainda **PARCIAL** porque nenhum teste manual real dentro do Roblox Studio foi executado por este agente (sem acesso a Roblox Studio) — ver `TEST_PLAN.md`, `manual_studio_test`. A parte "publicado nunca alcança localhost" é estrutural e não depende de teste; a parte "localhost funciona em Studio" depende de confirmação manual da Paola.

## EM ABERTO — implementação da priority queue

Opções a avaliar: heap com prioridade + dedupe por chave, ou múltiplas filas (uma por nível de prioridade) consumidas em ordem com fairness pra não starvar P3/P4 completamente. Decisão depende de testes de carga reais, não só teoria.

## EM ABERTO — estratégia de overflow da fila

Quando a fila bounded encher: drop do mais antigo P4? Drop de novos P4? Nunca dropar P0/P1? Precisa virar regra explícita, testada com `event flood` (ver `TEST_PLAN.md`).

## EM ABERTO — banco de dados

Ainda não há necessidade comprovada. Evidência necessária: volume, retenção e requisitos de consulta que justifiquem a dependência.

## EM ABERTO — OBS / MQTT / hardware

Integrações futuras, fora do fluxo mínimo. Evidência necessária: contratos e testes isolados.

## EM ABERTO — SaaS / multi-tenant

Complexidade prematura. Evidência necessária: validação do produto local primeiro.

---

## DECIDIDO — reconexão com backoff exponencial limitado

Sequência: 2s, 4s, 8s, 16s, 30s, 30s, 30s (patamar). Log em cada tentativa. Condição clara de parada quando a live termina de fato (sinal explícito, não timeout arbitrário). A implementação precisa distinguir erro transitório de encerramento normal.

## DECIDIDO — sem banco de dados na v1

Persistência de eventos via JSONL local (`/events/YYYY-MM-DD.jsonl`). Suficiente pra debug/replay na v1 local. Banco entra só se um caso de uso real justificar (ex: analytics multi-sessão) — não adicionar dependência sem necessidade.

## DECIDIDO — prioridade nasce no Event Engine, não no Roblox

O Roblox recebe eventos já priorizados e classificados. Nenhuma lógica de "isso é importante" vive em Luau.

## DECIDIDO — health check separado de endpoints sensíveis na API local

`/health` não expõe nem aceita payload de evento. Endpoints de evento validam payload, limitam tamanho e rejeitam tipo desconhecido quando apropriado.

## DECIDIDO — event engine independente da origem (ADR-001)

O domínio do jogo recebe eventos e comandos abstratos. Regras de TikTok ficam no adaptador e no normalizer. Isso permite futuras fontes sem acoplar o Roblox a uma plataforma específica. Consequência: exige um schema interno versionado e uma etapa explícita de normalização.

### Fase 10: Resiliência, Recuperação e Hardening
**Contexto**: O sistema precisa lidar graciosamente com falhas parciais (ex: broker MQTT indisponível, OBS fechado, webhook Roblox lento) sem crashar o processo principal.
**Decisão 1 - Sem Circuit Breakers**:
- Foi analisado se precisávamos implementar Circuit Breakers (estado Open/Closed/Half-Open). Optamos por NÃO. Timeouts robustos (ex: 5s no OBS), bounded queues com drop por prioridade (max 100/nível), e disconnect handlers (retry logic com backoff exponencial capped - 2, 4, 8, 16, 30s) garantem robustez e resiliência sem adicionar a complexidade de gerenciar a máquina de estados do CB.
**Decisão 2 - Estado Puramente Efêmero**:
- Todos os caches (deduplication cache, queues limitadas de OBS/MQTT, interaction cooldown state) são efêmeros (em memória).
- A vantagem é que um restart é garantido limpo — não sofremos com "poison pills" guardadas em SQLite ou Redis.
- A desvantagem (aceitável) é uma pequena janela de tempo onde um evento replicado pelo TikTok poderá ser reprocessado ao ligar, mitigado rapidamente pelo preenchimento imediato da rule table.
**Decisão 3 - Watchdog Flag**:
- O Watchdog serve como um sensor de sanidade. O stall não mata a thread (apenas a sinaliza como DEGRADED) permitindo observabilidade via /health local, dando a chance de um shutdown ou reconnect assistido.
**Decisão 4 - Chaos Testing Incorporado**:
- Ao invés de testes convencionais unitários apenas, simuladores de slow e failing consumers validam as restrições de latência em cenários adversos de OOM e latência (Fase 10 tests).

## DECIDIDO — fila limitada com prioridade (ADR-002)

A fila terá limite de capacidade e tratamento distinto por prioridade. Comentários de baixa prioridade podem ser agregados ou descartados; controle crítico e eventos de alto valor devem ter reserva ou política de overflow observável. Motivo: memória infinita não resolve throughput e pode derrubar o processo durante flood.

## DECIDIDO — batching condicionado por política (ADR-003)

Batching não será padrão universal. A engine deve considerar tamanho, idade do primeiro evento, prioridade, tipo e capacidade do consumidor. O batch precisa declarar a latência adicionada e não pode incluir comandos que exijam ordem ou resposta imediata sem validação.

## DECIDIDO — observabilidade como contrato operacional (ADR-008)

A aplicação deverá expor: estado de conexão, contadores de eventos, profundidade da fila, latência média/p95, descartes, erros, reconexões e entidades ativas. Não será permitido alegar saúde apenas porque o processo está rodando.

---

## DECIDIDO — dataclasses da stdlib no domínio, não Pydantic (fase 1)

`Event`, `EventUser` e `Command` são `@dataclass(frozen=True, slots=True)` com validação manual em `__post_init__`, sem Pydantic. Motivo: `domain_first` exige que o domínio seja testável sem nenhum framework de infraestrutura, e a stdlib já resolve o problema de validação nesta fase sem dependência nova. Se a API local (FastAPI) precisar de Pydantic depois, o mapping fica na borda (`api/`), não dentro do domínio.

## DECIDIDO — Python 3.11+ como baseline (fase 1)

Motivo: uso de `dataclass(slots=True)` (3.10+) e `X | None` em anotações de tipo. Ambiente de desenvolvimento validado nesta fase: Python 3.12.3. Ainda não verificado na máquina real da Paola — checar antes de assumir compatibilidade total.

## DECIDIDO — sem `[build-system]` no pyproject.toml por enquanto (fase 1)

O projeto ainda não precisa ser instalável como pacote (`pip install -e .`) porque não há nenhum consumidor externo do código ainda. `pytest` roda direto via `pythonpath = ["."]`. Empacotamento formal (hatchling ou equivalente) fica pra fase 8 (Packaging), quando existir um motivo real (launcher, distribuição).

## EM ABERTO — application/, infrastructure/ e api/ da fundação

O `target_architecture` da fase 1 propõe `src/application`, `src/infrastructure` e `src/api`, mas nenhum dos três foi criado agora: não faz sentido ter módulos de orquestração, integração ou API sem nada real pra orquestrar, integrar ou expor ainda (regra da fase 1: "não criar arquivos vazios apenas para deixar a árvore bonita"). Eles entram na fase 2 em diante, quando a ingestão do TikTok (fase 2) ou a API local (fase 3) começarem de verdade.

## EM ABERTO — regra de dedupe por evento não-GIFT/COMMENT

`Event.deduplication_key()` cai pro payload inteiro serializado pra FOLLOW/SHARE/LIKE/SYSTEM/MANUAL/CUSTOM — comportamento conservador (quase nunca considera duplicata) até existirem dados reais de live pra calibrar uma regra melhor tipo por tipo.

---

## DECIDIDO — PriorityQueueSet com 5 filas separadas em vez de heapq (fase 3)

Em vez de uma única `asyncio.PriorityQueue` (que usa `heapq` e não preserva ordem FIFO de itens com mesma prioridade nem permite controle de tamanho por prioridade fácil), o Event Engine usa 5 `asyncio.Queue` distintas, com um scheduler explícito WRR (Weighted Round Robin) e uma express lane para SYSTEM events.
Motivo: Evita STARVATION silencioso de prioridades P3/P4, permite limitar o buffer de cada nível de forma independente (ex: drop early de likes se a fila encher) e simplifica as métricas de profundidade.

## DECIDIDO — Agregação de tempo-real e Deduplicação LRU (fase 3)

Deduplicação: Usa `OrderedDict` com `maxsize` e verificação de TTL lazy na leitura, limpo periodicamente. Se o cache exceder `maxsize`, aplica LRU eviction.
Agregação: `EventAggregator` colapsa floods num `AggregatedEvent` (`count >= 2`). Gifts, eventos SYSTEM e MANUAL não são agregáveis. Usa dicionários sem coleções não limitadas.

## DECIDIDO — Dispatcher com Consumers isolados via Protocol (fase 3)

O envio de eventos para consumidores (como o futuro Roblox Bridge) não ocorre via herança nem acoplamento forte. O `Dispatcher` aceita qualquer objeto que implemente o `EventConsumer` Protocol (duck typing). Falha em um consumidor não afeta outros nem trava a fila principal. O Roblox Bridge será apenas mais um consumer registrado na Fase 4.

---

## DECIDIDO — semântica de entrega at-least-once com idempotência (fase 4)

Escolhida em vez de tentar exactly-once (que a spec da fase 4 proíbe prometer sem prova) ou at-most-once (que perderia eventos em qualquer falha de rede). `RobloxBridge` nunca regenera `event_id`; `GET /events` nunca remove do buffer; o lado Roblox mantém cache de dedupe bounded. Ver `ROBLOX_BRIDGE.md`.

## DECIDIDO — cursor-based consumption em vez de lease/ack obrigatório (fase 4)

Único consumidor (um Roblox Studio local) nesta fase — um modelo de lease/distribuição multi-consumidor seria complexidade sem uso real (`anti_overengineering`). `POST /ack` existe só para observabilidade (saber o atraso do Roblox), não controla o que é retido no buffer.

## DECIDIDO — buffer em memória bounded (deque), sem persistência (fase 4)

Mesma lógica do "sem banco de dados na v1": não há requisito real de sobreviver a um restart do processo Python ainda. Overflow descarta o mais antigo (FIFO) e incrementa `events_evicted_total`, nunca cresce sem limite.

## DECIDIDO — `RobloxBridge` como `EventConsumer`, não como novo caminho de fila (fase 4)

O bridge se registra no `Dispatcher` já existente da fase 3 (`EventConsumer` Protocol) em vez de o Event Engine ganhar um segundo mecanismo de entrega. Mantém a garantia de "Roblox é só mais um consumer" estabelecida em `DECIDIDO — Dispatcher com Consumers isolados` (fase 3).

## EM ABERTO — `app.py` de composição da aplicação

`RobloxBridge`/`local_api` (fase 4) e `TikTokLiveConnector`/`EventProcessor` (fases 2-3) ainda não estão fisicamente ligados num processo único rodando de ponta a ponta. Isso é composição — decidir como o processo principal inicia TikTok + engine + bridge + API juntos, com shutdown coordenado entre os quatro. Não implementado ainda porque não era escopo de nenhuma fase até agora; vira bloqueio real assim que alguém quiser rodar o sistema completo pela primeira vez.

## EM ABERTO — o que fazer quando `gap_detected` é true

`RobloxBridge.get_events_since` já sinaliza quando eventos foram descartados por overflow antes do Roblox consumir. `BridgeClient.lua` hoje só loga um aviso. Decidir uma política real (pular e seguir, alertar o creator na tela, pedir replay de alguma fonte) depende de dados de quão frequente isso é numa live real — não decidir isso agora por suposição.

---

## decisões que não devem ser tomadas por suposição

Não assumir que uma biblioteca possui um método, que localhost é acessível em produção, que um evento externo é idempotente, que gifts têm nomes estáveis, que o Roblox tolera qualquer frequência de request ou que logs com usernames são inofensivos. Cada item precisa de evidência reproduzível.

## DECIDIDO — runtime Roblox server-side e identidade explícita (fase 5)

O runtime de gameplay vive em `roblox/src` e recebe `Command` já decodificado. Ele não interpreta TikTok e não converte nome exibido da live em username Roblox. A resolução aceita apenas `parameters.avatar_user_id` ou um mapping configurado; sem identidade confiável, usa fallback local.

## DECIDIDO — APIs assíncronas atuais de avatar

Foram verificadas no Creator Hub as APIs `Players:GetHumanoidDescriptionFromUserIdAsync` e `Players:CreateHumanoidModelFromDescriptionAsync`. Os nomes sem `Async` aparecem como deprecated na documentação atual e não são usados. `Instance:SetAttribute/GetAttribute/Destroy`, `Model:PivotTo` e `Debris:AddItem` também foram conferidos. O cleanup principal permanece explícito no `CleanupManager`, porque Debris sozinho não fornece a contagem, prioridade e idempotência exigidas.

## DECIDIDO — fallback e limites bounded do avatar runtime

O fallback é um Model local com Part ancorada e Humanoid, sem rede ou asset ID. O runtime inicia com máximo de 100 avatares ativos, TTL de 60 segundos para comentários, 300 segundos para gifts, 100 spawns pendentes e 10 spawns por segundo. Esses valores são defaults experimentais, não capacidade medida do Roblox. Quando o limite ativo é atingido, a política remove o registro mais antigo não protegido; se só houver registros protegidos, rejeita o novo spawn.

## DECIDIDO — efeitos allowlisted sem conteúdo arbitrário

Os efeitos iniciais são `HEARTS`, `GOLD_AURA` e `BLUE_GLOW`, implementados com `Highlight`. Asset IDs, scripts externos e texto do TikTok não controlam código, instâncias ou assets. Todo efeito possui lifetime e é destruído pelo seu serviço ou no shutdown.

## DECIDIDO — Interaction Rules Engine declarativo (fase 6)

Regras vivem em `configs/interaction_rules.json` e são validadas para produzir `ActionDefinition`, `ActionResult` e finalmente `GameEvent`. O connector TikTok continua limitado à normalização; o Roblox Runtime continua limitado à execução. O `InteractionConsumer` conecta o Dispatcher ao `RobloxBridge` sem criar uma fila paralela.

## DECIDIDO — apenas actions com consumidor real

Nesta fase, `SPAWN_AVATAR` é o único action type habilitado porque é o único handler de gameplay implementado e conectado. `PLAY_EFFECT`, `SHOW_MESSAGE` e `UPDATE_COUNTER` permanecem conceitos futuros; efeitos já existentes entram como parâmetro allowlisted do spawn. Uma configuração que aceita uma action sem consumidor só cria sucesso falso, então ela falha cedo.

## DECIDIDO — gifts, quantidade e streaks sem suposição

O normalizer verificado fornece `gift_id`, `gift_name`, `repeat_count`, `combo_count` e `repeat_end`. O engine usa `gift_id`/`gift_name` para matching e `repeat_count` apenas quando `min_quantity` foi configurado. Não existe interpretação de coins ou streak, pois o contrato atual não prova sua semântica operacional nem evita contagem dupla.

## DECIDIDO — agregação opt-in, combos adiados

Agregação exige configuração explícita de janela e threshold e possui máximo de estados. Gifts não são agregados por padrão. Combos não foram implementados porque ainda não há uma mecânica real que justifique estado adicional bounded, reset, timeout e dedupe.

## DECIDIDO — logs de auditoria em background (fase 7)

O `EventAuditLogger` envia rastros para os arquivos JSONL por meio de um `asyncio.Queue` e uma worker task dedicada (limite rígido na fila). Isso foi escolhido para impedir que I/O (disco lento) acabe travando a performance do loop de eventos principal (`asyncio`). Se a fila encher, rastros podem ser descartados, mas o fluxo não trava.

## DECIDIDO — watchdog de ausência de progresso, não só processo vivo (fase 7)

A saúde do sistema é avaliada pelo tempo desde o último avanço útil (`mark_active` e `record_success`), ao invés de checar se o processo está rodando ou se a porta 8787 responde. Isso ataca diretamente falhas silenciosas de coroutines travadas (ex. starvation de worker).

---

## DECIDIDO — obsws-python para obs-websocket 5.x (fase 8)

Biblioteca `obsws-python>=1.7,<2.0` escolhida para comunicação com OBS Studio. O cliente síncrono `ReqClient` é isolado e encapsulado usando `loop.run_in_executor(None, ...)` com timeout, garantindo que requisições bloqueantes do cliente OBS nunca travem o loop asyncio do Event Engine.

## DECIDIDO — OBS Adapter como consumidor desacoplado no Dispatcher (fase 8)

O `OBSAdapter` registra-se no `Dispatcher` como `EventConsumer`. O método `handle()` apenas valida e enfileira `OBSAction` em uma fila prioritária própria (`OBSPriorityQueue`), retornando imediatamente. Uma worker task em background consome as ações e executa a comunicação via WebSocket. Se o OBS falhar, desconectar ou travar, a ingestão do TikTok e o Roblox Bridge continuam operando normalmente sem degradação.

## DECIDIDO — allowlists estritas e proteção contra payloads maliciosos (fase 8)

Cenas e fontes do OBS só podem ser acionadas se constarem nas allowlists configuradas (`allowed_scenes` e `allowed_sources`). Requisições para reproduzir mídias (`OBS_TRIGGER_MEDIA`) recusam paths de arquivos locais ou URLs externas vindas do payload do evento — mídias devem ser predefinidas no OBS. Textos enviados a overlays de tela têm caracteres de controle removidos para evitar erros de renderização.

## DECIDIDO — respeitar override manual do streamer em efeitos temporários (fase 8)

Efeitos visuais temporários (ex: trocar para a cena de celebração por 5 segundos e retornar) verificam a cena atual no OBS antes de restaurar a cena inicial. Se o streamer mudou a cena manualmente no OBS durante o efeito, o efeito cancela a restauração automática para priorizar a decisão manual do streamer.

---

## DECIDIDO — MQTT como consumidor desacoplado do Rule Engine (fase 9)

O `MQTTAdapter` consome `GameEvent` emitidos pelo `InteractionRuleEngine` e despacha para dispositivos físicos (ESP32). Ele opera como um bridge complementar via `InteractionConsumer.bridges`. Uma falha de broker ou conexão nunca interrompe a ingestão do TikTok, a ponte com o Roblox ou as automações de OBS.

## DECIDIDO — aiomqtt (Paho MQTT) para AsyncIO nativo (fase 9)

Utilizou-se `aiomqtt>=2.3.0` sobre `paho-mqtt>=2.1.0`. A comunicação com o broker é totalmente nativa no loop assíncrono do Python, implementando reconexão automática com backoff exponencial controlado (`[2, 4, 8, 16, 30]s`), keepalive e clean session.

## DECIDIDO — separação estrita entre comandos e telemetria (fase 9)

O tráfego MQTT foi estritamente segregado em tópicos com semânticas distintas:
- Comandos outbound (`liveengine/v1/device/{device_id}/command`) com validação de payload, allowlist e TTL.
- Telemetria inbound (`liveengine/v1/device/{device_id}/heartbeat` e `/telemetry`) consumida assincronamente para rastrear liveness (`device_last_seen`) e erros de execução reportados pelo firmware.

## DECIDIDO — proibição de mensagens retidas para comandos one-shot (fase 9)

Mensagens publicadas com comandos físicos utilizam explicitamente `retain=False`. Se um ESP32 reconectar após período offline, ele não deve executar cegamente comandos antigos cuja relevância temporal na Live já expirou.

## DECIDIDO — limites físicos rígidos e segurança por design (fase 9)

Atuação física na Fase 9 é limitada estritamente a indicadores visuais de baixa tensão (LEDs, anéis neopixel, matrizes de LED e displays). Qualquer controle de alta tensão, potência térmica ou atuadores mecânicos perigosos é expressamente vedado. Entradas de usuários da Live (comentários ou usernames) nunca determinam nomes de tópicos ou rotinas de baixo nível.

## DECIDIDO — configuração única por ambiente e CLI oficial (fase 11)

A fonte oficial de configuração é o ambiente, carregado opcionalmente de `.env` por `src.app._load_env_file`. `src.config` converte strings em valores tipados, valida ranges e produz `Settings`/`FeatureFlags`; `AppConfig` agrega os contratos específicos de engine, bridge, OBS, MQTT, paths e lifecycle. Não serão mantidos arquivos de configuração concorrentes nem constantes operacionais escondidas em adapters.

## DECIDIDO — `liveengine` como entrypoint local

O pacote registra `liveengine = src.cli:main` no `pyproject.toml`. Os comandos oficiais são `version`, `check`, `config validate`, `status`, `start`, `stop` e `simulate`. `start` usa um PID file bounded e um único processo local; `stop` envia shutdown gracioso. Não foi adicionado service manager, Docker, installer, cloud ou auto-update.

## DECIDIDO — integrações opcionais e capabilities efetivas

TikTok, OBS, MQTT, gravação e Roblox são controlados por feature flags. OBS e MQTT desligados não impedem o core de iniciar. A Local API expõe capabilities efetivas, distinguindo componente criado/pronto de integração apenas configurada. Senhas não aparecem em `/health`, CLI, logs normais ou arquivos gerados.

## DECIDIDO — simulation e dry-run locais

`liveengine simulate` usa eventos determinísticos com `source = simulation`, carrega as regras reais e passa pelo Interaction Rules Engine. `--dry-run` imprime os GameEvents sem publicar no Roblox, OBS ou MQTT. Isso permite validar regras e bursts sem fingir que uma simulação é TikTok real. O modo `replay` fica reservado no contrato, mas ainda não há reprodutor persistente implementado.

## DECIDIDO — packaging mínimo reproduzível

O projeto usa `setuptools` via `pyproject.toml`, mantém dependências diretas declaradas e instala em venv com `pip install -e '.[dev]'`. A compatibilidade efetivamente testada nesta fase é Python 3.12 em Linux; Python 3.10+ permanece requisito declarado, mas não deve ser tratado como validado sem executar a suíte nesse interpretador.
*** End Patch
