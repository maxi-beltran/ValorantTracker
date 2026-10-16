from http.server import BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor
import urllib.request
import urllib.parse
import urllib.error
import os
import json
import gzip
import re

ALLOWED_PREFIXES = (
    "/valorant/v1/account/",
    "/valorant/v1/by-puuid/account/",
    "/valorant/v2/mmr/",
    "/valorant/v2/by-puuid/mmr/",
    "/valorant/v3/matches/",
    "/valorant/v3/by-puuid/matches/",
)

FORBIDDEN_PATTERNS = re.compile(r"(\.\.|://|@|\s|\\)")


def is_valid_endpoint(endpoint: str) -> bool:
    if not endpoint or not endpoint.startswith("/"):
        return False
    if FORBIDDEN_PATTERNS.search(endpoint):
        return False
    return any(endpoint.startswith(prefix) for prefix in ALLOWED_PREFIXES)


def fetch_henrik(endpoint: str, api_key: str, timeout: int = 12):
    target_url = f"https://api.henrikdev.xyz{endpoint}"
    req = urllib.request.Request(
        target_url,
        headers={
            "Authorization": api_key,
            "User-Agent": "ValorantTrackerProxy/2.0",
            "Accept-Encoding": "gzip",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return resp.status, json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read()
        if e.headers.get("Content-Encoding") == "gzip":
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass
        try:
            body = json.loads(raw.decode("utf-8", errors="ignore"))
        except Exception:
            body = {"error": f"HTTP {e.code}"}
        return e.code, body
    except Exception as e:
        return 500, {"error": str(e)}


def compact_matches_payload(payload: dict) -> dict:
    """
    Extrae 3k, 4k, Ace y KAST aproximado de las rondas y elimina los arrays
    gigantes de coordenadas ('rounds' y 'kills') para reducir el peso un 98%.
    """
    if not isinstance(payload, dict) or "data" not in payload:
        return payload

    matches = payload.get("data")
    if not isinstance(matches, list):
        return payload

    compacted_matches = []
    for match in matches:
        if not isinstance(match, dict):
            continue

        rounds = match.get("rounds") or []
        all_players = match.get("players", {}).get("all_players", [])

        # Calcular multikills (2k, 3k, 4k, 5k/Ace) por jugador en cada ronda
        multikills_by_puuid = {}
        rounds_with_contribution = {}

        for r in rounds:
            player_stats = r.get("player_stats") or []
            for ps in player_stats:
                puuid = ps.get("player_puuid")
                if not puuid:
                    continue
                k_count = len(ps.get("kill_events") or [])
                if k_count == 0 and isinstance(ps.get("kills"), int):
                    k_count = ps.get("kills", 0)

                mk = multikills_by_puuid.setdefault(puuid, {"3k": 0, "4k": 0, "5k": 0})
                if k_count == 3:
                    mk["3k"] += 1
                elif k_count == 4:
                    mk["4k"] += 1
                elif k_count >= 5:
                    mk["5k"] += 1

                if k_count > 0 or ps.get("damage", 0) >= 50:
                    rounds_with_contribution[puuid] = rounds_with_contribution.get(puuid, 0) + 1

        compact_players = []
        for p in all_players:
            puuid = p.get("puuid", "")
            assets = p.get("assets", {})
            agent_assets = assets.get("agent", {})
            compact_players.append({
                "puuid": puuid,
                "name": p.get("name", ""),
                "tag": p.get("tag", ""),
                "team": p.get("team", "Red"),
                "character": p.get("character", "Unknown"),
                "currenttier": p.get("currenttier", 0),
                "currenttier_patched": p.get("currenttier_patched", "Unranked"),
                "damage_made": p.get("damage_made", 0),
                "damage_received": p.get("damage_received", 0),
                "stats": p.get("stats", {}),
                "agent_icon": agent_assets.get("small", ""),
                "multikills": multikills_by_puuid.get(puuid, {"3k": 0, "4k": 0, "5k": 0}),
                "impact_rounds": rounds_with_contribution.get(puuid, 0),
            })

        meta = match.get("metadata", {})
        compacted_matches.append({
            "metadata": {
                "map": meta.get("map", "Unknown"),
                "mode": meta.get("mode", "Competitive"),
                "mode_id": meta.get("mode_id", ""),
                "game_start": meta.get("game_start", 0),
                "game_start_patched": meta.get("game_start_patched", ""),
                "rounds_played": meta.get("rounds_played", 1),
                "matchid": meta.get("matchid", ""),
                "cluster": meta.get("cluster", ""),
            },
            "teams": match.get("teams", {}),
            "players": {"all_players": compact_players},
        })

    return {"status": payload.get("status", 200), "data": compacted_matches}


class handler(BaseHTTPRequestHandler):
    def _send_json(self, status_code: int, data: dict, cache_seconds: int = 60):
        raw_bytes = json.dumps(data, separators=(",", ":")).encode("utf-8")
        accept_enc = self.headers.get("Accept-Encoding", "")

        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if status_code == 200 and cache_seconds > 0:
            self.send_header(
                "Cache-Control",
                f"public, s-maxage={cache_seconds}, stale-while-revalidate=120",
            )

        if "gzip" in accept_enc:
            compressed = gzip.compress(raw_bytes, compresslevel=6)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(compressed)))
            self.end_headers()
            self.wfile.write(compressed)
        else:
            self.send_header("Content-Length", str(len(raw_bytes)))
            self.end_headers()
            self.wfile.write(raw_bytes)

    def do_GET(self):
        api_key = os.environ.get("VALORANT_API_KEY", "").strip()
        if not api_key:
            self._send_json(500, {"error": "VALORANT_API_KEY no configurada en Vercel"}, 0)
            return

        parsed_path = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed_path.query)

        # MODO BATCH ULTRA RÁPIDO: Pide MMR + Matches en paralelo dentro de Vercel
        if query.get("batch", [""])[0] == "1":
            region = query.get("region", ["latam"])[0].strip().lower()
            name = urllib.parse.quote(query.get("name", [""])[0].strip())
            tag = urllib.parse.quote(query.get("tag", [""])[0].strip())
            mode = query.get("mode", [""])[0].strip().lower()
            size = min(max(int(query.get("size", ["10"])[0] or 10), 1), 10)

            if not name or not tag:
                self._send_json(400, {"error": "Faltan name o tag"}, 0)
                return

            mmr_ep = f"/valorant/v2/mmr/{region}/{name}/{tag}"
            mode_param = f"&mode={urllib.parse.quote(mode)}" if mode and mode != "all" else ""
            matches_ep = f"/valorant/v3/matches/{region}/{name}/{tag}?size={size}{mode_param}"

            with ThreadPoolExecutor(max_workers=2) as pool:
                f_mmr = pool.submit(fetch_henrik, mmr_ep, api_key)
                f_matches = pool.submit(fetch_henrik, matches_ep, api_key)
                mmr_code, mmr_body = f_mmr.result()
                mat_code, mat_body = f_matches.result()

            if mat_code == 200:
                mat_body = compact_matches_payload(mat_body)

            status = 404 if (mmr_code == 404 and mat_code == 404) else 200
            self._send_json(
                status,
                {
                    "mmr_status": mmr_code,
                    "matches_status": mat_code,
                    "mmr": mmr_body.get("data", {}) if mmr_code == 200 else {},
                    "matches": mat_body.get("data", []) if mat_code == 200 else [],
                },
                cache_seconds=60,
            )
            return

        # MODO CLÁSICO POR ENDPOINT (retrocompatible)
        endpoint = query.get("endpoint", [""])[0]
        if not is_valid_endpoint(endpoint):
            self._send_json(403, {"error": "Endpoint inválido o no permitido"}, 0)
            return

        code, body = fetch_henrik(endpoint, api_key)
        if code == 200 and endpoint.startswith("/valorant/v3/matches/"):
            body = compact_matches_payload(body)

        self._send_json(code, body, cache_seconds=60 if code == 200 else 0)