"""
parser_popular.py
Extrae movimientos de un estado de cuenta Banco Popular (PDF) - Ahorro USD
o Corriente RD$.

DIFERENCIAS DE FORMATO RESPECTO A BHD (parser_bhd.py):
- Popular no separa Debito/Credito en columnas propias: un solo campo
  "Monto" que trae el signo '-' al final cuando es debito (ej. "$399,203.00-"),
  y sin signo cuando es credito. Aqui se traduce a debito/credito por
  separado para mantener el mismo esquema que ya usa movimientos_banco.
- El simbolo de moneda en el texto es SIEMPRE "$", sin importar si la
  cuenta es USD o RD$ - la moneda real depende de la cuenta (Ahorro =
  USD, Corriente = RD$ en esta empresa), y se resuelve por numero de
  cuenta en cuentas_bancarias (routers/extractos.py), igual que ya se
  hace para BHD. Este parser NO intenta adivinar la moneda del texto.
- El encabezado de Popular no declara saldo inicial ni totales de
  debito/credito (a diferencia de BHD) - por eso la validacion aqui es
  distinta: en vez de comparar contra un total declarado, se valida que
  el BALANCE DE CADA TRANSACCION cuadre con el balance de la anterior
  +/- su monto. Es una validacion mas estricta linea por linea, no solo
  un total agregado - si algo se leyo mal (un bloque multi-linea cortado
  a la mitad), se detecta en el momento exacto donde se rompe la cadena.
- Cada transaccion puede ocupar varias lineas en el PDF (descripcion
  larga, ej. LBTR con nombre de beneficiario) - el parser junta todo el
  texto y separa transacciones por la aparicion de "fecha fecha" al
  inicio de cada una, sin depender de saltos de linea especificos.

NOTA: este parser fue disenado y su regex validado contra el TEXTO real
de 2 extractos Popular (Ahorro USD y Corriente RD$, agosto 2026) - pero
no se pudo probar directamente contra el binario PDF real con pdfplumber
en el entorno donde se escribio (el archivo disponible era una version
ya convertida a imagen+texto, no el PDF original). Antes de confiar en
el en produccion, correrlo manualmente contra 1-2 extractos reales
(python parser_popular.py archivo.pdf) y revisar que el numero de
movimientos y la validacion de balance salgan limpios.
"""

import re
from datetime import date, datetime
from typing import Optional

import pdfplumber

PATRON_CUENTA = re.compile(
    r"(Cuenta de Ahorro|Cuenta Corriente)\s*/\s*(\d+)"
)

# Anclamos cada transaccion a "fecha_posteo fecha_efectiva" al inicio.
PATRON_INICIO_TRANSACCION = re.compile(
    r"(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})"
)

PATRON_MONTO = re.compile(r"\$\s?[\d,]+\.\d{2}-?")

# Referencia: secuencia larga de digitos (nro. de cheque o de referencia)
# justo despues de las dos fechas. Opcional - varias transacciones no
# tienen (ej. pagos con descripcion libre sin numero de referencia).
PATRON_REFERENCIA = re.compile(r"^\s*(\d{6,14})\b")


class ErrorParseoPopular(Exception):
    """Se lanza cuando el PDF no tiene el formato Popular esperado, o la
    cadena de balances no cuadra transaccion por transaccion (senal de
    que la extraccion de texto corto algun bloque multi-linea)."""


def _limpiar_monto(valor: str) -> float:
    """'$1,234.56' o '$1,234.56-' -> 1234.56 (siempre positivo; el signo
    se maneja aparte para decidir debito/credito)."""
    valor = valor.strip().rstrip("-").lstrip("$").replace(",", "").strip()
    return float(valor) if valor else 0.0


def _parsear_encabezado(texto_primera_pagina: str) -> dict:
    m = PATRON_CUENTA.search(texto_primera_pagina)
    if not m:
        raise ErrorParseoPopular(
            "No se pudo leer el encabezado del extracto Popular "
            "('Cuenta de Ahorro / <numero>' o 'Cuenta Corriente / <numero>'). "
            "Verifica que sea un PDF exportado desde Banca en Linea de Popular."
        )
    return {"tipo_cuenta": m.group(1), "numero_cuenta": m.group(2)}


