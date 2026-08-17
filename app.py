import streamlit as st
import io
import fitz  # PyMuPDF
from concurrent.futures import ThreadPoolExecutor, as_completed
from deep_translator import GoogleTranslator
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_JUSTIFY, TA_CENTER, TA_LEFT
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from xml.sax.saxutils import escape
import os
import time
from datetime import datetime

# ============================================================
# CONFIGURACIÓN
# ============================================================
MAX_CHARS_PER_FRAGMENT = 4500
FONT_PATH = os.path.join(os.path.dirname(__file__), "fonts", "DejaVuSans.ttf")
# Reducimos los workers a 3 para evitar el Rate Limiting (Error 500) de Google
WORKERS_PARALELOS = 3 

# ============================================================
# FUNCIONES DE ESTRUCTURA CON PYMUPDF
# ============================================================
def detectar_estructura_pymupdf(pdf_bytes):
    """
    Usa PyMuPDF para agrupar automáticamente párrafos y detectar títulos
    basándose en el tamaño de la fuente.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    bloques_estructurados = []
    
    # Variables para calcular el tamaño de fuente promedio (texto normal)
    tamanos_fuentes = {}
    
    # Pasada 1: Identificar el tamaño de fuente del cuerpo del texto
    for pagina in doc:
        bloques = pagina.get_text("dict")["blocks"]
        for b in bloques:
            if b["type"] == 0:  # Tipo 0 es texto
                for linea in b["lines"]:
                    for span in linea["spans"]:
                        size = round(span["size"], 1)
                        tamanos_fuentes[size] = tamanos_fuentes.get(size, 0) + len(span["text"])
                        
    # El tamaño de fuente con más caracteres es el "texto normal"
    if tamanos_fuentes:
        fuente_base = max(tamanos_fuentes, key=tamanos_fuentes.get)
    else:
        fuente_base = 10.0

    # Pasada 2: Extraer bloques reales
    for num_pag, pagina in enumerate(doc, start=1):
        alto_pagina = pagina.rect.height
        bloques = pagina.get_text("dict")["blocks"]
        
        for b in bloques:
            if b["type"] == 0:
                # Filtrar encabezados/pies de página (ignorar top 10% y bottom 10%)
                y0 = b["bbox"][1]
                if y0 < alto_pagina * 0.10 or y0 > alto_pagina * 0.90:
                    continue

                texto_bloque = ""
                max_size_in_block = 0
                
                for linea in b["lines"]:
                    for span in linea["spans"]:
                        texto_bloque += span["text"] + " "
                        if span["size"] > max_size_in_block:
                            max_size_in_block = round(span["size"], 1)
                
                texto_bloque = texto_bloque.strip()
                if len(texto_bloque) < 2:
                    continue
                
                # Heurística de jerarquía basada en tamaño de fuente
                if max_size_in_block > fuente_base + 3:
                    tipo = 'titulo_capitulo'
                elif max_size_in_block > fuente_base + 1:
                    tipo = 'titulo_seccion'
                else:
                    tipo = 'texto'
                    
                bloques_estructurados.append((tipo, texto_bloque, num_pag))
                
    return bloques_estructurados

# ============================================================
# TRADUCCIÓN PARALELA CON MANEJO DE ERRORES
# ============================================================
def dividir_texto_inteligente(texto, max_len=MAX_CHARS_PER_FRAGMENT):
    """Divide el texto en fragmentos respetando los puntos y espacios."""
    if len(texto) <= max_len:
        return [texto]
    fragmentos = []
    while len(texto) > max_len:
        idx = texto.rfind('. ', 0, max_len) # Cortar en puntos
        if idx == -1 or idx < max_len * 0.5:
            idx = texto.rfind(' ', 0, max_len) # Fallback a espacios
        if idx == -1: 
            idx = max_len
        fragmentos.append(texto[:idx+1])
        texto = texto[idx+1:].lstrip()
    if texto:
        fragmentos.append(texto)
    return fragmentos

def traducir_bloque(bloque, idioma_destino):
    """Traduce un bloque intentando evadir baneos temporales de IP."""
    tipo, contenido, pagina = bloque
    traductor = GoogleTranslator(source='auto', target=idioma_destino)
    
    fragmentos = dividir_texto_inteligente(contenido)
    traducciones = []
    error = 0
    
    for frag in fragmentos:
        intentos = 0
        exito = False
        while intentos < 3 and not exito:
            try:
                trad = traductor.translate(frag)
                
                # Verificar si la API devolvió su propio mensaje de error web
                if trad and ("Error 500" in trad or "Server Error" in trad):
                    raise Exception("Falso positivo: Error 500 devuelto como texto.")
                
                traducciones.append(trad)
                exito = True
                time.sleep(0.5) # Pausa amigable para no saturar la API
                
            except Exception as e:
                intentos += 1
                time.sleep(2 * intentos) # Backoff: Espera 2s, luego 4s, etc.
                
        if not exito:
            error += 1
            traducciones.append(frag) # Conserva el original si falla definitivamente
            
    return (tipo, ' '.join(traducciones), pagina, error)

def traducir_en_paralelo(parrafos, idioma_destino='es'):
    """Ejecuta las traducciones utilizando hilos paralelos limitados."""
    barra = st.progress(0, text="Iniciando traducción en paralelo...")
    
    resultados = [None] * len(parrafos)
    errores_totales = 0
    completados = 0
    total = len(parrafos)
    
    with ThreadPoolExecutor(max_workers=WORKERS_PARALELOS) as executor:
        futuros = {executor.submit(traducir_bloque, b, idioma_destino): i for i, b in enumerate(parrafos)}
        
        for futuro in as_completed(futuros):
            idx = futuros[futuro]
            tipo, trad, pag, err = futuro.result()
            resultados[idx] = (tipo, trad, pag)
            errores_totales += err
            completados += 1
            
            if completados % 5 == 0 or completados == total:
                barra.progress(completados / total, text=f"Traduciendo... {completados}/{total} bloques")
                
    barra.empty()
    return resultados, errores_totales

# ============================================================
# GENERACIÓN DE PDF Y LOG
# ============================================================
def add_page_number(canvas, doc):
    """Agrega número de página al pie del PDF generado."""
    page_num = canvas.getPageNumber()
    canvas.saveState()
    canvas.setFont('DejaVuSans', 9)
    canvas.drawCentredString(105*mm, 15*mm, str(page_num))
    canvas.restoreState()

def generar_pdf_estructurado(resultado_traduccion):
    """Construye el PDF aplicando estilos académicos."""
    if not os.path.exists(FONT_PATH):
        st.error(f"Falta la fuente en {FONT_PATH}")
        st.stop()
        
    pdfmetrics.registerFont(TTFont("DejaVuSans", FONT_PATH))

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        rightMargin=20*mm, leftMargin=20*mm, topMargin=20*mm, bottomMargin=25*mm
    )

    estilos = getSampleStyleSheet()
    estilo_normal = ParagraphStyle(
        'Normal', parent=estilos['Normal'], fontName='DejaVuSans', 
        fontSize=11, leading=15, alignment=TA_JUSTIFY, spaceAfter=10, firstLineIndent=5*mm
    )
    estilo_capitulo = ParagraphStyle(
        'Capitulo', parent=estilos['Heading1'], fontName='DejaVuSans', 
        fontSize=16, leading=20, alignment=TA_CENTER, spaceAfter=15, spaceBefore=20, bold=True
    )
    estilo_seccion = ParagraphStyle(
        'Seccion', parent=estilos['Heading2'], fontName='DejaVuSans', 
        fontSize=13, leading=16, alignment=TA_LEFT, spaceAfter=10, spaceBefore=10, bold=True
    )

    historia = []
    for tipo, contenido, pagina in resultado_traduccion:
        contenido = escape(contenido)
        if tipo == 'titulo_capitulo':
            historia.append(Paragraph(contenido, estilo_capitulo))
        elif tipo == 'titulo_seccion':
            historia.append(Paragraph(contenido, estilo_seccion))
        else:
            historia.append(Paragraph(contenido, estilo_normal))

    doc.build(historia, onFirstPage=add_page_number, onLaterPages=add_page_number)
    buffer.seek(0)
    return buffer.getvalue()

def generar_log(resultado_traduccion, errores, tiempo_inicio):
    """Genera un archivo de log con estadísticas del proceso."""
    lineas = []
    lineas.append("="*60)
    lineas.append("LOG DE TRADUCCIÓN DE PDF")
    lineas.append(f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lineas.append(f"Tiempo total: {tiempo_inicio:.2f} segundos")
    lineas.append(f"Fragmentos con error (no traducidos): {errores}")
    lineas.append("-"*60)

    total_texto = 0
    total_titulos = 0
    for tipo, _, _ in resultado_traduccion:
        if tipo == 'texto':
            total_texto += 1
        else:
            total_titulos += 1
    lineas.append(f"Total de párrafos de texto procesados: {total_texto}")
    lineas.append(f"Total de títulos procesados: {total_titulos}")
    lineas.append("-"*60)

    lineas.append("Estructura final (primeros 20 elementos):")
    for i, (tipo, contenido, pagina) in enumerate(resultado_traduccion[:20]):
        lineas.append(f"  {i+1:3d}. {tipo:20s} | Pág {pagina:3d} | {contenido[:50]}...")
    if len(resultado_traduccion) > 20:
        lineas.append(f"  ... y {len(resultado_traduccion)-20} elementos más.")

    lineas.append("="*60)
    return "\n".join(lineas)

# ============================================================
# INTERFAZ STREAMLIT
# ============================================================
st.set_page_config(page_title="Traductor Académico de PDFs", layout="centered")
st.title("📚 Traductor de PDFs Académicos V2")
st.markdown("Sube tu documento PDF. La herramienta detectará la estructura, filtrará los encabezados y utilizará procesamiento en paralelo optimizado para evitar bloqueos del servidor.")

archivo_subido = st.file_uploader("Elige tu archivo PDF", type="pdf")

if archivo_subido is not None:
    pdf_bytes = archivo_subido.read()
    
    with st.spinner("Analizando fuentes y estructura del documento..."):
        bloques = detectar_estructura_pymupdf(pdf_bytes)
        
    st.success(f"✅ Estructura detectada: {len(bloques)} bloques de texto y títulos.")

    with st.expander("Ver estructura detectada (primeros 10 elementos)"):
        for i, (tipo, contenido, pagina) in enumerate(bloques[:10]):
            st.write(f"{i+1}. [{tipo}] Pág {pagina}: {contenido[:80]}...")

    if st.button("🌐 Iniciar Traducción Segura", type="primary"):
        t_inicio = time.time()
        
        resultados, errores = traducir_en_paralelo(bloques)
        
        t_total = time.time() - t_inicio
        st.info(f"📊 Traducción completada en {t_total:.1f} seg. ({errores} fragmentos fallidos o devueltos en idioma original).")
        
        with st.spinner("Ensamblando PDF final..."):
            try:
                pdf_final = generar_pdf_estructurado(resultados)
                
                st.download_button(
                    label="📥 Descargar PDF Traducido",
                    data=pdf_final,
                    file_name="traduccion_formateada.pdf",
                    mime="application/pdf",
                    type="primary"
                )
                
                log_texto = generar_log(resultados, errores, t_total)
                st.download_button(
                    label="📋 Descargar LOG del proceso",
                    data=log_texto,
                    file_name="traduccion_log.txt",
                    mime="text/plain"
                )
                
            except Exception as e:
                st.error(f"❌ Error al generar los archivos finales: {e}")
