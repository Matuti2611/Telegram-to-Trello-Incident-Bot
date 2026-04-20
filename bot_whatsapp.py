import os
import requests
from flask import Flask, request
from dotenv import load_dotenv
import google.generativeai as genai
from pydantic import BaseModel, Field

# 1. Cargar variables de entorno
load_dotenv()
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TRELLO_KEY = os.getenv("TRELLO_KEY")
TRELLO_TOKEN = os.getenv("TRELLO_TOKEN")
TRELLO_LIST_ID = os.getenv("TRELLO_LIST_ID")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")

# 2. Inicializar clientes
app = Flask(__name__)
genai.configure(api_key=GEMINI_API_KEY)

# 3. Estructura de datos para IA
class TicketData(BaseModel):
    direccion: str = Field(description="Dirección de la propiedad o edificio")
    unidad: str = Field(description="Departamento o unidad. Si es casa, 'N/A'")
    categoria: str = Field(description="Plomeria, Electricidad, Limpieza, Ruidos, Otros")
    urgencia: str = Field(description="Baja, Media, o Alta")
    resumen_operativo: str = Field(description="Resumen corto del problema")
    datos_faltantes: list[str] = Field(description="Campos faltantes: direccion, unidad, problema")

# --- FUNCIONES DE APOYO ---

def limpiar_numero(number):
    """
    Lógica para Argentina: Meta a veces manda el '9' pero no lo acepta para enviar.
    Si el número empieza con 549..., le quitamos el 9.
    """
    if number.startswith("549"):
        return "54" + number[3:]
    return number

def enviar_whatsapp(texto, recipient_id):
    # IMPORTANTE: Limpiamos el número antes de enviar
    target_number = limpiar_numero(recipient_id)
    
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    data = {
        "messaging_product": "whatsapp",
        "to": target_number,
        "type": "text",
        "text": {"body": texto}
    }
    res = requests.post(url, json=data, headers=headers)
    if res.status_code != 200:
        print(f"❌ ERROR META ({res.status_code}): {res.text}")
    else:
        print(f"✅ Mensaje enviado a {target_number}")

def descargar_media_whatsapp(media_id):
    url_info = f"https://graph.facebook.com/v18.0/{media_id}"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    res = requests.get(url_info, headers=headers).json()
    return requests.get(res['url'], headers=headers).content

def transcribir_audio(audio_bytes):
    try:
        url = "https://api.deepgram.com/v1/listen?model=nova-2&language=es&smart_format=true"
        headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}", "Content-Type": "audio/ogg"}
        response = requests.post(url, headers=headers, data=audio_bytes)
        return response.json()['results']['channels'][0]['alternatives'][0]['transcript']
    except:
        return None

def crear_ticket_trello(datos: TicketData, foto_bytes=None):
    url = "https://api.trello.com/1/cards"
    IDS_LABELS = {
        "Plomeria": "69e0d71afef070af8a0ffee0", "Electricidad": "69e0d71afef070af8a0ffee1",
        "Limpieza": "69e0d71afef070af8a0ffee2", "Ruidos": "69e0d71afef070af8a0ffee4",
        "Otros": "69e0d71afef070af8a0ffee5", "Baja": "69e0e8640f16ad3791384b4d",
        "Media": "69e0e85704cab7cb51e69cc6", "Alta": "69e0e8491e98a7b95ba3a5e9"
    }
    labels = [IDS_LABELS[datos.categoria]] if datos.categoria in IDS_LABELS else []
    urg = "Media" if datos.urgencia == "Media" else datos.urgencia
    if urg in IDS_LABELS: labels.append(IDS_LABELS[urg])

    query = {
        'key': TRELLO_KEY, 'token': TRELLO_TOKEN, 'idList': TRELLO_LIST_ID,
        'name': f"[{datos.urgencia}] {datos.categoria} - {datos.direccion} ({datos.unidad})",
        'desc': f"**Ubicación:** {datos.direccion}\n**Unidad:** {datos.unidad}\n\n**Resumen:** {datos.resumen_operativo}",
        'idLabels': ",".join(labels), 'pos': 'top'
    }
    res = requests.post(url, params=query)
    if res.status_code == 200 and foto_bytes:
        card_id = res.json()['id']
        requests.post(f"https://api.trello.com/1/cards/{card_id}/attachments", 
                      params={'key': TRELLO_KEY, 'token': TRELLO_TOKEN}, 
                      files={'file': ('foto.jpg', foto_bytes)})
    return res.status_code == 200

def procesar_con_ia(texto):
    model = genai.GenerativeModel('gemini-3-flash-preview')
    prompt = f"Analiza: '{texto}'. Extrae direccion, unidad, categoria, urgencia y resumen. Si falta algo indica en datos_faltantes."
    response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json", "response_schema": TicketData})
    return TicketData.model_validate_json(response.text)

# --- LÓGICA DE MEMORIA ---
memoria = {}

def flujo_principal(wa_id, texto, foto_bytes=None):
    if wa_id not in memoria: memoria[wa_id] = {"textos": [], "foto": None}
    if texto: memoria[wa_id]["textos"].append(texto)
    if foto_bytes: memoria[wa_id]["foto"] = foto_bytes
    
    historial = " ".join(memoria[wa_id]["textos"])
    if not historial: return

    try:
        ticket = procesar_con_ia(historial)
        if ticket.datos_faltantes:
            ayuda = {"direccion": "📍 ¿Dirección?", "unidad": "🏢 ¿Unidad?", "problema": "📝 ¿Qué pasó?"}
            enviar_whatsapp(ayuda.get(ticket.datos_faltantes[0], "🤖 ¿Me das más detalles?"), wa_id)
            return

        if crear_ticket_trello(ticket, memoria[wa_id]["foto"]):
            enviar_whatsapp(f"✅ Ticket Registrado: {ticket.resumen_operativo}", wa_id)
            memoria.pop(wa_id)
    except Exception as e:
        print(f"Error procesando IA: {e}")
        enviar_whatsapp("❌ Error al procesar tu solicitud.", wa_id)

# --- WEBHOOK ENDPOINT ---

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        if request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args.get("hub.challenge")
        return "Error", 403

    data = request.get_json()
    try:
        if 'messages' in data['entry'][0]['changes'][0]['value']:
            message = data['entry'][0]['changes'][0]['value']['messages'][0]
            wa_id = message['from'] # Recibimos ej: 549341...
            
            if message['type'] == 'text':
                flujo_principal(wa_id, message['text']['body'])
            
            elif message['type'] == 'audio':
                audio_bytes = descargar_media_whatsapp(message['audio']['id'])
                texto = transcribir_audio(audio_bytes)
                if texto:
                    enviar_whatsapp(f"🎤 Entendí: {texto}", wa_id)
                    flujo_principal(wa_id, texto)
            
            elif message['type'] == 'image':
                foto_bytes = descargar_media_whatsapp(message['image']['id'])
                caption = message['image'].get('caption', '')
                flujo_principal(wa_id, caption, foto_bytes=foto_bytes)
                enviar_whatsapp("📸 Foto recibida.", wa_id)

    except Exception as e:
        print(f"Error Webhook: {e}")
    
    return "OK", 200

if __name__ == "__main__":
    app.run(port=5000, debug=True)