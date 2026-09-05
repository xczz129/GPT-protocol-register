import json
import sqlite3

conn = sqlite3.connect("/root/full/gpt-register-fp/data/registered_accounts.sqlite3")
for (key, payload) in conn.execute(
    "SELECT canonical_key, payload FROM accounts WHERE payload LIKE '%@example.com%'"
):
    rec = json.loads(payload)
    print(key)
    print("  email:", rec.get("email"))
    print("  registered_at:", rec.get("registered_at"))
    print("  updated_at:", rec.get("updated_at"))
    print("  status:", rec.get("status"), "| health:", rec.get("health_status"))
    print("  keys:", sorted(k for k in rec.keys())[:20])
