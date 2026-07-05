"""
tests/test_scoring.py
─────────────────────
Tests unitarios para app/services/scoring.py.

Cubren: ajuste de pesos, cálculo de TCO, conversión TCO→score,
ajustes de scores individuales y el motor de ranking completo.

No requieren conexión a Supabase — todos los datos son fixtures en memoria.

Ejecutar:
  pytest tests/test_scoring.py -v
"""

import pytest
from app.models.crm import CRMCandidate
from app.models.intake import IntakeProfile
from app.services.filter import FilterOutput
from app.services.scoring import (
    score_and_rank,
    calculate_dynamic_weights,
    calculate_full_tco,
    tco_to_score,
    BASE_WEIGHTS,
)


# ══════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ══════════════════════════════════════════════════════════════════════════════

def make_crm(**overrides) -> CRMCandidate:
    """CRM base con datos completos y scores neutros (7.0 en todo)."""
    defaults = {
        "crm_id":                        "crm_test",
        "name":                          "CRM Test",
        "crm_category":                  "generalista",
        "best_fit_team_size_min":        1,
        "best_fit_team_size_max":        200,
        "gdpr_compliant":                True,
        "data_hosting_regions":          ["EU"],
        "avg_implementation_weeks":      4,
        "requires_external_consultant":  "optional",
        "annual_license_eur":            3_000,
        "annual_price_increase_pct":     0.07,
        "price_per_contact":             False,
        "contact_tier_thresholds":       None,
        "implementation_cost_est_eur":   {"min": 0, "typical": 800, "max": 3_000},
        "training_cost_est_eur":         {"min": 0, "typical": 500, "max": 1_500},
        "migration_cost_est_eur":        {"from_excel": 200, "from_crm": 1_000, "from_erp": 3_000},
        "price_lock_policy":             "annual_increase_capped",
        "learning_curve_score":          7.0,
        "implementation_complexity_score": 7.0,
        "lockin_risk_score":             7.0,
        "support_score":                 7.0,
        "review_score":                  7.0,
        "native_integrations":           ["gmail", "slack"],
        "scoring_confidence":            "high",
    }
    defaults.update(overrides)
    return CRMCandidate(**defaults)


def make_profile(**overrides) -> IntakeProfile:
    """Perfil base tech B2B, presupuesto medio, sin IT dedicado."""
    defaults = {
        "sector":            "Tecnología / SaaS",
        "modelo":            "B2B — ventas a empresas",
        "sistema_actual":    "Excel o Google Sheets",
        "presupuesto":       "5.000 – 15.000€/año",
        "presupuesto_flex":  "Hay algo de margen (+20–30%)",
        "empleados":         "26 – 50 personas",
        "usuarios_crm":      "6 – 15 usuarios",
        "suite":             "Google Workspace (Gmail, Drive, Calendar)",
        "tools":             ["Slack / Teams (mensajería)"],
        "equipo_tech":       "Hay alguien con perfil técnico pero no dedicado",
        "clientes":          "100 – 500",
        "crecimiento":       "Crecimiento moderado (+10–30%)",
    }
    defaults.update(overrides)
    return IntakeProfile(**defaults)


def make_filter_output(*crms) -> FilterOutput:
    """Simula la salida de filter.py con los CRMs dados como 'passed'."""
    return FilterOutput(passed=list(crms), excluded=[])


# ══════════════════════════════════════════════════════════════════════════════
# TESTS — PESOS DINÁMICOS
# ══════════════════════════════════════════════════════════════════════════════

