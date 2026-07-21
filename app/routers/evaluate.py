"""
app/routers/evaluate.py
───────────────────────
Endpoint principal de evaluación de CRM.

Pipeline completo en un solo request:
  POST /api/evaluate
    → load_crm_candidates()    Supabase → CRMCandidate[]
    → apply_hard_filters()     filtra por TCO, usuarios, GDPR, complejidad
    → score_and_rank()         TCO completo + scoring ponderado + ranking
    → search_semantic_context() RAG chunks desde pgvector
    → stream_verdict()         Claude SSE → StreamingResponse

Respuestas:
  200 text/event-stream  ← evaluación con éxito, tokens en SSE
  200 application/json   ← error E01 (ningún CRM pasa los filtros)
  503 application/json   ← error de conexión con Supabase
"""

import json
import logging
import uuid
from typing import AsyncGenerator, Optional

from fastapi import APIRouter
from fastapi.responses import StreamingResponse, JSONResponse

from app.models.intake import IntakeProfile
from app.services.retrieval import load_crm_candidates, search_semantic_context, _get_client
from app.services.filter import apply_hard_filters
from app.services.scoring import score_and_rank, ScoredCRM
from app.services.llm import stream_verdict

logger = logging.getLogger(__name__)
router = APIRouter()


# ══════════════════════════════════════════════════════════════════════════════
# SERIALIZACIÓN DEL EVENTO METADATA
# ══════════════════════════════════════════════════════════════════════════════

# Nombres legibles de las 6 variables de scoring para el frontend
VARIABLE_LABELS_ES: dict[str, str] = {
    "tco":               "Coste total (TCO 3 años)",
    "curva_aprendizaje": "Curva de aprendizaje",
    "complejidad_impl":  "Complejidad de implementación",
    "lockin_risk":       "Riesgo de dependencia",
    "soporte":           "Calidad de soporte",
    "reviews":           "Opiniones verificadas",
}

# Umbrales para clasificar cada variable como fortaleza, debilidad o neutral
PRO_THRESHOLD = 7.5   # score ≥ 7.5 → fortaleza
CON_THRESHOLD = 5.0   # score < 5.0 → debilidad


def _serialize_crm(crm: ScoredCRM) -> dict:
    """
    Serializa un ScoredCRM al formato que necesita el frontend para renderizar:
      - Las 6 tarjetas de desglose por variable (score + peso + categoría)
      - Las listas de pros y contras derivadas automáticamente de los scores
      - Las alertas específicas del CRM

    Los pros/contras se derivan de umbrales sobre raw_score, no de prosa del LLM.
    Esto evita parsing frágil y da datos estructurados inmediatos al frontend.
    """
    breakdown: dict = {}
    pros: list = []
    cons: list = []

    for var, detail in crm.score_breakdown.items():
        label     = VARIABLE_LABELS_ES.get(var, var)
        score     = round(detail.raw_score, 1)
        weight_pct = round(detail.weight * 100, 1)

        if score >= PRO_THRESHOLD:
            category = "pro"
            pros.append({"variable": var, "label": label, "score": score})
        elif score < CON_THRESHOLD:
            category = "con"
            cons.append({"variable": var, "label": label, "score": score})
        else:
            category = "neutral"

        breakdown[var] = {
            "label":      label,
            "score":      score,
            "weight_pct": weight_pct,
            "category":   category,
        }

    return {
        "rank":            crm.rank,
        "id":              crm.crm_id,
        "name":            crm.crm_name,
        "category":        crm.crm_category,
        "score":           crm.final_score,
        "tco_3y":          crm.tco_3y_eur,
        "tco_score":       round(crm.tco_score, 1),
        "score_breakdown": breakdown,
        "pros":            pros,
        "cons":            cons,
        "flags": [
            {
                "code":     f.code,
                "severity": f.severity,
                "message":  f.message,
            }
            for f in crm.flags
        ],
    }


# ══════════════════════════════════════════════════════════════════════════════
# PERSISTENCIA DE LA SESIÓN DE EVALUACIÓN (feedback loop futuro)
# ══════════════════════════════════════════════════════════════════════════════

