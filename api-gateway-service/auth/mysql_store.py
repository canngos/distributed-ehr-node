import os
from contextlib import contextmanager
from typing import Dict, Iterator, Optional

import bcrypt
import pymysql
from pymysql.cursors import DictCursor

CREATE_USERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_name VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NULL,
    doctor_id VARCHAR(128) NULL,
    patient_id VARCHAR(128) NULL,
    role ENUM('patient', 'doctor') NOT NULL,
    user_status ENUM('pending', 'active', 'inactive') NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
)
"""


def _primary_connection_params() -> Dict[str, object]:
    return {
        "host": os.getenv("MYSQL_PRIMARY_HOST", "localhost"),
        "port": int(os.getenv("MYSQL_PRIMARY_PORT", "3306")),
        "user": os.getenv("MYSQL_AUTH_USER", "auth_user"),
        "password": os.getenv("MYSQL_AUTH_PASSWORD", "auth_password"),
        "database": os.getenv("MYSQL_AUTH_DATABASE", "auth_db"),
        "cursorclass": DictCursor,
        "autocommit": True,
        "connect_timeout": 5,
        "read_timeout": 5,
        "write_timeout": 5,
    }


@contextmanager
def primary_connection() -> Iterator[pymysql.connections.Connection]:
    conn = pymysql.connect(**_primary_connection_params())
    try:
        yield conn
    finally:
        conn.close()


def initialize_schema() -> None:
    root_conn = pymysql.connect(
        host=os.getenv("MYSQL_PRIMARY_HOST", "localhost"),
        port=int(os.getenv("MYSQL_PRIMARY_PORT", "3306")),
        user=os.getenv("MYSQL_AUTH_USER", "auth_user"),
        password=os.getenv("MYSQL_AUTH_PASSWORD", "auth_password"),
        cursorclass=DictCursor,
        autocommit=True,
        connect_timeout=5,
    )
    try:
        database_name = os.getenv("MYSQL_AUTH_DATABASE", "auth_db")
        with root_conn.cursor() as cursor:
            try:
                cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{database_name}`")
            except pymysql.MySQLError:
                # In managed setups the auth user may not have CREATE DATABASE privilege.
                pass
    finally:
        root_conn.close()

    with primary_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(CREATE_USERS_TABLE_SQL)

    _migrate_users_table()

    _seed_default_users()

def _migrate_users_table() -> None:
    """Apply non-destructive schema/data migrations for auth.users."""
    with primary_connection() as conn:
        with conn.cursor() as cursor:
            # Legacy status value used by older deployments.
            try:
                cursor.execute(
                    "UPDATE users SET user_status = 'active' WHERE user_status = 'registered'"
                )
            except pymysql.MySQLError:
                pass

            cursor.execute("ALTER TABLE users MODIFY COLUMN password_hash VARCHAR(255) NULL")
            cursor.execute(
                "ALTER TABLE users MODIFY COLUMN user_status ENUM('pending','active','inactive') NOT NULL DEFAULT 'pending'"
            )


# Default accounts that must always exist in the system.
# These are created once on startup and never overwritten.
_DEFAULT_USERS = [
    {
        "username": "doctor1",
        "password": "test",
        "role": "doctor",
        "doctor_id": "DEFAULT-DOCTOR-001",
        "patient_id": None,
        "user_status": "active",
    },
]


def _seed_default_users() -> None:
    """Insert default users if they do not already exist."""
    for user in _DEFAULT_USERS:
        try:
            existing = get_user_by_username(user["username"])
            if existing is not None:
                continue  # already seeded, skip

            password_hash = bcrypt.hashpw(
                user["password"].encode("utf-8"), bcrypt.gensalt()
            ).decode("utf-8")

            create_user(
                username=user["username"],
                password_hash=password_hash,
                role=user["role"],
                user_status=user["user_status"],
                doctor_id=user.get("doctor_id"),
                patient_id=user.get("patient_id"),
            )
            print(f"[Auth] Default user seeded: {user['username']} (role={user['role']})")
        except pymysql.MySQLError as exc:
            print(f"[Auth] Failed to seed default user '{user['username']}': {exc}")


def get_user_by_username(username: str) -> Optional[Dict[str, object]]:
    query = """
    SELECT id, user_name, password_hash, doctor_id, patient_id, role, user_status
    FROM users
    WHERE user_name = %s
    """
    with primary_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query, (username,))
            return cursor.fetchone()


def create_user(
    username: str,
    password_hash: Optional[str],
    role: str,
    user_status: str,
    doctor_id: Optional[str] = None,
    patient_id: Optional[str] = None,
) -> None:
    query = """
    INSERT INTO users (user_name, password_hash, doctor_id, patient_id, role, user_status)
    VALUES (%s, %s, %s, %s, %s, %s)
    """
    with primary_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                query,
                (username, password_hash, doctor_id, patient_id, role, user_status),
            )


def set_password_and_activate_user(username: str, password_hash: str) -> bool:
    query = """
    UPDATE users
    SET password_hash = %s,
        user_status = 'active'
    WHERE user_name = %s AND user_status = 'pending'
    """
    with primary_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query, (password_hash, username))
            return cursor.rowcount == 1