class TestCalculateDynamicWeights:

    def test_pesos_base_sin_ajustes(self):
        """Sin señales especiales, los pesos deben ser los base normalizados."""
        profile = make_profile(
            equipo_tech="Hay alguien con perfil técnico pero no dedicado",
            presupuesto_flex="Hay algo de margen (+20–30%)",
            crecimiento="Crecimiento rápido (+30–100%)",
        )
        weights, adjustments = calculate_dynamic_weights(profile)

        assert adjustments == []
        assert abs(sum(weights.values()) - 1.0) < 1e-5
        assert abs(weights["tco"] - 0.25) < 0.01

    def test_ajuste_sin_it(self):
        """Sin IT: curva_aprendizaje sube, complejidad_impl sube."""
        profile = make_profile(equipo_tech="No, somos un equipo no técnico")
        weights, adjustments = calculate_dynamic_weights(profile)

        assert weights["curva_aprendizaje"] > BASE_WEIGHTS["curva_aprendizaje"]
        assert weights["complejidad_impl"]  > BASE_WEIGHTS["complejidad_impl"]
        assert len(adjustments) >= 2
        assert abs(sum(weights.values()) - 1.0) < 1e-5

    def test_ajuste_presupuesto_flexible(self):
        """Presupuesto flexible: tco baja."""
        profile = make_profile(presupuesto_flex="Flexible si el ROI está bien justificado")
        weights, adjustments = calculate_dynamic_weights(profile)

        assert weights["tco"] < BASE_WEIGHTS["tco"]
        assert any(a.variable == "tco" and a.delta < 0 for a in adjustments)

    def test_ajuste_crecimiento_moderado(self):
        """Crecimiento moderado: lockin_risk baja."""
        profile = make_profile(crecimiento="Crecimiento moderado (+10–30%)")
        weights, adjustments = calculate_dynamic_weights(profile)

        assert weights["lockin_risk"] < BASE_WEIGHTS["lockin_risk"]
        assert any(a.variable == "lockin_risk" and a.delta < 0 for a in adjustments)

    def test_pesos_siempre_suman_1(self):
        """Los pesos deben sumar 1.0 sin importar cuántos ajustes se apliquen."""
        profile = make_profile(
            equipo_tech="No, somos un equipo no técnico",
            presupuesto_flex="Flexible si el ROI está bien justificado",
            crecimiento="Crecimiento moderado (+10–30%)",
        )
        weights, _ = calculate_dynamic_weights(profile)
        assert abs(sum(weights.values()) - 1.0) < 1e-5

    def test_todos_los_pesos_son_positivos(self):
        """Ningún peso debe ser negativo después de los ajustes."""
        profile = make_profile(
            equipo_tech="No, somos un equipo no técnico",
            presupuesto_flex="Flexible si el ROI está bien justificado",
            crecimiento="Estable, sin cambios significativos",
        )
        weights, _ = calculate_dynamic_weights(profile)
        assert all(v > 0 for v in weights.values())


# ══════════════════════════════════════════════════════════════════════════════
# TESTS — CÁLCULO DE TCO COMPLETO
# ══════════════════════════════════════════════════════════════════════════════

class TestCalculateFullTCO:

    def test_tco_basico_sin_extras(self):
        """CRM simple sin contactos extra, con datos de implementación conocidos."""
        crm = make_crm(
            annual_license_eur=3_000,
            annual_price_increase_pct=0.0,   # sin incremento, para simplificar
            price_per_contact=False,
            implementation_cost_est_eur={"min": 0, "typical": 500, "max": 2_000},
            training_cost_est_eur={"typical": 500},
            migration_cost_est_eur={"from_excel": 200},
        )
        profile = make_profile(sistema_actual="Excel o Google Sheets")
        tco = calculate_full_tco(crm, profile)

        # Licencias: 3000 × 3 = 9000
        # Impl: 500 (typical, perfil técnico parcial)
        # Training: 500
        # Migration: 200
        # Total esperado: 10200
        assert tco == pytest.approx(10_200, rel=0.01)

    def test_incremento_anual_se_aplica(self):
        """El incremento de precio debe aplicarse de forma compuesta año a año."""
        crm = make_crm(
            annual_license_eur=10_000,
            annual_price_increase_pct=0.10,
            implementation_cost_est_eur={},
            training_cost_est_eur={},
            migration_cost_est_eur={},
        )
        profile = make_profile(sistema_actual="No usamos nada / papel")
        tco = calculate_full_tco(crm, profile)

        # y1=10000, y2=11000, y3=12100 → licenses=33100
        # impl=800 (default), training=500 (default), migration=0
        assert tco == pytest.approx(10_000 + 11_000 + 12_100 + 800 + 500, rel=0.01)

    def test_contactos_extra_se_suman(self):
        """Si el CRM cobra por contactos, debe sumarse al TCO."""
        crm = make_crm(
            annual_license_eur=5_000,
            annual_price_increase_pct=0.0,
            price_per_contact=True,
            contact_tier_thresholds={"1": 0, "201": 50},  # >200 contactos: +50€/mes
            implementation_cost_est_eur={},
            training_cost_est_eur={},
            migration_cost_est_eur={},
        )
        profile = make_profile(
            clientes="100 – 500",    # → 300 contactos estimados
            sistema_actual="No usamos nada / papel",
        )
        tco = calculate_full_tco(crm, profile)

        # Licencias: 5000 × 3 = 15000
        # Contactos: 50€/mes × 36 = 1800
        # Impl: 800 (default), Training: 500 (default), Migration: 0
        assert tco == pytest.approx(15_000 + 1_800 + 800 + 500, rel=0.01)

    def test_migracion_cero_sin_sistema_previo(self):
        """Si no había sistema previo, la migración debe ser 0."""
        crm = make_crm(
            annual_license_eur=3_000,
            annual_price_increase_pct=0.0,
            implementation_cost_est_eur={},
            training_cost_est_eur={},
            migration_cost_est_eur={"from_excel": 500, "from_crm": 1000},
        )
        profile = make_profile(sistema_actual="No usamos nada / papel")
        tco = calculate_full_tco(crm, profile)

        # No debe incluir coste de migración
        tco_con_migracion = calculate_full_tco(
            make_crm(
                annual_license_eur=3_000,
                annual_price_increase_pct=0.0,
                implementation_cost_est_eur={},
                training_cost_est_eur={},
                migration_cost_est_eur={"from_excel": 500},
            ),
            make_profile(sistema_actual="Excel o Google Sheets"),
        )
        assert tco < tco_con_migracion

    def test_implementacion_minima_con_it_experimentado(self):
        """Con IT experimentado, el coste de implementación debe ser el mínimo."""
        crm = make_crm(
            annual_license_eur=3_000,
            annual_price_increase_pct=0.0,
            implementation_cost_est_eur={"min": 0, "typical": 1_500, "max": 5_000},
            training_cost_est_eur={},
            migration_cost_est_eur={},
        )
        profile = make_profile(
            equipo_tech="Sí, tenemos IT / desarrollador interno",
            it_experiencia="Sí, tienen experiencia con implementaciones SaaS",
            sistema_actual="No usamos nada / papel",
        )
        tco = calculate_full_tco(crm, profile)

        # Impl mínima = 0, training default = 500, migration = 0
        # Licencias 3000 × 3 = 9000 → total = 9500
        assert tco == pytest.approx(9_500, rel=0.01)


