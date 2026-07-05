"""
app/services/filter.py
──────────────────────
Motor de filtros de exclusión duros.

Los filtros corren ANTES del scoring y no afectan puntuaciones —
simplemente eliminan CRMs del conjunto de evaluación.
Un CRM excluido aquí aparece en el informe como "opción futura"
con la razón legible de exclusión.

Orden de ejecución (del más barato al más caro computacionalmente):
  F01 — Límite de usuarios del plan        (comparación simple)
  F02 — GDPR y hosting EU en regulados     (comparación simple)
  F03 — Complejidad sin soporte técnico    (comparación simple)
  F04 — TCO de licencias vs presupuesto    (cálculo)

Un CRM excluido en F01 no llega a F02. Esto ahorra ~40% de
evaluaciones en perfiles con muchos CRMs fuera de rango de usuarios.
"""

from typing import List, Optional
from pydantic import BaseModel

from app.models.crm import CRMCandidate
from app.models.intake import IntakeProfile


# ══════════════════════════════════════════════════════════════════════════════
# TABLAS DE MAPPING — intake strings → valores numéricos
# Los strings coinciden exactamente con las opciones del formulario.
# ══════════════════════════════════════════════════════════════════════════════

# Presupuesto anual → límite superior en EUR
# Usamos el límite SUPERIOR del rango (postura conservadora: la empresa
# realmente no quiere gastar más que ese número).
BUDGET_UPPER_EUR: dict[str, Optional[float]] = {
    "Menos de 1.000€/año":        1_000,
    "1.000 – 5.000€/año":         5_000,
    "5.000 – 15.000€/año":       15_000,
    "15.000 – 40.000€/año":      40_000,
    "Más de 40.000€/año":          None,   # sin límite práctico
    "No tenemos límite definido":  None,   # sin filtro
}

# Flexibilidad presupuestaria → multiplicador sobre el presupuesto anual
# Se aplica al TCO de 3 años total.
FLEX_MULTIPLIER: dict[str, float] = {
    "Límite rígido, no podemos superarlo":      1.0,
    "Hay algo de margen (+20–30%)":             1.3,
    "Flexible si el ROI está bien justificado": 1.5,
}

# Usuarios CRM declarados → límite superior del rango
# Usado para comparar contra best_fit_team_size_max del CRM.
USERS_UPPER: dict[str, int] = {
    "1 – 5 usuarios":     5,
    "6 – 15 usuarios":   15,
    "16 – 30 usuarios":  30,
    "31 – 60 usuarios":  60,
    "Más de 60 usuarios": 100,   # estimación conservadora para el filtro
}

# Sectores que exigen GDPR verificado + hosting en EU
REGULATED_SECTORS = {
    "Salud / Farma",
    "Finanzas / Seguros",
}

# Perfiles de equipo sin ninguna capacidad técnica interna
NO_IT_PROFILES = {
    "No, somos un equipo no técnico",
}

# Respuestas que indican que no hay presupuesto para consultor externo
NO_CONSULTANT_BUDGET = {
    "No está contemplado en el presupuesto que indicamos",
}

# Semanas máximas de implementación aceptables sin ningún soporte técnico
MAX_IMPL_WEEKS_NO_IT = 12


# ══════════════════════════════════════════════════════════════════════════════
# MODELOS DE OUTPUT
# ══════════════════════════════════════════════════════════════════════════════

class ExclusionResult(BaseModel):
    """Registro de un CRM excluido, con razón legible para el informe."""
    crm_id: str
    crm_name: str
    filter_code: str        # F01 | F02 | F03 | F04
    reason: str             # texto en español para el informe final
    tco_rough_eur: Optional[float] = None   # solo F04


class FilterOutput(BaseModel):
    """Resultado completo del motor de filtros."""
    passed: List[CRMCandidate]
    excluded: List[ExclusionResult]

    @property
    def passed_count(self) -> int:
        return len(self.passed)

    @property
    def excluded_count(self) -> int:
        return len(self.excluded)

    def summary(self) -> str:
        return (
            f"{self.passed_count} CRMs pasan al scoring · "
            f"{self.excluded_count} excluidos"
        )


# ══════════════════════════════════════════════════════════════════════════════
# MOTOR PRINCIPAL
# ══════════════════════════════════════════════════════════════════════════════

