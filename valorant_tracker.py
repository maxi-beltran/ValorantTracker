"""
Valorant Tracker — App de escritorio (CustomTkinter + requests).

Consulta rango, estadísticas e historial de partidas (cualquier modo de juego)
de cualquier Nombre#TAG a través de un proxy propio en Vercel (la API Key de
HenrikDev vive SOLO en el servidor, nunca en este código ni en GitHub).
"""

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import quote

import customtkinter as ctk
import requests

# ─────────────────────────── Configuración ─────────────────────────── #
# URL de producción del proxy en Vercel (terminada en /api/proxy).
# Esta URL es pública a propósito: NO contiene ningún secreto.
# Si haces un fork y despliegas tu propio proxy, cambia la URL aquí o
# define la variable de entorno VALORANT_PROXY_URL.
PROXY_URL = os.environ.get(
    "VALORANT_PROXY_URL",
    "https://valorant-tracker-proxy.vercel.app/api/proxy",
)

REGIONS = ["latam", "br", "na", "eu", "ap", "kr"]
MATCH_COUNT = 10          # máximo que permite HenrikDev v3 por petición
REQUEST_TIMEOUT = 20      # segundos

# Modos de juego: etiqueta visible -> valor del parámetro "mode" de HenrikDev.
# None = sin filtro (todas las partidas recientes).
GAME_MODES = {
    "Todos los modos": None,
    "Competitivo": "competitive",
    "No competitivo": "unrated",
    "Swiftplay": "swiftplay",
    "Premier": "premier",
    "Deathmatch": "deathmatch",
    "Team Deathmatch": "teamdeathmatch",
    "Spike Rush": "spikerush",
    "Escalada": "escalation",
    "Replicación": "replication",
    "Guerra de nieve": "snowball",
    "Personalizada": "custom",
}
# Inverso (id de la API -> etiqueta) para mostrar el modo de cada partida.
MODE_ID_TO_LABEL = {v: k for k, v in GAME_MODES.items() if v}

# ─────────────────────────── Paleta ─────────────────────────── #
ACCENT = "#ff4655"
ACCENT_HOVER = "#d93846"
GREEN = "#2ecc71"
RED = "#ff4655"
YELLOW = "#f1c40f"
BLUE = "#3498db"
ORANGE = "#f39c12"
PURPLE = "#9b7bff"
GRAY = "gray"
CARD_BG = "#1e1f22"
BTN_BG = "#2b2d31"
BTN_HOVER = "#404249"
ROW_BG = {"win": "#192b23", "loss": "#2e1a1d", "draw": "#26272b", "unknown": "#26272b"}
ROW_ACCENT = {"win": GREEN, "loss": RED, "draw": GRAY, "unknown": GRAY}
ROW_TEXT = {"win": "VICTORIA", "loss": "DERROTA", "draw": "EMPATE", "unknown": "FINALIZADA"}
TEAM_BG = {"blue": "#182636", "red": "#331c20"}
TEAM_NAME = {"blue": "EQUIPO AZUL", "red": "EQUIPO ROJO"}
FFA_BG = "#24252b"

RANK_COLORS = {
    "iron": "#8d8d8d",
    "bronze": "#b07d4f",
    "silver": "#c8d1d4",
    "gold": "#e8b84a",
    "platinum": "#3fb9c6",
    "diamond": "#c18cf2",
    "ascendant": "#2ecc71",
    "immortal": "#ff4f73",
    "radiant": "#fff3a6",
}

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")


# ─────────────────────────── Helpers ─────────────────────────── #
def rank_color(rank: str) -> str:
    rank = (rank or "").lower()
    for key, color in RANK_COLORS.items():
        if key in rank:
            return color
    return GRAY


