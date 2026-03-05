import os
import re
import base64
import asyncio
import json
import logging
import tempfile
from urllib.parse import quote
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import httpx
import folium
from folium import IFrame
import polyline as polyline_decoder

# Logging para debug no Railway
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
MODEL = "claude-opus-4-5"
MAX_RETRIES = 3

# Armazena fotos por usuario
user_photos = {}

# ── Cores para marcadores e rotas ──
MARKER_COLORS = [
    "#E63946", "#457B9D", "#2A9D8F", "#E9C46A", "#F4A261",
    "#264653", "#6A0572", "#AB83A1", "#FF6B6B", "#4ECDC4",
]

# ── /start ──
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📦 Olá! Sou seu assistente de rotas de entrega.\n\n"
        "Como usar:\n"
        "1️⃣ Escreva o número do pacote a caneta na etiqueta\n"
        "2️⃣ Tire a foto e mande aqui (pode mandar várias de uma vez)\n"
        "3️⃣ Digite /rota para gerar a rota organizada\n\n"
        "🗺️ Agora com MAPA INTERATIVO! Todos os pontos plotados de uma vez!\n\n"
        "Outros comandos:\n"
        "📊 /status — ver quantas fotos já foram enviadas\n"
        "🗑️ /limpar — apagar tudo e começar do zero"
    )

# ── /status ──
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

# ── /limpar ──
async def limpar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_photos[user_id] = []
    await update.message.reply_text("🗑️ Fotos limpas! Pode mandar as novas.")

# ── Recebe foto ──
async def receber_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in user_photos:
        user_photos[user_id] = []

    # Pega a maior resolução disponível
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

# ── Extrai CEP numérico do endereço para ordenação ──
def extrair_cep_numerico(endereco):
    """Extrai o CEP do endereço e retorna como número para ordenação."""
    match = re.search(r"(\d{5})-?(\d{3})", endereco)
    if match:
        return int(match.group(1) + match.group(2))
    return 99999999  # Coloca no final se não encontrar CEP


# ══════════════════════════════════════════════════
#  GEOCODIFICAÇÃO — Converte endereço em lat/lng
# ══════════════════════════════════════════════════
async def geocodificar(client: httpx.AsyncClient, endereco: str):
    """Geocodifica um endereço usando Nominatim (OpenStreetMap)."""
    try:
        response = await client.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": endereco,
                "format": "json",
                "limit": 1,
                "countrycodes": "br",
            },
            headers={"User-Agent": "RoboRotaBot/1.0"},
        )
        data = response.json()
        if data:
            return {
                "lat": float(data[0]["lat"]),
                "lng": float(data[0]["lon"]),
            }
        # Tenta com endereço simplificado (só rua + cidade + CEP)
        partes = endereco.split(",")
        if len(partes) >= 3:
            simples = ", ".join([partes[0].strip(), partes[-2].strip(), partes[-1].strip()])
            response2 = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params={
                    "q": simples,
                    "format": "json",
                    "limit": 1,
                    "countrycodes": "br",
                },
                headers={"User-Agent": "RoboRotaBot/1.0"},
            )
            data2 = response2.json()
            if data2:
                return {
                    "lat": float(data2[0]["lat"]),
                    "lng": float(data2[0]["lon"]),
                }
        logger.warning(f"Geocodificação falhou para: {endereco}")
        return None
    except Exception as e:
        logger.error(f"Erro na geocodificação: {e}")
        return None


# ══════════════════════════════════════════════════
#  OSRM — Otimiza a ordem da rota (TSP)
# ══════════════════════════════════════════════════
async def otimizar_rota_osrm(client: httpx.AsyncClient, coordenadas):
    """
    Usa OSRM Trip API para resolver o problema do caixeiro viajante (TSP).
    Retorna as coordenadas e índices na ordem otimizada.
    """
    if len(coordenadas) < 2:
        return list(range(len(coordenadas)))

    # Formata coordenadas para OSRM: lng,lat;lng,lat;...
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

        if data.get("code") == "Ok" and data.get("trips"):
            # Extrai a ordem otimizada dos waypoints
            waypoints = data["trips"][0].get("legs", [])
            trip_waypoints = data.get("waypoints", [])
            ordem = [wp["waypoint_index"] for wp in trip_waypoints]
            return ordem
        else:
            logger.warning(f"OSRM Trip falhou: {data.get('code', 'unknown')}")
            return list(range(len(coordenadas)))
    except Exception as e:
        logger.error(f"Erro no OSRM Trip: {e}")
        return list(range(len(coordenadas)))


