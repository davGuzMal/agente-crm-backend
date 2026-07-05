"""
tests/test_filter.py
────────────────────
Tests unitarios para app/services/filter.py.

Cubren los 4 filtros + casos borde importantes.
No requieren conexión a Supabase — usan fixtures de CRMCandidate en memoria.

Ejecutar:
  pytest tests/test_filter.py -v
"""

import pytest
from app.models.crm import CRMCandidate
from app.models.intake import IntakeProfile
from app.services.filter import apply_hard_filters, FilterOutput


# ══════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ══════════════════════════════════════════════════════════════════════════════

def make_crm(**overrides) -> CRMCandidate:
    """CRM base que pasa todos los filtros. Sobrescribir campos para testear."""
    defaults = {
        "crm_id": "test_001",
        "name": "CRM Test",
        "crm_category": "generalista",
        "best_fit_team_size_min": 1,
        "best_fit_team_size_max": 200,       # amplio — pasa F01
        "gdpr_compliant": True,              # pasa F02
        "data_hosting_regions": ["EU", "US"],# pasa F02
        "avg_implementation_weeks": 4,       # pasa F03
        "requires_external_consultant": "optional",  # pasa F03
        "annual_license_eur": 2_000,         # pasa F04 con presupuesto 5k/año
        "learning_curve_score": 7.5,
        "implementation_complexity_score": 7.0,
        "lockin_risk_score": 8.0,
        "support_score": 7.0,
        "review_score": 7.5,
        "native_integrations": ["gmail", "slack"],
        "scoring_confidence": "high",
    }
    defaults.update(overrides)
    return CRMCandidate(**defaults)


def make_profile(**overrides) -> IntakeProfile:
    """Perfil base tech B2B que pasa todos los filtros."""
    defaults = {
        "sector": "Tecnología / SaaS",
        "modelo": "B2B — ventas a empresas",
        "sistema_actual": "Excel o Google Sheets",
        "presupuesto": "5.000 – 15.000€/año",
        "presupuesto_flex": "Hay algo de margen (+20–30%)",
        "empleados": "26 – 50 personas",
        "usuarios_crm": "6 – 15 usuarios",     # → 15 usuarios max
        "suite": "Google Workspace (Gmail, Drive, Calendar)",
        "tools": ["Slack / Teams (mensajería)"],
        "equipo_tech": "Hay alguien con perfil técnico pero no dedicado",
        "clientes": "100 – 500",
        "crecimiento": "Crecimiento moderado (+10–30%)",
    }
    defaults.update(overrides)
    return IntakeProfile(**defaults)


# ══════════════════════════════════════════════════════════════════════════════
# TESTS — CASO BASE: TODO PASA
# ══════════════════════════════════════════════════════════════════════════════

class TestBaseCase:

    def test_crm_pasa_todos_los_filtros(self):
        crm = make_crm()
        profile = make_profile()
        result = apply_hard_filters(profile, [crm])

        assert result.passed_count == 1
        assert result.excluded_count == 0
        assert result.passed[0].crm_id == "test_001"

    def test_multiples_crms_todos_pasan(self):
        crms = [
            make_crm(crm_id="crm_001", name="CRM A"),
            make_crm(crm_id="crm_002", name="CRM B"),
            make_crm(crm_id="crm_003", name="CRM C"),
        ]
        profile = make_profile()
        result = apply_hard_filters(profile, crms)

        assert result.passed_count == 3
        assert result.excluded_count == 0

    def test_lista_vacia_lanza_error(self):
        profile = make_profile()
        with pytest.raises(ValueError, match="vacía"):
            apply_hard_filters(profile, [])


# ══════════════════════════════════════════════════════════════════════════════
# TESTS — F01: LÍMITE DE USUARIOS
# ══════════════════════════════════════════════════════════════════════════════

