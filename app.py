import io
import os
import streamlit as st

from pypdf import PdfReader
from deep_translator import GoogleTranslator

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    PageBreak,
)
from xml.sax.saxutils import escape


# ============================================================
# CONFIGURACIÓN
# ============================================================

MAX_CHARS = 4500

FONT_PATH = os.path.join(
    os.path.dirname(__file__),
    "fonts",
    "DejaVuSans.ttf"
)


# ============================================================
# FUENTE UNICODE
# ============================================================

def registrar_fuente():
    """
    Registra una fuente TTF con soporte Unicode.
    """

    if not os.path.exists(FONT_PATH):
        raise FileNotFoundError(
            f"No se encontró la fuente Unicode:\n{FONT_PATH}"
        )

    if "DejaVuSans" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(
            TTFont("DejaVuSans", FONT_PATH)
        )


# ============================================================
# EXTRACCIÓN DEL PDF
# ============================================================

def extraer_texto_pdf(archivo):
    """
    Extrae el texto de todas las páginas del PDF.
    """

    lector = PdfReader(archivo)

    paginas = []

    for numero, pagina in enumerate(lector.pages, start=1):

        try:
            texto = pagina.extract_text()

            if texto:
                paginas.append(texto.strip())

        except Exception as e:
            st.warning(
                f"No se pudo extraer correctamente "
                f"la página {numero}: {e}"
            )

    return "\n\n".join(paginas)


# ============================================================
# DIVISIÓN INTELIGENTE DEL TEXTO
# ============================================================

def dividir_texto(texto, max_len=MAX_CHARS):
    """
    Divide un texto largo intentando respetar:

    1. párrafos
    2. saltos de línea
    3. espacios
    4. corte duro como último recurso
    """

    if not texto:
        return []

    fragmentos = []

    restante = texto.strip()

    while len(restante) > max_len:

        # ----------------------------------------------------
        # 1. Intentar cortar en un párrafo
        # ----------------------------------------------------

        corte = restante.rfind("\n\n", 0, max_len)

        if corte > max_len * 0.5:
            corte += 2

        else:

            # ------------------------------------------------
            # 2. Intentar cortar en salto de línea
            # ------------------------------------------------

            corte = restante.rfind("\n", 0, max_len)

            if corte > max_len * 0.5:
                corte += 1

            else:

                # --------------------------------------------
                # 3. Intentar cortar en espacio
                # --------------------------------------------

                corte = restante.rfind(" ", 0, max_len)

                if corte > max_len * 0.5:
                    corte += 1

                else:

                    # ----------------------------------------
                    # 4. Último recurso
                    # ----------------------------------------

                    corte = max_len

        fragmentos.append(restante[:corte])

        restante = restante[corte:].lstrip()

    if restante:
        fragmentos.append(restante)

    return fragmentos


# ============================================================
# TRADUCCIÓN
# ============================================================

def traducir_texto_con_progreso(
    texto,
    idioma_origen="en",
    idioma_destino="es",
    max_len=MAX_CHARS
):
    """
    Traduce un texto largo mediante fragmentos.

    Conserva los saltos de línea y muestra progreso.
    """

    if not texto or not texto.strip():
        return ""

    fragmentos = dividir_texto(
        texto,
        max_len=max_len
    )

    if not fragmentos:
        return ""

    barra = st.progress(
        0,
        text="Iniciando traducción..."
    )

    status = st.empty()

    traductor = GoogleTranslator(
        source=idioma_origen,
        target=idioma_destino
    )

    traducciones = []

    total = len(fragmentos)

    errores = 0

    for i, fragmento in enumerate(fragmentos, start=1):

        porcentaje = i / total

        barra.progress(
            porcentaje,
            text=(
                f"Traduciendo fragmento "
                f"{i} de {total}..."
            )
        )

        status.text(
            f"Procesando fragmento {i}/{total} "
            f"({porcentaje * 100:.0f}%)"
        )

        try:

            traduccion = traductor.translate(
                fragmento
            )

            if not traduccion:
                raise ValueError(
                    "La API devolvió una traducción vacía."
                )

            traducciones.append(traduccion)

        except Exception as e:

            errores += 1

            st.warning(
                f"⚠️ Error en el fragmento "
                f"{i}/{total}: {e}"
            )

            # Conservamos el texto original
            # para no perder información.
            traducciones.append(fragmento)

    barra.empty()
    status.empty()

    if errores:
        st.warning(
            f"La traducción terminó con "
            f"{errores} fragmento(s) sin traducir."
        )

    # IMPORTANTE:
    # usar saltos de línea en lugar de " "
    # para no destruir la estructura.
    return "\n".join(traducciones)


# ============================================================
# GENERACIÓN DEL PDF
# ============================================================

