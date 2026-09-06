"""
routers/flujo_caja.py
El reporte final: cuanto efectivo confirmado entro/salio, por cuenta y
consolidado, cuanto sigue pendiente de conciliar, y que tanto "ruido"
(transferencias internas + conversion de divisas) se esta excluyendo.

Nota sobre el consolidado en una sola moneda: se usa la tasa oficial del
BCRD del ultimo dia del periodo (tabla tasas_cambio_bcrd). Si todavia no
hay tasas cargadas para ese periodo, el consolidado sale como null en vez
de inventar un numero - el detalle por cuenta (en su propia moneda) sigue
mostrandose siempre, con o sin tasa.
"""

import calendar
from datetime import date, datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from db import get_db
from routers.auth import usuario_actual

router = APIRouter(prefix="/api/flujo-caja", tags=["flujo_caja"])

MESES_ES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril", 5: "Mayo", 6: "Junio",
    7: "Julio", 8: "Agosto", 9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre",
}


def _rango_periodo(periodo: str) -> tuple[date, date]:
    """'2026-07' -> (2026-07-01, 2026-07-31)."""
    anio, mes = (int(p) for p in periodo.split("-"))
    ultimo_dia = calendar.monthrange(anio, mes)[1]
    return date(anio, mes, 1), date(anio, mes, ultimo_dia)


def _obtener_tasa_bcrd(db: Session, hasta_fecha: date) -> float | None:
    """Tasa de venta BCRD vigente mas reciente en o antes de hasta_fecha. None si no hay ninguna cargada."""
    fila = db.execute(
        text(
            "SELECT tasa_venta FROM tasas_cambio_bcrd "
            "WHERE fecha <= :fecha ORDER BY fecha DESC LIMIT 1"
        ),
        {"fecha": hasta_fecha},
    ).first()
    return float(fila[0]) if fila else None


@router.get("/periodos")
def listar_periodos(db: Session = Depends(get_db), usuario: dict = Depends(usuario_actual)):
    filas = db.execute(
        text("""
            SELECT DISTINCT date_trunc('month', fecha)::date AS mes
            FROM movimientos_banco
            ORDER BY mes DESC
        """)
    ).all()
    return [
        {"valor": f"{fila[0].year}-{fila[0].month:02d}", "etiqueta": f"{MESES_ES[fila[0].month]} {fila[0].year}"}
        for fila in filas
    ]


