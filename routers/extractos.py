"""
routers/extractos.py
Subida de estados de cuenta (PDF). Por ahora solo soporta el formato BHD -
parser_popular.py todavia no esta escrito, asi que un PDF de Popular
va a fallar con un mensaje claro en vez de un error confuso.
"""

import os
import tempfile

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy import text
from sqlalchemy.orm import Session

from db import get_db
from parser_bhd import ErrorParseoBHD, parse_bhd_statement
from reglas_conciliacion import clasificar_estado_inicial, ejecutar_sugerencias
from routers.auth import requiere_rol

router = APIRouter(prefix="/api/extractos", tags=["extractos"])


@router.post("/upload")
async def subir_extracto(
    archivo: UploadFile,
    db: Session = Depends(get_db),
    usuario: dict = Depends(requiere_rol("admin", "contabilidad")),
):
    if not archivo.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="Solo se aceptan archivos PDF.")

    # Guardar en un archivo temporal - los parsers trabajan sobre una ruta en disco
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(await archivo.read())
        ruta_temp = tmp.name

    try:
        try:
            datos = parse_bhd_statement(ruta_temp)
        except ErrorParseoBHD as e:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"No se pudo leer '{archivo.filename}' como extracto BHD ({e}). "
                    "Si es un extracto de Popular, ese formato todavia no esta soportado."
                ),
            )

        cuenta = db.execute(
            text("SELECT id, moneda FROM cuentas_bancarias WHERE numero_cuenta = :n AND activa = TRUE"),
            {"n": datos["numero_cuenta"]},
        ).mappings().first()

        if not cuenta:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"La cuenta {datos['numero_cuenta']} no esta registrada. "
                    "Agregala primero en Backoffice > Cuentas bancarias."
                ),
            )

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
                    "cuenta_id": cuenta["id"],
                    "fecha": mov["fecha"],
                    "referencia": mov["referencia"],
                    "codigo_movimiento": mov["codigo_movimiento"],
                    "descripcion": mov["descripcion"],
                    "debito": mov["debito"],
                    "credito": mov["credito"],
                    "saldo": mov["saldo"],
                    "archivo_origen": archivo.filename,
                    "estado_conciliacion": estado_inicial,
                },
            ).first()
            if resultado:
                nuevos += 1
            else:
                duplicados += 1
        db.commit()

        sugerencias = ejecutar_sugerencias(db)

        return {
            "cuenta": datos["numero_cuenta"],
            "moneda": datos["moneda"],
            "periodo_inicio": str(datos["periodo_inicio"]),
            "periodo_fin": str(datos["periodo_fin"]),
            "movimientos_nuevos": nuevos,
            "movimientos_duplicados": duplicados,
            "ya_cargado": nuevos == 0 and duplicados > 0,
            **sugerencias,
        }
    finally:
        os.unlink(ruta_temp)
