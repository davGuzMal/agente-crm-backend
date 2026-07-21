"""
app/services/llm.py
───────────────────
Módulo de llamada a Claude con streaming SSE estructurado.

La novedad respecto al esqueleto anterior: el StreamParser actúa como
máquina de estados sobre los tokens de Claude. Detecta las etiquetas
de sección ([VEREDICTO], [RANKING]…) aunque lleguen fragmentadas en
múltiples tokens, y emite eventos SSE estructurados.

El frontend recibe eventos tipados y nunca necesita escanear strings:

  {"type": "metadata",      ...}           ← ranking pre-calculado (de evaluate.py)
  {"type": "section_start", "section": X}  ← inicio de sección detectado
  {"type": "token",  "section": X, "content": "..."}  ← fragmento de texto
  {"type": "done",   "sections_completed": [...]}      ← stream finalizado
  {"type": "error",  "code": "E02", "message": "..."}  ← error recuperable
"""

import json
import logging
from typing import AsyncGenerator, List, Optional

import anthropic

from app.config import settings
from app.models.intake import IntakeProfile
from app.services.scoring import ScoringOutput
from app.services.filter import ExclusionResult

logger = logging.getLogger(__name__)

_client = anthropic.AsyncAnthropic(
    api_key=settings.anthropic_api_key,
    timeout=45.0,
)

MODEL      = "claude-sonnet-4-6"
MAX_TOKENS = 3500
TEMPERATURE = 0.3


# ══════════════════════════════════════════════════════════════════════════════
# SECCIONES CONOCIDAS Y TAGS
# ══════════════════════════════════════════════════════════════════════════════

KNOWN_SECTIONS = [
    "VEREDICTO",
    "RANKING",
    "ANALISIS_GANADOR",
    "ALTERNATIVA",
    "ALERTAS",
    "CONFIANZA",
]

# Tags completos que Claude emitirá: [VEREDICTO], [RANKING], etc.
SECTION_TAGS = {f"[{s}]" for s in KNOWN_SECTIONS}

# Longitud máxima de cualquier tag (para decidir cuándo dejar de esperar)
MAX_TAG_LEN = max(len(t) for t in SECTION_TAGS)   # 18 → "[ANALISIS_GANADOR]"


# ══════════════════════════════════════════════════════════════════════════════
# MÁQUINA DE ESTADOS: StreamParser
# ══════════════════════════════════════════════════════════════════════════════