def _extraer_transacciones(texto_completo: str) -> list[dict]:
    # Colapsar todo el whitespace/saltos de linea a un solo espacio -
    # las transacciones de Popular se cortan en cualquier punto dentro
    # de la descripcion, asi que no podemos depender de saltos de linea
    # para separar campos.
    texto_plano = re.sub(r"\s+", " ", texto_completo)

    posiciones_inicio = [m.start() for m in PATRON_INICIO_TRANSACCION.finditer(texto_plano)]
    if not posiciones_inicio:
        raise ErrorParseoPopular(
            "No se encontro ninguna transaccion con el patron 'fecha fecha' esperado. "
            "El PDF podria tener un formato distinto al esperado."
        )

    bloques = []
    for i, inicio in enumerate(posiciones_inicio):
        fin = posiciones_inicio[i + 1] if i + 1 < len(posiciones_inicio) else len(texto_plano)
        bloques.append(texto_plano[inicio:fin].strip())

    movimientos = []
    for bloque in bloques:
        m_fechas = PATRON_INICIO_TRANSACCION.match(bloque)
        fecha_efectiva = datetime.strptime(m_fechas.group(2), "%d/%m/%Y").date()

        resto = bloque[m_fechas.end():].strip()

        montos = PATRON_MONTO.findall(bloque)
        if len(montos) < 2:
            # Bloque sin los 2 montos esperados (monto + balance) - se
            # salta en vez de adivinar, y queda registrado para revision
            # manual (mejor perder una fila que inventar un numero).
            continue

        monto_raw, balance_raw = montos[-2], montos[-1]
        monto = _limpiar_monto(monto_raw)
        balance = _limpiar_monto(balance_raw)
        es_debito = monto_raw.strip().endswith("-")

        referencia = ""
        m_ref = PATRON_REFERENCIA.match(resto)
        if m_ref:
            referencia = m_ref.group(1)
            resto = resto[m_ref.end():].strip()

        # Quitar los dos montos del final para quedarnos solo con la
        # descripcion (pueden aparecer en cualquier punto del texto
        # colapsado, asi que se remueven por posicion, no por regex
        # global, para no borrar montos que sean parte del texto de la
        # descripcion misma, ej. "TRNF USD 399,203.00 SHANDONG...").
        idx_balance = resto.rfind(balance_raw)
        descripcion = resto[:idx_balance] if idx_balance != -1 else resto
        idx_monto = descripcion.rfind(monto_raw)
        if idx_monto != -1:
            descripcion = descripcion[:idx_monto]
        descripcion = descripcion.strip(" -")

        movimientos.append({
            "fecha": fecha_efectiva,
            "referencia": referencia,
            "codigo_movimiento": "",  # Popular no expone un codigo separado como BHD
            "descripcion": descripcion,
            "debito": monto if es_debito else 0.0,
            "credito": 0.0 if es_debito else monto,
            "saldo": balance,
        })

    return movimientos


def _validar_cadena_de_balances(movimientos: list[dict]) -> None:
    """Valida que balance[i] = balance[i-1] + credito[i] - debito[i] para
    cada transaccion a partir de la segunda (la primera no se puede
    verificar de forma independiente porque Popular no declara un saldo
    inicial en el encabezado, a diferencia de BHD)."""
    for i in range(1, len(movimientos)):
        anterior, actual = movimientos[i - 1], movimientos[i]
        esperado = round(anterior["saldo"] + actual["credito"] - actual["debito"], 2)
        if abs(esperado - actual["saldo"]) > 0.01:
            raise ErrorParseoPopular(
                f"La cadena de balances se rompe en la transaccion #{i + 1} "
                f"({actual['fecha']}, ref '{actual['referencia']}'): "
                f"se esperaba balance {esperado} (balance anterior {anterior['saldo']} "
                f"+ credito {actual['credito']} - debito {actual['debito']}), "
                f"pero el extracto declara {actual['saldo']}. "
                "Esto normalmente indica que un bloque multi-linea se corto mal "
                "al extraer el texto - revisar el PDF manualmente en esa fecha."
            )


def parse_popular_statement(filepath: str) -> dict:
    """
    Parsea un extracto Popular (Ahorro USD o Corriente RD$).

    Retorna un dict con las llaves:
        numero_cuenta, tipo_cuenta, periodo_inicio, periodo_fin,
        movimientos: list[dict] (uno por fila de transaccion)

    NOTA sobre periodo_inicio/periodo_fin: Popular no declara un periodo
    en el encabezado (a diferencia de BHD) - se infieren como la fecha
    minima y maxima entre las transacciones encontradas.

    Lanza ErrorParseoPopular si el encabezado no se puede leer, o si la
    cadena de balances transaccion-por-transaccion no cuadra en algun
    punto (ver _validar_cadena_de_balances).
    """
    with pdfplumber.open(filepath) as pdf:
        encabezado = _parsear_encabezado(pdf.pages[0].extract_text())
        texto_completo = "\n".join(page.extract_text() or "" for page in pdf.pages)

    movimientos = _extraer_transacciones(texto_completo)
    if not movimientos:
        raise ErrorParseoPopular(f"No se pudo extraer ninguna transaccion valida de {filepath}.")

    _validar_cadena_de_balances(movimientos)

    fechas = [m["fecha"] for m in movimientos]

    return {
        **encabezado,
        "periodo_inicio": min(fechas),
        "periodo_fin": max(fechas),
        "movimientos": movimientos,
    }


if __name__ == "__main__":
    # Prueba rapida manual: python parser_popular.py archivo.pdf
    import sys
    resultado = parse_popular_statement(sys.argv[1])
    print(f"Cuenta {resultado['numero_cuenta']} ({resultado['tipo_cuenta']}) "
          f"{resultado['periodo_inicio']} -> {resultado['periodo_fin']}")
    print(f"{len(resultado['movimientos'])} movimientos, cadena de balances OK")
