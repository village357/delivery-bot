import os
import base64
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import httpx

# ── Configurações (preenchidas via variáveis de ambiente) ──
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# Armazena fotos por usuário enquanto ele envia
user_photos: dict[int, list[str]] = {}

# ── Comando /start ──
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Olá! Sou seu assistente de rotas de entrega.\n\n"
        "📸 Manda as fotos dos pacotes (pode mandar várias de uma vez)\n"
        "🗺️ Depois digita /rota para gerar o link do Google Maps\n"
        "🗑️ Digita /limpar para começar do zero"
    )

# ── Comando /limpar ──
async def limpar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_photos[user_id] = []
    await update.message.reply_text("🗑️ Fotos limpas! Pode mandar as novas.")

# ── Recebe foto ──
async def receber_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in user_photos:
        user_photos[user_id] = []

    # Pega a melhor qualidade disponível
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    
    # Baixa a imagem em memória
    async with httpx.AsyncClient() as client:
        response = await client.get(file.file_path)
        image_b64 = base64.b64encode(response.content).decode()
    
    user_photos[user_id].append(image_b64)
    count = len(user_photos[user_id])
    
    await update.message.reply_text(f"✅ Foto {count} recebida! Manda mais ou digita /rota para gerar a rota.")

# ── Comando /rota ──
async def gerar_rota(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    fotos = user_photos.get(user_id, [])

    if not fotos:
        await update.message.reply_text("⚠️ Nenhuma foto recebida ainda! Manda as fotos dos pacotes primeiro.")
        return

    await update.message.reply_text(f"🔍 Analisando {len(fotos)} foto(s)... Aguarda um momento!")

    enderecos = []
    erros = 0

    async with httpx.AsyncClient(timeout=30) as client:
        # Processa todas as fotos em paralelo
        tasks = [extrair_endereco(client, foto) for foto in fotos]
        resultados = await asyncio.gather(*tasks, return_exceptions=True)

    for resultado in resultados:
        if isinstance(resultado, Exception) or resultado is None:
            erros += 1
        else:
            enderecos.append(resultado)

    if not enderecos:
        await update.message.reply_text("❌ Não consegui ler nenhum endereço nas fotos. Tente fotos mais nítidas.")
        return

    # Monta o link do Google Maps
    destinos = "/".join([addr.replace(" ", "+") for addr in enderecos])
    maps_url = f"https://www.google.com/maps/dir/{destinos}"

    # Monta a mensagem de resposta
    lista = "\n".join([f"{i+1}. {addr}" for i, addr in enumerate(enderecos)])
    msg = f"🗺️ *Rota com {len(enderecos)} parada(s):*\n\n{lista}\n\n"
    
    if erros > 0:
        msg += f"⚠️ {erros} foto(s) não puderam ser lidas.\n\n"
    
    msg += f"👇 Toque no link para abrir no Google Maps:\n{maps_url}"

    await update.message.reply_text(msg, parse_mode="Markdown")
    
    # Limpa as fotos após gerar a rota
    user_photos[user_id] = []

# ── Chama Claude Vision para extrair endereço ──
async def extrair_endereco(client: httpx.AsyncClient, image_b64: str) -> str | None:
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
                                "Esta é uma foto de etiqueta de entrega brasileira. "
                                "Extraia o endereço COMPLETO do destinatário "
                                "(rua, número, bairro, cidade, estado, CEP se houver). "
                                "Responda APENAS com o endereço em uma linha, sem explicações. "
                                "Se não encontrar endereço, responda: NAO_ENCONTRADO"
                            )
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

# ── Inicia o bot ──
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("limpar", limpar))
    app.add_handler(CommandHandler("rota", gerar_rota))
    app.add_handler(MessageHandler(filters.PHOTO, receber_foto))
    print("🤖 Bot rodando...")
    app.run_polling()

if __name__ == "__main__":
    main()
```

---

### 📄 `requirements.txt` — Dependências
```
python-telegram-bot==20.7
httpx==0.27.0
```

---

### 📄 `Procfile` — Para o Railway entender como rodar
```
worker: python bot.py
```

---

## 🚀 Passo a passo para colocar no ar

### Passo 1 — Criar o bot no Telegram (2 minutos)

1. Abre o Telegram e busca por **@BotFather**
2. Manda `/newbot`
3. Escolhe um nome: ex. `Rotas Entrega`
4. Escolhe um username: ex. `rotasentrega_bot`
5. O BotFather te manda um token assim: `7823456789:AAF...` → **guarda esse token**

---

### Passo 2 — Subir no Railway (5 minutos)

1. Acessa **railway.app** e cria conta grátis com o GitHub
2. Clica em **"New Project"** → **"Deploy from GitHub"**
3. Cria um repositório no GitHub com os 3 arquivos acima e conecta
4. No Railway, vai em **"Variables"** e adiciona:
```
   TELEGRAM_TOKEN = (o token do BotFather)
   ANTHROPIC_API_KEY = (sua chave sk-ant-...)
