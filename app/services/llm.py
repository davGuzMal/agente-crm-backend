"""
app/services/llm.py
───────────────────
Módulo de llamada a Claude con streaming SSE para la generación del veredicto.

El LLM NO calcula rankings — recibe el ScoringOutput ya calculado por el
backend y lo interpreta en lenguaje natural en 6 secciones estructuradas.

Flujo:
  1. build_user_message()  → serializa ScoringOutput + perfil a texto estructurado
  2. stream_verdict()       → llama a Claude con stream=True y emite SSE tokens
  3. El router convierte el generator en StreamingResponse (text/event-stream)

Formato SSE emitido:
  data: {"token": "texto parcial"}\n\n   ← tokens de Claude
  data: {"type": "done"}\n\n             ← fin del stream
  data: {"type": "error", ...}\n\n       ← error recuperable
"""

import json
import logging
from typing import AsyncGenerator, List

import anthropic

from app.config import settings
from app.models.intake import IntakeProfile
from app.services.scoring import ScoringOutput
from app.services.filter import ExclusionResult

logger = logging.getLogger(__name__)

# ── Cliente Anthropic (singleton por módulo) ──────────────────────────────────
_client = anthropic.AsyncAnthropic(
    api_key=settings.anthropic_api_key,
    timeout=45.0,
)

# ── Parámetros del modelo ─────────────────────────────────────────────────────
MODEL         = "claude-sonnet-4-6"
MAX_TOKENS    = 1800
TEMPERATURE   = 0.3


