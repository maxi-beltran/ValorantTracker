"""
Proxy serverless para Vercel -> HenrikDev Valorant API.

Uso desde la app de escritorio:
    GET /api/proxy?endpoint=/valorant/v2/mmr/latam/Nombre/TAG

El proxy:
  * Inyecta la API Key (variable de entorno VALORANT_API_KEY) en el header
    Authorization. La key nunca sale del servidor.
  * Solo permite endpoints de Valorant en una lista blanca (evita que tu
    proxy se use como "open proxy" o para SSRF).
  * Devuelve el JSON y el status code de HenrikDev de forma transparente.
  * Cachea en el edge de Vercel las respuestas 200 durante 60 s para
    ahorrar cuota de la API.

Solo usa la librería estándar -> no necesita requirements.txt.
"""

import json
import os
from http.server import BaseHTTPRequestHandler
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

HENRIK_BASE = "https://api.henrikdev.xyz"
HENRIK_HOST = "api.henrikdev.xyz"
UPSTREAM_TIMEOUT = 12  # segundos (menor que maxDuration de vercel.json)
MAX_ENDPOINT_LEN = 300

# Endpoints permitidos (prefijos). Añade aquí los que necesites en el futuro.
ALLOWED_PREFIXES = (
    "/valorant/v1/account/",
    "/valorant/v2/account/",
    "/valorant/v2/mmr/",
    "/valorant/v3/mmr/",
    "/valorant/v1/mmr-history/",
    "/valorant/v3/matches/",
    "/valorant/v4/matches/",
    "/valorant/v2/match/",
    "/valorant/v4/match/",
)
FORBIDDEN_SEQUENCES = ("..", "://", "\\", "@", "\r", "\n")


def validate_endpoint(endpoint: str):
    """Devuelve un mensaje de error si el endpoint no es válido, o None."""
    if not endpoint:
        return "Falta el parámetro 'endpoint'."
    if len(endpoint) > MAX_ENDPOINT_LEN:
        return "Endpoint demasiado largo."
    if any(seq in endpoint for seq in FORBIDDEN_SEQUENCES):
        return "Endpoint inválido."
    if not endpoint.startswith(ALLOWED_PREFIXES):
        return "Endpoint no permitido por el proxy."
    return None


class handler(BaseHTTPRequestHandler):  # Vercel requiere el nombre "handler"
    # ------------------------------------------------------------------ #
    # Métodos HTTP
    # ------------------------------------------------------------------ #
    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        api_key = os.environ.get("VALORANT_API_KEY")
        if not api_key:
            return self._send_json(500, {"error": "VALORANT_API_KEY no está configurada en el servidor."})

        query = parse_qs(urlparse(self.path).query)
        endpoint = (query.get("endpoint") or [""])[0].strip()

        error = validate_endpoint(endpoint)
        if error:
            return self._send_json(400, {"error": error})

        target_url = HENRIK_BASE + endpoint
        if urlparse(target_url).hostname != HENRIK_HOST:
            return self._send_json(400, {"error": "Endpoint inválido."})

        request = Request(
            target_url,
            headers={
                "Authorization": api_key,
                "Accept": "application/json",
                "User-Agent": "valorant-tracker-proxy/1.0",
            },
        )

        try:
            with urlopen(request, timeout=UPSTREAM_TIMEOUT) as resp:
                status, body = resp.status, resp.read()
        except HTTPError as e:  # 4xx / 5xx de HenrikDev -> se reenvían tal cual
            status, body = e.code, (e.read() or b"")
        except (URLError, TimeoutError) as e:
            reason = getattr(e, "reason", e)
            return self._send_json(504, {"error": f"No se pudo contactar con HenrikDev: {reason}"})

        self._send_upstream(status, body)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_upstream(self, status: int, body: bytes):
        # Garantiza que siempre devolvemos JSON válido al cliente.
        try:
            json.loads(body or b"null")
        except ValueError:
            body = json.dumps({"error": "Respuesta no válida de HenrikDev.", "status": status}).encode()

        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header(
            "Cache-Control",
            "public, s-maxage=60, stale-while-revalidate=120" if status == 200 else "no-store",
        )
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)
