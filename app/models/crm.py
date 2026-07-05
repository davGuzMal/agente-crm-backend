"""
app/models/crm.py
─────────────────
Modelo de datos de un CRM candidato tal como llega desde Supabase
antes de pasar por los filtros y el scoring.

Combina campos de tres tablas:
  - crm_catalog  → identidad, límites de equipo, GDPR
  - crm_scoring  → scores base de variables + datos de implementación
  - crm_pricing  → annual_license_eur (calculado por retrieval.py
                    según el nº de usuarios del intake) + campos TCO
"""

from pydantic import BaseModel
from typing import List, Optional


class CRMCandidate(BaseModel):

    # ── Identidad ────────────────────────────────────────────────────────────
    crm_id: str
    name: str
    crm_category: str          # generalista | vertical | erp_module | inside_sales

    # ── Límites de equipo (filtro F01) ───────────────────────────────────────
    best_fit_team_size_min: int
    best_fit_team_size_max: int

    # ── Compliance (filtro F02) ──────────────────────────────────────────────
    gdpr_compliant: bool
    data_hosting_regions: List[str]        # ["EU", "US", "APAC"]

    # ── Implementación (filtro F03 + scoring) ────────────────────────────────
    avg_implementation_weeks: float
    requires_external_consultant: str      # never | optional | recommended | required

    # ── Precio base (filtro F04) ─────────────────────────────────────────────
    annual_license_eur: float
    """
    Calculado por retrieval.py para el nº exacto de usuarios del intake.
    Ejemplo: plan Pro × 12 usuarios × 12 meses = 9.600 €/año.
    No incluye implementación ni migración.
    """

    # ── Campos TCO completo (scoring.py) ─────────────────────────────────────
    annual_price_increase_pct: float = 0.07
    """
    Incremento anual de precio histórico (fracción, no porcentaje).
    Ejemplo: 0.07 → 7% anual. Salesforce ~0.10, HubSpot ~0.07.
    """

    price_per_contact: bool = False
    """
    True si el precio escala por número de contactos (modelo HubSpot).
    Si True, contact_tier_thresholds debe estar presente.
    """

    contact_tier_thresholds: Optional[dict] = None
    """
    Solo si price_per_contact=True.
    Formato: {"1000": 0, "5001": 45, "10001": 90}
    Clave = contactos mínimos del tier, valor = €/mes adicionales.
    """

    implementation_cost_est_eur: dict = {}
    """
    Estimación de coste de implementación.
    Formato: {"min": 0, "typical": 1500, "max": 8000}
    retrieval.py lo carga desde crm_pricing.implementation_cost_est_eur.
    """

    training_cost_est_eur: dict = {}
    """
    Estimación de coste de formación inicial.
    Formato: {"min": 0, "typical": 600, "max": 2000}
    """

    migration_cost_est_eur: dict = {}
    """
    Coste estimado de migración según sistema de origen.
    Formato: {"from_excel": 200, "from_crm": 1200, "from_erp": 4000}
    """

    price_lock_policy: Optional[str] = None
    """
    Historial de cambios de precio.
    Valores: "grandfathered" | "annual_increase_capped" | "market_rate" | "unpredictable"
    """

    # ── Variables de scoring base (crm_scoring) ───────────────────────────────
    learning_curve_score: Optional[float] = None           # 0–10
    implementation_complexity_score: Optional[float] = None # 0–10
    lockin_risk_score: Optional[float] = None               # 0–10
    support_score: Optional[float] = None                   # 0–10
    review_score: Optional[float] = None                    # 0–10

    # ── Datos de soporte e integración ────────────────────────────────────────
    native_integrations: List[str] = []
    support_spanish_available: bool = False

    # ── Metadatos de calidad (crm_data_quality) ───────────────────────────────
    scoring_confidence: Optional[str] = None   # high | medium | low
    pricing_last_updated: Optional[str] = None # ISO date string "YYYY-MM-DD"
