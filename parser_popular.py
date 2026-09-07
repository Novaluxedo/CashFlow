"""
parser_popular.py
Extrae movimientos de un estado de cuenta Banco Popular (PDF) - Ahorro USD
o Corriente RD$.

HISTORIAL DE DISEÑO (3 intentos anteriores, cada uno descartado con
evidencia real - dejar esto documentado para no repetir el mismo camino):

1. Texto plano + regex "ultimos dos montos del bloque": fallaba en
   transacciones de cambio de divisas, donde el equivalente en la otra
   moneda aparece DENTRO de la descripcion (ej. "TRNF USD 883,696.47
   SHANDONG... 1.00 RD$ 60. VEN...") y se confundia con el Monto real.

2. `extract_tables()` con deteccion por LINEAS dibujadas (la estrategia
   por defecto de pdfplumber): perdia ~50% de las transacciones reales -
   confirmado comparando el conteo de filas extraidas contra un conteo
   independiente de fechas de transaccion en el texto plano (13 filas
   via tabla vs 27 fechas reales en una pagina, por ejemplo). Hipotesis:
   las transacciones con descripcion de una sola linea tienen una linea
   divisoria completa, las de varias lineas no, y la deteccion por
   lineas las descarta en silencio.

3. `extract_tables()` con estrategia 'text' (por alineacion de columnas):
   SI capturaba el 100% de las lineas, pero los limites de columna no
   son consistentes de una fila a otra - una palabra de una linea de
   continuacion a veces cae en la posicion de "Monto" en vez de en
   "Descripcion", causando errores de conversion (ValueError: could not
   convert string to float: 'Al').

SOLUCION ACTUAL (intento 4): en vez de confiar en que pdfplumber agrupe
las palabras en columnas correctamente, se usa `extract_words()` (que da
cada palabra individual con su posicion x/y exacta) y se clasifica cada
palabra por su PROPIO PATRON DE TEXTO, no por en que columna cayo:
    - "algo/algo/algo" con formato DD/MM/YYYY -> es una fecha
    - "$1,234.56" o "$1,234.56-" (palabra COMPLETA, no solo que contenga
      "$") -> es un Monto o Balance
    - Una fila de Descripcion que menciona una divisa (ej. "RD$ 60.")
      nunca forma una palabra completa que empiece exactamente con "$"
      seguida de 2 decimales sin nada mas pegado - por eso no se
      confunde con el Monto/Balance reales, sin importar en que
      posicion x haya caido.

Una "transaccion" empieza en la primera linea (agrupando palabras por
coordenada Y) que tenga 2 fechas - las lineas siguientes sin fechas se
tratan como continuacion de la Descripcion, hasta la proxima linea con
2 fechas.

VALIDACION: Popular no declara un saldo inicial ni totales de debito/
credito en el encabezado (a diferencia de BHD). Se valida que el balance
de cada transaccion cuadre contra el balance anterior +/- su monto; si
una fila puntual no cuadra, se registra como ADVERTENCIA y se
resincroniza desde ahi (no se aborta el archivo completo por una fila
aislada) - solo se lanza error si mas del 5% de las filas no cuadran,
señal de un problema estructural real.
"""

import re
from datetime import date, datetime

import pdfplumber

PATRON_CUENTA = re.compile(r"(Cuenta de Ahorro|Cuenta Corriente)\s*/\s*(\d+)")

PATRON_FECHA_TOKEN = re.compile(r"^\d{2}/\d{2}/\d{4}$")
PATRON_MONTO_TOKEN = re.compile(r"^\$[\d,]+\.\d{2}-?$")
PATRON_REFERENCIA_TOKEN = re.compile(r"^\d{6,14}$")

TOLERANCIA_MISMA_LINEA_PX = 3  # palabras con 'top' a menos de esto se consideran la misma linea visual


class ErrorParseoPopular(Exception):
    """Se lanza cuando el PDF no tiene el formato Popular esperado, o la
    cadena de balances no cuadra transaccion por transaccion mas alla de
    un margen aceptable de filas aisladas raras."""


def _limpiar_monto(valor: str) -> float:
    """'$1,234.56' o '$1,234.56-' -> 1234.56 (siempre positivo; el signo
    se maneja aparte para decidir debito/credito)."""
    valor = (valor or "").strip().rstrip("-").lstrip("$").replace(",", "").strip()
    return float(valor) if valor else 0.0


def _parsear_encabezado(texto_primera_pagina: str) -> dict:
    m = PATRON_CUENTA.search(texto_primera_pagina or "")
    if not m:
        raise ErrorParseoPopular(
            "No se pudo leer el encabezado del extracto Popular "
            "('Cuenta de Ahorro / <numero>' o 'Cuenta Corriente / <numero>'). "
            "Verifica que sea un PDF exportado desde Banca en Linea de Popular."
        )
    return {"tipo_cuenta": m.group(1), "numero_cuenta": m.group(2)}


