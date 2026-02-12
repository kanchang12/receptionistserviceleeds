#!/usr/bin/env python3
"""
VoiceBot — First-time database setup.
Run once to create all tables and an admin user.

Usage:
    python init_db.py

Reads DATABASE_URL from environment or .env file.
"""

import os
import sys
import getpass
import psycopg2

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from werkzeug.security import generate_password_hash
except ImportError:
    print("[ERROR] werkzeug not installed. Run: pip install werkzeug")
    sys.exit(1)


DATABASE_URL = os.environ.get('DATABASE_URL')

if not DATABASE_URL:
    print("=" * 50)
    print("DATABASE_URL not found in environment.")
    print("Enter your PostgreSQL connection string:")
    print("Example: postgresql://user:pass@host:5432/dbname")
    print("=" * 50)
    DATABASE_URL = input("DATABASE_URL: ").strip()
    if not DATABASE_URL:
        print("[ERROR] No database URL provided.")
        sys.exit(1)


def run():
    print("\n🔧 VoiceBot — Database Setup\n")

    # ── Connect ──
    print(f"Connecting to database...")
    try:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        cur = conn.cursor()
        print("✅ Connected.\n")
    except Exception as e:
        print(f"❌ Failed to connect: {e}")
        sys.exit(1)

    # ── Run schema ──
    schema_path = os.path.join(os.path.dirname(__file__), 'schema.sql')
    if not os.path.exists(schema_path):
        print(f"❌ schema.sql not found at {schema_path}")
        sys.exit(1)

    print("Running schema.sql ...")
    with open(schema_path, 'r') as f:
        schema_sql = f.read()

    try:
        cur.execute(schema_sql)
        print("✅ All tables created.\n")
    except psycopg2.errors.DuplicateTable:
        print("⚠️  Tables already exist — skipping schema.\n")
        conn.rollback()
        conn.autocommit = True
    except Exception as e:
        print(f"❌ Schema error: {e}")
        sys.exit(1)

    # ── Check if admin exists ──
    cur.execute("SELECT id FROM users WHERE role='admin' LIMIT 1")
    existing = cur.fetchone()

    if existing:
        print("ℹ️  Admin user already exists — skipping admin creation.")
        print("\n✅ Setup complete. You're good to go.\n")
        cur.close()
        conn.close()
        return

    # ── Create admin user ──
    print("─" * 40)
    print("Create your admin account:")
    print("─" * 40)

    name = input("  Name: ").strip()
    if not name:
        name = "Admin"

    email = input("  Email: ").strip()
    if not email:
        print("❌ Email is required.")
        sys.exit(1)

    password = getpass.getpass("  Password: ")
    if len(password) < 8:
        print("❌ Password must be at least 8 characters.")
        sys.exit(1)

    password_confirm = getpass.getpass("  Confirm password: ")
    if password != password_confirm:
        print("❌ Passwords don't match.")
        sys.exit(1)

    hashed = generate_password_hash(password)

    try:
        cur.execute(
            """INSERT INTO users (name, email, password_hash, role, status)
               VALUES (%s, %s, %s, 'admin', 'active')
               RETURNING id""",
            (name, email, hashed)
        )
        admin_id = cur.fetchone()[0]
        print(f"\n✅ Admin user created (ID: {admin_id})")
    except psycopg2.errors.UniqueViolation:
        print(f"\n⚠️  User with email {email} already exists.")
        conn.rollback()
    except Exception as e:
        print(f"\n❌ Failed to create admin: {e}")
        sys.exit(1)

    cur.close()
    conn.close()

    print("\n" + "=" * 50)
    print("✅ Setup complete!")
    print("")
    print("Next steps:")
    print(f"  1. Set your env vars (see .env.example)")
    print(f"  2. Run the app:  gunicorn app:app -c gunicorn_config.py")
    print(f"  3. Login at /login with your admin credentials")
    print("=" * 50 + "\n")


if __name__ == '__main__':
    run()
