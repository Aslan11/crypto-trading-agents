__all__ = ["Service", "docker_service"]

from contextlib import asynccontextmanager
from dataclasses import dataclass

@dataclass
class Service:
    target_host: str = "localhost"
    grpc_port: int = 7233
    namespace: str = "default"

@asynccontextmanager
async def docker_service():
    yield Service()
