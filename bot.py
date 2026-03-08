import os
import re
import base64
import asyncio
import json
import logging
import uuid
import time
import threading
from urllib.parse import quote
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import httpx
import folium
from folium import Element
import polyline as polyline_decoder
import pgeocode
from flask import Flask, Response, abort

# ══════════════════════════════════════════════════
#  CONFIGURAÇÃO
# ══════════════════════════════════════════════════
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
MODEL = "claude-opus-4-5"
MAX_RETRIES = 3
PORT = int(os.environ.get("PORT", 8080))

# URL base do Railway (detecta automaticamente)
RAILWAY_URL = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
if RAILWAY_URL and not RAILWAY_URL.startswith("http"):
    RAILWAY_URL = f"https://{RAILWAY_URL}"

# Armazena fotos por usuario
user_photos = {}

# Armazena mapas gerados (id -> html_content)
mapas_gerados = {}
MAPA_TTL = 86400  # 24 horas em segundos

# Geocoder offline para CEPs brasileiros
nomi = pgeocode.Nominatim("br")

# Cores para marcadores
MARKER_COLORS = [
    "#E63946", "#457B9D", "#2A9D8F", "#E9C46A", "#F4A261",
    "#264653", "#6A0572", "#AB83A1", "#FF6B6B", "#4ECDC4",
]


# ══════════════════════════════════════════════════
#  FLASK WEB SERVER — serve os mapas
# ══════════════════════════════════════════════════
flask_app = Flask(__name__)


@flask_app.route("/health")
def health():
    return "OK", 200


@flask_app.route("/mapa/<mapa_id>")
def servir_mapa(mapa_id):
    """Serve o mapa HTML interativo pelo ID."""
    entry = mapas_gerados.get(mapa_id)
    if not entry:
        abort(404)

    # Checa expiração
    if time.time() - entry["criado_em"] > MAPA_TTL:
        del mapas_gerados[mapa_id]
        abort(404)

    return Response(entry["html"], mimetype="text/html")


def limpar_mapas_expirados():
    """Remove mapas com mais de 24h."""
    agora = time.time()
    expirados = [k for k, v in mapas_gerados.items() if agora - v["criado_em"] > MAPA_TTL]
    for k in expirados:
        del mapas_gerados[k]


def rodar_flask():
    """Roda o Flask em thread separada."""
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


# ══════════════════════════════════════════════════
#  COMANDOS DO BOT
# ══════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📦 Olá! Sou seu assistente de rotas de entrega.\n\n"
        "Como usar:\n"
        "1️⃣ Escreva o número do pacote a caneta na etiqueta\n"
        "2️⃣ Tire a foto e mande aqui (pode mandar várias de uma vez)\n"
        "3️⃣ Digite /rota para gerar a rota organizada\n\n"
        "🗺️ Agora com MAPA INTERATIVO! Todos os pontos no mapa de uma vez!\n\n"
        "Outros comandos:\n"
        "📊 /status — ver quantas fotos já foram enviadas\n"
        "🗑️ /limpar — apagar tudo e começar do zero"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    fotos = user_photos.get(user_id, [])
    count = len(fotos)
    if count == 0:
        await update.message.reply_text(
            "📭 Nenhuma foto recebida ainda.\n"
            "Manda as fotos dos pacotes e depois digita /rota!"
        )
    else:
        await update.message.reply_text(
            f"📊 {count} foto(s) aguardando.\n"
            "Pode mandar mais ou digitar /rota para gerar a rota!"
        )


async def limpar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_photos[user_id] = []
    await update.message.reply_text("🗑️ Fotos limpas! Pode mandar as novas.")


async def receber_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in user_photos:
        user_photos[user_id] = []

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(file.file_path)
        image_b64 = base64.b64encode(response.content).decode()

    user_photos[user_id].append(image_b64)
    count = len(user_photos[user_id])
    await update.message.reply_text(
        f"✅ Foto {count} recebida! "
        "Manda mais ou digita /rota para gerar a rota."
    )


