"""
validar_sap_banco.py
Compara SAP (cobros/pagos) contra el extracto bancario real (movimientos_banco,
solo los que estan 'confirmado'), por cuenta - y ademas intenta emparejar
documento por documento (mismo monto, misma cuenta, fecha cercana) para
senalar cuales NO tienen contraparte en la otra fuente.

Es de solo lectura - no modifica nada en la base de datos, solo imprime
el reporte.

Uso:
    python validar_sap_banco.py
"""

from sqlalchemy import text

from db import SessionLocal

TOLERANCIA_DIAS = 3  # dias de diferencia aceptados entre la fecha del documento SAP y la del banco


def _cargar_cuentas_vinculadas(db):
    filas = db.execute(
        text("""
            SELECT c.id, c.sap_acct_code, c.moneda, COALESCE(c.alias, b.nombre || ' ' || c.numero_cuenta) AS alias
            FROM cuentas_bancarias c
            JOIN catalogo_bancos b ON b.codigo = c.banco_codigo
            WHERE c.sap_acct_code IS NOT NULL AND c.activa = TRUE
        """)
    ).mappings().all()
    return [dict(f) for f in filas]


def _cargar_sap(db, tabla, sap_acct_code):
    filas = db.execute(
        text(f"""
            SELECT doc_entry, fecha, monto
            FROM {tabla}
            WHERE cuenta_banco = :cuenta
        """),
        {"cuenta": sap_acct_code},
    ).mappings().all()
    return [dict(f) for f in filas]


def _cargar_banco(db, cuenta_id, tipo):
    """tipo = 'credito' (para cobros) o 'debito' (para pagos)."""
    filas = db.execute(
        text(f"""
            SELECT id, fecha, {tipo} AS monto, descripcion
            FROM movimientos_banco
            WHERE cuenta_id = :cuenta_id AND estado_conciliacion = 'confirmado' AND {tipo} > 0
        """),
        {"cuenta_id": cuenta_id},
    ).mappings().all()
    return [dict(f) for f in filas]


def _emparejar(items_sap, items_banco):
    """
    Emparejamiento greedy: para cada item de SAP, busca en items_banco uno
    con el mismo monto exacto y fecha dentro de TOLERANCIA_DIAS que no haya
    sido usado todavia. Retorna (emparejados, sap_sin_match, banco_sin_match).
    """
    banco_disponible = list(items_banco)
    emparejados = []
    sap_sin_match = []

    for doc in items_sap:
        candidato = None
        for i, mov in enumerate(banco_disponible):
            if abs(mov["monto"] - doc["monto"]) < 0.01 and abs((mov["fecha"] - doc["fecha"]).days) <= TOLERANCIA_DIAS:
                candidato = i
                break
        if candidato is not None:
            emparejados.append((doc, banco_disponible.pop(candidato)))
        else:
            sap_sin_match.append(doc)

    return emparejados, sap_sin_match, banco_disponible  # lo que queda en banco_disponible es lo sin match


def _reportar_lado(nombre, sap_items, banco_items, sap_label, banco_label):
    total_sap = sum(x["monto"] for x in sap_items)
    total_banco = sum(x["monto"] for x in banco_items)

    print(f"\n  {nombre}")
    print(f"  SAP ({sap_label}):              {total_sap:>18,.2f}  ({len(sap_items)} documentos)")
    print(f"  Banco (confirmado, {banco_label}): {total_banco:>16,.2f}  ({len(banco_items)} movimientos)")
    print(f"  Diferencia (banco - SAP):   {total_banco - total_sap:>17,.2f}")

    emparejados, sap_sin_match, banco_sin_match = _emparejar(sap_items, banco_items)
    print(f"  Emparejados documento-a-documento: {len(emparejados)}")

    if sap_sin_match:
        print(f"\n  ⚠ En SAP SIN movimiento bancario que los explique ({len(sap_sin_match)}):")
        for doc in sap_sin_match[:10]:
            print(f"      doc_entry={doc['doc_entry']}  fecha={doc['fecha']}  monto={doc['monto']:,.2f}")
        if len(sap_sin_match) > 10:
            print(f"      ... y {len(sap_sin_match) - 10} mas")

    if banco_sin_match:
        print(f"\n  ⚠ Confirmados en el banco SIN documento en SAP que los explique ({len(banco_sin_match)}):")
        for mov in banco_sin_match[:10]:
            print(f"      fecha={mov['fecha']}  monto={mov['monto']:,.2f}  desc={(mov['descripcion'] or '')[:50]}")
        if len(banco_sin_match) > 10:
            print(f"      ... y {len(banco_sin_match) - 10} mas")


def main():
    db = SessionLocal()
    try:
        cuentas = _cargar_cuentas_vinculadas(db)
        if not cuentas:
            print("Ninguna cuenta bancaria tiene sap_acct_code asignado todavia. "
                  "Corre 02_vincular_cuentas_sap.sql en Neon primero.")
            return

        for cuenta in cuentas:
            print(f"\n{'='*70}")
            print(f"CUENTA: {cuenta['alias']}  (SAP: {cuenta['sap_acct_code']}, moneda: {cuenta['moneda']})")
            print('='*70)

            cobros = _cargar_sap(db, "cobros", cuenta["sap_acct_code"])
            creditos_banco = _cargar_banco(db, cuenta["id"], "credito")
            _reportar_lado("COBROS (entradas)", cobros, creditos_banco, "cobros", "credito")

            pagos = _cargar_sap(db, "pagos", cuenta["sap_acct_code"])
            debitos_banco = _cargar_banco(db, cuenta["id"], "debito")
            _reportar_lado("PAGOS (salidas)", pagos, debitos_banco, "pagos", "debito")

    finally:
        db.close()


if __name__ == "__main__":
    main()
