from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import evaluate
import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = FastAPI(
    title="Agente CRM — Backend",
    version="0.1.0",
    description="API de evaluación de CRM para PYMES europeas"
)

# CORS — el frontend llama a /api/evaluate en su propio dominio (Vercel) y
# ese route handler reenvía servidor-a-servidor hacia aquí, así que esto no
# es parte del camino crítico. Se mantiene por si hay llamadas directas
# desde el navegador (debugging, docs de FastAPI, futuros clientes).
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",                        # Next.js dev
        "https://crm-agent-frontend-smoky.vercel.app",   # Frontend en producción
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(evaluate.router, prefix="/api")


@app.get("/health")
async def health_check():
    return {"status": "ok", "version": "0.1.0"}