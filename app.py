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
import json
import hashlib
from datetime import datetime

# ============================================================
# CONFIGURACIÓN INDUSTRIAL Y PARÁMETROS
# ============================================================
MAX_CHARS_PER_FRAGMENT = 4500
FONT_PATH = os.path.join(os.path.dirname(__file__), "fonts", "DejaVuSans.ttf")
CHECKPOINT_DIR = os.path.join(os.path.dirname(__file__), ".checkpoints")

# Reglas estrictas de arquitectura según la devolución
WORKERS_PARALELOS = 2
TAMANO_LOTE = 15
PAUSA_BASE_ENTRE_LOTES = 3.0

# Crear directorio de checkpoints si no existe
if not os.path.exists(CHECKPOINT_DIR):
    os.makedirs(CHECKPOINT_DIR)

# ============================================================
# FUNCIONES DE CHECKPOINT (PERSISTENCIA EN DISCO)
# ============================================================
def generar_hash_archivo(pdf_bytes):
    """Genera un ID único para el PDF basado en su contenido."""
    return hashlib.md5(pdf_bytes).hexdigest()

def cargar_checkpoint(file_hash):
    """Carga el progreso guardado en disco si existe."""
    filepath = os.path.join(CHECKPOINT_DIR, f"{file_hash}.json")
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"resultados": {}, "errores_totales": 0, "ultimo_indice": -1}