# ══════════════════════════════════════════════════
#  OSRM — Busca a geometria da rota ponto a ponto
# ══════════════════════════════════════════════════
async def obter_rota_osrm(client: httpx.AsyncClient, coordenadas_ordenadas):
    """
    Busca a geometria da rota completa via OSRM Route API.
    Retorna lista de pontos [lat, lng] para desenhar no mapa.
    """
    if len(coordenadas_ordenadas) < 2:
        return []

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
            # Decodifica polyline para lista de [lat, lng]
            pontos = polyline_decoder.decode(geometry)
            # Duração total em minutos
            duracao = data["routes"][0].get("duration", 0) / 60
            # Distância total em km
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
    """
    Gera um mapa HTML interativo com Leaflet.js usando Folium.
    Mostra todos os pontos e a rota entre eles.
    """
    # Centro do mapa (média das coordenadas)
    lats = [p["coords"]["lat"] for p in pacotes_com_coords]
    lngs = [p["coords"]["lng"] for p in pacotes_com_coords]
    center_lat = sum(lats) / len(lats)
    center_lng = sum(lngs) / len(lngs)

    # Cria o mapa
    m = folium.Map(
        location=[center_lat, center_lng],
        zoom_start=13,
        tiles=None,
    )

    # Adiciona tile layer com atribuição
    folium.TileLayer(
        tiles="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        attr="© OpenStreetMap contributors",
        name="OpenStreetMap",
    ).add_to(m)

    # Adiciona tile layer escura como alternativa
    folium.TileLayer(
        tiles="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
        attr="© CartoDB",
        name="Modo Escuro",
    ).add_to(m)

    # Adiciona controle de camadas
    folium.LayerControl().add_to(m)

    # Desenha a rota se disponível
    if rota_info and rota_info["pontos"]:
        folium.PolyLine(
            locations=rota_info["pontos"],
            color="#2563EB",
            weight=5,
            opacity=0.8,
            dash_array="",
            tooltip=f"🚗 {rota_info['distancia_km']} km — ~{rota_info['duracao_min']} min",
        ).add_to(m)

    # Adiciona marcadores numerados
    for i, p in enumerate(pacotes_com_coords):
        lat = p["coords"]["lat"]
        lng = p["coords"]["lng"]
        num_label = i + 1
        cor = MARKER_COLORS[i % len(MARKER_COLORS)]

        # Popup com informações
        num_pacote = f" — Pacote {p['numero']}" if p.get("numero") else ""
        popup_html = f"""
        <div style="font-family: 'Segoe UI', sans-serif; min-width: 220px;">
            <div style="background: {cor}; color: white; padding: 8px 12px;
                        border-radius: 8px 8px 0 0; font-weight: bold; font-size: 14px;">
                📍 Parada {num_label}{num_pacote}
            </div>
            <div style="padding: 10px 12px; font-size: 13px; line-height: 1.5;">
                <b>Bairro:</b> {p.get('bairro', '—')}<br>
                <b>Endereço:</b> {p['endereco']}<br>
                <a href="https://www.google.com/maps/search/{quote(p['endereco'], safe='')}"
                   target="_blank"
                   style="color: #2563EB; text-decoration: none; font-weight: bold;">
                   🔗 Abrir no Google Maps
                </a>
            </div>
        </div>
        """

        # Marcador com número
        icon_html = f"""
        <div style="
            background: {cor};
            color: white;
            border: 3px solid white;
            border-radius: 50%;
            width: 32px;
            height: 32px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: bold;
            font-size: 14px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.3);
        ">{num_label}</div>
        """

        folium.Marker(
            location=[lat, lng],
            popup=folium.Popup(popup_html, max_width=300),
            tooltip=f"Parada {num_label}: {p.get('bairro', p['endereco'][:30])}",
            icon=folium.DivIcon(
                html=icon_html,
                icon_size=(32, 32),
                icon_anchor=(16, 16),
            ),
        ).add_to(m)

    # Ajusta o zoom para mostrar todos os pontos
    m.fit_bounds([[min(lats) - 0.005, min(lngs) - 0.005],
                  [max(lats) + 0.005, max(lngs) + 0.005]])

    # Adiciona painel de informações
    duracao = rota_info.get("duracao_min", 0) if rota_info else 0
    distancia = rota_info.get("distancia_km", 0) if rota_info else 0
    total = len(pacotes_com_coords)

    info_html = f"""
    <div style="
        position: fixed;
        bottom: 20px;
        left: 20px;
        z-index: 9999;
        background: rgba(255,255,255,0.95);
        border-radius: 12px;
        padding: 16px 20px;
        box-shadow: 0 4px 20px rgba(0,0,0,0.15);
        font-family: 'Segoe UI', sans-serif;
        max-width: 280px;
        backdrop-filter: blur(10px);
    ">
        <div style="font-size: 16px; font-weight: bold; margin-bottom: 8px; color: #1a1a2e;">
            🚚 Rota de Entregas
        </div>
        <div style="font-size: 13px; color: #555; line-height: 1.8;">
            📍 <b>{total}</b> parada(s)<br>
            🛣️ <b>{distancia}</b> km total<br>
            ⏱️ <b>~{duracao}</b> min estimado
        </div>
    </div>
    """
    m.get_root().html.add_child(folium.Element(info_html))

    # Adiciona título
    title_html = f"""
    <div style="
        position: fixed;
        top: 10px;
        left: 50%;
        transform: translateX(-50%);
        z-index: 9999;
        background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
        color: white;
        border-radius: 30px;
        padding: 10px 24px;
        box-shadow: 0 4px 15px rgba(0,0,0,0.2);
        font-family: 'Segoe UI', sans-serif;
        font-size: 14px;
        font-weight: bold;
        white-space: nowrap;
    ">
        📦 Robo Rota — {total} entregas
    </div>
    """
    m.get_root().html.add_child(folium.Element(title_html))

    # Salva em arquivo temporário
    tmp_file = tempfile.NamedTemporaryFile(
        suffix=".html", prefix="rota_", delete=False, mode="w", encoding="utf-8"
    )
    m.save(tmp_file.name)
    tmp_file.close()
    return tmp_file.name


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
        f"🔍 Analisando {len(fotos)} foto(s)... Aguarda um momento!"
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
            "❌ Não consegui ler nenhum endereço nas fotos. "
            "Tente fotos mais nítidas e bem iluminadas."
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
        else:
            logger.info(f"Duplicata removida: {p['endereco']}")

    dupes = len(pacotes) - len(pacotes_unicos)

    await update.message.reply_text(
        f"📍 {len(pacotes_unicos)} endereço(s) extraído(s)! Geocodificando e otimizando rota..."
    )

    # 3. Geocodifica todos os endereços (sequencial por rate limit Nominatim: 1 req/s)
    coordenadas = []
    async with httpx.AsyncClient(timeout=60) as client:
        for p in pacotes_unicos:
            result = await geocodificar(client, p["endereco"])
            coordenadas.append(result)
            # Rate limit do Nominatim: 1 req/s
            if len(coordenadas) < len(pacotes_unicos):
                await asyncio.sleep(1.1)

    # Filtra apenas pacotes com geocodificação bem-sucedida
    pacotes_com_coords = []
    pacotes_sem_coords = []
    for p, coord in zip(pacotes_unicos, coordenadas):
        if coord:
            p["coords"] = coord
            pacotes_com_coords.append(p)
        else:
            pacotes_sem_coords.append(p)

    if not pacotes_com_coords:
        # Fallback: envia apenas links do Google Maps
        await update.message.reply_text(
            "⚠️ Não consegui geocodificar os endereços para o mapa. "
            "Enviando links individuais como alternativa..."
        )
        await _enviar_links_texto(update, pacotes_unicos, erros, dupes)
        user_photos[user_id] = []
        return

    # 4. Otimiza a ordem da rota via OSRM
    async with httpx.AsyncClient(timeout=30) as client:
        coords_para_otimizar = [p["coords"] for p in pacotes_com_coords]
        ordem = await otimizar_rota_osrm(client, coords_para_otimizar)

    # Reordena os pacotes pela rota otimizada
    pacotes_ordenados = [pacotes_com_coords[i] for i in ordem]

    # 5. Obtém geometria da rota para desenhar no mapa
    async with httpx.AsyncClient(timeout=30) as client:
        coords_ordenadas = [p["coords"] for p in pacotes_ordenados]
        rota_info = await obter_rota_osrm(client, coords_ordenadas)

    # 6. Gera mapa HTML interativo
    mapa_path = gerar_mapa_html(pacotes_ordenados, rota_info)

    # 7. Envia mensagem texto com lista
    total = len(pacotes_ordenados)
    msg = f"🗺️ Rota otimizada — {total} parada(s):\n\n"

    for i, p in enumerate(pacotes_ordenados, 1):
        num = f" [Pacote {p['numero']}]" if p["numero"] is not None else ""
        bairro = p["bairro"] or "—"
        link = "https://www.google.com/maps/search/" + quote(p["endereco"], safe="")
        msg += f"{i}️⃣ {bairro}{num}\n"
        msg += f"   {p['endereco']}\n"
        msg += f"   🔗 {link}\n\n"

    if rota_info and rota_info["pontos"]:
        msg += f"🛣️ Distância total: {rota_info['distancia_km']} km\n"
        msg += f"⏱️ Tempo estimado: ~{rota_info['duracao_min']} min\n\n"

    if erros > 0:
        msg += f"⚠️ {erros} foto(s) não puderam ser lidas.\n"

    if dupes > 0:
        msg += f"♻️ {dupes} endereço(s) duplicado(s) removidos.\n"

    if pacotes_sem_coords:
        msg += f"\n⚠️ {len(pacotes_sem_coords)} endereço(s) não puderam ser plotados no mapa.\n"

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

    # 8. Envia mapa como documento HTML
    try:
        with open(mapa_path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename="rota_entregas.html",
                caption=(
                    "🗺️ Mapa interativo com todas as paradas!\n"
                    "Abra no navegador para ver todos os pontos e a rota no mapa."
                ),
            )
    except Exception as e:
        logger.error(f"Erro ao enviar mapa: {e}")
        await update.message.reply_text(
            "⚠️ Não consegui enviar o mapa HTML. Use os links individuais acima."
        )
    finally:
        # Limpa arquivo temporário
        try:
            os.unlink(mapa_path)
        except Exception:
            pass

    # Limpa fotos após gerar a rota
    user_photos[user_id] = []


