import os
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

    # Ordena pelo número do pacote (caneta) se houver
    pacotes_com_num = sorted(
        [p for p in pacotes if p["numero"] is not None],
        key=lambda x: x["numero"]
    )
    pacotes_sem_num = [p for p in pacotes if p["numero"] is None]
    pacotes_ordenados = pacotes_com_num + pacotes_sem_num

    # Agrupa por bairro
    bairros = {}
    for p in pacotes_ordenados:
        bairro = p["bairro"] or "Outros"
        if bairro not in bairros:
            bairros[bairro] = []
        bairros[bairro].append(p)

    # Monta mensagem agrupada por bairro
    msg = f"🗺️ Rota com {len(pacotes)} parada(s):\n\n"
    for bairro, pkgs in bairros.items():
        msg += f"📍 {bairro}:\n"
        for p in pkgs:
            num = f"[Pacote {p['numero']}] " if p["numero"] is not None else ""
            msg += f"  {num}{p['endereco']}\n"
        msg += "\n"

    # Monta link do Google Maps com encoding correto para acentos
    enderecos = [p["endereco"] for p in pacotes_ordenados]
    destinos = "/".join([quote(e, safe="") for e in enderecos])
    maps_url = "https://www.google.com/maps/dir/" + destinos

    if erros > 0:
        msg += f"⚠️ {erros} foto(s) não puderam ser lidas.\n\n"

    msg += f"👇 Toque para abrir no Google Maps:\n{maps_url}"

    await update.message.reply_text(msg)

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
                                    "Extraia APENAS o endereço do DESTINATÁRIO (ignore o remetente). "
                                    "Responda APENAS em formato JSON assim:\n"
                                    "{\"numero\": 6, \"bairro\": \"Jardim Planalto\", \"endereco\": \"Rua Jose Vencel, 36, Jardim Planalto, Santa Rita do Passa Quatro - SP, CEP 13670-744\"}\n"
                                    "IMPORTANTE: formate o endereco como: Rua, Numero, Bairro, Cidade - UF, CEP XXXXX-XXX\n"
                                    "Isso é essencial para o Google Maps encontrar o lugar certo.\n"
                                    "Se não tiver número escrito a caneta, coloque null no numero.\n"
                                    "Se não encontrar endereço, responda apenas: NAO_ENCONTRADO"
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
