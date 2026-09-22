# Resiliência e Confiabilidade (Fase 10)

## Princípios de Resiliência

1. **Prioridade Sob Saturação**: O sistema protege P0 (System) e P1 (Gift) sob qualquer custo. Funções de baixo impacto (Like/Follow - P4) são sacrificadas sob pressão, descartadas antes mesmo de enfileirar na engine.
2. **Graceful Degradation**: Falha em um sistema externo (MQTT, OBS) não trava o loop principal nem prejudica o Roblox. Os componentes degradam independentemente.
3. **Estado Efêmero**: Para manter a simplicidade e a recuperação rápida, caches e buffers de mensagens vivem apenas em memória. Um crash resulta na perda desses dados, mas permite inicialização atômica instantânea, coerente com o escopo de "scripts para streamers", sem necessidade de manter bancos de dados complexos em produção local.
4. **Isolamento via Dispatcher**: Nenhuma exceção lançada por um consumidor (`OBSAdapter`, `RobloxBridge`, `MQTTAdapter`) destrói a task de roteamento do evento ou prejudica outros consumidores do mesmo evento.
5. **Backpressure**: Limitadores estritos evitam Out-Of-Memory (OOM).

## Arquitetura de Retry e Backoff

As integrações de saída (`OBS`, `MQTT`) utilizam uma mesma filosofia de reconnect:
* A thread principal não é bloqueada tentando conexão.
* Reconnect é feito com **Backoff Exponencial Capped** (ex: 2s, 4s, 8s, 16s, máximo de 30s).
* Mensagens geradas enquanto a integração está fora ficam guardadas nas respectivas **Filas Internas (Priority Queue)**.
* Mensagens pendentes têm restrições de **Tamanho da Fila** e **Expiração (TTL)** (ex: comandos MQTT expiram após 30s).

## Drop Policy (Overflow)

O `PriorityQueueSet` do Engine descarta novos eventos baseando-se na lotação atual do buffer (`depth`):
* >50% lotado: descarta novos `P4` (Likes)
* >60% lotado: descarta novos `P3` (Comentários)
* >80% lotado: descarta novos `P2` (Shares/Emotes especiais)
* P0 e P1 nunca são rejeitados ativamente por saturação de prioridade.

## Por que NÃO usamos Circuit Breakers?
Após análise detalhada do `RobloxBridge`, `MQTT` e `OBS`, optou-se por NÃO implementar Circuit Breakers:
* **Bounded Retries e Timeouts**: Os componentes de saída já expiram requests. O OBS usa `request_timeout_s=5s`.
* **Bounded Queues**: Quando o MQTT sai do ar, a fila satura localmente em 100 mensagens/nível e descarta o resto. O consumo da Engine não é bloqueado.
* **Polling Isolado**: O Roblox usa HTTP Polling. A engine empilha as mensagens; se o Roblox sumir, o buffer da bridge apaga os mais velhos (eviction) silenciando a falha passivamente.
* Acrescentar a máquina de estados do Circuit Breaker traria complexidade de manutenção desnecessária num cenário de loop local (localhost).

## Watchdog
Um `Watchdog` background monitora a atividade das integrações. Caso uma conexão pare de registrar sucessos sem emitir eventos de erro visíveis no loop (por exemplo, pacote UDP engolido silenciosamente), o watchdog marca como `DEGRADED`, acendendo luzes no painel de observabilidade local.

---
Para a matriz de análise de riscos item-a-item, veja [FAILURE_MODES.md](./FAILURE_MODES.md).
