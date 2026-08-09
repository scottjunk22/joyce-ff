"""
WSGI entry point for production hosts (gunicorn, PythonAnywhere, etc.).

    gunicorn wsgi:app

The database location comes from the JOYCE_DB_PATH environment variable (point
it at a persistent path on the host); it falls back to data/league.sqlite.
"""

from joyce_ff.webapp import create_app

app = create_app()
