from app.server import app, ensure_runtime_started  # noqa: F401 – gunicorn lädt wsgi:app


ensure_runtime_started()
