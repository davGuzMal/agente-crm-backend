"""
app/services/retrieval.py
─────────────────────────
Carga los datos de Supabase y construye los objetos CRMCandidate
listos para pasar a filter.py y scoring.py.

Responsabilidades:
  1. Consultar crm_catalog, crm_pricing, crm_scoring, crm_data_quality
  2. Seleccionar el plan óptimo para el nº de usuarios del intake
  3. Calcular annual_license_eur para ese plan concreto
  4. Construir CRMCandidate completo con todos los campos
  5. Búsqueda vectorial en crm_embeddings para el contexto RAG del LLM

Nota sobre async:
  El cliente supabase-py es síncrono. Las funciones están marcadas como
  async para integrarse limpiamente con FastAPI, pero las llamadas a
  Supabase corren de forma bloqueante. Para producción con alta concurrencia,
  envolver con asyncio.to_thread().
"""

import logging
from typing import List, Optional
from uuid import UUID
from supabase import create_client, Client

from app.config import settings
from app.models.crm import CRMCandidate
from app.models.intake import IntakeProfile

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTES
# ══════════════════════════════════════════════════════════════════════════════

# Usuarios CRM del intake → límite superior del rango (consistente con filter.py)
USERS_UPPER: dict[str, int] = {
    "1 – 5 usuarios":      5,
    "6 – 15 usuarios":    15,
    "16 – 30 usuarios":   30,
    "31 – 60 usuarios":   60,
    "Más de 60 usuarios": 100,
}

# Número mínimo de reviews en G2 para considerar el review_score fiable
MIN_REVIEWS_FOR_SCORE = 50

# Puntuación neutra si no hay datos suficientes
NEUTRAL_SCORE = 5.0

# Mapeo de etiquetas del intake → slugs del campo best_fit_sectors en Supabase.
#
# Cada sector del intake puede corresponderse con VARIOS slugs en la BD porque
# los slugs son más granulares (ej. "tech" + "startups" + "PLG" ≈ "Tecnología / SaaS").
# Usar listas permite absorber la heterogeneidad de la BD sin tocarla.
#
# Mantener sincronizado con los valores reales de crm_catalog.best_fit_sectors.
SECTOR_SLUG_MAP: dict[str, list[str] | None] = {
    "Tecnología / SaaS": [
        "tech", "startups", "PLG", "remote_software_engineering",
    ],
    "Servicios profesionales": [
        "servicios_profesionales", "servicios_b2b", "consultoria",
        "professional_coaching",
    ],
    "Retail / eCommerce": [
        "retail", "e-commerce",
    ],
    "Manufactura": [
        "manufactura", "manufacturing",         # "manufacturing" = typo en BD, cubre los dos
        "construction_contracting",
    ],
    "Salud / Farma": [
        "salud",
    ],
    "Finanzas / Seguros": [
        "finanzas", "venture_capital",
    ],
    "Inmobiliaria": [
        "inmobiliaria", "real_state",            # "real_state" = typo en BD, cubre los dos
        "property_development_firms", "commercial_brokerages",
    ],
    "Hostelería / turismo": [
        "hosteleria",                            # slug aún no en BD — el fallback lo cubre
    ],
    "Educación": [
        "educacion",
    ],
    "Agencias / Consultoría": [
        "agencias", "consultoria", "design_studios",
        "digital_marketing", "media_creative", "digital_media",
        "content_creators",
    ],
    "Otro": None,   # sin sector definido → incluir todos los CRMs
}

# Slugs que describen tipo de empresa o modelo de negocio, no sector vertical.
# Un CRM cuyos best_fit_sectors sean SOLO estos slugs se trata como universal
# (aplica a cualquier sector si el modelo del intake es B2B).
CROSS_CUTTING_SLUGS: frozenset[str] = frozenset({
    "B2B", "sales", "startups", "PLG",
})


# ══════════════════════════════════════════════════════════════════════════════
# PREFILTRO DE SECTOR
# ══════════════════════════════════════════════════════════════════════════════

