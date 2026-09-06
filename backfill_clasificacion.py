"""
backfill_clasificacion.py
Script de UNA SOLA VEZ: reclasifica los movimientos que ya se cargaron
ANTES de que existiera clasificar_estado_inicial() (todos habian quedado
en 'por_conciliar' por el default viejo del schema).

No toca nada que ya haya sido resuelto a mano (resuelto_por IS NOT NULL),
ni nada que ya tenga una sugerencia automatica pendiente de confirmar.

Uso:
    python backfill_clasificacion.py
"""

from sqlalchemy import text

from db import SessionLocal
from reglas_conciliacion import clasificar_estado_inicial


def main():
    db = SessionLocal()
    try:
        filas = db.execute(
            text("""
                SELECT id, codigo_movimiento, descripcion
                FROM movimientos_banco
                WHERE estado_conciliacion = 'por_conciliar'
                  AND resuelto_por IS NULL
                  AND sugerencia_auto IS NULL
            """)
        ).mappings().all()

        actualizados_a_confirmado = 0
        for fila in filas:
            nuevo_estado = clasificar_estado_inicial(fila["codigo_movimiento"], fila["descripcion"])
            if nuevo_estado == "confirmado":
                db.execute(
                    text("UPDATE movimientos_banco SET estado_conciliacion = 'confirmado' WHERE id = :id"),
                    {"id": fila["id"]},
                )
                actualizados_a_confirmado += 1

        db.commit()
        print(f"Revisados: {len(filas)} movimientos pendientes.")
        print(f"Reclasificados a 'confirmado': {actualizados_a_confirmado}")
        print(f"Se quedaron en 'por_conciliar' (patron ambiguo real): {len(filas) - actualizados_a_confirmado}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
