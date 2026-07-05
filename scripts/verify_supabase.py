"""
scripts/verify_supabase.py
──────────────────────────
Verifica que la conexión a Supabase funciona y que las tablas
y datos del Agente CRM están disponibles.

Uso (desde la raíz del proyecto, con venv activo):
  python scripts/verify_supabase.py

Salida:
  ✓ verde → el backend puede operar contra Supabase
  ✗ rojo  → revisar credenciales, tablas o datos faltantes
"""

import os
import sys
from pathlib import Path

# Cargar .env desde la raíz del proyecto (un nivel arriba de /scripts)
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

try:
    from supabase import create_client
except ImportError:
    print("\n✗ Falta el paquete supabase. Ejecuta: pip install supabase\n")
    sys.exit(1)


# ── Configuración ─────────────────────────────────────────────────────────────

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")  # service key para acceso completo

EXPECTED_TABLES = [
    "crm_catalog",
    "crm_pricing",
    "crm_scoring",
    "crm_embeddings",
    "crm_data_quality",
]

# CRMs que deben existir (los 3 ya insertados según el estado actual)
EXPECTED_CRMS = ["HubSpot CRM", "Pipedrive", "Zoho CRM"]

# Campos obligatorios que no deben ser nulos en crm_scoring
SCORING_REQUIRED_FIELDS = [
    "learning_curve_score",
    "implementation_complexity_score",
    "lockin_risk_score",
    "support_score",
]


# ── Helpers de output ─────────────────────────────────────────────────────────

def ok(label: str, detail: str = "") -> bool:
    print(f"  \033[92m✓\033[0m {label}" + (f"  — {detail}" if detail else ""))
    return True

def fail(label: str, detail: str = "") -> bool:
    print(f"  \033[91m✗\033[0m {label}" + (f"  — {detail}" if detail else ""))
    return False

def info(msg: str):
    print(f"    \033[94m·\033[0m {msg}")

def section(title: str):
    print(f"\n{title}")


# ── Verificaciones ────────────────────────────────────────────────────────────

def check_credentials() -> bool:
    section("1. Credenciales en .env")
    url_ok = ok("SUPABASE_URL presente", SUPABASE_URL[:40] + "...") if SUPABASE_URL else fail("SUPABASE_URL ausente")
    key_ok = ok("SUPABASE_SERVICE_KEY presente", "***" + SUPABASE_KEY[-6:]) if SUPABASE_KEY else fail("SUPABASE_SERVICE_KEY ausente")
    return url_ok and key_ok


def check_connection() -> "supabase.Client | None":
    section("2. Conexión con Supabase")
    try:
        client = create_client(SUPABASE_URL, SUPABASE_KEY)
        # Ping real: query que falla si no hay conexión
        client.table("crm_catalog").select("crm_id").limit(1).execute()
        ok("Conexión establecida")
        return client
    except Exception as e:
        fail("Conexión fallida", str(e))
        return None


def check_tables(client) -> bool:
    section("3. Tablas del esquema")
    all_ok = True
    for table in EXPECTED_TABLES:
        try:
            res = client.table(table).select("*", count="exact").limit(0).execute()
            count = res.count if res.count is not None else "?"
            ok(f"'{table}'", f"{count} filas")
        except Exception as e:
            fail(f"'{table}'", str(e))
            all_ok = False
    return all_ok


def check_crm_catalog(client) -> bool:
    section("4. CRMs en crm_catalog")
    try:
        res = client.table("crm_catalog").select("name, crm_category, gdpr_compliant").execute()
        found = {r["name"] for r in res.data}
        all_ok = True

        for expected in EXPECTED_CRMS:
            if expected in found:
                row = next(r for r in res.data if r["name"] == expected)
                ok(f"'{expected}'", f"categoría: {row['crm_category']} · GDPR: {row['gdpr_compliant']}")
            else:
                fail(f"'{expected}'", "NO encontrado — revisar INSERT")
                all_ok = False

        extra = found - set(EXPECTED_CRMS)
        if extra:
            info(f"CRMs adicionales en catálogo: {', '.join(sorted(extra))}")

        return all_ok
    except Exception as e:
        fail("Consulta crm_catalog fallida", str(e))
        return False


