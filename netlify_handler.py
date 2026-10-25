"""ASGI entry point for Python hosts (run uvicorn netlify_handler:app).

Netlify uses netlify/functions/api.mjs to proxy to this separately hosted API.
"""
from src.main import app
