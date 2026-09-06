"""
parsers/parser_bhd.py
Extrae movimientos de un estado de cuenta BHD (PDF) en formato tabular limpio.

Validado contra los extractos reales: los totales calculados (suma de
debitos/creditos por fila) cuadran exacto con los totales que el propio
BHD declara en el encabezado de cada extracto.
"""

import re
from datetime import date, datetime
from typing import Optional

import pdfplumber

MONEDA_MAP = {"US$": "USD", "RD$": "RD$"}

PATRON_ENCABEZADO = re.compile(
    r"([\w.,\s]+S\.R\.L\.)\s+(\d+)\s+(\d+)\s+"
    r"(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+"
    r"(US\$|RD\$)\s*([\d,]+\.\d{2})\s+"
    r"(US\$|RD\$)\s*([\d,]+\.\d{2})\s+"
    r"(US\$|RD\$)\s*([\d,]+\.\d{2})\s+"
    r"(US\$|RD\$)\s*([\d,]+\.\d{2})"
)


class ErrorParseoBHD(Exception):
    """Se lanza cuando el PDF no tiene el formato BHD esperado, o los
    totales calculados no cuadran con los declarados en el encabezado."""


def _limpiar_monto(valor: Optional[str]) -> float:
    """Convierte 'US$ 1,234.56' o 'RD$ 1,234.56' a float. '' o None -> 0.0."""
    if not valor:
        return 0.0
    valor = valor.strip()
    for simbolo in ("US$", "RD$"):
        if valor.startswith(simbolo):
            valor = valor[len(simbolo):]
            break
    valor = valor.replace(",", "").strip()
    return float(valor) if valor else 0.0


def _parsear_encabezado(texto_primera_pagina: str) -> dict:
    m = PATRON_ENCABEZADO.search(texto_primera_pagina)
    if not m:
        raise ErrorParseoBHD(
            "No se pudo leer el encabezado del extracto BHD "
            "(Empresa/RNC/Numero de cuenta/Periodo/Saldos). "
            "Verifica que sea un PDF exportado desde BHD Internet Banking Empresarial."
        )
    return {
        "numero_cuenta": m.group(3),
        "periodo_inicio": datetime.strptime(m.group(4), "%d/%m/%Y").date(),
        "periodo_fin": datetime.strptime(m.group(5), "%d/%m/%Y").date(),
        "moneda": MONEDA_MAP[m.group(6)],
        "saldo_inicial": float(m.group(7).replace(",", "")),
        "total_debito": float(m.group(9).replace(",", "")),
        "total_credito": float(m.group(11).replace(",", "")),
        "saldo_final": float(m.group(13).replace(",", "")),
    }


def parse_bhd_statement(filepath: str) -> dict:
    """
    Parsea un extracto BHD completo.

    Retorna un dict con las llaves:
        numero_cuenta, moneda, periodo_inicio, periodo_fin,
        saldo_inicial, total_debito, total_credito, saldo_final,
        movimientos: list[dict] (uno por fila de transaccion)

    Lanza ErrorParseoBHD si el encabezado no se puede leer, o si la suma
    de los movimientos extraidos no cuadra con los totales declarados
    (senal de que la extraccion de tablas fallo en alguna pagina).
    """
    with pdfplumber.open(filepath) as pdf:
        encabezado = _parsear_encabezado(pdf.pages[0].extract_text())

        movimientos = []
        for page in pdf.pages:
            for tabla in page.extract_tables():
                for fila in tabla[1:]:  # fila[0] es el encabezado de columnas, se repite cada pagina
                    if not fila or not fila[0]:
                        continue
                    fecha_raw = fila[0].strip()
                    if not re.match(r"\d{2}/\d{2}/\d{4}", fecha_raw):
                        continue  # fila basura / de encabezado mal detectada

                    movimientos.append({
                        "fecha": datetime.strptime(fecha_raw, "%d/%m/%Y").date(),
                        "referencia": (fila[1] or "").strip(),
                        "ncf": (fila[2] or "").strip(),
                        "codigo_movimiento": (fila[3] or "").strip(),
                        "descripcion": (fila[4] or "").replace("\n", " ").strip(),
                        "debito": _limpiar_monto(fila[5]),
                        "credito": _limpiar_monto(fila[6]),
                        "saldo": _limpiar_monto(fila[8]),
                    })

    suma_debito = round(sum(m["debito"] for m in movimientos), 2)
    suma_credito = round(sum(m["credito"] for m in movimientos), 2)

    if abs(suma_debito - encabezado["total_debito"]) > 0.01 or \
       abs(suma_credito - encabezado["total_credito"]) > 0.01:
        raise ErrorParseoBHD(
            f"Los totales no cuadran en {filepath}: "
            f"debito extraido={suma_debito} vs declarado={encabezado['total_debito']}, "
            f"credito extraido={suma_credito} vs declarado={encabezado['total_credito']}. "
            "Revisa si el PDF tiene paginas con formato distinto."
        )

    return {**encabezado, "movimientos": movimientos}


if __name__ == "__main__":
    # Prueba rapida manual: python parser_bhd.py archivo.pdf
    import sys
    resultado = parse_bhd_statement(sys.argv[1])
    print(f"Cuenta {resultado['numero_cuenta']} ({resultado['moneda']}) "
          f"{resultado['periodo_inicio']} -> {resultado['periodo_fin']}")
    print(f"{len(resultado['movimientos'])} movimientos, totales cuadrados OK")
