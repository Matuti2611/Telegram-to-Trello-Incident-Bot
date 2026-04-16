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
    direccion: str = Field(description="Dirección de la propiedad o nombre del edificio")
    unidad: str = Field(description="Departamento, piso o unidad específica. Si es una casa, poner 'N/A'")
    categoria: str = Field(description="Plomeria, Electricidad, Limpieza, Ruidos, u Otros")
    urgencia: str = Field(description="Baja, Media, o Alta")
    resumen_operativo: str = Field(description="Resumen corto y formal del problema")
    datos_faltantes: list[str] = Field(description="Lista de campos faltantes: direccion, unidad, problema")

# 4. Mapeo de Etiquetas (Labels)
IDS_LABELS = {
    "Plomeria": "69e0d71afef070af8a0ffee0",
    "Electricidad": "69e0d71afef070af8a0ffee1",
    "Limpieza": "69e0d71afef070af8a0ffee2",
    "Ruidos": "69e0d71afef070af8a0ffee4",
    "Otros": "69e0d71afef070af8a0ffee5",
    "Baja": "69e0e8640f16ad3791384b4d",
    "Media": "69e0e85704cab7cb51e69cc6",
    "Alta": "69e0e8491e98a7b95ba3a5e9"
}

# 5. Función para crear tarjeta en Trello
def crear_ticket_completo(datos: TicketData, foto_bytes=None):
    url_card = "https://api.trello.com/1/cards"
    
    labels = []
    if datos.categoria in IDS_LABELS: labels.append(IDS_LABELS[datos.categoria])
    urgencia_key = "Media" if datos.urgencia == "Media" else datos.urgencia
    if urgencia_key in IDS_LABELS: labels.append(IDS_LABELS[urgencia_key])

    # Título estandarizado con Dirección y Unidad
    nombre_ticket = f"[{datos.urgencia}] {datos.categoria} - {datos.direccion} ({datos.unidad})"

    query = {
        'key': TRELLO_KEY,
        'token': TRELLO_TOKEN,
        'idList': TRELLO_LIST_ID,
        'name': nombre_ticket,
        'desc': f"**Ubicación:** {datos.direccion} - Unidad: {datos.unidad}\n\n**Resumen:** {datos.resumen_operativo}\n\n---\n*Generado por Incident Bot*",
        'idLabels': ",".join(labels),
        'pos': 'top'
    }

    response = requests.post(url_card, params=query)
    
    if response.status_code == 200:
        card_id = response.json().get('id')
        if foto_bytes:
            url_attach = f"https://api.trello.com/1/cards/{card_id}/attachments"
            archivos = {'file': ('evidencia.jpg', foto_bytes)}
            requests.post(url_attach, params={'key': TRELLO_KEY, 'token': TRELLO_TOKEN}, files=archivos)
        return True
    return False

# 6. Función para procesar con IA (Gemini 3 Flash)
def procesar_con_ia(texto_usuario: str) -> TicketData:
    model = genai.GenerativeModel('gemini-3-flash-preview')
    prompt = (
        f"Analiza este reclamo: '{texto_usuario}'. "
        f"1. Extrae la 'direccion' (calle o nombre del edificio). Si no está, agrégalo a datos_faltantes. "
        f"2. Extrae la 'unidad' (departamento/piso). Si parece ser un edificio y no hay unidad, agrégalo a datos_faltantes. "
        f"3. Si es una casa particular, en 'unidad' pon 'N/A'. "
        f"4. Si no se describe el problema, agrega 'problema' a datos_faltantes. "
        f"5. Categorías: Plomeria, Electricidad, Limpieza, Ruidos, Otros. "
        f"6. Prioridades: Baja, Media, Alta."
    )
    
    response = model.generate_content(
        prompt,
        generation_config=genai.GenerationConfig(
            response_mime_type="application/json",
            response_schema=TicketData,
        ),
    )
    return TicketData.model_validate_json(response.text)

# 7. Lógica de Memoria y Flujo
memoria_conversaciones = {}

def flujo_principal(message, texto_a_procesar):
    chat_id = message.chat.id
    
    if chat_id not in memoria_conversaciones or isinstance(memoria_conversaciones[chat_id], list):
        memoria_conversaciones[chat_id] = {"textos": [], "foto": None}
    
    if texto_a_procesar:
        memoria_conversaciones[chat_id]["textos"].append(texto_a_procesar)
    
    historial = " ".join(memoria_conversaciones[chat_id]["textos"])
    if not historial: return

    bot.send_chat_action(chat_id, 'typing')
    
    try:
        ticket = procesar_con_ia(historial)
        
        # CHEQUEO DE DATOS FALTANTES PERSONALIZADO
        if ticket.datos_faltantes:
            mensajes_ayuda = {
                "direccion": "📍 ¿Podrías decirme la **dirección** o nombre del edificio?",
                "unidad": "🏢 Veo que es un edificio, ¿cuál es tu **piso o departamento**?",
                "problema": "📝 ¿Qué es lo que está pasando exactamente? Necesito una descripción."
            }
            falta = ticket.datos_faltantes[0]
            bot.reply_to(message, mensajes_ayuda.get(falta, "🤖 Necesito más detalles para procesar el ticket."))
            return

        # Si todo está OK, creamos el ticket
        foto_bytes = memoria_conversaciones[chat_id].get("foto")
        if crear_ticket_completo(ticket, foto_bytes):
            bot.reply_to(message, (
                f"✅ **¡Ticket Registrado!**\n\n"
                f"📍 **Ubicación:** {ticket.direccion}\n"
                f"🚪 **Unidad:** {ticket.unidad}\n"
                f"🏷️ **Categoría:** {ticket.categoria}\n"
                f"🚨 **Urgencia:** {ticket.urgencia}\n"
                f"📄 **Resumen:** {ticket.resumen_operativo}"
            ), parse_mode="Markdown")
            memoria_conversaciones.pop(chat_id, None)
        else:
            bot.reply_to(message, "❌ Error al conectar con Trello.")

    except Exception as e:
        print(f"Error: {e}")
        bot.reply_to(message, "❌ Hubo un error al procesar tu mensaje.")

# --- MANEJADORES ---

@bot.message_handler(content_types=['photo'])
def manejar_foto(message):
    chat_id = message.chat.id
    file_info = bot.get_file(message.photo[-1].file_id)
    foto_bytes = bot.download_file(file_info.file_path)
    
    if chat_id not in memoria_conversaciones or isinstance(memoria_conversaciones[chat_id], list):
        memoria_conversaciones[chat_id] = {"textos": [], "foto": None}
    
    memoria_conversaciones[chat_id]["foto"] = foto_bytes
    
    if message.caption:
        flujo_principal(message, message.caption)
    else:
        bot.reply_to(message, "📸 Foto recibida. Por favor, decime la dirección, unidad y el problema.")

@bot.message_handler(func=lambda message: True)
def manejar_texto(message):
    flujo_principal(message, message.text)

print("🚀 Rag & Roll Bot Online - MVP Week 1")
bot.infinity_polling()