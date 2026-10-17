"""
Database connection for the Ciliconchip Portal.

Connection details come from Streamlit secrets ([mysql] host, port, database, user, password, ssl_ca).
Without them the app uses a local SQLite file - for testing only.
"""

import os
import tempfile
from typing import Dict, Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import URL

LOCAL_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "portal_local.db")


def make_engine(cfg: Optional[Dict]):
    if not cfg:
        return create_engine(f"sqlite:///{LOCAL_DB}")
    url = URL.create("mysql+pymysql", username=str(cfg["user"]), password=str(cfg["password"]),
                     host=str(cfg["host"]), port=int(cfg.get("port", 3306)), database=str(cfg["database"]),
                     query={"charset": "utf8mb4"})
    connect_args = {"connect_timeout": 10}
    if cfg.get("ssl_ca"):
        ca = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)
        ca.write(str(cfg["ssl_ca"]))
        ca.close()
        connect_args["ssl"] = {"ca": ca.name}
    elif str(cfg.get("ssl", "")).lower() in ("true", "1", "yes", "required"):
        import certifi
        connect_args["ssl"] = {"ca": certifi.where()}
    return create_engine(url, pool_pre_ping=True, pool_recycle=280, pool_size=5, max_overflow=5,
                         connect_args=connect_args)
