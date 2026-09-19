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

## EM ABERTO — FastAPI vs Flask pra API local

Tendência: FastAPI, por causa de async nativo (bate melhor com asyncio do resto do engine) e validação de payload via Pydantic (ajuda direto em `local_api_security`). Decisão final só depois de confirmar que não tem atrito com a lib de ingestão escolhida.

## EM ABERTO — onde mora a Gift Mapping Engine

Ver `ARCHITECTURE.md`, seção "decisão pendente". Impacta workflow do creator, não só arquitetura. Trade-off: Python-side mantém o Roblox mais burro e o mapping mais fácil de versionar/testar fora do Studio; Luau-side evita republish de config toda vez que muda um efeito visual.

## EM ABERTO — resolução de identidade TikTok → Roblox

Como (e se) o sistema associa uma conta do TikTok a um usuário/avatar do Roblox. Sem isso definido, o campo `user_id` do evento pós-engine fica sem contrato real.

## EM ABERTO — localhost em Roblox Studio vs jogo publicado

Risco #1 do `PROJECT_SPEC.md`. Decisão de arquitetura (ex: "v1 é Studio-only" vs "v1 precisa de túnel/proxy público") depende do resultado do teste experimental (`TEST_PLAN.md`, fase 3). Não tratar como resolvido até validar. Studio e publicado podem ter restrições diferentes — evidência necessária: experimento nos dois ambientes.

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

## decisões que não devem ser tomadas por suposição

Não assumir que uma biblioteca possui um método, que localhost é acessível em produção, que um evento externo é idempotente, que gifts têm nomes estáveis, que o Roblox tolera qualquer frequência de request ou que logs com usernames são inofensivos. Cada item precisa de evidência reproduzível.
