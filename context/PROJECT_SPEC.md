# PROJECT_SPEC.md — TikTok × Roblox Live Engine

## status do documento

Especificação inicial registrada a partir do contexto mestre. O repositório, no momento da análise, continha apenas `LICENSE` e um commit inicial — sem código, dependências, testes, configurações ou integrações verificadas.

**Estado:** proposta arquitetural inicial, ainda não implementada.

---

## o que é

Uma event engine: pega eventos de uma live do TikTok, normaliza, prioriza, filtra e manda pro Roblox como comandos de jogo abstratos. O Roblox nunca sabe que existe TikTok — ele só recebe `SPAWN_AVATAR`, `APPLY_EFFECT`, etc.

Não é "um bot que spawna avatar quando alguém manda presente". É a camada que decide *o que* deve virar evento, *com que prioridade*, e *o que descartar* quando a live está bombando e 500 comentários chegam por segundo.

---

## objetivo da fase atual (v1, local)

Ferramenta local, rodando na máquina do creator, para desenvolvimento e uso controlado. **Não é** uma versão cloud/multi-tenant ainda — isso é visão de longo prazo, não trabalho de agora.

A primeira entrega deve provar, nesta ordem:

1. Recepção de um evento real ou de um adaptador experimental de fonte.
2. Normalização para o evento interno versionado.
3. Classificação de prioridade, deduplicação e aplicação de limites.
4. Enfileiramento com capacidade limitada e política explícita de descarte.
5. Exposição segura de eventos processados via API local.
6. Consumo pelo Roblox Studio via bridge documentado (HttpService).
7. Registro JSONL e métricas mínimas para depuração.

---

## fora de escopo da v1

Não implementar agora, mas não travar o caminho:

- multi-tenant / multi-creator
- billing, licenciamento, control plane na nuvem
- autenticação de múltiplos usuários
- dashboard web público
- disponibilidade 24/7, segurança de rede pública, escalabilidade ilimitada
- suporte a todas as modalidades de LIVE
- transformar cada comentário em entidade persistente no jogo

---

## por que isso importa

O risco real não é "não vai ter função X". É **arquitetura errada na v1 forçar reescrita inteira depois**. Por isso o `EVENT_ENGINE` traduz tudo pra um formato interno — trocar TikTok por outra plataforma no futuro não deveria tocar no Roblox nem na fila.

---

## critérios de aceitação arquitetural

A fundação será considerada pronta para implementação quando:

- o contrato do evento interno estiver versionado e validável;
- as fronteiras entre source, engine, queue, API e Roblox estiverem documentadas;
- a estratégia de prioridade, deduplicação, agregação e overflow estiver definida;
- as hipóteses dependentes de APIs externas estiverem listadas como experimentos;
- existirem testes para eventos inválidos, duplicados, flood, desconexão e consumidor lento;
- nenhum segredo ou dependência específica tiver sido inventado;
- o fluxo local puder ser executado com uma fonte determinística de teste.

---

## riscos que precisam ser verificados antes de codar sério

| # | Risco | O que precisa ser testado |
|---|---|---|
| 1 | **localhost em Roblox** | Em Studio (Play Solo/Team Test) o server roda localmente — `localhost` deve funcionar. Em jogo publicado, o server roda na infra da Roblox — `localhost` **não** aponta pra sua máquina. Testar experimentalmente antes de virar premissa (ver TEST_PLAN.md, fase 3). |
| 2 | **limite de requests do HttpService** | Documentação oficial cita 500 req/min por server. Isso vira teto real pro polling — precisa entrar no dimensionamento da fila e do polling interval. |
| 3 | **biblioteca de conexão com TikTok Live** | Não assumir nenhuma API/método sem checar documentação/código da lib escolhida. Comportamento de reconexão, rate limit e formato de evento variam por biblioteca e versão. |
| 4 | **throughput real de uma live** | Os "500 eventos/s" são hipotéticos. Dimensionamento de fila/dedupe precisa ser validado com dados reais, não só teoria. |
| 5 | **Roblox Studio ↔ Local API** | Não há garantia de que HttpService aceita todos os headers/métodos de uma API REST comum — validar na fase 3 antes de fechar o contrato final. |
| 6 | **resolução de identidade TikTok → Roblox** | Como (e se) o sistema associa uma conta do TikTok a um usuário/avatar do Roblox. Sem isso definido, o campo `user_id` do evento pós-engine fica sem contrato real. |

---

## hipóteses que ainda precisam de evidência

| Hipótese | Evidência exigida |
|---|---|
| A biblioteca escolhida consegue receber os eventos necessários do TikTok | documentação oficial, protótipo e teste de conexão |
| O ambiente Roblox consegue alcançar a API local | teste no Studio e teste publicado, separadamente |
| O mecanismo escolhido pelo bridge atende a frequência de eventos | experimento com latência, erros e limites |
| O Roblox consegue resolver e carregar a identidade/avatar no fluxo esperado | protótipo com usuário válido e inválido |
| A integração OBS possui a API e o modelo de autenticação esperado | documentação e teste local |
| O volume de eventos de uma LIVE cabe na política inicial | teste de carga com dados sintéticos e métricas |

---

## stack proposta (a confirmar antes de fixar)

- **ingestão TikTok**: Python + biblioteca de TikTok Live (a escolher e validar — não assumir métodos sem checar)
- **event engine / fila / API local**: Python + asyncio, FastAPI — Flask fica de lado se não tivermos necessidade de WebSocket/async nativo
- **Roblox bridge**: Luau, HttpService fazendo polling (ou long-polling) na API local
- **OBS**: obs-websocket (a validar versão/protocolo)
- **persistência de eventos**: arquivo JSONL local, sem banco de dados na v1

Nenhum desses itens é definitivo — cada um vira uma entrada em `DECISIONS.md` quando for fixado de verdade.

---

## princípios de produto

A confiabilidade do fluxo vale mais do que efeitos chamativos. O sistema deve falhar de modo visível, limitado e recuperável. Interfaces e mecânicas devem nascer do domínio de live interativa, não de um template genérico. Toda decisão importante precisa ser registrada e toda afirmação operacional precisa ser sustentada por teste ou documentação.
