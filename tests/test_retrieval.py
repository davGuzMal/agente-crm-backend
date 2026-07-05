"""
tests/test_retrieval.py
───────────────────────
Tests unitarios para app/services/retrieval.py.

Las funciones puras (_select_best_plan, _calculate_annual_license,
_derive_review_score, _build_profile_query) se testean directamente.

load_crm_candidates() se testea con el cliente Supabase mockeado
para no requerir conexión real en el test suite.

Ejecutar:
  pytest tests/test_retrieval.py -v
"""

import pytest
from unittest.mock import MagicMock, patch

from app.models.intake import IntakeProfile
from app.services.retrieval import (
    _select_best_plan,
    _calculate_annual_license,
    _derive_review_score,
    _build_profile_query,
    _build_candidate,
    load_crm_candidates,
    USERS_UPPER,
)


# ══════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ══════════════════════════════════════════════════════════════════════════════

def make_profile(**overrides) -> IntakeProfile:
    defaults = {
        "sector":           "Tecnología / SaaS",
        "modelo":           "B2B — ventas a empresas",
        "sistema_actual":   "Excel o Google Sheets",
        "presupuesto":      "5.000 – 15.000€/año",
        "presupuesto_flex": "Hay algo de margen (+20–30%)",
        "empleados":        "26 – 50 personas",
        "usuarios_crm":     "6 – 15 usuarios",
        "suite":            "Google Workspace (Gmail, Drive, Calendar)",
        "tools":            [],
        "equipo_tech":      "Hay alguien con perfil técnico pero no dedicado",
        "clientes":         "100 – 500",
        "crecimiento":      "Crecimiento moderado (+10–30%)",
    }
    defaults.update(overrides)
    return IntakeProfile(**defaults)


# Plans de ejemplo que simulan datos reales de Supabase
PLAN_PER_USER = {
    "name": "Professional",
    "price_eur_month": 40,
    "users_included": 1,
    "users_max": 500,
    "billing": "per_user",
}

PLAN_FLAT = {
    "name": "Team",
    "price_eur_month": 200,
    "users_included": 10,
    "users_max": 25,
    "billing": "flat",
}

PLAN_UNLIMITED_USERS = {
    "name": "Enterprise",
    "price_eur_month": 80,
    "users_included": 1,
    "users_max": None,   # ilimitado
    "billing": "per_user",
}

PRICING_ROW = {
    "crm_id": "hub_001",
    "plans": [PLAN_PER_USER],
    "discount_annual_pct": 20,
    "annual_price_increase_pct": 0.07,
    "price_per_contact": False,
    "contact_tier_thresholds": None,
    "implementation_cost_est_eur": {"min": 500, "typical": 1500, "max": 5000},
    "training_cost_est_eur": {"min": 0, "typical": 600, "max": 2000},
    "migration_cost_est_eur": {"from_excel": 200, "from_crm": 1200},
    "pricing_last_updated": "2025-05-20",
}


# ══════════════════════════════════════════════════════════════════════════════
# TESTS — _select_best_plan
# ══════════════════════════════════════════════════════════════════════════════