class StreamParser:
    """
    Parsea el stream de tokens de Claude y emite eventos estructurados.

    Problema: una etiqueta como [VEREDICTO] puede llegar fragmentada:
      token 1 → "[VERED"
      token 2 → "ICTO]\n"

    Solución: buffer acumulativo + búsqueda de tags completos en cada feed().
    Mientras el buffer podría ser el inicio de un tag conocido, lo retiene.
    En cuanto confirma que no lo es, emite todo como contenido.

    Uso:
        parser = StreamParser()
        for raw_token in claude_stream:
            for event in parser.feed(raw_token):
                yield sse(event)
        for event in parser.flush():
            yield sse(event)
    """

    def __init__(self):
        self.buffer: str = ""
        self.current_section: Optional[str] = None
        self.sections_completed: List[str] = []

    # ── API pública ───────────────────────────────────────────────────────────

    def feed(self, token: str) -> List[dict]:
        """
        Ingesta un token y devuelve los eventos SSE que deben emitirse ahora.
        Puede devolver cero, uno o varios eventos por token.
        """
        self.buffer += token
        return self._drain()

    def flush(self) -> List[dict]:
        """
        Vacía el buffer al final del stream.
        Emite cualquier contenido retenido como contenido de la sección activa.
        """
        events = []
        if self.buffer.strip():
            events += self._content_events(self.buffer)
        # Cerrar la última sección abierta si no está ya en completed
        if self.current_section and self.current_section not in self.sections_completed:
            self.sections_completed.append(self.current_section)
        self.buffer = ""
        return events

    # ── Lógica interna ────────────────────────────────────────────────────────

    def _drain(self) -> List[dict]:
        """
        Procesa el buffer en bucle hasta que no haya más tags completos.
        Cada iteración:
          1. Busca el tag completo más temprano en el buffer.
          2. Si lo encuentra → emite contenido previo + section_start, continúa.
          3. Si no → decide si retener el final del buffer (posible tag parcial)
             o emitir todo y vaciar.
        """
        events: List[dict] = []

        while True:
            # Buscar el tag completo más cercano al inicio del buffer
            earliest_tag: Optional[str] = None
            earliest_pos: Optional[int] = None

            for tag in SECTION_TAGS:
                pos = self.buffer.find(tag)
                if pos != -1 and (earliest_pos is None or pos < earliest_pos):
                    earliest_tag = tag
                    earliest_pos = pos

            if earliest_tag is not None:
                # — Emitir contenido que precede al tag —
                before = self.buffer[:earliest_pos]
                if before:
                    events += self._content_events(before)

                # — Cerrar sección actual —
                if self.current_section and self.current_section not in self.sections_completed:
                    self.sections_completed.append(self.current_section)

                # — Abrir nueva sección —
                section_name = earliest_tag[1:-1]   # quitar [ y ]
                events.append({"type": "section_start", "section": section_name})
                self.current_section = section_name

                # — Avanzar buffer más allá del tag —
                rest = self.buffer[earliest_pos + len(earliest_tag):]
                self.buffer = rest.lstrip("\n")   # newline inmediato tras el tag es decorativo

                # — Continuar el bucle: puede haber más tags —

            else:
                # No hay tag completo en el buffer.
                # ¿El final del buffer podría ser el inicio de un tag?
                bracket = self.buffer.rfind("[")

                if bracket != -1:
                    partial = self.buffer[bracket:]
                    is_potential = (
                        any(tag.startswith(partial) for tag in SECTION_TAGS)
                        and len(partial) < MAX_TAG_LEN
                    )
                    if is_potential:
                        # Emitir lo que hay antes del [ y retener el resto
                        safe = self.buffer[:bracket]
                        if safe:
                            events += self._content_events(safe)
                        self.buffer = partial
                    else:
                        # El [ no va a formar un tag — emitir todo
                        if self.buffer:
                            events += self._content_events(self.buffer)
                        self.buffer = ""
                else:
                    # Sin [ en el buffer — emitir todo sin reservas
                    if self.buffer:
                        events += self._content_events(self.buffer)
                    self.buffer = ""

                break   # No quedan tags que procesar en este ciclo

        return events

    def _content_events(self, text: str) -> List[dict]:
        """Envuelve un fragmento de texto en el evento token de la sección actual."""
        if not text:
            return []
        return [{"type": "token", "section": self.current_section, "content": text}]


# ══════════════════════════════════════════════════════════════════════════════
# SYSTEM PROMPT
# ══════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """Eres un analista independiente de software CRM para pymes europeas.
No tienes ninguna relación comercial con ningún proveedor de CRM.

Recibirás un mensaje estructurado con:
- PERFIL DE EMPRESA: datos de la empresa que evalúa un CRM
- PESOS APLICADOS: los pesos del scoring ajustados dinámicamente al perfil
- RANKING CALCULADO: el ranking matemático de CRMs (NO modificar el orden ni los scores)
- CRMS EXCLUIDOS: opciones descartadas antes del scoring y por qué
- ALERTAS DEL SISTEMA: flags detectados durante el análisis
- CONTEXTO DE REVIEWS: fragmentos de opiniones verificadas de empresas similares

Tu destinatario es el responsable de la decisión de compra: director de operaciones,
gerente general o CEO de una PYME de 5–250 empleados. Es inteligente pero no técnico.
Valora la honestidad directa sobre el optimismo comercial.

FORMATO DE RESPUESTA OBLIGATORIO — exactamente estas 6 secciones con sus etiquetas:

[VEREDICTO]
2–3 frases. Por qué el CRM #1 gana para este perfil concreto.
Menciona su nombre en la primera frase. Sin rodeos.

[RANKING]
Para cada CRM en el ranking evaluado: posición, nombre, score/100, TCO 3 años,
y en una frase cuándo es la opción correcta.
Para cada CRM excluido: nombre y motivo de exclusión en una frase.

[ANALISIS_GANADOR]
3–4 párrafos sobre el CRM recomendado:
- Por qué encaja con el perfil concreto de esta empresa (no genérico)
- Fortalezas específicas relevantes para su situación
- Limitaciones reales que deben tener en cuenta
- Próximos pasos concretos para avanzar

