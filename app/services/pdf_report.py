"""
Genera un PDF simple (texto/tablas, sin diseño elaborado) a partir de
verdict_sections + scoring_metadata ya persistidos en evaluation_sessions.

V1 explícitamente básico — decisión de David, 11/08/2026.
- Sin logo, sin cabecera visual, sin gráficos: solo títulos y multi_cell.
- Sin dependencia de sistema: fpdf2 es puro Python (a diferencia de
  WeasyPrint, que necesitaría librerías extra que Render puede no tener).

Por qué fpdf2 y no reportlab/jinja2+weasyprint:
- reportlab es heavyweight (muchos más kwargs y conceptos) y la v1 no
  los aprovecha.
- WeasyPrint pide Cairo/Pango a nivel de sistema — incompatible con el
  principio "sin coste nuevo de infraestructura" del R11.

Limitación de fpdf2 con la fuente built-in Helvetica:
- Solo soporta Latin-1 (ISO-8859-1). Los caracteres Unicode fuera de
  ese rango (em-dash U+2014, en-dash U+2013, comillas tipográficas,
  ellipsis U+2026) explotan con FPDFUnicodeEncodingException. Claude
  emite esos caracteres con frecuencia en sus veredictos.
- Truco de la v1: _latin_safe() los reemplaza por ASCII antes de
  pasarlos al PDF. Es una pérdida cosmética menor que evita meter un
  TTF (que añadiría ~1MB y la gestión de font_path en Render).

Si en una iteración futura hace falta algo visual o soporte Unicode
completo, la salida de este módulo (bytes PDF) es la misma que
consumiría cualquier mejora — solo se cambia la implementación interna.
"""
from fpdf import FPDF


# Mapeo de caracteres Unicode frecuentes → ASCII equivalente.
# Mantenerlo explícito y pequeño — la v1 no necesita cobertura Unicode.
_UNICODE_TO_ASCII = {
    "—": "-",   # em-dash —
    "–": "-",   # en-dash –
    "‘": "'",   # left single quote '
    "’": "'",   # right single quote '
    "“": '"',   # left double quote "
    "”": '"',   # right double quote "
    "…": "...", # horizontal ellipsis …
    " ": " ",   # non-breaking space
}


def _latin_safe(text: str) -> str:
    """
    Convierte texto Unicode a un subconjunto ASCII/Latin-1 seguro para
    la fuente built-in Helvetica de fpdf2.

    1) Reemplaza los caracteres problemáticos más comunes (em-dash,
       comillas tipográficas, ellipsis) por equivalentes ASCII.
    2) Si tras ese paso queda algún carácter fuera de Latin-1, lo
       sustituye por '?' — degradación silenciosa, no rompe el PDF.
    """
    for src, dst in _UNICODE_TO_ASCII.items():
        text = text.replace(src, dst)
    return text.encode("latin-1", errors="replace").decode("latin-1")


def build_report_pdf(scoring_metadata: dict, verdict_sections: dict) -> bytes:
    """
    Renderiza el informe a partir del snapshot persistido en
    evaluation_sessions (scoring_metadata + verdict_sections).

    Args:
        scoring_metadata: Dict con campos winner, runner_up, confidence,
            ranking[], excluded[], weight_adjustments[], applied_weights{}.
            (Estructura que produce evaluate.py:308)
        verdict_sections: Dict {section_name: text} con las 6 secciones
            del veredicto de Claude. Algunas pueden faltar (ej. cuando el
            stream se truncó) — el PDF omite silenciosamente las vacías.

    Returns:
        bytes del PDF, listo para attach en el email o servir como
        application/pdf desde FastAPI.
    """
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, _latin_safe("Trust - Informe de evaluación de CRM"), ln=True)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(
        0, 8,
        _latin_safe(f"Recomendación: {scoring_metadata.get('winner', '-')}"),
        ln=True,
    )
    pdf.ln(4)

    section_titles = {
        "VEREDICTO": "Veredicto",
        "RANKING": "Ranking",
        "ANALISIS_GANADOR": "Análisis del CRM recomendado",
        "ALTERNATIVA": "Alternativa recomendada",
        "ALERTAS": "Alertas del análisis",
        "CONFIANZA": "Nivel de confianza",
    }
    for key, title in section_titles.items():
        text = verdict_sections.get(key)
        if not text:
            continue
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 8, _latin_safe(title), ln=True)
        pdf.set_font("Helvetica", "", 10)
        pdf.multi_cell(0, 6, _latin_safe(text))
        pdf.ln(3)

    return bytes(pdf.output())