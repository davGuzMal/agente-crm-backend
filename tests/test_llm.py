"""
tests/test_llm.py
─────────────────
Tests unitarios para app/services/llm.py.

Las funciones puras (build_user_message, SYSTEM_PROMPT) se testean directamente.
stream_verdict() se testea mockeando el cliente Anthropic para no
consumir tokens reales ni requerir API key en CI.

Ejecutar:
  pytest tests/test_llm.py -v
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.intake import IntakeProfile
from app.services.filter import ExclusionResult, FilterOutput
from app.services.scoring import (
    ScoredCRM, ScoringOutput, ScoreDetail, AlertFlag, WeightAdjustment
)
from app.services.llm import (
    SYSTEM_PROMPT,
    build_user_message,
    stream_verdict,
    MODEL,
    MAX_TOKENS,
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
        "tools":            ["Slack / Teams (mensajería)", "Stripe / PayPal (pagos)"],
        "equipo_tech":      "Hay alguien con perfil técnico pero no dedicado",
        "clientes":         "100 – 500",
        "crecimiento":      "Crecimiento moderado (+10–30%)",
    }
    defaults.update(overrides)
    return IntakeProfile(**defaults)


def make_score_detail(raw: float, weight: float) -> ScoreDetail:
    return ScoreDetail(
        raw_score=raw,
        weight=weight,
        weighted_contribution=round(raw * weight, 4),
    )


def make_scored_crm(crm_id: str, name: str, rank: int, score: float, tco: float) -> ScoredCRM:
    breakdown = {
        "tco":               make_score_detail(8.5, 0.23),
        "curva_aprendizaje": make_score_detail(7.0, 0.18),
        "complejidad_impl":  make_score_detail(8.0, 0.17),
        "lockin_risk":       make_score_detail(8.5, 0.13),
        "soporte":           make_score_detail(7.0, 0.15),
        "reviews":           make_score_detail(7.8, 0.14),
    }
    return ScoredCRM(
        crm_id=crm_id,
        crm_name=name,
        crm_category="generalista",
        rank=rank,
        final_score=score,
        tco_3y_eur=tco,
        tco_score=8.5,
        score_breakdown=breakdown,
        flags=[],
    )


def make_scoring_output() -> ScoringOutput:
    return ScoringOutput(
        ranked_crms=[
            make_scored_crm("zoho_001", "Zoho CRM",    rank=1, score=82.4, tco=6_800),
            make_scored_crm("hub_001",  "HubSpot CRM", rank=2, score=78.1, tco=32_131),
            make_scored_crm("fresh_001","Freshsales",  rank=3, score=74.9, tco=10_800),
        ],
        applied_weights={
            "tco": 0.23, "curva_aprendizaje": 0.18, "complejidad_impl": 0.17,
            "lockin_risk": 0.13, "soporte": 0.15, "reviews": 0.14,
        },
        weight_adjustments=[
            WeightAdjustment(variable="curva_aprendizaje", delta=+0.03, reason="Sin IT dedicado"),
            WeightAdjustment(variable="complejidad_impl",  delta=+0.02, reason="Sin IT dedicado"),
        ],
        all_flags=[
            AlertFlag(
                code="tco_limite", crm_id="hub_001", crm_name="HubSpot CRM",
                severity="warning",
                message="TCO estimado supera el presupuesto declarado en un 214%.",
            )
        ],
        scoring_confidence="high",
    )


def make_excluded_crms() -> list:
    return [
        ExclusionResult(
            crm_id="sf_001", crm_name="Salesforce Starter",
            filter_code="F04",
            reason="TCO supera el presupuesto en un 200%.",
            tco_rough_eur=48_000,
        )
    ]


# ══════════════════════════════════════════════════════════════════════════════
# TESTS — SYSTEM_PROMPT
# ══════════════════════════════════════════════════════════════════════════════

class TestSystemPrompt:

    def test_no_es_vacio(self):
        assert len(SYSTEM_PROMPT) > 200

    def test_contiene_etiquetas_de_seccion(self):
        for tag in ["[VEREDICTO]", "[RANKING]", "[ANALISIS_GANADOR]",
                    "[ALTERNATIVA]", "[ALERTAS]", "[CONFIANZA]"]:
            assert tag in SYSTEM_PROMPT, f"Falta etiqueta {tag} en SYSTEM_PROMPT"

    def test_menciona_restriccion_de_ranking(self):
        assert "NO modificar" in SYSTEM_PROMPT or "No cambies" in SYSTEM_PROMPT

    def test_menciona_independencia_comercial(self):
        assert "independiente" in SYSTEM_PROMPT or "relación comercial" in SYSTEM_PROMPT

    def test_especifica_idioma(self):
        assert "español" in SYSTEM_PROMPT.lower()


# ══════════════════════════════════════════════════════════════════════════════
# TESTS — build_user_message
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildUserMessage:

    def test_contiene_nombre_crm_ganador(self):
        msg = build_user_message(make_scoring_output(), make_profile(), [])
        assert "Zoho CRM" in msg

    def test_contiene_todos_los_crms_del_ranking(self):
        msg = build_user_message(make_scoring_output(), make_profile(), [])
        assert "HubSpot CRM" in msg
        assert "Freshsales" in msg

    def test_contiene_scores_finales(self):
        msg = build_user_message(make_scoring_output(), make_profile(), [])
        assert "82.4" in msg
        assert "78.1" in msg

    def test_contiene_tco_formateado(self):
        msg = build_user_message(make_scoring_output(), make_profile(), [])
        assert "6,800" in msg or "6.800" in msg

    def test_contiene_perfil_empresa(self):
        msg = build_user_message(make_scoring_output(), make_profile(), [])
        assert "Tecnología / SaaS" in msg
        assert "6 – 15 usuarios" in msg
        assert "Google Workspace" in msg

    def test_contiene_herramientas_del_stack(self):
        msg = build_user_message(make_scoring_output(), make_profile(), [])
        assert "Slack" in msg or "Stripe" in msg

    def test_contiene_pesos_aplicados(self):
        msg = build_user_message(make_scoring_output(), make_profile(), [])
        assert "PESOS APLICADOS" in msg
        assert "23.0%" in msg or "23%" in msg

    def test_contiene_ajustes_de_pesos(self):
        msg = build_user_message(make_scoring_output(), make_profile(), [])
        assert "Sin IT dedicado" in msg

    def test_contiene_alertas(self):
        msg = build_user_message(make_scoring_output(), make_profile(), [])
        assert "tco_limite" in msg
        assert "HubSpot CRM" in msg

    def test_contiene_crms_excluidos(self):
        msg = build_user_message(
            make_scoring_output(), make_profile(), [],
            excluded_crms=make_excluded_crms(),
        )
        assert "Salesforce Starter" in msg
        assert "F04" in msg

    def test_sin_excluidos_no_muestra_seccion(self):
        msg = build_user_message(make_scoring_output(), make_profile(), [], excluded_crms=[])
        # La sección no debe aparecer si no hay excluidos
        assert "CRMS EXCLUIDOS" not in msg

    def test_incluye_chunks_semanticos(self):
        chunks = [
            {"crm_id": "zoho_001", "chunk_type": "review_summary", "content": "Muy buena integración con Google."},
        ]
        msg = build_user_message(make_scoring_output(), make_profile(), chunks)
        assert "Muy buena integración" in msg
        assert "zoho_001" in msg

    def test_limita_chunks_a_8(self):
        chunks = [
            {"crm_id": f"crm_{i}", "chunk_type": "review", "content": f"Contenido {i}"}
            for i in range(15)
        ]
        msg = build_user_message(make_scoring_output(), make_profile(), chunks)
        # Solo los primeros 8 chunks deben estar en el mensaje
        assert "Contenido 7" in msg    # chunk #8 (índice 7)
        assert "Contenido 8" not in msg  # chunk #9 debe estar omitido

    def test_contiene_instruccion_final(self):
        msg = build_user_message(make_scoring_output(), make_profile(), [])
        assert "INSTRUCCIÓN" in msg

    def test_devuelve_string_no_vacio(self):
        msg = build_user_message(make_scoring_output(), make_profile(), [])
        assert isinstance(msg, str)
        assert len(msg) > 500

    def test_confidencia_incluida_en_mensaje(self):
        msg = build_user_message(make_scoring_output(), make_profile(), [])
        assert "HIGH" in msg or "high" in msg.lower()


# ══════════════════════════════════════════════════════════════════════════════
# TESTS — stream_verdict (con cliente Anthropic mockeado)
# ══════════════════════════════════════════════════════════════════════════════

class MockAsyncStream:
    """
    Mock del context manager de streaming de Anthropic.
    Emula la API: `async with client.messages.stream(...) as stream`.
    """
    def __init__(self, tokens: list):
        self._tokens = tokens

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    @property
    def text_stream(self):
        async def _gen():
            for token in self._tokens:
                yield token
        return _gen()


class TestStreamVerdict:

    @pytest.mark.asyncio
    async def test_emite_tokens_en_formato_sse(self):
        tokens = ["[VEREDICTO]\n", "Zoho CRM es ", "la mejor opción."]
        mock_stream = MockAsyncStream(tokens)

        with patch("app.services.llm._client") as mock_client:
            mock_client.messages.stream.return_value = mock_stream

            chunks = []
            async for chunk in stream_verdict(make_scoring_output(), make_profile(), []):
                chunks.append(chunk)

        # Verificar que todos los chunks tienen formato SSE correcto
        token_chunks = [c for c in chunks if '"token"' in c]
        for chunk in token_chunks:
            assert chunk.startswith("data: ")
            assert chunk.endswith("\n\n")

    @pytest.mark.asyncio
    async def test_emite_evento_done_al_finalizar(self):
        mock_stream = MockAsyncStream(["texto"])

        with patch("app.services.llm._client") as mock_client:
            mock_client.messages.stream.return_value = mock_stream

            chunks = []
            async for chunk in stream_verdict(make_scoring_output(), make_profile(), []):
                chunks.append(chunk)

        last_chunk = chunks[-1]
        data = json.loads(last_chunk.replace("data: ", "").strip())
        assert data.get("type") == "done"

    @pytest.mark.asyncio
    async def test_tokens_son_json_valido(self):
        mock_stream = MockAsyncStream(["Hola ", "mundo"])

        with patch("app.services.llm._client") as mock_client:
            mock_client.messages.stream.return_value = mock_stream

            chunks = []
            async for chunk in stream_verdict(make_scoring_output(), make_profile(), []):
                chunks.append(chunk)

        for chunk in chunks:
            raw = chunk.replace("data: ", "").strip()
            parsed = json.loads(raw)   # no debe lanzar excepción
            assert isinstance(parsed, dict)

    @pytest.mark.asyncio
    async def test_timeout_emite_error_e02(self):
        import anthropic as ant

        with patch("app.services.llm._client") as mock_client:
            mock_client.messages.stream.side_effect = ant.APITimeoutError(
                request=MagicMock()
            )

            chunks = []
            async for chunk in stream_verdict(make_scoring_output(), make_profile(), []):
                chunks.append(chunk)

        assert len(chunks) == 1
        data = json.loads(chunks[0].replace("data: ", "").strip())
        assert data["type"] == "error"
        assert data["code"] == "E02"

    @pytest.mark.asyncio
    async def test_error_generico_emite_e99(self):
        with patch("app.services.llm._client") as mock_client:
            mock_client.messages.stream.side_effect = Exception("Error inesperado")

            chunks = []
            async for chunk in stream_verdict(make_scoring_output(), make_profile(), []):
                chunks.append(chunk)

        data = json.loads(chunks[0].replace("data: ", "").strip())
        assert data["type"] == "error"
        assert data["code"] == "E99"

    @pytest.mark.asyncio
    async def test_llama_a_claude_con_parametros_correctos(self):
        mock_stream = MockAsyncStream(["ok"])

        with patch("app.services.llm._client") as mock_client:
            mock_client.messages.stream.return_value = mock_stream

            async for _ in stream_verdict(make_scoring_output(), make_profile(), []):
                pass

            call_kwargs = mock_client.messages.stream.call_args.kwargs
            assert call_kwargs["model"] == MODEL
            assert call_kwargs["max_tokens"] == MAX_TOKENS
            assert call_kwargs["system"] == SYSTEM_PROMPT
            assert len(call_kwargs["messages"]) == 1
            assert call_kwargs["messages"][0]["role"] == "user"

    @pytest.mark.asyncio
    async def test_acepta_lista_vacia_de_contexto(self):
        """stream_verdict no debe fallar con semantic_context vacío."""
        mock_stream = MockAsyncStream(["ok"])

        with patch("app.services.llm._client") as mock_client:
            mock_client.messages.stream.return_value = mock_stream

            chunks = []
            async for chunk in stream_verdict(
                make_scoring_output(), make_profile(), semantic_context=[]
            ):
                chunks.append(chunk)

        assert any('"done"' in c for c in chunks)
