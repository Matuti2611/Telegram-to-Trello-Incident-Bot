# 🤖 Proyecto Multi-Bot (Telegram & WhatsApp)

Este proyecto integra bots de mensajería con IA, gestión de tareas en Trello y un dashboard de control. Sigue las instrucciones a continuación para la puesta en marcha.

---

## 🛠️ Configuración Inicial

### Variables de Entorno
Crea un archivo `.env` en el directorio raíz y completa los siguientes campos:

```env
# Telegram Bot API
TELEGRAM_TOKEN=

# WhatsApp Cloud API (Meta)
WHATSAPP_TOKEN=
PHONE_NUMBER_ID=
VERIFY_TOKEN=
WHATSAPP_APP_SECRET=  # Se encuentra en Settings -> Basic de tu App en Meta
GRAPH_API_VERSION=v21.0

# Inteligencia Artificial
GEMINI_API_KEY=

# Gestión de Trello
TRELLO_KEY=
TRELLO_TOKEN=
TRELLO_LIST_ID=

# Procesamiento de Audio
DEEPGRAM_API_KEY=
```
## Instalación
Instalar python y poner el siguiente comando en la terminal:
```
pip install pyTelegramBotAPI google-generativeai deepgram-sdk streamlit flask python-dotenv pydantic requests
```

## Ejecución
En una terminal
```
ngrok http 5000
```
(esto dara una URL que hay que copiar en meta developers)

En otra
```
waitress-serve --port=5000 whatsapp_bot:app
```

## Prender el Bot

### El bot de Telegram
```
python bot.py 
```
(Para este bot no son necesarios las dos terminales anteriores)

### El dashboard
```
streamlit run dashboard.py
```

### El bot de WhatsApp
```
python whatsapp_bot.py
```
