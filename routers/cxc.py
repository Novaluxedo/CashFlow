"""
routers/cxc.py
Cuentas por Cobrar - las dos metricas validadas en la investigacion de la
cuenta control 1140101 (ver GUIA_CASHFLOW_CONTINUIDAD.html / auditoria).

Metrica 1 (/seguimiento): Ventas vs. Cobros por cliente y mes, para poder
dar seguimiento puntual si el agregado global no cuadra. Ventas viene de
cxc_movimientos.debito (siempre USD, confirmado). Cobros viene de la
tabla `cobros` (ya separada por moneda), convertida a USD con su propia
tasa_cambio - nunca de cxc_movimientos.credito, que mezcla USD y RD$ sin
convertir por ser una cuenta multi-moneda.

Metrica 2 (/saldo-abierto): Antigueadad de saldos por factura individual,
usando saldo_abierto_debito - el campo que SAP mantiene automaticamente
via su motor de reconciliacion interna, confirmado confiable con casos
reales durante la investigacion.
"""

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from db import get_db
from routers.auth import usuario_actual

router = APIRouter(prefix="/api/cxc", tags=["cuentas_por_cobrar"])


@router.get("/saldo-abierto")
def saldo_abierto(
    cliente: str | None = Query(None, description="Filtrar por codigo de cliente (ShortName en SAP)"),
    dias_minimo: int = Query(0, description="Solo facturas con al menos este numero de dias abiertas"),
    db: Session = Depends(get_db),
    usuario: dict = Depends(usuario_actual),
):
    """
    Metrica 2 - Antiguedad de saldos por factura, a la fecha de hoy.
    saldo_abierto_debito ya viene calculado por SAP (BalDueDeb) - no se
    recalcula aqui, solo se expone.
    """
    filtros = ["m.trans_type = '13'", "m.saldo_abierto_debito > 0"]
    parametros = {}

    if cliente:
        filtros.append("m.cliente_code = :cliente")
        parametros["cliente"] = cliente

    if dias_minimo:
        filtros.append("(CURRENT_DATE - m.fecha) >= :dias_minimo")
        parametros["dias_minimo"] = dias_minimo

    where_sql = " AND ".join(filtros)

    filas = db.execute(
        text(f"""
            SELECT m.cliente_code, m.doc_entry, m.doc_num, m.fecha AS fecha_venta,
                   m.debito AS venta_original_usd, m.saldo_abierto_debito AS saldo_abierto_usd,
                   (CURRENT_DATE - m.fecha) AS dias_abierto
            FROM cxc_movimientos m
            WHERE {where_sql}
            ORDER BY dias_abierto DESC
        """),
        parametros,
    ).mappings().all()

    resultado = [
        {
            "cliente_code": f["cliente_code"],
            "doc_entry": f["doc_entry"],
            "doc_num": f["doc_num"],
            "fecha_venta": str(f["fecha_venta"]),
            "venta_original_usd": float(f["venta_original_usd"]),
            "saldo_abierto_usd": float(f["saldo_abierto_usd"]),
            "dias_abierto": f["dias_abierto"],
        }
        for f in filas
    ]

    return {
        "total_facturas_abiertas": len(resultado),
        "total_saldo_abierto_usd": sum(r["saldo_abierto_usd"] for r in resultado),
        "facturas": resultado,
    }


@router.get("/saldo-abierto/resumen-por-cliente")
def saldo_abierto_resumen_por_cliente(
    db: Session = Depends(get_db),
    usuario: dict = Depends(usuario_actual),
):
    """
    Igual que /saldo-abierto pero agregado por cliente - util para ver
    concentracion de riesgo de credito de un vistazo.
    """
    filas = db.execute(
        text("""
            SELECT cliente_code,
                   COUNT(*) AS facturas_abiertas,
                   SUM(saldo_abierto_debito) AS saldo_abierto_usd,
                   MAX(CURRENT_DATE - fecha) AS dias_mas_antiguo
            FROM cxc_movimientos
            WHERE trans_type = '13' AND saldo_abierto_debito > 0
            GROUP BY cliente_code
            ORDER BY saldo_abierto_usd DESC
        """)
    ).mappings().all()

    return [
        {
            "cliente_code": f["cliente_code"],
            "facturas_abiertas": f["facturas_abiertas"],
            "saldo_abierto_usd": float(f["saldo_abierto_usd"]),
            "dias_mas_antiguo": f["dias_mas_antiguo"],
        }
        for f in filas
    ]


