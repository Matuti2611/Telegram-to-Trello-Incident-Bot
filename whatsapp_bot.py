"""
Incident Bot: WhatsApp -> Gemini -> Trello.

Refactor notas:
- Valida la firma X-Hub-Signature-256 en cada webhook (seguridad).
- Deduplica por id de mensaje (WhatsApp reintenta si no ve 200 a tiempo).
- Responde 200 rapido y procesa en background (threading).
- Memoria conversacional y set de vistos con lock y TTL.
- Timeouts en todas las llamadas HTTP; logging estructurado.
- IDs de etiquetas de Trello en trello_labels.json.
- Dev server: python whatsapp_bot.py. Prod: gunicorn -w 4 whatsapp_bot:app
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

import google.generativeai as genai
import requests
from dotenv import load_dotenv
from flask import Flask, abort, request
from pydantic import BaseModel, Field

import asyncio
from prisma import Prisma

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("incident-bot")


def _req(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Falta variable de entorno: {name}")
    return val


WHATSAPP_TOKEN = _req("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = _req("PHONE_NUMBER_ID")
VERIFY_TOKEN = _req("VERIFY_TOKEN")
APP_SECRET = _req("WHATSAPP_APP_SECRET")
GEMINI_API_KEY = _req("GEMINI_API_KEY")
TRELLO_KEY = _req("TRELLO_KEY")
TRELLO_TOKEN = _req("TRELLO_TOKEN")
TRELLO_LIST_ID = _req("TRELLO_LIST_ID")
DEEPGRAM_API_KEY = _req("DEEPGRAM_API_KEY")

GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v21.0")
HTTP_TIMEOUT = (5, 30)  # (connect, read) segundos

LABELS_PATH = Path(__file__).parent / "trello_labels.json"
with LABELS_PATH.open(encoding="utf-8") as f:
    TRELLO_LABELS: dict[str, str] = json.load(f)

genai.configure(api_key=GEMINI_API_KEY)


# ---------------------------------------------------------------------------
# Schema de datos para IA
# ---------------------------------------------------------------------------

class TicketData(BaseModel):
    direccion: str = Field(default="", description="Direccion de la propiedad (calle y altura). Ej: Pellegrini 1200.")
    unidad: str = Field(default="", description="Departamento o unidad. Si es casa, 'N/A'.")
    categoria: str = Field(default="", description="Plomeria, Electricidad, Limpieza, Ruidos, Gas, Otros.")
    urgencia: str = Field(default="", description="Baja, Media o Alta.")
    resumen_operativo: str = Field(default="", description="Resumen corto para el tecnico.")
    datos_faltantes: list[str] = Field(
        default_factory=list,
        description="Lista de campos vacios o faltantes. Vacia si estan todos los datos.",
    )
    respuesta_usuario: str = Field(default="", description="Mensaje para el usuario.")


# ---------------------------------------------------------------------------
# Estado en memoria con lock y TTL
# ---------------------------------------------------------------------------

MEMORY_TTL_SEC = 30 * 60       # 30 min sin actividad -> se limpia
DEDUPE_TTL_SEC = 24 * 60 * 60  # 24 h

_state_lock = threading.Lock()
_memoria: dict[str, dict] = {}          # wa_id -> {"textos": [...], "foto": bytes|None, "updated_at": ts}
_seen_messages: dict[str, float] = {}   # msg_id -> ts
_esperando_id: dict[str, dict] = {}


def _gc_expired_locked() -> None:
    now = time.time()
    for k in [k for k, v in _memoria.items() if now - v.get("updated_at", 0) > MEMORY_TTL_SEC]:
        _memoria.pop(k, None)
    for k in [k for k, ts in _seen_messages.items() if now - ts > DEDUPE_TTL_SEC]:
        _seen_messages.pop(k, None)


def already_processed(msg_id: str) -> bool:
    """Marca el msg como visto y devuelve True si ya lo habiamos procesado."""
    with _state_lock:
        _gc_expired_locked()
        if msg_id in _seen_messages:
            return True
        _seen_messages[msg_id] = time.time()
        return False


def _ensure_memoria_locked(wa_id: str) -> dict:
    m = _memoria.setdefault(wa_id, {"textos": [], "foto": None, "updated_at": time.time()})
    m["updated_at"] = time.time()
    return m


def append_text(wa_id: str, texto: str) -> None:
    with _state_lock:
        m = _ensure_memoria_locked(wa_id)
        m["textos"].append(texto)


def set_foto(wa_id: str, foto: bytes) -> None:
    with _state_lock:
        m = _ensure_memoria_locked(wa_id)
        m["foto"] = foto


def snapshot(wa_id: str) -> tuple[list[str], Optional[bytes]]:
    with _state_lock:
        m = _memoria.get(wa_id)
        if not m:
            return [], None
        return list(m["textos"]), m["foto"]


def clear_memoria(wa_id: str) -> None:
    with _state_lock:
        _memoria.pop(wa_id, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalizar_numero_ar(number: str) -> str:
    """Quita el '9' intermedio de moviles argentinos (54 9 11 ... -> 54 11 ...)."""
    if number.startswith("549"):
        return "54" + number[3:]
    return number


def verificar_firma(raw_body: bytes, signature_header: str) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = signature_header.split("=", 1)[1]
    mac = hmac.new(APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, expected)


def enviar_whatsapp(texto: str, recipient_id: str) -> None:
    target = normalizar_numero_ar(recipient_id)
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    data = {
        "messaging_product": "whatsapp",
        "to": target,
        "type": "text",
        "text": {"body": texto},
    }
    try:
        res = requests.post(url, json=data, headers=headers, timeout=HTTP_TIMEOUT)
        if res.status_code >= 400:
            log.error("WhatsApp send failed %s: %s", res.status_code, res.text)
    except requests.RequestException:
        log.exception("Error enviando mensaje a WhatsApp")


def descargar_media(media_id: str) -> Optional[bytes]:
    try:
        info = requests.get(
            f"https://graph.facebook.com/{GRAPH_API_VERSION}/{media_id}",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
            timeout=HTTP_TIMEOUT,
        )
        info.raise_for_status()
        media_url = info.json().get("url")
        if not media_url:
            log.error("No hay 'url' en metadata de media %s", media_id)
            return None
        data = requests.get(
            media_url,
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
            timeout=HTTP_TIMEOUT,
        )
        data.raise_for_status()
        return data.content
    except requests.RequestException:
        log.exception("Error descargando media %s", media_id)
        return None


def transcribir_audio(audio_bytes: bytes) -> Optional[str]:
    try:
        res = requests.post(
            "https://api.deepgram.com/v1/listen?model=nova-2&language=es&smart_format=true",
            headers={
                "Authorization": f"Token {DEEPGRAM_API_KEY}",
                "Content-Type": "audio/ogg",
            },
            data=audio_bytes,
            timeout=HTTP_TIMEOUT,
        )
        res.raise_for_status()
        transcript = (
            res.json()["results"]["channels"][0]["alternatives"][0]["transcript"]
        )
        return transcript or None
    except (requests.RequestException, KeyError, IndexError, ValueError):
        log.exception("Error transcribiendo audio en Deepgram")
        return None


# ---------------------------------------------------------------------------
# Trello
# ---------------------------------------------------------------------------

def crear_ticket_trello(datos: TicketData, foto_bytes: Optional[bytes] = None, cliente_id: str = "Sin ID") -> Optional[str]:
    labels = []
    for clave in (datos.categoria, datos.urgencia):
        label_id = TRELLO_LABELS.get(clave)
        if label_id:
            labels.append(label_id)
        else:
            log.warning("Sin label configurada para %r", clave)

    query = {
        "key": TRELLO_KEY,
        "token": TRELLO_TOKEN,
        "idList": TRELLO_LIST_ID,
        "name": f"[{cliente_id}] [{datos.urgencia}] {datos.categoria} - {datos.direccion}",
        "desc": (
            f"**ID Cliente:** {cliente_id}\n\n"
            f"**Unidad:** {datos.unidad}\n\n"
            f"**Descripcion:** {datos.resumen_operativo}\n\n"
            "*Ticket creado por Incident Bot*"
        ),
        "idLabels": ",".join(labels),
        "pos": "top",
    }

    try:
        res = requests.post(
            "https://api.trello.com/1/cards",
            params=query,
            timeout=HTTP_TIMEOUT,
        )
        res.raise_for_status()
        card_id = res.json()["id"]
    except (requests.RequestException, KeyError, ValueError):
        log.exception("Fallo creando card en Trello")
        return None

    if foto_bytes:
        try:
            requests.post(
                f"https://api.trello.com/1/cards/{card_id}/attachments",
                params={"key": TRELLO_KEY, "token": TRELLO_TOKEN},
                files={"file": ("foto.jpg", foto_bytes)},
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException:
            log.exception("No se pudo adjuntar foto al ticket %s", card_id)

    return card_id


# ---------------------------------------------------------------------------
# IA
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "Sos un asistente de mantenimiento que se llama Claudio. Tu tono es amable y empatico. "
    "REGLA DE ORO: Si la informacion ya aparece en el historial, NO la pidas de nuevo.\n"
    "\n"
    "QUE PODES PREGUNTAR:\n"
    "- Direccion (calle y altura, ej 'Pellegrini 1200').\n"
    "- Unidad, SOLO si la persona dijo que vive en un edificio/departamento y aun no la dio.\n"
    "- Detalle del problema si el resumen no se entiende.\n"
    "\n"
    "QUE NO PODES PREGUNTAR NUNCA:\n"
    "- La categoria: inferila del relato (Plomeria, Electricidad, Limpieza, Ruidos, Gas, Otros).\n"
    "- La urgencia: inferila del relato. Guia: Alta = gas, incendio, inundacion, sin luz, peligro; "
    "Media = ruidos persistentes, perdidas menores, reparaciones molestas pero no peligrosas; "
    "Baja = pedidos cosmeticos o no urgentes.\n"
    "\n"
    "REGLAS PARA datos_faltantes:\n"
    "- Solo pueden ir en datos_faltantes: 'direccion', 'unidad' (si corresponde), "
    "o 'detalle' (si el problema no se entiende).\n"
    "- NUNCA pongas 'categoria' ni 'urgencia' en datos_faltantes. Siempre elegi vos un valor.\n"
    "- Si no falta ninguno de los anteriores, datos_faltantes = [].\n"
    "\n"
    "OTROS:\n"
    "- NO asumas direcciones. Si el usuario no dijo calle Y altura, direccion queda vacia y va en datos_faltantes.\n"
    "- 'piso 5' NO es una direccion, es la unidad. Si solo tenes la unidad, falta la direccion.\n"
    "- Si el historial es corto o solo hay un saludo, no inventes datos tecnicos.\n"
    "- Si la persona no vive en un edificio, en unidad pone 'N/A' y no la pidas.\n"
    "- El campo respuesta_usuario siempre debe tener texto util, nunca vacio. "
    "Si datos_faltantes esta vacio, respuesta_usuario es un mensaje breve de agradecimiento.\n"
    "\n"
    "FORMATO DE RESPUESTA: devolve SIEMPRE un JSON valido con exactamente estas claves "
    "(todas obligatorias, strings salvo datos_faltantes que es array de strings):\n"
    '{"direccion": "", "unidad": "", "categoria": "", "urgencia": "", '
    '"resumen_operativo": "", "datos_faltantes": [], "respuesta_usuario": ""}'
)


def procesar_con_ia(historial: str) -> Optional[TicketData]:
    try:
        model = genai.GenerativeModel(
            "gemini-3-flash-preview",
            system_instruction=SYSTEM_PROMPT,
        )
        # ACÁ ESTÁ LA CLAVE: Solo le pedimos que sea JSON, 
        # pero NO le pasamos el schema de Pydantic.
        response = model.generate_content(
            f"HISTORIAL: {historial}",
            generation_config={"response_mime_type": "application/json"}, 
        )
    except Exception:
        log.exception("Error llamando a Gemini (historial=%r)", historial)
        return None

    # Parseo manual tolerante: usamos model_validate, que respeta los defaults
    # de TicketData para claves faltantes. Evita pasarle el Pydantic model al
    # SDK (que rompe con el campo 'default' en el proto Schema).
    try:
        raw = json.loads(response.text)
        if not isinstance(raw, dict):
            raise ValueError(f"Gemini no devolvio un objeto JSON: {raw!r}")
        return TicketData.model_validate(raw)
    except Exception:
        log.exception("Error parseando JSON de Gemini: %r", getattr(response, "text", None))
        return None


# ---------------------------------------------------------------------------
# Validacion deterministica de direccion
# ---------------------------------------------------------------------------

_RX_TIENE_TEXTO = re.compile(r"[A-Za-zAEIOUaeiouNn]{3,}")
_RX_TIENE_NUMERO = re.compile(r"\d{2,}")


def direccion_parece_valida(direccion: str) -> bool:
    if not direccion:
        return False
    return bool(_RX_TIENE_TEXTO.search(direccion) and _RX_TIENE_NUMERO.search(direccion))


# ---------------------------------------------------------------------------
# Procesamiento (corre en thread aparte)
# ---------------------------------------------------------------------------

def procesar_mensaje(msg: dict, wa_id: str) -> None:
    try:
        tipo = msg.get("type")
        texto_actual = ""

        if tipo == "text":
            texto_actual = msg["text"]["body"]

        elif tipo == "audio":
            audio = descargar_media(msg["audio"]["id"])
            if audio:
                texto_actual = transcribir_audio(audio) or ""
            if not texto_actual:
                enviar_whatsapp(
                    "No pude entender el audio. Me lo escribis o lo grabas de nuevo?",
                    wa_id,
                )
                return
            log.info("Transcripcion (%s): %r", wa_id, texto_actual)

        elif tipo == "image":
            foto = descargar_media(msg["image"]["id"])
            if foto:
                set_foto(wa_id, foto)
            texto_actual = msg["image"].get("caption", "") or "[el usuario envio una foto sin texto]"

        else:
            log.info("Tipo de mensaje no soportado: %s", tipo)
            return

        if not texto_actual:
            return

        if wa_id in _esperando_id:
            nuevo_id = texto_actual.strip()

            if nuevo_id.lower() == "cancelar":
                _esperando_id.pop(wa_id, None)
                clear_memoria(wa_id)
                enviar_whatsapp("Ok, cancelé la carga del ticket. Avisame cuando quieras reportar otro problema.", wa_id)
                return

            if len(nuevo_id) > 10 or " " in nuevo_id:
                # El texto no parece un ID, el usuario probablemente ignoró el pedido.
                # Cancelamos el estado de espera y continuamos el flujo normalmente
                log.info("El texto '%s' no parece un ID. Cancelando espera.", nuevo_id)
                _esperando_id.pop(wa_id, None)
            else:
                datos_pausados = _esperando_id.pop(wa_id)
                ticket_pausado = datos_pausados["ticket"]
                foto_pausada = datos_pausados["foto"]

                async def guardar_nuevo_cliente():
                    db = Prisma()
                    await db.connect()
                    await db.clienteubicacion.create(
                        data={
                            "direccion": ticket_pausado.direccion,
                            "clienteId": nuevo_id
                        }
                    )
                    await db.disconnect()

                try:
                    asyncio.run(guardar_nuevo_cliente())
                except Exception as e:
                    log.error(f"Error guardando en BD: {e}")

                card_id = crear_ticket_trello(ticket_pausado, foto_pausada, nuevo_id)
                if card_id:
                    enviar_whatsapp(f"✅ ¡Perfecto! Dirección registrada y ticket cargado en Trello con el ID: {nuevo_id}.", wa_id)
                    clear_memoria(wa_id)
                else:
                    enviar_whatsapp("Entendí el ID pero falló la creación del ticket en Trello.", wa_id)
                return

        append_text(wa_id, texto_actual)
        textos, foto = snapshot(wa_id)
        historial = " | ".join(textos)
        log.info("Procesando historial (%s): %r", wa_id, historial)

        ticket = procesar_con_ia(historial)
        if ticket is None:
            enviar_whatsapp(
                "Uy, tuve un problema procesando tu mensaje. Podes intentarlo de nuevo?",
                wa_id,
            )
            return

        # Chequeo deterministico de direccion; el LLM a veces la da por valida aunque falte altura.
        if not direccion_parece_valida(ticket.direccion):
            if "direccion" not in ticket.datos_faltantes:
                ticket.datos_faltantes.append("direccion")

        if ticket.datos_faltantes:
            log.info("Faltan datos (%s): %s", wa_id, ticket.datos_faltantes)
            respuesta = ticket.respuesta_usuario.strip() or (
                "Me falta informacion para armar el ticket. "
                "Contame, por favor, direccion (calle y altura), "
                "unidad si vivis en un edificio, y un detalle del problema."
            )
            enviar_whatsapp(respuesta, wa_id)
            return

        async def buscar_cliente():
            db = Prisma()
            await db.connect()
            cliente = await db.clienteubicacion.find_first(
                where={"direccion": {"equals": ticket.direccion, "mode": "insensitive"}}
            )
            await db.disconnect()
            return cliente

        try:
            cliente_encontrado = asyncio.run(buscar_cliente())
        except Exception as e:
            log.error(f"Error consultando BD: {e}")
            cliente_encontrado = None

        if cliente_encontrado:
            card_id = crear_ticket_trello(ticket, foto, cliente_encontrado.clienteId)
            if card_id:
                unidad_txt = f" ({ticket.unidad})" if ticket.unidad and ticket.unidad.upper() != "N/A" else ""
                confirmacion = (
                    f"Listo, tome nota. Te paso el resumen de tu reclamo:\n\n"
                    f"- ID Cliente: {cliente_encontrado.clienteId}\n"
                    f"- Categoria: {ticket.categoria}\n"
                    f"- Urgencia: {ticket.urgencia}\n"
                    f"- Direccion: {ticket.direccion}{unidad_txt}\n"
                    f"- Detalle: {ticket.resumen_operativo}\n\n"
                    f"Ya paso al equipo de mantenimiento. Cualquier cambio te aviso por aca."
                )
                enviar_whatsapp(confirmacion, wa_id)
                clear_memoria(wa_id)
            else:
                enviar_whatsapp(
                    "Entendi todo pero fallo la creacion del ticket. "
                    "Intentalo de nuevo en un momento, por favor.",
                    wa_id,
                )
        else:
            _esperando_id[wa_id] = {"ticket": ticket, "foto": foto}
            enviar_whatsapp(
                f"📍 Detecté una dirección nueva: *{ticket.direccion}*.\n"
                f"Por favor, respondeme a este mensaje ÚNICAMENTE con el **ID de Cliente** asociado para registrarlo.", 
                wa_id
            )
    except Exception:
        log.exception("Error en procesar_mensaje (%s)", wa_id)


# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.get("/health")
def health():
    return {"status": "ok"}, 200


@app.get("/webhook")
def verify():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge", ""), 200
    return "No autorizado", 403


@app.post("/webhook")
def webhook():
    raw_body = request.get_data(cache=True)
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verificar_firma(raw_body, signature):
        log.warning("Firma invalida o ausente")
        abort(403)

    data = request.get_json(silent=True) or {}
    try:
        entry = data["entry"][0]["changes"][0]["value"]
    except (KeyError, IndexError, TypeError):
        return "OK", 200

    # WhatsApp manda statuses (delivered/read/sent) al mismo endpoint; los ignoramos.
    messages = entry.get("messages")
    if not messages:
        return "OK", 200

    for msg in messages:
        msg_id = msg.get("id")
        wa_id = msg.get("from")
        if not msg_id or not wa_id:
            continue
        if already_processed(msg_id):
            log.info("Mensaje duplicado ignorado: %s", msg_id)
            continue
        # Respondemos 200 rapido y procesamos en background.
        threading.Thread(
            target=procesar_mensaje,
            args=(msg, wa_id),
            daemon=True,
            name=f"proc-{msg_id[:8]}",
        ).start()

    return "OK", 200


if __name__ == "__main__":
    # Dev: python whatsapp_bot.py. Prod: waitress-serve --host=0.0.0.0 --port=5000 whatsapp_bot:app
    app.run(host="0.0.0.0", port=5000, debug=False)
