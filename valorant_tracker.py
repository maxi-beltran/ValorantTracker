import os
import re
import json
import time
import base64
import socket
import urllib3
import webbrowser
import subprocess
from urllib.parse import quote, urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------- CONFIGURACIÓN ---------------- #
PROXY_URLS = [
    os.environ.get("VALORANT_PROXY_URL", "").strip(),
    "https://valorant-tracker-proxy.vercel.app/api/proxy",
    "https://valorant-proxy-one.vercel.app/api/proxy",
    "https://valorant-tracker-proxy-czkk7sjr5-maxi-beltrans-projects.vercel.app/api/proxy",
]
PROXY_URLS = [u for u in PROXY_URLS if u]
PORT = 8765

CLIENT_PLATFORM_B64 = (
    "ew0KCSJwbGF0Zm9ybVR5cGUiOiAiUEMiLA0KCSJwbGF0Zm9ybU9TIjog"
    "IldpbmRvd3MiLA0KCSJwbGF0Zm9ybU9TVmVyc2lvbiI6ICIxMC4wLjE5"
    "MDQyLjEuMjU2LjY0Yml0IiwNCgkicGxhdGZvcm1DaGlwc2V0IjogIlVu"
    "a25vd24iDQp9"
)


class ValorantLiveEngine:
    def __init__(self):
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({"Accept-Encoding": "gzip"})

        self.executor = ThreadPoolExecutor(max_workers=12)
        self.client_version = "release-09.08-shipping-10-2800000"
        self.agents_by_uuid = {}
        self.agents_by_name = {}
        self.maps_by_url = {}
        self.tiers_by_id = {}
        self.puuid_name_cache = {}
        self.player_stats_cache = {}
        self.profile_cache = {}
        self.region_cache = {}

        self.executor.submit(self._load_valorant_static_data)

    def _load_valorant_static_data(self):
        try:
            v_res = self.session.get("https://valorant-api.com/v1/version", timeout=8)
            if v_res.status_code == 200:
                self.client_version = v_res.json().get("data", {}).get("riotClientVersion", self.client_version)

            a_res = self.session.get("https://valorant-api.com/v1/agents?isPlayableCharacter=true", timeout=8)
            if a_res.status_code == 200:
                for a in a_res.json().get("data", []):
                    uuid = (a.get("uuid") or "").lower()
                    name = a.get("displayName") or "Agente"
                    icon = a.get("displayIconSmall") or a.get("displayIcon") or ""
                    self.agents_by_uuid[uuid] = {"name": name, "icon": icon}
                    self.agents_by_name[name.lower()] = icon

            m_res = self.session.get("https://valorant-api.com/v1/maps", timeout=8)
            if m_res.status_code == 200:
                for m in m_res.json().get("data", []):
                    name = m.get("displayName") or "Mapa"
                    map_url = (m.get("mapUrl") or "").lower()
                    splash = m.get("stylizedBackgroundImage") or m.get("splash") or m.get("listViewIcon") or ""
                    banner = m.get("listViewIcon") or splash
                    info = {"name": name, "splash": splash, "banner": banner}
                    self.maps_by_url[name.lower()] = info
                    if map_url:
                        self.maps_by_url[map_url] = info

            t_res = self.session.get("https://valorant-api.com/v1/competitivetiers", timeout=8)
            if t_res.status_code == 200:
                tiers_data = t_res.json().get("data", [])
                if tiers_data:
                    for t in tiers_data[-1].get("tiers", []):
                        tid = t.get("tier", 0)
                        self.tiers_by_id[tid] = {
                            "name": t.get("tierName", "Unranked"),
                            "icon": t.get("smallIcon") or t.get("largeIcon") or ""
                        }
        except Exception:
            pass

    def _call_proxy(self, endpoint: str, timeout: int = 15):
        """Llama al proxy de Vercel probando los dominios configurados y validando que devuelva JSON."""
        encoded_ep = quote(endpoint, safe="")
        last_deployment_error = False

        for base in PROXY_URLS:
            try:
                r = self.session.get(f"{base}?endpoint={encoded_ep}", timeout=timeout)
                # Si Vercel responde que el proyecto/función no existe
                if r.headers.get("x-vercel-error") == "NOT_FOUND":
                    continue
                # Si Vercel interceptó con una página de login (Deployment Protection activa)
                ct = r.headers.get("Content-Type", "").lower()
                if "text/html" in ct or r.text.strip().startswith("<!DOCTYPE"):
                    last_deployment_error = True
                    continue
                # Si devuelve JSON válido (sea 200, 404 de jugador no encontrado de Henrik, 429, etc.)
                if "json" in ct or r.text.strip().startswith(("{", "[")):
                    return r
            except Exception:
                continue

        if last_deployment_error:
            class MockDeploymentProtectionResponse:
                status_code = 403
                def json(self):
                    return {
                        "error": "El servidor Vercel devolvió una página de login porque 'Deployment Protection' está activa. Desactívala en tu panel de Vercel (Project Settings -> Deployment Protection -> Vercel Authentication: Disabled)."
                    }
            return MockDeploymentProtectionResponse()

        return None

    def _get_lockfile(self):
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        lockfile_path = os.path.join(local_appdata, "Riot Games", "Riot Client", "Config", "lockfile")
        if not os.path.exists(lockfile_path):
            return None
        try:
            with open(lockfile_path, "r", encoding="utf-8") as f:
                parts = f.read().strip().split(":")
                if len(parts) >= 5:
                    return {
                        "name": parts[0],
                        "pid": parts[1],
                        "port": parts[2],
                        "password": parts[3],
                        "protocol": parts[4],
                    }
        except Exception:
            return None
        return None

    def _get_region_and_shard(self):
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        log_path = os.path.join(local_appdata, "VALORANT", "Saved", "Logs", "ShooterGame.log")
        region, shard = "latam", "na"
        if os.path.exists(log_path):
            try:
                with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                matches = re.findall(r"https://glz-(.+?)-1\.(.+?)\.a\.pvp\.net", content)
                if matches:
                    region, shard = matches[-1][0], matches[-1][1]
            except Exception:
                pass
        return region, shard

    def _get_local_headers(self, lock):
        auth_str = f"riot:{lock['password']}".encode("utf-8")
        b64_auth = base64.b64encode(auth_str).decode("utf-8")
        url = f"https://127.0.0.1:{lock['port']}/entitlements/v1/token"
        r = self.session.get(url, headers={"Authorization": f"Basic {b64_auth}"}, verify=False, timeout=3)
        if r.status_code != 200:
            return None, None
        data = r.json()
        puuid = data.get("subject")
        headers = {
            "Authorization": f"Bearer {data.get('accessToken')}",
            "X-Riot-Entitlements-JWT": data.get("token"),
            "X-Riot-ClientPlatform": CLIENT_PLATFORM_B64,
            "X-Riot-ClientVersion": self.client_version,
            "Content-Type": "application/json",
        }
        return puuid, headers

    def get_live_match_state(self) -> dict:
        lock = self._get_lockfile()
        if not lock:
            return {
                "status": "closed",
                "message": "Valorant no esta abierto. Abre el juego para detectar la sala automaticamente."
            }

        try:
            my_puuid, headers = self._get_local_headers(lock)
        except Exception:
            return {"status": "closed", "message": "Esperando conexion con Riot Client..."}

        if not my_puuid or not headers:
            return {"status": "menu", "message": "Riot Client detectado. Inicia sesion en Valorant."}

        region, shard = self._get_region_and_shard()
        glz_base = f"https://glz-{region}-1.{shard}.a.pvp.net"
        pd_base = f"https://pd.{shard}.a.pvp.net"

        state_type = "menu"
        match_id = None
        match_raw = None

        try:
            cg_p = self.session.get(f"{glz_base}/core-game/v1/players/{my_puuid}", headers=headers, timeout=4)
            if cg_p.status_code == 200:
                match_id = cg_p.json().get("MatchID")
                if match_id:
                    cg_m = self.session.get(f"{glz_base}/core-game/v1/matches/{match_id}", headers=headers, timeout=4)
                    if cg_m.status_code == 200:
                        state_type = "ingame"
                        match_raw = cg_m.json()
        except Exception:
            pass

        if not match_raw:
            try:
                pg_p = self.session.get(f"{glz_base}/pregame/v1/players/{my_puuid}", headers=headers, timeout=4)
                if pg_p.status_code == 200:
                    match_id = pg_p.json().get("MatchID")
                    if match_id:
                        pg_m = self.session.get(f"{glz_base}/pregame/v1/matches/{match_id}", headers=headers, timeout=4)
                        if pg_m.status_code == 200:
                            state_type = "pregame"
                            match_raw = pg_m.json()
            except Exception:
                pass

        if not match_raw:
            my_names = self._resolve_names(pd_base, headers, [my_puuid], region)
            me = my_names.get(my_puuid, {"name": "Jugador", "tag": region.upper()})
            return {
                "status": "menu",
                "region": region,
                "me": f"{me['name']}#{me['tag']}",
                "message": f"Conectado como {me['name']}#{me['tag']} ({region.upper()}). En el Lobby esperando partida..."
            }

        map_id = (match_raw.get("MapID") or "").lower()
        map_info = self.maps_by_url.get(map_id, {"name": map_id.split("/")[-1].title(), "splash": "", "banner": ""})
        server_pod = match_raw.get("GamePodID", "")

        raw_players = []
        if state_type == "ingame":
            raw_players = match_raw.get("Players", [])
        else:
            ally_team = match_raw.get("AllyTeam") or {}
            team_id = ally_team.get("TeamID", "Blue")
            for p in ally_team.get("Players", []):
                p["TeamID"] = team_id
                raw_players.append(p)

        puuids = [p.get("Subject") for p in raw_players if p.get("Subject")]
        names_map = self._resolve_names(pd_base, headers, puuids, region)

        enriched_players = list(self.executor.map(
            lambda p: self._enrich_live_player(p, names_map, region, my_puuid),
            raw_players
        ))

        return {
            "status": state_type,
            "match_id": match_id,
            "map_name": map_info["name"],
            "map_splash": map_info["splash"],
            "server": server_pod.split("-")[-2].upper() if "-" in server_pod else region.upper(),
            "region": region,
            "players": enriched_players,
        }

    def _resolve_names(self, pd_base: str, headers: dict, puuids: list, region: str) -> dict:
        """Resuelve los Nombre#TAG reales incluso si tienen Modo Incognito activado."""
        out = {}
        missing_puuids = []

        for u in puuids:
            if u in self.puuid_name_cache:
                out[u] = self.puuid_name_cache[u]
            else:
                missing_puuids.append(u)

        if missing_puuids:
            try:
                r = self.session.put(f"{pd_base}/name-service/v2/players", headers=headers, json=missing_puuids, timeout=4)
                if r.status_code == 200:
                    for item in r.json():
                        sub = item.get("Subject")
                        gname = (item.get("GameName") or "").strip()
                        gtag = (item.get("TagLine") or "").strip()
                        if sub and gname and gtag:
                            info = {"name": gname, "tag": gtag}
                            out[sub] = info
                            self.puuid_name_cache[sub] = info
            except Exception:
                pass

        # Si algun jugador en Modo Incognito devolvio nombre vacio en Pregame, lo resolvemos por PUUID
        still_missing = [u for u in puuids if u not in out]
        for u in still_missing:
            resolved = self._resolve_single_puuid_fallback(u, region)
            if resolved:
                out[u] = resolved
                self.puuid_name_cache[u] = resolved

        return out

    def _resolve_single_puuid_fallback(self, puuid: str, region: str) -> dict | None:
        # Intento 1: Via proxy (si el proxy permite by-puuid)
        for ep in (f"/valorant/v1/by-puuid/account/{puuid}", f"/valorant/v2/by-puuid/mmr/{region}/{puuid}"):
            r = self._call_proxy(ep, timeout=6)
            if r and r.status_code == 200:
                d = r.json().get("data", {})
                if d.get("name") and d.get("tag"):
                    return {"name": d["name"], "tag": d["tag"]}
        return None

    def _enrich_live_player(self, p_obj: dict, names_map: dict, region: str, my_puuid: str) -> dict:
        puuid = p_obj.get("Subject", "")
        identity = p_obj.get("PlayerIdentity", {})
        incognito = identity.get("Incognito", False)
        account_lvl = identity.get("AccountLevel", 0)

        char_id = (p_obj.get("CharacterID") or "").lower()
        ag_info = self.agents_by_uuid.get(char_id, {"name": "Seleccionando...", "icon": ""})

        nm = names_map.get(puuid)
        has_valid_name = bool(nm and nm.get("name") and nm.get("tag"))
        if not has_valid_name:
            nm = {"name": f"Jugador Oculto ({ag_info['name']})", "tag": "OCULTO"}

        riot_id = f"{nm['name']}#{nm['tag']}"

        if has_valid_name:
            cached = self.player_stats_cache.get(riot_id.lower())
            if cached and (time.time() - cached[0] < 300):
                stats_info = cached[1]
            else:
                stats_info = self._fetch_player_summary_via_proxy(region, nm["name"], nm["tag"])
                self.player_stats_cache[riot_id.lower()] = (time.time(), stats_info)
        else:
            stats_info = {
                "rank": "Unranked", "rank_icon": "", "rr": 0,
                "peak_rank": "N/A", "kd": "-", "winrate": "-", "hs": "-", "games": 0
            }

        return {
            "puuid": puuid,
            "is_me": (puuid == my_puuid),
            "name": nm["name"],
            "tag": nm["tag"],
            "riot_id": riot_id if has_valid_name else "",
            "team": p_obj.get("TeamID", "Blue"),
            "agent_name": ag_info["name"],
            "agent_icon": ag_info["icon"],
            "incognito": incognito,
            "level": account_lvl,
            **stats_info,
        }

    def _fetch_player_summary_via_proxy(self, region: str, name: str, tag: str) -> dict:
        default_stats = {
            "rank": "Unranked", "rank_icon": "", "rr": 0,
            "peak_rank": "N/A", "kd": "-", "winrate": "-", "hs": "-", "games": 0,
        }
        try:
            data = self.search_player_profile(region, name, tag, "competitive")
            if "error" in data:
                return default_stats
            s = data.get("summary", {})
            return {
                "rank": s.get("rank", "Unranked"),
                "rank_icon": s.get("rank_icon", ""),
                "rr": s.get("rr", 0),
                "peak_rank": s.get("peak", "N/A"),
                "kd": s.get("kd", "-"),
                "winrate": s.get("winrate", "-"),
                "hs": s.get("hs", "-"),
                "games": s.get("games", 0),
            }
        except Exception:
            return default_stats

    ALL_REGIONS = ("latam", "na", "br", "eu", "ap", "kr")

    def _detect_region(self, name: str, tag: str):
        """Detecta la region de una cuenta sin que el usuario la elija. Devuelve (region, error)."""
        safe_name = quote(name, safe="")
        safe_tag = quote(tag, safe="")

        r = self._call_proxy(f"/valorant/v1/account/{safe_name}/{safe_tag}", 12)
        if r is not None:
            try:
                j = r.json()
            except Exception:
                j = {}
            if r.status_code == 200:
                reg = ((j.get("data") or {}).get("region") or "").lower()
                if reg:
                    return reg, None
            elif isinstance(j, dict) and j.get("error") and r.status_code in (403, 500):
                err = str(j.get("error"))
                if "Deployment Protection" in err or "VALORANT_API_KEY" in err:
                    return None, err

        # Respaldo: probar el MMR en todas las regiones en paralelo
        def probe(reg):
            rr = self._call_proxy(f"/valorant/v2/mmr/{reg}/{safe_name}/{safe_tag}", 10)
            return reg if (rr is not None and rr.status_code == 200) else None

        for found in self.executor.map(probe, self.ALL_REGIONS):
            if found:
                return found, None
        return None, f"No se encontro al jugador '{name}#{tag}'."

    def search_player_profile(self, region: str, name: str, tag: str, mode: str = "all") -> dict:
        name, tag = name.strip(), tag.strip()
        mode = (mode or "all").strip().lower()
        region = (region or "auto").strip().lower()
        if not name or not tag:
            return {"error": "Debes ingresar un Nombre#TAG valido."}

        cache_key = f"{region}:{name.lower()}#{tag.lower()}:{mode}"
        cached = self.profile_cache.get(cache_key)
        if cached and (time.time() - cached[0] < 45):
            return cached[1]

        t0 = time.time()
        safe_name = quote(name, safe="")
        safe_tag = quote(tag, safe="")

        if region in ("", "auto"):
            region_key = f"{name.lower()}#{tag.lower()}"
            region = self.region_cache.get(region_key)
            if not region:
                region, err = self._detect_region(name, tag)
                if not region:
                    return {"error": err or f"No se encontro al jugador '{name}#{tag}'."}
                self.region_cache[region_key] = region

        mmr_ep = f"/valorant/v2/mmr/{region}/{safe_name}/{safe_tag}"
        mode_param = "" if mode == "all" else f"&mode={quote(mode, safe='')}"
        mat_ep = f"/valorant/v3/matches/{region}/{safe_name}/{safe_tag}?size=10{mode_param}"

        f_mmr = self.executor.submit(self._call_proxy, mmr_ep, 15)
        f_mat = self.executor.submit(self._call_proxy, mat_ep, 15)
        r_mmr = f_mmr.result()
        r_mat = f_mat.result()

        if r_mmr is None and r_mat is None:
            return {"error": "No se pudo conectar con el servidor Proxy de Vercel (Revisa que tu proyecto este desplegado)."}

        for resp in (r_mmr, r_mat):
            if resp is not None:
                try:
                    j = resp.json()
                    if isinstance(j, dict) and "error" in j:
                        err_msg = str(j.get("error"))
                        if "Deployment Protection" in err_msg or "VALORANT_API_KEY" in err_msg:
                            return {"error": err_msg}
                except Exception:
                    pass

        mmr_code = r_mmr.status_code if r_mmr is not None else 500
        mat_code = r_mat.status_code if r_mat is not None else 500

        if mmr_code == 404 and mat_code == 404:
            return {"error": f"No se encontro al jugador '{name}#{tag}' en {region.upper()}."}

        mmr_data = r_mmr.json().get("data", {}) if (r_mmr is not None and mmr_code == 200) else {}
        matches_data = r_mat.json().get("data", []) if (r_mat is not None and mat_code == 200) else []

        result = self._format_profile_response(name, tag, region, mmr_data, matches_data, time.time() - t0)
        result["mode"] = mode
        self.profile_cache[cache_key] = (time.time(), result)
        return result

    def _format_profile_response(self, name: str, tag: str, region: str, mmr_data: dict, matches_data: list, elapsed: float) -> dict:
        curr = mmr_data.get("current_data", {})
        high = mmr_data.get("highest_rank", {})
        target_puuid = mmr_data.get("puuid")

        tier_id = curr.get("currenttier") or 0
        tier_obj = self.tiers_by_id.get(tier_id, {})
        rank_str = curr.get("currenttierpatched") or tier_obj.get("name") or "Unranked"
        rank_icon = (curr.get("images") or {}).get("small") or tier_obj.get("icon") or ""
        rr = curr.get("ranking_in_tier") or 0
        mmr_delta = curr.get("mmr_change_to_last_game") or 0

        peak_str = high.get("patched_tier") or "N/A"
        peak_season = (high.get("season") or "").upper()

        tot_games = tot_wins = tot_draws = 0
        tot_k = tot_d = tot_a = 0
        tot_dmg_delta = tot_rounds = tot_hs = tot_shots = tot_score = 0

        formatted_matches = []

        for match in matches_data:
            meta = match.get("metadata", {})
            rounds_played = max(meta.get("rounds_played", 1), 1)
            all_players = match.get("players", {}).get("all_players", [])

            ranked_lobby = sorted(all_players, key=lambda x: x.get("stats", {}).get("score", 0), reverse=True)

            player_obj = None
            placement = 10
            for idx, p in enumerate(ranked_lobby):
                if (target_puuid and p.get("puuid") == target_puuid) or (
                    p.get("name", "").lower() == name.lower() and p.get("tag", "").lower() == tag.lower()
                ):
                    player_obj = p
                    placement = idx + 1
                    break

            if not player_obj:
                continue

            tot_games += 1
            team_key = (player_obj.get("team") or "Red").lower()
            my_team = (match.get("teams") or {}).get(team_key, {})

            rw = my_team.get("rounds_won", 0)
            rl = my_team.get("rounds_lost", 0)
            has_won = my_team.get("has_won", rw > rl)
            is_draw = (rw == rl and rw > 0)

            if is_draw:
                tot_draws += 1
            elif has_won:
                tot_wins += 1

            st = player_obj.get("stats", {})
            k, d, a = st.get("kills", 0), st.get("deaths", 0), st.get("assists", 0)
            score = st.get("score", 0)
            hs = st.get("headshots", 0)
            shots = hs + st.get("bodyshots", 0) + st.get("legshots", 0)

            dmg_made = player_obj.get("damage_made", 0)
            dmg_recv = player_obj.get("damage_received", 0)
            dd_delta = round((dmg_made - dmg_recv) / rounds_played)
            acs = round(score / rounds_played)
            hs_pct = round((hs / shots) * 100) if shots > 0 else 0
            kd = round(k / max(d, 1), 2)

            tot_k += k
            tot_d += d
            tot_a += a
            tot_dmg_delta += (dmg_made - dmg_recv)
            tot_rounds += rounds_played
            tot_hs += hs
            tot_shots += shots
            tot_score += score

            mk = player_obj.get("multikills")
            if not mk and "rounds" in match:
                mk = {"3k": 0, "4k": 0, "5k": 0}
                for r in match.get("rounds", []):
                    for ps in r.get("player_stats", []):
                        if ps.get("player_puuid") == player_obj.get("puuid"):
                            kc = len(ps.get("kill_events") or [])
                            if kc == 3:
                                mk["3k"] += 1
                            elif kc == 4:
                                mk["4k"] += 1
                            elif kc >= 5:
                                mk["5k"] += 1
            mk = mk or {"3k": 0, "4k": 0, "5k": 0}

            map_name = meta.get("map", "Mapa")
            map_assets = self.maps_by_url.get(map_name.lower(), {"banner": "", "splash": ""})
            ag_name = player_obj.get("character", "Agente")
            ag_icon = (
                player_obj.get("agent_icon")
                or (player_obj.get("assets") or {}).get("agent", {}).get("small")
                or self.agents_by_name.get(ag_name.lower(), "")
            )

            p_tier = player_obj.get("currenttier") or 0
            p_tier_icon = self.tiers_by_id.get(p_tier, {}).get("icon", "")

            lobby_list = []
            for lp in ranked_lobby:
                lst = lp.get("stats", {})
                lk, ld, la = lst.get("kills", 0), lst.get("deaths", 0), lst.get("assists", 0)
                lhs = lst.get("headshots", 0)
                lshots = lhs + lst.get("bodyshots", 0) + lst.get("legshots", 0)
                l_ag = lp.get("character", "?")
                l_tier = lp.get("currenttier") or 0
                lobby_list.append({
                    "name": lp.get("name", "Desconocido"),
                    "tag": lp.get("tag", "???"),
                    "riot_id": f"{lp.get('name', 'Desconocido')}#{lp.get('tag', '???')}",
                    "team": lp.get("team", "Red"),
                    "agent": l_ag,
                    "agent_icon": lp.get("agent_icon") or (lp.get("assets") or {}).get("agent", {}).get("small") or self.agents_by_name.get(l_ag.lower(), ""),
                    "rank": lp.get("currenttier_patched", "Unranked"),
                    "rank_icon": self.tiers_by_id.get(l_tier, {}).get("icon", ""),
                    "k": lk, "d": ld, "a": la,
                    "kd": f"{lk / max(ld, 1):.2f}",
                    "dda": round((lp.get("damage_made", 0) - lp.get("damage_received", 0)) / rounds_played),
                    "hs": round((lhs / lshots) * 100) if lshots > 0 else 0,
                    "acs": round(lst.get("score", 0) / rounds_played),
                })

            epoch = meta.get("game_start", 0)
            formatted_matches.append({
                "match_id": meta.get("matchid", ""),
                "map": map_name,
                "map_banner": map_assets.get("banner") or map_assets.get("splash") or "",
                "mode": meta.get("mode", "Competitive"),
                "epoch": epoch,
                "day_label": time.strftime("%b %d", time.localtime(epoch)) if epoch else "Recientes",
                "agent": ag_name,
                "agent_icon": ag_icon,
                "rank": player_obj.get("currenttier_patched", "Unranked"),
                "rank_icon": p_tier_icon,
                "placement": placement,
                "has_won": has_won,
                "is_draw": is_draw,
                "rw": rw,
                "rl": rl,
                "k": k, "d": d, "a": a,
                "kd": kd,
                "dda": dd_delta,
                "hs": hs_pct,
                "acs": acs,
                "shots": shots,
                "hs_hits": hs,
                "dmg_diff": dmg_made - dmg_recv,
                "rounds": rounds_played,
                "score": score,
                "multikills": mk,
                "lobby": lobby_list,
            })

        losses = tot_games - tot_wins - tot_draws
        wr = f"{(tot_wins / tot_games) * 100:.1f}%" if tot_games > 0 else "N/A"
        ov_kd = f"{tot_k / max(tot_d, 1):.2f}" if tot_games > 0 else "N/A"
        ov_dda = round(tot_dmg_delta / max(tot_rounds, 1)) if tot_games > 0 else 0
        ov_acs = round(tot_score / max(tot_rounds, 1)) if tot_games > 0 else 0
        ov_hs = f"{(tot_hs / tot_shots * 100):.0f}%" if tot_shots > 0 else "0%"

        return {
            "name": name,
            "tag": tag,
            "region": region,
            "elapsed": round(elapsed, 2),
            "summary": {
                "rank": rank_str,
                "rank_icon": rank_icon,
                "rr": rr,
                "mmr_delta": mmr_delta,
                "peak": peak_str,
                "peak_season": peak_season,
                "winrate": wr,
                "wins": tot_wins,
                "losses": losses,
                "draws": tot_draws,
                "games": tot_games,
                "kd": ov_kd,
                "kda_totals": f"{tot_k}K / {tot_d}D / {tot_a}A",
                "dda": ov_dda,
                "acs": ov_acs,
                "hs": ov_hs,
            },
            "matches": formatted_matches,
        }