def _sector_matches(catalog: dict, intake_sector: str) -> bool:
    """
    Devuelve True si este CRM debe evaluarse para el sector del intake.

    Usa best_fit_sectors de crm_catalog (slugs cortos) y SECTOR_SLUG_MAP
    para traducir el sector del intake al conjunto de slugs relevantes.

    Reglas en orden de evaluación:
      1. best_fit_sectors vacío/null → CRM universal, siempre True.
      2. best_fit_sectors contiene SOLO slugs cruzados (B2B, sales…) → universal.
      3. Sector del intake es "Otro" o sin slugs definidos → True (sin filtrar).
      4. Intersección entre slugs del intake y best_fit_sectors → True si hay match.
         False si no hay ningún slug en común.
    """
    crm_slugs: set[str] = set(catalog.get("best_fit_sectors") or [])

    # Regla 1: sin restricción sectorial definida
    if not crm_slugs:
        return True

    # Regla 2: solo slugs cruzados → el CRM aplica a cualquier sector B2B
    if crm_slugs.issubset(CROSS_CUTTING_SLUGS):
        return True

    # Regla 3: sector del intake sin definición de slugs
    intake_slugs = SECTOR_SLUG_MAP.get(intake_sector)
    if intake_slugs is None:
        return True  # "Otro" o sector desconocido

    # Regla 4: intersección
    return bool(crm_slugs & set(intake_slugs))


# ══════════════════════════════════════════════════════════════════════════════
# CLIENTE SUPABASE
# ══════════════════════════════════════════════════════════════════════════════

def _get_client() -> Client:
    """
    Crea un cliente Supabase con la service key (acceso completo).
    Se crea por llamada — no es un singleton para facilitar el testing.
    """
    return create_client(settings.supabase_url, settings.supabase_service_key)


# ══════════════════════════════════════════════════════════════════════════════
# SELECCIÓN DE PLAN Y CÁLCULO DE LICENCIA ANUAL
# ══════════════════════════════════════════════════════════════════════════════

def _select_best_plan(plans: list, num_users: int) -> Optional[dict]:
    """
    Encuentra el plan más barato que cubre el número de usuarios requerido.

    Criterio de calificación: users_max >= num_users, o users_max es None (ilimitado).

    Modelos de facturación:
      - "per_user": coste mensual = price_eur_month × num_users
      - "flat":     coste mensual = price_eur_month (fijo, sin importar usuarios)

    Devuelve el plan con menor coste mensual estimado, o None si ninguno
    puede cubrir el equipo.

    Args:
        plans:     Array de planes de crm_pricing.plans (jsonb[])
        num_users: Número de usuarios del intake

    Returns:
        Diccionario del plan seleccionado con campo adicional "_monthly_cost",
        o None si no hay planes que cubran el número de usuarios.
    """
    qualifying = []

    for plan in plans:
        users_max = plan.get("users_max")

        # El plan no cubre el equipo → descartar
        if users_max is not None and users_max < num_users:
            continue

        billing = plan.get("billing", "per_user")
        price = float(plan.get("price_eur_month", 0))

        if billing == "per_user":
            monthly_cost = price * num_users
        else:  # "flat"
            monthly_cost = price

        qualifying.append({**plan, "_monthly_cost": monthly_cost})

    if not qualifying:
        return None

    return min(qualifying, key=lambda p: p["_monthly_cost"])


def _calculate_annual_license(pricing_row: dict, num_users: int) -> Optional[float]:
    """
    Calcula el coste anual de licencias para un CRM dado el número de usuarios.

    Aplica el descuento por facturación anual si está disponible.
    Asume siempre facturación anual (que es el caso habitual en B2B).

    Args:
        pricing_row: Fila completa de crm_pricing para un CRM.
        num_users:   Número de usuarios del intake.

    Returns:
        Coste anual en EUR, o None si no hay planes válidos para el equipo.
    """
    plans = pricing_row.get("plans") or []

    if not plans:
        logger.warning("pricing_row sin campo 'plans' o vacío")
        return None

    best_plan = _select_best_plan(plans, num_users)

    if best_plan is None:
        logger.warning(
            f"Ningún plan cubre {num_users} usuarios para este CRM"
        )
        return None

    monthly_cost = best_plan["_monthly_cost"]
    discount_pct = float(pricing_row.get("discount_annual_pct") or 0)
    annual = monthly_cost * 12 * (1 - discount_pct / 100)

    return round(annual, 2)


# ══════════════════════════════════════════════════════════════════════════════
# DERIVACIÓN DE REVIEW SCORE
# ══════════════════════════════════════════════════════════════════════════════