def _agrupar_en_lineas(palabras: list[dict]) -> list[list[dict]]:
    """Agrupa palabras individuales (con su propia coordenada 'top') en
    lineas visuales, y ordena las palabras de cada linea de izquierda a
    derecha por 'x0'. No depende de que pdfplumber detecte columnas."""
    lineas: list[dict] = []
    for palabra in sorted(palabras, key=lambda p: (p["top"], p["x0"])):
        for linea in lineas:
            if abs(linea["top"] - palabra["top"]) <= TOLERANCIA_MISMA_LINEA_PX:
                linea["palabras"].append(palabra)
                break
        else:
            lineas.append({"top": palabra["top"], "palabras": [palabra]})

    for linea in lineas:
        linea["palabras"].sort(key=lambda p: p["x0"])
    lineas.sort(key=lambda l: l["top"])
    return [linea["palabras"] for linea in lineas]


def _clasificar_palabras_de_linea(palabras_linea: list[dict]) -> dict:
    """Separa las palabras de una linea en fechas / montos / referencia /
    descripcion, segun el PATRON de cada palabra - nunca segun su
    posicion de columna."""
    fechas = [p for p in palabras_linea if PATRON_FECHA_TOKEN.match(p["text"])]
    fechas.sort(key=lambda p: p["x0"])

    montos = [p for p in palabras_linea if PATRON_MONTO_TOKEN.match(p["text"])]
    montos.sort(key=lambda p: p["x0"])

    usadas = {id(p) for p in fechas} | {id(p) for p in montos}

    referencias = [
        p for p in palabras_linea
        if id(p) not in usadas and PATRON_REFERENCIA_TOKEN.match(p["text"])
    ]
    usadas |= {id(p) for p in referencias}

    resto = [p for p in palabras_linea if id(p) not in usadas]
    resto.sort(key=lambda p: p["x0"])
    descripcion = " ".join(p["text"] for p in resto)

    return {"fechas": fechas, "montos": montos, "referencias": referencias, "descripcion": descripcion}


def _finalizar_transaccion(actual: dict) -> dict | None:
    """Convierte una transaccion acumulada a su forma final, o None si le
    faltan datos esenciales (se descarta en vez de inventar un numero)."""
    if actual["monto_raw"] is None or actual["balance_raw"] is None:
        return None

    fecha = datetime.strptime(actual["fecha_efectiva_raw"], "%d/%m/%Y").date()
    monto = _limpiar_monto(actual["monto_raw"])
    balance = _limpiar_monto(actual["balance_raw"])
    es_debito = actual["monto_raw"].endswith("-")
    descripcion = " ".join(p for p in actual["descripcion_partes"] if p).strip()

    return {
        "fecha": fecha,
        "referencia": actual["referencia"],
        "codigo_movimiento": "",  # Popular no expone un codigo separado como BHD
        "descripcion": descripcion,
        "debito": monto if es_debito else 0.0,
        "credito": 0.0 if es_debito else monto,
        "saldo": balance,
    }


def _extraer_transacciones(lineas: list[list[dict]]) -> list[dict]:
    movimientos = []
    actual = None

    for palabras_linea in lineas:
        clasificado = _clasificar_palabras_de_linea(palabras_linea)
        es_inicio_de_transaccion = len(clasificado["fechas"]) >= 2

        if es_inicio_de_transaccion:
            if actual is not None:
                finalizada = _finalizar_transaccion(actual)
                if finalizada is not None and not _es_duplicado_de_salto_de_pagina(movimientos, finalizada):
                    movimientos.append(finalizada)

            montos = clasificado["montos"]
            actual = {
                "fecha_efectiva_raw": clasificado["fechas"][1]["text"],
                "referencia": clasificado["referencias"][0]["text"] if clasificado["referencias"] else "",
                "descripcion_partes": [clasificado["descripcion"]] if clasificado["descripcion"] else [],
                "monto_raw": montos[0]["text"] if len(montos) >= 2 else None,
                "balance_raw": montos[-1]["text"] if len(montos) >= 2 else None,
            }
        elif actual is not None and clasificado["descripcion"]:
            actual["descripcion_partes"].append(clasificado["descripcion"])

    if actual is not None:
        finalizada = _finalizar_transaccion(actual)
        if finalizada is not None and not _es_duplicado_de_salto_de_pagina(movimientos, finalizada):
            movimientos.append(finalizada)

    return movimientos