class TestSelectBestPlan:

    def test_plan_per_user_calcula_coste_correcto(self):
        plan = _select_best_plan([PLAN_PER_USER], num_users=10)
        assert plan is not None
        assert plan["_monthly_cost"] == 400   # 40 × 10

    def test_plan_flat_ignora_num_usuarios(self):
        plan = _select_best_plan([PLAN_FLAT], num_users=5)
        assert plan is not None
        assert plan["_monthly_cost"] == 200   # precio fijo

    def test_retorna_none_si_ningun_plan_cubre_usuarios(self):
        plan_pequeno = {**PLAN_FLAT, "users_max": 5}
        result = _select_best_plan([plan_pequeno], num_users=20)
        assert result is None

    def test_selecciona_plan_mas_barato_entre_dos(self):
        plan_caro   = {**PLAN_PER_USER, "price_eur_month": 60, "users_max": 500}
        plan_barato = {**PLAN_PER_USER, "price_eur_month": 30, "users_max": 200}
        result = _select_best_plan([plan_caro, plan_barato], num_users=10)
        assert result["price_eur_month"] == 30

    def test_plan_usuarios_ilimitados_siempre_califica(self):
        result = _select_best_plan([PLAN_UNLIMITED_USERS], num_users=999)
        assert result is not None
        assert result["_monthly_cost"] == 80 * 999

    def test_excluye_plan_con_users_max_insuficiente(self):
        plan_pequeño = {**PLAN_PER_USER, "users_max": 5}
        result = _select_best_plan([plan_pequeño], num_users=10)
        assert result is None

    def test_plan_en_el_limite_exacto_de_usuarios(self):
        # users_max = 15, num_users = 15 → debe calificar
        plan = {**PLAN_PER_USER, "users_max": 15}
        result = _select_best_plan([plan], num_users=15)
        assert result is not None

    def test_lista_vacia_retorna_none(self):
        assert _select_best_plan([], num_users=10) is None

    def test_prefiere_flat_si_es_mas_barato_con_mucho_equipo(self):
        # Con 30 usuarios: per_user = 40×30=1200, flat = 800
        flat_barato = {**PLAN_FLAT, "price_eur_month": 800, "users_max": 50, "billing": "flat"}
        per_user    = {**PLAN_PER_USER, "price_eur_month": 40, "users_max": 200}
        result = _select_best_plan([flat_barato, per_user], num_users=30)
        assert result["billing"] == "flat"


# ══════════════════════════════════════════════════════════════════════════════
# TESTS — _calculate_annual_license
# ══════════════════════════════════════════════════════════════════════════════

class TestCalculateAnnualLicense:

    def test_calculo_basico_per_user_sin_descuento(self):
        pricing = {
            "plans": [{**PLAN_PER_USER, "price_eur_month": 40}],
            "discount_annual_pct": 0,
        }
        result = _calculate_annual_license(pricing, num_users=10)
        # 40 × 10 usuarios × 12 meses × (1 - 0) = 4800
        assert result == pytest.approx(4_800, rel=0.01)

    def test_descuento_anual_se_aplica(self):
        pricing = {
            "plans": [{**PLAN_PER_USER, "price_eur_month": 40}],
            "discount_annual_pct": 20,
        }
        result = _calculate_annual_license(pricing, num_users=10)
        # 4800 × 0.80 = 3840
        assert result == pytest.approx(3_840, rel=0.01)

    def test_sin_planes_retorna_none(self):
        assert _calculate_annual_license({"plans": []}, num_users=10) is None

    def test_sin_campo_plans_retorna_none(self):
        assert _calculate_annual_license({}, num_users=10) is None

    def test_sin_planes_que_cubran_equipo_retorna_none(self):
        pricing = {
            "plans": [{**PLAN_PER_USER, "users_max": 5}],
            "discount_annual_pct": 0,
        }
        assert _calculate_annual_license(pricing, num_users=20) is None

    def test_descuento_nulo_no_rompe(self):
        pricing = {
            "plans": [PLAN_PER_USER],
            "discount_annual_pct": None,
        }
        result = _calculate_annual_license(pricing, num_users=5)
        assert result is not None
        assert result > 0

    def test_resultado_redondeado_a_2_decimales(self):
        pricing = {
            "plans": [{**PLAN_PER_USER, "price_eur_month": 33.33}],
            "discount_annual_pct": 0,
        }
        result = _calculate_annual_license(pricing, num_users=3)
        # Debe tener máximo 2 decimales
        assert result == round(result, 2)


# ══════════════════════════════════════════════════════════════════════════════
# TESTS — _derive_review_score
# ══════════════════════════════════════════════════════════════════════════════

