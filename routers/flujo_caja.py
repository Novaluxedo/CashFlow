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
    """Tasa de venta BCRD vigente mas reciente en o antes de hasta_fecha. None si no hay ninguna cargada.
    (Ya no se usa en /api/flujo-caja - se dejo aparte por si se necesita mas adelante para otro reporte.)"""
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

    # ---------- Consolidado por moneda (sin conversion - USD y RD$ aparte) ----------
    consolidado_usd = {"entradas": 0.0, "salidas": 0.0, "neto": 0.0}
    consolidado_rd = {"entradas": 0.0, "salidas": 0.0, "neto": 0.0}
    for c in por_cuenta:
        destino = consolidado_usd if c["moneda"] == "USD" else consolidado_rd
        destino["entradas"] += c["entradas"]
        destino["salidas"] += c["salidas"]
        destino["neto"] += c["neto"]

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
    por_conciliar_por_moneda = {p["moneda"]: float(p["monto"]) for p in pendientes}

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
        "consolidado_usd": consolidado_usd,
        "consolidado_rd": consolidado_rd,
        "por_conciliar_por_moneda": por_conciliar_por_moneda,
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
    Flujo de efectivo por metodo directo, desglosado por forma de cobro/pago
    (efectivo, transferencia, tarjeta, cheque) - sin necesidad de clasificar
    cuentas a mano. Fuente: cobros/pagos de SAP.
    """
    inicio, fin = _rango_periodo(periodo)

    entradas = db.execute(
        text("""
            SELECT forma_cobro, moneda, SUM(monto) AS total, COUNT(*) AS cantidad
            FROM cobros
            WHERE fecha BETWEEN :inicio AND :fin
            GROUP BY forma_cobro, moneda
            ORDER BY total DESC
        """),
        {"inicio": inicio, "fin": fin},
    ).mappings().all()

    salidas = db.execute(
        text("""
            SELECT forma_pago, moneda, SUM(monto) AS total, COUNT(*) AS cantidad
            FROM pagos
            WHERE fecha BETWEEN :inicio AND :fin
            GROUP BY forma_pago, moneda
            ORDER BY total DESC
        """),
        {"inicio": inicio, "fin": fin},
    ).mappings().all()

    etiquetas = {
        "efectivo": "Ventas en efectivo", "transferencia": "Ventas por transferencia",
        "tarjeta": "Ventas con tarjeta", "cheque": "Cobros por cheque", "otro": "Otros cobros",
    }
    etiquetas_pago = {
        "efectivo": "Pagos en efectivo", "transferencia": "Pagos por transferencia",
        "cheque": "Pagos con cheque", "otro": "Otros pagos",
    }

    lineas_entradas = [
        {
            "etiqueta": etiquetas.get(e["forma_cobro"], e["forma_cobro"]),
            "moneda": e["moneda"], "monto": float(e["total"]), "cantidad": e["cantidad"],
        }
        for e in entradas
    ]
    lineas_salidas = [
        {
            "etiqueta": etiquetas_pago.get(s["forma_pago"], s["forma_pago"]),
            "moneda": s["moneda"], "monto": float(s["total"]), "cantidad": s["cantidad"],
        }
        for s in salidas
    ]

    return {
        "periodo": periodo,
        "entradas": lineas_entradas,
        "salidas": lineas_salidas,
        "total_entradas": sum(l["monto"] for l in lineas_entradas),
        "total_salidas": sum(l["monto"] for l in lineas_salidas),
    }


@router.get("/metodo-directo/tendencia")
def flujo_metodo_directo_tendencia(db: Session = Depends(get_db), usuario: dict = Depends(usuario_actual)):
    """
    Mismo desglose por forma de cobro/pago que /metodo-directo, pero para
    TODOS los meses con datos - pensado para pintar una tabla con los
    meses en columnas.
    """
    etiquetas = {
        "efectivo": "Ventas en efectivo", "transferencia": "Ventas por transferencia",
        "tarjeta": "Ventas con tarjeta", "cheque": "Cobros por cheque", "otro": "Otros cobros",
    }
    etiquetas_pago = {
        "efectivo": "Pagos en efectivo", "transferencia": "Pagos por transferencia",
        "cheque": "Pagos con cheque", "otro": "Otros pagos",
    }

    entradas = db.execute(
        text("""
            SELECT date_trunc('month', fecha)::date AS mes, forma_cobro, moneda, SUM(monto) AS total
            FROM cobros
            GROUP BY date_trunc('month', fecha), forma_cobro, moneda
        """)
    ).mappings().all()

    salidas = db.execute(
        text("""
            SELECT date_trunc('month', fecha)::date AS mes, forma_pago, moneda, SUM(monto) AS total
            FROM pagos
            GROUP BY date_trunc('month', fecha), forma_pago, moneda
        """)
    ).mappings().all()

    filas = []
    for e in entradas:
        filas.append({
            "periodo": f"{e['mes'].year}-{e['mes'].month:02d}",
            "etiqueta": f"{MESES_ES[e['mes'].month][:3]} {e['mes'].year}",
            "tipo": "entrada",
            "linea": etiquetas.get(e["forma_cobro"], e["forma_cobro"]),
            "moneda": e["moneda"],
            "monto": float(e["total"]),
        })
    for s in salidas:
        filas.append({
            "periodo": f"{s['mes'].year}-{s['mes'].month:02d}",
            "etiqueta": f"{MESES_ES[s['mes'].month][:3]} {s['mes'].year}",
            "tipo": "salida",
            "linea": etiquetas_pago.get(s["forma_pago"], s["forma_pago"]),
            "moneda": s["moneda"],
            "monto": float(s["total"]),
        })

    return filas


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
