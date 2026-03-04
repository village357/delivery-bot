import os
import re
import base64
import asyncio
import json
import logging
from urllib.parse import quote
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import httpx

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

# ── /start ──
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📦 Olá! Sou seu assistente de rotas de entrega.\n\n"
        "Como usar:\n"
        "1️⃣ Escreva o número do pacote a caneta na etiqueta\n"
        "2️⃣ Tire a foto e mande aqui (pode mandar várias de uma vez)\n"
        "3️⃣ Digite /rota para gerar a rota organizada\n\n"
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

# ── /rota ──
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

    # Remove endereços duplicados (mantém o primeiro encontrado)
    vistos = set()
    pacotes_unicos = []
    for p in pacotes:
        chave = p["endereco"].strip().lower()
        if chave not in vistos:
            vistos.add(chave)
            pacotes_unicos.append(p)
        else:
            logger.info(f"Duplicata removida: {p['endereco']}")

    # Ordena por CEP (proximidade geográfica real)
    pacotes_ordenados = sorted(pacotes_unicos, key=lambda p: extrair_cep_numerico(p["endereco"]))

    # Monta mensagem com link individual por parada
    total = len(pacotes_ordenados)
    msg = f"🗺️ Rota otimizada — {total} parada(s):\n\n"

    for i, p in enumerate(pacotes_ordenados, 1):
        num = f" [Pacote {p['numero']}]" if p["numero"] is not None else ""
        bairro = p["bairro"] or "—"
        link = "https://www.google.com/maps/search/" + quote(p["endereco"], safe="")
        msg += f"{i}️⃣ {bairro}{num}\n"
        msg += f"   {p['endereco']}\n"
        msg += f"   � {link}\n\n"

    if erros > 0:
        msg += f"⚠️ {erros} foto(s) não puderam ser lidas.\n\n"

    dupes = len(pacotes) - len(pacotes_unicos)
    if dupes > 0:
        msg += f"♻️ {dupes} endereço(s) duplicado(s) removidos.\n\n"

    # Link de rota completa no final (opcional)
    enderecos = [p["endereco"] for p in pacotes_ordenados]
    destinos = "/".join([quote(e, safe="") for e in enderecos])
    rota_url = "https://www.google.com/maps/dir//" + destinos
    msg += f"🚗 Rota completa (todas as paradas):\n{rota_url}"

    # Divide mensagem se exceder limite do Telegram (4096 chars)
    if len(msg) <= 4096:
        await update.message.reply_text(msg)
    else:
        # Envia paradas em blocos + link da rota separado
        partes = msg.rsplit("\n🚗", 1)
        texto_paradas = partes[0]

        # Divide texto das paradas em chunks de ~4000 chars
        while texto_paradas:
            if len(texto_paradas) <= 4096:
                await update.message.reply_text(texto_paradas)
                texto_paradas = ""
            else:
                corte = texto_paradas.rfind("\n\n", 0, 4096)
                if corte == -1:
                    corte = 4096
                await update.message.reply_text(texto_paradas[:corte])
                texto_paradas = texto_paradas[corte:].lstrip("\n")

        # Envia link da rota completa em mensagem separada
        if len(partes) > 1:
            await update.message.reply_text("🚗" + partes[1])

    # Limpa as fotos após gerar a rota
    user_photos[user_id] = []


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
    logger.info("Bot rodando...")
    app.run_polling()

if __name__ == "__main__":
    main()
