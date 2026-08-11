"""
Envío del informe en PDF por SMTP de Zoho Mail EU.

Usa smtplib de la librería estándar de Python — no hace falta ninguna
dependencia nueva para esto (fpdf2 es la única, y es solo para el PDF).

Decisiones:
- SMTP_SSL puerto 465 (TLS implícito) — Zoho lo soporta y evita el
  handshake STARTTLS que añadiría un round-trip innecesario.
- Variables de entorno: ZOHO_EMAIL, ZOHO_SMTP_HOST, ZOHO_APP_PASSWORD.
  La contraseña es una "app password" generada en el panel de Zoho, NO
  la contraseña normal de la cuenta.
- Sin pool/retry: send_report_email() se llama una vez por destinatario
  dentro de un lote; los fallos se capturan en el caller (router
  interno) que registra el error y deja informe_enviado_at = NULL para
  que el lote de mañana lo reintente.
"""
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email.mime.text import MIMEText


def send_report_email(to_email: str, pdf_bytes: bytes) -> None:
    """
    Envía el PDF adjunto a `to_email` desde la cuenta Zoho configurada.

    Args:
        to_email:  Dirección del destinatario (validada ya en pilot_contact.py).
        pdf_bytes: Contenido del PDF, tal cual lo devuelve build_report_pdf().

    Raises:
        KeyError  si falta alguna de las env vars (ZOHO_EMAIL, ZOHO_SMTP_HOST,
                  ZOHO_APP_PASSWORD). El caller lo capturará.
        smtplib.SMTPAuthenticationError si las credenciales son inválidas.
        smtplib.SMTPException / OSError si hay un problema de red.
    """
    msg = MIMEMultipart()
    msg["From"] = f"Trust <{os.environ['ZOHO_EMAIL']}>"
    msg["To"] = to_email
    msg["Subject"] = "Tu informe de evaluación de CRM — Trust"

    body = (
        "Hola,\n\n"
        "Adjunto tu informe de evaluación de CRM generado por Trust.\n\n"
        "Un saludo,\nEquipo Trust"
    )
    msg.attach(MIMEText(body, "plain"))

    attachment = MIMEApplication(pdf_bytes, _subtype="pdf")
    attachment.add_header("Content-Disposition", "attachment", filename="informe-trust.pdf")
    msg.attach(attachment)

    with smtplib.SMTP_SSL(os.environ["ZOHO_SMTP_HOST"], 465) as server:
        server.login(os.environ["ZOHO_EMAIL"], os.environ["ZOHO_APP_PASSWORD"])
        server.send_message(msg)