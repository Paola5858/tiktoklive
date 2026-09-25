# TikTok × Roblox Live Engine

Aplicação local que recebe eventos de uma LIVE do TikTok, normaliza, prioriza, limita e transforma regras aprovadas em comandos para Roblox, OBS e dispositivos MQTT opcionais. O Roblox não interpreta TikTok. Ele recebe comandos abstratos.

## Status

A composição end-to-end existe em `src/app.py`. A fase 11 consolida o pacote instalável, a CLI `liveengine`, configuração centralizada, feature flags, diagnóstico, PID file, modos de simulação e documentação de execução. OBS e MQTT continuam opcionais e desligados por padrão.

A integração `TikTokLive==7.0.1` é um projeto de engenharia reversa com licença Modified AGPL-3.0. O uso previsto é local e a versão permanece pinada para impedir mudanças silenciosas no contrato.

## Pré-requisitos

O ambiente validado nesta fase é Linux com Python 3.12. Node não é necessário para o engine. OBS Studio 28+ e um broker MQTT são necessários somente quando suas respectivas features forem habilitadas. Roblox Studio é necessário para validar o consumidor Luau e deve usar HttpService habilitado. A aplicação não instala nem inicia essas ferramentas externas por conta própria.

Python 3.10+ é aceito pelo metadata do pacote; Python 3.12 foi o ambiente efetivamente testado. A biblioteca TikTok exige conectividade e uma LIVE disponível apenas no modo `live`.

## Instalação reproduzível

A partir da raiz do clone:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env
```

O comando `pip install -e` agora usa o `build-system` oficial e registra a CLI `liveengine`. O `.env` é local, ignorado pelo Git e nunca deve conter valores que sejam colados em issues ou logs.

## Configuração

Existe uma única estratégia oficial: variáveis de ambiente, normalmente carregadas de `.env`. O arquivo `.env.example` documenta todas as variáveis por categoria: execução, flags, TikTok, Event Engine, Local API/Roblox, OBS, MQTT e paths.

As configurações são tipadas e validadas antes de iniciar processamento. Portas aceitam somente 1–65535, limites precisam ser positivos, thresholds ficam entre 0 e 1, URLs/hosts não são aceitos silenciosamente como vazios e uma feature habilitada exige seus dados mínimos. Senhas de OBS e MQTT só entram pelo ambiente e não aparecem no diagnóstico normal.

As flags principais são `FEATURE_TIKTOK`, `FEATURE_ROBLOX`, `FEATURE_OBS`, `FEATURE_MQTT`, `FEATURE_EVENT_RECORDING` e `FEATURE_DEBUG_LOGGING`. OBS e MQTT desligados não impedem o core de iniciar. `RUN_MODE` aceita `live`, `simulation` e `replay`; `simulation` não exige `TIKTOK_UNIQUE_ID`.

## CLI oficial

Depois da instalação:

```bash
liveengine version
liveengine check
liveengine config validate
liveengine status
liveengine start
liveengine stop
liveengine simulate --count 20 --dry-run
```

Também é possível usar `python -m src.cli` se o entrypoint ainda não estiver no `PATH`.

`check` valida Python, configuração, arquivo de regras, porta da Local API e dependências opcionais. O comando retorna exit code `0` quando tudo está pronto, `1` quando o processo está parado no `status` e `2` para configuração ou pré-requisito inválido. `status` nunca imprime senhas: informa processo, PID, API e estado de health.

`start` inicia uma única instância em background, registra o PID em `logs/liveengine.pid` e direciona saída para `logs/engine.log`. `stop` envia encerramento gracioso. Não há daemon proprietário, serviço de sistema ou instalador gigante escondido.

## Primeiro uso seguro

Para validar sem TikTok, Roblox, OBS ou MQTT:

```bash
RUN_MODE=simulation FEATURE_TIKTOK=false FEATURE_OBS=false FEATURE_MQTT=false liveengine check
liveengine simulate --count 20 --dry-run
```

O modo simulation cria eventos com `source = simulation`, mistura comentários e gifts fictícios e passa pelo `InteractionRuleEngine`. Com `--dry-run`, os `GameEvents` são impressos e nenhum consumidor externo é chamado. Um gift de exemplo não representa um gift real nem seu valor econômico.

Para iniciar o fluxo local real, configure `TIKTOK_UNIQUE_ID`, mantenha `RUN_MODE=live` e execute:

```bash
liveengine check
liveengine start
liveengine status
```

A Local API fica, por padrão, em `http://127.0.0.1:8787`. O Roblox Studio faz polling de `/events`; veja `context/ROBLOX_BRIDGE.md` antes de testar. `localhost` no Studio local não equivale a acesso a uma máquina do creator em uma experiência publicada.

## Startup e shutdown

O startup segue a ordem: carregar `.env`, validar config, configurar logging, criar watchdog/métricas, criar Dispatcher e bridge, carregar regras, iniciar consumers opcionais, iniciar Event Processor, subir Local API e só então iniciar a ingestão TikTok no modo live. A instância só grava o PID depois de os componentes principais terem sido criados.