# ══════════════════════════════════════════════════
#  GEOCODIFICAÇÃO — BrasilAPI + pgeocode (fallback)
# ══════════════════════════════════════════════════

def extrair_cep(endereco):
    """Extrai o CEP do endereço."""
    match = re.search(r"(\d{5})-?(\d{3})", endereco)
    if match:
        return match.group(1) + match.group(2)  # sem hífen: 13670744
    return None


def extrair_cep_numerico(endereco):
    """Extrai o CEP como número para ordenação."""
    cep = extrair_cep(endereco)
    if cep:
        return int(cep)
    return 99999999


async def geocodificar_brasilapi(client: httpx.AsyncClient, cep: str):
    """Geocodifica usando BrasilAPI CEP v2 — retorna lat/lng direto do CEP."""
    try:
        cep_formatado = cep.replace("-", "").strip()
        response = await client.get(
            f"https://brasilapi.com.br/api/cep/v2/{cep_formatado}",
            timeout=10,
        )
        if response.status_code == 200:
            data = response.json()
            loc = data.get("location", {})
            coords = loc.get("coordinates", {})
            lat = coords.get("latitude")
            lng = coords.get("longitude")
            if lat is not None and lng is not None:
                logger.info(f"BrasilAPI OK para CEP {cep_formatado}: {lat}, {lng}")
                return {"lat": float(lat), "lng": float(lng)}
            else:
                logger.warning(f"BrasilAPI sem coords para CEP {cep_formatado}")
        else:
            logger.warning(f"BrasilAPI status {response.status_code} para CEP {cep_formatado}")
    except Exception as e:
        logger.error(f"BrasilAPI erro: {e}")
    return None


def geocodificar_pgeocode(cep: str):
    """Geocodifica usando pgeocode (offline) — fallback."""
    try:
        cep_formatado = cep.replace("-", "").strip()
        # pgeocode espera CEP com 5 dígitos (prefixo)
        cep5 = cep_formatado[:5]
        result = nomi.query_postal_code(cep5)
        if result is not None and not (result["latitude"] != result["latitude"]):  # NaN check
            lat = float(result["latitude"])
            lng = float(result["longitude"])
            logger.info(f"pgeocode OK para CEP {cep5}: {lat}, {lng}")
            return {"lat": lat, "lng": lng}
        else:
            logger.warning(f"pgeocode sem coords para CEP {cep5}")
    except Exception as e:
        logger.error(f"pgeocode erro: {e}")
    return None


async def geocodificar_nominatim(client: httpx.AsyncClient, endereco: str):
    """Geocodifica usando Nominatim — último fallback."""
    try:
        response = await client.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": endereco,
                "format": "json",
                "limit": 1,
                "countrycodes": "br",
            },
            headers={"User-Agent": "RoboRotaBot/2.0"},
            timeout=10,
        )
        data = response.json()
        if data:
            lat = float(data[0]["lat"])
            lng = float(data[0]["lon"])
            logger.info(f"Nominatim OK para: {endereco[:40]}... -> {lat}, {lng}")
            return {"lat": lat, "lng": lng}
    except Exception as e:
        logger.error(f"Nominatim erro: {e}")
    return None


async def geocodificar(client: httpx.AsyncClient, endereco: str):
    """
    Geocodifica com estratégia de fallback:
    1. BrasilAPI CEP v2 (mais confiável para Brasil)
    2. pgeocode offline (rápido, sem rede)
    3. Nominatim (último recurso)
    """
    cep = extrair_cep(endereco)

    if cep:
        # 1. BrasilAPI
        coords = await geocodificar_brasilapi(client, cep)
        if coords:
            return coords

        # 2. pgeocode offline
        coords = geocodificar_pgeocode(cep)
        if coords:
            return coords

    # 3. Nominatim (último recurso)
    coords = await geocodificar_nominatim(client, endereco)
    return coords


# ══════════════════════════════════════════════════
#  OSRM — Otimiza rota e obtém geometria
# ══════════════════════════════════════════════════

