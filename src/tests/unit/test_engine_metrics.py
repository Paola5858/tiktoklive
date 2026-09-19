"""Testes unitários das métricas do engine.

Cobertos:
- Contadores incrementam corretamente
- LatencyTracker não cresce além de maxlen
- Percentis são None com amostras insuficientes (< 20)
- Percentis calculados com amostras suficientes
- record_dropped() retorna True apenas a cada N drops
- snapshot() retorna dict com todos os campos obrigatórios
- update_queue_depth() atualiza o dict de profundidade por prioridade
"""

from __future__ import annotations

import pytest

from src.engine.metrics import EngineMetrics, LatencyTracker, now_ms


# ---------------------------------------------------------------------------
# LatencyTracker
# ---------------------------------------------------------------------------

def test_latency_tracker_maxlen_respected():
    """LatencyTracker não cresce além de maxlen."""
    tracker = LatencyTracker(maxlen=10)
    for i in range(20):
        tracker.record(float(i))
    assert tracker.count == 10


def test_latency_tracker_empty_snapshot():
    """Snapshot vazio retorna None para todos os campos de latência."""
    tracker = LatencyTracker(maxlen=100)
    snap = tracker.snapshot()
    assert snap["count"] == 0
    assert snap["avg_ms"] is None
    assert snap["p50_ms"] is None
    assert snap["p95_ms"] is None


def test_latency_tracker_percentiles_none_with_few_samples():
    """Percentis são None quando há menos de 20 amostras."""
    tracker = LatencyTracker(maxlen=100)
    for i in range(10):
        tracker.record(float(i))
    snap = tracker.snapshot()
    assert snap["count"] == 10
    assert snap["avg_ms"] is not None  # Média sempre calculada
    assert snap["p50_ms"] is None
    assert snap["p95_ms"] is None


def test_latency_tracker_percentiles_calculated_with_sufficient_samples():
    """Percentis calculados quando há >= 20 amostras."""
    tracker = LatencyTracker(maxlen=1000)
    for i in range(100):
        tracker.record(float(i))  # 0.0 a 99.0
    snap = tracker.snapshot()
    assert snap["p50_ms"] is not None
    assert snap["p95_ms"] is not None
    # P95 deve ser maior que P50
    assert snap["p95_ms"] > snap["p50_ms"]


def test_latency_tracker_average_is_correct():
    """Média é calculada corretamente."""
    tracker = LatencyTracker(maxlen=100)
    tracker.record(10.0)
    tracker.record(20.0)
    snap = tracker.snapshot()
    assert snap["avg_ms"] == 15.0


def test_latency_tracker_invalid_maxlen():
    with pytest.raises(ValueError):
        LatencyTracker(maxlen=0)


def test_latency_tracker_p95_semantics():
    """P95 deve estar aproximadamente no 95° percentil."""
    tracker = LatencyTracker(maxlen=1000)
    # 100 amostras: 0 a 99
    for i in range(100):
        tracker.record(float(i))
    snap = tracker.snapshot()
    # P95 de 0..99 deve estar em ~94 (index 95 de 100 = 95.0)
    assert 90.0 <= snap["p95_ms"] <= 99.0


# ---------------------------------------------------------------------------
# EngineMetrics counters
# ---------------------------------------------------------------------------

def test_record_received_increments():
    m = EngineMetrics()
    m.record_received()
    m.record_received()
    assert m.events_received == 2


def test_record_accepted_increments():
    m = EngineMetrics()
    m.record_accepted()
    assert m.events_accepted == 1


def test_record_rejected_increments():
    m = EngineMetrics()
    m.record_rejected()
    assert m.events_rejected == 1


def test_record_deduplicated_increments():
    m = EngineMetrics()
    m.record_deduplicated()
    assert m.events_deduplicated == 1


def test_record_aggregated_increments():
    m = EngineMetrics()
    m.record_aggregated(count=5)
    assert m.events_aggregated == 5


