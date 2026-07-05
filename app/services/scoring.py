"""
app/services/scoring.py
───────────────────────
Motor de scoring ponderado para el Agente CRM.

Recibe la lista de CRMs que pasaron los filtros duros (FilterOutput.passed)
y el perfil del intake. Calcula el TCO completo, aplica los ajustes
de pesos dinámicos y devuelve el ranking ordenado.

El LLM nunca ejecuta este proceso — recibe el ScoringOutput ya calculado
y lo interpreta en lenguaje natural en las 6 secciones del veredicto.

Flujo interno:
  1. calculate_dynamic_weights  → pesos ajustados + normalizados
  2. calculate_full_tco         → TCO real a 3 años por CRM
  3. _tco_to_score              → TCO convertido a score 0–10
  4. _adjust_variable_scores    → ajustes de scores individuales por perfil
  5. score_and_rank             → ranking final + alertas
"""

from typing import List, Optional, Tuple
from pydantic import BaseModel

from app.models.crm import CRMCandidate
from app.models.intake import IntakeProfile
from app.services.filter import FilterOutput


# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTES Y MAPPERS
# ══════════════════════════════════════════════════════════════════════════════

BASE_WEIGHTS: dict[str, float] = {
    "tco":               0.25,
    "curva_aprendizaje": 0.15,
    "complejidad_impl":  0.15,
    "lockin_risk":       0.15,
    "soporte":           0.15,
    "reviews":           0.15,
}

# Presupuesto anual declarado → límite superior en EUR
# Usado como referencia para calcular el TCO score.
BUDGET_ANNUAL_EUR: dict[str, float] = {
    "Menos de 1.000€/año":        1_000,
    "1.000 – 5.000€/año":         5_000,
    "5.000 – 15.000€/año":       15_000,
    "15.000 – 40.000€/año":      40_000,
    "Más de 40.000€/año":        80_000,
    "No tenemos límite definido": 80_000,
}

# Flexibilidad presupuestaria → multiplicador
FLEX_MULTIPLIER: dict[str, float] = {
    "Límite rígido, no podemos superarlo":      1.0,
    "Hay algo de margen (+20–30%)":             1.3,
    "Flexible si el ROI está bien justificado": 1.5,
}

# Clientes activos → estimación de contactos en base de datos
CLIENTS_TO_CONTACTS: dict[str, int] = {
    "Menos de 100":       100,
    "100 – 500":          300,
    "500 – 2.000":      1_250,
    "2.000 – 10.000":   6_000,
    "Más de 10.000":   12_000,
}

# Sistema de origen → clave en migration_cost_est_eur
MIGRATION_SOURCE_KEY: dict[str, str] = {
    "No usamos nada / papel":               "from_nothing",
    "Excel o Google Sheets":                "from_excel",
    "Un CRM (HubSpot, Salesforce, Zoho…)":  "from_crm",
    "Un ERP con módulo CRM":                "from_erp",
    "Varias herramientas sin integrar":     "from_excel",
}

# Perfiles de equipo
NO_IT_PROFILES = {"No, somos un equipo no técnico"}
FULL_IT_PROFILES = {"Sí, tenemos IT / desarrollador interno"}

# Crecimientos que reducen la urgencia de portabilidad
MODERATE_GROWTH = {
    "Estable, sin cambios significativos",
    "Crecimiento moderado (+10–30%)",
    "Hemos reducido / reestructurado",
}

# Crecimientos que penalizan precios impredecibles
FAST_GROWTH = {
    "Crecimiento rápido (+30–100%)",
    "Crecimiento muy rápido (más del doble)",
}


# ══════════════════════════════════════════════════════════════════════════════
# MODELOS DE OUTPUT
# ══════════════════════════════════════════════════════════════════════════════

class ScoreDetail(BaseModel):
    """Puntuación de una variable individual con su peso aplicado."""
    raw_score: float             # 0–10, valor base (o ajustado si aplica)
    weight: float                # peso final tras ajuste dinámico
    weighted_contribution: float # raw_score × weight


class AlertFlag(BaseModel):
    """Alerta generada durante el scoring para incluir en el informe."""
    code: str         # tco_limite | consultor_recomendado | variable_debil
    crm_id: str
    crm_name: str
    severity: str     # warning | info
    message: str      # texto en español para el informe


class WeightAdjustment(BaseModel):
    """Registro de un ajuste de peso aplicado, con su razón."""
    variable: str
    delta: float      # positivo = sube, negativo = baja
    reason: str


