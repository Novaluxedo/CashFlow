"""
main.py
Backend principal del dashboard de Flujo de Efectivo - Novalum.
Corre en Render. Sirve la API y (mas adelante) el frontend estatico en /public.

Los routers reales (auth, backoffice, extractos, conciliacion, flujo_caja)
se agregan aqui a medida que se van escribiendo - por ahora solo el
endpoint de salud, para que el primer deploy en Render funcione.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from db import check_connection

app = FastAPI(
    title="Flujo de Efectivo - Novalum",
    version="0.1.0",
)

# CORS abierto por ahora (mientras el frontend y el backend se prueban por separado).
# Cuando el frontend viva en el mismo dominio de Render, esto se puede restringir.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def salud():
    """
    Endpoint de salud. Sirve para:
    - Confirmar que Render desplego correctamente.
    - Confirmar que la conexion a Neon esta viva.
    """
    return {
        "status": "ok",
        "servicio": "flujo-efectivo-novalum",
        "base_de_datos": "conectada" if check_connection() else "sin conexion",
    }


@app.get("/")
def raiz():
    """La raiz manda directo al login."""
    return FileResponse("public/login.html")


@app.get("/login.html")
def pagina_login():
    return FileResponse("public/login.html")


@app.get("/index.html")
def pagina_dashboard():
    return FileResponse("public/index.html")


@app.get("/backoffice.html")
def pagina_backoffice():
    return FileResponse("public/backoffice.html")


# Sirve cualquier otro archivo estatico dentro de public/ (por si se agregan
# imagenes, css o js sueltos mas adelante). Va al final para no tapar las
# rutas /api/* que se agregan con los routers.
app.mount("/", StaticFiles(directory="public"), name="public")


# ------------------------------------------------------------------
# Routers - se van descomentando a medida que se escriben
# ------------------------------------------------------------------
# from routers import auth, backoffice, extractos, conciliacion, flujo_caja
#
# app.include_router(auth.router)
# app.include_router(backoffice.router)
# app.include_router(extractos.router)
# app.include_router(conciliacion.router)
# app.include_router(flujo_caja.router)