O shutdown recebe SIGINT/SIGTERM, impede novas entradas, encerra TikTok, drena o que for permitido pelo Event Processor, para OBS/MQTT, encerra a Local API, finaliza auditoria e remove o PID file pertencente ao próprio processo. Buffers continuam efêmeros por decisão da v1.

## Observabilidade e arquivos

`GET /health` expõe o bridge e o snapshot operacional sem senhas. Auditoria JSONL fica em `logs/events/`; o log operacional fica em `logs/engine.log`. O PID e os JSONL são artefatos locais ignorados pelo Git. Métricas de fila, drops, retries, watchdog, OBS e MQTT seguem os contratos em `context/OBSERVABILITY.md` e `context/RELIABILITY.md`.

## Testes

```bash
.venv/bin/python -m pytest -q
```

Os testes unitários, de carga, falha e integração controlada não exigem uma LIVE, OBS Studio, broker ou Roblox Studio reais. O teste `src/tests/integration/test_obs_live.py` é experimental e deve ser executado apenas quando OBS estiver configurado; ele faz skip quando o serviço não está disponível.

Antes de alegar compatibilidade operacional, ainda é necessário separar evidências de Roblox Studio local, OBS real e uma LIVE TikTok real. Os números de carga sintética não são uma promessa de throughput de uma LIVE.

## Troubleshooting

| Sintoma | Causa provável | Como verificar | Como corrigir |
|---|---|---|---|
| `TIKTOK_UNIQUE_ID` obrigatório | modo live com TikTok habilitado | `liveengine check` | preencha o ID sem ou com `@`, ou use `RUN_MODE=simulation` |
| porta 8787 ocupada | outra API/processo local | `liveengine status` e `ss -ltnp` | pare o processo ou altere `LOCAL_API_PORT` |
| processo já em execução | PID file de uma instância ativa | `liveengine status` | use `liveengine stop`; remova PID somente se o processo não existir |
| OBS offline | `OBS_ENABLED` ou `FEATURE_OBS` habilitado sem OBS em 4455 | `liveengine check`, `OBS_PORT` | inicie OBS com obs-websocket ou desligue a feature |
| MQTT não conecta | broker ausente, host/porta/TLS incorretos | logs e `MQTT_BROKER_HOST` | inicie/configure o broker ou mantenha MQTT desabilitado |
| Roblox não recebe eventos | Studio sem HttpService, URL/porta errada ou buffer evicto | `GET /health`, logs e `gap_detected` | habilite HttpService, use a URL local correta e ajuste polling/buffer |
| regras não carregam | JSON inválido, schema incompatível ou action não allowlisted | `liveengine config validate` | corrija `configs/interaction_rules.json` |
| logs não aparecem | diretório sem permissão | `ls -ld logs` | corrija a permissão ou ajuste `AUDIT_LOG_DIR` |
| fila saturada | consumidor externo lento ou flood | `GET /health` e métricas | reduza ações, ajuste limites com cuidado e aceite drops de baixa prioridade |
| dependência ausente | venv não instalado ou pacote incompleto | `liveengine check` | recrie o venv e rode `pip install -e '.[dev]'` |

## Estrutura principal

```text
src/app.py                 composição e lifecycle
src/cli.py                 CLI oficial
src/config.py              Settings, flags e parsing centralizado
src/domain/                eventos, comandos e prioridades
src/ingestion/             TikTok, normalização e buffers
src/engine/                fila, dedupe, agregação e dispatcher
src/interaction/           regras, cooldown, dedupe, GameEvents e consumer
src/observability/         watchdog, métricas, auditoria e resiliência
src/adapters/              Roblox Local API, OBS e MQTT
configs/                   regras e mappings versionados
roblox/src/                BridgeClient e runtime Luau
context/                   contratos, decisões, falhas e operação
```

## Limitações atuais

A resolução TikTok → usuário Roblox continua explícita e não automática. Combos econômicos, ranking, autenticação pública, publicação cloud, SaaS, billing, instalador complexo e auto-update estão fora da v1. A experiência publicada do Roblox não pode alcançar a Local API do creator via `localhost`. O modo `replay` está reservado no contrato, mas não há gravador/reprodutor completo de eventos nesta fase.

Consulte `context/PROJECT_SPEC.md`, `context/ARCHITECTURE.md`, `context/DECISIONS.md`, `context/TEST_PLAN.md`, `context/RELIABILITY.md`, `context/FAILURE_MODES.md`, `context/OBSERVABILITY.md`, `context/INTERACTION_RULES.md`, `context/OBS_INTEGRATION.md` e `context/MQTT_INTEGRATION.md` para as decisões detalhadas.

## dashboard operacional (fase 12)

Com o processo em execução, abra [`http://127.0.0.1:8787/dashboard`](http://127.0.0.1:8787/dashboard). O painel usa o mesmo backend da ponte e mostra status real, atividade limitada, fila, latência quando disponível, Roblox, OBS, MQTT, regras read-only e logs bounded. Não há dados fictícios para preencher cards vazios.

O dashboard atualiza o snapshot em um único polling de 3 segundos. Regras e logs são endpoints de leitura separados. A arquitetura visual e as limitações estão em [`context/UI_ARCHITECTURE.md`](context/UI_ARCHITECTURE.md). O comando `liveengine start` continua sendo a forma oficial de subir a Local API.
