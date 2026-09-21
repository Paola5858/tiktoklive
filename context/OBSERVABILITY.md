# Observabilidade e Controle Operacional

Este documento detalha o sistema de observabilidade e controle operacional do engine TikTok × Roblox, implementado na Fase 7.

## Arquitetura de Observabilidade

A estratégia de observabilidade foca em evitar falhas silenciosas e garantir que o estado do sistema possa ser interpretado em tempo quase real, sem adicionar overhead significativo que prejudicaria o processamento `asyncio`.

O sistema foi desenhado para expor um **snapshot operacional** unificado na Local API (`GET /health`), pronto para ser consumido por um futuro Dashboard de Operações, e exportar logs de auditoria em arquivo para troubleshooting a posteriori.

## Componentes

### 1. Watchdog (`src/observability/health.py`)
O Watchdog atua como monitor de vitalidade dos componentes essenciais.
- **Registro:** Componentes como `TikTokLiveConnector`, `EventProcessor` e `RobloxBridge` se registram e ganham uma referência `ComponentHealth`.
- **Pings:** Cada componente chama `.mark_active()`, `.record_success()` ou `.record_failure()` no fluxo de suas operações normais.
- **Detecção de Stall:** Quando `watchdog.check()` é invocado, ele avalia há quanto tempo cada componente enviou um ping. Se o tempo exceder o limite (ex: 30 segundos), o componente é marcado como `DEGRADED`, o que muda o status global para `unhealthy`.

### 2. Operational Snapshot (`src/observability/metrics.py`)
Agregador responsável por consolidar:
- **Estado do Watchdog** (qual componente está travado, há quanto tempo).
- **Métricas do Engine** (throughput, profundidade das filas, quedas por overflow).
- **Latência Percentil** (p50, p95 end-to-end, tempo de fila, tempo de processamento).

### 3. Audit Logging (`src/observability/audit.py`)
Eventos cruciais precisam deixar rastro sem prejudicar o I/O.
- Implementado sobre um `asyncio.Queue` de limite rígido e com uma Task assíncrona dedicada à gravação.
- Exporta os rastros em arquivos JSON Lines rotacionados diariamente (`logs/events/YYYY-MM-DD.jsonl`).
- Os payloads focam em `event_id`, tipo, latência final e status (`processed` ou `failed`), evitando duplicar os dados transacionais pesados que já foram enviados ao Roblox.

### 4. Logging Estruturado (JSON)
Substituição do `logging` padrão para arquivos, através da classe `JsonFormatter`. Permite consumo via ferramentas externas sem parsing complexo.

## Contrato de Dados (`GET /health`)

A chamada `GET /health` do bridge retorna o seguinte formato de payload:

```json
{
  "status": "healthy",
  "bridge": {
    "schema_version": "1.0",
    "buffer_depth": 15,
    "buffer_capacity": 500,
    "events_delivered_total": 1204,
    "events_evicted_total": 0,
    "last_event_at": "2026-09-21T19:40:00Z",
    "last_poll_at": "2026-09-21T19:40:02Z",
    "last_acknowledged_sequence": 1200,
    "highest_sequence_ever": 1204
  },
  "system": {
    "status": "healthy",
    "health": {
      "EventProcessor": {
        "state": "READY",
        "status": "healthy",
        "last_success_age_ms": 120.0,
        "last_failure_age_ms": null,
        "last_activity_age_ms": 120.0,
        "failure_reason": null
      }
    },
    "queue": {
      "depth_total": 0,
      "depth_by_priority": {"0": 0, "1": 0, "2": 0, "3": 0, "4": 0},
      "dropped": 0,
      "overflow": 0
    },
    "throughput": {
      "received": 1250,
      "accepted": 1204,
      "processed": 1204,
      "dispatched": 1204,
      "failed": 0
    },
    "latency": {
      "end_to_end": {
        "count": 1204,
        "avg_ms": 15.2,
        "p50_ms": 12.0,
        "p95_ms": 25.4
      }
    }
  }
}
```

## Como o Sistema reage a Falhas?

1. **Worker Lento/Travado:** O `EventProcessor` faz pings no loop principal. Se uma task travar dentro de um dispatcher por culpa de I/O mal comportado, o ping cessa. O Watchdog detecta após `N` segundos e muda status para `unhealthy`.
2. **Queda de Conexão com TikTok:** O conector reporta a falha e entra em backoff exponencial. O Watchdog indica `DISCONNECTED`.
3. **Roblox Bridge Cheio:** Fica registrado no total de `events_evicted_total` dentro da propriedade `bridge`.
4. **Roblox Fora do Ar:** Se o jogo no Roblox para de fazer pooling, o `last_activity_age_ms` do `RobloxBridgeConsumer` subirá, disparando a condição `unhealthy` no Watchdog.