def _parse_sse_chunk(chunk: str) -> Optional[dict]:
    """
    Reconstruye el dict del evento a partir de la línea SSE ya formateada
    que produce stream_verdict() (formato "data: {...}\n\n"). Se usa solo
    para acumular estado a persistir; el chunk original se sigue emitiendo
    al cliente sin modificar.
    """
    if not chunk.startswith("data: "):
        return None
    try:
        return json.loads(chunk[len("data: "):].strip())
    except json.JSONDecodeError:
        return None


def _persist_evaluation_session(
    *,
    session_id: Optional[str],
    profile: IntakeProfile,
    scoring_metadata: dict,
    semantic_context: list,
    verdict_sections: dict,
    sections_completed: list,
    llm_error_code: Optional[str],
) -> None:
    """
    Guarda un registro de la sesión de evaluación en Supabase para permitir,
    en el futuro, comparar la recomendación del agente contra la elección
    real de CRM del piloto / su feedback de satisfacción.

    Nunca debe romper la respuesta al usuario: cualquier fallo se loguea
    y se descarta.
    """
    row = {
        "session_id":          session_id or str(uuid.uuid4()),
        "intake_profile":       json.loads(profile.model_dump_json()),
        "scoring_metadata":     scoring_metadata,
        "semantic_context":     semantic_context,
        "verdict_sections":     verdict_sections,
        "sections_completed":   sections_completed,
        "model":                "claude",
        "llm_error_code":       llm_error_code,
    }
    try:
        client = _get_client()
        client.table("evaluation_sessions").insert(row).execute()
        logger.info(f"Sesión de evaluación persistida: {row['session_id']}")
    except Exception as exc:
        logger.error(f"No se pudo persistir la sesión de evaluación: {exc}")