class TestF01Usuarios:

    def test_excluye_si_crm_tiene_menos_usuarios_que_el_equipo(self):
        # Equipo necesita 15 usuarios, CRM soporta solo 10
        crm = make_crm(best_fit_team_size_max=10)
        profile = make_profile(usuarios_crm="6 – 15 usuarios")  # → necesita 15
        result = apply_hard_filters(profile, [crm])

        assert result.excluded_count == 1
        assert result.excluded[0].filter_code == "F01"

    def test_pasa_si_crm_cubre_exactamente_el_numero(self):
        # Equipo necesita 15, CRM soporta exactamente 15
        crm = make_crm(best_fit_team_size_max=15)
        profile = make_profile(usuarios_crm="6 – 15 usuarios")
        result = apply_hard_filters(profile, [crm])

        assert result.passed_count == 1

    def test_pasa_si_crm_tiene_holgura(self):
        crm = make_crm(best_fit_team_size_max=200)
        profile = make_profile(usuarios_crm="31 – 60 usuarios")  # → 60 usuarios
        result = apply_hard_filters(profile, [crm])

        assert result.passed_count == 1

    def test_excluye_con_equipo_muy_grande(self):
        # "Más de 60 usuarios" → 100 usuarios en el mapper
        crm = make_crm(best_fit_team_size_max=50)
        profile = make_profile(usuarios_crm="Más de 60 usuarios")
        result = apply_hard_filters(profile, [crm])

        assert result.excluded_count == 1
        assert "60" in result.excluded[0].reason or "100" in result.excluded[0].reason


# ══════════════════════════════════════════════════════════════════════════════
# TESTS — F02: GDPR Y HOSTING EU
# ══════════════════════════════════════════════════════════════════════════════

class TestF02GDPR:

    def test_no_aplica_en_sector_no_regulado(self):
        # Un CRM sin GDPR ni EU hosting pasa si el sector no es regulado
        crm = make_crm(gdpr_compliant=False, data_hosting_regions=["US"])
        profile = make_profile(sector="Retail / eCommerce")
        result = apply_hard_filters(profile, [crm])

        assert result.passed_count == 1

    def test_excluye_sin_gdpr_en_salud(self):
        crm = make_crm(gdpr_compliant=False, data_hosting_regions=["EU"])
        profile = make_profile(sector="Salud / Farma")
        result = apply_hard_filters(profile, [crm])

        assert result.excluded_count == 1
        assert result.excluded[0].filter_code == "F02"
        assert "GDPR" in result.excluded[0].reason

    def test_excluye_sin_eu_hosting_en_finanzas(self):
        crm = make_crm(gdpr_compliant=True, data_hosting_regions=["US", "APAC"])
        profile = make_profile(sector="Finanzas / Seguros")
        result = apply_hard_filters(profile, [crm])

        assert result.excluded_count == 1
        assert result.excluded[0].filter_code == "F02"

    def test_excluye_sin_gdpr_ni_eu_en_sector_regulado(self):
        # Ambos problemas → ambos deben aparecer en la razón
        crm = make_crm(gdpr_compliant=False, data_hosting_regions=["US"])
        profile = make_profile(sector="Salud / Farma")
        result = apply_hard_filters(profile, [crm])

        assert result.excluded_count == 1
        reason = result.excluded[0].reason
        assert "GDPR" in reason
        assert "Unión Europea" in reason

    def test_pasa_con_gdpr_y_eu_en_sector_regulado(self):
        crm = make_crm(gdpr_compliant=True, data_hosting_regions=["EU"])
        profile = make_profile(sector="Finanzas / Seguros")
        result = apply_hard_filters(profile, [crm])

        assert result.passed_count == 1


# ══════════════════════════════════════════════════════════════════════════════
# TESTS — F03: COMPLEJIDAD SIN SOPORTE TÉCNICO
# ══════════════════════════════════════════════════════════════════════════════