async def otimizar_rota_osrm(client: httpx.AsyncClient, coordenadas):
    """Usa OSRM Trip API para resolver TSP."""
    if len(coordenadas) < 2:
        return list(range(len(coordenadas)))

    coords_str = ";".join([f"{c['lng']},{c['lat']}" for c in coordenadas])

    try:
        response = await client.get(
            f"https://router.project-osrm.org/trip/v1/driving/{coords_str}",
            params={
                "overview": "full",
                "geometries": "polyline",
                "roundtrip": "false",
                "source": "first",
                "steps": "false",
            },
            timeout=30,
        )
        data = response.json()

        if data.get("code") == "Ok" and data.get("waypoints"):
            ordem = [wp["waypoint_index"] for wp in data["waypoints"]]
            return ordem
        else:
            logger.warning(f"OSRM Trip falhou: {data.get('code', 'unknown')}")
            return list(range(len(coordenadas)))
    except Exception as e:
        logger.error(f"Erro no OSRM Trip: {e}")
        return list(range(len(coordenadas)))


async def obter_rota_osrm(client: httpx.AsyncClient, coordenadas_ordenadas):
    """Busca geometria da rota via OSRM Route API."""
    if len(coordenadas_ordenadas) < 2:
        return {"pontos": [], "duracao_min": 0, "distancia_km": 0}

    coords_str = ";".join([f"{c['lng']},{c['lat']}" for c in coordenadas_ordenadas])

    try:
        response = await client.get(
            f"https://router.project-osrm.org/route/v1/driving/{coords_str}",
            params={
                "overview": "full",
                "geometries": "polyline",
                "steps": "false",
            },
            timeout=30,
        )
        data = response.json()

        if data.get("code") == "Ok" and data.get("routes"):
            geometry = data["routes"][0]["geometry"]
            pontos = polyline_decoder.decode(geometry)
            duracao = data["routes"][0].get("duration", 0) / 60
            distancia = data["routes"][0].get("distance", 0) / 1000
            return {
                "pontos": pontos,
                "duracao_min": round(duracao),
                "distancia_km": round(distancia, 1),
            }
        return {"pontos": [], "duracao_min": 0, "distancia_km": 0}
    except Exception as e:
        logger.error(f"Erro ao obter rota OSRM: {e}")
        return {"pontos": [], "duracao_min": 0, "distancia_km": 0}


# ══════════════════════════════════════════════════
#  GERA MAPA INTERATIVO COM FOLIUM (Leaflet.js)
# ══════════════════════════════════════════════════

