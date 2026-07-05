"""
app/models/intake.py
────────────────────
Schema del perfil de empresa tal como llega desde el formulario de intake.

Todos los campos de selección múltiple son strings que coinciden
exactamente con las etiquetas del formulario. El backend hace el mapping
a valores numéricos — no el frontend. Esto evita desincronización
entre capas si el wording del formulario cambia.

Campos opcionales: solo se rellenan si la rama correspondiente
del formulario conversacional fue activada.
"""

from pydantic import BaseModel
from typing import Optional, List


class IntakeProfile(BaseModel):

    # ── Paso 1: Sector y modelo de negocio ───────────────────────────────────
    sector: str
    """
    Opciones:
      "Retail / eCommerce" | "Servicios profesionales" | "Tecnología / SaaS"
      "Manufactura" | "Salud / Farma" | "Educación" | "Inmobiliaria"
      "Hostelería / turismo" | "Otro"
    """

    modelo: str
    """
    Opciones:
      "B2B — ventas a empresas" | "B2C — ventas a consumidores" | "Mixto B2B + B2C"
    """

    # ── Paso 2: CRM actual ───────────────────────────────────────────────────
    sistema_actual: str
    """
    Opciones:
      "No usamos nada / papel" | "Excel o Google Sheets"
      "Un CRM (HubSpot, Salesforce, Zoho…)" | "Un ERP con módulo CRM"
      "Varias herramientas sin integrar"
    """

    # Ramas opcionales del paso 2
    crm_actual_nombre: Optional[str] = None      # Rama: tiene CRM
    motivo_cambio: Optional[str] = None          # Rama: tiene CRM
    datos_excel: Optional[str] = None            # Rama: usa Excel

    # ── Paso 3: Presupuesto ──────────────────────────────────────────────────
    presupuesto: str
    """
    Opciones:
      "Menos de 1.000€/año" | "1.000 – 5.000€/año" | "5.000 – 15.000€/año"
      "15.000 – 40.000€/año" | "Más de 40.000€/año" | "No tenemos límite definido"
    """

    presupuesto_flex: str
    """
    Opciones:
      "Límite rígido, no podemos superarlo"
      "Hay algo de margen (+20–30%)"
      "Flexible si el ROI está bien justificado"
    """

    # ── Paso 4: Tamaño del equipo ────────────────────────────────────────────
    empleados: str
    """
    Opciones:
      "1 – 10 personas" | "11 – 25 personas" | "26 – 50 personas"
      "51 – 100 personas" | "101 – 250 personas" | "Más de 250 personas"
    """

    usuarios_crm: str
    """
    Opciones:
      "1 – 5 usuarios" | "6 – 15 usuarios" | "16 – 30 usuarios"
      "31 – 60 usuarios" | "Más de 60 usuarios"
    """

    # ── Paso 5: Stack tecnológico ────────────────────────────────────────────
    suite: str
    """
    Opciones:
      "Google Workspace (Gmail, Drive, Calendar)"
      "Microsoft 365 (Outlook, Teams, SharePoint)"
      "Ambas" | "Ninguna / correo propio"
    """

    tools: List[str] = []
    """
    Multi-select. Valores posibles:
      "Slack / Teams (mensajería)" | "Shopify / WooCommerce (eCommerce)"
      "Stripe / PayPal (pagos)" | "Zapier / Make (automatización)"
      "Un ERP (SAP, Odoo, Navision…)" | "Herramienta de marketing (Mailchimp, Klaviyo…)"
      "Ninguna relevante"
    """

    erp_nombre: Optional[str] = None   # Rama: tiene ERP

    # ── Paso 6: Equipo técnico ───────────────────────────────────────────────
    equipo_tech: str
    """
    Opciones:
      "Sí, tenemos IT / desarrollador interno"
      "Hay alguien con perfil técnico pero no dedicado"
      "No, somos un equipo no técnico"
      "Podemos contratar un consultor externo"
    """

    # Ramas opcionales del paso 6
    it_experiencia: Optional[str] = None         # Rama: tiene IT
    consultor_presupuesto: Optional[str] = None  # Rama: puede contratar consultor

    # ── Paso 7: Contexto comercial ───────────────────────────────────────────
    clientes: str
    """
    Opciones:
      "Menos de 100" | "100 – 500" | "500 – 2.000"
      "2.000 – 10.000" | "Más de 10.000"
    """

    crecimiento: str
    """
    Opciones:
      "Estable, sin cambios significativos" | "Crecimiento moderado (+10–30%)"
      "Crecimiento rápido (+30–100%)" | "Crecimiento muy rápido (más del doble)"
      "Hemos reducido / reestructurado"
    """

    usuarios_futuros: Optional[str] = None   # Rama: crecimiento rápido
