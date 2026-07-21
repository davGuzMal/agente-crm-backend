"""
app/models/feedback.py
───────────────────────
Payload para actualizar una fila de evaluation_sessions después de que se
recogió feedback real de una empresa piloto (qué CRM eligió de verdad, y
si la recomendación del agente le pareció acertada).

Se separa de intake.py porque no forma parte del contrato de /evaluate —
es un update posterior, hecho por quien conduce la entrevista de feedback
(no por el usuario final del formulario).
"""

from pydantic import BaseModel
from typing import Optional


class EvaluationFeedback(BaseModel):

    crm_elegido_real: Optional[str] = None
    """
    Nombre del CRM que la empresa piloto terminó eligiendo en la realidad,
    tal cual se quiera registrar (no necesariamente el crm_id del catálogo,
    ya que la empresa pudo elegir algo fuera de las 15 CRMs evaluadas).
    """

    feedback_satisfaccion: Optional[str] = None
    """
    Texto libre con la valoración de la empresa piloto sobre el veredicto:
    si el ranking/ganador le pareció acertado, qué le faltó, etc.
    """