ENGINE = ValorantLiveEngine()


# ---------------- INTERFAZ HTML5 + CSS3 + ICONOS SVG ---------------- #
HTML_UI = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<title>VALORANT TRACKER PRO</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@500;600;700;800;900&display=swap" rel="stylesheet">
<style>
  :root {
    --bg-main: #0a0e14;
    --bg-panel: #111722;
    --bg-card: #161f2e;
    --border: #222e42;
    --accent: #ff4655;
    --win: #10b981;
    --loss: #ff4655;
    --gold: #f59e0b;
    --cyan: #38bdf8;
    --text: #f8fafc;
    --muted: #8b9bb4;
  }
  * {
    box-sizing: border-box; margin: 0; padding: 0;
    font-family: 'Inter', 'Segoe UI', system-ui, sans-serif;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
    text-rendering: geometricPrecision;
  }
  body { background: var(--bg-main); color: var(--text); overflow-x: hidden; user-select: none; }

  .navbar {
    display: flex; align-items: center; justify-content: space-between;
    background: rgba(17, 23, 34, 0.96); border-bottom: 1px solid var(--border);
    padding: 12px 24px; position: sticky; top: 0; z-index: 50; backdrop-filter: blur(10px);
  }
  .brand { display: flex; align-items: center; gap: 10px; font-size: 18px; font-weight: 900; letter-spacing: 0.6px; }
  .brand-mark { width: 12px; height: 12px; background: var(--accent); transform: rotate(45deg); border-radius: 2px; }
  .brand span { color: var(--accent); }
  .nav-tabs { display: flex; gap: 6px; background: #0b0f17; padding: 4px; border-radius: 9px; border: 1px solid var(--border); }
  .nav-tab {
    padding: 8px 16px; border-radius: 6px; border: none; cursor: pointer;
    font-weight: 700; font-size: 12.5px; color: var(--muted); background: transparent;
    transition: all 0.18s; display: flex; align-items: center; gap: 8px;
  }
  .nav-tab.active { background: var(--accent); color: #fff; }
  .dot-live { width: 7px; height: 7px; border-radius: 50%; background: var(--win); box-shadow: 0 0 8px var(--win); }

  .container { max-width: 1120px; margin: 18px auto; padding: 0 20px 40px; }

  .search-panel {
    background: var(--bg-panel); border: 1px solid var(--border); border-radius: 10px;
    padding: 16px 18px; position: relative; margin-bottom: 14px;
  }
  .search-row { display: flex; align-items: center; gap: 10px; }
  .mode-filter { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin: 0 4px 14px; }
  .mf-label { font-size: 11px; font-weight: 800; letter-spacing: 1px; color: var(--muted); margin-right: 4px; }
  .mf-chip { background: #111a2b; color: #cbd5e1; border: 1px solid #1e293b; border-radius: 999px; padding: 6px 14px; font-size: 12px; font-weight: 700; cursor: pointer; transition: all .15s ease; font-family: inherit; }
  .mf-chip:hover { border-color: #ff4655; color: #fff; transform: translateY(-1px); }
  .mf-chip.active { background: #ff4655; border-color: #ff4655; color: #fff; }
  .find-title { font-size: 16px; font-weight: 900; letter-spacing: 0.5px; margin-right: 6px; }
  select.ctrl {
    background: #192231; color: #fff; border: 1px solid #2b3952; border-radius: 7px;
    padding: 9px 12px; font-weight: 700; font-size: 12.5px; outline: none; cursor: pointer;
  }
  .input-wrap {
    flex: 1; display: flex; align-items: center; background: #161e2c;
    border: 1.5px solid var(--accent); border-radius: 7px; padding: 3px 10px;
  }
  .riot-badge {
    background: #263246; color: #cbd5e1; font-size: 10px; font-weight: 800;
    padding: 4px 7px; border-radius: 4px; margin-right: 10px; letter-spacing: 0.4px;
  }
  .input-wrap input {
    flex: 1; background: transparent; border: none; color: #fff;
    font-size: 13.5px; font-weight: 600; outline: none; padding: 6px 0;
  }
  .clear-btn { background: transparent; border: none; color: var(--muted); cursor: pointer; font-size: 13px; font-weight: 800; padding: 0 6px; }
  .btn-primary {
    background: var(--accent); color: #fff; border: none; border-radius: 7px;
    padding: 10px 18px; font-weight: 800; font-size: 12.5px; cursor: pointer; transition: 0.15s;
  }
  .btn-primary:hover { background: #e03543; }
  .btn-primary:disabled { opacity: 0.5; cursor: default; }

  .dropdown {
    display: none; margin-top: 10px; background: #141c29; border: 1px solid #28364d;
    border-radius: 8px; padding: 10px;
  }
  .dropdown.open { display: block; }
  .dd-tabs { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; background: #0c111a; padding: 4px; border-radius: 6px; margin-bottom: 8px; }
  .dd-tab {
    background: transparent; border: none; color: var(--muted); padding: 6px;
    font-weight: 700; font-size: 12px; border-radius: 5px; cursor: pointer;
  }
  .dd-tab.active { background: var(--accent); color: #fff; }
  .dd-item {
    display: flex; align-items: center; justify-content: space-between;
    padding: 7px 12px; border-radius: 6px; background: #1a2434; margin-bottom: 4px;
  }
  .tag-pill { background: #0c111a; color: var(--muted); font-size: 11px; font-weight: 700; padding: 2px 6px; border-radius: 4px; margin-left: 6px; }
  .fav-btn {
    background: #232f44; border: 1px solid #33435e; color: var(--muted);
    font-size: 10px; font-weight: 800; padding: 4px 8px; border-radius: 4px; cursor: pointer; margin-right: 6px;
  }
  .fav-btn.active { background: rgba(245, 158, 11, 0.18); color: var(--gold); border-color: var(--gold); }

  .stats-grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 10px; margin-bottom: 16px; }
  .stat-card {
    background: var(--bg-panel); border: 1px solid var(--border); border-radius: 9px;
    padding: 12px 14px; display: flex; align-items: center; gap: 10px;
  }
  .stat-card img { width: 38px; height: 38px; object-fit: contain; }
  .stat-label { font-size: 10px; font-weight: 800; color: var(--muted); text-transform: uppercase; letter-spacing: 0.4px; }
  .stat-val { font-size: 16.5px; font-weight: 800; margin: 2px 0; }
  .stat-sub { font-size: 11px; color: var(--muted); font-weight: 600; }

  .day-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 6px 8px; border-bottom: 1px solid #1a2333; margin-top: 8px;
  }
  .day-left { display: flex; align-items: center; gap: 10px; font-size: 15px; font-weight: 800; }
  .day-count { background: #212c40; color: #cbd5e1; font-size: 11.5px; padding: 2px 7px; border-radius: 4px; }
  .day-wl { font-size: 13.5px; font-weight: 800; margin-left: 6px; }
  .day-right { display: flex; gap: 20px; font-size: 11.5px; color: var(--muted); font-weight: 700; }
  .day-right b { color: #fff; font-size: 13.5px; display: block; text-align: right; }

  .match-card {
    position: relative; height: 74px; border-radius: 8px; overflow: hidden;
    margin: 6px 0; border: 1px solid #243247; cursor: pointer;
    display: flex; align-items: center; justify-content: space-between;
    padding: 0 18px 0 16px; transition: border-color 0.12s;
    background-size: cover; background-position: center;
  }
  .match-card:hover { border-color: #4b5e7a; }
  .match-card.win::before {
    content: ""; position: absolute; inset: 0;
    background: linear-gradient(90deg, rgba(14, 58, 47, 0.92) 0%, rgba(14, 44, 36, 0.72) 42%, rgba(13, 19, 29, 0.94) 100%);
    z-index: 1;
  }
  .match-card.loss::before {
    content: ""; position: absolute; inset: 0;
    background: linear-gradient(90deg, rgba(54, 20, 30, 0.92) 0%, rgba(34, 20, 30, 0.75) 42%, rgba(13, 19, 29, 0.94) 100%);
    z-index: 1;
  }
  .match-card.draw::before {
    content: ""; position: absolute; inset: 0;
    background: linear-gradient(90deg, rgba(40, 48, 62, 0.92) 0%, rgba(18, 24, 36, 0.94) 100%);
    z-index: 1;
  }
  .side-bar { position: absolute; left: 0; top: 0; bottom: 0; width: 4px; z-index: 3; }
  .match-card.win .side-bar { background: var(--win); }
  .match-card.loss .side-bar { background: var(--loss); }
  .match-card.draw .side-bar { background: var(--gold); }

  .mc-content { position: relative; z-index: 2; display: flex; align-items: center; width: 100%; justify-content: space-between; }
  .mc-left { display: flex; align-items: center; gap: 13px; width: 245px; }
  .agent-ava { width: 50px; height: 50px; border-radius: 6px; background: #1b2536; object-fit: cover; border: 1px solid rgba(255,255,255,0.12); }
  .mc-time { font-size: 11px; color: #cbd5e1; font-weight: 600; }
  .mc-map-row { display: flex; align-items: center; gap: 8px; margin-top: 2px; }
  .mc-map { font-size: 17px; font-weight: 800; color: #fff; }

  .badge { font-size: 10.5px; font-weight: 800; padding: 2px 7px; border-radius: 4px; display: inline-block; }
  .badge-mvp { background: #eab308; color: #0f172a; }
  .badge-pos { background: rgba(45, 58, 76, 0.85); color: #cbd5e1; border: 1px solid #475569; }
  .badge-mk { background: rgba(42, 54, 46, 0.9); color: #facc15; border: 1px solid #a16207; margin-right: 5px; }

  .mc-score { width: 88px; text-align: center; }
  .mc-score .lbl { font-size: 10.5px; color: var(--muted); font-weight: 600; }
  .mc-score .val { font-size: 16.5px; font-weight: 800; margin-top: 2px; }

  .mc-rank { display: flex; align-items: center; gap: 8px; width: 145px; }
  .mc-rank img { width: 30px; height: 30px; object-fit: contain; }

  .mc-badges { flex: 1; display: flex; align-items: center; padding-left: 8px; }
  .mc-stats { display: flex; align-items: center; gap: 20px; text-align: right; }
  .mc-stat .lbl { font-size: 10.5px; color: var(--muted); font-weight: 600; }
  .mc-stat .val { font-size: 15.5px; font-weight: 800; margin-top: 2px; }

  .live-banner {
    background: var(--bg-panel); border: 1px solid var(--border); border-radius: 10px;
    padding: 18px 20px; margin-bottom: 14px; display: flex; align-items: center; justify-content: space-between;
    background-size: cover; background-position: center; position: relative; overflow: hidden;
  }
  .live-banner::before {
    content: ""; position: absolute; inset: 0;
    background: linear-gradient(90deg, rgba(14,19,29,0.95) 0%, rgba(14,19,29,0.78) 100%); z-index: 1;
  }
  .live-banner > * { position: relative; z-index: 2; }
  .team-box { background: var(--bg-panel); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; margin-bottom: 14px; }
  .team-hdr { padding: 9px 16px; font-weight: 800; font-size: 12px; letter-spacing: 0.5px; }
  .team-hdr.blue { background: rgba(16, 185, 129, 0.14); color: #34d399; border-bottom: 1px solid rgba(16, 185, 129, 0.28); }
  .team-hdr.red { background: rgba(255, 70, 85, 0.14); color: #f87171; border-bottom: 1px solid rgba(255, 70, 85, 0.28); }
  .p-row {
    display: flex; align-items: center; justify-content: space-between;
    padding: 10px 16px; border-bottom: 1px solid #1b2434;
  }
  .p-row:last-child { border-bottom: none; }
  .incog-tag {
    background: rgba(255, 70, 85, 0.16); color: #ff6b78; border: 1px solid rgba(255, 70, 85, 0.4);
    font-size: 9.5px; font-weight: 800; padding: 2px 6px; border-radius: 4px; margin-left: 8px; letter-spacing: 0.4px;
  }

  .modal-bg {
    display: none; position: fixed; inset: 0; background: rgba(5, 8, 14, 0.84);
    z-index: 100; align-items: center; justify-content: center; backdrop-filter: blur(4px);
  }
  .modal-bg.open { display: flex; }
  .modal {
    background: var(--bg-panel); border: 1px solid #2e3d56; border-radius: 12px;
    width: 920px; max-height: 86vh; overflow-y: auto; padding: 20px;
  }
</style>
</head>
<body>

<div class="navbar">
  <div class="brand"><div class="brand-mark"></div> VALORANT <span>TRACKER PRO</span></div>
  <div class="nav-tabs">
    <button class="nav-tab active" id="tabBtnLive" onclick="switchMainView('live')">
      <div class="dot-live"></div> PARTIDA EN VIVO
    </button>
    <button class="nav-tab" id="tabBtnSearch" onclick="switchMainView('search')">
      BUSCAR JUGADOR E HISTORIAL
    </button>
  </div>
  <div style="font-size:12px; font-weight:600; color:var(--muted);">RADAR ACTIVO</div>
</div>

<div class="container">

  <div id="viewLive">
    <div class="live-banner" id="liveHeaderBanner">
      <div>
        <div style="font-size:11px; font-weight:800; color:var(--cyan); letter-spacing:0.6px;" id="liveStateBadge">ESCANEANDO CLIENTE DE VALORANT</div>
        <div style="font-size:22px; font-weight:900; margin-top:4px;" id="liveTitle">Esperando partida en curso</div>
        <div style="font-size:12.5px; color:var(--muted); margin-top:3px;" id="liveSubtitle">Al entrar a Seleccion de Agente o cargar el mapa, se listaran los jugadores con sus rangos y nombres reales.</div>
      </div>
      <button class="btn-primary" onclick="pollLiveMatch(true)">Actualizar Ahora</button>
    </div>

    <div id="liveTeamsContainer"></div>
  </div>

  <div id="viewSearch" style="display:none;">
    <div class="search-panel">
      <div class="search-row">
        <div class="find-title">FIND PLAYERS</div>
        <input type="hidden" id="selRegion" value="auto">
        <input type="hidden" id="selMode" value="all">
        <div class="input-wrap">
          <span class="riot-badge">RIOT ID</span>
          <input type="text" id="inpRiotId" placeholder="Ingresa Nombre#TAG (ej. EL LUIS#1069)..."
                 onfocus="openDropdown()" oninput="onSearchTyping()" onkeydown="if(event.key==='Enter') runProfileSearch()">
          <button class="clear-btn" onclick="clearInput()">X</button>
        </div>
        <button class="btn-primary" id="btnSearch" onclick="runProfileSearch()">Buscar Perfil</button>
      </div>

      <div class="dropdown" id="searchDropdown">
        <div class="dd-tabs">
          <button class="dd-tab active" id="ddTab_recent" onclick="setDdTab('recent')">RECIENTES</button>
          <button class="dd-tab" id="ddTab_favorites" onclick="setDdTab('favorites')">FAVORITOS</button>
          <button class="dd-tab" id="ddTab_results" onclick="setDdTab('results')">RESULTADOS</button>
        </div>
        <div id="ddList"></div>
        <div style="text-align:center; margin-top:6px;">
          <button onclick="closeDropdown()" style="background:transparent; border:none; color:var(--muted); font-size:11px; font-weight:700; cursor:pointer;">OCULTAR LISTA</button>
        </div>
      </div>
    </div>

    <div id="searchStatus" style="font-size:12.5px; font-weight:600; color:var(--muted); margin: 6px 4px 12px;">
      Ingresa cualquier Nombre#TAG o selecciona "Ver Historial" desde la pestaña de partida en vivo.
    </div>

    <div class="mode-filter" id="modeFilter" style="display:none;"></div>

    <div class="stats-grid" id="summaryGrid" style="display:none;">
      <div class="stat-card">
        <img id="sumRankImg" src="" style="display:none;">
        <div><div class="stat-label">Rango Actual</div><div class="stat-val" id="sumRank" style="color:var(--win)">-</div><div class="stat-sub" id="sumRR"></div></div>
      </div>
      <div class="stat-card">
        <div><div class="stat-label">Peak Rank</div><div class="stat-val" id="sumPeak" style="color:var(--gold)">-</div><div class="stat-sub" id="sumPeakSub"></div></div>
      </div>
      <div class="stat-card">
        <div><div class="stat-label">Winrate</div><div class="stat-val" id="sumWR">-</div><div class="stat-sub" id="sumWL"></div></div>
      </div>
      <div class="stat-card">
        <div><div class="stat-label">K/D Ratio</div><div class="stat-val" id="sumKD">-</div><div class="stat-sub" id="sumKDA"></div></div>
      </div>
      <div class="stat-card">
        <div><div class="stat-label">DDΔ / Ronda</div><div class="stat-val" id="sumDDA">-</div><div class="stat-sub" id="sumACS"></div></div>
      </div>
      <div class="stat-card">
        <div><div class="stat-label">Headshot %</div><div class="stat-val" id="sumHS" style="color:var(--cyan)">-</div><div class="stat-sub">Precision</div></div>
      </div>
    </div>

    <div id="matchesContainer"></div>
  </div>

</div>

<div class="modal-bg" id="inspectorModal" onclick="if(event.target===this) closeInspector()">
  <div class="modal">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:14px;">
      <div>
        <div style="font-size:17px; font-weight:900;" id="modalTitle">INSPECTOR DE SALA</div>
        <div style="font-size:12px; color:var(--muted);">Estadisticas completas y Riot ID reales de los 10 jugadores.</div>
      </div>
      <button class="btn-primary" style="background:#2b3952;" onclick="closeInspector()">Cerrar</button>
    </div>
    <div id="modalPlayersList"></div>
  </div>
</div>

<script>
let ddActiveTab = 'recent';
let currentMatches = [];
let lastLiveMatchId = null;

function getHistoryStore() {
  try { return JSON.parse(localStorage.getItem('val_tracker_store') || '{"recent":[],"favorites":[]}'); }
  catch { return {recent:[], favorites:[]}; }
}
function saveHistoryStore(store) {
  localStorage.setItem('val_tracker_store', JSON.stringify(store));
}
function addRecentPlayer(riot_id, region) {
  let st = getHistoryStore();
  st.recent = st.recent.filter(x => x.riot_id.toLowerCase() !== riot_id.toLowerCase());
  st.recent.unshift({riot_id, region});
  st.recent = st.recent.slice(0, 10);
  saveHistoryStore(st);
}
function toggleFavPlayer(riot_id, region) {
  let st = getHistoryStore();
  let idx = st.favorites.findIndex(x => x.riot_id.toLowerCase() === riot_id.toLowerCase());
  if (idx >= 0) st.favorites.splice(idx, 1);
  else st.favorites.unshift({riot_id, region});
  saveHistoryStore(st);
  renderDropdown();
}

function switchMainView(view) {
  document.getElementById('viewLive').style.display = (view === 'live') ? 'block' : 'none';
  document.getElementById('viewSearch').style.display = (view === 'search') ? 'block' : 'none';
  document.getElementById('tabBtnLive').classList.toggle('active', view === 'live');
  document.getElementById('tabBtnSearch').classList.toggle('active', view === 'search');
}

function openDropdown() {
  document.getElementById('searchDropdown').classList.add('open');
  renderDropdown();
}
function closeDropdown() {
  document.getElementById('searchDropdown').classList.remove('open');
}
function clearInput() {
  document.getElementById('inpRiotId').value = '';
  setDdTab('recent');
}
function setDdTab(t) {
  ddActiveTab = t;
  ['recent','favorites','results'].forEach(k => {
    document.getElementById('ddTab_' + k).classList.toggle('active', k === t);
  });
  renderDropdown();
}
function onSearchTyping() {
  let v = document.getElementById('inpRiotId').value.trim();
  setDdTab(v ? 'results' : 'recent');
  openDropdown();
}
function renderDropdown() {
  let st = getHistoryStore();
  let q = document.getElementById('inpRiotId').value.trim().toLowerCase();
  let items = [];
  if (ddActiveTab === 'recent') items = st.recent;
  else if (ddActiveTab === 'favorites') items = st.favorites;
  else {
    let pool = [...st.favorites, ...st.recent];
    let seen = new Set();
    pool.forEach(x => {
      if (!seen.has(x.riot_id.toLowerCase()) && x.riot_id.toLowerCase().includes(q)) {
        seen.add(x.riot_id.toLowerCase());
        items.push(x);
      }
    });
    if (q.includes('#') && !seen.has(q)) {
      items.unshift({riot_id: document.getElementById('inpRiotId').value.trim(), region: 'auto'});
    }
  }

  let box = document.getElementById('ddList');
  if (!items.length) {
    box.innerHTML = `<div style="padding:12px; color:var(--muted); font-size:12px; text-align:center;">Sin jugadores guardados en esta vista.</div>`;
    return;
  }
  box.innerHTML = items.map(it => {
    let [n, tg] = it.riot_id.split('#');
    let isFav = st.favorites.some(f => f.riot_id.toLowerCase() === it.riot_id.toLowerCase());
    let safeRid = it.riot_id.replace(/'/g, "\\'");
    return `<div class="dd-item">
      <div style="display:flex; align-items:center;">
        <span style="font-weight:800; font-size:13.5px;">${n}</span>
        ${tg ? `<span class="tag-pill">#${tg}</span>` : ''}
        <span style="font-size:10px; color:var(--muted); margin-left:8px; font-weight:800;">${(it.region||'latam').toUpperCase()}</span>
      </div>
      <div>
        <button class="fav-btn ${isFav ? 'active' : ''}" onclick="toggleFavPlayer('${safeRid}','${it.region||'latam'}')">${isFav ? 'FAVORITO' : '+ FAV'}</button>
        <button class="btn-primary" style="padding:5px 12px; font-size:11px;" onclick="loadPlayerFromList('${safeRid}','${it.region||'latam'}')">Ver Perfil</button>
      </div>
    </div>`;
  }).join('');
}

function loadPlayerFromList(riotId, region) {
  if (!riotId || !riotId.includes('#')) return;
  switchMainView('search');
  document.getElementById('selRegion').value = 'auto';
  document.getElementById('inpRiotId').value = riotId;
  closeDropdown();
  runProfileSearch(true);
}

const MODE_OPTIONS = [
  ['all', 'Todos'],
  ['competitive', 'Competitivo'],
  ['swiftplay', 'Swiftplay'],
  ['unrated', 'Normal'],
  ['spikerush', 'Spike Rush'],
  ['deathmatch', 'Deathmatch'],
  ['escalation', 'Escalation'],
  ['replication', 'Replication'],
];

function renderModeFilter() {
  let cur = document.getElementById('selMode').value;
  let box = document.getElementById('modeFilter');
  box.style.display = 'flex';
  box.innerHTML = `<span class="mf-label">MODO</span>` + MODE_OPTIONS.map(([v, label]) =>
    `<button class="mf-chip ${v === cur ? 'active' : ''}" onclick="setModeFilter('${v}')">${label}</button>`
  ).join('');
}

function setModeFilter(mode) {
  document.getElementById('selMode').value = mode;
  renderModeFilter();
  runProfileSearch(false);
}

function timeAgo(epoch) {
  if (!epoch) return 'Reciente';
  let diff = Math.max(Math.floor(Date.now()/1000 - epoch), 0);
  if (diff < 3600) return Math.max(Math.floor(diff/60), 1) + 'm ago';
  if (diff < 86400) return Math.floor(diff/3600) + 'h ago';
  return Math.floor(diff/86400) + 'd ago';
}

async function runProfileSearch(resetMode = true) {
  closeDropdown();
  if (resetMode) {
    document.getElementById('selMode').value = 'all';
    document.getElementById('selRegion').value = 'auto';
    document.getElementById('modeFilter').style.display = 'none';
  }
  let rid = document.getElementById('inpRiotId').value.trim();
  if (!rid.includes('#')) {
    document.getElementById('searchStatus').innerHTML = `<span style="color:var(--loss)">Formato invalido. Debes incluir el #TAG (ej. EL LUIS#1069)</span>`;
    return;
  }
  let [name, tag] = rid.split('#');
  let region = document.getElementById('selRegion').value;
  let mode = document.getElementById('selMode').value;

  addRecentPlayer(`${name.trim()}#${tag.trim()}`, 'auto');

  let btn = document.getElementById('btnSearch');
  btn.disabled = true; btn.innerText = 'Consultando...';
  document.getElementById('searchStatus').innerHTML = `<span style="color:var(--cyan)">Buscando a ${name}#${tag} en todas las regiones...${mode !== 'all' ? ' (filtro: ' + mode + ')' : ''}</span>`;
  document.getElementById('matchesContainer').innerHTML = '';

  try {
    let r = await fetch(`/api/profile?region=${encodeURIComponent(region)}&name=${encodeURIComponent(name.trim())}&tag=${encodeURIComponent(tag.trim())}&mode=${encodeURIComponent(mode)}`);
    let data = await r.json();
    btn.disabled = false; btn.innerText = 'Buscar Perfil';

    if (data.error) {
      document.getElementById('searchStatus').innerHTML = `<span style="color:var(--loss)">${data.error}</span>`;
      return;
    }
    document.getElementById('selRegion').value = data.region || 'auto';
    renderModeFilter();
    renderProfileData(data);
  } catch (e) {
    btn.disabled = false; btn.innerText = 'Buscar Perfil';
    document.getElementById('searchStatus').innerHTML = `<span style="color:var(--loss)">Error de conexion: ${e}</span>`;
  }
}

function renderProfileData(data) {
  let s = data.summary;
  document.getElementById('summaryGrid').style.display = 'grid';
  let rImg = document.getElementById('sumRankImg');
  if (s.rank_icon) { rImg.src = s.rank_icon; rImg.style.display = 'block'; } else { rImg.style.display = 'none'; }

  document.getElementById('sumRank').innerText = s.rank;
  let sign = s.mmr_delta >= 0 ? '+' : '';
  document.getElementById('sumRR').innerText = `${s.rr} RR (${sign}${s.mmr_delta} ult.)`;
  document.getElementById('sumPeak').innerText = s.peak;
  document.getElementById('sumPeakSub').innerText = s.peak_season ? `Acto ${s.peak_season}` : '';
  document.getElementById('sumWR').innerText = s.winrate;
  document.getElementById('sumWL').innerText = `${s.wins}W // ${s.losses}L (${s.games})`;
  document.getElementById('sumKD').innerText = s.kd;
  document.getElementById('sumKDA').innerText = s.kda_totals;
  document.getElementById('sumDDA').innerText = (s.dda > 0 ? '+' : '') + s.dda;
  document.getElementById('sumACS').innerText = `ACS Prom: ${s.acs}`;
  document.getElementById('sumHS').innerText = s.hs;

  currentMatches = data.matches || [];
  document.getElementById('searchStatus').innerHTML = `<span style="color:var(--win)">Perfil de <b>${data.name}#${data.tag}</b> (${data.region.toUpperCase()}) cargado en ${data.elapsed}s</span>`;

  if (!currentMatches.length) {
    document.getElementById('matchesContainer').innerHTML = `<div style="padding:30px; text-align:center; color:var(--muted);">El jugador no registra partidas recientes en este modo.</div>`;
    return;
  }

  let groups = {};
  let order = [];
  currentMatches.forEach((m, idx) => {
    m._idx = idx;
    if (!groups[m.day_label]) { groups[m.day_label] = []; order.push(m.day_label); }
    groups[m.day_label].push(m);
  });

  let html = '';
  order.forEach(day => {
    let list = groups[day];
    let w = list.filter(x => x.has_won && !x.is_draw).length;
    let l = list.filter(x => !x.has_won && !x.is_draw).length;
    let ks = list.reduce((a,b)=>a+b.k, 0), ds = list.reduce((a,b)=>a+b.d, 0), as = list.reduce((a,b)=>a+b.a, 0);
    let rds = Math.max(list.reduce((a,b)=>a+b.rounds, 0), 1);
    let ddaAvg = Math.round(list.reduce((a,b)=>a+b.dmg_diff, 0) / rds);
    let acsAvg = Math.round(list.reduce((a,b)=>a+b.score, 0) / rds);
    let shots = list.reduce((a,b)=>a+b.shots, 0);
    let hsAvg = shots > 0 ? Math.round((list.reduce((a,b)=>a+b.hs_hits, 0) / shots) * 100) : 0;

    html += `<div class="day-header">
      <div class="day-left">
        <span>${day}</span>
        <span class="day-count">${list.length}</span>
        <div class="day-wl"><span style="color:var(--win)">${w} W</span> <span style="color:#475569">//</span> <span style="color:var(--loss)">${l} L</span></div>
      </div>
      <div class="day-right">
        <div>K/D <b>${(ks/Math.max(ds,1)).toFixed(1)}</b></div>
        <div>${ks} K // ${ds} D // ${as} A <b>${((ks+as)/Math.max(ds,1)).toFixed(2)} K/D/A</b></div>
        <div>DDΔ <b>${ddaAvg}</b></div>
        <div>HS% <b>${hsAvg}</b></div>
        <div>ACS <b>${acsAvg}</b></div>
      </div>
    </div>`;

    list.forEach(m => {
      let cls = m.is_draw ? 'draw' : (m.has_won ? 'win' : 'loss');
      let posBadge = m.placement === 1
        ? `<span class="badge badge-mvp">MVP</span>`
        : `<span class="badge badge-pos">${m.placement}${m.placement===2?'nd':(m.placement===3?'rd':'th')}</span>`;

      let mkBadges = '';
      if (m.multikills['5k'] > 0) mkBadges += `<span class="badge badge-mk">Ace</span>`;
      if (m.multikills['4k'] > 0) mkBadges += `<span class="badge badge-mk">4k${m.multikills['4k']>1?' x'+m.multikills['4k']:''}</span>`;
      if (m.multikills['3k'] > 0) mkBadges += `<span class="badge badge-mk">3k${m.multikills['3k']>1?' x'+m.multikills['3k']:''}</span>`;
      if (m.kd >= 1.4 || m.dda >= 45) mkBadges += `<span class="badge badge-mk" style="color:#cbd5e1; border-color:#64748b;">High Impact</span>`;

      let kdCol = m.kd >= 2.0 ? 'var(--cyan)' : (m.kd >= 1.0 ? 'var(--win)' : 'var(--loss)');
      let ddaCol = m.dda > 0 ? 'var(--win)' : (m.dda < 0 ? 'var(--loss)' : '#fff');

      html += `<div class="match-card ${cls}" style="background-image:url('${m.map_banner}')" onclick="openInspector(${m._idx})">
        <div class="side-bar"></div>
        <div class="mc-content">
          <div class="mc-left">
            <img class="agent-ava" src="${m.agent_icon}">
            <div>
              <div class="mc-time">${timeAgo(m.epoch)} // ${m.mode}</div>
              <div class="mc-map-row"><span class="mc-map">${m.map}</span> ${posBadge}</div>
            </div>
          </div>
          <div class="mc-score">
            <div class="lbl">Score</div>
            <div class="val"><span style="color:var(--win)">${m.rw}</span> : <span style="color:var(--loss)">${m.rl}</span></div>
          </div>
          <div class="mc-rank">
            ${m.rank_icon ? `<img src="${m.rank_icon}">` : ''}
            <div>
              <div style="font-size:10px; color:var(--gold); font-weight:800;">${m.agent.toUpperCase()}</div>
              <div style="font-size:13.5px; font-weight:800;">${m.rank}</div>
            </div>
          </div>
          <div class="mc-badges">${mkBadges}</div>
          <div class="mc-stats">
            <div class="mc-stat"><div class="lbl">K/D</div><div class="val" style="color:${kdCol}">${m.kd.toFixed(1)}</div></div>
            <div class="mc-stat" style="width:88px;"><div class="lbl">K / D / A</div><div class="val">${m.k} / ${m.d} / ${m.a}</div></div>
            <div class="mc-stat" style="width:45px;"><div class="lbl">DDΔ</div><div class="val" style="color:${ddaCol}">${m.dda}</div></div>
            <div class="mc-stat" style="width:40px;"><div class="lbl">HS%</div><div class="val">${m.hs}</div></div>
            <div class="mc-stat" style="width:45px;"><div class="lbl">ACS</div><div class="val">${m.acs}</div></div>
          </div>
        </div>
      </div>`;
    });
  });

  document.getElementById('matchesContainer').innerHTML = html;
}

function openInspector(idx) {
  let m = currentMatches[idx];
  if (!m) return;
  document.getElementById('modalTitle').innerText = `SALA DE PARTIDA — ${m.map.toUpperCase()} (${m.rw} : ${m.rl})`;
  let reg = document.getElementById('selRegion').value;

  let blue = m.lobby.filter(p => p.team === 'Blue');
  let red = m.lobby.filter(p => p.team !== 'Blue');

  const renderTeam = (arr, title, cls) => `
    <div class="team-box">
      <div class="team-hdr ${cls}">${title}</div>
      ${arr.map(p => {
        let safeRid = p.riot_id.replace(/'/g, "\\'");
        return `
        <div class="p-row">
          <div style="display:flex; align-items:center; gap:10px; width:280px;">
            <img src="${p.agent_icon}" style="width:34px; height:34px; border-radius:5px; background:#0f172a;">
            <div>
              <div style="font-weight:800; font-size:13.5px;">${p.name} <span class="tag-pill">#${p.tag}</span></div>
              <div style="font-size:11px; color:var(--gold); font-weight:700;">${p.agent}</div>
            </div>
          </div>
          <div style="display:flex; align-items:center; gap:6px; width:140px;">
            ${p.rank_icon ? `<img src="${p.rank_icon}" style="width:24px; height:24px;">` : ''}
            <span style="font-size:12.5px; font-weight:700; color:var(--win);">${p.rank}</span>
          </div>
          <div style="font-size:12.5px; font-weight:700;">${p.k} / ${p.d} /${p.a} <span style="color:var(--muted); margin-left:6px;">(K/D ${p.kd})</span></div>
          <div style="font-size:12px; color:#cbd5e1;">DDΔ: <b>${p.dda}</b> &nbsp;|&nbsp; HS: <b>${p.hs}%</b> &nbsp;|&nbsp; ACS: <b>${p.acs}</b></div>
          <button class="btn-primary" style="padding:6px 12px; font-size:11px;" onclick="closeInspector(); loadPlayerFromList('${safeRid}','${reg}')">Ver Perfil</button>
        </div>`;
      }).join('')}
    </div>`;

  document.getElementById('modalPlayersList').innerHTML = renderTeam(blue, 'EQUIPO AZUL', 'blue') + renderTeam(red, 'EQUIPO ROJO', 'red');
  document.getElementById('inspectorModal').classList.add('open');
}
function closeInspector() {
  document.getElementById('inspectorModal').classList.remove('open');
}

async function pollLiveMatch(force = false) {
  try {
    let r = await fetch('/api/live');
    let data = await r.json();

    let badge = document.getElementById('liveStateBadge');
    let title = document.getElementById('liveTitle');
    let sub = document.getElementById('liveSubtitle');
    let banner = document.getElementById('liveHeaderBanner');
    let container = document.getElementById('liveTeamsContainer');

    if (data.status === 'closed' || data.status === 'menu') {
      badge.innerText = data.status === 'closed' ? 'VALORANT CERRADO' : 'VALORANT ACTIVO — EN LOBBY';
      title.innerText = data.status === 'closed' ? 'Esperando a que abras Valorant' : (data.me || 'En el Menu Principal');
      sub.innerText = data.message;
      banner.style.backgroundImage = 'none';
      if (!force && lastLiveMatchId === null) return;
      lastLiveMatchId = null;
      container.innerHTML = '';
      return;
    }

    badge.innerText = data.status === 'pregame' ? 'SELECCION DE AGENTE EN VIVO' : 'PARTIDA EN CURSO DETECTADA';
    title.innerText = `${data.map_name.toUpperCase()} — Servidor ${data.server}`;
    sub.innerText = `Mostrando datos y rangos de los jugadores de tu sala en tiempo real.`;
    if (data.map_splash) banner.style.backgroundImage = `url('${data.map_splash}')`;

    let players = data.players || [];
    let blue = players.filter(p => p.team === 'Blue');
    let red = players.filter(p => p.team !== 'Blue');

    let isFFA = (players.length > 10) || (blue.length === players.length && players.length > 5);

    const renderLiveTeam = (arr, name, cls) => !arr.length ? '' : `
      <div class="team-box">
        <div class="team-hdr ${cls}">${name} (${arr.length} Jugadores)</div>
        ${arr.map(p => {
          let safeRid = (p.riot_id || '').replace(/'/g, "\\'");
          let safeReg = (data.region || 'latam').replace(/'/g, "\\'");
          return `
          <div class="p-row" style="${p.is_me ? 'background:rgba(56,189,248,0.08);' : ''}">
            <div style="display:flex; align-items:center; gap:12px; width:310px;">
              ${p.agent_icon ? `<img src="${p.agent_icon}" style="width:40px; height:40px; border-radius:6px; background:#0f172a;">` : `<div style="width:40px;height:40px;border-radius:6px;background:#1e293b;"></div>`}
              <div>
                <div style="font-weight:800; font-size:13.5px; display:flex; align-items:center;">
                  <span>${p.name}</span>${p.tag && p.tag !== 'OCULTO' ? `<span class="tag-pill">#${p.tag}</span>` : ''}
                  ${p.incognito ? `<span class="incog-tag">MODO INCOGNITO</span>` : ''}
                </div>
                <div style="font-size:11px; color:var(--gold); font-weight:700; margin-top:2px;">${p.agent_name} &nbsp;•&nbsp; Nivel ${p.level}</div>
              </div>
            </div>
            <div style="display:flex; align-items:center; gap:8px; width:170px;">
              ${p.rank_icon ? `<img src="${p.rank_icon}" style="width:28px; height:28px;">` : ''}
              <div>
                <div style="font-size:13px; font-weight:800; color:var(--win);">${p.rank}${p.rr !== undefined && p.rr !== null ? ` (${p.rr} RR)` : ''}</div>
                <div style="font-size:11px; color:var(--muted);">Peak: ${p.peak_rank}</div>
              </div>
            </div>
            <div style="font-size:12.5px; font-weight:700;">
              K/D: <span style="color:var(--cyan)">${p.kd}</span> &nbsp;|&nbsp;
              Winrate: <span style="color:var(--win)">${p.winrate}</span> &nbsp;|&nbsp;
              HS: <b>${p.hs}</b>
            </div>
            <button class="btn-primary" style="padding:7px 14px; font-size:11px;"
                    ${!safeRid ? 'disabled title="Nombre oculto en partida"' : ''}
                    onclick="loadPlayerFromList('${safeRid}','${safeReg}')">Ver Historial</button>
          </div>`;
        }).join('')}
      </div>`;

    if (isFFA) {
      container.innerHTML = renderLiveTeam(players, 'JUGADORES (TODOS CONTRA TODOS)', 'blue');
    } else {
      container.innerHTML = renderLiveTeam(blue, 'EQUIPO ALIADO / AZUL', 'blue') + renderLiveTeam(red, 'EQUIPO ENEMIGO / ROJO', 'red');
    }
  } catch (e) {}
}

setInterval(() => pollLiveMatch(false), 4000);
pollLiveMatch(true);
</script>
</body>
</html>
"""


class TrackerLocalHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, data: dict, status: int = 200):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            raw = HTML_UI.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        if parsed.path == "/api/live":
            self._send_json(ENGINE.get_live_match_state())
            return

        if parsed.path == "/api/profile":
            q = parse_qs(parsed.query)
            region = q.get("region", ["auto"])[0]
            name = q.get("name", [""])[0]
            tag = q.get("tag", [""])[0]
            mode = q.get("mode", ["all"])[0]
            self._send_json(ENGINE.search_player_profile(region, name, tag, mode))
            return

        self.send_response(404)
        self.end_headers()


APP_WINDOW_WIDTH = 1060
APP_WINDOW_HEIGHT = 880


def _fitted_window_geometry():
    """Tamano justo para la app, centrado y sin exceder la pantalla."""
    width, height = APP_WINDOW_WIDTH, APP_WINDOW_HEIGHT
    screen_w, screen_h = 1920, 1080
    try:
        import ctypes
        user32 = ctypes.windll.user32
        screen_w = user32.GetSystemMetrics(0)
        screen_h = user32.GetSystemMetrics(1)
    except Exception:
        pass
    width = min(width, screen_w - 40)
    height = min(height, screen_h - 80)
    return width, height, max((screen_w - width) // 2, 0), max((screen_h - height) // 2 - 20, 0)


def open_native_app_window(url: str):
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    width, height, pos_x, pos_y = _fitted_window_geometry()
    profile_dir = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "ValorantTrackerApp")
    for browser_path in candidates:
        if os.path.exists(browser_path):
            try:
                subprocess.Popen([
                    browser_path,
                    f"--app={url}",
                    f"--user-data-dir={profile_dir}",
                    f"--window-size={width},{height}",
                    f"--window-position={pos_x},{pos_y}",
                    "--no-first-run",
                    "--no-default-browser-check",
                ])
                return
            except Exception:
                pass
    webbrowser.open(url)


if __name__ == "__main__":
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        while s.connect_ex(("127.0.0.1", PORT)) == 0:
            PORT += 1

    server = ThreadingHTTPServer(("127.0.0.1", PORT), TrackerLocalHandler)
    app_url = f"http://127.0.0.1:{PORT}"
    print(f"Valorant Tracker Pro iniciado en: {app_url}")

    open_native_app_window(app_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass