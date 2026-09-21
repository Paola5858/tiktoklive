# ROBLOX_BRIDGE.md — a ponte Python ↔ Roblox

## visão geral

```
Event Engine (Dispatcher)
   ↓ handle(event)
RobloxBridge (src/adapters/roblox.py)
   — traduz Event/AggregatedEvent em GameEventEnvelope
   — guarda num buffer bounded (deque com maxlen), com sequence_number
   ↓
Local API (src/adapters/local_api.py, FastAPI)
   — GET  /health
   — GET  /events?since=<cursor>&limit=<n>
   — POST /ack {"up_to_sequence": n}
   ↓ HTTP (polling)
BridgeClient.lua (roblox/src/BridgeClient.lua)
   — Script server-side no Roblox
   — polling com backoff, dedupe, cursor
   ↓
Game Event Router (dentro do próprio BridgeClient.lua, placeholder)
   — handlers por event_type — gameplay real é fase 5
```

`RobloxBridge` é ao mesmo tempo um `EventConsumer` (registrado no `Dispatcher`
do engine) e a fonte de dados da Local API — a mesma instância faz as duas
pontas, pra não duplicar estado.

---

## a descoberta crítica desta fase: localhost e HttpService

A fase 1 e a fase 3 deixaram isso como `EM ABERTO`. Esta fase verificou
contra a documentação oficial atual da Roblox (`create.roblox.com/docs`,
consultada em 2026) em vez de assumir:

- **Em Studio (Play Solo / Team Test)**: o servidor da experiência roda na
  própria máquina de quem está testando. A documentação oficial de
  `HttpService` traz, no momento desta pesquisa, um exemplo de código
  (`CreateWebStreamClient`) conectando literalmente a
  `http://localhost:11434` para falar com um servidor Ollama rodando na
  mesma máquina — ou seja, `localhost`/`127.0.0.1` **pode** funcionar em
  Studio. Há também relatos antigos no fórum de desenvolvedores (2020-2024)
  de erros como `Trust check failed` ou `HttpService is not allowed to
  access ROBLOX resources` ao tentar `127.0.0.1` sem protocolo — o que
  sugere que a forma exata da URL (`http://localhost:PORTA` vs `127.0.0.1`
  puro) importa, e que o comportamento pode ter mudado entre versões do
  engine.
- **Em experiência publicada**: o servidor roda na infraestrutura da
  Roblox, uma máquina física diferente da do creator. `localhost` ali
  nunca vai apontar pro computador do creator — isso não é uma limitação
  de configuração, é a definição de "localhost".
- **Rate limit confirmado nos docs atuais**: 500 requisições HTTP por
  minuto por server do Roblox (fora do escopo do Open Cloud, que tem seu
  próprio limite de 2500/min). Ultrapassar isso pode travar o envio de
  requisições por ~30 segundos.
- **Restrição de porta confirmada**: portas abaixo de 1024 são bloqueadas,
  exceto 80 e 443.

### o que isso significa pra arquitetura

- `BASE_URL = "http://localhost:8787"` é o **default de desenvolvimento em
  Studio**, não uma arquitetura de produção. Isso já estava certo na
  intuição da fase 1 ("a primeira versão pode ser uma ferramenta local"),
  mas agora está confirmado por documentação, não por suposição.
- **Nenhum teste manual real em Studio foi feito por este agente** — ver
  `TEST_PLAN.md`, `manual_studio_test`. O que está documentado aqui vem da
  documentação oficial da Roblox, não de execução verificada. Antes de
  confiar nisso pra qualquer decisão de produto, rode o teste manual.
- Para qualquer coisa além de Studio local (incluindo só *compartilhar* o
  teste com outra pessoa), a Local API precisa estar exposta num endereço
  público em HTTPS — um túnel de desenvolvimento (ngrok, Cloudflare Tunnel,
  etc.) resolve isso sem exigir infraestrutura cloud própria. Isso não é
  "produção" — é o mínimo pra sair de "só funciona na minha máquina, talvez".

---

## semântica de entrega

