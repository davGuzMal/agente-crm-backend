# Graph Report - .  (2026-08-10)

## Corpus Check
- Corpus is ~17,178 words - fits in a single context window. You may not need a graph.

## Summary
- 379 nodes · 992 edges · 19 communities (17 shown, 2 thin omitted)
- Extraction: 86% EXTRACTED · 14% INFERRED · 0% AMBIGUOUS · INFERRED: 142 edges (avg confidence: 0.56)
- Token cost: 2,400 input · 1,100 output

## Community Hubs (Navigation)
- LLM Service & Rationales
- CRM & Intake Models
- App Config & Feedback
- Hard-Filter Service
- Scoring Service & Tests
- License Pricing (Retrieval)
- Candidate Builder (Retrieval)
- Supabase Verification Scripts
- Supabase Client Packages
- FastAPI Stack & Dev Workflow
- LLM Provider SDKs
- Venv Activation Workflow
- Pytest Framework

## God Nodes (most connected - your core abstractions)
1. `IntakeProfile` - 68 edges
2. `CRMCandidate` - 39 edges
3. `apply_hard_filters()` - 39 edges
4. `StreamParser` - 35 edges
5. `make_profile()` - 34 edges
6. `FilterOutput` - 30 edges
7. `make_profile()` - 30 edges
8. `make_crm()` - 29 edges
9. `score_and_rank()` - 24 edges
10. `ExclusionResult` - 23 edges

## Surprising Connections (you probably didn't know these)
- `TestBaseCase` --uses--> `CRMCandidate`  [INFERRED]
  tests/test_filter.py → app/models/crm.py
- `TestF01Usuarios` --uses--> `CRMCandidate`  [INFERRED]
  tests/test_filter.py → app/models/crm.py
- `TestF02GDPR` --uses--> `CRMCandidate`  [INFERRED]
  tests/test_filter.py → app/models/crm.py
- `TestF03Implementacion` --uses--> `CRMCandidate`  [INFERRED]
  tests/test_filter.py → app/models/crm.py
- `TestF04Presupuesto` --uses--> `CRMCandidate`  [INFERRED]
  tests/test_filter.py → app/models/crm.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **FastAPI Runtime Stack** — requirements_fastapi, requirements_uvicorn, requirements_starlette, requirements_pydantic, requirements_pydantic_settings [INFERRED 0.85]
- **Supabase Ecosystem** — requirements_supabase, requirements_supabase_auth, requirements_supabase_functions, requirements_postgrest, requirements_storage3, requirements_realtime, requirements_asyncpg [INFERRED 0.85]
- **LLM Provider SDKs** — requirements_openai, requirements_anthropic, requirements_httpx [INFERRED 0.85]

## Communities (19 total, 2 thin omitted)

### Community 0 - "LLM Service & Rationales"
Cohesion: 0.07
Nodes (37): ExclusionResult, Registro de un CRM excluido, con razón legible para el informe., build_user_message(), app/services/llm.py ─────────────────── Módulo de llamada a Claude con streaming, Vacía el buffer al final del stream.         Emite cualquier contenido retenido, Procesa el buffer en bucle hasta que no haya más tags completos.         Cada it, Envuelve un fragmento de texto en el evento token de la sección actual., Llama a Claude con streaming y emite eventos SSE estructurados.      Usa StreamP (+29 more)

### Community 1 - "CRM & Intake Models"
Cohesion: 0.06
Nodes (46): CRMCandidate, BaseModel, app/models/crm.py ───────────────── Modelo de datos de un CRM candidato tal como, IntakeProfile, BaseModel, app/models/intake.py ──────────────────── Schema del perfil de empresa tal como, _filter_budget(), _filter_gdpr() (+38 more)

### Community 2 - "App Config & Feedback"
Cohesion: 0.06
Nodes (43): Settings, EvaluationFeedback, BaseModel, app/models/feedback.py ─────────────────────── Payload para actualizar una fila, PilotContact, BaseModel, app/models/pilot_contact.py ──────────────────────────── Payload para registrar/, evaluate() (+35 more)

### Community 3 - "Hard-Filter Service"
Cohesion: 0.12
Nodes (17): apply_hard_filters(), FilterOutput, BaseModel, Aplica los 4 filtros de exclusión duros en cascada.      Args:         profile:, Resultado completo del motor de filtros., make_crm(), make_profile(), tests/test_filter.py ──────────────────── Tests unitarios para app/services/filt (+9 more)