def apply_hard_filters(
    profile: IntakeProfile,
    candidates: List[CRMCandidate],
) -> FilterOutput:
    """
    Aplica los 4 filtros de exclusión duros en cascada.

    Args:
        profile:    Perfil de empresa del intake.
        candidates: Lista de CRMs cargados desde Supabase por retrieval.py.

    Returns:
        FilterOutput con los CRMs que pasan y los que fueron excluidos.

    Raises:
        ValueError: Si la lista de candidatos está vacía.
    """
    if not candidates:
        raise ValueError("La lista de CRMs candidatos está vacía.")

    passed: List[CRMCandidate] = []
    excluded: List[ExclusionResult] = []

    for crm in candidates:
        # Los filtros se evalúan en cortocircuito:
        # el primero que retorna una ExclusionResult detiene la cadena.
        exclusion = (
            _filter_users(crm, profile)            # F01
            or _filter_gdpr(crm, profile)          # F02
            or _filter_implementation(crm, profile) # F03
            or _filter_budget(crm, profile)        # F04
        )

        if exclusion:
            excluded.append(exclusion)
        else:
            passed.append(crm)

    return FilterOutput(passed=passed, excluded=excluded)


# ══════════════════════════════════════════════════════════════════════════════
# F01 — LÍMITE DE USUARIOS DEL PLAN
# ══════════════════════════════════════════════════════════════════════════════

def _filter_users(
    crm: CRMCandidate,
    profile: IntakeProfile,
) -> Optional[ExclusionResult]:
    """
    Excluye si el CRM no puede dar servicio al número de usuarios del equipo.

    Compara el límite superior del rango declarado en el intake
    contra best_fit_team_size_max del CRM en Supabase.

    Ejemplo:
      intake.usuarios_crm = "16 – 30 usuarios" → necesita soportar 30 usuarios
      crm.best_fit_team_size_max = 25 → EXCLUIDO
    """
    intake_users = USERS_UPPER.get(profile.usuarios_crm, 0)

    if crm.best_fit_team_size_max < intake_users:
        return ExclusionResult(
            crm_id=crm.crm_id,
            crm_name=crm.name,
            filter_code="F01",
            reason=(
                f"{crm.name} está diseñado para equipos de hasta "
                f"{crm.best_fit_team_size_max} usuarios. "
                f"Tu equipo necesita soporte para {intake_users}. "
                f"Este CRM quedaría pequeño desde el inicio."
            ),
        )
    return None


# ══════════════════════════════════════════════════════════════════════════════
# F02 — GDPR Y HOSTING EU EN SECTORES REGULADOS
# ══════════════════════════════════════════════════════════════════════════════

def _filter_gdpr(
    crm: CRMCandidate,
    profile: IntakeProfile,
) -> Optional[ExclusionResult]:
    """
    En sectores regulados (Salud / Farma, Finanzas / Seguros) exige:
      1. gdpr_compliant = True  (certificación GDPR real, no solo hosting EU)
      2. "EU" en data_hosting_regions  (los datos no pueden salir de Europa)

    En sectores no regulados este filtro no aplica.
    """
    if profile.sector not in REGULATED_SECTORS:
        return None

    has_eu_hosting = "EU" in crm.data_hosting_regions
    issues = []

    if not crm.gdpr_compliant:
        issues.append("no tiene certificación GDPR verificada")
    if not has_eu_hosting:
        issues.append("no ofrece alojamiento de datos en la Unión Europea")

    if issues:
        return ExclusionResult(
            crm_id=crm.crm_id,
            crm_name=crm.name,
            filter_code="F02",
            reason=(
                f"{crm.name} no cumple los requisitos de compliance "
                f"para el sector {profile.sector}: "
                + " y ".join(issues) + ". "
                "Este CRM puede ser evaluado si tu empresa opera "
                "fuera de sectores regulados."
            ),
        )
    return None


# ══════════════════════════════════════════════════════════════════════════════
# F03 — COMPLEJIDAD DE IMPLEMENTACIÓN SIN SOPORTE TÉCNICO
# ══════════════════════════════════════════════════════════════════════════════