[ALTERNATIVA]
2 párrafos sobre el CRM en segunda posición:
- En qué escenarios concretos sería mejor que el #1
- Qué tendría que cambiar en el perfil de la empresa para que el #2 sea la opción correcta

[ALERTAS]
Cada alerta del sistema explicada en lenguaje llano para un no técnico.
Solo incluye las relevantes y accionables. Para cada alerta: título + explicación en 2–3 frases.

[CONFIANZA]
Nivel del análisis (Alta / Media / Baja) y justificación en 2–3 frases.
Si hay datos desactualizados o pocas reviews en el sector, mencionarlo aquí.

RESTRICCIONES ABSOLUTAS:
- No cambies el orden del ranking ni los scores numéricos bajo ningún concepto.
- No menciones precios sin incluir la fecha de verificación que recibirás en el payload.
- Prohibido usar lenguaje de marketing: "líder del mercado", "solución robusta", "potente", etc.
- No recomiendes empresas de consultoría o partners específicos.
- No uses formato Markdown (tablas, negritas con **, listas con guiones). Escribe en prosa corrida, frase por frase, tal como se describe en cada sección.
- Si scoring_confidence es "low", recomienda verificación adicional antes de decidir.
- Usa español neutro europeo. Tutea al lector. Sé directo y concreto."""


# ══════════════════════════════════════════════════════════════════════════════
# BUILD USER MESSAGE
# ══════════════════════════════════════════════════════════════════════════════

def build_user_message(
    scoring_output: ScoringOutput,
    profile: IntakeProfile,
    semantic_context: List[dict],
    excluded_crms: List[ExclusionResult] = [],
) -> str:
    lines: List[str] = []

    lines += [
        "## PERFIL DE EMPRESA",
        f"Sector: {profile.sector}  |  Modelo: {profile.modelo}",
        f"Presupuesto: {profile.presupuesto}  ({profile.presupuesto_flex})",
        f"Equipo: {profile.empleados}  |  Usuarios CRM: {profile.usuarios_crm}",
        f"Suite principal: {profile.suite}",
        f"Sistema actual: {profile.sistema_actual}",
        f"Perfil técnico: {profile.equipo_tech}",
        f"Clientes activos: {profile.clientes}  |  Crecimiento: {profile.crecimiento}",
    ]
    if profile.tools:
        lines.append(f"Herramientas en uso: {', '.join(profile.tools)}")
    lines.append("")

    lines.append("## PESOS APLICADOS AL SCORING")
    for var, w in scoring_output.applied_weights.items():
        lines.append(f"  {var}: {w * 100:.1f}%")
    if scoring_output.weight_adjustments:
        lines.append("Ajustes dinámicos realizados:")
        for adj in scoring_output.weight_adjustments:
            sign = "+" if adj.delta > 0 else ""
            lines.append(f"  {adj.variable}: {sign}{adj.delta * 100:.0f}%  — {adj.reason}")
    lines.append("")

    lines += [
        "## RANKING CALCULADO — NO MODIFICAR ORDEN NI SCORES",
        f"Confianza del scoring: {scoring_output.scoring_confidence.upper()}",
        "",
    ]
    for crm in scoring_output.ranked_crms:
        lines.append(f"#{crm.rank}  {crm.crm_name}  ({crm.crm_category})")
        lines.append(f"  Score final:  {crm.final_score} / 100")
        lines.append(f"  TCO 3 años:   {crm.tco_3y_eur:,.0f} €")
        lines.append("  Desglose:")
        for var, detail in crm.score_breakdown.items():
            lines.append(
                f"    {var:<22} {detail.raw_score:4.1f}/10  "
                f"(peso {detail.weight * 100:.1f}%)"
            )
        lines.append("")

    if excluded_crms:
        lines.append("## CRMS EXCLUIDOS DEL RANKING")
        for exc in excluded_crms:
            lines.append(f"  [{exc.filter_code}] {exc.crm_name}: {exc.reason}")
        lines.append("")

    if scoring_output.all_flags:
        lines.append("## ALERTAS DEL SISTEMA")
        for flag in scoring_output.all_flags:
            lines.append(f"  [{flag.severity.upper()}] {flag.crm_name} / {flag.code}: {flag.message}")
        lines.append("")

    if semantic_context:
        lines.append("## CONTEXTO DE REVIEWS VERIFICADAS")
        for chunk in semantic_context[:10]:
            lines.append(f"[{chunk.get('crm_id', '')} | {chunk.get('chunk_type', '')}]")
            lines.append(str(chunk.get("content", ""))[:400])
            lines.append("")
    else:
        lines += ["## CONTEXTO DE REVIEWS", "No disponible para esta evaluación.", ""]

    lines += [
        "## INSTRUCCIÓN",
        "Genera el veredicto completo siguiendo exactamente el formato de las 6 secciones.",
        "Fundamenta cada sección en los datos del ranking, las alertas y el contexto de reviews.",
    ]
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# STREAM DEL VEREDICTO — con StreamParser
# ══════════════════════════════════════════════════════════════════════════════

async def stream_verdict(
    scoring_output: ScoringOutput,
    profile: IntakeProfile,
    semantic_context: List[dict],
    excluded_crms: List[ExclusionResult] = [],
) -> AsyncGenerator[str, None]:
    """
    Llama a Claude con streaming y emite eventos SSE estructurados.

    Usa StreamParser para detectar las etiquetas de sección en el stream
    de tokens y emitir eventos tipados. El frontend no necesita escanear texto.

    Eventos emitidos (en orden):
      section_start  → cuando se detecta una nueva sección
      token          → fragmento de texto de la sección activa
      done           → stream finalizado, incluye lista de secciones completadas
      error          → error recuperable con código y mensaje
    """
    user_message = build_user_message(
        scoring_output, profile, semantic_context, excluded_crms
    )
    parser = StreamParser()

    def sse(event: dict) -> str:
        return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    logger.info(
        f"Iniciando stream Claude — CRMs: {len(scoring_output.ranked_crms)} "
        f"| Confianza: {scoring_output.scoring_confidence}"
    )

    try:
        stop_reason = "end_turn"  # fallback seguro si algo falla antes de leerlo

        async with _client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        ) as stream:
            async for text in stream.text_stream:
                for event in parser.feed(text):
                    yield sse(event)

            # flush() debe ir DENTRO del bloque: el buffer puede tener
            # contenido retenido esperando un tag que nunca terminó de llegar.
            for event in parser.flush():
                yield sse(event)

            # get_final_message() solo es accesible mientras el contexto
            # async with está abierto — fuera del bloque ya no existe.
            stop_reason = (await stream.get_final_message()).stop_reason

        # ── Evento terminal según stop_reason ─────────────────────────────────
        if stop_reason == "max_tokens":
            logger.warning(
                f"Stream truncado por límite de tokens (MAX_TOKENS={MAX_TOKENS}). "
                f"Secciones completadas: {parser.sections_completed}"
            )
            yield sse({
                "type":               "error",
                "code":               "E_TRUNCATED",
                "message":            (
                    "El análisis fue cortado porque Claude alcanzó el límite de longitud. "
                    "Las secciones ya recibidas son válidas e íntegras. "
                    "Si esto ocurre con frecuencia, ajusta MAX_TOKENS en llm.py."
                ),
                "sections_completed": parser.sections_completed,
            })
        else:
            logger.info(f"Stream completado — secciones: {parser.sections_completed}")
            yield sse({"type": "done", "sections_completed": parser.sections_completed})

    except anthropic.APITimeoutError:
        logger.error("Timeout en llamada a Claude (>45s)")
        yield sse({"type": "error", "code": "E02",
                   "message": "El análisis tardó demasiado. El contenido parcial sigue siendo válido. Reintenta la generación."})

    except anthropic.RateLimitError:
        logger.error("Rate limit de la API de Anthropic")
        yield sse({"type": "error", "code": "E03",
                   "message": "Límite de uso de la API alcanzado. Reintenta en unos segundos."})

    except anthropic.APIError as exc:
        logger.error(f"Error de la API de Anthropic: {exc}")
        yield sse({"type": "error", "code": "E04", "message": f"Error de la API: {exc}"})

    except Exception as exc:
        logger.exception(f"Error inesperado en stream_verdict: {exc}")
        yield sse({"type": "error", "code": "E99",
                   "message": "Error interno. Por favor, inténtalo de nuevo."})