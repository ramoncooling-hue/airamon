"""Revalidate local frontend assets when desktop builds share an origin."""

from fastapi.staticfiles import StaticFiles

# Security headers for the local single-page app. The frontend keeps its
# rendering code inline (no build step), so script/style need 'unsafe-inline';
# everything else stays same-origin and image loads are limited to local files
# (markdown answers downgrade remote images to links before rendering).
SECURITY_HEADERS = {
    'Content-Security-Policy': (
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        # The local UI must never be framed, base-URI hijacked or post forms
        # anywhere but the app origin.
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'"
    ),
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
}


class RevalidatingStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers['Cache-Control'] = 'no-cache, must-revalidate'
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        return response