# ══════════════════════════════════════════════════════════════════════════════
# SYSTEM PROMPT — definido por rol, no por estructura de datos
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
- Si scoring_confidence es "low", recomienda verificación adicional antes de decidir.
- Usa español neutro europeo. Tutea al lector. Sé directo y concreto."""


# ══════════════════════════════════════════════════════════════════════════════
# CONSTRUCCIÓN DEL MENSAJE DE USUARIO
# ══════════════════════════════════════════════════════════════════════════════

def build_user_message(
    scoring_output: ScoringOutput,
    profile: IntakeProfile,
    semantic_context: List[dict],
    excluded_crms: List[ExclusionResult] = [],
) -> str:
    """
    Serializa el ScoringOutput y el perfil en el mensaje estructurado
    que recibirá Claude. El formato es texto plano legible, no JSON,
    para que el LLM lo procese con menos fricción.
    """
    lines: List[str] = []

    # ── Perfil de empresa ─────────────────────────────────────────────────────
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

    # ── Pesos aplicados ───────────────────────────────────────────────────────
    lines.append("## PESOS APLICADOS AL SCORING")
    for var, w in scoring_output.applied_weights.items():
        lines.append(f"  {var}: {w * 100:.1f}%")
    if scoring_output.weight_adjustments:
        lines.append("Ajustes dinámicos realizados:")
        for adj in scoring_output.weight_adjustments:
            sign = "+" if adj.delta > 0 else ""
            lines.append(f"  {adj.variable}: {sign}{adj.delta * 100:.0f}%  — {adj.reason}")
    lines.append("")

    # ── Ranking calculado ─────────────────────────────────────────────────────
    lines += [
        "## RANKING CALCULADO — NO MODIFICAR ORDEN NI SCORES",
        f"Confianza del scoring: {scoring_output.scoring_confidence.upper()}",
        "",
    ]

    for crm in scoring_output.ranked_crms:
        lines.append(f"#{crm.rank}  {crm.crm_name}  ({crm.crm_category})")
        lines.append(f"  Score final:  {crm.final_score} / 100")
        lines.append(f"  TCO 3 años:   {crm.tco_3y_eur:,.0f} €")
        lines.append("  Desglose de variables:")
        for var, detail in crm.score_breakdown.items():
            lines.append(
                f"    {var:<22} {detail.raw_score:4.1f}/10  "
                f"(peso {detail.weight * 100:.1f}%  →  contribución {detail.weighted_contribution:.3f})"
            )
        lines.append("")

    # ── CRMs excluidos ────────────────────────────────────────────────────────
    if excluded_crms:
        lines.append("## CRMS EXCLUIDOS DEL RANKING")
        for exc in excluded_crms:
            lines.append(f"  [{exc.filter_code}] {exc.crm_name}: {exc.reason}")
        lines.append("")

    # ── Alertas del sistema ───────────────────────────────────────────────────
    if scoring_output.all_flags:
        lines.append("## ALERTAS DEL SISTEMA")
        for flag in scoring_output.all_flags:
            lines.append(
                f"  [{flag.severity.upper()}] {flag.crm_name} / {flag.code}: {flag.message}"
            )
        lines.append("")

    # ── Contexto semántico (RAG) ──────────────────────────────────────────────
    if semantic_context:
        lines.append("## CONTEXTO DE REVIEWS VERIFICADAS")
        lines.append("(Fragmentos de opiniones de empresas similares — usa para fundamentar el análisis)")
        lines.append("")
        for chunk in semantic_context[:8]:   # máximo 8 chunks para controlar tokens
            crm_id     = chunk.get("crm_id", "")
            chunk_type = chunk.get("chunk_type", "")
            content    = str(chunk.get("content", ""))[:400]
            lines.append(f"[{crm_id} | {chunk_type}]")
            lines.append(content)
            lines.append("")
    else:
        lines.append("## CONTEXTO DE REVIEWS")
        lines.append("No hay contexto semántico disponible para esta evaluación.")
        lines.append("")

    # ── Instrucción final ─────────────────────────────────────────────────────
    lines += [
        "## INSTRUCCIÓN",
        "Genera el veredicto completo siguiendo exactamente el formato de las 6 secciones.",
        "Fundamenta cada sección en los datos del ranking, las alertas y el contexto de reviews.",
        "El análisis debe ser específico para el perfil de empresa descrito, no genérico.",
    ]

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# STREAMING DEL VEREDICTO
# ══════════════════════════════════════════════════════════════════════════════

async def stream_verdict(
    scoring_output: ScoringOutput,
    profile: IntakeProfile,
    semantic_context: List[dict],
    excluded_crms: List[ExclusionResult] = [],
) -> AsyncGenerator[str, None]:
    """
    Llama a Claude con streaming activado y emite los tokens en formato SSE.

    Formato de los eventos emitidos:
      data: {"token": "..."}          ← fragmento de texto generado
      data: {"type": "done"}          ← stream completado con éxito
      data: {"type": "error", ...}    ← error recuperable

    El frontend acumula los tokens y detecta las etiquetas de sección
    ([VEREDICTO], [RANKING], etc.) para renderizar cada bloque con su estilo.

    Args:
        scoring_output:   Resultado completo del motor de scoring.
        profile:          Perfil de empresa del intake.
        semantic_context: Chunks RAG de crm_embeddings (puede ser lista vacía).
        excluded_crms:    CRMs descartados por los filtros duros.

    Yields:
        Strings en formato SSE: "data: {json}\n\n"
    """
    user_message = build_user_message(
        scoring_output, profile, semantic_context, excluded_crms
    )

    logger.info(
        f"Iniciando stream Claude — CRMs en ranking: {len(scoring_output.ranked_crms)} "
        f"| Confianza: {scoring_output.scoring_confidence}"
    )

    try:
        async with _client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        ) as stream:
            async for text in stream.text_stream:
                yield f"data: {json.dumps({'token': text}, ensure_ascii=False)}\n\n"

        logger.info("Stream Claude completado con éxito")
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    except anthropic.APITimeoutError:
        logger.error("Timeout en la llamada a Claude (>45s)")
        yield (
            f"data: {json.dumps({'type': 'error', 'code': 'E02', 'message': 'El análisis tardó demasiado. El contenido parcial ya recibido sigue siendo válido. Puedes reintentar solo la generación del veredicto.'})}\n\n"
        )

    except anthropic.RateLimitError:
        logger.error("Rate limit de la API de Anthropic")
        yield (
            f"data: {json.dumps({'type': 'error', 'code': 'E03', 'message': 'Límite de uso de la API alcanzado. Reintenta en unos segundos.'})}\n\n"
        )

    except anthropic.APIError as exc:
        logger.error(f"Error de la API de Anthropic: {exc}")
        yield (
            f"data: {json.dumps({'type': 'error', 'code': 'E04', 'message': f'Error de la API: {str(exc)}'})}\n\n"
        )

    except Exception as exc:
        logger.exception(f"Error inesperado en stream_verdict: {exc}")
        yield (
            f"data: {json.dumps({'type': 'error', 'code': 'E99', 'message': 'Error interno. Por favor, inténtalo de nuevo.'})}\n\n"
        )
