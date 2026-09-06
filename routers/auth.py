"""
routers/auth.py
Login y verificacion de sesion.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from db import get_db
from security import crear_token, leer_token, verificar_password

router = APIRouter(prefix="/api/auth", tags=["auth"])

bearer_scheme = HTTPBearer()


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    rol: str
    pendiente_setup: bool


@router.post("/login", response_model=LoginResponse)
def login(datos: LoginRequest, db: Session = Depends(get_db)):
    fila = db.execute(
        text(
            "SELECT username, password_hash, rol, activo, pendiente_setup "
            "FROM usuarios WHERE username = :username"
        ),
        {"username": datos.username},
    ).mappings().first()

    # Mensaje generico a proposito: no revelar si el problema fue el
    # usuario o la contrasena, para no facilitar enumerar usuarios validos.
    credenciales_invalidas = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Usuario o contraseña incorrectos.",
    )

    if not fila:
        raise credenciales_invalidas

    if not fila["activo"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Este usuario esta desactivado. Contacta a un Admin.",
        )

    if not verificar_password(datos.password, fila["password_hash"]):
        raise credenciales_invalidas

    token = crear_token(username=fila["username"], rol=fila["rol"])

    return LoginResponse(
        access_token=token,
        rol=fila["rol"],
        pendiente_setup=fila["pendiente_setup"],
    )


def usuario_actual(credenciales: HTTPAuthorizationCredentials = Depends(bearer_scheme)) -> dict:
    """
    Dependency para proteger cualquier endpoint de otros routers:

        from routers.auth import usuario_actual

        @router.get("/algo-protegido")
        def algo(usuario: dict = Depends(usuario_actual)):
            ...

    Retorna {"username": ..., "rol": ...} si el token es valido.
    """
    try:
        payload = leer_token(credenciales.credentials)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sesion invalida o vencida. Vuelve a iniciar sesion.",
        )
    return {"username": payload["sub"], "rol": payload["rol"]}


def requiere_rol(*roles_permitidos: str):
    """
    Factory de dependency para restringir un endpoint a ciertos roles:

        @router.post("/algo-solo-admin")
        def algo(usuario: dict = Depends(requiere_rol("admin"))):
            ...
    """
    def verificador(usuario: dict = Depends(usuario_actual)) -> dict:
        if usuario["rol"] not in roles_permitidos:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permiso para hacer esto.",
            )
        return usuario
    return verificador
