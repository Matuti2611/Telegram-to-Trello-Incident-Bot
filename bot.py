import os
import telebot
import requests
from dotenv import load_dotenv
import google.generativeai as genai
from pydantic import BaseModel, Field
# Importamos solo el cliente para evitar errores de versión
from deepgram import DeepgramClient 

# 1. Cargar variables de entorno
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TRELLO_KEY = os.getenv("TRELLO_KEY")
TRELLO_TOKEN = os.getenv("TRELLO_TOKEN")
TRELLO_LIST_ID = os.getenv("TRELLO_LIST_ID")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")

# 2. Inicializar clientes
bot = telebot.TeleBot(TELEGRAM_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)
deepgram = DeepgramClient(api_key=DEEPGRAM_API_KEY)

# 3. Estructura de datos (IA)
class TicketData(BaseModel):
    direccion: str = Field(description="Dirección de la propiedad o edificio")
    unidad: str = Field(description="Departamento o unidad. Si es casa, 'N/A'")
    categoria: str = Field(description="Plomeria, Electricidad, Limpieza, Ruidos, Otros")
    urgencia: str = Field(description="Baja, Media, o Alta")
    resumen_operativo: str = Field(description="Resumen corto del problema")
    datos_faltantes: list[str] = Field(description="Lista de campos faltantes: direccion, unidad, problema")

# --- FUNCIONES DE APOYO ---

def transcribir_audio(audio_bytes):
    try:
        # Le pegamos directo a la API sin usar la librería DeepgramClient
        url = "https://api.deepgram.com/v1/listen?model=nova-2&language=es&smart_format=true"
        headers = {
            "Authorization": f"Token {DEEPGRAM_API_KEY}",
            "Content-Type": "audio/ogg"
        }
        
        response = requests.post(url, headers=headers, data=audio_bytes)
        
        if response.status_code == 200:
            res_json = response.json()
            transcript = res_json['results']['channels'][0]['alternatives'][0]['transcript']
            return transcript
        else:
            print(f"❌ Error API Deepgram: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        print(f"❌ Error de conexión: {e}")
        return None

def crear_ticket_trello(datos: TicketData, foto_bytes=None):
    url = "https://api.trello.com/1/cards"
    # Mapeo de IDs de tu tablero
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
    
    labels = [IDS_LABELS[datos.categoria]] if datos.categoria in IDS_LABELS else []
    urg = "Media" if datos.urgencia == "Media" else datos.urgencia
    if urg in IDS_LABELS: labels.append(IDS_LABELS[urg])

    # Título profesional para Trello
    nombre_ticket = f"[{datos.urgencia}] {datos.categoria} - {datos.direccion} ({datos.unidad})"

    query = {
        'key': TRELLO_KEY, 
        'token': TRELLO_TOKEN, 
        'idList': TRELLO_LIST_ID,
        'name': nombre_ticket,
        'desc': f"**Ubicación:** {datos.direccion}\n**Unidad:** {datos.unidad}\n\n**Resumen:** {datos.resumen_operativo}\n\n*Generado vía Incident Bot*",
        'idLabels': ",".join(labels), 
        'pos': 'top'
    }
    
    res = requests.post(url, params=query)
    if res.status_code == 200 and foto_bytes:
        card_id = res.json()['id']
        attach_url = f"https://api.trello.com/1/cards/{card_id}/attachments"
        requests.post(attach_url, params={'key': TRELLO_KEY, 'token': TRELLO_TOKEN}, 
                      files={'file': ('evidencia.jpg', foto_bytes)})
    return res.status_code == 200

def procesar_con_ia(texto):
    model = genai.GenerativeModel('gemini-3-flash-preview')
    prompt = (
        f"Analiza este reclamo: '{texto}'. "
        "1. Extrae 'direccion' (edificio o calle). "
        "2. Extrae 'unidad' (depto/piso). Si es casa pon 'N/A'. "
        "3. Si falta direccion, unidad o descripcion del problema, indícalo en datos_faltantes. "
        "Categorías: Plomeria, Electricidad, Limpieza, Ruidos, Otros. Prioridades: Baja, Media, Alta."
    )
    response = model.generate_content(
        prompt, 
        generation_config={"response_mime_type": "application/json", "response_schema": TicketData}
    )
    return TicketData.model_validate_json(response.text)

# --- LÓGICA DE MEMORIA Y FLUJO ---
memoria = {}

def flujo_principal(message, texto_nuevo):
    chat_id = message.chat.id
    if chat_id not in memoria:
        memoria[chat_id] = {"textos": [], "foto": None}
    
    if texto_nuevo:
        memoria[chat_id]["textos"].append(texto_nuevo)
    
    historial = " ".join(memoria[chat_id]["textos"])
    if not historial: return

    bot.send_chat_action(chat_id, 'typing')
    try:
        ticket = procesar_con_ia(historial)
        
        if ticket.datos_faltantes:
            ayuda = {
                "direccion": "📍 ¿Me decís la **dirección** o nombre del edificio?",
                "unidad": "🏢 ¿Cuál es el **departamento o piso**?",
                "problema": "📝 ¿Qué es lo que está pasando exactamente?"
            }
            falta = ticket.datos_faltantes[0]
            bot.reply_to(message, ayuda.get(falta, "🤖 ¿Me das más detalles?"))
            return

        # Si tenemos todo, creamos el ticket
        if crear_ticket_trello(ticket, memoria[chat_id]["foto"]):
            bot.reply_to(message, (
                f"✅ **¡Ticket Registrado!**\n\n"
                f"📍 **Ubicación:** {ticket.direccion}\n"
                f"🚪 **Unidad:** {ticket.unidad}\n"
                f"🏷️ **Categoría:** {ticket.categoria}\n"
                f"📄 **Resumen:** {ticket.resumen_operativo}"
            ), parse_mode="Markdown")
            memoria.pop(chat_id)
        else:
            bot.reply_to(message, "❌ Error al conectar con Trello.")
    except Exception as e:
        print(f"Error: {e}")
        bot.reply_to(message, "❌ Error procesando el reporte.")

# --- MANEJADORES DE TELEGRAM ---

@bot.message_handler(content_types=['voice'])
def manejar_voz(message):
    bot.reply_to(message, "🎤 Procesando audio...")
    file_info = bot.get_file(message.voice.file_id)
    audio_bytes = bot.download_file(file_info.file_path)
    
    texto = transcribir_audio(audio_bytes)
    
    if texto:
        bot.reply_to(message, f"📝 *Entendí:* _{texto}_", parse_mode="Markdown")
        flujo_principal(message, texto)
    else:
        bot.reply_to(message, "❌ No pude procesar el audio.")

@bot.message_handler(content_types=['photo'])
def manejar_foto(message):
    chat_id = message.chat.id
    file_info = bot.get_file(message.photo[-1].file_id)
    if chat_id not in memoria:
        memoria[chat_id] = {"textos": [], "foto": None}
    
    memoria[chat_id]["foto"] = bot.download_file(file_info.file_path)
    
    if message.caption:
        flujo_principal(message, message.caption)
    else:
        bot.reply_to(message, "📸 Foto recibida. Ahora decime la dirección y el problema.")

@bot.message_handler(func=lambda m: True)
def manejar_texto(message):
    flujo_principal(message, message.text)

print("🚀 Rag & Roll Bot Online (Voz + Foto + Texto)")
bot.infinity_polling()