**At-least-once, com idempotência no consumidor.** Não prometemos
exactly-once — polling, retry e a ausência de um mecanismo de lease fariam
essa promessa falsa (ver `delivery_semantics` na spec da fase 4: "não deve
ser prometido sem uma implementação e prova que realmente sustentem isso").

Como isso se sustenta na prática:
- O `RobloxBridge` nunca regenera `event_id` — o mesmo evento sempre chega
  com a mesma identidade, não importa quantas vezes apareça numa resposta.
- `GET /events` **nunca remove nada do buffer** — só overflow por
  capacidade (`buffer_capacity`, FIFO) descarta eventos, nunca uma leitura.
- O lado Roblox mantém um cache de dedupe bounded (`DEDUP_CACHE_CAPACITY`
  entradas, `CONFIG` em `BridgeClient.lua`) e ignora `event_id` repetido.
- `POST /ack` é **observabilidade, não controle de fluxo**: ele avança
  `last_acknowledged_sequence` no lado Python só pra dar visibilidade de
  quanto o Roblox está atrasado. Nada é apagado por causa de um ack.

## cursor e detecção de gap

- `sequence_number` é um inteiro monotônico atribuído pelo `RobloxBridge`
  na hora que um evento chega (não é o `event_id`).
- O Roblox manda `since=<último sequence_number visto>` a cada poll.
- Se `since` for anterior ao mais antigo ainda disponível no buffer
  (porque eventos foram descartados por overflow antes de serem lidos), a
  resposta traz `gap_detected: true`. O `BridgeClient.lua` atual só loga
  um aviso quando isso acontece — decidir o que fazer com a lacuna
  (pular, pedir replay, etc.) é uma decisão de produto pra quando houver
  dado real de quão frequente isso é.

## por que buffer em memória, não fila persistente

Único consumidor (um Roblox Studio local), sem requisito de sobreviver a
um restart do processo Python ainda. Persistência de eventos pendentes
entraria como complexidade real quando existir um motivo real (`anti_overengineering`
da fase 4: nada de banco/fila externa "porque parece mais robusto").

## rate limits e polling

- `POLL_INTERVAL_SECONDS = 2` no `BridgeClient.lua` é baseline, não número
  mágico — com esse intervalo, um único servidor do Roblox faz ~30
  requisições/minuto pra `/events`, bem abaixo do limite de 500/min
  documentado. Isso dá margem enorme pra somar `/ack` (mais 30/min) e
  ainda sobrar espaço, mas o valor não foi calibrado com uma live real.
- Backoff em falha usa a mesma sequência documentada em `DECISIONS.md`
  (2, 4, 8, 16, 30s, patamar em 30s) — resetado após uma resposta com sucesso.
- `EVENTS_PER_POLL = 25` e o teto do servidor `max_events_per_poll = 100`
  (ver `RobloxBridgeConfig`) evitam que uma única resposta fique gigante.

## contrato do envelope (schema_version 1.0)

```json
{
  "schema_version": "1.0",
  "sequence_number": 42,
  "event_id": "uuid",
  "event_type": "COMMENT | GIFT | FOLLOW | SHARE | LIKE | SYSTEM | MANUAL | CUSTOM | AGGREGATED",
  "timestamp": "ISO-8601 UTC",
  "priority": 0,
  "payload": { "...": "específico do event_type" },
  "source": "tiktok",
  "user": { "display_name": "...", "external_id": "..." }
}
```

Isto **não é** o `Command`/`CommandType` de `src/domain/commands.py`
(`SPAWN_AVATAR`, `APPLY_EFFECT`, etc.) — aquele é o contrato de comando de
jogo já mapeado, que só existe depois que o Gift Mapping Engine (fase 5)
decidir a tradução. O envelope desta fase é o transporte genérico de
qualquer evento processado. Rejeição segura de versão: o `BridgeClient.lua`
compara `schema_version` e recusa interpretar uma resposta com versão
diferente da esperada, em vez de tentar adivinhar campos.

## segurança desta fase

- Sem autenticação — decisão explícita da fase 4 (`authentication.initial_version`):
  isto é uma ferramenta de creator local, não um serviço multi-usuário.
- `/health` não aceita nem expõe payload de evento (herda a regra da fase 1).
- `/events` e `/ack` validam todo input via Pydantic (FastAPI): `since`/`limit`
  têm bounds explícitos (`limit` entre 1 e 200), `up_to_sequence` não aceita
  negativo. Nada é interpretado como código — payload é sempre dado.
- `localhost não é mecanismo de segurança` (ver `security.localhost_warning`
  na spec): se a API sair de localhost via túnel, ela fica exposta pra
  qualquer um que descubra a URL. Autenticação real é trabalho de fase
  futura, não desta.

## limitações conhecidas

- `HttpService:RequestAsync` **não tem um campo de timeout configurável**
  na sua tabela de opções (confirmado na documentação oficial — os campos
  são `Url`, `Method`, `Headers`, `Body`, `Compress`). Não implementamos
  um timeout customizado do lado Luau porque isso exigiria inventar um
  mecanismo (ex: cancelar via outra coroutine) que a spec pede pra não
  fazer sem necessidade comprovada — o comportamento de timeout de rede
  fica a cargo do runtime da Roblox.
- Nenhum teste manual real em Roblox Studio foi executado — ver seção
  acima e `TEST_PLAN.md`.
- `Event.priority` não existe como atributo do `Event` do domínio (a
  prioridade é resolvida no `EventProcessor` e não persiste no objeto) —
  `to_envelope()` resolve o baseline de novo via
  `DEFAULT_PRIORITY_BY_EVENT_TYPE` quando falta. Isso é consistente com o
  resto do sistema, mas significa que uma prioridade "fina" (por gift
  específico, não por tipo) só vai aparecer no envelope quando o Gift
  Mapping Engine existir e a resolução de prioridade for estendida.
- O aviso de depreciação `Using httpx with starlette.testclient is
  deprecated; install httpx2 instead` aparece nos testes (`httpx 0.28.1`,
  Starlette recente). Não migramos pra `httpx2` nesta fase — é um pacote
  novo (2026) e a suíte de testes funciona normalmente com o aviso; vale
  reavaliar quando `httpx2` estiver mais maduro.

## como testar sem Roblox

`src/tests/unit/test_roblox_bridge.py` e `test_local_api.py` cobrem tudo
isso sem nenhuma rede real: tradução de envelope, buffer/cursor/eviction,
gap detection, ack, e os endpoints via `fastapi.testclient.TestClient`
(que fala com a app ASGI em memória, sem subir um servidor de verdade).

## como testar manualmente com Roblox Studio (quando possível)

1. `python -m src.adapters.local_api` sobe a Local API sozinha em
   `127.0.0.1:8787` com um bridge vazio.
2. Alimentar eventos manualmente (ex: num REPL, `await bridge.handle(evento)`)
   ou aguardar a integração completa com o TikTok connector (ainda não
   conectada nesta fase — ver "o que ainda não funciona" abaixo).
3. Em Roblox Studio: File > Experience Settings > Security > habilitar
   "Allow HTTP Requests".
4. Colocar `BridgeClient.lua` em `ServerScriptService`, chamar
   `BridgeClient.start()` a partir de um Script separado (ou direto no
   fim do módulo, se preferir um Script em vez de ModuleScript).
5. Play Solo e observar o output: deve aparecer log de polling e, se
   houver eventos no buffer, o handler de exemplo imprimindo.
6. Derrubar a Local API (Ctrl+C) e observar o backoff nos logs do Studio;
   subir de novo e confirmar que o polling volta ao intervalo normal.

## o que ainda não funciona (e não deveria ser apresentado como funcionando)

- **Publicado**: nada aqui foi validado fora de Studio. Ver seção sobre
  localhost acima.
- **Pipeline completo**: `RobloxBridge` ainda não está fisicamente
  conectado ao `TikTokLiveConnector`/`EventProcessor` num processo único —
  isso é composição (`app.py`), que continua em aberto (ver `DECISIONS.md`).
  Esta fase entrega o bridge e a API; ligar tudo end-to-end é o próximo passo.
- **Gift Mapping Engine / avatar**: fora de escopo desta fase por decisão
  explícita (`avatar_scope`). O `GameEventRouter` em `BridgeClient.lua` é
  só o esqueleto — os handlers reais de gameplay são fase 5.
