import os
import base64
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import httpx

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

user_photos: dict[int, list[str]] = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Ola! Sou seu assistente de rotas de entrega.\n\n"
        "Manda as fotos dos pacotes (pode mandar varias de uma vez)\n"
        "Depois digita /rota para gerar o link do Google Maps\n"
        "Digita /limpar para comecar do zero"
    )

async def limpar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_photos[user_id] = []
    await update.message.reply_text("Fotos limpas! Pode mandar as novas.")

async def receber_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in user_photos:
        user_photos[user_id] = []
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    async with httpx.AsyncClient() as client:
        response = await client.get(file.file_path)
        image_b64 = base64.b64encode(response.content).decode()
    user_photos[user_id].append(image_b64)
    count = len(user_photos[user_id])
    await update.message.reply_text(f"Foto {count} recebida! Manda mais ou digita /rota para gerar a rota.")

async def gerar_rota(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    fotos = user_photos.get(user_id, [])
    if not fotos:
        await update.message.reply_text("Nenhuma foto recebida ainda! Manda as fotos dos pacotes primeiro.")
        return
    await update.message.reply_text(f"Analisando {len(fotos)} foto(s)... Aguarda!")
    async with httpx.AsyncClient(timeout=30) as client:
        tasks = [extrair_endereco(client, foto) for foto in fotos]
        resultados = await asyncio.gather(*tasks, return_exceptions=True)
    enderecos = [r for r in resultados if isinstance(r, str)]
    erros = len(resultados) - len(enderecos)
    if not enderecos:
        await update.message.reply_text("Nao consegui ler nenhum endereco nas fotos. Tente fotos mais nitidas.")
        return
    destinos = "/".join([addr.replace(" ", "+") for addr in enderecos])
    maps_url = f"https://www.google.com/maps/dir/{destinos}"
    lista = "\n".join([f"{i+1}. {addr}" for i, addr in enumerate(enderecos)])
    msg = f"Rota com {len(enderecos)} parada(s):\n\n{lista}\n\n"
    if erros > 0:
        msg += f"{erros} foto(s) nao puderam ser lidas.\n\n"
    msg += f"Toque no link para abrir no Google Maps:\n{maps_url}"
    await update.message.reply_text(msg)
    user_photos[user_id] = []

async def extrair_endereco(client: httpx.AsyncClient, image_b64: str):
    try:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
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
                            "text": "Esta e uma foto de etiqueta de entrega brasileira. Extraia o endereco COMPLETO do destinatario (rua, numero, bairro, cidade, estado, CEP se houver). Responda APENAS com o endereco em uma linha, sem explicacoes. Se nao encontrar endereco, responda: NAO_ENCONTRADO"
                        }
                    ]
                }]
            }
        )
        data = response.json()
        text = data["content"][0]["text"].strip()
        return None if text == "NAO_ENCONTRADO" else text
    except Exception:
        return None

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("limpar", limpar))
    app.add_handler(CommandHandler("rota", gerar_rota))
    app.