@router.get("")
def flujo_caja(
    periodo: str = Query(..., description="Formato YYYY-MM, ej. 2026-07"),
    db: Session = Depends(get_db),
    usuario: dict = Depends(usuario_actual),
):
    inicio, fin = _rango_periodo(periodo)
    tasa_bcrd = _obtener_tasa_bcrd(db, fin)

    # ---------- Flujo confirmado, por cuenta ----------
    por_cuenta_filas = db.execute(
        text("""
            SELECT c.id, COALESCE(c.alias, b.nombre || ' ' || c.numero_cuenta) AS alias, c.moneda,
                   COALESCE(SUM(m.credito), 0) AS entradas,
                   COALESCE(SUM(m.debito), 0) AS salidas
            FROM cuentas_bancarias c
            JOIN catalogo_bancos b ON b.codigo = c.banco_codigo
            LEFT JOIN movimientos_banco m
                   ON m.cuenta_id = c.id
                  AND m.estado_conciliacion = 'confirmado'
                  AND m.fecha BETWEEN :inicio AND :fin
            WHERE c.activa = TRUE
            GROUP BY c.id, c.alias, b.nombre, c.numero_cuenta, c.moneda
            ORDER BY c.moneda, alias
        """),
        {"inicio": inicio, "fin": fin},
    ).mappings().all()

    por_cuenta = [
        {
            "alias": f["alias"],
            "moneda": f["moneda"],
            "entradas": float(f["entradas"]),
            "salidas": float(f["salidas"]),
            "neto": float(f["entradas"]) - float(f["salidas"]),
        }
        for f in por_cuenta_filas
    ]

    # ---------- Consolidado (si hay tasa BCRD disponible) ----------
    def _convertir_a_rd(monto: float, moneda: str) -> float | None:
        if moneda == "RD$":
            return monto
        if tasa_bcrd is None:
            return None
        return monto * tasa_bcrd

    entradas_convertidas = [_convertir_a_rd(c["entradas"], c["moneda"]) for c in por_cuenta]
    salidas_convertidas = [_convertir_a_rd(c["salidas"], c["moneda"]) for c in por_cuenta]

    total_entradas_rd = None if any(v is None for v in entradas_convertidas) else sum(entradas_convertidas)
    total_salidas_rd = None if any(v is None for v in salidas_convertidas) else sum(salidas_convertidas)

    # ---------- Por conciliar (confiabilidad del dato) ----------
    pendientes = db.execute(
        text("""
            SELECT c.moneda, COUNT(*) AS cantidad,
                   COALESCE(SUM(GREATEST(m.debito, m.credito)), 0) AS monto
            FROM movimientos_banco m
            JOIN cuentas_bancarias c ON c.id = m.cuenta_id
            WHERE m.estado_conciliacion = 'por_conciliar'
              AND m.fecha BETWEEN :inicio AND :fin
            GROUP BY c.moneda
        """),
        {"inicio": inicio, "fin": fin},
    ).mappings().all()

    items_por_conciliar = sum(p["cantidad"] for p in pendientes)
    montos_pendientes_rd = [_convertir_a_rd(float(p["monto"]), p["moneda"]) for p in pendientes]
    total_por_conciliar_rd = (
        None if any(v is None for v in montos_pendientes_rd) else sum(montos_pendientes_rd)
    )

    items_confirmados = db.execute(
        text("""
            SELECT COUNT(*) FROM movimientos_banco
            WHERE estado_conciliacion = 'confirmado' AND fecha BETWEEN :inicio AND :fin
        """),
        {"inicio": inicio, "fin": fin},
    ).scalar()

    total_items_periodo = items_confirmados + items_por_conciliar
    porcentaje_confirmado = (
        round(100 * items_confirmados / total_items_periodo, 1) if total_items_periodo else 100.0
    )

    # ---------- Movimientos excluidos (transparencia) ----------
    excluidos_filas = db.execute(
        text("""
            SELECT m.fecha, m.estado_conciliacion AS categoria, m.descripcion,
                   GREATEST(m.debito, m.credito) AS monto, c.moneda
            FROM movimientos_banco m
            JOIN cuentas_bancarias c ON c.id = m.cuenta_id
            WHERE m.estado_conciliacion IN ('transferencia_interna', 'conversion_divisas')
              AND m.fecha BETWEEN :inicio AND :fin
            ORDER BY m.fecha DESC
        """),
        {"inicio": inicio, "fin": fin},
    ).mappings().all()

    excluidos = [
        {
            "fecha": str(f["fecha"]),
            "categoria": f["categoria"],
            "descripcion": f["descripcion"],
            "monto": float(f["monto"]),
            "moneda": f["moneda"],
        }
        for f in excluidos_filas
    ]

    return {
        "periodo": periodo,
        "tasa_bcrd_usada": tasa_bcrd,
        "total_entradas_rd": total_entradas_rd,
        "total_salidas_rd": total_salidas_rd,
        "total_por_conciliar_rd": total_por_conciliar_rd,
        "items_por_conciliar": items_por_conciliar,
        "porcentaje_confirmado": porcentaje_confirmado,
        "por_cuenta": por_cuenta,
        "excluidos": excluidos,
    }


