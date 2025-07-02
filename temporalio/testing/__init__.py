from contextlib import asynccontextmanager
from dataclasses import dataclass
try:
    from importlib import import_module
    _orig = import_module('temporalio.testing')
    for attr in dir(_orig):
        globals()[attr] = getattr(_orig, attr)
except Exception:
    _orig = None

@dataclass
class Service:
    target_host: str = "localhost"
    grpc_port: int = 7233
    namespace: str = "default"

@asynccontextmanager
async def docker_service():
    yield Service()