def test_record_aggregated_flush():
    m = EngineMetrics()
    m.record_aggregated_flush()
    m.record_aggregated_flush()
    assert m.aggregated_batches_flushed == 2


def test_record_queued_increments_and_tracks_depth():
    m = EngineMetrics()
    m.record_queued(priority=1)
    m.record_queued(priority=1)
    m.record_queued(priority=3)
    assert m.events_queued == 3
    assert m.depth_by_priority[1] == 2
    assert m.depth_by_priority[3] == 1


def test_record_dropped_increments():
    m = EngineMetrics()
    m.record_dropped()
    m.record_dropped()
    assert m.events_dropped == 2
    assert m.events_overflow == 2


def test_record_dropped_log_throttle():
    """record_dropped() retorna True apenas a cada log_every_n drops."""
    m = EngineMetrics()
    log_triggers = 0
    for _ in range(100):
        if m.record_dropped(log_every_n=50):
            log_triggers += 1
    # A cada 50 drops, 1 log → 100 drops = 2 triggers
    assert log_triggers == 2


def test_record_processing_start_and_end():
    m = EngineMetrics()
    m.record_processing_start()
    m.record_processing_start()
    assert m.events_processing == 2
    assert m.in_flight_events == 2

    m.record_processing_end(failed=False)
    assert m.events_processing == 1
    assert m.in_flight_events == 1
    assert m.events_processed == 1
    assert m.events_failed == 0

    m.record_processing_end(failed=True)
    assert m.events_processing == 0
    assert m.events_failed == 1


def test_in_flight_does_not_go_negative():
    """in_flight_events não vai abaixo de 0."""
    m = EngineMetrics()
    m.record_processing_end(failed=False)  # Sem start
    assert m.in_flight_events == 0
    assert m.events_processing == 0


def test_record_dispatched_and_failure():
    m = EngineMetrics()
    m.record_dispatched()
    m.record_dispatched()
    m.record_dispatch_failure()
    assert m.events_dispatched == 2
    assert m.dispatch_failures == 1


def test_worker_start_stop():
    m = EngineMetrics()
    m.record_worker_started()
    m.record_worker_started()
    assert m.active_workers == 2
    m.record_worker_stopped()
    assert m.active_workers == 1


def test_worker_stopped_does_not_go_negative():
    m = EngineMetrics()
    m.record_worker_stopped()  # Sem start
    assert m.active_workers == 0


def test_update_queue_depth():
    m = EngineMetrics()
    m.update_queue_depth(priority=0, depth=5)
    m.update_queue_depth(priority=3, depth=10)
    assert m.depth_by_priority[0] == 5
    assert m.depth_by_priority[3] == 10


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def test_snapshot_contains_required_keys():
    m = EngineMetrics()
    snap = m.snapshot()
    required = {
        "received", "accepted", "rejected", "deduplicated",
        "aggregated", "queued", "dropped", "overflow",
        "processed", "failed", "dispatched", "dispatch_failures",
        "active_workers", "in_flight", "queue_depth_by_priority",
        "queue_depth_total", "latency",
    }
    for k in required:
        assert k in snap, f"Campo obrigatório '{k}' ausente no snapshot"


def test_snapshot_latency_fields():
    m = EngineMetrics()
    snap = m.snapshot()
    latency = snap["latency"]
    required_phases = {"ingest_to_queue", "queue_wait", "processing", "end_to_end"}
    for phase in required_phases:
        assert phase in latency, f"Fase de latência '{phase}' ausente"


def test_snapshot_is_independent_copy():
    """Modificar o snapshot não afeta as métricas internas."""
    m = EngineMetrics()
    m.record_received()
    snap = m.snapshot()
    snap["received"] = 999  # Modificar o snapshot

    # Métrica interna não foi afetada
    assert m.events_received == 1


# ---------------------------------------------------------------------------
# now_ms
# ---------------------------------------------------------------------------

def test_now_ms_is_positive():
    t = now_ms()
    assert t > 0


def test_now_ms_increases():
    t1 = now_ms()
    t2 = now_ms()
    assert t2 >= t1