@router.get("/resumen-mensual")
def resumen_mensual(
    db: Session = Depends(get_db),
    usuario: dict = Depends(usuario_actual),
):
    """
    Metricas 1 y 2, agregadas por mes solamente (sin desglose por cliente) -
    pensado para el dashboard principal (index.html), no para investigacion
    puntual. Para el detalle por cliente, usar /seguimiento y /saldo-abierto.

    Metrica 1 (ventas_usd, cobros_usd, variacion_neta_usd): igual formula
    que /seguimiento pero agregando todos los clientes juntos por mes.

    Metrica 2 (saldo_abierto_usd, facturas_abiertas): saldo abierto HOY,
    agrupado por el mes en que se origino la venta - es una foto actual,
    no un historico de como se movio el saldo mes a mes.
    """
    ventas = db.execute(
        text("""
            SELECT date_trunc('month', fecha)::date AS mes, SUM(debito) AS ventas_usd
            FROM cxc_movimientos
            WHERE trans_type = '13'
            GROUP BY 1
        """)
    ).mappings().all()

    cobros = db.execute(
        text("""
            SELECT date_trunc('month', fecha)::date AS mes,
                   SUM(
                       CASE WHEN moneda = 'USD' THEN monto
                            ELSE monto * COALESCE(tasa_cambio, 0)
                       END
                   ) AS cobros_usd,
                   SUM(CASE WHEN tasa_cambio IS NULL AND moneda != 'USD' THEN 1 ELSE 0 END) AS cobros_sin_tasa
            FROM cobros
            GROUP BY 1
        """)
    ).mappings().all()

    saldo_abierto = db.execute(
        text("""
            SELECT date_trunc('month', fecha)::date AS mes,
                   COUNT(*) AS facturas_abiertas,
                   SUM(saldo_abierto_debito) AS saldo_abierto_usd
            FROM cxc_movimientos
            WHERE trans_type = '13' AND saldo_abierto_debito > 0
            GROUP BY 1
        """)
    ).mappings().all()

    combinado: dict = {}
    total_cobros_sin_tasa = 0

    for v in ventas:
        combinado.setdefault(v["mes"], {}).update({"ventas_usd": float(v["ventas_usd"])})

    for c in cobros:
        combinado.setdefault(c["mes"], {}).update({"cobros_usd": float(c["cobros_usd"] or 0)})
        total_cobros_sin_tasa += c["cobros_sin_tasa"]

    for s in saldo_abierto:
        combinado.setdefault(s["mes"], {}).update({
            "facturas_abiertas": s["facturas_abiertas"],
            "saldo_abierto_usd": float(s["saldo_abierto_usd"]),
        })

    resultado = []
    for mes, datos in sorted(combinado.items()):
        ventas_usd = datos.get("ventas_usd", 0.0)
        cobros_usd = datos.get("cobros_usd", 0.0)
        resultado.append({
            "periodo": f"{mes.year}-{mes.month:02d}",
            "ventas_usd": round(ventas_usd, 2),
            "cobros_usd": round(cobros_usd, 2),
            "variacion_neta_usd": round(ventas_usd - cobros_usd, 2),
            "facturas_abiertas": datos.get("facturas_abiertas", 0),
            "saldo_abierto_usd": round(datos.get("saldo_abierto_usd", 0.0), 2),
        })

    respuesta = {"meses": resultado}

    if total_cobros_sin_tasa:
        respuesta["advertencia"] = (
            f"{total_cobros_sin_tasa} cobro(s) en RD$ sin tasa_cambio registrada - "
            "sus cobros_usd salen incompletos hasta volver a correr sync_flujo.py."
        )

    return respuesta


