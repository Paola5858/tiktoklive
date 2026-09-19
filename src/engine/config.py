"""Configuração do Event Engine.

Todos os parâmetros configuráveis do engine vivem aqui com defaults documentados.
Nenhum valor mágico espalhado pelos módulos internos.

Defaults baseados em análise conservadora para uma live de médio porte
(estimativa hipotética — calibrar com dados reais, ver TEST_PLAN.md):
- ~60 eventos/s de pico
- consumidor Roblox com ~500 req/min (polling a cada 2s)
- 4 workers async são suficientes para o throughput esperado localmente

Os valores de threshold de overflow determinam a fração da capacidade total
(somando todas as filas) acima da qual cada prioridade começa a ser descartada.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """Parâmetros configuráveis do Event Engine.

    Attributes:
        queue_capacity_per_level: Capacidade máxima de cada fila de prioridade.
            Total máximo de eventos em fila = queue_capacity_per_level * 5.
        p0_express_capacity: Capacidade extra da fila P0 para eventos SYSTEM
            críticos (live_ended, shutdown). Esses eventos têm um "express lane"
            que ignora os thresholds de overflow normais, mas tem seu próprio
            limite para não crescer indefinidamente.
        n_workers: Número de workers async no pool. Valor baixo é seguro —
            o overhead de coordenação cresce com workers demais para I/O simples.
        max_in_flight: Máximo de eventos sendo processados simultaneamente
            (inclui dispatch para consumers). Controla backpressure entre
            a fila e os workers.
        dedup_maxsize: Tamanho máximo do cache de deduplicação (LRU).
        dedup_ttl_seconds: TTL de cada entrada no cache de deduplicação.
            Após esse tempo, um evento com a mesma chave é considerado novo.
        aggregation_window_seconds: Janela temporal de agregação.
            Eventos P4 e floods de comentário são agrupados nessa janela.
        aggregation_max_bucket_size: Máximo de eventos agrupados em um único
            AggregatedEvent antes de um flush forçado.
        overflow_threshold_p1: Fração da capacidade total acima da qual P1
            pode ser descartado. 0.0 = nunca descarta; 1.0 = sempre descarta.
        overflow_threshold_p2: Idem para P2.
        overflow_threshold_p3: Idem para P3.
        overflow_threshold_p4: Idem para P4.
        fairness_weights: Peso de cada prioridade no scheduler WRR.
            Quantidade de eventos que o scheduler consome de cada nível
            antes de passar para o próximo, quando há eventos concorrentes.
            P0 tem peso máximo (sempre drenado primeiro).
        shutdown_drain_timeout_seconds: Tempo máximo para dragar eventos P0
            pendentes durante shutdown antes de forçar encerramento.
        latency_sample_size: Tamanho máximo do buffer de amostras de latência.
            Mantido como deque limitado — sem crescimento ilimitado.
        log_every_n_drops: Emite log de warning a cada N drops de uma vez
            para evitar log flooding durante pressão.
    """

    # Queue
    queue_capacity_per_level: int = 500
    p0_express_capacity: int = 100

    # Workers
    n_workers: int = 4
    max_in_flight: int = 16

    # Deduplication
    dedup_maxsize: int = 10_000
    dedup_ttl_seconds: float = 30.0

    # Aggregation
    aggregation_window_seconds: float = 2.0
    aggregation_max_bucket_size: int = 50

    # Overflow thresholds (fração do total de capacidade)
    # P0: sem threshold (express lane + queue capacity)
    # P1: drop acima de 90% da capacidade total
    overflow_threshold_p1: float = 0.90
    # P2: drop acima de 80%
    overflow_threshold_p2: float = 0.80
    # P3: drop acima de 60%
    overflow_threshold_p3: float = 0.60
    # P4: drop acima de 50%
    overflow_threshold_p4: float = 0.50

    # Fairness WRR — pesos de consumo por prioridade durante concorrência
    # Interpretação: o scheduler consome até N eventos de cada nível
    # antes de checar o nível anterior novamente.
    fairness_weights: dict[int, int] = field(
        default_factory=lambda: {
            0: 100,  # P0: drena completamente antes de qualquer outro
            1: 8,    # P1: 8 eventos antes de checar P0 novamente
            2: 4,    # P2
            3: 2,    # P3
            4: 1,    # P4
        }
    )

    # Shutdown
    shutdown_drain_timeout_seconds: float = 5.0

    # Observability
    latency_sample_size: int = 1_000
    log_every_n_drops: int = 50

    def __post_init__(self) -> None:
        if self.queue_capacity_per_level <= 0:
            raise ValueError("queue_capacity_per_level deve ser > 0")
        if self.p0_express_capacity < 0:
            raise ValueError("p0_express_capacity não pode ser negativo")
        if self.n_workers <= 0:
            raise ValueError("n_workers deve ser > 0")
        if self.max_in_flight <= 0:
            raise ValueError("max_in_flight deve ser > 0")
        if self.dedup_maxsize <= 0:
            raise ValueError("dedup_maxsize deve ser > 0")
        if self.dedup_ttl_seconds <= 0:
            raise ValueError("dedup_ttl_seconds deve ser > 0")
        if self.aggregation_window_seconds <= 0:
            raise ValueError("aggregation_window_seconds deve ser > 0")
        if self.aggregation_max_bucket_size <= 0:
            raise ValueError("aggregation_max_bucket_size deve ser > 0")
        for name, threshold in [
            ("overflow_threshold_p1", self.overflow_threshold_p1),
            ("overflow_threshold_p2", self.overflow_threshold_p2),
            ("overflow_threshold_p3", self.overflow_threshold_p3),
            ("overflow_threshold_p4", self.overflow_threshold_p4),
        ]:
            if not (0.0 <= threshold <= 1.0):
                raise ValueError(f"{name} deve estar entre 0.0 e 1.0, recebeu {threshold}")

    @property
    def total_queue_capacity(self) -> int:
        """Capacidade total de eventos em fila (todas as prioridades)."""
        return self.queue_capacity_per_level * 5

    @classmethod
    def default(cls) -> "EngineConfig":
        """Retorna configuração padrão com todos os valores documentados."""
        return cls()
