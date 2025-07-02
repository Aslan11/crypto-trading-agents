class ClientSession:
    def __init__(self, read_stream=None, write_stream=None):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, exc_type, exc, tb):
        pass
    async def initialize(self):
        pass
    async def list_tools(self):
        class Tool:
            def __init__(self, name):
                self.name = name
                self.description = ""
                self.inputSchema = {}
        return type('Resp', (), {'tools': [Tool('get_portfolio_status')]})
    async def call_tool(self, name, args):
        return type('Res', (), {'model_dump': lambda self=self: {}})()