def guardar_checkpoint(file_hash, resultados_dict, errores, ultimo_indice):
    """Guarda atómicamente el progreso del lote en el disco."""
    filepath = os.path.join(CHECKPOINT_DIR, f"{file_hash}.json")
    temp_filepath = filepath + ".tmp"
    
    data = {
        "resultados": resultados_dict,
        "errores_totales": errores,
        "ultimo_indice": ultimo_indice
    }
    
    # Escritura atómica para evitar corrupción si se corta la luz justo al guardar
    with open(temp_filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(temp_filepath, filepath)

# ============================================================
# FUNCIONES DE ESTRUCTURA CON PYMUPDF
# ============================================================
def detectar_estructura_pymupdf(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    bloques_estructurados = []
    tamanos_fuentes = {}
    
    # Pasada 1: Muestreo
    for pagina in doc:
        bloques = pagina.get_text("dict")["blocks"]
        for b in bloques:
            if b["type"] == 0:
                for linea in b["lines"]:
                    for span in linea["spans"]:
                        size = round(span["size"], 1)
                        tamanos_fuentes[size] = tamanos_fuentes.get(size, 0) + len(span["text"])
                        
    fuente_base = max(tamanos_fuentes, key=tamanos_fuentes.get) if tamanos_fuentes else 10.0

    # Pasada 2: Extracción con filtrado geométrico (10% superior/inferior)
    for num_pag, pagina in enumerate(doc, start=1):
        alto_pagina = pagina.rect.height
        bloques = pagina.get_text("dict")["blocks"]
        
        for b in bloques:
            if b["type"] == 0:
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
                
                if max_size_in_block > fuente_base + 3:
                    tipo = 'titulo_capitulo'
                elif max_size_in_block > fuente_base + 1:
                    tipo = 'titulo_seccion'
                else:
                    tipo = 'texto'
                    
                bloques_estructurados.append((tipo, texto_bloque, num_pag))
                
    return bloques_estructurados

# ============================================================
# TRADUCCIÓN CON RATE LIMITING Y REINTENTOS LINEALES
# ============================================================
def dividir_texto_inteligente(texto, max_len=MAX_CHARS_PER_FRAGMENT):
    if len(texto) <= max_len:
        return [texto]
    fragmentos = []
    while len(texto) > max_len:
        idx = texto.rfind('. ', 0, max_len)
        if idx == -1 or idx < max_len * 0.5:
            idx = texto.rfind(' ', 0, max_len)
        if idx == -1: 
            idx = max_len
        fragmentos.append(texto[:idx+1])
        texto = texto[idx+1:].lstrip()
    if texto:
        fragmentos.append(texto)
    return fragmentos

def traducir_bloque(bloque, idioma_destino):
    tipo, contenido, pagina = bloque
    traductor = GoogleTranslator(source='auto', target=idioma_destino)
    
    fragmentos = dividir_texto_inteligente(contenido)
    traducciones = []
    error_count = 0
    reintentos_usados = 0
    t_inicio = time.time()
    
    for frag in fragmentos:
        intentos = 0
        exito = False
        while intentos < 3 and not exito:
            try:
                trad = traductor.translate(frag)
                if trad and ("Error 500" in trad or "Server Error" in trad or "That's an error" in trad):
                    raise Exception("Respuesta corrupta (Rate Limit/Error 500)")
                
                traducciones.append(trad)
                exito = True
            except Exception:
                intentos += 1
                reintentos_usados += 1
                if intentos < 3:
                    # Espera lineal exacta solicitada: 3s, 6s, 9s
                    time.sleep(3 * intentos) 
                
        if not exito:
            error_count += 1
            traducciones.append(frag) # Fallback: mantener original
            
    latencia = time.time() - t_inicio
    return (tipo, ' '.join(traducciones), pagina, error_count, reintentos_usados, latencia)

def procesar_pipeline_industrial(bloques, file_hash, panel_metricas, idioma_destino='es'):
    """Orquestador principal con Checkpoints y Rate Limiting Adaptativo."""
    checkpoint = cargar_checkpoint(file_hash)
    resultados_dict = checkpoint["resultados"]
    errores_totales = checkpoint["errores_totales"]
    total_bloques = len(bloques)
    
    # Determinar qué falta procesar
    indices_faltantes = [i for i in range(total_bloques) if str(i) not in resultados_dict]
    
    if not indices_faltantes:
        return [resultados_dict[str(i)] for i in range(total_bloques)], errores_totales
        
    pausa_adaptativa = PAUSA_BASE_ENTRE_LOTES
    lotes = [indices_faltantes[i:i + TAMANO_LOTE] for i in range(0, len(indices_faltantes), TAMANO_LOTE)]
    
    bloques_procesados_hoy = 0
    reintentos_sesion = 0
    
    for idx_lote, lote_indices in enumerate(lotes):
        sub_resultados = {}
        reintentos_lote = 0
        
        with ThreadPoolExecutor(max_workers=WORKERS_PARALELOS) as executor:
            # Enviar solo los bloques correspondientes a los índices del lote
            futuros = {executor.submit(traducir_bloque, bloques[idx], idioma_destino): idx for idx in lote_indices}
            
            for futuro in as_completed(futuros):
                idx_interno = futuros[futuro]
                tipo, trad, pag, err, reint, lat = futuro.result()
                
                sub_resultados[str(idx_interno)] = (tipo, trad, pag)
                errores_totales += err
                reintentos_lote += reint
                reintentos_sesion += reint
                bloques_procesados_hoy += 1
        
        # Consolidar progreso
        resultados_dict.update(sub_resultados)
        guardar_checkpoint(file_hash, resultados_dict, errores_totales, max(lote_indices))
        
        # Rate Limiting Adaptativo
        if reintentos_lote > 0:
            pausa_adaptativa = min(pausa_adaptativa + 2.0, 10.0) # Penalización si hay fallos
        else:
            pausa_adaptativa = max(PAUSA_BASE_ENTRE_LOTES, pausa_adaptativa - 0.5) # Recuperación
        
        # Actualizar Panel UI (Observabilidad)
        progreso_global = len(resultados_dict) / total_bloques
        with panel_metricas.container():
            col1, col2, col3 = st.columns(3)
            col1.metric("Progreso Global", f"{len(resultados_dict)} / {total_bloques}")
            col2.metric("Reintentos de Red", f"{reintentos_sesion}")
            col3.metric("Pausa Adaptativa (Anti-Ban)", f"{pausa_adaptativa:.1f} s")
            st.progress(progreso_global)
            
        if idx_lote < len(lotes) - 1:
            time.sleep(pausa_adaptativa)

    # Reconstruir la lista final ordenada
    lista_final_ordenada = [resultados_dict[str(i)] for i in range(total_bloques)]
    return lista_final_ordenada, errores_totales

# ============================================================
# GENERACIÓN DE PDF Y LOGS
# ============================================================
def add_page_number(canvas, doc):
    page_num = canvas.getPageNumber()
    canvas.saveState()
    canvas.setFont('DejaVuSans', 9)
    canvas.drawCentredString(105*mm, 15*mm, str(page_num))
    canvas.restoreState()

def generar_pdf_estructurado(resultado_traduccion):
    if not os.path.exists(FONT_PATH):
        st.error(f"Falta archivo tipográfico en: {FONT_PATH}")
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

def generar_log(resultado_traduccion, errores, tiempo_total):
    lineas = [
        "="*60, "LOG AUTOMÁTICO DE PROCESAMIENTO INDUSTRIAL",
        f"Fecha ejecución: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Tiempo total invertido en sesión: {tiempo_total:.2f} segundos",
        f"Bloques con error definitivo (mantenidos en original): {errores}",
        "-"*60
    ]
    total_texto = sum(1 for t, _, _ in resultado_traduccion if t == 'texto')
    lineas.append(f"Párrafos procesados: {total_texto}")
    lineas.append(f"Títulos formateados: {len(resultado_traduccion) - total_texto}")
    lineas.append("="*60)
    return "\n".join(lineas)

# ============================================================
# INTERFAZ STREAMLIT
# ============================================================
st.set_page_config(page_title="Traductor Académico Automático", layout="centered")
st.title("📚 Traductor de PDFs Académicos Profesional")
st.markdown("Motor industrial tolerante a fallos. Soporta desconexiones gracias a su sistema de **Checkpoints atómicos en disco**.")

# Inicialización segura
if "pdf_final" not in st.session_state: st.session_state.pdf_final = None
if "log_texto" not in st.session_state: st.session_state.log_texto = None
if "traduccion_lista" not in st.session_state: st.session_state.traduccion_lista = False

archivo_subido = st.file_uploader("Sube el documento PDF completo aquí", type="pdf")

if archivo_subido is not None:
    pdf_bytes = archivo_subido.read()
    file_hash = generar_hash_archivo(pdf_bytes) # ID Único para el checkpoint
    
    if "bloques_detectados" not in st.session_state or st.session_state.get("archivo_actual") != archivo_subido.name:
        with st.spinner("Analizando topología y buscando Checkpoints previos..."):
            st.session_state.bloques_detectados = detectar_estructura_pymupdf(pdf_bytes)
            st.session_state.archivo_actual = archivo_subido.name
            st.session_state.traduccion_lista = False
            
            # Verificar si existe checkpoint previo para informar al usuario
            chk = cargar_checkpoint(file_hash)
            st.session_state.progreso_previo = len(chk["resultados"])
            
    total_bloques = len(st.session_state.bloques_detectados)
    
    if st.session_state.progreso_previo > 0 and st.session_state.progreso_previo < total_bloques:
        st.warning(f"⚠️ Se detectó una sesión previa interrumpida. El sistema se reanudará automáticamente desde el bloque {st.session_state.progreso_previo}.")
    else:
        st.success(f"✅ Análisis completado: {total_bloques} bloques estructurados.")

    # Panel vacío reservado para la observabilidad
    panel_metricas = st.empty()

    if st.button("🚀 Iniciar Procesamiento Industrial", type="primary"):
        t_inicio = time.time()
        
        # Ejecutar el Pipeline Orquestador
        resultados, errores = procesar_pipeline_industrial(
            st.session_state.bloques_detectados, 
            file_hash, 
            panel_metricas
        )
        
        t_total = time.time() - t_inicio
        st.info(f"📊 Pipeline finalizado con éxito en {t_total:.1f} segundos de sesión activa.")
        
        with st.spinner("Compilando archivo PDF final bajo estándar académico..."):
            try:
                st.session_state.pdf_final = generar_pdf_estructurado(resultados)
                st.session_state.log_texto = generar_log(resultados, errores, t_total)
                st.session_state.traduccion_lista = True
                
                # Borrar el archivo de checkpoint tras completar al 100% (opcional)
                filepath = os.path.join(CHECKPOINT_DIR, f"{file_hash}.json")
                if os.path.exists(filepath):
                    os.remove(filepath)
                    
                st.rerun() 
            except Exception as e:
                st.error(f"❌ Error crítico en compilación: {e}")

# Renderizado de descargas
if st.session_state.traduccion_lista:
    st.markdown("---")
    st.success("🎉 ¡El documento completo ha sido ensamblado!")
    col1, col2 = st.columns(2)
    with col1:
        st.download_button(
            label="📥 Descargar PDF Académico", data=st.session_state.pdf_final,
            file_name="traduccion_industrial.pdf", mime="application/pdf",
            use_container_width=True, type="primary"
        )
    with col2:
        st.download_button(
            label="📋 Descargar Reporte de Calidad", data=st.session_state.log_texto,
            file_name="reporte_pipeline.txt", mime="text/plain",
            use_container_width=True
        )
