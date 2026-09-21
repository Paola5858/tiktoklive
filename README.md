# tiktoklive

event engine que transforma eventos de uma live do TikTok em eventos interativos no Roblox (spawn de avatar, efeitos, etc), com fila, prioridade e observabilidade — sem o Roblox nunca precisar entender a estrutura interna do TikTok.

## status atual: fase 5 — Roblox Avatar Runtime

o domínio, o connector TikTok e o Event Engine existem. A fase 5 adiciona o runtime server-side em `roblox/src`: router de GameEvents, AvatarService, cache bounded, fallback, limites de spawn, efeitos allowlisted e cleanup automático. **o transporte Roblox Bridge/API local não está presente no histórico revisado**, então o runtime recebe comandos já decodificados pela fronteira `LiveRuntime:handle(command)`.

A biblioteca é um projeto de engenharia reversa e declara Modified AGPL-3.0. Nesta fase ela é usada localmente e a versão está pinada para que mudanças upstream não alterem silenciosamente o contrato.

## documentação de contexto

- [`context/PROJECT_SPEC.md`](context/PROJECT_SPEC.md) — escopo, objetivo, riscos
- [`context/ARCHITECTURE.md`](context/ARCHITECTURE.md) — pipeline, contratos, estrutura de pastas
- [`context/EVENT_SCHEMA.md`](context/EVENT_SCHEMA.md) — schema do evento interno e do comando
- [`context/DECISIONS.md`](context/DECISIONS.md) — decisões arquiteturais registradas
- [`context/TEST_PLAN.md`](context/TEST_PLAN.md) — plano de testes por fase

## setup

requer python 3.11 ou superior.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
```

(o projeto ainda não tem `[build-system]` no `pyproject.toml` de propósito — não faz sentido empacotar/instalar algo que ainda não tem nenhuma integração real. isso entra quando a fase de packaging chegar.)

## rodando os testes

```bash
pytest
```

todos os testes atuais rodam sem internet e sem Roblox: são testes de domínio, normalização e lifecycle com client externo simulado.

## observando uma live

Com o ambiente virtual ativo, rode:

```bash
python -m src.tiktok_probe @username_da_live
```

O probe registra apenas o tipo, o identificador externo quando disponível e o nome exibido. Ctrl+C executa o shutdown do connector. A biblioteca precisa conseguir resolver o perfil e a LIVE precisa estar ativa; uma LIVE real ainda precisa ser validada manualmente.

## runtime Roblox

Copie os ModuleScripts de `roblox/src` para um container no `ServerScriptService` do Studio, mantendo-os como irmãos de `init.server.lua`. O Script de entrada cria o `LiveRuntime`, inicia o cleanup e deixa o ponto de integração para o Bridge:

```lua
local ok, reason = runtime:handle(decodedCommand)
```

Leia `roblox/ROBLOX_RUNTIME.md` e execute o checklist em `roblox/tests/ROBLOX_RUNTIME_TESTS.md`. A aparência real do avatar e a comunicação com a API local não foram executadas nesta máquina, porque o ambiente não possui Roblox Studio.

## estrutura

```
src/
  config.py         # configuração validada via variável de ambiente
  errors.py         # erros base da aplicação
  logging.py        # logging estruturado
  domain/
    events.py        # Event, EventType, EventStatus, EventUser
    commands.py       # Command — o que sai do event engine rumo ao Roblox
    priorities.py     # modelo de prioridade (P0-P4)
    errors.py         # erros de domínio
  ingestion/
    base.py           # estados e métricas do connector
    buffer.py         # buffer bounded
    normalizer.py     # TikTokLive → Event
    tiktok.py         # conexão, listeners, backoff e shutdown
  tiktok_probe.py     # entrypoint local da fase 2
  tests/unit/         # testes de domínio, sem infraestrutura
context/               # memória persistente do projeto (specs, decisões, testes)
```
