"""Provision a throwaway Gremlin Server (TinkerGraph) container for managed validation."""

from __future__ import annotations

from typing import Any

from rows2graph.validators.gremlin.server import GremlinConfig

# Gremlin Server image. The minor version MUST track the installed
# gremlinpython minor (3.8.x) so client and server agree on the
# serialization protocol; a mismatch fails at submit time.
IMAGE = "tinkerpop/gremlin-server:3.8.1"
_PORT = 8182
# The base Gremlin Server image logs this line once the websocket channel
# accepts connections. The plain DockerContainer has no built-in readiness
# wait, so we block on this log line.
_READY_LOG = "Channel started at port 8182"


def start() -> tuple[Any, GremlinConfig]:
    """Start an empty Gremlin Server container and return ``(container, config)``.

    The default TinkerGraph backend is schemaless and unauthenticated, which
    is sufficient for catching parse / step-compatibility errors. The URL is
    assembled from the published host and port so it honours
    ``TESTCONTAINERS_HOST_OVERRIDE`` when set.
    """
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs

    container = DockerContainer(IMAGE).with_exposed_ports(_PORT)
    container.start()
    wait_for_logs(container, _READY_LOG, timeout=90)
    host = container.get_container_host_ip()
    port = container.get_exposed_port(_PORT)
    config = GremlinConfig(url=f"ws://{host}:{port}/gremlin", traversal_source="g")
    return container, config