@router.get("/detalle-periodo")
def detalle_periodo(
    periodo: str = Query(..., description="YYYY-MM, obligatorio"),
    db: Session = Depends(get_db),
    usuario: dict = Depends(usuario_actual),
):
    """
    Vista para el dashboard principal: factura a factura del mes
    seleccionado, mas las dos metricas resumidas para ese mismo mes.

    Metrica 1 (cash_entrado_usd): cuanto efectivo real entro ese mes -
    viene de `cobros` (no de las facturas), convertido a USD con la tasa
    real de cada cobro. Un cobro de este mes puede pagar una factura de
    un mes anterior - por eso este numero es independiente de la lista
    de facturas de abajo.

    Metrica 2 (ventas_usd = cobrado_usd + saldo_abierto_usd): para las
    facturas EMITIDAS en este mes, cuanto de eso ya se cobro (a la fecha
    de hoy, via BalDueDeb) y cuanto sigue en CxC. Por construccion estos
    dos siempre deben sumar el total facturado - si no cuadra exacto, es
    señal de un problema de datos, no de la formula.
    """
    anio, mes = (int(p) for p in periodo.split("-"))
    parametros = {"anio": anio, "mes": mes}

    # ---------- Metrica 1: cash entrado ese mes (independiente de la factura) ----------
    cobros_mes = db.execute(
        text("""
            SELECT
                SUM(CASE WHEN moneda = 'USD' THEN monto ELSE monto * COALESCE(tasa_cambio, 0) END) AS cash_entrado_usd,
                SUM(CASE WHEN tasa_cambio IS NULL AND moneda != 'USD' THEN 1 ELSE 0 END) AS cobros_sin_tasa
            FROM cobros
            WHERE EXTRACT(YEAR FROM fecha) = :anio AND EXTRACT(MONTH FROM fecha) = :mes
        """),
        parametros,
    ).mappings().first()

    # ---------- Metrica 2 + listado factura a factura, de lo emitido ese mes ----------
    facturas = db.execute(
        text("""
            SELECT doc_entry, doc_num, cliente_code, fecha,
                   debito AS venta_usd,
                   (debito - saldo_abierto_debito) AS cobrado_usd,
                   saldo_abierto_debito AS saldo_abierto_usd
            FROM cxc_movimientos
            WHERE trans_type = '13'
              AND EXTRACT(YEAR FROM fecha) = :anio AND EXTRACT(MONTH FROM fecha) = :mes
            ORDER BY fecha, doc_num
        """),
        parametros,
    ).mappings().all()

    facturas_out = []
    total_ventas = 0.0
    total_cobrado = 0.0
    total_saldo_abierto = 0.0

    for f in facturas:
        venta = float(f["venta_usd"])
        cobrado = float(f["cobrado_usd"])
        saldo = float(f["saldo_abierto_usd"])
        total_ventas += venta
        total_cobrado += cobrado
        total_saldo_abierto += saldo

        if saldo <= 0.005:
            estado = "pagada"
        elif cobrado <= 0.005:
            estado = "abierta"
        else:
            estado = "parcial"

        facturas_out.append({
            "doc_entry": f["doc_entry"],
            "doc_num": f["doc_num"],
            "cliente_code": f["cliente_code"],
            "fecha": str(f["fecha"]),
            "venta_usd": round(venta, 2),
            "cobrado_usd": round(cobrado, 2),
            "saldo_abierto_usd": round(saldo, 2),
            "estado": estado,
        })

    diferencia_cuadre = round(total_ventas - (total_cobrado + total_saldo_abierto), 2)

    return {
        "periodo": periodo,
        "metrica_1_cash_entrado_usd": round(float(cobros_mes["cash_entrado_usd"] or 0), 2),
        "metrica_1_cobros_sin_tasa": cobros_mes["cobros_sin_tasa"] or 0,
        "metrica_2_ventas_usd": round(total_ventas, 2),
        "metrica_2_cobrado_usd": round(total_cobrado, 2),
        "metrica_2_saldo_abierto_usd": round(total_saldo_abierto, 2),
        "metrica_2_diferencia_cuadre": diferencia_cuadre,
        "facturas": facturas_out,
    }