def _es_duplicado_de_salto_de_pagina(movimientos_ya_procesados: list[dict], candidata: dict) -> bool:
    """
    Detecta si `candidata` es un duplicado exacto de la ULTIMA transaccion
    ya procesada - esto pasa cuando el PDF repite la ultima fila de una
    pagina como "vista previa" al inicio de la siguiente (el texto existe
    dos veces en el PDF real, no es un error de extraccion nuestro).

    Se compara fecha, referencia, monto y balance - si los 4 coinciden
    exacto, es casi con certeza la misma transaccion repetida por el
    salto de pagina, no una coincidencia real (dos transacciones
    genuinamente distintas con exactamente el mismo monto Y el mismo
    balance resultante en el mismo dia es virtualmente imposible).
    """
    if not movimientos_ya_procesados:
        return False
    anterior = movimientos_ya_procesados[-1]
    return (
        anterior["fecha"] == candidata["fecha"]
        and anterior["referencia"] == candidata["referencia"]
        and anterior["debito"] == candidata["debito"]
        and anterior["credito"] == candidata["credito"]
        and anterior["saldo"] == candidata["saldo"]
    )


def _validar_cadena_de_balances(movimientos: list[dict]) -> list[str]:
    """
    Valida que balance[i] = balance[i-1] + credito[i] - debito[i] a partir
    de la segunda transaccion (Popular no declara saldo inicial, a
    diferencia de BHD, asi que la primera no se puede verificar sola).

    Cuando una fila no cuadra, se registra como ADVERTENCIA y se
    RESINCRONIZA (se usa el balance que esa fila declara como base para
    seguir verificando las siguientes) en vez de abortar la carga del
    mes completo por una sola fila rara. Si mas del 5% de las filas no
    cuadran, se lanza error de todas formas - a esa altura ya no es una
    fila aislada, es señal de un problema estructural.
    """
    advertencias = []
    for i in range(1, len(movimientos)):
        anterior, actual = movimientos[i - 1], movimientos[i]
        esperado = round(anterior["saldo"] + actual["credito"] - actual["debito"], 2)
        if abs(esperado - actual["saldo"]) > 0.01:
            advertencias.append(
                f"Transaccion #{i + 1} ({actual['fecha']}, ref '{actual['referencia']}'): "
                f"se esperaba balance {esperado} (balance anterior {anterior['saldo']} "
                f"+ credito {actual['credito']} - debito {actual['debito']}), "
                f"pero el extracto declara {actual['saldo']}. Se resincronizo desde este "
                "punto - revisar esta fila manualmente para confirmar que es correcta."
            )

    porcentaje = len(advertencias) / len(movimientos) if movimientos else 0
    if porcentaje > 0.05:
        raise ErrorParseoPopular(
            f"{len(advertencias)} de {len(movimientos)} transacciones ({porcentaje:.1%}) "
            "no cuadran contra la transaccion anterior - demasiado para ser filas aisladas. "
            "Detalle:\n" + "\n".join(advertencias)
        )

    return advertencias


def parse_popular_statement(filepath: str) -> dict:
    """
    Parsea un extracto Popular (Ahorro USD o Corriente RD$) clasificando
    cada palabra por su propio patron de texto (fecha/monto/referencia),
    no por la columna donde pdfplumber la haya colocado - ver el
    docstring del modulo para el porque de este diseño.

    Retorna un dict con: numero_cuenta, tipo_cuenta, periodo_inicio,
    periodo_fin, movimientos (list[dict]), advertencias (list[str]).

    Lanza ErrorParseoPopular si el encabezado no se puede leer, si no se
    encuentra ninguna transaccion, o si mas del 5% de las filas no
    cuadran en la cadena de balances.
    """
    with pdfplumber.open(filepath) as pdf:
        encabezado = _parsear_encabezado(pdf.pages[0].extract_text())

        todas_las_lineas = []
        for pagina in pdf.pages:
            palabras = pagina.extract_words()
            todas_las_lineas.extend(_agrupar_en_lineas(palabras))

    movimientos = _extraer_transacciones(todas_las_lineas)
    if not movimientos:
        raise ErrorParseoPopular(f"No se pudo extraer ninguna transaccion valida de {filepath}.")

    advertencias = _validar_cadena_de_balances(movimientos)

    fechas = [m["fecha"] for m in movimientos]

    return {
        **encabezado,
        "periodo_inicio": min(fechas),
        "periodo_fin": max(fechas),
        "movimientos": movimientos,
        "advertencias": advertencias,
    }


if __name__ == "__main__":
    # Prueba rapida manual: python parser_popular.py archivo.pdf
    import sys
    resultado = parse_popular_statement(sys.argv[1])
    print(f"Cuenta {resultado['numero_cuenta']} ({resultado['tipo_cuenta']}) "
          f"{resultado['periodo_inicio']} -> {resultado['periodo_fin']}")
    print(f"{len(resultado['movimientos'])} movimientos")
    if resultado["advertencias"]:
        print(f"\n⚠ {len(resultado['advertencias'])} advertencia(s) - revisar manualmente:")
        for a in resultado["advertencias"]:
            print(f"  - {a}")
    else:
        print("Cadena de balances OK, sin advertencias.")
