import asyncio
from src.engine.config import EngineConfig
from src.engine.metrics import EngineMetrics
from src.engine.dispatcher import Dispatcher, NullConsumer
from src.engine.processor import EventProcessor
from src.observability.health import Watchdog
from src.observability.resilience import ResilienceMetrics
from src.observability.metrics import OperationalSnapshot
from src.adapters.roblox import RobloxBridge
from src.adapters.local_api import create_app
from fastapi.testclient import TestClient

async def main():
    metrics = EngineMetrics()
    resilience = ResilienceMetrics()
    watchdog = Watchdog(resilience=resilience)

    dispatcher = Dispatcher(metrics)
    bridge = RobloxBridge(watchdog=watchdog)
    dispatcher.register(bridge)

    processor = EventProcessor(
        config=EngineConfig(),
        metrics=metrics,
        dispatcher=dispatcher,
        watchdog=watchdog,
        resilience=resilience,
    )

    snapshot = OperationalSnapshot(metrics, watchdog, resilience=resilience)

    app = create_app(bridge, snapshot)

    client = TestClient(app)
    resp = client.get("/health")
    import json
    print(json.dumps(resp.json(), indent=2))

if __name__ == "__main__":
    asyncio.run(main())
