"""
app/models/pilot_contact.py
────────────────────────────
Payload para registrar/actualizar los datos de contacto de la empresa
piloto asociada a una sesión de evaluación.

Vive en una tabla separada (pilot_contacts) en vez de columnas dentro de
evaluation_sessions a propósito: evaluation_sessions es el payload técnico
(perfil, scoring, contexto RAG, veredicto) que en algún momento podría
exportarse o analizarse sin fricción de privacidad. Los datos de contacto
son PII y deben poder gestionarse (o borrarse) de forma independiente.
"""

from pydantic import BaseModel, EmailStr
from typing import Optional


class PilotContact(BaseModel):

    session_id: str
    """
    Debe corresponder a un session_id ya existente en evaluation_sessions
    (FK). Si no existe, la inserción falla con un error de integridad
    referencial que el endpoint traduce a 404.
    """

    empresa_nombre: Optional[str] = None
    contacto_nombre: Optional[str] = None
    # SPRINT-BE-002 / CC-005: validación de formato. Antes recibíamos texto
    # libre; al pasar a tráfico público sin fricción, queremos un 422 claro
    # si el usuario teclea "no-es-un-email". Requiere email-validator.
    contacto_email: Optional[EmailStr] = None
    contacto_telefono: Optional[str] = None
    notas: Optional[str] = None
    """Notas libres de seguimiento (ej. fecha de llamada, próximos pasos)."""