def safe_json(response) -> dict:
    try:
        data = response.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def describe_http_error(response) -> str:
    """Convierte un status HTTP del proxy/API en un mensaje legible."""
    code = response.status_code
    body = safe_json(response)
    detail = body.get("error") or ""
    errors = body.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        detail = errors[0].get("message") or detail

    if code == 401 and not body:
        return ("El proxy de Vercel respondió 401 (Deployment Protection). "
                "Usa la URL de producción o desactiva la protección en Vercel.")
    if code in (401, 403):
        return "La API rechazó la petición: revisa VALORANT_API_KEY en Vercel."
    if code == 429:
        return "⏳ Límite de peticiones alcanzado. Espera un minuto e inténtalo de nuevo."
    if code >= 500:
        return f"Error del servidor ({code}). {detail}".strip()
    return f"Error HTTP {code}" + (f": {detail}" if detail else "")


def player_stats(player: dict, rounds: int) -> dict:
    st = player.get("stats") or {}
    k = st.get("kills") or 0
    d = st.get("deaths") or 0
    a = st.get("assists") or 0
    hs = st.get("headshots") or 0
    shots = hs + (st.get("bodyshots") or 0) + (st.get("legshots") or 0)
    dmg = player.get("damage_made") or 0
    score = st.get("score") or 0
    return {
        "k": k, "d": d, "a": a, "hs": hs, "shots": shots, "dmg": dmg, "score": score,
        "kd": k / max(d, 1),
        "adr": dmg / rounds,
        "acs": score / rounds,
        "hs_pct": (hs / shots * 100) if shots else 0.0,
    }


def match_players(match: dict) -> list:
    return (match.get("players") or {}).get("all_players") or []


def match_mode_label(meta: dict) -> str:
    mode_id = (meta.get("mode_id") or "").lower()
    return MODE_ID_TO_LABEL.get(mode_id) or meta.get("mode") or "Partida"


def is_ffa(match: dict) -> bool:
    """True para modos todos-contra-todos (Deathmatch): sin equipos ni rondas."""
    meta = match.get("metadata") or {}
    mode = f"{meta.get('mode_id') or ''} {meta.get('mode') or ''}".lower().replace(" ", "")
    if "deathmatch" in mode and "team" not in mode:
        return True
    teams = {(p.get("team") or "").lower() for p in match_players(match)}
    return len(teams) > 2


def team_result(match: dict, team: str):
    """Devuelve (resultado, rondas_ganadas, rondas_perdidas) para un equipo."""
    t = (match.get("teams") or {}).get((team or "").lower())
    if not isinstance(t, dict):
        return "unknown", 0, 0
    won = t.get("rounds_won") or 0
    lost = t.get("rounds_lost") or 0
    if t.get("has_won"):
        return "win", won, lost
    if won == lost:
        return "draw", won, lost
    return "loss", won, lost


def ffa_ranking(players: list) -> list:
    """Clasificación de Deathmatch: más kills primero (desempate por score)."""
    return sorted(
        players,
        key=lambda p: (-((p.get("stats") or {}).get("kills") or 0),
                       -((p.get("stats") or {}).get("score") or 0)),
    )


def analyze_match(match: dict, me: dict) -> dict:
    """Resultado y estadísticas de 'me' en una partida, para cualquier modo."""
    meta = match.get("metadata") or {}
    rounds = max(meta.get("rounds_played") or 1, 1)
    ffa = is_ffa(match)

    if ffa:
        ranking = ffa_ranking(match_players(match))
        place = next((i for i, p in enumerate(ranking, 1) if p is me), len(ranking))
        result = "win" if place == 1 else "loss"
        score_str = f"#{place} de {len(ranking)}"
    else:
        result, won, lost = team_result(match, me.get("team"))
        score_str = f"{won} - {lost}"

    return {
        "meta": meta,
        "ffa": ffa,
        "result": result,
        "score_str": score_str,
        "rounds": rounds,
        "mode_label": match_mode_label(meta),
        "stats": player_stats(me, rounds),
    }


def format_date(meta: dict) -> str:
    ts = meta.get("game_start")
    if ts:
        try:
            return datetime.fromtimestamp(int(ts)).strftime("%d/%m %H:%M")
        except (ValueError, OSError, TypeError):
            pass
    return ""


