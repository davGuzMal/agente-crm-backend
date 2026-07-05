from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional, List

router = APIRouter()


class IntakeProfile(BaseModel):
    sector: str
    modelo: str                          # B2B | B2C | mixto
    presupuesto_eur: int
    presupuesto_flex: str                # rigido | algo_margen | flexible
    usuarios_crm: int
    empleados: int
    sistema_actual: str
    suite: str
    tools: List[str] = []
    equipo_tech: str
    clientes: str
    crecimiento: str
    # Campos opcionales (ramas del intake)
    crm_actual_nombre: Optional[str] = None
    motivo_cambio: Optional[str] = None
    datos_excel: Optional[str] = None
    erp_nombre: Optional[str] = None
    it_experiencia: Optional[str] = None
    consultor_presupuesto: Optional[str] = None
    usuarios_futuros: Optional[str] = None


@router.post("/evaluate")
async def evaluate(profile: IntakeProfile):
    """
    Endpoint principal de evaluación.
    Recibe el perfil del intake y devuelve el veredicto en streaming.
    Fases:
      1. Filtros de exclusión duros
      2. Cálculo TCO + ajuste pesos
      3. Score ponderado + ranking
      4. Llamada LLM con streaming
    """
    # TODO: implementar en semanas 5-6
    return {
        "status": "received",
        "profile": profile.model_dump()
    }