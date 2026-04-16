import streamlit as st
import requests
import os
from dotenv import load_dotenv

load_dotenv()

# Configuración de la página
st.set_page_config(page_title="Rag & Roll - Ops Dashboard", page_icon="🛠️")

st.title("Rag & Roll: Panel de Operaciones")
st.markdown("Aquí puedes ver los reclamos que entran desde Telegram en tiempo real.")

# Traemos las credenciales del .env
TK = os.getenv("TRELLO_KEY")
TT = os.getenv("TRELLO_TOKEN")
LI = os.getenv("TRELLO_LIST_ID")

def cargar_tickets():
    url = f"https://api.trello.com/1/lists/{LI}/cards"
    params = {'key': TK, 'token': TT, 'attachments': 'true', 'labels': 'true'}
    return requests.get(url, params=params).json()

if st.button('🔄 Sincronizar con Trello'):
    tickets = cargar_tickets()
    
    if not tickets:
        st.warning("No hay tickets nuevos.")
    else:
        for t in tickets:
            # Creamos una tarjeta visual para cada ticket
            with st.container(border=True):
                c1, c2 = st.columns([2, 1])
                with c1:
                    st.subheader(t['name'])
                    st.write(t['desc'])
                    # Mostrar las etiquetas coloridas
                    for l in t['labels']:
                        st.button(f"🏷️ {l['name']}", key=f"{t['id']}_{l['id']}", disabled=True)
                
                with c2:
                    # Si el bot subió una foto, la mostramos acá
                    url_att = f"https://api.trello.com/1/cards/{t['id']}/attachments"
                    res = requests.get(url_att, params={'key': TK, 'token': TT}).json()
                    if res:
                        st.image(res[0]['url'], use_container_width=True)
                    
                    st.link_button("Ver en Trello", t['shortUrl'])