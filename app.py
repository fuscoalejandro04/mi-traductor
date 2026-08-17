import streamlit as st
import io
from pypdf import PdfReader
from deep_translator import GoogleTranslator
from fpdf import FPDF

# ------------------------------------------------------------
# Funciones del backend
# ------------------------------------------------------------

def extraer_texto_pdf(archivo):
    """Extrae el texto de todas las páginas del PDF subido."""
    lector = PdfReader(archivo)
    texto_completo = ""
    for pagina in lector.pages:
        texto_pagina = pagina.extract_text()
        if texto_pagina:
            texto_completo += texto_pagina + "\n"
    return texto_completo

def traducir_texto_con_progreso(texto, idioma_origen="en", idioma_destino="es", max_len=5000):
    """
    Traduce un texto largo dividiéndolo en fragmentos de hasta max_len caracteres.
    Muestra una barra de progreso en Streamlit.
    """
    if not texto or not texto.strip():
        return ""

    # Dividir el texto en fragmentos respetando palabras
    fragmentos = []
    while len(texto) > max_len:
        # Buscar un espacio o salto de línea cerca del límite para no cortar palabras
        corte = texto.rfind(' ', 0, max_len)
        if corte == -1:  # Si no hay espacio, cortamos exacto
            corte = max_len
        fragmentos.append(texto[:corte])
        texto = texto[corte:].lstrip()  # eliminar espacios sobrantes
    if texto:
        fragmentos.append(texto)

    # Configurar barra de progreso
    barra = st.progress(0, text="Iniciando traducción...")
    status_text = st.empty()

    traductor = GoogleTranslator(source=idioma_origen, target=idioma_destino)
    traducciones = []
    total = len(fragmentos)

    for i, frag in enumerate(fragmentos):
        # Actualizar barra de progreso
        porcentaje = (i + 1) / total
        barra.progress(porcentaje, text=f"Traduciendo fragmento {i+1} de {total}...")
        status_text.text(f"Procesando fragmento {i+1} de {total} ({(porcentaje*100):.0f}%)")
        
        # Traducir el fragmento
        try:
            traduccion = traductor.translate(frag)
            traducciones.append(traduccion)
        except Exception as e:
            status_text.error(f"Error al traducir el fragmento {i+1}: {str(e)}")
            # En caso de error, dejamos el fragmento sin traducir
            traducciones.append(frag)

    # Limpiar mensajes de progreso
    barra.empty()
    status_text.empty()

    # Unir todos los fragmentos traducidos
    return " ".join(traducciones)

def crear_pdf(texto_traducido):
    """
    Crea un archivo PDF en memoria con el texto traducido.
    Usa codificación latin-1 para soportar caracteres españoles (á, é, ñ, etc.)
    """
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font('Helvetica', size=12)
    
    # Forzamos codificación latin-1 para evitar errores con tildes y eñes
    try:
        texto_para_pdf = texto_traducido.encode('latin-1', 'ignore').decode('latin-1')
    except:
        texto_para_pdf = texto_traducido
    
    pdf.multi_cell(0, 10, texto_para_pdf)
    
    # Guardamos en un objeto BytesIO para descarga inmediata
    output = io.BytesIO()
    pdf.output(output)
    return output.getvalue()

# ------------------------------------------------------------
# Interfaz de usuario con Streamlit
# ------------------------------------------------------------

st.set_page_config(
    page_title="Traductor de PDFs",
    page_icon="📄",
    layout="centered"
)

st.title("📄 Traductor de PDF (Inglés → Español)")
st.markdown("Sube un PDF en inglés y obtén su traducción al español.")

# Widget para subir el archivo
archivo_subido = st.file_uploader("Elige tu archivo PDF", type="pdf")

if archivo_subido is not None:
    # 1. Extraer texto
    with st.spinner("Extrayendo texto del PDF..."):
        texto_original = extraer_texto_pdf(archivo_subido)
    
    if texto_original.strip():
        st.subheader("📖 Texto original (Inglés)")
        st.text_area("", texto_original, height=200, key="original")
        
        # 2. Botón para traducir
        if st.button("🌐 Traducir a Español", type="primary"):
            # Llamamos a la función con barra de progreso
            texto_traducido = traducir_texto_con_progreso(texto_original)
            
            if texto_traducido:
                st.subheader("🇪🇸 Texto traducido (Español)")
                st.text_area("", texto_traducido, height=200, key="traducido")
                
                # 3. Crear PDF y botón de descarga
                pdf_bytes = crear_pdf(texto_traducido)
                st.download_button(
                    label="📥 Descargar PDF traducido",
                    data=pdf_bytes,
                    file_name="documento_traducido.pdf",
                    mime="application/pdf"
                )
            else:
                st.error("No se pudo traducir el texto. Revisa que el contenido sea válido.")
    else:
        st.warning("⚠️ No se pudo extraer texto del PDF. Asegúrate de que no sea un documento escaneado (solo imágenes).")
else:
    st.info("👆 Sube un archivo PDF para comenzar.")
