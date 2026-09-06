"""
routers/conciliacion.py
Modulo "Por conciliar": listar movimientos pendientes y resolverlos.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from db import get_db
from routers.auth import requiere_rol, usuario_actual

router = APIRouter(prefix="/api/conciliacion", tags=["conciliacion"])

ESTADOS_VALIDOS = ("confirmado", "transferencia_interna", "conversion_divisas", "por_conciliar")


@router.get("")
def listar_conciliacion(
    estado: str = "por_conciliar",
    db: Session = Depends(get_db),
    usuario: dict = Depends(usuario_actual),
):
    filas = db.execute(
        text("""
            SELECT m.id, m.fecha, m.descripcion, m.codigo_movimiento,
                   m.debito, m.credito, m.sugerencia_auto,
                   c.moneda, COALESCE(c.alias, b.nombre || ' ' || c.numero_cuenta) AS cuenta_alias
            FROM movimientos_banco m
            JOIN cuentas_bancarias c ON c.id = m.cuenta_id
            JOIN catalogo_bancos b ON b.codigo = c.banco_codigo
            WHERE m.estado_conciliacion = :estado
            ORDER BY m.fecha DESC
        """),
        {"estado": estado},
    ).mappings().all()
    return [dict(f) for f in filas]


class ResolverRequest(BaseModel):
    estado_conciliacion: str
    nota_resolucion: Optional[str] = None


@router.post("/{movimiento_id}/resolver")
def resolver_item(
    movimiento_id: int,
    datos: ResolverRequest,
    db: Session = Depends(get_db),
    usuario: dict = Depends(requiere_rol("admin", "contabilidad")),
):
    if datos.estado_conciliacion not in ESTADOS_VALIDOS:
        raise HTTPException(status_code=422, detail=f"Estado invalido. Usa uno de: {ESTADOS_VALIDOS}")

    fila = db.execute(
        text("SELECT id, resuelto_por FROM movimientos_banco WHERE id = :id"),
        {"id": movimiento_id},
    ).mappings().first()
    if not fila:
        raise HTTPException(status_code=404, detail="Movimiento no encontrado.")

    # Solo Admin puede revertir algo que ya habia sido resuelto por alguien mas.
    if fila["resuelto_por"] and usuario["rol"] != "admin":
        raise HTTPException(
            status_code=403,
            detail="Este movimiento ya fue resuelto. Solo un Admin puede cambiarlo.",
        )

    db.execute(
        text("""
            UPDATE movimientos_banco
            SET estado_conciliacion = :estado,
                nota_resolucion = :nota,
                resuelto_por = :usuario,
                resuelto_en = :ahora
            WHERE id = :id
        """),
        {
            "estado": datos.estado_conciliacion,
            "nota": datos.nota_resolucion,
            "usuario": usuario["username"],
            "ahora": datetime.now(timezone.utc),
            "id": movimiento_id,
        },
    )
    db.commit()
    return {"mensaje": "Movimiento actualizado."}
