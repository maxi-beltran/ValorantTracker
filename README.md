# 🎯 Valorant Tracker

App de escritorio en Python (CustomTkinter) para consultar **rango, estadísticas e historial de partidas** de cualquier jugador de Valorant usando su `Nombre#TAG`.

Los datos vienen de la [API de HenrikDev](https://docs.henrikdev.xyz/) a través de un **proxy propio en Vercel**, así que **la API Key nunca está en el código** ni en este repositorio.

## ✨ Funciones

- 🔎 Búsqueda por `Nombre#TAG` y región (`latam`, `br`, `na`, `eu`, `ap`, `kr`)
- 🎮 Filtro por modo: Competitivo, No competitivo, Swiftplay, Premier, Deathmatch, Team Deathmatch, Spike Rush, Escalada, Replicación, Guerra de nieve, Personalizada o todos
- 📊 Resumen: Rango actual, Peak Rank, Winrate, K/D, ADR y Headshot %
- 📜 Historial de las últimas 10 partidas (mapa, agente, KDA, marcador, K/D, ADR, HS%, ACS)
- 👥 **Inspector de Sala**: los jugadores de cada partida con sus nombres reales (incluso en modo incógnito), rangos y estadísticas, con botón **Ver Perfil**

## 🚀 Uso (app de escritorio)

Requiere **Python 3.10+**.

```bash
git clone https://github.com/<tu-usuario>/valorant-tracker.git
cd valorant-tracker
pip install -r requirements-desktop.txt
python valorant_tracker.py
```

La app ya apunta a un proxy público, no necesitas ninguna API Key.

## 🛠️ Desplegar tu propio proxy (opcional)

Si haces un fork y quieres usar tu propia API Key:

1. Consigue una API Key en [HenrikDev](https://docs.henrikdev.xyz/).
2. Importa el repositorio en [Vercel](https://vercel.com/new).
3. En **Settings → Environment Variables** añade `VALORANT_API_KEY` con tu key.
4. Despliega y apunta la app a tu proxy:
   ```bash
   # PowerShell
   $env:VALORANT_PROXY_URL = "https://<tu-proyecto>.vercel.app/api/proxy"
   python valorant_tracker.py
   ```

## 📁 Estructura

```
├── valorant_tracker.py        # App de escritorio
├── requirements-desktop.txt   # Dependencias de la app
├── api/
│   └── proxy.py               # Función serverless (Vercel) -> HenrikDev
├── vercel.json                # Configuración de Vercel
├── .vercelignore              # Archivos que no se suben a Vercel
└── .gitignore
```

## 🔒 Seguridad

- La API Key solo existe como variable de entorno en Vercel.
- El proxy solo acepta endpoints de Valorant en una lista blanca y cachea respuestas 60 s.

## ⚠️ Aviso

Proyecto no oficial. No está asociado ni respaldado por Riot Games. Valorant y Riot Games son marcas registradas de Riot Games, Inc.
