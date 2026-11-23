"""Loopback-only official SDK peer. All values and credentials are synthetic."""
import asyncio
from contextlib import asynccontextmanager, contextmanager
import socket
import threading
import time
from types import SimpleNamespace

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, RedirectResponse, StreamingResponse
from starlette.routing import Route
import uvicorn


@contextmanager
def running_server(mode='normal', token='fixture-token-only'):
    state = SimpleNamespace(calls=[], requests=[], pages=0, token=token)

    async def list_tools(ctx, params):
        state.pages += 1
        if mode == 'slow_list':
            await asyncio.sleep(30)
        schema = {'type': 'object', 'properties': {'query': {'type': 'string'}},
                  'required': ['query']}
        if mode == 'schema_leak':
            schema['description'] = token
        names = [('lookup', True), ('delete', False), ('unlisted', True)]
        if mode == 'pages':
            names = [(f'tool_{state.pages}_{i}', True) for i in range(40)]
        return ListToolsResult(
            tools=[Tool(name=name, description='Synthetic fixture', inputSchema=schema,
                        annotations=ToolAnnotations(readOnlyHint=read_only)) for name, read_only in names],
            nextCursor='again' if mode == 'pages' else None)

    async def call_tool(ctx, params):
        state.calls.append(params.name)
        query = params.arguments['query']
        if query == 'slow':
            await asyncio.sleep(30)
        return CallToolResult(
            content=[TextContent(type='text', text=(token if query in ('secret', 'error')
                                                  else 'x' * 20000 if query == 'large'
                                                  else query + ' value'))],
            isError=query == 'error')

    peer = Server('isolated-fixture', on_list_tools=list_tools, on_call_tool=call_tool)
    manager = StreamableHTTPSessionManager(peer, json_response=True)

    async def endpoint(scope, receive, send):
        state.requests.append(scope['path'])
        if mode in ('oversized_json', 'oversized_sse'):
            async def oversized():
                yield b'data: ' if mode == 'oversized_sse' else b'{"payload":"'
                for _ in range(40):
                    yield b'x' * 65536
                yield b'\n\n' if mode == 'oversized_sse' else b'"}'
            await StreamingResponse(oversized(), media_type='text/event-stream' if mode == 'oversized_sse' else 'application/json')(scope, receive, send)
            return
        if scope['path'] == '/redirect':
            await RedirectResponse('/mcp', status_code=307)(scope, receive, send)
            return
        if scope['path'] == '/malformed':
            await PlainTextResponse('invalid-json-' + token, media_type='application/json')(scope, receive, send)
            return
        headers = dict(scope['headers'])
        if headers.get(b'authorization') != ('Bearer ' + token).encode():
            await PlainTextResponse(token, status_code=401)(scope, receive, send)
            return
        await manager.handle_request(scope, receive, send)

    @asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    class Endpoint:
        async def __call__(self, scope, receive, send):
            await endpoint(scope, receive, send)

    app = Starlette(routes=[Route(path, endpoint=Endpoint(), methods=['GET', 'POST', 'DELETE'])
                            for path in ('/mcp', '/redirect', '/malformed')], lifespan=lifespan)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    state.url = f'http://127.0.0.1:{sock.getsockname()[1]}/mcp'
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False,
                                         timeout_graceful_shutdown=1))
    thread = threading.Thread(target=lambda: asyncio.run(server.serve(sockets=[sock])), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError('Fixture did not start')
            time.sleep(0.01)
        yield state
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        if thread.is_alive():
            raise RuntimeError('Fixture did not stop')