def _derive_review_score(
    quality_row: dict,
    sector_avg_rating: Optional[float],
) -> Optional[float]:
    """
    Construye el review_score (0–10) a partir de dos fuentes distintas:

      - review_count_g2:   total de reseñas G2 del CRM → viene de crm_data_quality.
                           Se usa para el umbral mínimo de fiabilidad.
      - sector_avg_rating: rating medio del sector específico del intake → viene de
                           crm_embeddings.metadata (chunk_type_id=1, review_summary_sector).
                           Se pasa como parámetro porque load_crm_candidates() lo
                           obtiene con una query separada antes de construir candidatos.

    Si no hay avg_rating para el sector del intake o hay pocas reseñas globales,
    devuelve None → scoring.py usará 5.0 como fallback y marcará confianza "medium".
    """
    review_count = quality_row.get("review_count_g2") or 0

    if sector_avg_rating is None or review_count < MIN_REVIEWS_FOR_SCORE:
        return None

    # Normalizar rating G2 de escala 1–5 a 0–10
    normalized = (float(sector_avg_rating) - 1) / 4 * 10

    # Penalización por volumen bajo (entre MIN_REVIEWS_FOR_SCORE y 200 reseñas)
    if review_count < 200:
        volume_factor = (
            0.85
            + (review_count - MIN_REVIEWS_FOR_SCORE)
            / (200 - MIN_REVIEWS_FOR_SCORE)
            * 0.15
        )
        normalized *= volume_factor

    return round(max(0.0, min(10.0, normalized)), 2)


# ══════════════════════════════════════════════════════════════════════════════
# CONSTRUCCIÓN DE CRMCandidate
# ══════════════════════════════════════════════════════════════════════════════

def _build_candidate(
    crm_id: str,
    catalog: dict,
    pricing: dict,
    scoring: dict,
    quality: dict,
    annual_license: float,
    sector_avg_rating: Optional[float] = None,
) -> CRMCandidate:
    """
    Construye un CRMCandidate completo a partir de las filas de Supabase.

    Usa valores por defecto conservadores cuando un campo está ausente,
    de forma que el CRM siga siendo evaluable aunque los datos no estén completos.
    Los valores por defecto generarán scoring_confidence = "medium" o "low".
    """
    return CRMCandidate(
        # ── Identidad ─────────────────────────────────────────────────────────
        crm_id=crm_id,
        name=catalog.get("name", ""),
        crm_category=catalog.get("crm_category", "generalista"),

        # ── Límites de equipo ─────────────────────────────────────────────────
        best_fit_team_size_min=catalog.get("best_fit_team_size_min", 1),
        best_fit_team_size_max=catalog.get("best_fit_team_size_max", 999),

        # ── Compliance ────────────────────────────────────────────────────────
        gdpr_compliant=catalog.get("gdpr_compliant", False),
        data_hosting_regions=catalog.get("data_hosting_regions") or [],

        # ── Implementación ────────────────────────────────────────────────────
        avg_implementation_weeks=float(scoring.get("avg_implementation_weeks") or 4),
        requires_external_consultant=scoring.get("requires_external_consultant") or "optional",

        # ── Precio calculado ──────────────────────────────────────────────────
        annual_license_eur=annual_license,

        # ── Campos TCO ────────────────────────────────────────────────────────
        annual_price_increase_pct=float(pricing.get("annual_price_increase_pct") or 0.07),
        price_per_contact=bool(pricing.get("price_per_contact", False)),
        contact_tier_thresholds=pricing.get("contact_tier_thresholds"),
        implementation_cost_est_eur=pricing.get("implementation_cost_est_eur") or {},
        training_cost_est_eur=pricing.get("training_cost_est_eur") or {},
        migration_cost_est_eur=pricing.get("migration_cost_est_eur") or {},
        price_lock_policy=scoring.get("price_lock_policy"),

        # ── Scores base ───────────────────────────────────────────────────────
        learning_curve_score=scoring.get("learning_curve_score"),
        implementation_complexity_score=scoring.get("implementation_complexity_score"),
        lockin_risk_score=scoring.get("lockin_risk_score"),
        support_score=scoring.get("support_score"),
        review_score=_derive_review_score(quality, sector_avg_rating),

        # ── Soporte e integraciones ───────────────────────────────────────────
        native_integrations=scoring.get("native_integrations") or [],
        support_spanish_available=bool(scoring.get("support_spanish_available", False)),

        # ── Metadatos de calidad ──────────────────────────────────────────────
        scoring_confidence=quality.get("scoring_confidence"),
        pricing_last_updated=str(pricing.get("pricing_last_updated") or ""),
    )


# ══════════════════════════════════════════════════════════════════════════════
# CARGA PRINCIPAL DE CANDIDATOS
# ══════════════════════════════════════════════════════════════════════════════