class ScoredCRM(BaseModel):
    """CRM evaluado con score final, TCO y desglose de variables."""
    crm_id: str
    crm_name: str
    crm_category: str
    rank: int = 0                        # asignado tras ordenar

    final_score: float                   # 0–100
    tco_3y_eur: float                    # TCO completo a 3 años
    tco_score: float                     # 0–10

    score_breakdown: dict[str, ScoreDetail]
    flags: List[AlertFlag] = []


class ScoringOutput(BaseModel):
    """
    Resultado completo del motor de scoring.
    Es el payload que recibe el LLM para generar el veredicto.
    """
    ranked_crms: List[ScoredCRM]
    applied_weights: dict[str, float]
    weight_adjustments: List[WeightAdjustment]
    all_flags: List[AlertFlag]
    scoring_confidence: str              # high | medium | low

    @property
    def winner(self) -> Optional[ScoredCRM]:
        return self.ranked_crms[0] if self.ranked_crms else None

    @property
    def runner_up(self) -> Optional[ScoredCRM]:
        return self.ranked_crms[1] if len(self.ranked_crms) > 1 else None


# ══════════════════════════════════════════════════════════════════════════════
# PUNTO DE ENTRADA PRINCIPAL
# ══════════════════════════════════════════════════════════════════════════════

def score_and_rank(
    filter_output: FilterOutput,
    profile: IntakeProfile,
) -> ScoringOutput:
    """
    Calcula el score ponderado de los CRMs que pasaron los filtros y los ordena.

    Args:
        filter_output: Resultado de apply_hard_filters(). Solo se usan los .passed.
        profile:       Perfil de empresa del intake.

    Returns:
        ScoringOutput con el ranking completo y todos los metadatos.

    Raises:
        ValueError: Si no hay CRMs que hayan pasado los filtros.
    """
    if not filter_output.passed:
        raise ValueError(
            "No hay CRMs candidatos después del filtrado. "
            "Considera ampliar el presupuesto o flexibilizarlo."
        )

    # 1. Pesos dinámicos
    weights, adjustments = calculate_dynamic_weights(profile)

    # 2. Puntuar cada CRM
    scored: List[ScoredCRM] = []
    all_flags: List[AlertFlag] = []

    for crm in filter_output.passed:
        scored_crm, flags = _score_crm(crm, profile, weights)
        scored.append(scored_crm)
        all_flags.extend(flags)

    # 3. Ordenar por score final (mayor primero)
    scored.sort(key=lambda x: x.final_score, reverse=True)

    # 4. Asignar ranking
    for i, crm in enumerate(scored):
        crm.rank = i + 1

    # 5. Confianza global
    confidence = _calculate_confidence(scored, all_flags)

    return ScoringOutput(
        ranked_crms=scored,
        applied_weights=weights,
        weight_adjustments=adjustments,
        all_flags=all_flags,
        scoring_confidence=confidence,
    )


# ══════════════════════════════════════════════════════════════════════════════
# AJUSTE DINÁMICO DE PESOS
# ══════════════════════════════════════════════════════════════════════════════

def calculate_dynamic_weights(
    profile: IntakeProfile,
) -> Tuple[dict[str, float], List[WeightAdjustment]]:
    """
    Ajusta los pesos base según las señales del perfil y normaliza a 1.0.

    Reglas aplicadas:
      - Sin IT propio:        curva_aprendizaje +3%, complejidad_impl +2%
      - Presupuesto flexible: tco −2%
      - Crecimiento estable:  lockin_risk −2%

    La normalización garantiza que los pesos sumen exactamente 1.0
    independientemente de cuántas reglas se activen.
    """
    weights = dict(BASE_WEIGHTS)
    adjustments: List[WeightAdjustment] = []

    # Sin IT: la facilidad de adopción es más crítica
    if profile.equipo_tech in NO_IT_PROFILES:
        weights["curva_aprendizaje"] += 0.03
        weights["complejidad_impl"]  += 0.02
        adjustments.append(WeightAdjustment(
            variable="curva_aprendizaje", delta=+0.03,
            reason="Equipo sin perfil técnico dedicado"
        ))
        adjustments.append(WeightAdjustment(
            variable="complejidad_impl", delta=+0.02,
            reason="Equipo sin perfil técnico dedicado"
        ))

    # Presupuesto flexible: el TCO pesa menos (otros factores pueden justificar sobrecosto)
    if profile.presupuesto_flex == "Flexible si el ROI está bien justificado":
        weights["tco"] -= 0.02
        adjustments.append(WeightAdjustment(
            variable="tco", delta=-0.02,
            reason="Presupuesto flexible si el ROI está justificado"
        ))

    # Crecimiento estable o moderado: el lock-in es menos urgente
    if profile.crecimiento in MODERATE_GROWTH:
        weights["lockin_risk"] -= 0.02
        adjustments.append(WeightAdjustment(
            variable="lockin_risk", delta=-0.02,
            reason="Crecimiento estable o moderado — menor urgencia de portabilidad"
        ))

    # Normalizar para que la suma sea siempre exactamente 1.0
    total = sum(weights.values())
    weights = {k: round(v / total, 6) for k, v in weights.items()}

    return weights, adjustments


