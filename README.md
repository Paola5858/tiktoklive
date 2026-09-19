# tiktoklive

event engine que transforma eventos de uma live do TikTok em eventos interativos no Roblox (spawn de avatar, efeitos, etc), com fila, prioridade e observabilidade — sem o Roblox nunca precisar entender a estrutura interna do TikTok.

## status atual: fase 1 — fundação

só existe o domínio (`src/domain/`) e a configuração base (`src/config.py`, `src/errors.py`, `src/logging.py`). **ainda não tem**: conector de TikTok, bridge de Roblox, integração com OBS, fila, API local, MQTT. isso é proposital — ver `context/PROJECT_SPEC.md` e as fases registradas em `context/DECISIONS.md`.

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
pip install pytest
cp .env.example .env
```

(o projeto ainda não tem `[build-system]` no `pyproject.toml` de propósito — não faz sentido empacotar/instalar algo que ainda não tem nenhuma integração real. isso entra quando a fase de packaging chegar.)

## rodando os testes

```bash
pytest
```

todos os testes atuais rodam sem internet e sem Roblox: são testes de domínio puro (criação de evento e de comando, validação, prioridade, dedupe, configuração).

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
  tests/unit/         # testes de domínio, sem infraestrutura
context/               # memória persistente do projeto (specs, decisões, testes)
```
