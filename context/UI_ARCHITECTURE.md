# UI_ARCHITECTURE.md — dashboard operacional

## Objetivo

A fase 12 adiciona uma superfície operacional local para responder rapidamente: o processo está funcionando, onde está a pressão e qual integração está falhando? A interface é servida pela própria Local API em `/dashboard`. Não há frontend separado, CDN, login ou serviço cloud.

## Arquitetura

```text
FastAPI /dashboard
  ├── index.html
  ├── assets/styles.css
  └── assets/app.js

FastAPI /api/dashboard/snapshot
  ├── OperationalSnapshot.get_snapshot()
  ├── RobloxBridge.health_snapshot()
  ├── TikTokLiveConnector.state + ConnectorMetrics
  ├── OBSAdapter.health_snapshot()
  └── MQTTAdapter state, queue, metrics e last_seen

FastAPI /api/dashboard/rules  -> configs/interaction_rules.json validado
FastAPI /api/dashboard/logs   -> logs/events/*.jsonl, leitura limitada e redaction
```

A aquisição de dados fica no backend. O browser não calcula health, throughput crítico ou latência. Ele apenas formata o snapshot fornecido. Isso evita uma segunda fonte de verdade e mantém a diferença entre configured, connected, healthy, degraded, disabled e desconhecido.

## Áreas da tela

A visão geral mostra status do sistema, total recebido nesta execução, profundidade da fila e latência p95 quando o backend possui amostra. A atividade mostra no máximo 50 envelopes recentes do `RobloxBridge`, com filtro por tipo e uma leitura de correlação para `event_id`, sequência, origem, usuário e payload.

A área de integrações separa TikTok, Roblox Bridge, OBS e MQTT. O painel Roblox mostra polling, buffer, entregas e evictions. O painel OBS mostra conexão, cena quando disponível, fila, falhas e latência. O painel MQTT distingue broker de dispositivos vistos via heartbeat; credenciais não entram no payload.

Regras são read-only. A tela não inventa editor de DSL, contagem de triggers ou last triggered, porque o contrato atual não persiste esses dados. Ela mostra id, enabled, prioridade, matcher implícito e ações carregadas do JSON validado.

Logs usam os JSONL de auditoria, no máximo 200 linhas por resposta, com filtro de nível e busca. Campos que contêm `password`, `token`, `secret`, `api_key` ou `apikey` são redacted na borda.

## Atualização em tempo real

A UI usa **um único polling centralizado de 3 segundos** para `/api/dashboard/snapshot`. Não usa WebSocket ou SSE porque a Local API existente já trabalha com polling e a necessidade atual é baixa. Requisições anteriores são abortadas antes de uma nova atualização. Regras e logs carregam sob demanda inicial ou ação explícita; não possuem timers próprios.

O endpoint é sem estado do browser e não mantém histórico infinito. O cliente conserva somente a janela recebida pelo backend e o DOM limita a lista visual. Em uma próxima fase, se eventos reais exigirem maior taxa, a escolha deve ser reavaliada com medição, não com estética de dashboard moderno.

## Sistema visual

Tokens estão centralizados em `src/dashboard/styles.css`: fundo `#0b0d12`, superfícies em camadas, bordas discretas, raio principal de 14px, tipografia de interface sem serifa e títulos editoriais em Georgia. Verde-lima indica saúde/prontidão, amarelo indica transição ou atenção, vermelho indica falha, azul e violeta distinguem categorias sem transformar tudo em neon.

A densidade é alta, mas a hierarquia prioriza status antes de métricas secundárias. Glassmorphism é usado somente no rail fixo; os painéis usam superfície opaca com borda leve. Movimento se limita a transições de atualização e respeita `prefers-reduced-motion`.

## Acessibilidade e responsividade

A estrutura usa `nav`, `main`, `section`, `article`, `aside`, headings e labels. Linhas de eventos têm foco de teclado e respondem a Enter/Espaço. Estado não depende só de cor: os pills também exibem texto. Conteúdo dinâmico entra via `textContent`, não HTML arbitrário, para impedir XSS de comentários, usuários e logs.

Desktop é o alvo operacional. Em tablet, a grade cai para duas colunas; em mobile, para uma coluna e a navegação vira faixa horizontal. Tabelas reduzem colunas secundárias em telas estreitas sem remover o fluxo principal.

## Estados e honestidade dos dados

Sem resposta da API, a tela mostra “Sem conexão” e não troca o estado por zero. Sem eventos, mostra “Nenhum evento ainda”. OBS/MQTT desligados aparecem como desabilitados, não como offline. Roblox sem `last_poll_at` aparece sem confirmação, não conectado. Latência sem amostra aparece como `–`.

A interface não consegue afirmar que o Roblox Studio está ativo internamente; só consegue observar poll, ack e buffer no Python. Também não afirma que OBS executou um comando apenas por enfileirá-lo. A confirmação permanece limitada ao que o adapter reporta.

## Performance e segurança

O backend limita eventos a 50 e logs a 200. O browser não acumula páginas, subscriptions ou timers órfãos. Não há biblioteca de gráficos pesada: a fila por prioridade usa barras CSS simples, pois a necessidade é perceber pressão, não produzir analytics histórico.

A API de dashboard é local e continua sem autenticação, seguindo a decisão da v1. Expor a porta por túnel muda o risco e exige autenticação futura. O dashboard não mostra senha, token, host secreto ou payload mais amplo que o necessário para investigação.

## Limitações

Não existe histórico de triggers de regra, atividade de avatar, firmware, grupos de dispositivo, comandos manuais ou autenticação. O arquivo JSONL de auditoria contém apenas eventos registrados pelo backend e pode estar vazio. O browser não valida Roblox Studio real, OBS real, MQTT real ou uma LIVE real: esses experimentos continuam separados no `TEST_PLAN.md`.
