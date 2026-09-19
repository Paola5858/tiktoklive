# DECISIONS.md — decisões arquiteturais

## como ler este arquivo

Decisões marcadas como **EM ABERTO** ainda não foram implementadas nem validadas em execução. Decisões marcadas como **DECIDIDO** foram tomadas e justificadas. Nada aqui é "provavelmente vamos fazer assim" — se ainda não foi decidido, fica em aberto, não vira fato.

---

## EM ABERTO — biblioteca de conexão com TikTok Live

Precisa ser escolhida e testada antes de qualquer código de ingestão. Critério: manutenção ativa, suporte a reconexão, documentação de quais eventos ela emite. Não assumir formato de evento sem ler o código-fonte da lib escolhida. API e estabilidade não verificadas — evidência necessária: documentação, licença, protótipo.

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

## decisões que não devem ser tomadas por suposição

Não assumir que uma biblioteca possui um método, que localhost é acessível em produção, que um evento externo é idempotente, que gifts têm nomes estáveis, que o Roblox tolera qualquer frequência de request ou que logs com usernames são inofensivos. Cada item precisa de evidência reproduzível.
