from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import evaluate

app = FastAPI(
    title="Agente CRM — Backend",
    version="0.1.0",
    description="API de evaluación de CRM para PYMES europeas"
)

# CORS — ajusta en producción
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],  # Next.js dev
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(evaluate.router, prefix="/api")


@app.get("/health")
async def health_check():
    return {"status": "ok", "version": "0.1.0"}