### Community 4 - "Scoring Service & Tests"
Cohesion: 0.12
Nodes (20): Calcula el score ponderado de los CRMs que pasaron los filtros y los ordena., Convierte el TCO a 3 años en un score 0–10 relativo al presupuesto.      Función, score_and_rank(), tco_to_score(), make_crm(), make_filter_output(), make_profile(), Presupuesto base del perfil: 5.000 – 15.000€/año → 15.000€     Referencia 3y = 1 (+12 more)

### Community 5 - "License Pricing (Retrieval)"
Cohesion: 0.15
Nodes (6): _calculate_annual_license(), Encuentra el plan más barato que cubre el número de usuarios requerido.      Cri, Calcula el coste anual de licencias para un CRM dado el número de usuarios., _select_best_plan(), TestCalculateAnnualLicense, TestSelectBestPlan

### Community 6 - "Candidate Builder (Retrieval)"
Cohesion: 0.20
Nodes (6): _build_candidate(), _derive_review_score(), Construye el review_score (0–10) a partir de dos fuentes distintas:        - rev, Construye un CRMCandidate completo a partir de las filas de Supabase.      Usa v, TestBuildCandidate, TestDeriveReviewScore

### Community 7 - "Supabase Verification Scripts"
Cohesion: 0.48
Nodes (13): check_connection(), check_credentials(), check_crm_catalog(), check_embeddings(), check_pricing_data(), check_scoring_data(), check_tables(), fail() (+5 more)

### Community 8 - "Supabase Client Packages"
Cohesion: 0.22
Nodes (9): Asyncpg PostgreSQL Driver, PostgREST Client, PyJWT Token Library, Supabase Realtime Client, Supabase Storage Client (storage3), Supabase Python Client, Supabase Auth Subpackage, Supabase Functions Subpackage (+1 more)

### Community 9 - "FastAPI Stack & Dev Workflow"
Cohesion: 0.29
Nodes (8): Install Dependency and Freeze Requirements, Run Dev Server with Uvicorn, FastAPI Web Framework, Pydantic Data Validation, Pydantic Settings (Env Config), Python Dotenv (Env Loader), Starlette ASGI Framework, Uvicorn ASGI Server

### Community 10 - "LLM Provider SDKs"
Cohesion: 1.00
Nodes (3): Anthropic SDK, HTTPX Async HTTP Client, OpenAI SDK

## Knowledge Gaps
- **10 isolated node(s):** `Starlette ASGI Framework`, `Supabase Functions Subpackage`, `PostgREST Client`, `Supabase Storage Client (storage3)`, `Asyncpg PostgreSQL Driver` (+5 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **2 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `IntakeProfile` connect `CRM & Intake Models` to `LLM Service & Rationales`, `App Config & Feedback`, `Hard-Filter Service`, `Scoring Service & Tests`, `License Pricing (Retrieval)`, `Candidate Builder (Retrieval)`?**
  _High betweenness centrality (0.405) - this node is a cross-community bridge._
- **Why does `StreamParser` connect `LLM Service & Rationales` to `CRM & Intake Models`?**
  _High betweenness centrality (0.072) - this node is a cross-community bridge._
- **Why does `CRMCandidate` connect `CRM & Intake Models` to `LLM Service & Rationales`, `App Config & Feedback`, `Hard-Filter Service`, `Scoring Service & Tests`, `Candidate Builder (Retrieval)`?**
  _High betweenness centrality (0.070) - this node is a cross-community bridge._
- **Are the 32 inferred relationships involving `IntakeProfile` (e.g. with `ExclusionResult` and `FilterOutput`) actually correct?**
  _`IntakeProfile` has 32 INFERRED edges - model-reasoned connections that need verification._
- **Are the 17 inferred relationships involving `CRMCandidate` (e.g. with `ExclusionResult` and `FilterOutput`) actually correct?**
  _`CRMCandidate` has 17 INFERRED edges - model-reasoned connections that need verification._
- **Are the 11 inferred relationships involving `StreamParser` (e.g. with `IntakeProfile` and `ExclusionResult`) actually correct?**
  _`StreamParser` has 11 INFERRED edges - model-reasoned connections that need verification._
- **What connects `app/models/crm.py ───────────────── Modelo de datos de un CRM candidato tal como`, `app/models/feedback.py ─────────────────────── Payload para actualizar una fila`, `app/models/intake.py ──────────────────── Schema del perfil de empresa tal como` to the rest of the system?**
  _100 weakly-connected nodes found - possible documentation gaps or missing edges._