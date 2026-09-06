"""
security.py
Utilidades compartidas de autenticacion: hash de contrasenas y tokens JWT.
Usado por routers/auth.py y por cualquier otro router que necesite
verificar quien esta haciendo la peticion.
"""

import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from jose import JWTError, jwt
from passlib.context import CryptContext

load_dotenv()

DASHBOARD_SECRET = os.getenv("DASHBOARD_SECRET")
if not DASHBOARD_SECRET:
    raise RuntimeError(
        "Falta la variable de entorno DASHBOARD_SECRET. "
        "Definela en tu .env local, o en Environment Variables en Render. "
        "Puede ser cualquier cadena larga aleatoria."
    )

ALGORITMO = "HS256"
HORAS_EXPIRACION_TOKEN = 12

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hashear_password(password_plano: str) -> str:
    """Convierte una contrasena en texto plano a su hash bcrypt, para guardar en la BD."""
    return pwd_context.hash(password_plano)


def verificar_password(password_plano: str, password_hash: str) -> bool:
    """Compara una contrasena en texto plano contra el hash guardado en la BD."""
    return pwd_context.verify(password_plano, password_hash)


def crear_token(username: str, rol: str) -> str:
    """Genera un JWT firmado, valido por HORAS_EXPIRACION_TOKEN horas."""
    payload = {
        "sub": username,
        "rol": rol,
        "exp": datetime.now(timezone.utc) + timedelta(hours=HORAS_EXPIRACION_TOKEN),
    }
    return jwt.encode(payload, DASHBOARD_SECRET, algorithm=ALGORITMO)


def leer_token(token: str) -> dict:
    """
    Decodifica y valida un JWT. Lanza jose.JWTError si el token es invalido,
    esta vencido, o fue firmado con otro secreto.
    """
    try:
        return jwt.decode(token, DASHBOARD_SECRET, algorithms=[ALGORITMO])
    except JWTError as e:
        raise JWTError(f"Token invalido o vencido: {e}")
