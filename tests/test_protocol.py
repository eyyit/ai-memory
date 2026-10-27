import anyio
import httpx
from mcp.shared.memory import create_connected_server_and_client_session

from ai_memory.config import Settings
from ai_memory.mcp_server import create_mcp


def test_mcp_sdk_protocol_readonly_and_authenticated_proxy(monkeypatch):
    monkeypatch.delenv("AI_MEMORY_MCP_ALLOW_WRITES", raising=False)
    seen = []

    def response(request):
        seen.append(request)
        assert request.headers["authorization"] == "Bearer test-private-token"
        return httpx.Response(200, json={"items": [{"id": "synthetic", "content": "Likes hiking"}]})

    with httpx.Client(base_url="http://memory.test", headers={"Authorization": "Bearer test-private-token"},
                      transport=httpx.MockTransport(response)) as client:
        server = create_mcp(Settings(api_token="test-private-token"), http_client=client)

        async def scenario():
            async with create_connected_server_and_client_session(server) as session:
                tools = (await session.list_tools()).tools
                assert {tool.name for tool in tools} == {"search_memories", "get_memory", "list_memories"}
                assert all("token" not in tool.inputSchema.get("properties", {}) for tool in tools)
                result = await session.call_tool("search_memories", {"query": "hiking"})
                assert not result.isError
                assert "Likes hiking" in str(result.content)
                assert "test-private-token" not in str(result.content)
        anyio.run(scenario)
    assert seen[0].url.params["q"] == "hiking"


def test_mcp_sdk_write_tool_explicit_optin(monkeypatch):
    monkeypatch.setenv("AI_MEMORY_MCP_ALLOW_WRITES", "true")
    seen = []

    def response(request):
        seen.append(request)
        return httpx.Response(200, json={"duplicate": False, "memories_created": 1})

    with httpx.Client(base_url="http://memory.test", transport=httpx.MockTransport(response)) as client:
        server = create_mcp(Settings(api_token="test-private-token"), http_client=client)

        async def scenario():
            async with create_connected_server_and_client_session(server) as session:
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert "ingest_memory" in names
                result = await session.call_tool("ingest_memory", {"source": "test", "source_record_id": "1", "text": "I like hiking"})
                assert not result.isError
        anyio.run(scenario)
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/v1/ingest"


def test_real_stdio_entrypoint_initializes_without_database(monkeypatch):
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=sys.executable, args=["-m", "ai_memory.cli", "mcp"],
                                  env={"AI_MEMORY_API_TOKEN": "synthetic-private-token", "AI_MEMORY_MCP_ALLOW_WRITES": "false"})

    async def scenario():
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                assert {tool.name for tool in (await session.list_tools()).tools} == {"search_memories", "get_memory", "list_memories"}
    anyio.run(scenario)

def test_native_http_mcp_denies_reader_writes(rig, tmp_path):
    from ai_memory.api import create_app
    from test_api import auth_settings, headers
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    app = create_app(rig[0], auth_settings(tmp_path))
    async def scenario():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(base_url='http://testserver', headers=headers('reader'),
                    transport=httpx.ASGITransport(app)) as client:
                async with streamable_http_client('http://testserver/mcp', http_client=client) as (reader, writer, _):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        assert 'add_memory' in {t.name for t in (await session.list_tools()).tools}
                        assert not (await session.call_tool('list_memories', {})).isError
                        result = await session.call_tool('add_memory', {'content': 'Reader should not write', 'source_record_id': '1'})
                        assert result.isError
            async with httpx.AsyncClient(base_url='http://testserver', headers=headers('writer'),
                    transport=httpx.ASGITransport(app)) as client:
                async with streamable_http_client('http://testserver/mcp', http_client=client) as (reader, writer, _):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        assert not (await session.call_tool('add_memory', {'content': 'Likes hiking', 'source_record_id': '2'})).isError
    anyio.run(scenario)
    assert [r['content'] for r in rig[0].list('home')] == ['Likes hiking']

def test_native_mcp_requires_authentication(rig, tmp_path):
    from fastapi.testclient import TestClient
    from ai_memory.api import create_app
    from test_api import auth_settings
    with TestClient(create_app(rig[0], auth_settings(tmp_path))) as client:
        result = client.post('/mcp', json={'jsonrpc':'2.0', 'id':1, 'method':'initialize'})
        assert result.status_code == 401