@router.get("/validacion-banco")
def validacion_banco(
    periodo: str = Query(..., description="YYYY-MM, obligatorio"),
    db: Session = Depends(get_db),
    usuario: dict = Depends(usuario_actual),
):
    """
    Valida la Metrica 1 (Efectivo Cobrado) contra el estado de banco REAL,
    sin convertir monedas - cada una en la suya, como manda el principio
    original del proyecto. Solo BHD tiene extractos cargados por ahora
    (parser_popular.py todavia no existe), asi que la validacion se limita
    a BHD - si el mes no tiene extracto BHD cargado, se marca como tal en
    vez de mostrar un cuadre falso.
    """
    anio, mes = (int(p) for p in periodo.split("-"))
    parametros = {"anio": anio, "mes": mes}

    banco = db.execute(
        text("""
            SELECT c.moneda,
                   SUM(m.credito) AS creditos_confirmados,
                   COUNT(*) AS filas_totales
            FROM movimientos_banco m
            JOIN cuentas_bancarias c ON c.id = m.cuenta_id
            WHERE c.banco_codigo = 'BHD'
              AND EXTRACT(YEAR FROM m.fecha) = :anio AND EXTRACT(MONTH FROM m.fecha) = :mes
              AND m.estado_conciliacion = 'confirmado'
            GROUP BY c.moneda
        """),
        parametros,
    ).mappings().all()

    # Para saber si el mes tiene extracto cargado en absoluto (con o sin
    # confirmar todavia) - distinto de "cargado pero en cero por conciliar".
    banco_filas_totales = db.execute(
        text("""
            SELECT c.moneda, COUNT(*) AS filas
            FROM movimientos_banco m
            JOIN cuentas_bancarias c ON c.id = m.cuenta_id
            WHERE c.banco_codigo = 'BHD'
              AND EXTRACT(YEAR FROM m.fecha) = :anio AND EXTRACT(MONTH FROM m.fecha) = :mes
            GROUP BY c.moneda
        """),
        parametros,
    ).mappings().all()

    cobros_por_moneda = db.execute(
        text("""
            SELECT moneda, SUM(monto) AS total
            FROM cobros
            WHERE EXTRACT(YEAR FROM fecha) = :anio AND EXTRACT(MONTH FROM fecha) = :mes
            GROUP BY moneda
        """),
        parametros,
    ).mappings().all()

    banco_map = {b["moneda"]: float(b["creditos_confirmados"] or 0) for b in banco}
    banco_filas_map = {b["moneda"]: b["filas"] for b in banco_filas_totales}
    cobros_map = {c["moneda"]: float(c["total"] or 0) for c in cobros_por_moneda}

    resultado = {}
    for moneda in ("USD", "RD$"):
        disponible = banco_filas_map.get(moneda, 0) > 0
        cobros_val = cobros_map.get(moneda, 0.0)
        banco_val = banco_map.get(moneda, 0.0)
        resultado[moneda] = {
            "banco_disponible": disponible,
            "cobros_usd_o_rd": round(cobros_val, 2),
            "banco_confirmado": round(banco_val, 2),
            "diferencia": round(cobros_val - banco_val, 2) if disponible else None,
        }

    return {"periodo": periodo, "por_moneda": resultado}