@router.post("/evaluate")
async def evaluate(profile: IntakeProfile):
    """
    Evalúa los CRMs para el perfil de empresa recibido y devuelve
    el veredicto como stream de Server-Sent Events.

    El cliente debe consumir el stream y acumular los tokens hasta
    recibir {"type": "done"} o {"type": "error"}.
    """

    # ── Paso 1: Cargar candidatos desde Supabase ──────────────────────────────
    try:
        candidates = load_crm_candidates(profile)
    except RuntimeError as exc:
        logger.error(f"Error cargando CRMs desde Supabase: {exc}")
        return JSONResponse(
            status_code=503,
            content={
                "error": "E_SUPABASE",
                "message": "No se pudo conectar con la base de datos. Inténtalo de nuevo.",
            },
        )

    if not candidates:
        return JSONResponse(
            status_code=200,
            content={
                "error": "E00",
                "message": "No hay CRMs en el catálogo. Verifica la base de datos.",
            },
        )

    # ── Paso 2: Filtros de exclusión duros ────────────────────────────────────
    filter_output = apply_hard_filters(profile, candidates)

    logger.info(
        f"Filtrado: {filter_output.passed_count} CRMs al scoring, "
        f"{filter_output.excluded_count} excluidos"
    )

    # E01: ningún CRM pasa los filtros
    if not filter_output.passed:
        excluded_summary = [
            {"crm": e.crm_name, "motivo": e.reason}
            for e in filter_output.excluded
        ]
        return JSONResponse(
            status_code=200,
            content={
                "error": "E01",
                "message": (
                    "No encontramos CRMs que se ajusten a tu presupuesto y perfil. "
                    "Considera ampliar el presupuesto o ajustar el número de usuarios."
                ),
                "crms_excluidos": excluded_summary,
            },
        )

    # ── Paso 3: Scoring y ranking ─────────────────────────────────────────────
    scoring_output = score_and_rank(filter_output, profile)

    logger.info(
        f"Scoring completado — ganador: {scoring_output.winner.crm_name} "
        f"({scoring_output.winner.final_score}/100)"
    )

    # ── Paso 4: Contexto semántico para RAG ───────────────────────────────────
    # Usamos scoring_output.ranked_crms (recortado a top_n=5 en scoring.py),
    # no filter_output.passed. Este último puede incluir CRMs que pasaron los
    # filtros duros pero quedaron fuera del ranking final por score bajo — sus
    # chunks competirían por los huecos de semantic_context[:10] en llm.py sin
    # que el LLM llegue a mencionarlos nunca (no están en ranked_crms).
    crm_ids = [c.crm_id for c in scoring_output.ranked_crms]
    semantic_context = search_semantic_context(crm_ids, profile)

    logger.info(f"Contexto RAG: {len(semantic_context)} chunks recuperados")

    # ── Paso 5: Stream del veredicto con Claude ───────────────────────────────
    async def sse_pipeline() -> AsyncGenerator[str, None]:
        """
        Emite primero un evento de metadatos con el scoring completo,
        luego los tokens del veredicto de Claude.

        El frontend puede usar los metadatos para renderizar el ranking
        visual mientras Claude genera el texto narrativo.
        """
        # Evento inicial: metadatos del scoring (llega antes del primer token de Claude).
        # El frontend usa este evento para renderizar el ranking visual completo
        # mientras Claude genera el texto narrativo encima.
        metadata = {
            "type": "metadata",
            "scoring": {
                "winner":     scoring_output.winner.crm_name if scoring_output.winner else None,
                "runner_up":  scoring_output.runner_up.crm_name if scoring_output.runner_up else None,
                "confidence": scoring_output.scoring_confidence,

                # Ranking completo con desglose por variable, pros/contras y alertas.
                # Cada item generado por _serialize_crm() incluye todo lo que
                # el frontend necesita para renderizar las tarjetas sin esperar al LLM.
                "ranking": [
                    _serialize_crm(crm)
                    for crm in scoring_output.ranked_crms
                ],

                # CRMs descartados por los filtros duros (para mostrar como "opciones futuras")
                "excluded": [
                    {
                        "name":   e.crm_name,
                        "code":   e.filter_code,
                        "reason": e.reason,
                    }
                    for e in filter_output.excluded
                ],

                # Ajustes de pesos aplicados al perfil (para mostrar transparencia del scoring)
                "weight_adjustments": [
                    {
                        "variable":  adj.variable,
                        "label":     VARIABLE_LABELS_ES.get(adj.variable, adj.variable),
                        "delta_pct": round(adj.delta * 100, 1),
                        "reason":    adj.reason,
                    }
                    for adj in scoring_output.weight_adjustments
                ],

                "applied_weights": {
                    var: {
                        "label":     VARIABLE_LABELS_ES.get(var, var),
                        "weight_pct": round(w * 100, 1),
                    }
                    for var, w in scoring_output.applied_weights.items()
                },

                "flags_count": len(scoring_output.all_flags),
            },
        }
        yield f"data: {json.dumps(metadata, ensure_ascii=False)}\n\n"

        # Acumuladores para la persistencia de la sesión (se llenan a medida
        # que se re-parsean los eventos SSE ya formateados que emite
        # stream_verdict(); el chunk original se reenvía sin tocar).
        verdict_sections: dict[str, str] = {}
        sections_completed: list = []
        llm_error_code: Optional[str] = None

        # Tokens del veredicto narrativo de Claude
        async for chunk in stream_verdict(
            scoring_output=scoring_output,
            profile=profile,
            semantic_context=semantic_context,
            excluded_crms=filter_output.excluded,
        ):
            event = _parse_sse_chunk(chunk)
            if event:
                event_type = event.get("type")
                if event_type == "token" and event.get("section"):
                    section = event["section"]
                    verdict_sections[section] = (
                        verdict_sections.get(section, "") + event.get("content", "")
                    )
                elif event_type == "done":
                    sections_completed = event.get("sections_completed", [])
                elif event_type == "error":
                    llm_error_code = event.get("code")
            yield chunk

        _persist_evaluation_session(
            session_id=profile.session_id,
            profile=profile,
            scoring_metadata=metadata["scoring"],
            semantic_context=semantic_context,
            verdict_sections=verdict_sections,
            sections_completed=sections_completed,
            llm_error_code=llm_error_code,
        )

    return StreamingResponse(
        sse_pipeline(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "Connection":       "keep-alive",
            "X-Accel-Buffering": "no",      # desactiva buffering en nginx/proxy
        },
    )