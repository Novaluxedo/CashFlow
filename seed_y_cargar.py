"""
seed_y_cargar.py
Script de UNA SOLA VEZ (o cuantas veces quieras, es seguro repetirlo):
1. Crea las 4 cuentas bancarias conocidas si todavia no existen.
2. Recorre una carpeta local y carga todos los PDF que encuentre,
   intentando el parser de BHD y el de Popular en cada archivo.
3. Corre las reglas de sugerencia automatica al final.

Uso:
    python seed_y_cargar.py "C:\\ruta\\a\\tus\\extractos"

Es seguro correrlo varias veces: las cuentas no se duplican (se detectan
por numero_cuenta), y los movimientos tampoco (por la restriccion UNIQUE
de movimientos_banco) - asi que puedes volver a correrlo si agregas PDF
nuevos a la misma carpeta sin miedo a duplicar nada.
"""

import glob
import os
import sys

from sqlalchemy import text

from db import SessionLocal
from parser_bhd import ErrorParseoBHD, parse_bhd_statement
from parser_popular import ErrorParseoPopular, parse_popular_statement
from reglas_conciliacion import clasificar_estado_inicial, ejecutar_sugerencias

# Las 4 cuentas que ya identificamos en los extractos reales.
CUENTAS_CONOCIDAS = [
    {"banco_codigo": "BHD", "numero_cuenta": "15469770011", "moneda": "USD", "alias": "BHD USD"},
    {"banco_codigo": "BHD", "numero_cuenta": "15469770020", "moneda": "RD$", "alias": "BHD RD$"},
    {"banco_codigo": "POPULAR", "numero_cuenta": "807405949", "moneda": "USD", "alias": "Popular Ahorro USD"},
    {"banco_codigo": "POPULAR", "numero_cuenta": "807203187", "moneda": "RD$", "alias": "Popular Corriente RD$"},
]


def sembrar_cuentas(db):
    print("--- Sembrando cuentas bancarias ---")
    for c in CUENTAS_CONOCIDAS:
        existente = db.execute(
            text("SELECT id FROM cuentas_bancarias WHERE numero_cuenta = :n"),
            {"n": c["numero_cuenta"]},
        ).first()
        if existente:
            print(f"  {c['alias']} ({c['numero_cuenta']}) ya existia, se deja igual.")
            continue

        db.execute(
            text(
                "INSERT INTO cuentas_bancarias (banco_codigo, numero_cuenta, moneda, alias, creado_por) "
                "VALUES (:banco_codigo, :numero_cuenta, :moneda, :alias, 'seed_y_cargar.py')"
            ),
            c,
        )
        print(f"  + {c['alias']} ({c['numero_cuenta']}) creada.")
    db.commit()


def obtener_cuenta_id(db, numero_cuenta):
    fila = db.execute(
        text("SELECT id FROM cuentas_bancarias WHERE numero_cuenta = :n"),
        {"n": numero_cuenta},
    ).first()
    return fila[0] if fila else None


def cargar_pdf(db, ruta_pdf):
    nombre = os.path.basename(ruta_pdf)

    datos = None
    try:
        datos = parse_bhd_statement(ruta_pdf)
    except ErrorParseoBHD:
        pass

    if datos is None:
        try:
            datos = parse_popular_statement(ruta_pdf)
        except ErrorParseoPopular as e:
            print(f"  [SALTADO] {nombre}: no es un extracto BHD ni Popular valido ({e})")
            return 0, 0

    cuenta_id = obtener_cuenta_id(db, datos["numero_cuenta"])
    if not cuenta_id:
        print(f"  [SALTADO] {nombre}: cuenta {datos['numero_cuenta']} no esta registrada.")
        return 0, 0

    nuevos = 0
    duplicados = 0
    for mov in datos["movimientos"]:
        estado_inicial = clasificar_estado_inicial(mov["codigo_movimiento"], mov["descripcion"])
        resultado = db.execute(
            text("""
                INSERT INTO movimientos_banco
                    (cuenta_id, fecha, referencia, codigo_movimiento, descripcion,
                     debito, credito, saldo, archivo_origen, estado_conciliacion)
                VALUES
                    (:cuenta_id, :fecha, :referencia, :codigo_movimiento, :descripcion,
                     :debito, :credito, :saldo, :archivo_origen, :estado_conciliacion)
                ON CONFLICT (cuenta_id, fecha, referencia, debito, credito, saldo)
                DO NOTHING
                RETURNING id
            """),
            {
                "cuenta_id": cuenta_id,
                "fecha": mov["fecha"],
                "referencia": mov["referencia"],
                "codigo_movimiento": mov["codigo_movimiento"],
                "descripcion": mov["descripcion"],
                "debito": mov["debito"],
                "credito": mov["credito"],
                "saldo": mov["saldo"],
                "archivo_origen": nombre,
                "estado_conciliacion": estado_inicial,
            },
        ).first()
        if resultado:
            nuevos += 1
        else:
            duplicados += 1

    db.commit()
    print(f"  OK {nombre}: cuenta {datos['numero_cuenta']} ({datos['periodo_inicio']} -> {datos['periodo_fin']}) "
          f"- {nuevos} nuevos, {duplicados} ya existian")
    return nuevos, duplicados


def main():
    if len(sys.argv) < 2:
        print('Uso: python seed_y_cargar.py "C:\\ruta\\a\\tus\\extractos"')
        sys.exit(1)

    carpeta = sys.argv[1]
    if not os.path.isdir(carpeta):
        print(f"La carpeta no existe: {carpeta}")
        sys.exit(1)

    db = SessionLocal()
    try:
        sembrar_cuentas(db)

        print("\n--- Cargando PDF encontrados ---")
        pdfs = sorted(glob.glob(os.path.join(carpeta, "*.pdf")))
        if not pdfs:
            print("No se encontraron archivos .pdf en esa carpeta.")
            return

        total_nuevos = 0
        total_duplicados = 0
        for ruta in pdfs:
            n, d = cargar_pdf(db, ruta)
            total_nuevos += n
            total_duplicados += d

        print(f"\nTotal: {total_nuevos} movimientos nuevos, {total_duplicados} ya existian.")

        print("\n--- Corriendo sugerencias automaticas ---")
        resultado = ejecutar_sugerencias(db)
        print(f"  {resultado['conversion_divisas_sugeridas']} pares de conversion de divisas sugeridos")
        print(f"  {resultado['transferencias_sugeridas']} pares de transferencia interna sugeridos")

    finally:
        db.close()


if __name__ == "__main__":
    main()