def load_crm_candidates(profile: IntakeProfile) -> List[CRMCandidate]:
    """
    Carga todos los CRMs del catálogo desde Supabase y construye
    los CRMCandidate con annual_license_eur calculado para el perfil.

    Combina cuatro tablas en memoria (los volúmenes son pequeños — 15 CRMs):
      crm_catalog + crm_pricing + crm_scoring + crm_data_quality

    CRMs sin planes de pricing válidos para el equipo son omitidos con
    un log de warning. No generan error — simplemente no entran al filtrado.

    Args:
        profile: Perfil del intake con el nº de usuarios y demás campos.

    Returns:
        Lista de CRMCandidate listos para pasar a apply_hard_filters().

    Raises:
        RuntimeError: Si la conexión a Supabase falla completamente.
    """
    num_users = USERS_UPPER.get(profile.usuarios_crm, 15)

    try:
        client = _get_client()

        # Cargar las cuatro tablas en paralelo de facto
        # (Supabase Python client es síncrono — cuatro llamadas secuenciales)
        catalog_rows  = client.table("crm_catalog").select("*").execute().data
        pricing_rows  = client.table("crm_pricing").select("*").execute().data
        scoring_rows  = client.table("crm_scoring").select("*").execute().data
        quality_rows  = client.table("crm_data_quality").select("*").execute().data

        # Obtener avg_rating sector-específico desde crm_embeddings.
        # chunk_type_id=1 = review_summary_sector (ver tabla chunk_types).
        # Filtramos en Python porque el campo sector está dentro del jsonb metadata.
        review_chunks = (
            client.table("crm_embeddings")
            .select("crm_id, metadata")
            .eq("chunk_type_id", 1)
            .execute()
            .data
        )

    except Exception as exc:
        raise RuntimeError(
            f"No se pudo conectar con Supabase: {exc}. "
            "Verifica las credenciales en .env y ejecuta verify_supabase.py."
        ) from exc

    # Indexar por crm_id para O(1) lookup
    pricing_by_id = {r["crm_id"]: r for r in pricing_rows}
    scoring_by_id = {r["crm_id"]: r for r in scoring_rows}
    quality_by_id = {r["crm_id"]: r for r in quality_rows}

    # Construir índice de avg_rating por CRM para el sector del intake.
    # Un mismo CRM puede tener varios chunks para el mismo sector (distintas
    # tandas de scraping G2/Capterra, con esquemas de metadata ligeramente
    # distintos). company_size en el metadata NO es una dimensión limpia para
    # desambiguar — es una lista de rangos mezclados que describe la mezcla de
    # tamaños de empresa detrás de ese avg_rating, no una etiqueta por chunk.
    # En vez de quedarnos con el primer chunk encontrado (arbitrario, depende
    # del orden no garantizado de la query), promediamos ponderando por
    # review_count: un chunk con 847 reseñas debe pesar más que uno con 14.
    intake_slugs = SECTOR_SLUG_MAP.get(profile.sector) or []
    primary_slug = intake_slugs[0] if intake_slugs else None

    _rating_weighted_sum: dict[str, float] = {}
    _rating_weight_total: dict[str, float] = {}

    for row in review_chunks:
        meta = row.get("metadata") or {}
        if meta.get("sector") == primary_slug and "avg_rating" in meta:
            crm_id_r = row.get("crm_id")
            if not crm_id_r:
                continue
            # Fallback a peso 1 si algún chunk no trae review_count.
            weight = float(meta.get("review_count") or 1)
            _rating_weighted_sum[crm_id_r] = (
                _rating_weighted_sum.get(crm_id_r, 0.0) + float(meta["avg_rating"]) * weight
            )
            _rating_weight_total[crm_id_r] = _rating_weight_total.get(crm_id_r, 0.0) + weight

    avg_rating_by_crm: dict[str, float] = {
        crm_id_r: _rating_weighted_sum[crm_id_r] / _rating_weight_total[crm_id_r]
        for crm_id_r in _rating_weighted_sum
    }

    if primary_slug:
        logger.debug(
            f"avg_rating sector '{primary_slug}' encontrado para: "
            f"{list(avg_rating_by_crm.keys())}"
        )
    else:
        logger.debug("Sin slug primario para sector del intake — review_score usará fallback")

    candidates: List[CRMCandidate] = []

    for catalog in catalog_rows:
        crm_id = catalog["crm_id"]

        # Prefiltro de sector: descartar CRMs de nicho que no aplican al sector
        if not _sector_matches(catalog, profile.sector):
            logger.debug(
                f"[{crm_id}] Omitido — no aplica al sector '{profile.sector}'"
            )
            continue

        pricing = pricing_by_id.get(crm_id, {})
        scoring = scoring_by_id.get(crm_id, {})
        quality = quality_by_id.get(crm_id, {})

        # Calcular licencia anual para el nº de usuarios del intake
        annual_license = _calculate_annual_license(pricing, num_users)

        if annual_license is None:
            logger.warning(
                f"[{crm_id}] Sin plan válido para {num_users} usuarios — omitido del filtrado"
            )
            continue

        candidate = _build_candidate(
            crm_id, catalog, pricing, scoring, quality, annual_license,
            sector_avg_rating=avg_rating_by_crm.get(crm_id),
        )
        candidates.append(candidate)

    logger.info(f"Cargados {len(candidates)} CRMs candidatos (de {len(catalog_rows)} en catálogo)")
    return candidates


