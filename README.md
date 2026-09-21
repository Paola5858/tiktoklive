# tiktoklive

event engine que transforma eventos de uma live do TikTok em eventos interativos no Roblox (spawn de avatar, efeitos, etc), com fila, prioridade e observabilidade — sem o Roblox nunca precisar entender a estrutura interna do TikTok.

## status atual: fase 4 — Roblox Bridge

Domínio, connector de TikTok (`TikTokLive==7.0.1`), Event Engine (fila com prioridade WRR, dedupe, agregação, dispatcher) e agora a ponte com o Roblox (Local API + buffer + consumidor Luau) existem e têm testes automatizados passando — 169 no total. **Ainda não tem**: composição end-to-end num processo só (`app.py`), Gift Mapping Engine, avatar real, OBS, MQTT. Ver `context/ROBLOX_BRIDGE.md` para o contrato completo desta fase, incluindo um achado importante sobre `localhost` e HttpService.

A biblioteca de TikTok é um projeto de engenharia reversa e declara Modified AGPL-3.0. Nesta fase ela é usada localmente e a versão está pinada para que mudanças upstream não alterem silenciosamente o contrato.

## documentação de contexto

- [`context/PROJECT_SPEC.md`](context/PROJECT_SPEC.md) — escopo, objetivo, riscos
- [`context/ARCHITECTURE.md`](context/ARCHITECTURE.md) — pipeline, contratos, estrutura de pastas
- [`context/EVENT_SCHEMA.md`](context/EVENT_SCHEMA.md) — schema do evento interno e do comando
- [`context/DECISIONS.md`](context/DECISIONS.md) — decisões arquiteturais registradas
- [`context/TEST_PLAN.md`](context/TEST_PLAN.md) — plano de testes por fase
- [`context/ROBLOX_BRIDGE.md`](context/ROBLOX_BRIDGE.md) — a ponte Python ↔ Roblox: contrato, polling, idempotência, o que se sabe sobre localhost

## setup

requer python 3.11 ou superior.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install '.[dev]'
cp .env.example .env
```

**Nota de honestidade sobre `pip install -e '.[dev]'`:** o projeto ainda não
tem `[tool.setuptools]`/`[tool.hatch.build]` configurado, então mesmo um
install editable não deixa `src` importável fora do repositório — os
testes e scripts funcionam a partir da raiz do repo por causa de
`pythonpath = ["."]` no `pyproject.toml`, não por causa do install. Rode os
comandos abaixo sempre a partir da raiz do repositório. Empacotamento de
verdade fica pra fase 8 (Packaging).

## rodando os testes

```bash
pytest
```

Todos os testes atuais rodam sem internet e sem Roblox: domínio, normalização e lifecycle do connector com client externo simulado, Event Engine, e a Roblox Bridge/Local API via `fastapi.testclient.TestClient` (ASGI em memória, sem servidor real).

## observando uma live (fase 2)

```bash
python -m src.tiktok_probe @username_da_live
```

O probe registra apenas o tipo, o identificador externo quando disponível e o nome exibido. Ctrl+C executa o shutdown do connector. Uma LIVE real ainda precisa ser validada manualmente.

## testando o Roblox Bridge manualmente

```bash
python -m src.adapters.local_api
```

Sobe a Local API sozinha em `127.0.0.1:8787` com um bridge vazio, pra testar o polling do `roblox/src/BridgeClient.lua` a partir do Roblox Studio. Ver `context/ROBLOX_BRIDGE.md` pra o passo a passo completo e o que ainda não foi validado (nenhum teste manual real em Studio foi feito até agora).

## estrutura

```
src/
  config.py           # configuração validada via variável de ambiente
  errors.py           # erros base da aplicação
  logging.py          # logging estruturado
  domain/
    events.py          # Event, AggregatedEvent, EventType, EventStatus, EventUser
    commands.py         # Command, CommandType — formato-alvo pra quando o Gift Mapping Engine existir
    priorities.py        # modelo de prioridade (P0-P4)
    errors.py             # erros de domínio
  ingestion/
    base.py                # estados e métricas do connector
    buffer.py                # buffer bounded
    normalizer.py             # TikTokLive → Event
    tiktok.py                  # conexão, listeners, backoff e shutdown
  engine/
    queue.py                    # PriorityQueueSet — 5 filas + express lane + WRR
    dedup.py                     # cache de deduplicação LRU + TTL
    aggregator.py                 # agregação por janela temporal
    dispatcher.py                  # EventConsumer Protocol + despacho tolerante a falhas
    processor.py                    # orquestrador do pipeline (workers, shutdown)
    config.py                        # EngineConfig
    metrics.py                        # métricas do engine
  adapters/
    roblox.py                          # RobloxBridge — tradução Event → envelope + buffer
    local_api.py                        # FastAPI: GET /health, GET /events, POST /ack
  tiktok_probe.py                        # entrypoint local da fase 2
  tests/unit/                             # testes de domínio, engine e adapters, sem infraestrutura real
  tests/load/                              # testes de carga (flood de comentários, consumer lento)
roblox/
  src/
    BridgeClient.lua                        # consumidor Luau: polling, backoff, dedupe, router-esqueleto
context/                                      # memória persistente do projeto (specs, decisões, testes)
```
