"""Read-only access to the Odoo database, used to learn employees' job titles and companies."""

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

from django.conf import settings

logger = logging.getLogger('powerbi_report')


# Standard Odoo tables: the employee's matricule (matches the AD identifier), full name,
# recruitment date, the active flag, current job title, company and phone (mobile, else work).
# Override with ODOO_EMPLOYEE_QUERY (same columns, in this order; the phone may be left out)
# when the database differs.
DEFAULT_QUERY = """
SELECT
    e.matricule,
    btrim(concat_ws(' ', e.firstname, e.name)),
    e.date_recruit,
    e.active,
    COALESCE(NULLIF(btrim(e.job_title), ''), j.name),
    c.name,
    COALESCE(NULLIF(btrim(e.mobile_phone), ''), NULLIF(btrim(e.work_phone), ''))
FROM hr_employee e
LEFT JOIN hr_job j ON j.id = e.job_id
LEFT JOIN res_company c ON c.id = e.company_id
"""


class OdooError(Exception):
    """Odoo is not configured, unreachable, or returned something unexpected."""


@dataclass(frozen=True)
class OdooEmployee:
    employee_id: str
    full_name: str
    post_title: str
    hire_date: Optional[date]
    company: str = ''
    phone: str = ''


def is_configured() -> bool:
    return bool(settings.ODOO_DB_HOST and settings.ODOO_DB_NAME and settings.ODOO_DB_USER)


def _truthy(value) -> bool:
    return value in (True, 1) or str(value).strip().lower() in {'t', 'true', '1', 'yes', 'oui'}


def connect():
    """Open a read-only connection to the Odoo database."""
    if not is_configured():
        raise OdooError("Odoo n'est pas configuré (ODOO_DB_* dans le fichier .env).")
    try:
        import psycopg2
    except ImportError as exc:  # pragma: no cover - psycopg2 is a hard dependency
        raise OdooError("psycopg2 est requis pour lire Odoo.") from exc

    try:
        return psycopg2.connect(
            host=settings.ODOO_DB_HOST,
            port=settings.ODOO_DB_PORT,
            dbname=settings.ODOO_DB_NAME,
            user=settings.ODOO_DB_USER,
            password=settings.ODOO_DB_PASSWORD,
            sslmode=settings.ODOO_DB_SSLMODE,
            connect_timeout=settings.ODOO_DB_TIMEOUT,
            # Never write, never hang: this account is only meant to read.
            options="-c default_transaction_read_only=on -c statement_timeout=60000",
        )
    except psycopg2.Error as exc:
        logger.error("Odoo connection failed: %s", exc)
        raise OdooError(f"Connexion à Odoo impossible : {exc}") from exc


def fetch_current_employees() -> list[OdooEmployee]:
    """One row per employee: their current position (is_current), latest hire date wins."""
    import psycopg2

    connection = connect()
    try:
        with connection, connection.cursor() as cursor:
            cursor.execute(settings.ODOO_EMPLOYEE_QUERY or DEFAULT_QUERY)
            rows = cursor.fetchall()
    except psycopg2.Error as exc:
        logger.error("Odoo query failed: %s", exc)
        raise OdooError(
            f"Requête Odoo en échec : {exc} "
            "(`python manage.py inspect_odoo --test` vérifie la requête ; ODOO_EMPLOYEE_QUERY dans .env permet de la remplacer)"
        ) from exc
    finally:
        connection.close()

    latest: dict[str, OdooEmployee] = {}
    for row in rows:
        employee_id, full_name, hire_date, is_current, post_title, company = row[:6]
        phone = row[6] if len(row) > 6 else ''
        if not _truthy(is_current) or not full_name or not post_title:
            continue
        employee = OdooEmployee(
            employee_id=str(employee_id or '').strip(),
            full_name=str(full_name).strip(),
            post_title=str(post_title).strip(),
            hire_date=hire_date,
            company=str(company or '').strip(),
            phone=str(phone or '').strip(),
        )
        known = latest.get(employee.employee_id)
        if known is None or (hire_date and (known.hire_date is None or hire_date > known.hire_date)):
            latest[employee.employee_id] = employee
    return list(latest.values())