# ══════════════════════════════════════════════════════════════════════════════
# CÁLCULO DE TCO COMPLETO
# ══════════════════════════════════════════════════════════════════════════════

def calculate_full_tco(crm: CRMCandidate, profile: IntakeProfile) -> float:
    """
    TCO completo a 3 años:
      licencias (con incremento anual compuesto)
      + contactos extra (si el CRM cobra por contactos)
      + implementación (varía según perfil técnico)
      + formación inicial
      + migración (varía según sistema de origen)

    A diferencia del TCO rough de filter.py (solo licencias × 3),
    este incluye todos los costes estimados del ciclo de vida.
    """
    # Licencias con incremento anual compuesto
    y1 = crm.annual_license_eur
    rate = crm.annual_price_increase_pct          # fracción, ej: 0.07
    y2 = y1 * (1 + rate)
    y3 = y2 * (1 + rate)
    total_licenses = y1 + y2 + y3

    contacts_extra   = _calculate_contacts_cost(crm, profile)
    impl_cost        = _calculate_implementation_cost(crm, profile)
    training_cost    = _get_range_value(crm.training_cost_est_eur, "typical", 500)
    migration_cost   = _calculate_migration_cost(crm, profile)

    return total_licenses + contacts_extra + impl_cost + training_cost + migration_cost


def _calculate_contacts_cost(crm: CRMCandidate, profile: IntakeProfile) -> float:
    """
    Coste extra por contactos activos a lo largo de 3 años.
    Solo aplica si crm.price_per_contact = True.

    Busca en contact_tier_thresholds el tier correspondiente al nº de
    contactos estimados del intake, y multiplica por 36 meses.
    """
    if not crm.price_per_contact or not crm.contact_tier_thresholds:
        return 0.0

    contact_count = CLIENTS_TO_CONTACTS.get(profile.clientes, 300)
    monthly_extra = 0.0

    # Los tiers están ordenados por nº de contactos mínimos.
    # Buscar el mayor tier que el usuario supera.
    for limit_str, monthly_cost in sorted(
        crm.contact_tier_thresholds.items(), key=lambda x: int(x[0])
    ):
        if contact_count >= int(limit_str):
            monthly_extra = float(monthly_cost)
        else:
            break

    return monthly_extra * 36  # 36 meses = 3 años


def _calculate_implementation_cost(crm: CRMCandidate, profile: IntakeProfile) -> float:
    """
    Coste de implementación ajustado al perfil técnico del equipo.

    - IT completo y experimentado:  mínimo (lo hacen ellos)
    - IT parcial / no dedicado:     típico
    - Sin IT, con consultor:        máximo (honorarios externos)
    - Sin IT, sin consultor:        típico (lo hacen ellos, pero más lento)
    """
    ranges = crm.implementation_cost_est_eur

    if profile.equipo_tech in FULL_IT_PROFILES:
        if profile.it_experiencia == "Sí, tienen experiencia con implementaciones SaaS":
            return _get_range_value(ranges, "min", 0)
        return _get_range_value(ranges, "typical", 800)

    if profile.equipo_tech in NO_IT_PROFILES:
        if profile.consultor_presupuesto == "Sí, tenemos presupuesto para ello":
            return _get_range_value(ranges, "max", 3_000)
        return _get_range_value(ranges, "typical", 1_200)

    # Perfil técnico no dedicado o consultor sin confirmar presupuesto
    return _get_range_value(ranges, "typical", 800)


