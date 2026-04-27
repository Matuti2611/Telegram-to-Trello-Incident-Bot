import streamlit as st
import requests
import os
from dotenv import load_dotenv

# Configuración de entorno y página
load_dotenv()
st.set_page_config(page_title="Rag & Roll - Ops Dashboard", layout="wide", page_icon="🛠️")

# Estilo personalizado para mejorar la visual
st.markdown("""
    <style>
    .main { background-color: #f5f7f9; }
    .stMetric { background-color: #ffffff; padding: 10px; border-radius: 10px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
    </style>
    """, unsafe_allow_html=True)

# Credenciales
TRELLO_KEY = os.getenv("TRELLO_KEY")
TRELLO_TOKEN = os.getenv("TRELLO_TOKEN")
TRELLO_LIST_ID = os.getenv("TRELLO_LIST_ID")

@st.cache_data(ttl=60)
def get_trello_data():
    url = f"https://api.trello.com/1/lists/{TRELLO_LIST_ID}/cards"
    query = {
        'key': TRELLO_KEY,
        'token': TRELLO_TOKEN,
        'attachments': 'true',
        'labels': 'true'
    }
    response = requests.get(url, params=query)
    return response.json() if response.status_code == 200 else []

# --- INTERFAZ ---
st.title("🚀 Rag & Roll: Gestión de Operaciones")
st.subheader("Listado de Tickets Recientes")

if st.button("🔄 Sincronizar con Trello"):
    st.rerun()

tickets = get_trello_data()

if not tickets:
    st.info("No hay tickets pendientes en el tablero.")
else:
    for t in tickets:
        # Usamos un container con borde para simular un "detalle de ticket"
        with st.container(border=True):
            col_info, col_img = st.columns([3, 1])
            
            with col_info:
                # 1. Listado / Título del Ticket
                st.markdown(f"### {t['name']}")
                
                # 2. Detalle: Campos Extraídos (Categoría y Urgencia desde Labels)
                if t['labels']:
                    cols = st.columns(len(t['labels']))
                    for i, label in enumerate(t['labels']):
                        cols[i].info(f"**{label['name']}**")
                
                # 3. Resumen IA (Se extrae de la descripción de la tarjeta)
                st.markdown("#### ✨ Resumen de IA")
                st.info(t['desc'] if t['desc'] else "Sin descripción detallada.")

            with col_img:
                # Mostrar evidencia visual si existe
                url_att = f"https://api.trello.com/1/cards/{t['id']}/attachments"
                att_res = requests.get(url_att, params={'key': TRELLO_KEY, 'token': TRELLO_TOKEN}).json()
                if att_res:
                    st.image(att_res[0]['url'], caption="Evidencia del Incidente", use_container_width=True)
                
                st.link_button("📂 Gestionar en Trello", t['shortUrl'], use_container_width=True)
