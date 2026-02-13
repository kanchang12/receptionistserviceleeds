"""
sc1.py — Migration: Add Prime/ElevenLabs + caller_name columns
Run once: python sc1.py
Safe to re-run (skips existing columns).
"""

import os
import psycopg2

DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://localhost/voicebot')


def migrate():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    cur = conn.cursor()

    migrations = [
        # businesses — Prime tier support
        ("businesses", "is_prime", "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("businesses", "elevenlabs_agent_id", "VARCHAR(100) DEFAULT ''"),

        # calls — caller name + ElevenLabs tracking
        ("calls", "caller_name", "VARCHAR(200)"),
        ("calls", "elevenlabs_conversation_id", "VARCHAR(100)"),
        ("calls", "is_prime", "BOOLEAN NOT NULL DEFAULT FALSE"),

        # phone_numbers — per-number config (may already exist)
        ("phone_numbers", "label", "VARCHAR(100) DEFAULT ''"),
        ("phone_numbers", "greeting", "TEXT DEFAULT ''"),
        ("phone_numbers", "agent_personality", "TEXT DEFAULT ''"),
    ]

    for table, col, col_type in migrations:
        try:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
            print(f"  ✅ Added {table}.{col}")
        except psycopg2.errors.DuplicateColumn:
            conn.rollback()
            conn.autocommit = True
            print(f"  ⏭️  {table}.{col} already exists")
        except Exception as e:
            conn.rollback()
            conn.autocommit = True
            print(f"  ❌ {table}.{col}: {e}")

    # Update tier CHECK constraint: starter/growth/enterprise → standard/prime/prime_pro/prime_enterprise
    # PostgreSQL doesn't allow ALTER CHECK easily, so we drop and re-add
    try:
        cur.execute("ALTER TABLE businesses DROP CONSTRAINT IF EXISTS businesses_tier_check")
        cur.execute("ALTER TABLE businesses ADD CONSTRAINT businesses_tier_check CHECK (tier IN ('standard', 'prime', 'prime_pro', 'prime_enterprise', 'starter', 'growth', 'enterprise'))")
        print("  ✅ Updated tier CHECK constraint")
    except Exception as e:
        conn.rollback()
        conn.autocommit = True
        print(f"  ⚠️  Tier constraint: {e}")

    # Migrate old tier names to new
    try:
        cur.execute("UPDATE businesses SET tier='standard' WHERE tier='starter'")
        cur.execute("UPDATE businesses SET tier='prime', is_prime=TRUE WHERE tier='growth'")
        cur.execute("UPDATE businesses SET tier='prime_enterprise', is_prime=TRUE WHERE tier='enterprise'")
        print("  ✅ Migrated old tier values")
    except Exception as e:
        conn.rollback()
        conn.autocommit = True
        print(f"  ⚠️  Tier migration: {e}")

    # Add index on elevenlabs_conversation_id
    try:
        cur.execute("CREATE INDEX IF NOT EXISTS idx_calls_el ON calls(elevenlabs_conversation_id)")
        print("  ✅ Added index idx_calls_el")
    except Exception as e:
        conn.rollback()
        conn.autocommit = True
        print(f"  ⚠️  Index: {e}")

    cur.close()
    conn.close()
    print("\n✅ Migration complete.")


if __name__ == '__main__':
    print("Running sc1.py migration...\n")
    migrate()