class TestF03Implementacion:

    def test_no_aplica_si_tiene_it(self):
        # Con IT interno, no importa la complejidad del CRM
        crm = make_crm(avg_implementation_weeks=20, requires_external_consultant="required")
        profile = make_profile(equipo_tech="Sí, tenemos IT / desarrollador interno")
        result = apply_hard_filters(profile, [crm])

        assert result.passed_count == 1

    def test_no_aplica_si_puede_pagar_consultor(self):
        # Equipo no técnico pero con presupuesto para consultor
        crm = make_crm(avg_implementation_weeks=20, requires_external_consultant="required")
        profile = make_profile(
            equipo_tech="Podemos contratar un consultor externo",
            consultor_presupuesto="Sí, tenemos presupuesto para ello",
        )
        result = apply_hard_filters(profile, [crm])

        assert result.passed_count == 1

    def test_excluye_implementacion_larga_sin_it_ni_consultor(self):
        crm = make_crm(avg_implementation_weeks=16)  # > 12 semanas
        profile = make_profile(
            equipo_tech="No, somos un equipo no técnico",
            consultor_presupuesto="No está contemplado en el presupuesto que indicamos",
        )
        result = apply_hard_filters(profile, [crm])

        assert result.excluded_count == 1
        assert result.excluded[0].filter_code == "F03"
        assert "16" in result.excluded[0].reason

    def test_excluye_consultor_obligatorio_sin_soporte(self):
        crm = make_crm(
            avg_implementation_weeks=8,              # dentro del límite
            requires_external_consultant="required", # pero requiere consultor
        )
        profile = make_profile(
            equipo_tech="No, somos un equipo no técnico",
            consultor_presupuesto="No está contemplado en el presupuesto que indicamos",
        )
        result = apply_hard_filters(profile, [crm])

        assert result.excluded_count == 1
        assert result.excluded[0].filter_code == "F03"

    def test_pasa_implementacion_rapida_sin_it(self):
        # 4 semanas, sin consultor obligatorio → pasa aunque no tenga IT
        crm = make_crm(avg_implementation_weeks=4, requires_external_consultant="optional")
        profile = make_profile(
            equipo_tech="No, somos un equipo no técnico",
            consultor_presupuesto="No está contemplado en el presupuesto que indicamos",
        )
        result = apply_hard_filters(profile, [crm])

        assert result.passed_count == 1

    def test_pasa_exactamente_en_el_limite(self):
        # Exactamente 12 semanas → debe pasar (el filtro es estrictamente >)
        crm = make_crm(avg_implementation_weeks=12, requires_external_consultant="optional")
        profile = make_profile(
            equipo_tech="No, somos un equipo no técnico",
            consultor_presupuesto="No está contemplado en el presupuesto que indicamos",
        )
        result = apply_hard_filters(profile, [crm])

        assert result.passed_count == 1


# ══════════════════════════════════════════════════════════════════════════════
# TESTS — F04: TCO DE LICENCIAS VS PRESUPUESTO
# ══════════════════════════════════════════════════════════════════════════════

