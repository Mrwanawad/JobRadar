"""Agent worker process: `jobradar worker` (or `python -m jobradar worker`)."""

from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.worker import Worker

from jobradar.agent.activities import Agent
from jobradar.agent.search import JobIndex
from jobradar.agent.store import AgentStore

TASK_QUEUE = "jobradar-agent"
log = logging.getLogger(__name__)


def setup_telemetry(service: str = "agent-worker") -> None:
    """OTLP traces when OTEL_EXPORTER_OTLP_ENDPOINT is set; httpx (LLM + board calls) and
    Elasticsearch spans join the Temporal activity span that started them."""
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    os.environ.setdefault("OTEL_SERVICE_NAME", service)
    provider = TracerProvider(resource=Resource.create())
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(insecure=True)))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()


async def run() -> None:
    setup_telemetry()
    client = await Client.connect(
        os.getenv("TEMPORAL_ADDRESS", "localhost:7233"),
        namespace=os.getenv("TEMPORAL_NAMESPACE", "jobradar"),
        interceptors=[TracingInterceptor()],
    )
    store = AgentStore(
        os.getenv("DATABASE_URL", "postgresql://jobradar:jobradar@localhost/jobradar")
    )
    agent = Agent(store=store, index=JobIndex.from_env())
    threads = int(os.getenv("AGENT_THREADS", "8"))
    with ThreadPoolExecutor(threads) as pool:
        worker = Worker(
            client,
            task_queue=TASK_QUEUE,
            activities=agent.activities(),
            activity_executor=pool,
            max_concurrent_activities=threads,
        )
        log.info("agent worker started on %s", TASK_QUEUE)
        try:
            await worker.run()
        finally:
            store.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run())
