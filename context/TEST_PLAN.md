# TEST_PLAN.md — plano de testes

## estado

**Fase 1 (fundação) — 26 testes unitários implementados e passando**, cobrindo só domínio puro, sem rede e sem Roblox: `src/tests/unit/test_events.py`, `test_commands.py`, `test_priorities.py`, `test_config.py`. Executados em `Python 3.12.3`, com `26 passed` (comando: `pytest` na raiz do repo). Cobre: criação de `Event`/`Command` válido e inválido, geração de identidade única, invariantes (`source` vazio, `payload` não-dict, timestamp naive, `display_name` vazio), rejeição de enums e tipos desconhecidos, timestamps com timezone, chave de deduplication estável e canônica, resolução de prioridade padrão por tipo e ordenação P0→P4, validação de `ENVIRONMENT`/`LOG_LEVEL`.

**Fase 2 — connector TikTok implementado com 38 testes passando**, incluindo normalização de comment/gift/follow, identidade, timestamps, buffer bounded, estados, backoff limitado e shutdown idempotente, inclusive quando solicitado imediatamente após o start. A suíte continua sem depender de uma LIVE ativa. Nenhum teste real contra uma LIVE foi executado nesta sessão.

**Fase 3 — Event Engine completo, 139 testes unitários e 2 testes de carga passando**. Implementado o pipeline `receive → deduplicate → aggregate → priority_queue (WRR) → dispatch`. Inclui `PriorityQueueSet` (5 filas independentes + express lane), `DeduplicationCache` (LRU + TTL), `EventAggregator` (janela temporal, exclusão de gifts/P0), e `EventProcessor` (pool de workers e backpressure). Foram adicionados testes de carga em `src/tests/load/` simulando floods de comentários (validação de aggregation) e contenção rigorosa com consumer lento (garantia de drop P4 e sobrevivência P1). Todos os testes passando em Python 3.10 local.

**Fase 4 — Roblox Bridge (Local API + buffer + consumidor Luau), 28 testes novos, 169 no total (fase 1-4 combinadas)**. `src/tests/unit/test_roblox_bridge.py` cobre tradução `Event`/`AggregatedEvent` → `GameEventEnvelope`, buffer bounded com cursor, eviction FIFO, detecção de gap, ack sem regressão. `src/tests/unit/test_local_api.py` cobre os três endpoints via `fastapi.testclient.TestClient` (sem servidor real): health sem payload de evento, `/events` com `since`/`limit` válidos e inválidos (422 em query malformada), `/ack` com corpo válido/inválido, rota desconhecida (404). Executados em `Python 3.12.3`, `fastapi 0.141.1`, `uvicorn 0.53.0`, `httpx 0.28.1` — `169 passed`. **Nenhum teste manual real em Roblox Studio foi executado** (este agente não tem acesso a Roblox Studio) — ver `manual_studio_test` abaixo e `context/ROBLOX_BRIDGE.md`. `roblox/src/BridgeClient.lua` não tem suíte de testes própria porque não há runtime Luau disponível neste ambiente para rodá-la; a validação dessa lado é manual, por design desta fase.

**Fase 5 — Roblox Avatar Runtime implementado em Luau**, com router, handlers, identidade explícita, cache TTL/LRU, lookup in-flight, fallback, limite de instâncias, limite de spawn, efeitos allowlisted e cleanup centralizado. Como não há Roblox Studio nem runtime Luau neste ambiente, a validação executável desta fase é o checklist em `roblox/tests/ROBLOX_RUNTIME_TESTS.md`; criação efetiva de avatar, transporte HTTP e impacto de frame ainda não foram medidos.

**Fase 6 — Interaction Rules Engine implementado**, com 10 testes novos cobrindo matching por gift/comment, múltiplas ações, cooldown por usuário, dedupe, agregação opt-in, rate limit, expiração, validação e integração com o `RobloxBridge`. A execução local passou com a suíte completa; bursts de 1000 comentários e 100 gifts ainda precisam ser medidos com os limites de produção definidos por dados reais.

**Fase 7 — Observability implementada (Unit + Failure Injection)**. Adicionados testes cobrindo deteção de stall de workers assíncronos (`Watchdog`), geração de logs em background (`EventAuditLogger`), coleta consolidada de métricas (`OperationalSnapshot`) e formatter (`JsonFormatter`). O sistema expõe estado em tempo-real `/health` do Bridge.

O fluxo TikTok→Engine→Rules→RobloxBridge agora tem contratos implementados e observabilidade granular, mas a composição única do processo e o teste manual no Roblox Studio continuam pendentes. Combos, OBS, MQTT e handlers de mensagem/contador não foram implementados porque ainda não há mecânica real ou consumidor correspondente.

**Princípio:** cada camada testável isolada. Nada passa pra fase seguinte sem a fundação da fase anterior validada.

---

## pirâmide de validação

### 1. testes unitários

Validar sem rede:

- parsing e validação do evento interno;
- normalização de payloads válidos e inválidos;
- cálculo de prioridade;
- deduplicação por chave e janela;
- cooldown por usuário e global;
- ordenação da fila por prioridade e idade;
- limites de tamanho e capacidade;
- política de descarte e agregação;
- mapping de gift;
- expiração e cleanup de entidades;
- redaction de logs;
- backoff e transições de estado.

### 2. testes de contrato

Validar que:

