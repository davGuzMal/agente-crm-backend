"""
Router de endpoints internos — NO expuesto al frontend ni a internet
sin autenticación. Protegidos por un secreto compartido vía header
`X-Cron-Secret` (la cadena aleatoria definida en INTERNAL_CRON_SECRET).

Uso actual (R11): lote diario de envío de informes en PDF por Zoho Mail.
Disparado por GitHub Actions a las 08:00 UTC; este endpoint procesa
todos los pilot_contacts con email capturado y sin informe_enviado_at.
"""
import logging
import os

from fastapi import APIRouter, Header, HTTPException

from app.services.pdf_report import build_report_pdf
from app.services.email_sender import send_report_email
from app.routers.evaluate import _get_client  # reutiliza el cliente ya existente

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/internal/send-pending-reports")
def send_pending_reports(x_cron_secret: str = Header(None)):
    """
    Procesa el lote de informes pendientes:
      - Selecciona pilot_contacts con email Y sin informe_enviado_at.
      - Por cada fila, recupera scoring_metadata + verdict_sections de
        evaluation_sessions, genera el PDF y lo envía por Zoho.
      - Marca informe_enviado_at en Supabase al tener éxito.
      - Si falla el envío, NO marca timestamp: el lote de mañana lo
        reintenta automáticamente (decisión de David — fallo silencioso).

    Auth: header `X-Cron-Secret` debe coincidir con la env var
    `INTERNAL_CRON_SECRET`. Coincide con el secret configurado en GitHub
    Actions (mismo valor en Render y en repo Settings → Secrets).

    Returns:
        {"sent": int, "failed": int, "total": int} — útil para el log
        de GitHub Actions al ejecutar workflow_dispatch a mano.
    """
    if x_cron_secret != os.environ.get("INTERNAL_CRON_SECRET"):
        raise HTTPException(status_code=401, detail="unauthorized")

    client = _get_client()
    pending = (
        client.table("pilot_contacts")
        .select("session_id, contacto_email")
        .not_.is_("contacto_email", "null")
        .is_("informe_enviado_at", "null")
        .execute()
    )

    sent, failed = 0, 0
    for row in pending.data:
        session = (
            client.table("evaluation_sessions")
            .select("scoring_metadata, verdict_sections")
            .eq("session_id", row["session_id"])
            .single()
            .execute()
        )
        if not session.data:
            logger.warning(
                f"pilot_contacts {row['session_id']} sin evaluation_sessions — "
                "puede que el upsert fallara en su día. Saltando."
            )
            continue
        try:
            pdf_bytes = build_report_pdf(
                session.data["scoring_metadata"],
                session.data["verdict_sections"],
            )
            send_report_email(row["contacto_email"], pdf_bytes)
            client.table("pilot_contacts").update(
                {"informe_enviado_at": "now()"}
            ).eq("session_id", row["session_id"]).execute()
            sent += 1
        except Exception as exc:
            # Silencioso para el usuario, visible solo en logs — decisión de David.
            # Queda en NULL, el lote de mañana lo reintenta solo.
            logger.error(f"Fallo enviando informe a {row['session_id']}: {exc}")
            failed += 1

    logger.info(f"Lote finalizado: {sent} enviados, {failed} fallidos, {len(pending.data)} totales")
    return {"sent": sent, "failed": failed, "total": len(pending.data)}