# ─────────────────────────── App ─────────────────────────── #
class ValorantTrackerApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Valorant Profile & Match History Tracker")
        self.geometry("1080x720")
        self.minsize(980, 620)

        self._search_id = 0          # descarta respuestas de búsquedas antiguas
        self._current_puuid = None
        self._has_searched = False
        self._build_ui()

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        top_frame = ctk.CTkFrame(self, corner_radius=10)
        top_frame.pack(pady=(15, 10), padx=20, fill="x")

        ctk.CTkLabel(
            top_frame, text="VALORANT TRACKER",
            font=ctk.CTkFont(size=20, weight="bold"), text_color=ACCENT,
        ).pack(side="left", padx=15, pady=12)

        self.region_var = ctk.StringVar(value="latam")
        self.combo_region = ctk.CTkOptionMenu(
            top_frame, values=REGIONS, variable=self.region_var, width=80,
            fg_color=BTN_BG, button_color="#383a40", button_hover_color=BTN_HOVER,
        )
        self.combo_region.pack(side="left", padx=(5, 6))

        self.mode_var = ctk.StringVar(value="Competitivo")
        self.combo_mode = ctk.CTkOptionMenu(
            top_frame, values=list(GAME_MODES.keys()), variable=self.mode_var, width=150,
            fg_color=BTN_BG, button_color="#383a40", button_hover_color=BTN_HOVER,
            command=self._on_mode_change,
        )
        self.combo_mode.pack(side="left", padx=(0, 10))

        self.entry_riot_id = ctk.CTkEntry(
            top_frame, placeholder_text="Ingresa Nombre#TAG (ej. Jugador#LAS)", height=36,
        )
        self.entry_riot_id.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.entry_riot_id.bind("<Return>", lambda _e: self.start_search())

        self.btn_search = ctk.CTkButton(
            top_frame, text="Buscar Perfil", width=130, height=36,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(weight="bold"), command=self.start_search,
        )
        self.btn_search.pack(side="right", padx=15)

        self.lbl_status = ctk.CTkLabel(
            self, text="Busca cualquier Nombre#TAG para ver su rango, estadísticas e historial.",
            font=ctk.CTkFont(size=13), text_color=GRAY,
        )
        self.lbl_status.pack(pady=(0, 5))

        # Tarjetas de resumen
        self.stats_frame = ctk.CTkFrame(self, corner_radius=10)
        self.stats_frame.pack(pady=5, padx=20, fill="x")
        self.stats_frame.grid_columnconfigure(tuple(range(6)), weight=1, uniform="cards")

        self.stat_cards = {}
        titles = [
            ("rank", "RANGO ACTUAL"),
            ("peak", "PEAK RANK"),
            ("winrate", "WINRATE"),
            ("kd", "K/D RATIO"),
            ("adr", "DAÑO / RONDA"),
            ("hs", "HEADSHOT %"),
        ]
        for idx, (key, title) in enumerate(titles):
            card = ctk.CTkFrame(self.stats_frame, fg_color=CARD_BG, corner_radius=8)
            card.grid(row=0, column=idx, padx=6, pady=10, sticky="nsew")

            ctk.CTkLabel(card, text=title, font=ctk.CTkFont(size=11, weight="bold"),
                         text_color=GRAY).pack(pady=(8, 2), padx=8)
            lbl_val = ctk.CTkLabel(card, text="-", font=ctk.CTkFont(size=16, weight="bold"))
            lbl_val.pack(pady=(0, 2), padx=8)
            lbl_sub = ctk.CTkLabel(card, text="", font=ctk.CTkFont(size=11), text_color=GRAY)
            lbl_sub.pack(pady=(0, 8), padx=8)

            self.stat_cards[key] = (lbl_val, lbl_sub)

        self.lbl_history = ctk.CTkLabel(
            self, text=self._history_title(),
            font=ctk.CTkFont(size=14, weight="bold"), anchor="w",
        )
        self.lbl_history.pack(pady=(10, 2), padx=22, fill="x")

        self.history_scroll = ctk.CTkScrollableFrame(self, corner_radius=10)
        self.history_scroll.pack(pady=(2, 15), padx=20, fill="both", expand=True)

    def _history_title(self) -> str:
        label = self.mode_var.get()
        if GAME_MODES.get(label) is None:
            return "HISTORIAL DE PARTIDAS — TODOS LOS MODOS"
        return f"HISTORIAL DE PARTIDAS — {label.upper()}"

    def _on_mode_change(self, _value):
        self.lbl_history.configure(text=self._history_title())
        # Si ya hay un perfil cargado, recarga automáticamente con el nuevo modo
        if self._has_searched and "#" in self.entry_riot_id.get():
            self.start_search()

    def _set_card(self, key, value, sub="", color="white"):
        lbl_val, lbl_sub = self.stat_cards[key]
        lbl_val.configure(text=value, text_color=color)
        lbl_sub.configure(text=sub)

    def _reset_cards(self):
        for key in self.stat_cards:
            self._set_card(key, "-", "")

    # ------------------------------------------------------------------ #
    # Búsqueda
    # ------------------------------------------------------------------ #
    def start_search(self, custom_riot_id=None):
        if custom_riot_id:
            self.entry_riot_id.delete(0, "end")
            self.entry_riot_id.insert(0, custom_riot_id)

        riot_id = self.entry_riot_id.get().strip()
        parts = [p.strip() for p in riot_id.split("#")]
        if len(parts) != 2 or not all(parts):
            self.lbl_status.configure(
                text="⚠️ Formato inválido. Debes incluir el #TAG (Ejemplo: Jugador#LAS)",
                text_color=RED,
            )
            return

        name, tag = parts
        region = self.region_var.get()
        mode_label = self.mode_var.get()
        mode = GAME_MODES.get(mode_label)

        self._has_searched = True
        self._search_id += 1
        search_id = self._search_id

        self.btn_search.configure(state="disabled", text="Cargando...")
        self.lbl_status.configure(
            text=f"Consultando {mode_label} de {name}#{tag} ({region.upper()})...", text_color=BLUE,
        )
        self.lbl_history.configure(text=self._history_title())
        self._reset_cards()
        for widget in self.history_scroll.winfo_children():
            widget.destroy()

        threading.Thread(
            target=self.fetch_all_data,
            args=(search_id, region, name, tag, mode, mode_label),
            daemon=True,
        ).start()

    def fetch_via_proxy(self, api_endpoint: str):
        url = f"{PROXY_URL}?endpoint={quote(api_endpoint, safe='')}"
        return requests.get(url, timeout=REQUEST_TIMEOUT)

    def fetch_all_data(self, search_id, region, name, tag, mode, mode_label):
        safe_name, safe_tag = quote(name, safe=""), quote(tag, safe="")
        mmr_endpoint = f"/valorant/v2/mmr/{region}/{safe_name}/{safe_tag}"
        matches_endpoint = f"/valorant/v3/matches/{region}/{safe_name}/{safe_tag}?size={MATCH_COUNT}"
        if mode:
            matches_endpoint += f"&mode={mode}"

        try:
            # Ambas peticiones en paralelo -> la mitad de tiempo de espera.
            with ThreadPoolExecutor(max_workers=2) as pool:
                f_mmr = pool.submit(self.fetch_via_proxy, mmr_endpoint)
                f_matches = pool.submit(self.fetch_via_proxy, matches_endpoint)
                mmr_res, matches_res = f_mmr.result(), f_matches.result()
        except requests.Timeout:
            self.after(0, self.show_error, search_id, "⏳ El servidor tardó demasiado en responder. Reintenta.")
            return
        except requests.RequestException as e:
            self.after(0, self.show_error, search_id, f"Error de conexión con el proxy: {e}")
            return

        if mmr_res.status_code != 200 and matches_res.status_code != 200:
            if mmr_res.status_code == 404 and matches_res.status_code == 404:
                msg = f"No se encontró a '{name}#{tag}' en la región {region.upper()}."
            else:
                failing = matches_res if matches_res.status_code != 404 else mmr_res
                msg = describe_http_error(failing)
            self.after(0, self.show_error, search_id, msg)
            return

        mmr_data = (safe_json(mmr_res).get("data") or {}) if mmr_res.status_code == 200 else {}
        matches_data = (safe_json(matches_res).get("data") or []) if matches_res.status_code == 200 else []

        self.after(0, self.render_profile_and_matches,
                   search_id, name, tag, mode_label, mmr_data, matches_data)

    def show_error(self, search_id, message):
        if search_id != self._search_id:
            return
        self.lbl_status.configure(text=message, text_color=RED)
        self.btn_search.configure(state="normal", text="Buscar Perfil")

    # ------------------------------------------------------------------ #
    # Render
    # ------------------------------------------------------------------ #
    @staticmethod
    def _find_player(all_players, puuid, name, tag):
        for p in all_players:
            if puuid and p.get("puuid") == puuid:
                return p
        for p in all_players:
            if (p.get("name", "").lower() == name.lower()
                    and p.get("tag", "").lower() == tag.lower()):
                return p
        return None

    def render_profile_and_matches(self, search_id, name, tag, mode_label, mmr_data, matches_data):
        if search_id != self._search_id:
            return
        self.btn_search.configure(state="normal", text="Buscar Perfil")

        # Usa el nombre con mayúsculas correctas si la API lo devuelve
        name = mmr_data.get("name") or name
        tag = mmr_data.get("tag") or tag
        self._current_puuid = mmr_data.get("puuid")
        mode_text = "de todos los modos" if GAME_MODES.get(mode_label) is None else f"de {mode_label}"

        # ── Rango actual y peak (siempre competitivo) ──
        curr = mmr_data.get("current_data") or {}
        high = mmr_data.get("highest_rank") or {}

        rank_str = curr.get("currenttierpatched") or "Sin Rango"
        rr = curr.get("ranking_in_tier") or 0
        last_change = curr.get("mmr_change_to_last_game") or 0
        sign = "+" if last_change >= 0 else ""
        self._set_card("rank", rank_str, f"{rr} RR ({sign}{last_change} últ.)", rank_color(rank_str))

        peak_str = high.get("patched_tier") or "N/A"
        peak_season = high.get("season") or ""
        self._set_card("peak", peak_str, f"Acto: {peak_season.upper()}" if peak_season else "",
                       rank_color(peak_str) if peak_str != "N/A" else "white")

        # ── Partidas ──
        totals = dict(games=0, wins=0, losses=0, draws=0, k=0, d=0, a=0,
                      hs=0, shots=0, dmg=0, score=0, rounds=0)

        for match in matches_data:
            all_players = match_players(match)
            me = self._find_player(all_players, self._current_puuid, name, tag)
            if not me:
                continue
            if not self._current_puuid:
                self._current_puuid = me.get("puuid")

            info = analyze_match(match, me)
            s = info["stats"]

            totals["games"] += 1
            if info["result"] == "win":
                totals["wins"] += 1
            elif info["result"] == "loss":
                totals["losses"] += 1
            elif info["result"] == "draw":
                totals["draws"] += 1
            for key in ("k", "d", "a", "hs", "shots"):
                totals[key] += s[key]
            # ADR/ACS no tienen sentido en Deathmatch (no hay rondas)
            if not info["ffa"]:
                totals["dmg"] += s["dmg"]
                totals["score"] += s["score"]
                totals["rounds"] += info["rounds"]

            self._create_match_row(match, me, info)

        if totals["games"] == 0:
            msg = (f"Perfil de {name}#{tag} cargado, pero no tiene partidas recientes {mode_text}."
                   if not matches_data else
                   f"Perfil de {name}#{tag} cargado, pero no se encontró al jugador en sus partidas.")
            self.lbl_status.configure(text=msg, text_color=ORANGE)
            for key in ("winrate", "kd", "adr", "hs"):
                self._set_card(key, "N/A")
            return

        g = totals["games"]
        decided = totals["wins"] + totals["losses"] + totals["draws"]
        kd = totals["k"] / max(totals["d"], 1)
        hs_pct = (totals["hs"] / totals["shots"] * 100) if totals["shots"] else 0.0

        if decided:
            winrate = totals["wins"] / decided * 100
            record = f"{totals['wins']}V - {totals['losses']}D"
            if totals["draws"]:
                record += f" - {totals['draws']}E"
            self._set_card("winrate", f"{winrate:.1f}%", f"{record} ({g} part.)",
                           GREEN if winrate >= 50 else RED)
        else:
            self._set_card("winrate", "N/A", f"{g} partidas")

        self._set_card("kd", f"{kd:.2f}", f"{totals['k']}K / {totals['d']}D / {totals['a']}A",
                       GREEN if kd >= 1.0 else RED)

        if totals["rounds"]:
            adr = totals["dmg"] / totals["rounds"]
            acs = totals["score"] / totals["rounds"]
            self._set_card("adr", f"{adr:.1f}", f"ACS Promedio: {acs:.0f}")
        else:
            self._set_card("adr", "N/A", "Sin rondas (Deathmatch)")

        self._set_card("hs", f"{hs_pct:.1f}%", f"{totals['hs']} HS de {totals['shots']} tiros", BLUE)

        self.lbl_status.configure(
            text=f"✅ Mostrando perfil y últimas {g} partidas {mode_text} de {name}#{tag}",
            text_color=GREEN,
        )

    def _create_match_row(self, match, me, info):
        meta, s, result = info["meta"], info["stats"], info["result"]
        all_players = match_players(match)

        row = ctk.CTkFrame(self.history_scroll, fg_color=ROW_BG[result], corner_radius=8)
        row.pack(fill="x", pady=4, padx=4)
        row.grid_columnconfigure(1, minsize=220)
        row.grid_columnconfigure(2, minsize=140)
        row.grid_columnconfigure(3, minsize=120)
        row.grid_columnconfigure(4, weight=1)

        # Barra lateral de color
        ctk.CTkFrame(row, width=5, fg_color=ROW_ACCENT[result], corner_radius=3).grid(
            row=0, column=0, sticky="ns", padx=(6, 4), pady=8)

        # Resultado + modo + fecha / mapa + marcador
        col = ctk.CTkFrame(row, fg_color="transparent")
        col.grid(row=0, column=1, sticky="w", padx=8, pady=8)
        line1 = ctk.CTkFrame(col, fg_color="transparent")
        line1.pack(anchor="w")
        ctk.CTkLabel(line1, text=ROW_TEXT[result], font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=ROW_ACCENT[result]).pack(side="left")
        ctk.CTkLabel(line1, text=f"  {info['mode_label']}", font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=PURPLE).pack(side="left")
        date = format_date(meta)
        if date:
            ctk.CTkLabel(line1, text=f"  ·  {date}", font=ctk.CTkFont(size=11),
                         text_color=GRAY).pack(side="left")
        ctk.CTkLabel(col, text=f"{meta.get('map', 'Mapa')} ({info['score_str']})",
                     font=ctk.CTkFont(size=14, weight="bold")).pack(anchor="w")

        # Agente + rango en la partida
        match_rank = me.get("currenttier_patched") or "Unranked"
        col = ctk.CTkFrame(row, fg_color="transparent")
        col.grid(row=0, column=2, sticky="w", padx=8, pady=8)
        ctk.CTkLabel(col, text=me.get("character", "Agente"),
                     font=ctk.CTkFont(size=14, weight="bold"), text_color=YELLOW).pack(anchor="w")
        ctk.CTkLabel(col, text=match_rank, font=ctk.CTkFont(size=11),
                     text_color=rank_color(match_rank)).pack(anchor="w")

        # KDA
        col = ctk.CTkFrame(row, fg_color="transparent")
        col.grid(row=0, column=3, sticky="w", padx=8, pady=8)
        ctk.CTkLabel(col, text=f"{s['k']} / {s['d']} / {s['a']}",
                     font=ctk.CTkFont(size=14, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(col, text=f"K/D: {s['kd']:.2f}", font=ctk.CTkFont(size=12),
                     text_color=GREEN if s["kd"] >= 1 else RED).pack(anchor="w")

        # ADR / ACS / HS (en Deathmatch se muestra el score en lugar de ADR/ACS)
        col = ctk.CTkFrame(row, fg_color="transparent")
        col.grid(row=0, column=4, sticky="w", padx=8, pady=8)
        line = f"Score: {s['score']}" if info["ffa"] else f"ADR: {s['adr']:.0f}  |  ACS: {s['acs']:.0f}"
        ctk.CTkLabel(col, text=line, font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(col, text=f"Headshots: {s['hs_pct']:.1f}%", font=ctk.CTkFont(size=12),
                     text_color=BLUE).pack(anchor="w")

        ctk.CTkButton(
            row, text=f"Ver {len(all_players)} Jugadores", width=130, height=32,
            fg_color=BTN_BG, hover_color=BTN_HOVER,
            command=lambda m=match, i=info, t=me.get("team"): self.open_match_inspector(m, i, t),
        ).grid(row=0, column=5, padx=12, pady=8)

    # ------------------------------------------------------------------ #
    # Inspector de Sala
    # ------------------------------------------------------------------ #
    def open_match_inspector(self, match, info, my_team):
        meta = info["meta"]
        all_players = match_players(match)
        map_name = meta.get("map", "Mapa")
        rounds = info["rounds"]
        score_str = info["score_str"]
        title = f"{info['mode_label']} — {map_name} ({score_str})"

        win = ctk.CTkToplevel(self)
        win.title(f"Inspector de Sala - {title}")
        win.geometry("940x620")
        win.minsize(860, 450)
        win.transient(self)
        # Workaround: en Windows CTkToplevel a veces se abre detrás de la ventana principal
        win.after(150, lambda: (win.lift(), win.focus_force()))

        ctk.CTkLabel(
            win, text=f"JUGADORES DE LA PARTIDA — {title.upper()}",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(pady=(12, 2))
        ctk.CTkLabel(
            win, text="💡 Aquí aparecen los Nombre#TAG reales aunque hayan usado Modo Incógnito.",
            font=ctk.CTkFont(size=12), text_color=GRAY,
        ).pack(pady=(0, 10))

        scroll = ctk.CTkScrollableFrame(win)
        scroll.pack(fill="both", expand=True, padx=15, pady=(0, 15))

        # ── Deathmatch: clasificación única ──
        if info["ffa"]:
            header = ctk.CTkFrame(scroll, fg_color="transparent")
            header.pack(fill="x", pady=(10, 2), padx=4)
            ctk.CTkLabel(header, text="CLASIFICACIÓN", font=ctk.CTkFont(size=14, weight="bold"),
                         text_color=PURPLE).pack(side="left")
            self._inspector_column_headers(scroll, ffa=True)
            for place, p in enumerate(ffa_ranking(all_players), 1):
                self._inspector_player_row(scroll, win, p, None, rounds, place=place)
            return

        # ── Modos por equipos: primero el equipo del jugador buscado ──
        my_team = (my_team or "red").lower()
        team_order = [my_team, "red" if my_team == "blue" else "blue"]

        for team in team_order:
            players = sorted(
                (p for p in all_players if (p.get("team") or "").lower() == team),
                key=lambda p: -((p.get("stats") or {}).get("score") or 0),
            )
            if not players:
                continue

            result, t_won, _ = team_result(match, team)
            header = ctk.CTkFrame(scroll, fg_color="transparent")
            header.pack(fill="x", pady=(10, 2), padx=4)
            ctk.CTkLabel(header, text=TEAM_NAME.get(team, team.upper()),
                         font=ctk.CTkFont(size=14, weight="bold"),
                         text_color=BLUE if team == "blue" else RED).pack(side="left")
            if result != "unknown":
                ctk.CTkLabel(header, text=f"  {t_won} rondas — {ROW_TEXT[result]}",
                             font=ctk.CTkFont(size=12, weight="bold"),
                             text_color=ROW_ACCENT[result]).pack(side="left")

            self._inspector_column_headers(scroll)
            for p in players:
                self._inspector_player_row(scroll, win, p, team, rounds)

    # Anchos de columna compartidos por cabecera y filas (alineación)
    _INSPECTOR_COLS = [("AGENTE", 100), ("JUGADOR", 200), ("RANGO", 120),
                       ("K / D / A", 90), ("ACS", 55), ("ADR", 55), ("HS%", 55)]
    _INSPECTOR_COLS_FFA = [("# AGENTE", 100), ("JUGADOR", 200), ("RANGO", 120),
                           ("K / D / A", 90), ("SCORE", 55), ("K/D", 55), ("HS%", 55)]

    def _inspector_column_headers(self, parent, ffa=False):
        hdr = ctk.CTkFrame(parent, fg_color="transparent")
        hdr.pack(fill="x", padx=4)
        for text, width in (self._INSPECTOR_COLS_FFA if ffa else self._INSPECTOR_COLS):
            ctk.CTkLabel(hdr, text=text, width=width, anchor="w",
                         font=ctk.CTkFont(size=10, weight="bold"),
                         text_color=GRAY).pack(side="left", padx=(8, 0))

    def _inspector_player_row(self, parent, win, p, team, rounds, place=None):
        ffa = place is not None
        is_me = bool(self._current_puuid) and p.get("puuid") == self._current_puuid
        full_id = f"{p.get('name', 'Desconocido')}#{p.get('tag', '???')}"
        rank = p.get("currenttier_patched") or "Sin Rango"
        s = player_stats(p, rounds)
        agent = p.get("character", "?")

        bg = FFA_BG if ffa else TEAM_BG.get(team, CARD_BG)
        p_row = ctk.CTkFrame(parent, fg_color=bg, corner_radius=6,
                             border_width=2 if is_me else 0, border_color=YELLOW)
        p_row.pack(fill="x", pady=3, padx=4)

        kda_color = GREEN if s["kd"] >= 1 else RED
        if ffa:
            values = [
                (f"#{place} {agent}", YELLOW if place == 1 else "white", True),
                (full_id, "white", True),
                (rank, rank_color(rank), False),
                (f"{s['k']}/{s['d']}/{s['a']}", kda_color, True),
                (f"{s['score']}", "white", False),
                (f"{s['kd']:.2f}", kda_color, False),
                (f"{s['hs_pct']:.0f}%", BLUE, False),
            ]
            cols = self._INSPECTOR_COLS_FFA
        else:
            values = [
                (agent, YELLOW, True),
                (full_id, "white", True),
                (rank, rank_color(rank), False),
                (f"{s['k']}/{s['d']}/{s['a']}", kda_color, True),
                (f"{s['acs']:.0f}", "white", False),
                (f"{s['adr']:.0f}", "white", False),
                (f"{s['hs_pct']:.0f}%", BLUE, False),
            ]
            cols = self._INSPECTOR_COLS

        for (text, color, bold), (_, width) in zip(values, cols):
            ctk.CTkLabel(p_row, text=text, width=width, anchor="w", text_color=color,
                         font=ctk.CTkFont(weight="bold" if bold else "normal")
                         ).pack(side="left", padx=(8, 0), pady=6)

        ctk.CTkButton(
            p_row, text="Ver Perfil", width=85, height=26,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            command=lambda rid=full_id: (win.destroy(), self.start_search(rid)),
        ).pack(side="right", padx=8, pady=5)


if __name__ == "__main__":
    app = ValorantTrackerApp()
    app.mainloop()
