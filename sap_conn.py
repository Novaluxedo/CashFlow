"""
sap_conn.py
Conexion a SAP Business One (SQL Server) via pyodbc.
Solo se usa desde sync_flujo.py, que corre en tu PC con VPN activa -
nunca desde main.py / Render.
"""

import os

import pyodbc
from dotenv import load_dotenv

load_dotenv()

DB_DRIVER = os.getenv("DB_DRIVER")
DB_SERVER = os.getenv("DB_SERVER")
DB_DATABASE = os.getenv("DB_DATABASE")
DB_USERNAME = os.getenv("DB_USERNAME")
DB_PASSWORD = os.getenv("DB_PASSWORD")

REQUERIDAS = {
    "DB_DRIVER": DB_DRIVER, "DB_SERVER": DB_SERVER, "DB_DATABASE": DB_DATABASE,
    "DB_USERNAME": DB_USERNAME, "DB_PASSWORD": DB_PASSWORD,
}
faltantes = [k for k, v in REQUERIDAS.items() if not v]
if faltantes:
    raise RuntimeError(f"Faltan variables de entorno para conectar a SAP: {', '.join(faltantes)}")


def conectar_sap() -> pyodbc.Connection:
    """
    Abre una conexion a SAP B1. Requiere VPN activa si SAP no es accesible
    directo desde tu red. Lanza pyodbc.Error si no puede conectar (VPN
    caida, credenciales invalidas, servidor apagado, etc.).
    """
    cadena = (
        f"DRIVER={{{DB_DRIVER}}};"
        f"SERVER={DB_SERVER};"
        f"DATABASE={DB_DATABASE};"
        f"UID={DB_USERNAME};"
        f"PWD={DB_PASSWORD};"
    )
    return pyodbc.connect(cadena, timeout=15)