def gerar_mapa_html(pacotes_com_coords, rota_info):
    """Gera HTML do mapa interativo com Leaflet.js."""

    lats = [p["coords"]["lat"] for p in pacotes_com_coords]
    lngs = [p["coords"]["lng"] for p in pacotes_com_coords]
    center_lat = sum(lats) / len(lats)
    center_lng = sum(lngs) / len(lngs)

    m = folium.Map(
        location=[center_lat, center_lng],
        zoom_start=13,
        tiles=None,
    )

    # Tiles
    folium.TileLayer(
        tiles="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        attr="© OpenStreetMap",
        name="Mapa",
    ).add_to(m)

    folium.TileLayer(
        tiles="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
        attr="© CartoDB",
        name="Modo Escuro",
    ).add_to(m)

    folium.LayerControl().add_to(m)

    # Rota
    if rota_info and rota_info["pontos"]:
        folium.PolyLine(
            locations=rota_info["pontos"],
            color="#2563EB",
            weight=5,
            opacity=0.8,
            tooltip=f"🚗 {rota_info['distancia_km']} km — ~{rota_info['duracao_min']} min",
        ).add_to(m)

    # Marcadores
    for i, p in enumerate(pacotes_com_coords):
        lat = p["coords"]["lat"]
        lng = p["coords"]["lng"]
        num_label = i + 1
        cor = MARKER_COLORS[i % len(MARKER_COLORS)]

        num_pacote = f" — Pacote {p['numero']}" if p.get("numero") else ""
        gmaps_link = "https://www.google.com/maps/search/" + quote(p["endereco"], safe="")

        popup_html = f"""
        <div style="font-family: -apple-system, 'Segoe UI', sans-serif; min-width: 240px; "
        "border-radius: 12px; overflow: hidden;">
            <div style="background: {cor}; color: white; padding: 10px 14px; font-weight: 700; font-size: 15px;">
                📍 Parada {num_label}{num_pacote}
            </div>
            <div style="padding: 12px 14px; font-size: 13px; line-height: 1.6; background: #fff;">
                <b>Bairro:</b> {p.get('bairro') or '—'}<br>
                <b>Endereço:</b> {p['endereco']}<br><br>
                <a href="{gmaps_link}" target="_blank"
                   style="display: inline-block; background: #4285F4; color: white;
                          padding: 8px 16px; border-radius: 20px; text-decoration: none;
                          font-weight: 600; font-size: 13px;">
                   🗺️ Abrir no Google Maps
                </a>
            </div>
        </div>
        """

        icon_html = f"""
        <div style="
            background: {cor};
            color: white;
            border: 3px solid white;
            border-radius: 50%;
            width: 36px;
            height: 36px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 800;
            font-size: 15px;
            box-shadow: 0 3px 10px rgba(0,0,0,0.35);
            cursor: pointer;
        ">{num_label}</div>
        """

        folium.Marker(
            location=[lat, lng],
            popup=folium.Popup(popup_html, max_width=320),
            tooltip=f"📍 Parada {num_label}: {p.get('bairro') or p['endereco'][:25]}",
            icon=folium.DivIcon(
                html=icon_html,
                icon_size=(36, 36),
                icon_anchor=(18, 18),
            ),
        ).add_to(m)

    # Fit bounds
    m.fit_bounds([
        [min(lats) - 0.008, min(lngs) - 0.008],
        [max(lats) + 0.008, max(lngs) + 0.008]
    ])

    # Info panel
    duracao = rota_info.get("duracao_min", 0) if rota_info else 0
    distancia = rota_info.get("distancia_km", 0) if rota_info else 0
    total = len(pacotes_com_coords)

    # Lista de paradas para o painel lateral
    paradas_html = ""
    for i, p in enumerate(pacotes_com_coords):
        cor = MARKER_COLORS[i % len(MARKER_COLORS)]
        num_pacote = f" (Pacote {p['numero']})" if p.get("numero") else ""
        paradas_html += f"""
        <div style="display: flex; align-items: center; gap: 10px; padding: 8px 0;
                    border-bottom: 1px solid #eee;">
            <div style="background: {cor}; color: white; min-width: 28px; height: 28px;
                        border-radius: 50%; display: flex; align-items: center;
                        justify-content: center; font-weight: 700; font-size: 13px;">
                {i + 1}
            </div>
            <div style="font-size: 12px; line-height: 1.4;">
                <b>{p.get('bairro') or '—'}</b>{num_pacote}<br>
                <span style="color: #666;">{p['endereco'][:50]}...</span>
            </div>
        </div>
        """

    info_html = f"""
    <div id="info-panel" style="
        position: fixed;
        bottom: 20px;
        left: 20px;
        z-index: 9999;
        background: rgba(255,255,255,0.97);
        border-radius: 16px;
        padding: 0;
        box-shadow: 0 8px 32px rgba(0,0,0,0.18);
        font-family: -apple-system, 'Segoe UI', sans-serif;
        max-width: 320px;
        max-height: 70vh;
        overflow: hidden;
        backdrop-filter: blur(12px);
    ">
        <div style="background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
                    color: white; padding: 16px 20px; border-radius: 16px 16px 0 0;">
            <div style="font-size: 18px; font-weight: 800; margin-bottom: 4px;">
                🚚 Rota de Entregas
            </div>
            <div style="font-size: 13px; opacity: 0.9; display: flex; gap: 16px;">
                <span>📍 {total} paradas</span>
                <span>🛣️ {distancia} km</span>
                <span>⏱️ ~{duracao} min</span>
            </div>
        </div>
        <div style="padding: 12px 16px; max-height: 40vh; overflow-y: auto;">
            {paradas_html}
        </div>
        <div style="padding: 10px 16px; text-align: center; border-top: 1px solid #eee;">
            <span style="font-size: 11px; color: #999;">Toque nos marcadores para detalhes</span>
        </div>
    </div>
    """
    m.get_root().html.add_child(Element(info_html))

    # Toggle button para mobile (minimizar/expandir painel)
    toggle_js = """
    <script>
    document.addEventListener('DOMContentLoaded', function() {
        var panel = document.getElementById('info-panel');
        if (panel && window.innerWidth < 600) {
            // Em mobile, começa minimizado
            var content = panel.querySelector('div:nth-child(2)');
            var footer = panel.querySelector('div:nth-child(3)');
            if (content) content.style.display = 'none';
            if (footer) footer.style.display = 'none';

            panel.querySelector('div:first-child').addEventListener('click', function() {
                if (content) {
                    content.style.display = content.style.display === 'none' ? 'block' : 'none';
                }
                if (footer) {
                    footer.style.display = footer.style.display === 'none' ? 'block' : 'none';
                }
            });
            panel.querySelector('div:first-child').style.cursor = 'pointer';
        }
    });
    </script>
    """
    m.get_root().html.add_child(Element(toggle_js))

    # Meta viewport para mobile
    meta = '<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">'
    m.get_root().html.add_child(Element(meta))

    return m._repr_html_()


