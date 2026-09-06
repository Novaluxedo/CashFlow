"""
routers/backoffice.py
Catalogo de bancos (solo lectura) y CRUD de cuentas bancarias.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from db import get_db
from routers.auth import requiere_rol, usuario_actual

router = APIRouter(prefix="/api", tags=["backoffice"])


# ============================================================
# Catalogo de bancos (solo lectura - se pobla desde el schema)
# ============================================================
@router.get("/catalogo-bancos")
def listar_catalogo_bancos(db: Session = Depends(get_db), usuario: dict = Depends(usuario_actual)):
    filas = db.execute(
        text("SELECT codigo, nombre, tipo_entidad FROM catalogo_bancos ORDER BY nombre")
    ).mappings().all()
    return [dict(f) for f in filas]


# ============================================================
# Cuentas bancarias
# ============================================================
class CuentaBancariaIn(BaseModel):
    banco_codigo: str
    numero_cuenta: str
    moneda: str
    alias: Optional[str] = None


@router.get("/cuentas-bancarias")
def listar_cuentas(db: Session = Depends(get_db), usuario: dict = Depends(usuario_actual)):
    filas = db.execute(
        text(
            "SELECT c.id, c.numero_cuenta, c.moneda, c.alias, c.activa, "
            "       b.nombre AS banco_nombre, b.codigo AS banco_codigo "
            "FROM cuentas_bancarias c "
            "JOIN catalogo_bancos b ON b.codigo = c.banco_codigo "
            "WHERE c.activa = TRUE "
            "ORDER BY b.nombre, c.numero_cuenta"
        )
    ).mappings().all()
    return [dict(f) for f in filas]


@router.post("/cuentas-bancarias", status_code=status.HTTP_201_CREATED)
def crear_cuenta(
    datos: CuentaBancariaIn,
    db: Session = Depends(get_db),
    usuario: dict = Depends(requiere_rol("admin", "contabilidad")),
):
    if datos.moneda not in ("USD", "RD$"):
        raise HTTPException(status_code=422, detail="Moneda debe ser 'USD' o 'RD$'.")

    banco = db.execute(
        text("SELECT codigo FROM catalogo_bancos WHERE codigo = :c"),
        {"c": datos.banco_codigo},
    ).first()
    if not banco:
        raise HTTPException(status_code=404, detail="Banco no encontrado en el catalogo.")

    existente = db.execute(
        text("SELECT id FROM cuentas_bancarias WHERE numero_cuenta = :n"),
        {"n": datos.numero_cuenta},
    ).first()
    if existente:
        raise HTTPException(status_code=409, detail="Ya existe una cuenta con ese numero.")

    fila = db.execute(
        text(
            "INSERT INTO cuentas_bancarias (banco_codigo, numero_cuenta, moneda, alias, creado_por) "
            "VALUES (:banco_codigo, :numero_cuenta, :moneda, :alias, :creado_por) "
            "RETURNING id"
        ),
        {
            "banco_codigo": datos.banco_codigo,
            "numero_cuenta": datos.numero_cuenta,
            "moneda": datos.moneda,
            "alias": datos.alias,
            "creado_por": usuario["username"],
        },
    ).mappings().first()
    db.commit()
    return {"id": fila["id"], "mensaje": "Cuenta creada correctamente."}


@router.delete("/cuentas-bancarias/{cuenta_id}")
def desactivar_cuenta(
    cuenta_id: int,
    db: Session = Depends(get_db),
    usuario: dict = Depends(requiere_rol("admin")),
):
    resultado = db.execute(
        text("UPDATE cuentas_bancarias SET activa = FALSE WHERE id = :id RETURNING id"),
        {"id": cuenta_id},
    ).first()
    if not resultado:
        raise HTTPException(status_code=404, detail="Cuenta no encontrada.")
    db.commit()
    return {"mensaje": "Cuenta desactivada."}
