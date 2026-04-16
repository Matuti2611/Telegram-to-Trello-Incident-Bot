import os
import telebot
import requests
from dotenv import load_dotenv
import google.generativeai as genai
from pydantic import BaseModel, Field

# 1. Cargar variables de entorno
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TRELLO_KEY = os.getenv("TRELLO_KEY")
TRELLO_TOKEN = os.getenv("TRELLO_TOKEN")
TRELLO_LIST_ID = os.getenv("TRELLO_LIST_ID")

# 2. Inicializar clientes
bot = telebot.TeleBot(TELEGRAM_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)

# 3. Estructura de datos (IA)
class TicketData(BaseModel):
    unidad: str = Field(description="Número o letra del departamento/unidad (ej: '4B')")
    categoria: str = Field(description="Plomeria, Electricidad, Limpieza, Ruidos, u Otros")
    urgencia: str = Field(description="Baja, Media, o Alta")
    resumen_operativo: str = Field(description="Resumen corto y formal del problema")
    datos_faltantes: list[str] = Field(description="Datos que faltan para procesar el ticket")

# 4. Mapeo de Etiquetas (Labels) según tu JSON
IDS_LABELS = {
    # Categorías
    "Plomeria": "69e0d71afef070af8a0ffee0",
    "Electricidad": "69e0d71afef070af8a0ffee1",
    "Limpieza": "69e0d71afef070af8a0ffee2",
    "Ruidos": "69e0d71afef070af8a0ffee4",
    "Otros": "69e0d71afef070af8a0ffee5",
    # Prioridades
    "Baja": "69e0e8640f16ad3791384b4d",
    "Media": "69e0e85704cab7cb51e69cc6",
    "Alta": "69e0e8491e98a7b95ba3a5e9"
}

# 5. Función para crear tarjeta y subir adjuntos
def crear_ticket_completo(datos: TicketData, photo_path=None):
    url_card = "https://api.trello.com/1/cards"
    
    # Seleccionar etiquetas
    labels = []
    if datos.categoria in IDS_LABELS: labels.append(IDS_LABELS[datos.categoria])
    # Normalizamos "Media" a "Medio" si es necesario según tu JSON
    urgencia_key = "Media" if datos.urgencia == "Media" else datos.urgencia
    if urgencia_key in IDS_LABELS: labels.append(IDS_LABELS[urgencia_key])

    query = {
        'key': TRELLO_KEY,
        'token': TRELLO_TOKEN,
        'idList': TRELLO_LIST_ID,
        'name': f"[{datos.urgencia}] {datos.categoria} - Unidad {datos.unidad}",
        'desc': f"**Resumen:** {datos.resumen_operativo}\n\n---\n*Generado por Incident Bot*",
        'idLabels': ",".join(labels),
        'pos': 'top'
    }

    response = requests.post(url_card, params=query)
    
    if response.status_code == 200:
        card_id = response.json().get('id')
        # Si hay foto, la subimos como adjunto
        if photo_path and os.path.exists(photo_path):
            url_attach = f"https://api.trello.com/1/cards/{card_id}/attachments"
            with open(photo_path, 'rb') as f:
                files = {'file': (os.path.basename(photo_path), f)}
                requests.post(url_attach, params={'key': TRELLO_KEY, 'token': TRELLO_TOKEN}, files=files)
            os.remove(photo_path) # Borramos la foto local para no llenar espacio
        return True
    return False

# 6. Función para procesar con IA (Gemini 3.1 Flash Lite para más cuota)
def procesar_con_ia(texto_usuario: str) -> TicketData:
    model = genai.GenerativeModel('gemini-2.5-flash') # Puedes usar 'gemini-3.1-flash-lite' si tienes el error 429
    prompt = f"Extrae los datos de este reclamo: '{texto_usuario}'. Categorías válidas: Plomeria, Electricidad, Limpieza, Ruidos, Otros. Prioridades: Baja, Media, Alta."
    
    response = model.generate_content(
        prompt,
        generation_config=genai.GenerationConfig(
            response_mime_type="application/json",
            response_schema=TicketData,
        ),
    )
    return TicketData.model_validate_json(response.text)

# 7. Manejo de Telegram
memoria_conversaciones = {}

# --- MANEJADOR DE FOTOS ---
@bot.message_handler(content_types=['photo'])
def manejar_foto(message):
    chat_id = message.chat.id
    bot.reply_to(message, "📸 Foto recibida. Por favor, ahora cuéntame el problema y tu unidad.")
    
    # Descargamos la foto
    file_info = bot.get_file(message.photo[-1].file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    
    photo_name = f"photo_{chat_id}.jpg"
    with open(photo_name, 'wb') as new_file:
        new_file.write(downloaded_file)
    
    # Guardamos la ruta de la foto en la memoria para usarla cuando se complete el texto
    if chat_id not in memoria_conversaciones:
        memoria_conversaciones[chat_id] = {"textos": [], "foto": None}
    elif isinstance(memoria_conversaciones[chat_id], list): # Limpieza si venía del código anterior
         memoria_conversaciones[chat_id] = {"textos": [], "foto": None}
         
    memoria_conversaciones[chat_id]["foto"] = photo_name

# --- MANEJADOR DE TEXTO ---
@bot.message_handler(func=lambda message: True)
def manejar_mensaje(message):
    chat_id = message.chat.id
    
    if chat_id not in memoria_conversaciones or isinstance(memoria_conversaciones[chat_id], list):
        memoria_conversaciones[chat_id] = {"textos": [], "foto": None}
        
    memoria_conversaciones[chat_id]["textos"].append(message.text)
    historial = " ".join(memoria_conversaciones[chat_id]["textos"])
    
    bot.reply_to(message, "⚙️ Analizando...")
    
    try:
        ticket = procesar_con_ia(historial)
        
        if "unidad" in ticket.datos_faltantes or ticket.unidad.lower() == "desconocido":
             bot.reply_to(message, "🤖 ¿Cuál es tu unidad/departamento?")
             return
        
        # Intentar crear ticket
        foto = memoria_conversaciones[chat_id].get("foto")
        if crear_ticket_completo(ticket, foto):
            bot.reply_to(message, f"✅ Ticket Creado\n📍 Unidad: {ticket.unidad}\n🏷️ {ticket.categoria}\n🚨 Prioridad: {ticket.urgencia}")
            memoria_conversaciones.pop(chat_id, None)
        else:
            bot.reply_to(message, "❌ Error en Trello")

    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")

print("🚀 Bot en línea con Fotos + Labels")
bot.infinity_polling()