"""
db.py
Conexion a la base de datos Neon (PostgreSQL).

Usa la variable de entorno NEON_URL (definida en .env localmente,
o en Environment Variables dentro de Render en produccion).
"""

import os
from contextlib import contextmanager

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

load_dotenv()  # lee el archivo .env si existe (no hace nada en Render, ahi ya vienen inyectadas)

NEON_URL = os.getenv("NEON_URL")

if not NEON_URL:
    raise RuntimeError(
        "Falta la variable de entorno NEON_URL. "
        "Definela en tu archivo .env local, o en Environment Variables en Render."
    )

# Neon a veces entrega el connection string como 'postgres://' en vez de 'postgresql://'
# SQLAlchemy solo acepta el segundo formato.
if NEON_URL.startswith("postgres://"):
    NEON_URL = NEON_URL.replace("postgres://", "postgresql://", 1)

# pool_pre_ping evita errores por conexiones que Neon cierra tras un rato de inactividad
engine = create_engine(NEON_URL, pool_pre_ping=True)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """
    Dependency de FastAPI: entrega una sesion de base de datos por request
    y la cierra automaticamente al terminar.

    Uso en un endpoint:
        @app.get("/algo")
        def algo(db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_connection() -> bool:
    """Prueba rapida de conexion, usada por el endpoint de salud en main.py."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