def crear_pdf(texto_traducido):
    """
    Genera el PDF completamente en memoria.

    Devuelve bytes listos para Streamlit.
    """

    if not texto_traducido or not texto_traducido.strip():
        raise ValueError(
            "No hay texto para generar el PDF."
        )

    registrar_fuente()

    buffer = io.BytesIO()

    documento = SimpleDocTemplate(
        buffer,
        pagesize=A4,

        rightMargin=20 * mm,
        leftMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,

        title="Documento traducido",
        author="Traductor de PDFs",
    )

    estilos = getSampleStyleSheet()

    estilo_parrafo = ParagraphStyle(
        "TextoTraducido",

        parent=estilos["Normal"],

        fontName="DejaVuSans",
        fontSize=10.5,
        leading=15,

        alignment=TA_LEFT,

        spaceAfter=8,

        wordWrap="LTR",
    )

    historia = []

    # --------------------------------------------------------
    # Procesar párrafos
    # --------------------------------------------------------

    parrafos = texto_traducido.split("\n")

    for linea in parrafos:

        linea = linea.strip()

        if not linea:
            historia.append(
                Spacer(1, 5)
            )
            continue

        # Escapar caracteres especiales para ReportLab
        linea = escape(linea)

        historia.append(
            Paragraph(
                linea,
                estilo_parrafo
            )
        )

    if not historia:
        raise ValueError(
            "No se pudo generar contenido para el PDF."
        )

    # --------------------------------------------------------
    # Construcción
    # --------------------------------------------------------

    documento.build(historia)

    # Obtener bytes
    buffer.seek(0)

    pdf_bytes = buffer.getvalue()

    buffer.close()

    return pdf_bytes


# ============================================================
# INTERFAZ STREAMLIT
# ============================================================

st.set_page_config(
    page_title="Traductor de PDFs",
    page_icon="📄",
    layout="centered"
)


st.title(
    "📄 Traductor de PDF "
    "(Inglés → Español)"
)

st.markdown(
    "Sube un PDF en inglés y obtén "
    "una versión traducida al español."
)


# ============================================================
# UPLOAD
# ============================================================

archivo_subido = st.file_uploader(
    "Elige tu archivo PDF",
    type=["pdf"]
)


if archivo_subido is not None:

    # --------------------------------------------------------
    # Extracción
    # --------------------------------------------------------

    try:

        with st.spinner(
            "Extrayendo texto del PDF..."
        ):

            texto_original = extraer_texto_pdf(
                archivo_subido
            )

    except Exception as e:

        st.error(
            "❌ No se pudo leer el PDF."
        )

        st.exception(e)

        st.stop()


    # --------------------------------------------------------
    # Verificar texto
    # --------------------------------------------------------

    if not texto_original.strip():

        st.warning(
            "⚠️ No se pudo extraer texto del PDF."
        )

        st.info(
            "Es posible que el documento sea "
            "un PDF escaneado compuesto por imágenes."
        )

        st.stop()


    # --------------------------------------------------------
    # Mostrar original
    # --------------------------------------------------------

    st.subheader(
        "📖 Texto original (Inglés)"
    )

    st.text_area(
        "Contenido original",
        texto_original,
        height=250,
        key="original"
    )


    # --------------------------------------------------------
    # Traducción
    # --------------------------------------------------------

    if st.button(
        "🌐 Traducir a Español",
        type="primary"
    ):

        try:

            with st.spinner(
                "Preparando traducción..."
            ):

                texto_traducido = (
                    traducir_texto_con_progreso(
                        texto_original
                    )
                )

            if not texto_traducido.strip():

                st.error(
                    "❌ La traducción no produjo "
                    "ningún resultado."
                )

                st.stop()


            # ------------------------------------------------
            # Guardar en session_state
            # ------------------------------------------------

            st.session_state[
                "texto_traducido"
            ] = texto_traducido

            st.success(
                "✅ Traducción completada."
            )

        except Exception as e:

            st.error(
                "❌ Ocurrió un error durante "
                "la traducción."
            )

            st.exception(e)

            st.stop()


    # ========================================================
    # RESULTADO
    # ========================================================

    if "texto_traducido" in st.session_state:

        texto_traducido = (
            st.session_state["texto_traducido"]
        )

        st.subheader(
            "🇪🇸 Texto traducido (Español)"
        )

        st.text_area(
            "Contenido traducido",
            texto_traducido,
            height=300,
            key="traducido"
        )


        # ----------------------------------------------------
        # Generar PDF
        # ----------------------------------------------------

        try:

            with st.spinner(
                "Generando PDF..."
            ):

                pdf_bytes = crear_pdf(
                    texto_traducido
                )

            st.success(
                "📄 PDF generado correctamente."
            )

            st.download_button(
                label="📥 Descargar PDF traducido",

                data=pdf_bytes,

                file_name=(
                    "documento_traducido.pdf"
                ),

                mime="application/pdf",

                type="primary"
            )

        except Exception as e:

            st.error(
                "❌ No se pudo generar el PDF."
            )

            st.exception(e)