def _calculate_migration_cost(crm: CRMCandidate, profile: IntakeProfile) -> float:
    """
    Coste de migración según el sistema de origen declarado en el intake.
    Si el sistema de origen es 'nada / papel', la migración es 0.
    """
    source_key = MIGRATION_SOURCE_KEY.get(profile.sistema_actual, "from_excel")

    if source_key == "from_nothing":
        return 0.0

    cost = crm.migration_cost_est_eur.get(source_key)
    if cost is None:
        # Fallback: si no hay un valor específico para el origen, usar "typical"
        cost = _get_range_value(crm.migration_cost_est_eur, "typical", 300)

    return float(cost)


def _get_range_value(range_dict: dict, key: str, default: float) -> float:
    """Extrae un valor de un dict de rangos {min, typical, max}, con fallback."""
    if not range_dict:
        return default
    return float(range_dict.get(key, default))


# ══════════════════════════════════════════════════════════════════════════════
# CONVERSIÓN TCO → SCORE 0–10
# ══════════════════════════════════════════════════════════════════════════════

def tco_to_score(tco_3y: float, profile: IntakeProfile) -> float:
    """
    Convierte el TCO a 3 años en un score 0–10 relativo al presupuesto.

    Función piecewise linear sobre ratio = tco_3y / presupuesto_3y:
      ratio ≤ 0.5         → score = 10.0  (muy barato, amplio margen)
      0.5 < ratio ≤ 1.0   → score ∈ [7.0, 10.0]  (zona confortable)
      1.0 < ratio ≤ 1.5   → score ∈ [0.0,  7.0]  (zona de tensión)
      ratio > 1.5          → score = 0.0  (muy caro)

    Ejemplo:
      presupuesto = 5k/año → referencia 3y = 15k
      TCO = 6.8k  → ratio = 0.45  → score = 10.0
      TCO = 12k   → ratio = 0.80  → score = 8.2
      TCO = 20k   → ratio = 1.33  → score = 2.3
      TCO = 25k+  → ratio ≥ 1.5  → score = 0.0
    """
    annual_budget = BUDGET_ANNUAL_EUR.get(profile.presupuesto, 15_000)
    ref_3y = annual_budget * 3

    if ref_3y == 0:
        return 10.0

    ratio = tco_3y / ref_3y

    if ratio <= 0.5:
        return 10.0
    elif ratio <= 1.0:
        # Interpolación lineal: 10 en ratio=0.5, 7 en ratio=1.0
        return 7.0 + (1.0 - ratio) / 0.5 * 3.0
    elif ratio <= 1.5:
        # Interpolación lineal: 7 en ratio=1.0, 0 en ratio=1.5
        return (1.5 - ratio) / 0.5 * 7.0
    else:
        return 0.0


# ══════════════════════════════════════════════════════════════════════════════
# AJUSTES DINÁMICOS A SCORES INDIVIDUALES
# ══════════════════════════════════════════════════════════════════════════════

def _adjust_variable_scores(
    scores: dict[str, float],
    crm: CRMCandidate,
    profile: IntakeProfile,
    flags: List[AlertFlag],
) -> dict[str, float]:
    """
    Modifica los scores base de variables individuales según el perfil.
    Distinto de los ajustes de pesos — aquí cambia el score, no el peso.

    Reglas aplicadas:
      + Integración nativa con la suite del intake  → complejidad_impl +1.5
      - Sin IT + consultor recomendado              → complejidad_impl −2.0
      - Crecimiento rápido + precio impredecible    → lockin_risk −2.0
    """
    adjusted = dict(scores)

    # Bonus: integración nativa con la suite principal del equipo
    native = [t.lower() for t in crm.native_integrations]
    suite_match = False

    if "Google Workspace" in profile.suite:
        suite_match = any(k in native for k in ["gmail", "google_workspace", "google"])
    elif "Microsoft 365" in profile.suite:
        suite_match = any(k in native for k in ["outlook", "microsoft365", "teams"])

    if suite_match:
        adjusted["complejidad_impl"] = min(10.0, adjusted["complejidad_impl"] + 1.5)

    # Penalización: sin IT y el CRM requiere o recomienda consultor
    if (
        profile.equipo_tech in NO_IT_PROFILES
        and crm.requires_external_consultant in ("recommended", "required")
    ):
        adjusted["complejidad_impl"] = max(0.0, adjusted["complejidad_impl"] - 2.0)

    # Penalización: crecimiento rápido + política de precios impredecible
    if profile.crecimiento in FAST_GROWTH and crm.price_lock_policy == "unpredictable":
        adjusted["lockin_risk"] = max(0.0, adjusted["lockin_risk"] - 2.0)

    return adjusted


# ══════════════════════════════════════════════════════════════════════════════
# SCORING DE UN CRM INDIVIDUAL
# ══════════════════════════════════════════════════════════════════════════════