class TestDeriveReviewScore:

    def test_rating_perfecto_da_score_alto(self):
        quality = {"review_count_g2": 500, "avg_g2_rating": 5.0}
        score = _derive_review_score(quality)
        assert score == pytest.approx(10.0, abs=0.1)

    def test_rating_medio_da_score_medio(self):
        quality = {"review_count_g2": 500, "avg_g2_rating": 3.0}
        score = _derive_review_score(quality)
        # (3.0 - 1) / 4 × 10 = 5.0
        assert score == pytest.approx(5.0, abs=0.1)

    def test_pocas_reviews_retorna_none(self):
        quality = {"review_count_g2": 10, "avg_g2_rating": 4.5}
        assert _derive_review_score(quality) is None

    def test_sin_rating_retorna_none(self):
        quality = {"review_count_g2": 500, "avg_g2_rating": None}
        assert _derive_review_score(quality) is None

    def test_dict_vacio_retorna_none(self):
        assert _derive_review_score({}) is None

    def test_score_nunca_supera_10(self):
        quality = {"review_count_g2": 1000, "avg_g2_rating": 5.0}
        score = _derive_review_score(quality)
        assert score <= 10.0

    def test_score_nunca_negativo(self):
        quality = {"review_count_g2": 100, "avg_g2_rating": 1.0}
        score = _derive_review_score(quality)
        assert score >= 0.0


# ══════════════════════════════════════════════════════════════════════════════
# TESTS — _build_profile_query
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildProfileQuery:

    def test_incluye_sector(self):
        profile = make_profile(sector="Salud / Farma")
        query = _build_profile_query(profile)
        assert "Salud / Farma" in query

    def test_incluye_usuarios(self):
        profile = make_profile(usuarios_crm="16 – 30 usuarios")
        query = _build_profile_query(profile)
        assert "16 – 30 usuarios" in query

    def test_incluye_sistema_actual(self):
        profile = make_profile(sistema_actual="Un CRM (HubSpot, Salesforce, Zoho…)")
        query = _build_profile_query(profile)
        assert "CRM" in query

    def test_devuelve_string_no_vacio(self):
        query = _build_profile_query(make_profile())
        assert isinstance(query, str)
        assert len(query) > 20


# ══════════════════════════════════════════════════════════════════════════════
# TESTS — _build_candidate
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildCandidate:

    def _make_rows(self, **overrides):
        catalog = {
            "name": "Test CRM",
            "crm_category": "generalista",
            "best_fit_team_size_min": 1,
            "best_fit_team_size_max": 200,
            "gdpr_compliant": True,
            "data_hosting_regions": ["EU"],
        }
        pricing = {
            "annual_price_increase_pct": 0.07,
            "price_per_contact": False,
            "contact_tier_thresholds": None,
            "implementation_cost_est_eur": {"min": 0, "typical": 800, "max": 3000},
            "training_cost_est_eur": {"typical": 500},
            "migration_cost_est_eur": {"from_excel": 200},
            "discount_annual_pct": 0,
            "pricing_last_updated": "2025-05-01",
        }
        scoring = {
            "avg_implementation_weeks": 4,
            "requires_external_consultant": "optional",
            "learning_curve_score": 7.5,
            "implementation_complexity_score": 8.0,
            "lockin_risk_score": 7.0,
            "support_score": 6.5,
            "native_integrations": ["gmail"],
            "support_spanish_available": True,
        }
        quality = {
            "review_count_g2": 300,
            "avg_g2_rating": 4.2,
            "scoring_confidence": "high",
        }
        return catalog, pricing, scoring, quality

    def test_construye_candidate_correctamente(self):
        catalog, pricing, scoring, quality = self._make_rows()
        candidate = _build_candidate("crm_001", catalog, pricing, scoring, quality, 3600.0)

        assert candidate.crm_id == "crm_001"
        assert candidate.name == "Test CRM"
        assert candidate.annual_license_eur == 3600.0
        assert candidate.gdpr_compliant is True
        assert candidate.learning_curve_score == 7.5

    def test_campos_nulos_usan_defaults(self):
        catalog, pricing, scoring, quality = self._make_rows()
        scoring["learning_curve_score"] = None
        scoring["support_score"] = None

        candidate = _build_candidate("crm_001", catalog, pricing, scoring, quality, 1000.0)

        assert candidate.learning_curve_score is None
        assert candidate.support_score is None

    def test_review_score_derivado_de_quality(self):
        catalog, pricing, scoring, quality = self._make_rows()
        # avg_g2_rating=4.2 con 300 reviews → review_score calculado
        candidate = _build_candidate("crm_001", catalog, pricing, scoring, quality, 1000.0)
        assert candidate.review_score is not None
        assert 0 <= candidate.review_score <= 10


