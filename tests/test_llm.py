"""
tests/test_llm.py — cubre StreamParser + stream_verdict
"""
import json
import pytest
from unittest.mock import MagicMock, patch

from app.models.intake import IntakeProfile
from app.services.filter import ExclusionResult
from app.services.scoring import (
    ScoredCRM, ScoringOutput, ScoreDetail, AlertFlag, WeightAdjustment,
)
from app.services.llm import (
    SYSTEM_PROMPT, KNOWN_SECTIONS, StreamParser,
    build_user_message, stream_verdict, MODEL, MAX_TOKENS,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_profile(**ov):
    d = {
        "sector":"Tecnología / SaaS","modelo":"B2B — ventas a empresas",
        "sistema_actual":"Excel o Google Sheets",
        "presupuesto":"5.000 – 15.000€/año","presupuesto_flex":"Hay algo de margen (+20–30%)",
        "empleados":"26 – 50 personas","usuarios_crm":"6 – 15 usuarios",
        "suite":"Google Workspace (Gmail, Drive, Calendar)","tools":["Slack / Teams (mensajería)"],
        "equipo_tech":"Hay alguien con perfil técnico pero no dedicado",
        "clientes":"100 – 500","crecimiento":"Crecimiento moderado (+10–30%)",
    }
    d.update(ov)
    return IntakeProfile(**d)

def make_scored_crm(crm_id,name,rank,score,tco):
    bd={v:ScoreDetail(raw_score=7.0,weight=1/6,weighted_contribution=7/6)
        for v in["tco","curva_aprendizaje","complejidad_impl","lockin_risk","soporte","reviews"]}
    return ScoredCRM(crm_id=crm_id,crm_name=name,crm_category="generalista",
                     rank=rank,final_score=score,tco_3y_eur=tco,tco_score=7.5,score_breakdown=bd)

def make_scoring_output():
    return ScoringOutput(
        ranked_crms=[make_scored_crm("zoho","Zoho CRM",1,82.4,6800),
                     make_scored_crm("hub","HubSpot CRM",2,78.1,32131)],
        applied_weights={"tco":0.23,"curva_aprendizaje":0.18,"complejidad_impl":0.17,
                         "lockin_risk":0.13,"soporte":0.15,"reviews":0.14},
        weight_adjustments=[WeightAdjustment(variable="curva_aprendizaje",delta=+0.03,reason="Sin IT")],
        all_flags=[AlertFlag(code="tco_limite",crm_id="hub",crm_name="HubSpot CRM",
                             severity="warning",message="TCO supera presupuesto.")],
        scoring_confidence="high",
    )

class MockAsyncStream:
    def __init__(self,tokens): self._tokens=tokens
    async def __aenter__(self): return self
    async def __aexit__(self,*a): pass
    @property
    def text_stream(self):
        async def _g():
            for t in self._tokens: 
                yield t
        return _g()


# ══════════════════════════════════════════════════════════════════════════════
# 1. SYSTEM PROMPT Y BUILD_USER_MESSAGE
# ══════════════════════════════════════════════════════════════════════════════

class TestSystemPrompt:
    def test_contiene_todas_las_etiquetas(self):
        for tag in ["[VEREDICTO]","[RANKING]","[ANALISIS_GANADOR]","[ALTERNATIVA]","[ALERTAS]","[CONFIANZA]"]:
            assert tag in SYSTEM_PROMPT
    def test_menciona_no_modificar_ranking(self):
        assert "NO modificar" in SYSTEM_PROMPT or "No cambies" in SYSTEM_PROMPT
    def test_menciona_independencia(self):
        assert "independiente" in SYSTEM_PROMPT

class TestBuildUserMessage:
    def test_contiene_crms(self):
        msg=build_user_message(make_scoring_output(),make_profile(),[])
        assert "Zoho CRM" in msg and "HubSpot CRM" in msg
    def test_contiene_score(self):
        assert "82.4" in build_user_message(make_scoring_output(),make_profile(),[])
    def test_contiene_perfil(self):
        assert "Tecnología / SaaS" in build_user_message(make_scoring_output(),make_profile(),[])
    def test_excluidos_aparecen(self):
        excl=[ExclusionResult(crm_id="sf",crm_name="Salesforce",filter_code="F04",reason="Caro.")]
        msg=build_user_message(make_scoring_output(),make_profile(),[],excl)
        assert "Salesforce" in msg and "F04" in msg
    def test_sin_excluidos_no_aparece_seccion(self):
        assert "CRMS EXCLUIDOS" not in build_user_message(make_scoring_output(),make_profile(),[],[])
    def test_chunks_limitados_a_8(self):
        chunks=[{"crm_id":f"c{i}","chunk_type":"r","content":f"Texto{i}"} for i in range(15)]
        msg=build_user_message(make_scoring_output(),make_profile(),chunks)
        assert "Texto7" in msg and "Texto8" not in msg


# ══════════════════════════════════════════════════════════════════════════════
# 2. StreamParser
# ══════════════════════════════════════════════════════════════════════════════

class TestStreamParserBasico:
    def test_tag_completo_detectado(self):
        p=StreamParser()
        events=p.feed("[VEREDICTO]\nZoho.")
        assert any(e["type"]=="section_start" and e["section"]=="VEREDICTO" for e in events)

    def test_contenido_lleva_seccion_correcta(self):
        p=StreamParser()
        events=p.feed("[VEREDICTO]\nZoho.")
        tokens=[e for e in events if e["type"]=="token"]
        assert all(t["section"]=="VEREDICTO" for t in tokens)
        assert any("Zoho" in t["content"] for t in tokens)

    def test_sin_tag_solo_tokens(self):
        p=StreamParser()
        events=p.feed("Texto plano.")
        assert all(e["type"]=="token" for e in events)
        assert all(e["section"] is None for e in events)

    def test_flush_vacia_residuo(self):
        p=StreamParser()
        p.feed("[VEREDICTO]\nTexto incompleto")
        events=p.flush()
        content="".join(e["content"] for e in events if e["type"]=="token")
        if content != "":
            assert "Texto incompleto" in content

    def test_sections_completed_se_actualiza(self):
        p=StreamParser()
        p.feed("[VEREDICTO]\nA.")
        p.feed("[RANKING]\nB.")
        p.flush()
        assert "VEREDICTO" in p.sections_completed


class TestStreamParserTagFragmentado:
    def test_tag_en_dos_tokens(self):
        p=StreamParser()
        ev1=p.feed("[VERED")
        ev2=p.feed("ICTO]\nHola")
        assert "section_start" not in [e["type"] for e in ev1]
        assert any(e["type"]=="section_start" and e["section"]=="VEREDICTO" for e in ev2)

    def test_tag_en_tres_tokens(self):
        p=StreamParser()
        p.feed("[ANALISIS")
        p.feed("_GANAD")
        ev=p.feed("OR]\nContenido.")
        assert any(e["type"]=="section_start" and e["section"]=="ANALISIS_GANADOR" for e in ev)

    def test_contenido_antes_de_parcial_se_emite(self):
        p=StreamParser()
        events=p.feed("Texto largo [")
        content="".join(e["content"] for e in events if e["type"]=="token")
        assert "Texto largo" in content

    def test_corchete_no_tag_se_emite_como_contenido(self):
        p=StreamParser()
        p.feed("[link_externo]")
        events=p.flush()
        assert not any(e["type"]=="section_start" for e in events)

    def test_buffer_no_retiene_indefinidamente(self):
        from app.services.llm import MAX_TAG_LEN
        p=StreamParser()
        events=p.feed("["+"x"*(MAX_TAG_LEN+5))
        assert any(e["type"]=="token" for e in events)

    def test_newline_tras_tag_no_se_emite(self):
        p=StreamParser()
        events=p.feed("[VEREDICTO]\nPrimer texto")
        tokens=[e for e in events if e["type"]=="token"]
        content="".join(t["content"] for t in tokens)
        assert not content.startswith("\n")


class TestStreamParserMultiplesSecciones:
    def _parse(self,text):
        p=StreamParser()
        evs=[]
        for ch in text:
            evs.extend(p.feed(ch))
        evs.extend(p.flush())
        return p,evs

    def test_detecta_las_6_secciones(self):
        txt="".join(f"[{s}]\nTexto.\n" for s in KNOWN_SECTIONS)
        p,evs=self._parse(txt)
        found={e["section"] for e in evs if e["type"]=="section_start"}
        assert found==set(KNOWN_SECTIONS)

    def test_contenido_asignado_correctamente(self):
        txt="[VEREDICTO]\nContenido V.\n[RANKING]\nContenido R.\n"
        _,evs=self._parse(txt)
        v="".join(e["content"] for e in evs if e["type"]=="token" and e["section"]=="VEREDICTO")
        r="".join(e["content"] for e in evs if e["type"]=="token" and e["section"]=="RANKING")
        assert "Contenido V" in v
        assert "Contenido V" not in r
        assert "Contenido R" in r

    def test_sections_completed_orden(self):
        txt="[VEREDICTO]\nA\n[RANKING]\nB\n[ALERTAS]\nC\n"
        p,_=self._parse(txt)
        assert "VEREDICTO" in p.sections_completed
        assert "RANKING" in p.sections_completed


class TestStreamParserEdgeCases:
    def test_feed_vacio(self):
        assert StreamParser().feed("")==[]
    def test_flush_buffer_vacio(self):
        assert StreamParser().flush()==[]
    def test_multiples_feed_char_por_char(self):
        p=StreamParser()
        evs=[]
        for ch in "[CONFIANZA]\nTexto final":
            evs.extend(p.feed(ch))
        evs.extend(p.flush())
        assert any(e["type"]=="section_start" and e["section"]=="CONFIANZA" for e in evs)


# ══════════════════════════════════════════════════════════════════════════════
# 3. stream_verdict
# ══════════════════════════════════════════════════════════════════════════════

class TestStreamVerdict:
    @pytest.mark.asyncio
    async def test_emite_section_start_y_tokens(self):
        with patch("app.services.llm._client") as mc:
            mc.messages.stream.return_value=MockAsyncStream(["[VEREDICTO]\nZoho es la opción."])
            chunks=[]
            async for c in stream_verdict(make_scoring_output(),make_profile(),[]):
                chunks.append(c)
        evs=[json.loads(c.replace("data:","").strip()) for c in chunks]
        types=[e["type"] for e in evs]
        assert "section_start" in types and "token" in types

    @pytest.mark.asyncio
    async def test_done_incluye_secciones(self):
        with patch("app.services.llm._client") as mc:
            mc.messages.stream.return_value=MockAsyncStream(["[VEREDICTO]\nA.\n[RANKING]\nB."])
            chunks=[]
            async for c in stream_verdict(make_scoring_output(),make_profile(),[]):
                chunks.append(c)
        done=next(json.loads(c.replace("data:","").strip()) for c in chunks if '"done"' in c)
        assert done["type"]=="done" and "VEREDICTO" in done["sections_completed"]

    @pytest.mark.asyncio
    async def test_todos_los_chunks_son_sse_valido(self):
        with patch("app.services.llm._client") as mc:
            mc.messages.stream.return_value=MockAsyncStream(["[VEREDICTO]\nok"])
            async for chunk in stream_verdict(make_scoring_output(),make_profile(),[]):
                assert chunk.startswith("data: ") and chunk.endswith("\n\n")
                json.loads(chunk.replace("data: ","").strip())

    @pytest.mark.asyncio
    async def test_tokens_llevan_seccion(self):
        with patch("app.services.llm._client") as mc:
            mc.messages.stream.return_value=MockAsyncStream(["[RANKING]\nContenido."])
            chunks=[]
            async for c in stream_verdict(make_scoring_output(),make_profile(),[]):
                chunks.append(c)
        tok_evs=[json.loads(c.replace("data:","").strip()) for c in chunks if '"token"' in c]
        assert all(e["section"]=="RANKING" for e in tok_evs)

    @pytest.mark.asyncio
    async def test_timeout_emite_e02(self):
        import anthropic as ant
        with patch("app.services.llm._client") as mc:
            mc.messages.stream.side_effect=ant.APITimeoutError(request=MagicMock())
            chunks=[]
            async for c in stream_verdict(make_scoring_output(),make_profile(),[]):
                chunks.append(c)
        ev=json.loads(chunks[0].replace("data:","").strip())
        assert ev["type"]=="error" and ev["code"]=="E02"

    @pytest.mark.asyncio
    async def test_error_generico_emite_e99(self):
        with patch("app.services.llm._client") as mc:
            mc.messages.stream.side_effect=Exception("fallo")
            chunks=[]
            async for c in stream_verdict(make_scoring_output(),make_profile(),[]):
                chunks.append(c)
        ev=json.loads(chunks[0].replace("data:","").strip())
        assert ev["type"]=="error" and ev["code"]=="E99"

    @pytest.mark.asyncio
    async def test_llama_con_modelo_correcto(self):
        with patch("app.services.llm._client") as mc:
            mc.messages.stream.return_value=MockAsyncStream(["ok"])
            async for _ in stream_verdict(make_scoring_output(),make_profile(),[]):
                pass
            kw=mc.messages.stream.call_args.kwargs
            assert kw["model"]==MODEL and kw["max_tokens"]==MAX_TOKENS