def _filter_implementation(
    crm: CRMCandidate,
    profile: IntakeProfile,
) -> Optional[ExclusionResult]:
    """
    Excluye si el equipo no tiene capacidad técnica ni presupuesto para
    consultor externo, Y el CRM requiere una implementación compleja.

    Se activa solo cuando se cumplen LAS DOS condiciones de perfil:
      - equipo_tech = "No, somos un equipo no técnico"
      - consultor_presupuesto = "No está contemplado..."

    Un CRM es demasiado complejo si:
      - avg_implementation_weeks > 12  (más de 3 meses)
      - O requires_external_consultant = "required"

    Si el equipo tiene IT (aunque sea parcial) o puede pagar un consultor,
    este filtro no aplica — la complejidad se gestiona y se refleja
    en el score de complejidad, no en una exclusión.
    """
    no_it = profile.equipo_tech in NO_IT_PROFILES
    no_consultant = (
        profile.consultor_presupuesto in NO_CONSULTANT_BUDGET
        if profile.consultor_presupuesto is not None
        else profile.equipo_tech in NO_IT_PROFILES
        # Si el perfil es no técnico pero no hay campo de consultor,
        # asumimos que tampoco hay presupuesto para consultor.
    )

    if not (no_it and no_consultant):
        return None

    too_long = crm.avg_implementation_weeks > MAX_IMPL_WEEKS_NO_IT
    needs_consultant = crm.requires_external_consultant == "required"

    if too_long or needs_consultant:
        issues = []
        if too_long:
            issues.append(
                f"implementación típica de {crm.avg_implementation_weeks:.0f} semanas "
                f"(máximo viable sin soporte técnico: {MAX_IMPL_WEEKS_NO_IT})"
            )
        if needs_consultant:
            issues.append("requiere consultor externo de forma obligatoria")

        return ExclusionResult(
            crm_id=crm.crm_id,
            crm_name=crm.name,
            filter_code="F03",
            reason=(
                f"{crm.name} no es viable para un equipo sin perfil técnico "
                f"ni soporte externo: "
                + " y ".join(issues) + ". "
                "Reconsiderarlo si en el futuro incorporáis un perfil técnico "
                "o contratáis un partner de implementación."
            ),
        )
    return None


# ══════════════════════════════════════════════════════════════════════════════
# F04 — TCO DE LICENCIAS VS PRESUPUESTO
# ══════════════════════════════════════════════════════════════════════════════

def _filter_budget(
    crm: CRMCandidate,
    profile: IntakeProfile,
) -> Optional[ExclusionResult]:
    """
    Compara el TCO rough de licencias (3 años) contra el presupuesto
    disponible ajustado por la flexibilidad declarada.

    TCO rough = annual_license_eur × 3
    (calculado por retrieval.py para el nº exacto de usuarios del intake)

    Este filtro usa SOLO el coste de licencias, no el TCO completo.
    El razonamiento: si solo las licencias ya superan el umbral,
    el TCO completo (con implementación, formación, migración) lo
    superará con seguridad. El TCO completo se calcula en scoring.py
    para los CRMs que sí pasan este filtro.

    Umbral = presupuesto_anual × 3 años × flex_multiplier
      - Límite rígido:  × 1.0  (cero margen)
      - Algo de margen: × 1.3  (+30%)
      - Flexible:       × 1.5  (+50%)

    Si el presupuesto es "No tenemos límite definido" o
    "Más de 40.000€/año", no se aplica filtro.
    """
    annual_budget = BUDGET_UPPER_EUR.get(profile.presupuesto)

    if annual_budget is None:
        return None   # Sin límite definido — todos los CRMs pasan este filtro

    multiplier = FLEX_MULTIPLIER.get(profile.presupuesto_flex, 1.3)
    budget_ceiling_3y = annual_budget * 3 * multiplier

    tco_rough_3y = crm.annual_license_eur * 3

    if tco_rough_3y > budget_ceiling_3y:
        overage_pct = ((tco_rough_3y / budget_ceiling_3y) - 1) * 100
        return ExclusionResult(
            crm_id=crm.crm_id,
            crm_name=crm.name,
            filter_code="F04",
            tco_rough_eur=round(tco_rough_3y, 2),
            reason=(
                f"{crm.name} supera el presupuesto disponible. "
                f"Solo el coste de licencias a 3 años asciende a "
                f"{tco_rough_3y:,.0f} €, un {overage_pct:.0f}% por encima "
                f"del techo aplicado ({budget_ceiling_3y:,.0f} €). "
                f"Puede ser una opción viable si el presupuesto crece "
                f"o si evaluáis un plan de menor escala."
            ),
        )
    return None
