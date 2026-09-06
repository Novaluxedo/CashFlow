"""
routers/validacion.py
Expone la validacion cruzada SAP (cobros/pagos) vs extracto bancario
(movimientos_banco confirmados) como endpoint, para que se vea en Backoffice
en vez de tener que correr un script aparte.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from db import get_db
from routers.auth import usuario_actual

router = APIRouter(prefix="/api/validacion", tags=["validacion"])

TOLERANCIA_DIAS = 3


def _cargar_sap(db: Session, tabla: str, sap_acct_code: str):
    filas = db.execute(
        text(f"SELECT doc_entry, fecha, monto FROM {tabla} WHERE cuenta_banco = :cuenta"),
        {"cuenta": sap_acct_code},
    ).mappings().all()
    return [dict(f) for f in filas]


def _cargar_banco(db: Session, cuenta_id: int, tipo: str):
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
    banco_disponible = list(items_banco)
    emparejados = []
    sap_sin_match = []

    for doc in items_sap:
        candidato = None
        for i, mov in enumerate(banco_disponible):
            if abs(float(mov["monto"]) - float(doc["monto"])) < 0.01 and abs((mov["fecha"] - doc["fecha"]).days) <= TOLERANCIA_DIAS:
                candidato = i
                break
        if candidato is not None:
            emparejados.append((doc, banco_disponible.pop(candidato)))
        else:
            sap_sin_match.append(doc)

    return emparejados, sap_sin_match, banco_disponible


def _comparar_lado(sap_items, banco_items):
    total_sap = sum(float(x["monto"]) for x in sap_items)
    total_banco = sum(float(x["monto"]) for x in banco_items)
    emparejados, sap_sin_match, banco_sin_match = _emparejar(sap_items, banco_items)

    return {
        "total_sap": total_sap,
        "total_banco": total_banco,
        "diferencia": total_banco - total_sap,
        "documentos_sap": len(sap_items),
        "movimientos_banco": len(banco_items),
        "emparejados": len(emparejados),
        "sap_sin_match": [
            {"doc_entry": d["doc_entry"], "fecha": str(d["fecha"]), "monto": float(d["monto"])}
            for d in sap_sin_match
        ],
        "banco_sin_match": [
            {"fecha": str(m["fecha"]), "monto": float(m["monto"]), "descripcion": m["descripcion"]}
            for m in banco_sin_match
        ],
    }


@router.get("")
def validar_sap_banco(db: Session = Depends(get_db), usuario: dict = Depends(usuario_actual)):
    cuentas = db.execute(
        text("""
            SELECT c.id, c.sap_acct_code, c.moneda, COALESCE(c.alias, b.nombre || ' ' || c.numero_cuenta) AS alias
            FROM cuentas_bancarias c
            JOIN catalogo_bancos b ON b.codigo = c.banco_codigo
            WHERE c.sap_acct_code IS NOT NULL AND c.activa = TRUE
        """)
    ).mappings().all()

    resultado = []
    for cuenta in cuentas:
        cobros = _cargar_sap(db, "cobros", cuenta["sap_acct_code"])
        creditos_banco = _cargar_banco(db, cuenta["id"], "credito")
        pagos = _cargar_sap(db, "pagos", cuenta["sap_acct_code"])
        debitos_banco = _cargar_banco(db, cuenta["id"], "debito")

        resultado.append({
            "alias": cuenta["alias"],
            "moneda": cuenta["moneda"],
            "sap_acct_code": cuenta["sap_acct_code"],
            "cobros": _comparar_lado(cobros, creditos_banco),
            "pagos": _comparar_lado(pagos, debitos_banco),
        })

    return resultado