async def _enviar_links_texto(update, pacotes, erros, dupes):
    """Fallback: envia apenas links individuais do Google Maps."""
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
        msg += f"♻️ {dupes} endereço(s) duplicado(s) removidos.\n"

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


# ── Extrai endereço via Claude Vision com retry ──
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
                                    "{\"numero\": 6, \"bairro\": \"Jardim Planalto\", \"endereco\": \"Rua Jose Vencel, 36, Jardim Planalto, Santa Rita do Passa Quatro, SP, 13670-744\"}\n"
                                    "REGRAS CRÍTICAS para o campo 'endereco':\n"
                                    "1. Formato: Rua, Numero, Bairro, Cidade, UF, CEP (sem a palavra CEP, só os números)\n"
                                    "2. NUNCA escreva o nome completo do estado (ex: nunca 'São Paulo', nunca 'Minas Gerais') — use SEMPRE só a sigla de 2 letras (SP, MG, RJ, etc.)\n"
                                    "3. Inclua o CEP sem a palavra 'CEP' — só os números com hífen (ex: 13670-744)\n"
                                    "4. O CEP é o dado mais importante pois garante que o Maps encontre a cidade certa\n"
                                    "5. Se não tiver número escrito a caneta, coloque null no campo 'numero'\n"
                                    "6. Se não encontrar endereço, responda apenas: NAO_ENCONTRADO"
                                )
                            }
                        ]
                    }]
                }
            )
            data = response.json()

            # Checa erro da API
            if "error" in data:
                logger.warning(f"Erro da API (tentativa {tentativa}): {data['error']}")
                if tentativa < MAX_RETRIES:
                    await asyncio.sleep(2)
                    continue
                return None

            text = data["content"][0]["text"].strip()

            if "NAO_ENCONTRADO" in text:
                return None

            # Remove markdown se vier com ```json
            text = text.replace("```json", "").replace("```", "").strip()

            info = json.loads(text)
            endereco = info.get("endereco", "").strip()

            # Valida que o endereço não está vazio
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

# ── Main ──
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("limpar", limpar))
    app.add_handler(CommandHandler("rota", gerar_rota))
    app.add_handler(MessageHandler(filters.PHOTO, receber_foto))
    logger.info("Bot rodando com mapa interativo Leaflet.js + OSRM...")
    app.run_polling()

if __name__ == "__main__":
    main()
