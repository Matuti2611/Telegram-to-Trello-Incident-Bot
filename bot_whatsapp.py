import os
import requests
from flask import Flask, request
from dotenv import load_dotenv
import google.generativeai as genai
from pydantic import BaseModel, Field

# 1. CARGA DE CONFIGURACIÓN
load_dotenv()
app = Flask(__name__)

# Credenciales
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TRELLO_KEY = os.getenv("TRELLO_KEY")
TRELLO_TOKEN = os.getenv("TRELLO_TOKEN")
TRELLO_LIST_ID = os.getenv("TRELLO_LIST_ID")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)

# 2. ESQUEMA DE DATOS PARA IA
class TicketData(BaseModel):
    direccion: str = Field(description="Dirección de la propiedad o edificio")
    unidad: str = Field(description="Departamento o unidad. Si es casa, 'N/A'")
    categoria: str = Field(description="Plomeria, Electricidad, Limpieza, Ruidos, Gas, Otros")
    urgencia: str = Field(description="Baja, Media, o Alta")
    resumen_operativo: str = Field(description="Resumen corto para el técnico")
    datos_faltantes: list[str] = Field(description="Lista de campos vacíos o faltantes")
    respuesta_usuario: str = Field(description="Mensaje para el usuario")
    
# --- FUNCIONES DE SOPORTE ---

def limpiar_numero(number):
    """Ajuste para números de Argentina (quita el 9 intermedio)"""
    if number.startswith("549"):
        return "54" + number[3:]
    return number

def enviar_whatsapp(texto, recipient_id):
    target = limpiar_numero(recipient_id)
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    data = {"messaging_product": "whatsapp", "to": target, "type": "text", "text": {"body": texto}}
    
    res = requests.post(url, json=data, headers=headers)
    
    print(f"DEBUG ENVÍO: Status {res.status_code} - Resuesta: {res.text}")

def descargar_media_whatsapp(media_id):
    url_info = f"https://graph.facebook.com/v18.0/{media_id}"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    res = requests.get(url_info, headers=headers).json()
    return requests.get(res['url'], headers=headers).content

def transcribir_audio(audio_bytes):
    try:
        url = "https://api.deepgram.com/v1/listen?model=nova-2&language=es&smart_format=true"
        headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}", "Content-Type": "audio/ogg"}
        res = requests.post(url, headers=headers, data=audio_bytes)
        return res.json()['results']['channels'][0]['alternatives'][0]['transcript']
    except:
        return None

# --- LÓGICA DE TRELLO CON ETIQUETAS REALES ---

def crear_ticket_trello(datos: TicketData, foto_bytes=None):
    url = "https://api.trello.com/1/cards"
    
    # IDs extraídos de tu JSON de 'Gestión de Reclamos Rag & Roll'
    IDS_LABELS = {
        "Plomeria": "69e0d71afef070af8a0ffee0",
        "Electricidad": "69e0d71afef070af8a0ffee1",
        "Limpieza": "69e0d71afef070af8a0ffee2",
        "Ruidos": "69e0d71afef070af8a0ffee4",
        "Otros": "69e0d71afef070af8a0ffee5",
        "Gas": "69e62be231e026c4a0b1227c",
        "Alta": "69e0e8491e98a7b95ba3a5e9",
        "Media": "69e0e85704cab7cb51e69cc6",
        "Baja": "69e0e8640f16ad3791384b4d"
    }
    
    # Buscamos los IDs de etiquetas correspondientes a lo que detectó la IA
    labels = []
    if datos.categoria in IDS_LABELS:
        labels.append(IDS_LABELS[datos.categoria])
    if datos.urgencia in IDS_LABELS:
        labels.append(IDS_LABELS[datos.urgencia])
    
    query = {
        'key': TRELLO_KEY, 
        'token': TRELLO_TOKEN, 
        'idList': TRELLO_LIST_ID,
        'name': f"[{datos.urgencia}] {datos.categoria} - {datos.direccion}",
        'desc': f"**Unidad:** {datos.unidad}\n\n**Descripción:** {datos.resumen_operativo}\n\n*Ticket creado por Incident Bot*",
        'idLabels': ",".join(labels), 
        'pos': 'top'
    }
    
    res = requests.post(url, params=query)
    if res.status_code == 200:
        if foto_bytes:
            card_id = res.json()['id']
            requests.post(f"https://api.trello.com/1/cards/{card_id}/attachments", 
                          params={'key': TRELLO_KEY, 'token': TRELLO_TOKEN}, 
                          files={'file': ('foto.jpg', foto_bytes)})
        return True
    print(f"❌ Fallo Trello: {res.text}")
    return False