# ══════════════════════════════════════════════════
#  /rota — GERA ROTA + MAPA INTERATIVO
# ══════════════════════════════════════════════════

async def gerar_rota(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    fotos = user_photos.get(user_id, [])

    if not fotos:
        await update.message.reply_text(
            "⚠️ Nenhuma foto recebida ainda! Manda as fotos dos pacotes primeiro."
        )
        return

    await update.message.reply_text(
        f"🔍 Analisando {len(fotos)} foto(s) com IA... Aguarda!"
    )

    # 1. Extrai endereços via Claude Vision
    async with httpx.AsyncClient(timeout=90) as client:
        tasks = [extrair_info(client, foto) for foto in fotos]
        resultados = await asyncio.gather(*tasks, return_exceptions=True)

    pacotes = []
    erros = 0
    for r in resultados:
        if isinstance(r, dict):
            pacotes.append(r)
        else:
            erros += 1

    if not pacotes:
        await update.message.reply_text(
            "❌ Não consegui ler nenhum endereço. Tente fotos mais nítidas."
        )
        return

    # 2. Remove duplicados
    vistos = set()
    pacotes_unicos = []
    for p in pacotes:
        chave = p["endereco"].strip().lower()
        if chave not in vistos:
            vistos.add(chave)
            pacotes_unicos.append(p)

    dupes = len(pacotes) - len(pacotes_unicos)

    await update.message.reply_text(
        f"📍 {len(pacotes_unicos)} endereço(s)! Buscando coordenadas GPS..."
    )

    # 3. Geocodifica (BrasilAPI → pgeocode → Nominatim)
    coordenadas = []
    async with httpx.AsyncClient(timeout=60) as client:
        for p in pacotes_unicos:
            result = await geocodificar(client, p["endereco"])
            coordenadas.append(result)
            # Rate limit entre requests
            if len(coordenadas) < len(pacotes_unicos):
                await asyncio.sleep(0.5)

    # Filtra pacotes geocodificados
    pacotes_com_coords = []
    pacotes_sem_coords = []
    for p, coord in zip(pacotes_unicos, coordenadas):
        if coord:
            p["coords"] = coord
            pacotes_com_coords.append(p)
        else:
            pacotes_sem_coords.append(p)

    if not pacotes_com_coords:
        await update.message.reply_text(
            "⚠️ Não consegui localizar os endereços no mapa. "
            "Enviando links individuais..."
        )
        await _enviar_links_texto(update, pacotes_unicos, erros, dupes)
        user_photos[user_id] = []
        return

    await update.message.reply_text("🛣️ Otimizando rota e gerando mapa...")

    # 4. Otimiza ordem via OSRM Trip
    async with httpx.AsyncClient(timeout=30) as client:
        coords_para_otimizar = [p["coords"] for p in pacotes_com_coords]
        ordem = await otimizar_rota_osrm(client, coords_para_otimizar)

    pacotes_ordenados = [pacotes_com_coords[i] for i in ordem]

    # 5. Obtém geometria da rota
    async with httpx.AsyncClient(timeout=30) as client:
        coords_ordenadas = [p["coords"] for p in pacotes_ordenados]
        rota_info = await obter_rota_osrm(client, coords_ordenadas)

    # 6. Gera mapa HTML e salva no servidor
    html_content = gerar_mapa_html(pacotes_ordenados, rota_info)
    mapa_id = str(uuid.uuid4())[:8]
    mapas_gerados[mapa_id] = {
        "html": html_content,
        "criado_em": time.time(),
    }

    # Limpa mapas antigos
    limpar_mapas_expirados()

    # 7. Monta URL do mapa
    if RAILWAY_URL:
        mapa_url = f"{RAILWAY_URL}/mapa/{mapa_id}"
    else:
        mapa_url = f"http://localhost:{PORT}/mapa/{mapa_id}"

    # 8. Envia mensagem com lista + link do mapa
    total = len(pacotes_ordenados)
    msg = f"🗺️ ROTA OTIMIZADA — {total} parada(s)\n\n"

    for i, p in enumerate(pacotes_ordenados, 1):
        num = f" [Pacote {p['numero']}]" if p["numero"] is not None else ""
        bairro = p["bairro"] or "—"
        link = "https://www.google.com/maps/search/" + quote(p["endereco"], safe="")
        msg += f"{i}️⃣ {bairro}{num}\n"
        msg += f"   {p['endereco']}\n"
        msg += f"   🔗 {link}\n\n"

    if rota_info and rota_info["pontos"]:
        msg += f"🛣️ Distância: {rota_info['distancia_km']} km\n"
        msg += f"⏱️ Tempo: ~{rota_info['duracao_min']} min\n\n"

    if erros > 0:
        msg += f"⚠️ {erros} foto(s) não puderam ser lidas.\n"
    if dupes > 0:
        msg += f"♻️ {dupes} duplicata(s) removida(s).\n"
    if pacotes_sem_coords:
        msg += f"📌 {len(pacotes_sem_coords)} endereço(s) sem localização no mapa.\n"

    # Divide mensagem se necessário
    if len(msg) <= 4096:
        await update.message.reply_text(msg)
    else:
        while msg:
            if len(msg) <= 4096:
                await update.message.reply_text(msg)
                msg = ""
            else:
                corte = msg.rfind("\n\n", 0, 4096)
                if corte == -1:
                    corte = 4096
                await update.message.reply_text(msg[:corte])
                msg = msg[corte:].lstrip("\n")

    # Envia link do mapa interativo
    await update.message.reply_text(
        f"🗺️ MAPA INTERATIVO — Toque para abrir:\n\n"
        f"👉 {mapa_url}\n\n"
        "✅ Abre no navegador com zoom, clique nos pontos e rota completa!\n"
        "⏳ O link expira em 24h."
    )

    user_photos[user_id] = []


async def _enviar_links_texto(update, pacotes, erros, dupes):
    """Fallback: envia apenas links individuais."""
    pacotes_ordenados = sorted(pacotes, key=lambda p: extrair_cep_numerico(p["endereco"]))
    total = len(pacotes_ordenados)
    msg = f"🗺️ Rota — {total} parada(s):\n\n"

    for i, p in enumerate(pacotes_ordenados, 1):
        num = f" [Pacote {p['numero']}]" if p["numero"] is not None else ""
        bairro = p["bairro"] or "—"
        link = "https://www.google.com/maps/search/" + quote(p["endereco"], safe="")
        msg += f"{i}️⃣ {bairro}{num}\n"
        msg += f"   {p['endereco']}\n"
        msg += f"   🔗 {link}\n\n"

    if erros > 0:
        msg += f"⚠️ {erros} foto(s) não puderam ser lidas.\n"
    if dupes > 0:
        msg += f"♻️ {dupes} duplicata(s) removida(s).\n"

    if len(msg) <= 4096:
        await update.message.reply_text(msg)
    else:
        while msg:
            if len(msg) <= 4096:
                await update.message.reply_text(msg)
                msg = ""
            else:
                corte = msg.rfind("\n\n", 0, 4096)
                if corte == -1:
                    corte = 4096
                await update.message.reply_text(msg[:corte])
                msg = msg[corte:].lstrip("\n")


# ══════════════════════════════════════════════════
#  CLAUDE VISION — Extrai endereço da etiqueta
# ══════════════════════════════════════════════════

async def extrair_info(client: httpx.AsyncClient, image_b64: str):
    for tentativa in range(1, MAX_RETRIES + 1):
        try:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": MODEL,
                    "max_tokens": 300,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": image_b64
                                }
                            },
                            {
                                "type": "text",
                                "text": (
                                    "Esta é uma foto de etiqueta de entrega brasileira. "
                                    "Pode ter um número escrito a caneta pelo entregador (ex: 6, 12, 35). "
                                    "Extraia APENAS o endereço do DESTINATÁRIO (ignore completamente o remetente). "
                                    "Responda APENAS em formato JSON assim:\n"
                                    "{\"numero\": 6, \"bairro\": \"Jardim Planalto\", "
                                    "\"endereco\": \"Rua Jose, 36, Jardim, Cidade, SP, 13670-744\"}\n"
                                    "REGRAS CRÍTICAS para o campo 'endereco':\n"
                                    "1. Formato: Rua, Numero, Bairro, Cidade, UF, CEP "
                                    "(sem a palavra CEP, só números)\n"
                                    "2. NUNCA escreva o nome completo do estado "
                                    "(nunca 'São Paulo') — use SEMPRE sigla de 2 letras (SP, MG, RJ)\n"
                                    "3. Inclua o CEP sem a palavra 'CEP' — só os números com hífen\n"
                                    "4. O CEP é o dado mais importante pois garante "
                                    "que o Maps encontre a cidade certa\n"
                                    "5. Se não tiver número escrito a caneta, coloque null no campo 'numero'\n"
                                    "6. Se não encontrar endereço, responda apenas: NAO_ENCONTRADO"
                                )
                            }
                        ]
                    }]
                }
            )
            data = response.json()

            if "error" in data:
                logger.warning(f"Erro da API (tentativa {tentativa}): {data['error']}")
                if tentativa < MAX_RETRIES:
                    await asyncio.sleep(2)
                    continue
                return None

            text = data["content"][0]["text"].strip()

            if "NAO_ENCONTRADO" in text:
                return None

            text = text.replace("```json", "").replace("```", "").strip()
            info = json.loads(text)
            endereco = info.get("endereco", "").strip()

            if not endereco:
                logger.warning("Endereço vazio retornado pelo Claude")
                return None

            return {
                "numero": info.get("numero"),
                "bairro": info.get("bairro", "").strip() or None,
                "endereco": endereco
            }

        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Erro ao parsear resposta (tentativa {tentativa}): {e}")
            if tentativa < MAX_RETRIES:
                await asyncio.sleep(2)
                continue
            return None
        except Exception as e:
            logger.error(f"Erro inesperado (tentativa {tentativa}): {e}")
            if tentativa < MAX_RETRIES:
                await asyncio.sleep(2)
                continue
            return None

    return None


# ══════════════════════════════════════════════════
#  MAIN — Roda Flask + Telegram Bot
# ══════════════════════════════════════════════════

def main():
    # Inicia Flask em thread separada
    flask_thread = threading.Thread(target=rodar_flask, daemon=True)
    flask_thread.start()
    logger.info(f"Flask rodando na porta {PORT}")

    # Inicia Telegram bot
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("limpar", limpar))
    app.add_handler(CommandHandler("rota", gerar_rota))
    app.add_handler(MessageHandler(filters.PHOTO, receber_foto))

    logger.info("Bot V2 rodando com mapa interativo via web server!")
    app.run_polling()


if __name__ == "__main__":
    main()