@router.get("/seguimiento")
def seguimiento_ventas_vs_cobros(
    periodo: str | None = Query(None, description="YYYY-MM opcional. Sin esto, trae todos los meses disponibles."),
    cliente: str | None = Query(None, description="Filtrar por codigo de cliente"),
    db: Session = Depends(get_db),
    usuario: dict = Depends(usuario_actual),
):
    """
    Metrica 1 - Ventas vs. Cobros por cliente y mes, en USD.

    Ventas: SUM(cxc_movimientos.debito) donde trans_type='13' (facturas) -
    siempre en USD, confirmado.

    Cobros: SUM(cobros.monto * tasa_cambio) cuando moneda='RD$', o
    SUM(cobros.monto) cuando ya es USD - usando la tasa real que SAP
    aplico a cada cobro (ORCT.DocRate), nunca una tasa BCRD nuestra.

    variacion_neta = ventas - cobros. Positivo = el mes vendio mas de lo
    que cobro (crecio CxC). Negativo = se cobro mas de lo facturado ese
    mes (se cobro cartera vieja).

    Si un cliente/mes sale con una variacion que no tiene sentido de
    negocio, investigar ese caso puntual contra JDT1 directamente
    (Account='1140101' AND ShortName=<cliente>), tal como se valido en
    la investigacion original.
    """
    filtro_periodo_ventas = ""
    filtro_periodo_cobros = ""
    parametros = {}

    if periodo:
        anio, mes = (int(p) for p in periodo.split("-"))
        parametros["anio"] = anio
        parametros["mes"] = mes
        filtro_periodo_ventas = "AND EXTRACT(YEAR FROM fecha) = :anio AND EXTRACT(MONTH FROM fecha) = :mes"
        filtro_periodo_cobros = "AND EXTRACT(YEAR FROM fecha) = :anio AND EXTRACT(MONTH FROM fecha) = :mes"

    filtro_cliente_ventas = ""
    filtro_cliente_cobros = ""
    if cliente:
        parametros["cliente"] = cliente
        filtro_cliente_ventas = "AND cliente_code = :cliente"
        filtro_cliente_cobros = "AND cliente_code = :cliente"

    ventas = db.execute(
        text(f"""
            SELECT date_trunc('month', fecha)::date AS mes, cliente_code,
                   SUM(debito) AS ventas_usd
            FROM cxc_movimientos
            WHERE trans_type = '13' {filtro_periodo_ventas} {filtro_cliente_ventas}
            GROUP BY 1, 2
        """),
        parametros,
    ).mappings().all()

    cobros = db.execute(
        text(f"""
            SELECT date_trunc('month', fecha)::date AS mes, cliente_code,
                   SUM(
                       CASE WHEN moneda = 'USD' THEN monto
                            ELSE monto * COALESCE(tasa_cambio, 0)
                       END
                   ) AS cobros_usd,
                   SUM(CASE WHEN tasa_cambio IS NULL AND moneda != 'USD' THEN 1 ELSE 0 END) AS cobros_sin_tasa
            FROM cobros
            WHERE 1=1 {filtro_periodo_cobros} {filtro_cliente_cobros}
            GROUP BY 1, 2
        """),
        parametros,
    ).mappings().all()

    # Combinar ventas y cobros por (mes, cliente) - puede haber cliente
    # con venta pero sin cobro ese mes, o viceversa (cobro de mes viejo).
    combinado: dict[tuple, dict] = {}
    for v in ventas:
        clave = (v["mes"], v["cliente_code"])
        combinado[clave] = {"ventas_usd": float(v["ventas_usd"]), "cobros_usd": 0.0, "cobros_sin_tasa": 0}

    for c in cobros:
        clave = (c["mes"], c["cliente_code"])
        if clave not in combinado:
            combinado[clave] = {"ventas_usd": 0.0, "cobros_usd": 0.0, "cobros_sin_tasa": 0}
        combinado[clave]["cobros_usd"] = float(c["cobros_usd"] or 0)
        combinado[clave]["cobros_sin_tasa"] = c["cobros_sin_tasa"]

    resultado = []
    total_cobros_sin_tasa = 0
    for (mes, cliente_code), datos in sorted(combinado.items(), key=lambda x: (x[0][0], x[0][1])):
        total_cobros_sin_tasa += datos["cobros_sin_tasa"]
        resultado.append({
            "periodo": f"{mes.year}-{mes.month:02d}",
            "cliente_code": cliente_code,
            "ventas_usd": round(datos["ventas_usd"], 2),
            "cobros_usd": round(datos["cobros_usd"], 2),
            "variacion_neta_usd": round(datos["ventas_usd"] - datos["cobros_usd"], 2),
        })

    respuesta = {"detalle": resultado}

    if total_cobros_sin_tasa:
        respuesta["advertencia"] = (
            f"{total_cobros_sin_tasa} cobro(s) en RD$ sin tasa_cambio registrada "
            "(probablemente sincronizados con sync_flujo.py antes del cambio que "
            "agrega DocRate) - sus cobros_usd salen incompletos hasta que se "
            "vuelva a correr la sincronizacion de cobros."
        )

    return respuesta