# --- PROCESAMIENTO IA ---

def procesar_con_ia(historial):
    model = genai.GenerativeModel('gemini-2.5-flash-lite')
    prompt = (
        "Sos un asistente de mantenimiento que se llama Claudio. Tu tono es amable y empático. "
        "REGLA DE ORO: Si la información ya aparece en el historial, NO la pidas de nuevo. "
        "IMPORTANTE: Debes completar TODOS los campos del JSON. Si no falta nada, datos_faltantes es []. Si falta algún dato, debes pedírselo a la persona.\n"
        "No asumas ningún dato, y si la persona no vive en un edificio, no pongas ni pidas la unidad.\n"
        "- NO asumas direcciones. Si el usuario no dijo Calle y Altura (ej: Pellegrini 1200), "
        "el campo 'direccion' DEBE estar vacío y DEBES poner 'direccion' en 'datos_faltantes'.\n"
        "- 'piso 5' NO es una dirección, es la unidad. Si solo tenés la unidad, falta la dirección.\n"
        "- Si el historial es corto o solo hay un saludo, no inventes datos técnicos.\n"
        "Si el usuario hace un comentario después de crear el ticket, confirmá que ya tomaste nota y no reinicies el formulario.\n\n"
        f"HISTORIAL: {historial}"
    )
    
    response = model.generate_content(
        prompt, 
        generation_config={
            "response_mime_type": "application/json", 
            "response_schema": TicketData
        }
    )
    
    # Intentamos validar, si falla porque falta un campo, devolvemos un objeto básico
    try:
        return TicketData.model_validate_json(response.text)
    except Exception as e:
        print(f"⚠️ Error validando JSON: {e}. Intentando recuperación...")
        # Esto es un "parche" por si Gemini devuelve basura
        import json
        raw_json = json.loads(response.text)
        return TicketData(
            direccion=raw_json.get("direccion", ""),
            unidad=raw_json.get("unidad", ""),
            categoria=raw_json.get("categoria", "Otros"),
            urgencia=raw_json.get("urgencia", "Media"),
            resumen_operativo=raw_json.get("resumen_operativo", ""),
            datos_faltantes=raw_json.get("datos_faltantes", []),
            respuesta_usuario=raw_json.get("respuesta_usuario", "Se me mezclaron los cables, ¿me repetís?")
        )

# --- WEBHOOK Y MEMORIA ---
memoria = {}

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        if request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args.get("hub.challenge")
        return "No autorizado", 403

    data = request.get_json()
    try:
        entry = data['entry'][0]['changes'][0]['value']
        if 'messages' in entry:
            msg = entry['messages'][0]
            wa_id = msg['from']
            
            print(f"--- NUEVO MENSAJE DESDE: {wa_id} ---")
            
            if wa_id not in memoria: memoria[wa_id] = {"textos": [], "foto": None}
            
            texto_actual = ""
            if msg['type'] == 'text':
                texto_actual = msg['text']['body']
            elif msg['type'] == 'audio':
                audio = descargar_media_whatsapp(msg['audio']['id'])
                texto_actual = transcribir_audio(audio)
                print(f"Transcripción: {texto_actual}")
            elif msg['type'] == 'image':
                memoria[wa_id]["foto"] = descargar_media_whatsapp(msg['image']['id'])
                texto_actual = msg['image'].get('caption', '')

            if texto_actual:
                memoria[wa_id]["textos"].append(texto_actual)
                historial_completo = " | ".join(memoria[wa_id]["textos"])
                
                print("Llamando a Gemini...")
                ticket = procesar_con_ia(historial_completo)
                print(f"Gemini respondió: {ticket.resumen_operativo}")

                # Forzamos dirección si es muy corta
                if len(ticket.direccion.strip()) < 5:
                    if "direccion" not in ticket.datos_faltantes:
                        ticket.datos_faltantes.append("direccion")

                if not ticket.datos_faltantes:
                    print("Intentando crear ticket en Trello...")
                    if crear_ticket_trello(ticket, memoria[wa_id]["foto"]):
                        enviar_whatsapp(f"✅ Ticket creado: {ticket.direccion}", wa_id)
                        enviar_whatsapp(ticket.respuesta_usuario, wa_id)
                        memoria.pop(wa_id) 
                else:
                    print(f"Faltan datos: {ticket.datos_faltantes}")
                    enviar_whatsapp(ticket.respuesta_usuario, wa_id)

    except Exception as e:
        print(f"❌ ERROR CRÍTICO EN WEBHOOK: {e}")
    
    return "OK", 200

if __name__ == "__main__":
    app.run(port=5000, debug=True)