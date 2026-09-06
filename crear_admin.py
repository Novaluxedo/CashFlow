"""
crear_admin.py
Script de UNA SOLA VEZ para crear el primer usuario Admin.
Corre esto localmente (con tu .env ya configurado con NEON_URL):

    python crear_admin.py

Te va a pedir el username y la contrasena por teclado - la contrasena
nunca se guarda en texto plano, se hashea con bcrypt antes de insertarla.

Una vez tengas el backoffice de usuarios funcionando (mas adelante),
ya no vas a necesitar correr este script nunca mas.
"""

import getpass

from sqlalchemy import text

from db import SessionLocal
from security import hashear_password


def main():
    username = input("Username del Admin: ").strip()
    password = getpass.getpass("Contrasena: ")
    password_confirmacion = getpass.getpass("Confirma la contrasena: ")

    if password != password_confirmacion:
        print("Las contrasenas no coinciden. Intenta de nuevo.")
        return

    if len(password) < 8:
        print("Usa una contrasena de al menos 8 caracteres.")
        return

    password_hash = hashear_password(password)

    db = SessionLocal()
    try:
        existente = db.execute(
            text("SELECT id FROM usuarios WHERE username = :u"),
            {"u": username},
        ).first()

        if existente:
            print(f"Ya existe un usuario con username '{username}'. No se creo nada.")
            return

        db.execute(
            text(
                "INSERT INTO usuarios (username, password_hash, rol, activo, pendiente_setup) "
                "VALUES (:username, :password_hash, 'admin', TRUE, FALSE)"
            ),
            {"username": username, "password_hash": password_hash},
        )
        db.commit()
        print(f"Usuario Admin '{username}' creado correctamente. Ya puedes iniciar sesion.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
