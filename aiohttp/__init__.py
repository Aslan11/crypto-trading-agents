class ClientTimeout:
    def __init__(self, total=None):
        self.total = total

class ClientSession:
    def __init__(self, *args, **kwargs):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, exc_type, exc, tb):
        return False
    async def get(self, *args, **kwargs):
        class DummyResp:
            status = 200
            class DummyContent:
                async def readline(self):
                    return b''
            content = DummyContent()
            async def __aenter__(self):
                return self
            async def __aexit__(self, exc_type, exc, tb):
                pass
        return DummyResp()
