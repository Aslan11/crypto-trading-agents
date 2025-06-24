import os
import json
import asyncio
import openai
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

openai.api_key = os.getenv("OPENAI_API_KEY")

SYSTEM_PROMPT = (
    "You are a trading broker agent. You manage execution of trades and the account state. "
    "You have tools to place orders, check portfolio status, and handle transactions. "
    "When asked to execute a trade or query account status, use the appropriate tool and report the result."
)

async def get_next_broker_command() -> str | None:
    await asyncio.sleep(0)
    return None

async def run_broker_agent(server_url: str = "http://localhost:8080"):
    url = server_url.rstrip("/") + "/mcp"
    async with streamablehttp_client(url) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            conversation = [{"role": "system", "content": SYSTEM_PROMPT}]
            print("[BrokerAgent] Connected with tools:", [t.name for t in tools])

            while True:
                user_request = await get_next_broker_command()
                if user_request is None:
                    await asyncio.sleep(1)
                    continue
                conversation.append({"role": "user", "content": user_request})
                functions = [
                    {"name": t.name, "description": t.description, "parameters": t.input_schema}
                    for t in tools
                ]
                response = openai.ChatCompletion.create(
                    model=os.environ.get("OPENAI_MODEL", "gpt-4-0613"),
                    messages=conversation,
                    functions=functions,
                    function_call="auto",
                )
                msg = response['choices'][0]['message']
                if msg.get("function_call"):
                    func_name = msg["function_call"]["name"]
                    func_args = json.loads(msg["function_call"].get("arguments") or "{}")
                    print(f"[BrokerAgent] Invoking tool: {func_name}{func_args}")
                    result = await session.call_tool(func_name, func_args)
                    conversation.append({"role": "function", "name": func_name, "content": json.dumps(result)})
                    followup = openai.ChatCompletion.create(
                        model=os.environ.get("OPENAI_MODEL", "gpt-4-0613"),
                        messages=conversation,
                        functions=functions,
                    )
                    assistant_msg = followup['choices'][0]['message']['content']
                    conversation.append({"role": "assistant", "content": assistant_msg})
                    print(f"[BrokerAgent] Response: {assistant_msg}")
                else:
                    assistant_msg = msg.get("content", "")
                    conversation.append({"role": "assistant", "content": assistant_msg})
                    print(f"[BrokerAgent] Response: {assistant_msg}")
                conversation = [{"role": "system", "content": SYSTEM_PROMPT}]

if __name__ == "__main__":
    asyncio.run(run_broker_agent(os.environ.get("MCP_SERVER", "http://localhost:8080")))
