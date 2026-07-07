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
from typing import AsyncGenerator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse, JSONResponse

from app.models.intake import IntakeProfile
from app.services.retrieval import load_crm_candidates, search_semantic_context
from app.services.filter import apply_hard_filters
from app.services.scoring import score_and_rank
from app.services.llm import stream_verdict

logger = logging.getLogger(__name__)
router = APIRouter()


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
    crm_ids = [c.crm_id for c in filter_output.passed]
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
        # Evento inicial: metadatos del scoring (no depende de Claude)
        metadata = {
            "type": "metadata",
            "scoring": {
                "winner":    scoring_output.winner.crm_name if scoring_output.winner else None,
                "runner_up": scoring_output.runner_up.crm_name if scoring_output.runner_up else None,
                "confidence": scoring_output.scoring_confidence,
                "ranking": [
                    {
                        "rank":      crm.rank,
                        "name":      crm.crm_name,
                        "score":     crm.final_score,
                        "tco_3y":    crm.tco_3y_eur,
                    }
                    for crm in scoring_output.ranked_crms
                ],
                "excluded": [
                    {"name": e.crm_name, "code": e.filter_code}
                    for e in filter_output.excluded
                ],
                "flags_count": len(scoring_output.all_flags),
            },
        }
        yield f"data: {json.dumps(metadata, ensure_ascii=False)}\n\n"

        # Tokens del veredicto narrativo de Claude
        async for chunk in stream_verdict(
            scoring_output=scoring_output,
            profile=profile,
            semantic_context=semantic_context,
            excluded_crms=filter_output.excluded,
        ):
            yield chunk

    return StreamingResponse(
        sse_pipeline(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "Connection":       "keep-alive",
            "X-Accel-Buffering": "no",      # desactiva buffering en nginx/proxy
        },
    )
