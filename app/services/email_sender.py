"""
Envío del informe en PDF vía la API HTTPS de ZeptoMail — no SMTP.

Cambio de transporte tras confirmar que Render bloquea los puertos
SMTP (25/465/587) en el plan gratuito (changelog oficial de Render,
11/08/2026). El resto del flujo (pdf_report.py, internal.py) no cambia:
la firma de send_report_email() es la misma.

Por qué ZeptoMail en vez de SendGrid / Mailgun / Postmark:
- Ya estamos en el ecosistema Zoho (la cuenta de correo corporativo y el
  dominio trustcrmagent.com viven ahí), así que el dominio ya está
  verificado y el from-address es el mismo.
- Plan gratuito cubre los volúmenes del R11 (lotes diarios de un dígito).
- API HTTPS estándar — sin SDKs propietarios, sin overhead.

Detalles a tener en cuenta si alguien edita este archivo:
- "attachments" DEBE ser una lista [{...}]. Pasarlo como objeto suelto
  devuelve "Invalid value format" desde la API. Es el error más común
  documentado de ZeptoMail.
- El header de auth es "Zoho-enczapikey <TOKEN>", con un único espacio
  entre el esquema y el token. Sin ese espacio: 401.
- ZEPTOMAIL_API_URL puede ser api.zeptomail.com o api.zeptomail.eu
  según la región. Configurar la correcta desde el dashboard, NO
  asumirla (la última vez asumimos mal el host de SMTP y fue justo el
  problema).
"""
import base64
import os

import httpx


def send_report_email(to_email: str, pdf_bytes: bytes) -> None:
    """
    Envía el PDF adjunto a `to_email` desde la cuenta Zoho configurada
    en ZeptoMail.

    Args:
        to_email:  Dirección del destinatario (validada ya en pilot_contact.py).
        pdf_bytes: Contenido del PDF, tal cual lo devuelve build_report_pdf().

    Raises:
        KeyError si falta alguna env var (ZEPTOMAIL_TOKEN, ZEPTOMAIL_API_URL,
                  ZEPTOMAIL_FROM_EMAIL). El caller (internal.py) lo captura y
                  deja informe_enviado_at = NULL para reintento al día siguiente.
        httpx.HTTPStatusError si la API devuelve >= 400. El error incluye
                  el body de ZeptoMail con el código y mensaje exactos
                  ("Invalid value format", "Unauthorized", etc.).
        httpx.RequestError si hay un problema de red.
    """
    # --- DIAGNÓSTICO TEMPORAL (11/08/2026) — quitar en cuanto se resuelva el 500 ---
    token = os.environ["ZEPTOMAIL_TOKEN"]
    api_url = os.environ["ZEPTOMAIL_API_URL"]
    from_email = os.environ["ZEPTOMAIL_FROM_EMAIL"]
    print(
        f"[DIAG] TOKEN len={len(token)} starts='{token[:2]}' ends='{token[-2:]}' "
        f"tiene_espacios_extra={token != token.strip()}"
    )
    print(f"[DIAG] API_URL='{api_url}' tiene_espacios_extra={api_url != api_url.strip()}")
    print(f"[DIAG] FROM_EMAIL='{from_email}' tiene_espacios_extra={from_email != from_email.strip()}")
    # --- FIN DIAGNÓSTICO ---

    pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")

    payload = {
        "from": {
            "address": os.environ["ZEPTOMAIL_FROM_EMAIL"],
            "name": "Trust",
        },
        "to": [{"email_address": {"address": to_email}}],
        "subject": "Tu informe de evaluación de CRM — Trust",
        "textbody": (
            "Hola,\n\n"
            "Adjunto tu informe de evaluación de CRM generado por Trust.\n\n"
            "Un saludo,\nEquipo Trust"
        ),
        "attachments": [
            {
                "content": pdf_b64,
                "mime_type": "application/pdf",
                "name": "informe-trust.pdf",
            }
        ],
    }

    api_url = os.environ["ZEPTOMAIL_API_URL"]

    response = httpx.post(
        f"{api_url}/v1.1/email",
        json=payload,
        headers={
            "Authorization": f"Zoho-enczapikey {os.environ['ZEPTOMAIL_TOKEN']}",
            "Content-Type": "application/json",
        },
        timeout=15,
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        # Re-raise con el cuerpo completo de la respuesta. ZeptoMail incluye
        # ahí el código de error específico, el campo que falló y un
        # mensaje legible — raise_for_status() por defecto solo guarda el
        # código de estado, dejando "Server error '500' for url..." que
        # no deja diagnosticar. Sin este RuntimeError, los logs de Render
        # no muestran la causa real.
        raise RuntimeError(
            f"ZeptoMail respondió {e.response.status_code}: {e.response.text}"
        ) from e