"""Uvicorn entry point: uvicorn main:app --reload"""

from app.main import app

__all__ = ["app"]