# ══════════════════════════════════════════════════════════════════════════════
# TESTS — CONVERSIÓN TCO → SCORE
# ══════════════════════════════════════════════════════════════════════════════

class TestTCOToScore:
    """
    Presupuesto base del perfil: 5.000 – 15.000€/año → 15.000€
    Referencia 3y = 15.000 × 3 = 45.000€
    """

    def test_muy_barato_da_10(self):
        # TCO = 10k, ratio = 10k/45k = 0.22 → score = 10
        profile = make_profile(presupuesto="5.000 – 15.000€/año")
        assert tco_to_score(10_000, profile) == pytest.approx(10.0)

    def test_exactamente_en_presupuesto_da_7(self):
        # TCO = 45k, ratio = 1.0 → score = 7.0
        profile = make_profile(presupuesto="5.000 – 15.000€/año")
        assert tco_to_score(45_000, profile) == pytest.approx(7.0, abs=0.01)

    def test_ratio_0_75_score_entre_7_y_10(self):
        # ratio = 0.75 → score = 7 + (1.0-0.75)/0.5*3 = 7 + 1.5 = 8.5
        profile = make_profile(presupuesto="5.000 – 15.000€/año")
        tco = 45_000 * 0.75
        assert tco_to_score(tco, profile) == pytest.approx(8.5, abs=0.01)

    def test_muy_caro_da_0(self):
        # TCO = 90k, ratio = 2.0 → score = 0
        profile = make_profile(presupuesto="5.000 – 15.000€/año")
        assert tco_to_score(90_000, profile) == pytest.approx(0.0)

    def test_en_limite_1_5_da_0(self):
        # TCO = 67.5k, ratio = 1.5 → score = 0
        profile = make_profile(presupuesto="5.000 – 15.000€/año")
        assert tco_to_score(67_500, profile) == pytest.approx(0.0, abs=0.01)

    def test_score_nunca_negativo(self):
        profile = make_profile(presupuesto="Menos de 1.000€/año")
        assert tco_to_score(999_999, profile) >= 0.0

    def test_score_nunca_mayor_10(self):
        profile = make_profile(presupuesto="Más de 40.000€/año")
        assert tco_to_score(0, profile) <= 10.0


# ══════════════════════════════════════════════════════════════════════════════
# TESTS — MOTOR COMPLETO DE SCORING
# ══════════════════════════════════════════════════════════════════════════════

