"""
seed_y_cargar.py
UN SOLO script para todo lo relacionado a cargar extractos bancarios:
1. Crea las 4 cuentas bancarias conocidas si todavia no existen.
2. Recorre una carpeta local, y para cada PDF:
   - Intenta parser_bhd.py, luego parser_popular.py.
   - Si alguno lo reconoce, carga los movimientos a Neon.
   - Si NINGUNO lo reconoce (un banco nuevo sin parser todavia, o un
     PDF corrupto/no bancario), lo reporta como DESCONOCIDO con un
     fragmento del texto del PDF para poder identificarlo a simple
     vista, sin tumbar el resto de la carga.
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
from parser_bhd import parse_bhd_statement
from parser_popular import parse_popular_statement
from reglas_conciliacion import clasificar_estado_inicial, ejecutar_sugerencias

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

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


def _fragmento_diagnostico(ruta_pdf: str) -> str:
    """Primeros ~300 caracteres del PDF, para identificar a simple vista
    de que banco es cuando ningun parser lo reconoce."""
    if pdfplumber is None:
        return "(pdfplumber no disponible para diagnostico)"
    try:
        with pdfplumber.open(ruta_pdf) as pdf:
            texto = pdf.pages[0].extract_text() or ""
            return " ".join(texto.split())[:300]
    except Exception as e:
        return f"(no se pudo abrir el PDF en absoluto: {e})"


def cargar_pdf(db, ruta_pdf):
    """
    Intenta cargar un PDF a Neon, probando BHD y Popular en orden.

    Retorna (nuevos, duplicados, resultado) donde resultado es uno de
    'BHD', 'POPULAR', 'CUENTA_NO_REGISTRADA' o 'DESCONOCIDO' - para que
    main() pueda armar el resumen final agrupado por tipo.
    """
    nombre = os.path.basename(ruta_pdf)

    datos = None
    banco = None
    error_bhd = error_popular = ""

    try:
        datos = parse_bhd_statement(ruta_pdf)
        banco = "BHD"
    except Exception as e:
        error_bhd = str(e)

    if datos is None:
        try:
            datos = parse_popular_statement(ruta_pdf)
            banco = "POPULAR"
        except Exception as e:
            error_popular = str(e)

    if datos is None:
        fragmento = _fragmento_diagnostico(ruta_pdf)
        print(f"  [DESCONOCIDO] {nombre}")
        print(f"      BHD dijo: {error_bhd}")
        print(f"      Popular dijo: {error_popular}")
        print(f"      Primeros 300 caracteres del PDF: \"{fragmento}\"")
        return 0, 0, "DESCONOCIDO"

    cuenta_id = obtener_cuenta_id(db, datos["numero_cuenta"])
    if not cuenta_id:
        print(f"  [SALTADO] {nombre}: se reconocio como {banco}, pero la cuenta "
              f"{datos['numero_cuenta']} no esta en CUENTAS_CONOCIDAS.")
        return 0, 0, "CUENTA_NO_REGISTRADA"

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
    print(f"  OK [{banco}] {nombre}: cuenta {datos['numero_cuenta']} "
          f"({datos['periodo_inicio']} -> {datos['periodo_fin']}) "
          f"- {nuevos} nuevos, {duplicados} ya existian")
    for advertencia in datos.get("advertencias", []):
        print(f"      ⚠ {advertencia}")
    return nuevos, duplicados, banco


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
        conteo_por_resultado = {"BHD": 0, "POPULAR": 0, "CUENTA_NO_REGISTRADA": 0, "DESCONOCIDO": 0}

        for ruta in pdfs:
            n, d, resultado = cargar_pdf(db, ruta)
            total_nuevos += n
            total_duplicados += d
            conteo_por_resultado[resultado] += 1

        print(f"\nTotal: {total_nuevos} movimientos nuevos, {total_duplicados} ya existian.")
        print(f"Archivos: {conteo_por_resultado['BHD']} BHD, {conteo_por_resultado['POPULAR']} Popular, "
              f"{conteo_por_resultado['CUENTA_NO_REGISTRADA']} con cuenta no registrada, "
              f"{conteo_por_resultado['DESCONOCIDO']} de banco desconocido.")

        if conteo_por_resultado["DESCONOCIDO"] > 0:
            print("\n⚠ Hay archivos de un banco que ningun parser reconoce todavia - "
                  "revisa el detalle de arriba (fragmento de texto de cada uno) para "
                  "identificar de que banco son antes de escribir un parser nuevo.")

        print("\n--- Corriendo sugerencias automaticas ---")
        resultado = ejecutar_sugerencias(db)
        print(f"  {resultado['conversion_divisas_sugeridas']} pares de conversion de divisas sugeridos")
        print(f"  {resultado['transferencias_sugeridas']} pares de transferencia interna sugeridos")

    finally:
        db.close()


if __name__ == "__main__":
    main()
