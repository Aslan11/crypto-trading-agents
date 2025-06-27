import os
import json
import asyncio
import aiohttp
import openai
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

openai_client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM_PROMPT = (
    "You are a strategy ensemble agent. You aggregate trading signals from multiple strategies, "
    "perform risk checks, and decide whether to approve trade intents. "
    "You have tools for risk assessment and broadcasting intents. "
    "Only use the tools to analyze signals and approve or reject intents, and explain your decisions."
)

async def _stream_strategy_signals(
    session: aiohttp.ClientSession, base_url: str
) -> asyncio.AsyncIterator[dict]:
    """Yield strategy signals from the MCP server."""
    cursor = 0
    url = base_url.rstrip("/") + "/signal/strategy_signal"
    while True:
        try:
            async with session.get(url, params={"after": cursor}) as resp:
                if resp.status == 200:
                    events = await resp.json()
                else:
                    events = []
        except Exception:
            events = []

        if not events:
            await asyncio.sleep(1)
            continue

        for evt in events:
            ts = evt.get("ts")
            if ts is not None:
                cursor = max(cursor, ts)
            yield evt

async def run_ensemble_agent(server_url: str = "http://localhost:8080") -> None:
    """Run the ensemble agent and react to strategy signals."""
    base_url = server_url.rstrip("/")
    mcp_url = base_url + "/mcp"

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as http_session:
        async with streamablehttp_client(mcp_url) as (
            read_stream,
            write_stream,
            _,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools_resp = await session.list_tools()
                tools = tools_resp.tools
                conversation = [{"role": "system", "content": SYSTEM_PROMPT}]
                print(
                    "[EnsembleAgent] Connected to MCP server with tools:",
                    [t.name for t in tools],
                )

                async for incoming_signal in _stream_strategy_signals(
                    http_session, base_url
                ):
                    signal_str = (
                        f"Strategy signal received: {json.dumps(incoming_signal)}. "
                        f"Decide whether to approve this trade intent."
                    )
                    conversation.append({"role": "user", "content": signal_str})
                    functions = [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.inputSchema,
                        }
                        for tool in tools
                    ]
                    response = openai_client.chat.completions.create(
                        model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
                        messages=conversation,
                        tools=functions,
                        tool_choice="auto",
                    )
                    msg = response.choices[0].message
                    if msg.get("function_call"):
                        func_name = msg["function_call"]["name"]
                        func_args = json.loads(
                            msg["function_call"].get("arguments") or "{}"
                        )
                        print(
                            f"[EnsembleAgent] Tool requested: {func_name} {func_args}"
                        )
                        result = await session.call_tool(func_name, func_args)
                        conversation.append(
                            {
                                "role": "function",
                                "name": func_name,
                                "content": json.dumps(result),
                            }
                        )
                        continue

                    assistant_reply = msg.get("content", "")
                    conversation.append(
                        {"role": "assistant", "content": assistant_reply}
                    )
                    print(f"[EnsembleAgent] Decision: {assistant_reply}")
                    conversation = [
                        {"role": "system", "content": SYSTEM_PROMPT}
                    ]

if __name__ == "__main__":
    asyncio.run(run_ensemble_agent(os.environ.get("MCP_SERVER", "http://localhost:8080")))