class TestScoreAndRank:

    def test_un_crm_recibe_rank_1(self):
        crm = make_crm()
        profile = make_profile()
        result = score_and_rank(make_filter_output(crm), profile)

        assert len(result.ranked_crms) == 1
        assert result.ranked_crms[0].rank == 1

    def test_mejor_score_recibe_rank_1(self):
        """El CRM con mayor score debe quedar en posición 1."""
        crm_barato = make_crm(crm_id="barato", annual_license_eur=500)
        crm_caro   = make_crm(crm_id="caro",   annual_license_eur=20_000)
        profile    = make_profile(presupuesto="5.000 – 15.000€/año")

        result = score_and_rank(make_filter_output(crm_barato, crm_caro), profile)

        assert result.ranked_crms[0].crm_id == "barato"
        assert result.ranked_crms[1].crm_id == "caro"

    def test_ranking_correlativo_sin_huecos(self):
        crms = [make_crm(crm_id=f"crm_{i}") for i in range(4)]
        result = score_and_rank(make_filter_output(*crms), make_profile())
        ranks = [c.rank for c in result.ranked_crms]
        assert ranks == [1, 2, 3, 4]

    def test_score_final_en_rango_0_100(self):
        crm = make_crm()
        result = score_and_rank(make_filter_output(crm), make_profile())
        score = result.ranked_crms[0].final_score
        assert 0 <= score <= 100

    def test_winner_y_runner_up_properties(self):
        crm_a = make_crm(crm_id="a", annual_license_eur=1_000)
        crm_b = make_crm(crm_id="b", annual_license_eur=15_000)
        result = score_and_rank(make_filter_output(crm_a, crm_b), make_profile())

        assert result.winner is not None
        assert result.runner_up is not None
        assert result.winner.rank == 1
        assert result.runner_up.rank == 2

    def test_sin_candidatos_lanza_error(self):
        empty = FilterOutput(passed=[], excluded=[])
        with pytest.raises(ValueError, match="No hay CRMs candidatos"):
            score_and_rank(empty, make_profile())

    def test_desglose_tiene_6_variables(self):
        crm = make_crm()
        result = score_and_rank(make_filter_output(crm), make_profile())
        breakdown = result.ranked_crms[0].score_breakdown
        assert set(breakdown.keys()) == {
            "tco", "curva_aprendizaje", "complejidad_impl",
            "lockin_risk", "soporte", "reviews"
        }

    def test_suma_contribuciones_ponderadas(self):
        """La suma de weighted_contribution × 10 debe coincidir con final_score."""
        crm = make_crm()
        result = score_and_rank(make_filter_output(crm), make_profile())
        scored = result.ranked_crms[0]
        expected = round(
            sum(d.weighted_contribution for d in scored.score_breakdown.values()) * 10, 1
        )
        assert scored.final_score == pytest.approx(expected, abs=0.1)

    def test_alerta_tco_limite_generada(self):
        """Si el TCO supera el presupuesto en >20%, debe generarse alerta tco_limite."""
        crm = make_crm(
            annual_license_eur=30_000,
            annual_price_increase_pct=0.0,
            implementation_cost_est_eur={},
            training_cost_est_eur={},
            migration_cost_est_eur={},
        )
        # presupuesto: 5k/año → 3y ref = 15k. TCO licencias = 90k. >> 15k*1.2
        profile = make_profile(
            presupuesto="1.000 – 5.000€/año",
            sistema_actual="No usamos nada / papel",
        )
        result = score_and_rank(make_filter_output(crm), profile)
        codes = [f.code for f in result.all_flags]
        assert "tco_limite" in codes

    def test_alerta_consultor_recomendado(self):
        """Sin IT y CRM que recomienda consultor → alerta consultor_recomendado."""
        crm = make_crm(requires_external_consultant="recommended")
        profile = make_profile(equipo_tech="No, somos un equipo no técnico")
        result = score_and_rank(make_filter_output(crm), profile)
        codes = [f.code for f in result.all_flags]
        assert "consultor_recomendado" in codes

    def test_confianza_high_con_datos_completos(self):
        """Con todos los scores presentes en Supabase, la confianza debe ser 'high'."""
        crm = make_crm(
            learning_curve_score=8.0,
            implementation_complexity_score=7.5,
            lockin_risk_score=8.5,
            support_score=7.0,
            review_score=7.8,
        )
        result = score_and_rank(make_filter_output(crm), make_profile())
        assert result.scoring_confidence == "high"

    def test_confianza_medium_con_score_nulo(self):
        """Si algún score es None (y se usa 5.0 como fallback), confianza = 'medium'."""
        crm = make_crm(review_score=None)
        result = score_and_rank(make_filter_output(crm), make_profile())
        assert result.scoring_confidence in ("medium", "low")

    def test_ajuste_suite_google_workspace(self):
        """Integración nativa con Google Workspace debe mejorar complejidad_impl."""
        crm_con = make_crm(native_integrations=["gmail", "google_workspace"])
        crm_sin = make_crm(native_integrations=["outlook"])
        profile  = make_profile(suite="Google Workspace (Gmail, Drive, Calendar)")

        result_con = score_and_rank(make_filter_output(crm_con), profile)
        result_sin = score_and_rank(make_filter_output(crm_sin), profile)

        impl_con = result_con.ranked_crms[0].score_breakdown["complejidad_impl"].raw_score
        impl_sin = result_sin.ranked_crms[0].score_breakdown["complejidad_impl"].raw_score

        assert impl_con > impl_sin
