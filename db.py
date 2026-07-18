import sqlite3
from pathlib import Path
import hashlib
from datetime import datetime
import importlib
import importlib.util

# default database path (placed alongside the script)
DB_PATH = Path("inventory.db")

# try using werkzeug.security if available, else fallback
if importlib.util.find_spec("werkzeug.security"):
    mod = importlib.import_module("werkzeug.security")
    generate_password_hash = mod.generate_password_hash
    check_password_hash = mod.check_password_hash
else:
    def generate_password_hash(password):
        salt = "inventory_salt_v1"
        return hashlib.sha256((salt + password).encode()).hexdigest()

    def check_password_hash(stored, password):
        salt = "inventory_salt_v1"
        return stored == hashlib.sha256((salt + password).encode()).hexdigest()


class DB:
    def __init__(self, path=DB_PATH):
        self.path = Path(path)
        if not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            self.path,
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES
        )
        self.conn.row_factory = sqlite3.Row
        self.init_db()

    def init_db(self):
        schema_file = Path("schema.sql")
        if schema_file.exists():
            with open(schema_file, "r", encoding="utf-8") as f:
                sql = f.read()
            self.conn.executescript(sql)
            self.conn.commit()

        # create default admin user if not exists
        cur = self.conn.execute("SELECT COUNT(*) as c FROM users")
        row = cur.fetchone()
        if row is None or row["c"] == 0:
            uname = "admin"
            pwd = generate_password_hash("admin123")
            try:
                self.conn.execute(
                    "INSERT INTO users (username,password,role) VALUES (?,?,?)",
                    (uname, pwd, "admin")
                )
                self.conn.commit()
                print("[db] created default admin user -> username: admin  password: admin123")
            except Exception as e:
                print("[db] admin creation failed:", e)

        # create default staff user if not exists
        try:
            cur2 = self.conn.execute("SELECT COUNT(*) as c FROM users WHERE username='staff'")
            row2 = cur2.fetchone()
            if row2 is None or row2["c"] == 0:
                uname2 = "staff"
                pwd2 = generate_password_hash("staff123")
                self.conn.execute(
                    "INSERT INTO users (username,password,role) VALUES (?,?,?)",
                    (uname2, pwd2, "staff")
                )
                self.conn.commit()
                print("[db] created default staff user -> username: staff  password: staff123")
        except Exception as e:
            print("[db] staff creation failed:", e)

    # user helpers
    def verify_user(self, username, password):
        cur = self.conn.execute("SELECT * FROM users WHERE username=?", (username,))
        row = cur.fetchone()
        if not row:
            return False, None
        ok = check_password_hash(row["password"], password)
        return ok, row

    def get_user_by_username(self, username):
        cur = self.conn.execute("SELECT * FROM users WHERE username=?", (username,))
        return cur.fetchone()

    # items
    def add_item(self, name, qty=0, cost_price=0.0, sell_price=0.0,
                 sku=None, barcode=None, category=None, unit=None, reorder_level=0):
        cur = self.conn.execute(
            "INSERT INTO items (sku,barcode,name,category,unit,cost_price,sell_price,qty,reorder_level) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (sku, barcode, name, category, unit, cost_price, sell_price, qty, reorder_level)
        )
        self.conn.commit()
        return cur.lastrowid

    def update_item_qty(self, item_id, new_qty):
        self.conn.execute("UPDATE items SET qty=? WHERE id=?", (new_qty, item_id))
        self.conn.commit()

    def update_item(self, item_id, **kwargs):
        keys = []
        vals = []
        for k, v in kwargs.items():
            keys.append(f"{k}=?")
            vals.append(v)
        vals.append(item_id)
        q = "UPDATE items SET " + ", ".join(keys) + " WHERE id=?"
        self.conn.execute(q, vals)
        self.conn.commit()

    def delete_item(self, item_id):
        self.conn.execute("DELETE FROM items WHERE id=?", (item_id,))
        self.conn.commit()

    def list_items(self, search=None):
        if search:
            q = "SELECT * FROM items WHERE name LIKE ? OR sku LIKE ? ORDER BY name"
            cur = self.conn.execute(q, (f"%{search}%", f"%{search}%"))
        else:
            cur = self.conn.execute("SELECT * FROM items ORDER BY name")
        return cur.fetchall()

    def get_low_stock_items(self):
        cur = self.conn.execute("SELECT * FROM items WHERE qty < reorder_level ORDER BY name")
        return cur.fetchall()

    # stock movements
    def add_stock_movement(self, item_id, qty, movement_type, note=None):
        self.conn.execute(
            "INSERT INTO stock_movements (item_id,qty,movement_type,note) VALUES (?,?,?,?)",
            (item_id, qty, movement_type, note)
        )
        # update item qty if movement_type is in/out/sale
        if movement_type == "in":
            self.conn.execute("UPDATE items SET qty = qty + ? WHERE id=?", (qty, item_id))
        elif movement_type in ("out", "sale"):
            self.conn.execute("UPDATE items SET qty = qty - ? WHERE id=?", (qty, item_id))
        self.conn.commit()

    # sales
    def add_sale(self, invoice_no, customer, total):
        cur = self.conn.execute(
            "INSERT INTO sales (invoice_no,customer,total) VALUES (?,?,?)",
            (invoice_no, customer, total)
        )
        self.conn.commit()
        return cur.lastrowid

    def close(self):
        try:
            self.conn.close()
        except:
            pass
