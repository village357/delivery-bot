import os
import base64
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import httpx

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

user_photos = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Ola! Sou seu assistente de rotas de entrega.\n\n"
        "Como usar:\n"
        "1. Escreva o numero do pacote a caneta na etiqueta\n"
        "2. Tire a foto e mande aqui (pode mandar varias)\n"
        "3. Digite /rota para gerar a rota organizada\n"
        "4. Digite /limpar para comecar do zero"
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
    await update.message.reply_text("Foto " + str(count) + " recebida! Manda mais ou digita /rota para gerar a rota.")

async def gerar_rota(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    fotos = user_photos.get(user_id, [])
    if not fotos:
        await update.message.reply_text("Nenhuma foto recebida ainda! Manda as fotos dos pacotes primeiro.")
        return
    await update.message.reply_text("Analisando " + str(len(fotos)) + " foto(s)... Aguarda!")
    async with httpx.AsyncClient(timeout=60) as client:
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
        await update.message.reply_text("Nao consegui ler nenhum endereco nas fotos. Tente fotos mais nitidas.")
        return

    # Ordena pelo numero do pacote se tiver
    pacotes_com_num = [p for p in pacotes if p["numero"] is not None]
    pacotes_sem_num = [p for p in pacotes if p["numero"] is None]
    pacotes_com_num.sort(key=lambda x: x["numero"])
    pacotes_ordenados = pacotes_com_num + pacotes_sem_num

    # Agrupa por bairro
    bairros = {}
    for p in pacotes_ordenados:
        bairro = p["bairro"] or "Outros"
        if bairro not in bairros:
            bairros[bairro] = []
        bairros[bairro].append(p)

    # Monta mensagem agrupada
    msg = "Rota com " + str(len(pacotes)) + " parada(s):\n\n"
    for bairro, pkgs in bairros.items():
        msg += "📍 " + bairro + ":\n"
        for p in pkgs:
            num = "[Pacote " + str(p["numero"]) + "] " if p["numero"] else ""
            msg += "  " + num + p["endereco"] + "\n"
        msg += "\n"

    # Monta link do Maps com todos os enderecos
    enderecos = [p["endereco"] for p in pacotes_ordenados]
    destinos = "/".join([e.replace(" ", "+") for e in enderecos])
    maps_url = "https://www.google.com/maps/dir/" + destinos

    if erros > 0:
        msg += str(erros) + " foto(s) nao puderam ser lidas.\n\n"

    msg += "Toque no link para abrir no Google Maps:\n" + maps_url

    await update.message.reply_text(msg)
    user_photos[user_id] = []

async def extrair_info(client, image_b64):
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
                            "text": (
                                "Esta e uma foto de etiqueta de entrega brasileira. "
                                "Pode ter um numero escrito a caneta pelo entregador (ex: 6, 12, 35). "
                                "Responda APENAS em formato JSON assim:\n"
                                "{\"numero\": 6, \"bairro\": \"Jardim Planalto\", \"endereco\": \"Jardim Planalto, Rua Jose Vencel, Casa 36, Santa Rita do Passa Quatro, SP, CEP 13670-744\"}\n"
                                "Se nao tiver numero escrito a caneta, coloque null no numero.\n"
                                "Se nao encontrar endereco, responda: NAO_ENCONTRADO"
                            )
                        }
                    ]
                }]
            }
        )
        data = response.json()
        text = data["content"][0]["text"].strip()
        if text == "NAO_ENCONTRADO":
            return None
        # Remove markdown se vier com ```json
        text = text.replace("```json", "").replace("```", "").strip()
        import json
        info = json.loads(text)
        return {
            "numero": info.get("numero"),
            "bairro": info.get("bairro"),
            "endereco": info.get("endereco", "")
        }
    except Exception:
        return None

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("limpar", limpar))
    app.add_handler(CommandHandler("rota", gerar_rota))
    app.add_handler(MessageHandler(filters.PHOTO, receber_foto))
    print("Bot rodando...")
    app.run_polling()

if __name__ == "__main__":
    main()
```

---

Depois que o Railway subir, a resposta do bot vai ficar assim:
```
Rota com 3 parada(s):

📍 Jardim Planalto:
  [Pacote 6] Rua Jose Vencel, Casa 36...
  [Pacote 9] Rua das Flores, 150...

📍 Centro:
  [Pacote 12] Rua 22 de Maio, 80...

Toque no link para abrir no Google Maps:
https://www.google.com/maps/dir/...