class TestF04Presupuesto:

    def test_no_aplica_con_presupuesto_ilimitado(self):
        crm = make_crm(annual_license_eur=50_000)
        profile = make_profile(presupuesto="No tenemos límite definido")
        result = apply_hard_filters(profile, [crm])

        assert result.passed_count == 1

    def test_no_aplica_con_mas_de_40k(self):
        crm = make_crm(annual_license_eur=50_000)
        profile = make_profile(presupuesto="Más de 40.000€/año")
        result = apply_hard_filters(profile, [crm])

        assert result.passed_count == 1

    def test_excluye_si_licencias_superan_techo_rigido(self):
        # Presupuesto: 5k/año → techo = 5000 × 3 × 1.0 = 15.000€
        # CRM licencias: 6k/año → TCO rough = 18.000€ > 15.000€ → EXCLUIDO
        crm = make_crm(annual_license_eur=6_000)
        profile = make_profile(
            presupuesto="1.000 – 5.000€/año",
            presupuesto_flex="Límite rígido, no podemos superarlo",
        )
        result = apply_hard_filters(profile, [crm])

        assert result.excluded_count == 1
        assert result.excluded[0].filter_code == "F04"
        assert result.excluded[0].tco_rough_eur == 18_000

    def test_pasa_con_margen_del_30(self):
        # Presupuesto: 5k/año → techo = 5000 × 3 × 1.3 = 19.500€
        # CRM licencias: 6k/año → TCO rough = 18.000€ < 19.500€ → PASA
        crm = make_crm(annual_license_eur=6_000)
        profile = make_profile(
            presupuesto="1.000 – 5.000€/año",
            presupuesto_flex="Hay algo de margen (+20–30%)",
        )
        result = apply_hard_filters(profile, [crm])

        assert result.passed_count == 1

    def test_excluye_con_margen_pero_muy_caro(self):
        # Presupuesto: 5k/año → techo = 5000 × 3 × 1.3 = 19.500€
        # CRM licencias: 10k/año → TCO rough = 30.000€ > 19.500€ → EXCLUIDO
        crm = make_crm(annual_license_eur=10_000)
        profile = make_profile(
            presupuesto="1.000 – 5.000€/año",
            presupuesto_flex="Hay algo de margen (+20–30%)",
        )
        result = apply_hard_filters(profile, [crm])

        assert result.excluded_count == 1

    def test_razon_incluye_porcentaje_de_exceso(self):
        crm = make_crm(annual_license_eur=10_000)
        profile = make_profile(
            presupuesto="1.000 – 5.000€/año",
            presupuesto_flex="Límite rígido, no podemos superarlo",
        )
        result = apply_hard_filters(profile, [crm])
        # tco_rough=30k, ceiling=15k → overage 100%
        assert "100%" in result.excluded[0].reason


# ══════════════════════════════════════════════════════════════════════════════
# TESTS — COMPORTAMIENTO DEL MOTOR COMPLETO
# ══════════════════════════════════════════════════════════════════════════════

class TestMotorCompleto:

    def test_cortocircuito_f01_no_evalua_f02(self):
        """Un CRM excluido en F01 debe tener filter_code F01, no F02."""
        crm = make_crm(
            best_fit_team_size_max=5,   # falla F01
            gdpr_compliant=False,       # también fallaría F02
        )
        profile = make_profile(
            usuarios_crm="16 – 30 usuarios",
            sector="Salud / Farma",
        )
        result = apply_hard_filters(profile, [crm])

        assert result.excluded[0].filter_code == "F01"  # no F02

    def test_mezcla_de_crms_pasan_y_excluidos(self):
        crms = [
            make_crm(crm_id="pasa_001", name="CRM Barato", annual_license_eur=1_000),
            make_crm(crm_id="excluido_001", name="CRM Caro", annual_license_eur=20_000),
            make_crm(crm_id="pasa_002", name="CRM Medio", annual_license_eur=3_000),
        ]
        profile = make_profile(
            presupuesto="5.000 – 15.000€/año",   # techo con margen: 58.500€
            presupuesto_flex="Límite rígido, no podemos superarlo",  # techo rígido: 45.000€
        )
        # CRM Caro: 20.000 × 3 = 60.000 > 45.000 → excluido
        result = apply_hard_filters(profile, crms)

        passed_ids = {c.crm_id for c in result.passed}
        excluded_ids = {e.crm_id for e in result.excluded}

        assert "pasa_001" in passed_ids
        assert "pasa_002" in passed_ids
        assert "excluido_001" in excluded_ids

    def test_summary_string(self):
        crms = [
            make_crm(crm_id="a", annual_license_eur=1_000),
            make_crm(crm_id="b", annual_license_eur=50_000),
        ]
        profile = make_profile(
            presupuesto="1.000 – 5.000€/año",
            presupuesto_flex="Límite rígido, no podemos superarlo",
        )
        result = apply_hard_filters(profile, crms)
        summary = result.summary()

        assert "1 CRMs pasan" in summary
        assert "1 excluidos" in summary
