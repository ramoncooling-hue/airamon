"""A real stdio protocol peer with no application data or network access."""
import json
import os
import sys
import time

for line in sys.stdin:
    message = json.loads(line)
    if 'id' not in message:
        continue
    method = message.get('method')
    if method == 'initialize':
        result = {'protocolVersion': message['params']['protocolVersion'],
                  'capabilities': {'tools': {}}, 'serverInfo': {'name': 'fixture', 'version': '1'}}
    elif method == 'tools/list':
        schema = {'type': 'object', 'properties': {'query': {'type': 'string'}},
                  'required': ['query'], 'additionalProperties': False}
        if os.environ.get('MCP_TEST_SCHEMA_LEAK'):
            schema['description'] = os.environ['MCP_TEST_SECRET']
        result = {'tools': [{'name': name, 'inputSchema': schema,
                             'annotations': {'readOnlyHint': readonly}}
                            for name, readonly in [('lookup', True), ('delete', False), ('unlisted', True)]]}
    elif method == 'tools/call':
        query = message['params']['arguments']['query']
        if query == 'malformed':
            print('invalid JSON ' + os.environ['MCP_TEST_SECRET'], flush=True)
        if query == 'slow':
            time.sleep(30)
        result = {'content': [{'type': 'text', 'text': (
            'x' * 20000 if query == 'large' else 'fixture-secret' if query == 'error' else query + ' value')}],
                  'isError': query == 'error'}
    else:
        result = {}
    print(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}), flush=True)
