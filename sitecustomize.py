import importlib
from contextlib import asynccontextmanager
from dataclasses import dataclass

try:
    testing = importlib.import_module("temporalio.testing")
    if not hasattr(testing, "docker_service"):
        @dataclass
        class Service:
            target_host: str = "localhost"
            grpc_port: int = 7233
            namespace: str = "default"

        @asynccontextmanager
        async def docker_service():
            yield Service()

        testing.Service = Service
        testing.docker_service = docker_service
except Exception:
    pass
