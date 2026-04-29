"""Postgres connection helper for Innovite CRM.

Uses Supabase's transaction pooler (port 6543) via DATABASE_URL.

Usage:
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("select * from crm.clients")
            rows = cur.fetchall()
"""
import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get('DATABASE_URL')


@contextmanager
def get_conn():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL not set. Copy from Supabase → "
            "Project Settings → Database → Connection string → Transaction pooler."
        )
    with psycopg.connect(DATABASE_URL) as conn:
        yield conn


def fetch_all(sql: str, params: tuple = ()) -> list[dict]:
    """Run a SELECT and return rows as a list of dicts."""
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def fetch_one(sql: str, params: tuple = ()) -> dict | None:
    """Run a SELECT and return the first row as a dict (or None)."""
    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchone()


def execute(sql: str, params: tuple = ()) -> None:
    """Run an INSERT/UPDATE/DELETE."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