def _score_crm(
    crm: CRMCandidate,
    profile: IntakeProfile,
    weights: dict[str, float],
) -> Tuple[ScoredCRM, List[AlertFlag]]:
    """Calcula el score completo de un CRM individual y genera sus alertas."""
    flags: List[AlertFlag] = []

    # TCO completo y su conversión a score
    tco_3y = calculate_full_tco(crm, profile)
    t_score = tco_to_score(tco_3y, profile)

    # Alerta si el TCO supera el presupuesto base en más de un 20%
    annual_budget = BUDGET_ANNUAL_EUR.get(profile.presupuesto, 15_000)
    if tco_3y > annual_budget * 3 * 1.2:
        overage = ((tco_3y / (annual_budget * 3)) - 1) * 100
        flags.append(AlertFlag(
            code="tco_limite",
            crm_id=crm.crm_id,
            crm_name=crm.name,
            severity="warning",
            message=(
                f"El TCO estimado de {crm.name} a 3 años ({tco_3y:,.0f} €) "
                f"supera el presupuesto declarado en un {overage:.0f}%."
            ),
        ))

    # Scores base por variable (5.0 como fallback si no hay dato en Supabase)
    variable_scores: dict[str, float] = {
        "tco":               t_score,
        "curva_aprendizaje": crm.learning_curve_score              or 5.0,
        "complejidad_impl":  crm.implementation_complexity_score   or 5.0,
        "lockin_risk":       crm.lockin_risk_score                 or 5.0,
        "soporte":           crm.support_score                     or 5.0,
        "reviews":           crm.review_score                      or 5.0,
    }

    # Ajustes de scores individuales por perfil
    variable_scores = _adjust_variable_scores(variable_scores, crm, profile, flags)

    # Desglose ponderado
    breakdown: dict[str, ScoreDetail] = {
        var: ScoreDetail(
            raw_score=round(score, 2),
            weight=round(weights[var], 4),
            weighted_contribution=round(score * weights[var], 4),
        )
        for var, score in variable_scores.items()
    }

    # Score final 0–100
    final_score = round(
        sum(d.weighted_contribution for d in breakdown.values()) * 10, 1
    )

    # Alerta: consultor recomendado sin soporte técnico
    if (
        crm.requires_external_consultant in ("recommended", "required")
        and profile.equipo_tech in NO_IT_PROFILES
    ):
        flags.append(AlertFlag(
            code="consultor_recomendado",
            crm_id=crm.crm_id,
            crm_name=crm.name,
            severity="warning",
            message=(
                f"{crm.name} recomienda un consultor externo para la implementación. "
                "Considera añadir este coste al presupuesto total."
            ),
        ))

    # Alerta: variable débil en una dimensión con peso relevante
    for var, detail in breakdown.items():
        if detail.raw_score < 5.0 and detail.weight >= 0.13:
            flags.append(AlertFlag(
                code="variable_debil",
                crm_id=crm.crm_id,
                crm_name=crm.name,
                severity="info",
                message=(
                    f"{crm.name} tiene puntuación baja en '{var}' "
                    f"({detail.raw_score:.1f}/10), que tiene un peso del "
                    f"{detail.weight * 100:.0f}% en tu perfil."
                ),
            ))

    return ScoredCRM(
        crm_id=crm.crm_id,
        crm_name=crm.name,
        crm_category=crm.crm_category,
        final_score=final_score,
        tco_3y_eur=round(tco_3y, 2),
        tco_score=round(t_score, 2),
        score_breakdown=breakdown,
        flags=flags,
    ), flags


# ══════════════════════════════════════════════════════════════════════════════
# CONFIANZA GLOBAL DEL SCORING
# ══════════════════════════════════════════════════════════════════════════════

def _calculate_confidence(
    scored: List[ScoredCRM],
    all_flags: List[AlertFlag],
) -> str:
    """
    high:   todos los scores son datos reales de Supabase (sin fallback 5.0)
    medium: algún score usa el valor por defecto (dato ausente en Supabase)
    low:    múltiples scores por defecto o alertas de datos obsoletos
    """
    default_count = sum(
        1
        for crm in scored
        for detail in crm.score_breakdown.values()
        if detail.raw_score == 5.0
    )
    stale_flags = [f for f in all_flags if f.code == "pricing_stale"]

    if stale_flags or default_count > len(scored) * 2:
        return "low"
    elif default_count > 0:
        return "medium"
    return "high"