# ══════════════════════════════════════════════════════════════════════════════
# TESTS — load_crm_candidates (con Supabase mockeado)
# ══════════════════════════════════════════════════════════════════════════════

class TestLoadCrmCandidates:
    """
    Testea load_crm_candidates mockeando el cliente Supabase.
    Simula respuestas reales de las 4 tablas para 2 CRMs de ejemplo.
    """

    def _make_supabase_mock(self):
        """Crea un mock del cliente Supabase con datos de 2 CRMs."""
        mock_client = MagicMock()

        catalog_data = [
            {
                "crm_id": "hub_001",
                "name": "HubSpot CRM",
                "crm_category": "generalista",
                "best_fit_team_size_min": 3,
                "best_fit_team_size_max": 200,
                "gdpr_compliant": True,
                "data_hosting_regions": ["EU", "US"],
            },
            {
                "crm_id": "zoho_001",
                "name": "Zoho CRM",
                "crm_category": "generalista",
                "best_fit_team_size_min": 1,
                "best_fit_team_size_max": 300,
                "gdpr_compliant": True,
                "data_hosting_regions": ["EU"],
            },
        ]

        pricing_data = [
            {
                "crm_id": "hub_001",
                "plans": [{"name": "Pro", "price_eur_month": 90, "users_max": 500, "billing": "per_user"}],
                "discount_annual_pct": 20,
                "annual_price_increase_pct": 0.07,
                "price_per_contact": True,
                "contact_tier_thresholds": {"1": 0, "1001": 45},
                "implementation_cost_est_eur": {"min": 500, "typical": 1500, "max": 5000},
                "training_cost_est_eur": {"typical": 800},
                "migration_cost_est_eur": {"from_excel": 300},
                "pricing_last_updated": "2025-05-20",
            },
            {
                "crm_id": "zoho_001",
                "plans": [{"name": "Professional", "price_eur_month": 20, "users_max": 500, "billing": "per_user"}],
                "discount_annual_pct": 15,
                "annual_price_increase_pct": 0.05,
                "price_per_contact": False,
                "contact_tier_thresholds": None,
                "implementation_cost_est_eur": {"min": 0, "typical": 800, "max": 3000},
                "training_cost_est_eur": {"typical": 500},
                "migration_cost_est_eur": {"from_excel": 200},
                "pricing_last_updated": "2025-05-20",
            },
        ]

        scoring_data = [
            {
                "crm_id": "hub_001",
                "avg_implementation_weeks": 3,
                "requires_external_consultant": "optional",
                "learning_curve_score": 8.5,
                "implementation_complexity_score": 8.0,
                "lockin_risk_score": 6.5,
                "support_score": 8.0,
                "native_integrations": ["gmail", "slack"],
                "support_spanish_available": True,
            },
            {
                "crm_id": "zoho_001",
                "avg_implementation_weeks": 4,
                "requires_external_consultant": "optional",
                "learning_curve_score": 7.0,
                "implementation_complexity_score": 7.5,
                "lockin_risk_score": 8.5,
                "support_score": 6.5,
                "native_integrations": ["gmail", "google_workspace"],
                "support_spanish_available": True,
            },
        ]

        quality_data = [
            {
                "crm_id": "hub_001",
                "review_count_g2": 500,
                "avg_g2_rating": 4.4,
                "scoring_confidence": "high",
            },
            {
                "crm_id": "zoho_001",
                "review_count_g2": 350,
                "avg_g2_rating": 4.1,
                "scoring_confidence": "high",
            },
        ]

        def table_side_effect(table_name):
            data_map = {
                "crm_catalog":     catalog_data,
                "crm_pricing":     pricing_data,
                "crm_scoring":     scoring_data,
                "crm_data_quality": quality_data,
            }
            mock_table = MagicMock()
            mock_select = MagicMock()
            mock_execute = MagicMock()
            mock_execute.data = data_map.get(table_name, [])
            mock_select.execute.return_value = mock_execute
            mock_table.select.return_value = mock_select
            return mock_table

        mock_client.table.side_effect = table_side_effect
        return mock_client

    @patch("app.services.retrieval._get_client")
    def test_carga_dos_crms_correctamente(self, mock_get_client):
        mock_get_client.return_value = self._make_supabase_mock()
        profile = make_profile(usuarios_crm="6 – 15 usuarios")  # → 15 usuarios
        candidates = load_crm_candidates(profile)

        assert len(candidates) == 2
        names = {c.name for c in candidates}
        assert "HubSpot CRM" in names
        assert "Zoho CRM" in names

    @patch("app.services.retrieval._get_client")
    def test_annual_license_calculada_correctamente(self, mock_get_client):
        mock_get_client.return_value = self._make_supabase_mock()
        profile = make_profile(usuarios_crm="6 – 15 usuarios")  # → 15 usuarios
        candidates = load_crm_candidates(profile)

        zoho = next(c for c in candidates if c.crm_id == "zoho_001")
        # Plan: 20€/user/mes × 15 usuarios × 12 meses × (1 - 0.15) = 3060
        assert zoho.annual_license_eur == pytest.approx(3_060, rel=0.01)

    @patch("app.services.retrieval._get_client")
    def test_crm_sin_plan_para_equipo_es_omitido(self, mock_get_client):
        """Un CRM con plans vacío no debe aparecer en los candidatos."""
        mock_client = self._make_supabase_mock()

        # Modificar el mock para que zoho no tenga planes
        original_side_effect = mock_client.table.side_effect

        def patched_side_effect(table_name):
            table = original_side_effect(table_name)
            if table_name == "crm_pricing":
                # Sobrescribir datos de pricing para zoho
                mock_table = MagicMock()
                mock_select = MagicMock()
                mock_execute = MagicMock()
                mock_execute.data = [
                    {"crm_id": "hub_001", "plans": [{"name": "Pro", "price_eur_month": 90, "users_max": 500, "billing": "per_user"}], "discount_annual_pct": 0},
                    {"crm_id": "zoho_001", "plans": [], "discount_annual_pct": 0},  # sin planes
                ]
                mock_select.execute.return_value = mock_execute
                mock_table.select.return_value = mock_select
                return mock_table
            return table

        mock_client.table.side_effect = patched_side_effect
        mock_get_client.return_value = mock_client

        candidates = load_crm_candidates(make_profile())
        crm_ids = {c.crm_id for c in candidates}
        assert "zoho_001" not in crm_ids
        assert "hub_001" in crm_ids

    @patch("app.services.retrieval._get_client")
    def test_scores_base_se_transfieren(self, mock_get_client):
        mock_get_client.return_value = self._make_supabase_mock()
        candidates = load_crm_candidates(make_profile())
        hub = next(c for c in candidates if c.crm_id == "hub_001")

        assert hub.learning_curve_score == 8.5
        assert hub.support_score == 8.0
        assert "gmail" in hub.native_integrations

    @patch("app.services.retrieval._get_client")
    def test_supabase_error_lanza_runtime_error(self, mock_get_client):
        mock_get_client.side_effect = Exception("Connection refused")

        with pytest.raises(RuntimeError, match="No se pudo conectar"):
            load_crm_candidates(make_profile())