@router.get("/metodo-directo")
def flujo_metodo_directo(
    periodo: str = Query(..., description="Formato YYYY-MM"),
    db: Session = Depends(get_db),
    usuario: dict = Depends(usuario_actual),
):
    """
    Flujo de efectivo por metodo directo, usando SAP (movimientos_caja):
    cada linea de banco/caja se clasifica segun la categoria de su cuenta
    CONTRAPARTIDA (mapeo_cuentas). Entrada = la linea de banco fue debito
    (el efectivo aumento). Salida = fue credito (el efectivo disminuyo).
    """
    inicio, fin = _rango_periodo(periodo)

    filas = db.execute(
        text("""
            SELECT
                COALESCE(m.categoria, 'sin_clasificar') AS categoria,
                COALESCE(m.subcategoria, p.acct_name) AS subcategoria,
                SUM(mc.debito) AS entradas,
                SUM(mc.credito) AS salidas
            FROM movimientos_caja mc
            LEFT JOIN plan_cuentas p ON p.acct_code = mc.contrapartida_acct
            LEFT JOIN mapeo_cuentas m ON m.acct_code = mc.contrapartida_acct
            WHERE mc.fecha BETWEEN :inicio AND :fin
            GROUP BY COALESCE(m.categoria, 'sin_clasificar'), COALESCE(m.subcategoria, p.acct_name)
            ORDER BY categoria, subcategoria
        """),
        {"inicio": inicio, "fin": fin},
    ).mappings().all()

    secciones = {"operativo": [], "inversion": [], "financiamiento": [], "sin_clasificar": []}
    totales = {"operativo": 0.0, "inversion": 0.0, "financiamiento": 0.0, "sin_clasificar": 0.0}

    for f in filas:
        entradas = float(f["entradas"] or 0)
        salidas = float(f["salidas"] or 0)
        neto = entradas - salidas
        secciones[f["categoria"]].append({
            "subcategoria": f["subcategoria"],
            "entradas": entradas,
            "salidas": salidas,
            "neto": neto,
        })
        totales[f["categoria"]] += neto

    return {
        "periodo": periodo,
        "operativo": {"lineas": secciones["operativo"], "neto": totales["operativo"]},
        "inversion": {"lineas": secciones["inversion"], "neto": totales["inversion"]},
        "financiamiento": {"lineas": secciones["financiamiento"], "neto": totales["financiamiento"]},
        "sin_clasificar": {"lineas": secciones["sin_clasificar"], "neto": totales["sin_clasificar"]},
        "neto_total": sum(totales.values()),
        "items_sin_clasificar": len(secciones["sin_clasificar"]),
    }


@router.get("/tendencia")
def tendencia_mensual(db: Session = Depends(get_db), usuario: dict = Depends(usuario_actual)):
    """
    Serie mensual de entradas/salidas confirmadas por cuenta, para graficar
    la tendencia (no solo la foto de un mes). No hace conversion de moneda -
    el frontend debe agrupar/graficar por separado segun c.moneda.
    """
    filas = db.execute(
        text("""
            SELECT date_trunc('month', m.fecha)::date AS mes,
                   COALESCE(c.alias, b.nombre || ' ' || c.numero_cuenta) AS alias,
                   c.moneda,
                   COALESCE(SUM(m.credito), 0) AS entradas,
                   COALESCE(SUM(m.debito), 0) AS salidas
            FROM movimientos_banco m
            JOIN cuentas_bancarias c ON c.id = m.cuenta_id
            JOIN catalogo_bancos b ON b.codigo = c.banco_codigo
            WHERE m.estado_conciliacion = 'confirmado'
            GROUP BY date_trunc('month', m.fecha), c.alias, b.nombre, c.numero_cuenta, c.moneda
            ORDER BY mes ASC
        """)
    ).mappings().all()

    return [
        {
            "periodo": f"{f['mes'].year}-{f['mes'].month:02d}",
            "etiqueta": f"{MESES_ES[f['mes'].month][:3]} {f['mes'].year}",
            "alias": f["alias"],
            "moneda": f["moneda"],
            "entradas": float(f["entradas"]),
            "salidas": float(f["salidas"]),
            "neto": float(f["entradas"]) - float(f["salidas"]),
        }
        for f in filas
    ]
