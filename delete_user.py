import os, psycopg2
conn = psycopg2.connect(os.environ.get('DATABASE_URL'))
conn.autocommit = True
cur = conn.cursor()
cur.execute("DELETE FROM users WHERE email='webtestkan@gmail.com'")
print(f"Deleted {cur.rowcount} user(s)")
cur.close()
conn.close()
