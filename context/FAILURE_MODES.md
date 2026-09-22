# Modos de Falha e Comportamento Esperado (Fase 10)

Esta matriz mapeia o que acontece com o sistema quando cada parte falha, simulando cenários do mundo real (saturação, queda de rede, crashes parciais).

## 1. Fonte de Dados (TikTok)

| Componente Falhando | Modo de Falha | Consequência | Ação de Mitigação | Perda de Dados? |
|---|---|---|---|---|
| TikTok API | Disconnect abrupto | `TikTokLiveConnector` lança erro | Reconecta com backoff exponencial (2s, 4s, 8s, 16s, 30s) | Sim (eventos na live perdidos durante downtime) |
| Conexão de Internet | Drop silencioso de pacotes | Heartbeat timeout no SDK TikTok | Watchdog flagra stall; Reconexão acionada | Sim |
| Chat do TikTok | Event Flood (>50/s) | O buffer interno satura | `BoundedEventBuffer` começa a descartar eventos mais antigos (`put_nowait=False`) | Sim (drops at-most-once) |

## 2. Processamento Principal (Event Engine)

| Componente Falhando | Modo de Falha | Consequência | Ação de Mitigação | Perda de Dados? |
|---|---|---|---|---|
| `PriorityQueueSet` | Saturação de capacidade | Overflow Threshold atingido | Descarta novos P4 (se fila >50%), P3 (>60%), P2 (>80%). P0 e P1 nunca descartam | Sim (baixa prioridade) |
| Worker Task | Exceção não tratada | Worker morre e respawna? Não. | O `Dispatcher` isola o erro e loga. A task não morre. O erro incrementa métricas | Não (o evento já foi retirado, pode gerar re-delivery at-least-once em caso específico, mas normalmente erro de consumer não para o worker) |
| `EventProcessor` | CPU Starvation (busy-wait) | Aumento da latência | `max_in_flight` limita a concorrência a 16 eventos paralelos | Não (apenas backpressure para o TikTok buffer) |

## 3. Consumidores / Integrações

| Componente Falhando | Modo de Falha | Consequência | Ação de Mitigação | Perda de Dados? |
|---|---|---|---|---|
| OBS Studio | WebSocket Offline | `OBSAdapter` não consegue conectar | Retry assíncrono. A fila do OBS acumula ações de cenas/fontes | Não, até a fila encher (max 100/level), depois dropa |
| MQTT Broker | Conexão recusada | `MQTTAdapter` falha em conectar | Backoff exponencial (2s a 30s). Comandos são enfileirados. | Não, até TTL (30s) expirar ou fila MQTT encher |
| Roblox Studio (Luau) | Para de fazer HTTP Polling | O buffer do `RobloxBridge` começa a encher | O buffer (deque maxlen=500) começa a evictar (FIFO) o mais antigo | Sim (eventos não consumidos são perdidos após 500 itens) |
| Consumidor A (ex: MQTT) | Exceção por payload inválido | `MQTTAdapter` falha no envio | Dispatcher garante que a falha de um consumer não propaga. Roblox continua recebendo a mensagem | Não (apenas o destino MQTT falha) |

## 4. Lifecycle (Início e Fim)

| Componente Falhando | Modo de Falha | Consequência | Ação de Mitigação | Perda de Dados? |
|---|---|---|---|---|
| Processo Principal | SIGTERM (Ctrl+C) recebido enquanto processa | Interrupção | `stop()` graceful: ignora novos, tem 5s para drenar P0, aguarda cancelamento limpo | Sim (no buffer TikTok, mas P0 na engine garantido) |
| Processo Principal | SIGKILL (Crash) abrupto | Desligamento imediato | Nada pode ser feito no shutdown. | Sim (todos os buffers/memória perdidos) |
| Inicialização pós-crash | O sistema acorda; há duplicates? | Sim (TikTok pode re-enviar eventos antigos da mesma live) | `DeduplicationCache` inicia limpo, mas expiração com event_id protege contra re-processamento por novos eventos nos primeiros 30s da live | Aceitável (cooldowns controlam o abuso) |

---
*Gerado durante a Fase 10 (Hardening e Chaos Testing).*