- eventos produzidos pelo normalizer obedecem `EVENT_SCHEMA.md`;
- comandos enviados ao bridge têm versão e campos obrigatórios;
- versões desconhecidas são rejeitadas;
- payloads excessivos não atravessam a fronteira;
- o Roblox não precisa interpretar campos específicos do TikTok.

### 3. testes de integração

Substituir cada dependência externa por um servidor ou adaptador controlado, sem chamar uma API inexistente:

- source conecta, recebe e encerra;
- erro transitório dispara backoff;
- encerramento normal não entra em loop de reconexão;
- API local aceita comando válido e rejeita inválido;
- bridge entrega comando e trata timeout;
- recorder grava JSONL recuperável;
- health reporta componentes desconectados.

### 4. experimentos externos obrigatórios

| Experimento | Pergunta | Saída mínima |
|---|---|---|
| TikTok spike | quais eventos e metadados são realmente disponíveis? | payloads anonimizados e limites observados |
| Roblox Studio | Studio alcança a API local pelo mecanismo escolhido? | request/response, latência e erros |
| Roblox publicado | o mesmo fluxo funciona fora do Studio? | resultado separado, nunca inferido do Studio |
| Avatar | lookup, cache e cleanup respeitam limites? | contagem e tempo de vida |
| OBS | autenticação, comandos e reconexão funcionam? | contrato e falhas observadas |

### 5. testes de carga

Gerar eventos sintéticos em taxas crescentes (ex: 30, 100 e 500 eventos/s), sem alegar que representam uma LIVE real. Medir:

- throughput e profundidade máxima da fila;
- latência média e p95;
- descarte por classe de prioridade;
- memória, CPU e tempo de recuperação.

Verificar que P0/P1 não são silenciosamente expulsos por P3/P4.

### 6. testes de falha

Cobrir obrigatoriamente:

- desconexão do TikTok (queda de rede, live termina);
- evento malformado (payload inesperado da lib de ingestão);
- evento duplicado;
- flood de eventos (P4 inundando a fila);
- overflow da fila (validar drop policy);
- Roblox indisponível (bridge não consegue falar com a API local);
- OBS indisponível;
- API local indisponível (Roblox tentando poll sem resposta);
- username do Roblox inválido (na resolução de identidade);
- falha na busca de avatar;
- consumidor lento (Roblox processando mais devagar que a fila enche);
- processo reiniciado (o engine reconecta e recupera estado, ou começa limpo? — decisão a registrar em `DECISIONS.md`);
- arquivo JSONL sem permissão ou cheio;
- relógio fora de ordem.

---

## o que precisa ser validado experimentalmente antes de virar premissa

- **HttpService alcança `localhost` a partir de Studio (Play Solo/Team Test)?** — **parcialmente respondido na fase 4** por documentação oficial (exemplo atual da Roblox conectando a um servidor local via `CreateWebStreamClient`), mas **ainda sem teste manual real** feito por alguém com acesso a Roblox Studio. Ver `context/ROBLOX_BRIDGE.md`.
- **HttpService alcança `localhost` a partir de um jogo publicado?** — **respondido (estrutural, não precisa de teste)**: o servidor publicado roda na infraestrutura da Roblox, uma máquina diferente da do creator — `localhost` nesse contexto nunca aponta pro computador do creator. Ver `context/ROBLOX_BRIDGE.md`.
- **Limite de 500 req/min por server do HttpService** — **confirmado na documentação oficial atual** (fase 4). Com `POLL_INTERVAL_SECONDS = 2` do `BridgeClient.lua`, o consumo é de ~30 req/min pra `/events` + ~30 pra `/ack`, bem abaixo do limite — mas isso não foi testado contra uma live real com throughput variável.
- **Throughput real de uma live média/grande** — comentários e gifts por segundo em cenário real, pra calibrar tamanho da fila e cooldowns (os números do brief são hipotéticos). Ainda não medido.
- **Latência ponta a ponta** (TikTok → Roblox executando) em condição normal e sob flood. Ainda não medido — depende da composição end-to-end (`app.py`, ver `DECISIONS.md`) que ainda não existe.

---

## critérios de passagem

Um teste passa apenas quando o comportamento esperado é observável e verificável. Para cada falha, deve existir uma destas respostas: rejeição segura, retry limitado, degradação explícita, agregação, descarte com motivo ou encerramento controlado.

---

## ordem sugerida

- **Fase 1** (TikTok → Python) e **Fase 2** (Python → API local) são testáveis sem tocar em Roblox — priorizar essas antes de abrir o Studio.
- **Fase 3** (API local → Roblox Studio) é onde o risco de localhost vira fato ou vira bloqueio — não seguir pra fase 4 sem essa resposta.
- A fase de integração não avança para operação real enquanto Studio e ambiente publicado não tiverem resultados separados.

---

## métricas mínimas a registrar durante os testes

Eventos recebidos, normalizados, aceitos, processados, agregados e descartados; profundidade da fila; latência média e p95; erros; tentativas de reconexão; estado de TikTok, API, Roblox e OBS; entidades ativas; memória e CPU.

---

## reprodutibilidade

Cada cenário deve possuir seed ou fixture determinística quando possível. Dados de usuários devem ser fictícios ou minimizados. Testes contra serviços externos devem ser marcados como experimentais e não podem ser usados para esconder a ausência de testes unitários.