def check_scoring_data(client) -> bool:
    section("5. Datos de scoring (crm_scoring)")
    try:
        res = client.table("crm_scoring").select(
            "crm_id, learning_curve_score, implementation_complexity_score, "
            "lockin_risk_score, support_score"
        ).execute()

        if not res.data:
            fail("crm_scoring vacío", "No hay registros de scoring")
            return False

        ok("Registros encontrados", f"{len(res.data)} CRMs con datos de scoring")

        # Verificar nulos en campos obligatorios
        all_ok = True
        for field in SCORING_REQUIRED_FIELDS:
            nulls = [r["crm_id"] for r in res.data if r.get(field) is None]
            if nulls:
                fail(f"'{field}' sin nulos", f"Nulos en: {nulls}")
                all_ok = False
            else:
                ok(f"'{field}' completo")

        # Verificar rangos válidos (0–10)
        out_of_range = []
        for r in res.data:
            for field in SCORING_REQUIRED_FIELDS:
                val = r.get(field)
                if val is not None and not (0 <= val <= 10):
                    out_of_range.append(f"{r['crm_id']}.{field}={val}")
        if out_of_range:
            fail("Scores en rango 0–10", f"Fuera de rango: {out_of_range}")
            all_ok = False
        else:
            ok("Todos los scores en rango 0–10")

        return all_ok
    except Exception as e:
        fail("Consulta crm_scoring fallida", str(e))
        return False


def check_pricing_data(client) -> bool:
    section("6. Datos de pricing (crm_pricing)")
    try:
        res = client.table("crm_pricing").select(
            "crm_id, plans, annual_price_increase_pct, pricing_last_updated"
        ).execute()

        if not res.data:
            fail("crm_pricing vacío")
            return False

        ok("Registros encontrados", f"{len(res.data)} CRMs con pricing")

        # Verificar que plans no es nulo
        missing_plans = [r["crm_id"] for r in res.data if not r.get("plans")]
        if missing_plans:
            fail("Campo 'plans' completo", f"Vacío en: {missing_plans}")
            return False
        ok("Campo 'plans' presente en todos")

        # Verificar fechas de actualización
        import datetime
        today = datetime.date.today()
        stale = []
        for r in res.data:
            if r.get("pricing_last_updated"):
                try:
                    updated = datetime.date.fromisoformat(r["pricing_last_updated"][:10])
                    days_old = (today - updated).days
                    if days_old > 30:
                        stale.append(f"{r['crm_id']} ({days_old}d)")
                except Exception:
                    pass

        if stale:
            info(f"Precios con más de 30 días sin verificar: {', '.join(stale)}")
        else:
            ok("Precios actualizados en los últimos 30 días")

        return True
    except Exception as e:
        fail("Consulta crm_pricing fallida", str(e))
        return False


def check_embeddings(client) -> bool:
    section("7. Embeddings (crm_embeddings / pgvector)")
    try:
        res = client.table("crm_embeddings").select("*", count="exact").limit(0).execute()
        count = res.count or 0

        if count == 0:
            info("crm_embeddings vacío — el pipeline n8n aún no ha insertado chunks")
            info("El scoring funcionará sin RAG hasta que n8n procese los primeros CRMs")
            return True  # no es un error bloqueante para el MVP

        ok("Chunks insertados", f"{count} embeddings en pgvector")

        # Muestra de chunk types disponibles
        sample = client.table("crm_embeddings").select("crm_id, chunk_type_id").limit(20).execute()
        chunk_types = {}
        for r in sample.data:
            crm = r["crm_id"]
            ct = r["chunk_type_id"]
            chunk_types.setdefault(crm, set()).add(ct)

        for crm_id, types in chunk_types.items():
            types_str = [str(t) for t in sorted(types)]
            info(f"{crm_id}: {', '.join(types_str)}")

        return True
    except Exception as e:
        fail("Consulta crm_embeddings fallida", str(e))
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("\n\033[1m══ VERIFICACIÓN SUPABASE — AGENTE CRM ══\033[0m")

    # Paso 1: Credenciales
    if not check_credentials():
        print("\n\033[91m✗ Faltan credenciales en .env — abortando.\033[0m\n")
        sys.exit(1)

    # Paso 2: Conexión
    client = check_connection()
    if client is None:
        print("\n\033[91m✗ Sin conexión a Supabase — verifica URL y clave.\033[0m\n")
        sys.exit(1)

    # Pasos 3–7
    results = [
        check_tables(client),
        check_crm_catalog(client),
        check_scoring_data(client),
        check_pricing_data(client),
        check_embeddings(client),
    ]

    all_ok = all(results)

    print("\n" + "═" * 44)
    if all_ok:
        print("\033[92m  ✓ VERIFICACIÓN COMPLETA — Supabase operativo\033[0m")
        print("  El backend puede conectarse y leer los datos.\n")
    else:
        print("\033[91m  ✗ HAY PROBLEMAS — revisa los ✗ anteriores\033[0m")
        print("  Corrige los errores antes de arrancar el backend.\n")

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
