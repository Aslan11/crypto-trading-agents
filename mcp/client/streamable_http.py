from contextlib import asynccontextmanager

@asynccontextmanager
async def streamablehttp_client(url):
    yield None, None, None
