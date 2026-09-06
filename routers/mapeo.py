"""
routers/mapeo.py
Clasificacion de cuentas contables en operativo/inversion/financiamiento -
la pieza que le falta a SAP para el metodo directo. Solo se listan las
cuentas que realmente aparecen como CONTRAPARTIDA de un movimiento de
banco/caja (no las 460 del plan completo, para no abrumar).
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from db import get_db
from routers.auth import requiere_rol, usuario_actual

router = APIRouter(prefix="/api/mapeo-cuentas", tags=["mapeo_cuentas"])


@router.get("")
def listar_cuentas_por_clasificar(db: Session = Depends(get_db), usuario: dict = Depends(usuario_actual)):
    """
    Cuentas que aparecen como contrapartida de al menos un movimiento de
    banco/caja, con su clasificacion actual (si ya tiene) y cuanto volumen
    de dinero mueve - para priorizar clasificar primero las que mas pesan.
    """
    filas = db.execute(
        text("""
            SELECT p.acct_code, p.acct_name,
                   m.categoria, m.subcategoria, m.notas,
                   COALESCE(SUM(mc.debito + mc.credito), 0) AS volumen
            FROM movimientos_caja mc
            JOIN plan_cuentas p ON p.acct_code = mc.contrapartida_acct
            LEFT JOIN mapeo_cuentas m ON m.acct_code = p.acct_code
            WHERE mc.contrapartida_acct IS NOT NULL
            GROUP BY p.acct_code, p.acct_name, m.categoria, m.subcategoria, m.notas
            ORDER BY volumen DESC
        """)
    ).mappings().all()
    return [dict(f) for f in filas]


class MapeoCuentaIn(BaseModel):
    categoria: str  # 'operativo' | 'inversion' | 'financiamiento'
    subcategoria: Optional[str] = None
    notas: Optional[str] = None


@router.post("/{acct_code}")
def clasificar_cuenta(
    acct_code: str,
    datos: MapeoCuentaIn,
    db: Session = Depends(get_db),
    usuario: dict = Depends(requiere_rol("admin", "contabilidad")),
):
    if datos.categoria not in ("operativo", "inversion", "financiamiento"):
        raise HTTPException(status_code=422, detail="categoria debe ser operativo, inversion o financiamiento.")

    existe = db.execute(text("SELECT acct_code FROM plan_cuentas WHERE acct_code = :c"), {"c": acct_code}).first()
    if not existe:
        raise HTTPException(status_code=404, detail="Cuenta no encontrada en plan_cuentas.")

    db.execute(
        text("""
            INSERT INTO mapeo_cuentas (acct_code, categoria, subcategoria, notas, actualizado_en)
            VALUES (:acct_code, :categoria, :subcategoria, :notas, now())
            ON CONFLICT (acct_code) DO UPDATE SET
                categoria = EXCLUDED.categoria,
                subcategoria = EXCLUDED.subcategoria,
                notas = EXCLUDED.notas,
                actualizado_en = now()
        """),
        {"acct_code": acct_code, "categoria": datos.categoria, "subcategoria": datos.subcategoria, "notas": datos.notas},
    )
    db.commit()
    return {"mensaje": "Cuenta clasificada."}


@router.post("/default-operativo")
def clasificar_resto_como_operativo(
    db: Session = Depends(get_db),
    usuario: dict = Depends(requiere_rol("admin")),
):
    """
    Atajo: clasifica como 'operativo' cualquier cuenta contrapartida que
    todavia no tenga clasificacion. Uso tipico: clasificas a mano las pocas
    cuentas de prestamos/activos fijos, y con esto cierras el resto de un
    solo golpe (la gran mayoria de los movimientos SI son operativos).
    """
    resultado = db.execute(
        text("""
            INSERT INTO mapeo_cuentas (acct_code, categoria, actualizado_en)
            SELECT DISTINCT mc.contrapartida_acct, 'operativo', now()
            FROM movimientos_caja mc
            WHERE mc.contrapartida_acct IS NOT NULL
              AND mc.contrapartida_acct NOT IN (SELECT acct_code FROM mapeo_cuentas)
            ON CONFLICT (acct_code) DO NOTHING
            RETURNING acct_code
        """)
    )
    filas = resultado.fetchall()
    db.commit()
    return {"mensaje": f"{len(filas)} cuentas clasificadas como operativo por defecto."}