# ══════════════════════════════════════════════════════════════════════════════
# BÚSQUEDA VECTORIAL (RAG)
# ══════════════════════════════════════════════════════════════════════════════

def search_semantic_context(
    crm_ids: List[UUID],
    profile: IntakeProfile,
    top_k: int = 2,
) -> List[dict]:
    """
    Búsqueda vectorial en crm_embeddings para recuperar el contexto semántico
    relevante al perfil. El resultado se inyecta en el prompt del LLM.

    Requiere que la función RPC `match_crm_chunks` esté creada en Supabase
    (ver SQL en docs/supabase_functions.sql) y que OPENAI_API_KEY esté
    configurada en .env para generar el embedding de consulta.

    Si alguna condición no se cumple, devuelve lista vacía con un log
    de advertencia — el veredicto se genera sin contexto RAG pero no falla.

    Args:
        crm_ids: IDs de los CRMs que pasaron los filtros (para filtrar chunks).
        profile: Perfil del intake (construye la query semántica).
        top_k:   Chunks a recuperar por CRM.

    Returns:
        Lista de chunks con campos: crm_id, chunk_type, content, similarity.
    """
    query_text = _build_profile_query(profile)

    try:
        embedding = _generate_embedding(query_text)
    except Exception as exc:
        logger.warning(
            f"No se pudo generar el embedding para RAG: {exc}. "
            "El veredicto se generará sin contexto semántico."
        )
        return []

    try:
        client = _get_client()
        result = client.rpc(
            "match_crm_chunks",
            {
                "query_embedding": embedding,
                "filter_crm_id":   crm_ids,
                "match_count":     top_k,
            },
        ).execute()
        return result.data or []

    except Exception as exc:
        logger.warning(f"Búsqueda vectorial fallida: {exc}. Continuando sin RAG.")
        return []


def _build_profile_query(profile: IntakeProfile) -> str:
    """
    Construye el texto de consulta semántica a partir del perfil del intake.
    El texto debe capturar las dimensiones más relevantes para el filtrado
    de chunks: sector, tamaño, sistema actual y equipo técnico.
    """
    parts = [
        f"Empresa del sector {profile.sector}",
        f"modelo {profile.modelo}",
        f"con {profile.usuarios_crm} usuarios de CRM",
        f"sistema actual: {profile.sistema_actual}",
        f"stack: {profile.suite}",
        f"equipo técnico: {profile.equipo_tech}",
        f"crecimiento: {profile.crecimiento}",
    ]
    return ". ".join(parts)


def _generate_embedding(text: str) -> List[float]:
    """
    Genera un embedding de texto usando OpenAI text-embedding-3-small.

    IMPORTANTE: El modelo debe coincidir con el usado en el pipeline n8n
    que insertó los chunks en crm_embeddings. Si n8n usó un modelo distinto,
    actualiza este método para usar el mismo modelo.

    Requiere OPENAI_API_KEY en .env y el paquete openai instalado:
      pip install openai

    Raises:
        ImportError:  Si el paquete openai no está instalado.
        RuntimeError: Si OPENAI_API_KEY no está configurada.
        Exception:    Si la llamada a la API de OpenAI falla.
    """
    try:
        import openai
    except ImportError as exc:
        raise ImportError(
            "Falta el paquete 'openai'. Instálalo con: pip install openai"
        ) from exc

    api_key = settings.openai_api_key if hasattr(settings, "openai_api_key") else None
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY no configurada en .env. "
            "Necesaria para la búsqueda vectorial RAG."
        )

    client = openai.OpenAI(api_key=api_key)
    response = client.embeddings.create(
        model="text-embedding-3-small",
        input=text,
    )
    return response.data[0].embedding