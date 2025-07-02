class OpenAI:
    def __init__(self, api_key=None):
        pass
    class Chat:
        class Completions:
            def create(self, *args, **kwargs):
                class Choice:
                    message = type('msg', (), {'content': 'DONE', 'tool_calls': None})
                return type('Resp', (), {'choices': [Choice()]})
    chat = Chat()
