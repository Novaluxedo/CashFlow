"""
reglas_conciliacion.py
Motor de sugerencias automaticas para el modulo "Por conciliar".

IMPORTANTE: sugerir_conversion_divisas() y sugerir_transferencias_internas()
solo SUGIEREN (llena sugerencia_auto y movimiento_par_id). Nunca cambian
estado_conciliacion por si solas - eso requiere que alguien confirme desde
el backoffice (POST /api/conciliacion/{id}/resolver).

clasificar_estado_inicial() si decide el estado con el que un movimiento
entra por primera vez a la base de datos, en base a patrones concretos
encontrados en el analisis real de los extractos (no es una regla generica).
"""

from sqlalchemy import text
from sqlalchemy.orm import Session

# Codigos de movimiento que, por evidencia real, corresponden a operaciones
# ambiguas (conversion de divisas o creditos "ORD: NOVALUM" sin cruzar):
#   077 / 481 -> Venta Divisas (BHD USD / BHD RD$)
#   C86 -> ORD: NOVALUM SRL / NOVALUMSA en BHD RD$
#   C56 -> ORD: NOVALUM SRL en BHD USD
CODIGOS_AMBIGUOS = {"077", "481", "C86", "C56"}

# Fragmentos de descripcion que tambien senalan un movimiento ambiguo,
# independientemente del codigo:
FRAGMENTOS_AMBIGUOS = ("divisas", "via lbtr", "trans.devuelta", "trans devuelta")


def clasificar_estado_inicial(codigo_movimiento: str, descripcion: str) -> str:
    """
    Decide el estado_conciliacion con el que un movimiento nuevo entra a la BD.

    'por_conciliar' solo si coincide con un patron ambiguo conocido
    (conversion de divisas, transferencia entre bancos propios, o un
    'ORD: NOVALUM' sin contraparte clara). Todo lo demas -> 'confirmado'.
    """
    codigo = (codigo_movimiento or "").strip()
    desc = (descripcion or "").lower()

    if codigo in CODIGOS_AMBIGUOS:
        return "por_conciliar"
    if any(fragmento in desc for fragmento in FRAGMENTOS_AMBIGUOS):
        return "por_conciliar"
    return "confirmado"


def sugerir_conversion_divisas(db: Session) -> int:
    """
    Busca pares de movimientos pendientes con la misma 'referencia'
    (ej. 'Venta Divisas Ref. 2600908284') en dos cuentas de moneda distinta.
    Retorna cuantos pares encontro.
    """
    pares = db.execute(
        text("""
            SELECT a.id AS id_a, b.id AS id_b
            FROM movimientos_banco a
            JOIN movimientos_banco b
              ON a.referencia = b.referencia
             AND a.referencia IS NOT NULL AND a.referencia != ''
             AND a.cuenta_id != b.cuenta_id
             AND a.id < b.id
            JOIN cuentas_bancarias ca ON ca.id = a.cuenta_id
            JOIN cuentas_bancarias cb ON cb.id = b.cuenta_id
            WHERE a.estado_conciliacion = 'por_conciliar'
              AND b.estado_conciliacion = 'por_conciliar'
              AND ca.moneda != cb.moneda
              AND (a.descripcion ILIKE '%divisas%' OR b.descripcion ILIKE '%divisas%')
        """)
    ).mappings().all()

    for par in pares:
        db.execute(
            text(
                "UPDATE movimientos_banco SET sugerencia_auto = 'conversion_divisas', "
                "movimiento_par_id = :otro WHERE id = :este"
            ),
            {"otro": par["id_b"], "este": par["id_a"]},
        )
        db.execute(
            text(
                "UPDATE movimientos_banco SET sugerencia_auto = 'conversion_divisas', "
                "movimiento_par_id = :otro WHERE id = :este"
            ),
            {"otro": par["id_a"], "este": par["id_b"]},
        )
    db.commit()
    return len(pares)


def sugerir_transferencias_internas(db: Session) -> int:
    """
    Busca pares de movimientos pendientes en DOS CUENTAS DISTINTAS con el
    mismo monto exacto y la misma fecha (o +/- 1 dia habil), uno como
    debito (sale) y el otro como credito (entra). Retorna cuantos pares encontro.
    """
    pares = db.execute(
        text("""
            SELECT a.id AS id_a, b.id AS id_b
            FROM movimientos_banco a
            JOIN movimientos_banco b
              ON a.cuenta_id != b.cuenta_id
             AND a.debito > 0 AND b.credito > 0
             AND a.debito = b.credito
             AND ABS(a.fecha - b.fecha) <= 1
             AND a.id < b.id
            WHERE a.estado_conciliacion = 'por_conciliar'
              AND b.estado_conciliacion = 'por_conciliar'
              AND a.sugerencia_auto IS NULL
              AND b.sugerencia_auto IS NULL
        """)
    ).mappings().all()

    for par in pares:
        db.execute(
            text(
                "UPDATE movimientos_banco SET sugerencia_auto = 'transferencia_interna', "
                "movimiento_par_id = :otro WHERE id = :este"
            ),
            {"otro": par["id_b"], "este": par["id_a"]},
        )
        db.execute(
            text(
                "UPDATE movimientos_banco SET sugerencia_auto = 'transferencia_interna', "
                "movimiento_par_id = :otro WHERE id = :este"
            ),
            {"otro": par["id_a"], "este": par["id_b"]},
        )
    db.commit()
    return len(pares)


def ejecutar_sugerencias(db: Session) -> dict:
    """Corre todas las reglas de sugerencia disponibles. Llamar despues de cada carga de extracto."""
    return {
        "conversion_divisas_sugeridas": sugerir_conversion_divisas(db),
        "transferencias_sugeridas": sugerir_transferencias_internas(db),
    }
