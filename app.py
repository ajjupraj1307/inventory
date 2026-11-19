from pathlib import Path
import csv
import io
import sqlite3
from datetime import datetime, date

from flask import Flask, render_template, request, jsonify, redirect, url_for, session, Response
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "inventory.db"

app = Flask(__name__)
app.secret_key = "change-this-secret-before-production"


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS planning_parts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER UNIQUE,
        part_code TEXT UNIQUE NOT NULL,
        supplier TEXT,
        customer TEXT,
        lead_time_days INTEGER DEFAULT 90,
        reserved_qty REAL DEFAULT 0,
        safety_stock REAL DEFAULT 0,
        min_qty REAL DEFAULT 0,
        max_qty REAL DEFAULT 999999,
        substitute_part TEXT,
        planner TEXT,
        active INTEGER DEFAULT 1,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE SET NULL
    );

    CREATE TABLE IF NOT EXISTS forecast_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        part_code TEXT NOT NULL,
        snapshot_date TEXT NOT NULL,
        need_period TEXT NOT NULL,
        customer TEXT,
        forecast_qty REAL NOT NOT NULL,
        created_by TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(part_code) REFERENCES planning_parts(part_code) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS purchase_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        po_number TEXT UNIQUE NOT NULL,
        part_code TEXT NOT NULL,
        supplier TEXT,
        quantity REAL NOT NULL,
        requested_due_date TEXT,
        confirmed_due_date TEXT,
        status TEXT DEFAULT 'Pending',
        created_by TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(part_code) REFERENCES planning_parts(part_code) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS planning_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        part_code TEXT NOT NULL,
        action_type TEXT NOT NULL,
        quantity REAL DEFAULT 0,
        need_date TEXT,
        owner TEXT,
        status TEXT DEFAULT 'Pending',
        supplier_response TEXT,
        notes TEXT,
        created_by TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(part_code) REFERENCES planning_parts(part_code) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS planning_settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        action TEXT,
        details TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS planner_comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_type TEXT,
        entity_id TEXT,
        comment TEXT,
        username TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)

    defaults = {
        "lead_time_days": "90",
        "supply_health_weight": "70",
        "stability_health_weight": "30",
        "frozen_months": "1",
        "flexible_months": "3",
        "planning_horizon_months": "12",
    }
    for key, value in defaults.items():
        conn.execute("INSERT OR IGNORE INTO planning_settings(key,value) VALUES (?,?)", (key, value))

    # Import existing inventory items into planning parts.
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "items" in tables:
        for item in conn.execute("SELECT * FROM items"):
            code = item["sku"] or f"PART-{item['id']}"
            conn.execute("""
                INSERT OR IGNORE INTO planning_parts
                (item_id, part_code, supplier, customer, lead_time_days, reserved_qty,
                 safety_stock, min_qty, max_qty, substitute_part, planner)
                VALUES (?, ?, '', '', 90, 0, ?, 0, 999999, '', 'Unassigned')
            """, (item["id"], code, item["reorder_level"] or 0))
    conn.commit()

    # Create compatible demo users only when the users table is empty.
    if "users" in tables:
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            conn.execute("INSERT INTO users(username,password,role) VALUES(?,?,?)",
                         ("admin", generate_password_hash("admin123"), "administrator"))
            conn.execute("INSERT INTO users(username,password,role) VALUES(?,?,?)",
                         ("planner", generate_password_hash("planner123"), "planner"))
            conn.commit()
    conn.close()


def audit(action, details=""):
    conn = db()
    conn.execute("INSERT INTO audit_log(username,action,details) VALUES(?,?,?)",
                 (session.get("username", "system"), action, details))
    conn.commit()
    conn.close()


def current_user():
    if not session.get("user_id"):
        return None
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    conn.close()
    return row


def role_allowed(*roles):
    user = current_user()
    return bool(user and (user["role"] in roles or user["role"] == "administrator"))


def require_login():
    if not session.get("user_id"):
        return False
    return True


def get_settings():
    conn = db()
    rows = conn.execute("SELECT key,value FROM planning_settings").fetchall()
    conn.close()
    return {row["key"]: row["value"] for row in rows}


def part_rows(search="", status=""):
    conn = db()
    q = """
        SELECT pp.*, i.name, i.qty, i.reorder_level,
               i.cost_price, i.sell_price
        FROM planning_parts pp
        LEFT JOIN items i ON i.id=pp.item_id
        WHERE pp.active=1
    """
    params = []
    if search:
        q += " AND (pp.part_code LIKE ? OR pp.supplier LIKE ? OR pp.customer LIKE ? OR i.name LIKE ?)"
        params += [f"%{search}%"] * 4
    q += " ORDER BY pp.part_code"
    parts = conn.execute(q, params).fetchall()
    output = []

    for p in parts:
        code = p["part_code"]
        forecasts = conn.execute("""
            SELECT * FROM forecast_snapshots
            WHERE part_code=?
            ORDER BY snapshot_date DESC, need_period DESC
        """, (code,)).fetchall()
        latest = forecasts[0] if forecasts else None

        confirmed_po = conn.execute("""
            SELECT COALESCE(SUM(quantity),0) FROM purchase_orders
            WHERE part_code=? AND status='Confirmed'
        """, (code,)).fetchone()[0]

        on_hand = float(p["qty"] or 0)
        reserved = float(p["reserved_qty"] or 0)
        available = max(0, on_hand - reserved) + float(confirmed_po or 0)

        changes = []
        period_forecasts = {}
        for f in reversed(forecasts):
            period_forecasts.setdefault(f["need_period"], []).append(float(f["forecast_qty"]))
        for values in period_forecasts.values():
            for i in range(1, len(values)):
                if values[i-1]:
                    changes.append(abs(values[i]-values[i-1])/values[i-1])
        volatility = sum(changes)/len(changes) if changes else 0

        forecast_qty = float(latest["forecast_qty"]) if latest else 0
        safety = float(p["safety_stock"] or 0)
        configured_safety = safety
        if not configured_safety and forecast_qty:
            configured_safety = forecast_qty * volatility

        gap = max(0, forecast_qty - available)
        if forecast_qty and available < forecast_qty:
            risk_status = "Shortage"
        elif forecast_qty and available < forecast_qty + configured_safety:
            risk_status = "Safety stock risk"
        elif latest:
            risk_status = "Covered"
        else:
            risk_status = "No forecast"

        settings = get_settings()
        supply_weight = float(settings["supply_health_weight"])
        stability_weight = float(settings["stability_health_weight"])
        protected = forecast_qty + configured_safety
        supply_score = min(1, available/protected) if protected else 1
        stability_score = 1-min(1, volatility)
        health = round(supply_score*supply_weight+stability_score*stability_weight)

        if status and risk_status != status:
            continue

        output.append({
            "id": p["id"], "item_id": p["item_id"], "code": code,
            "name": p["name"] or code,
            "supplier": p["supplier"] or "Unassigned",
            "customer": p["customer"] or "Unassigned",
            "lead_time": p["lead_time_days"], "on_hand": on_hand,
            "reserved": reserved, "safety_stock": configured_safety,
            "min_qty": p["min_qty"], "max_qty": p["max_qty"],
            "substitute": p["substitute_part"], "planner": p["planner"] or "Unassigned",
            "forecast": forecast_qty, "need_period": latest["need_period"] if latest else "-",
            "available": available, "gap": gap, "volatility": volatility,
            "health": health, "status": risk_status,
        })
    conn.close()
    return output


@app.before_request
def open_database_schema():
    if not getattr(app, "_database_initialized", False):
        init_db()
        app._database_initialized = True


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if session.get("user_id"):
            return redirect(url_for("index"))
        return render_template("login.html")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    conn.close()

    if not user:
        return render_template("login.html", error="Invalid username or password")

    valid = False
    try:
        valid = check_password_hash(user["password"], password)
    except Exception:
        valid = False

    if not valid:
        valid = user["password"] == password

    if not valid:
        return render_template("login.html", error="Invalid username or password")

    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["role"] = user["role"]
    audit("login", "Logged in")
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    audit("logout", "Logged out")
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    if not require_login():
        return redirect(url_for("login"))
    user = current_user()
    return render_template("index.html", user=dict(user) if user else {})


@app.route("/api/dashboard")
def api_dashboard():
    if not require_login():
        return jsonify({"error": "Unauthorized"}), 401
    status = request.args.get("status", "")
    rows = part_rows(request.args.get("search", ""), status)
    return jsonify({
        "parts": rows,
        "parts_at_risk": sum(r["status"] != "Covered" and r["status"] != "No forecast" for r in rows),
        "pull_in_units": sum(r["gap"] for r in rows),
        "average_volatility": sum(r["volatility"] for r in rows)/len(rows) if rows else 0,
    })


@app.route("/api/parts", methods=["GET", "POST"])
def api_parts():
    if not require_login():
        return jsonify({"error": "Unauthorized"}), 401
    if request.method == "GET":
        return jsonify(part_rows(request.args.get("search", "")))

    if not role_allowed("administrator", "planner", "buyer"):
        return jsonify({"error": "Forbidden"}), 403
    data = request.json or {}
    code = (data.get("part_code") or "").strip()
    if not code:
        return jsonify({"error": "part_code is required"}), 400

    conn = db()
    conn.execute("""
        INSERT INTO planning_parts
        (part_code,supplier,customer,lead_time_days,reserved_qty,safety_stock,
         min_qty,max_qty,substitute_part,planner,active)
        VALUES(?,?,?,?,?,?,?,?,?,?,1)
        ON CONFLICT(part_code) DO UPDATE SET
        supplier=excluded.supplier,customer=excluded.customer,
        lead_time_days=excluded.lead_time_days,reserved_qty=excluded.reserved_qty,
        safety_stock=excluded.safety_stock,min_qty=excluded.min_qty,
        max_qty=excluded.max_qty,substitute_part=excluded.substitute_part,
        planner=excluded.planner,active=1,updated_at=CURRENT_TIMESTAMP
    """, (code, data.get("supplier", ""), data.get("customer", ""),
          data.get("lead_time_days", 90), data.get("reserved_qty", 0),
          data.get("safety_stock", 0), data.get("min_qty", 0),
          data.get("max_qty", 999999), data.get("substitute_part", ""),
          data.get("planner", "")))
    conn.commit()
    conn.close()
    audit("part_saved", code)
    return jsonify({"ok": True})


@app.route("/api/parts/<int:part_id>", methods=["DELETE"])
def delete_part(part_id):
    if not role_allowed("administrator", "planner"):
        return jsonify({"error": "Forbidden"}), 403
    conn = db()
    row = conn.execute("SELECT part_code FROM planning_parts WHERE id=?", (part_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    conn.execute("UPDATE planning_parts SET active=0 WHERE id=?", (part_id,))
    conn.commit()
    conn.close()
    audit("part_deactivated", row["part_code"])
    return jsonify({"ok": True})


@app.route("/api/forecasts", methods=["GET", "POST"])
def api_forecasts():
    if not require_login():
        return jsonify({"error": "Unauthorized"}), 401
    conn = db()
    if request.method == "GET":
        rows = conn.execute("SELECT * FROM forecast_snapshots ORDER BY part_code,snapshot_date,need_period").fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])

    if not role_allowed("administrator", "planner", "manager"):
        conn.close()
        return jsonify({"error": "Forbidden"}), 403
    data = request.json or {}
    required = ["part_code", "snapshot_date", "need_period", "forecast_qty"]
    if any(data.get(k) in (None, "") for k in required):
        conn.close()
        return jsonify({"error": "Missing required forecast fields"}), 400
    conn.execute("""
        INSERT INTO forecast_snapshots(part_code,snapshot_date,need_period,customer,forecast_qty,created_by)
        VALUES(?,?,?,?,?,?)
    """, (data["part_code"], data["snapshot_date"], data["need_period"],
          data.get("customer", ""), float(data["forecast_qty"]), session["username"]))
    conn.commit()
    conn.close()
    audit("forecast_added", f"{data['part_code']} {data['need_period']} {data['forecast_qty']}")
    return jsonify({"ok": True})


@app.route("/api/import/forecasts", methods=["POST"])
def import_forecasts():
    if not role_allowed("administrator", "planner", "manager"):
        return jsonify({"error": "Forbidden"}), 403
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "CSV file required"}), 400

    stream = io.StringIO(file.stream.read().decode("utf-8-sig"))
    reader = csv.DictReader(stream)
    required = {"part_code", "snapshot_date", "need_period", "forecast_qty"}
    if not reader.fieldnames or not required.issubset({x.strip().lower() for x in reader.fieldnames}):
        return jsonify({"error": "CSV must contain part_code,snapshot_date,need_period,forecast_qty"}), 400

    conn = db()
    count = 0
    errors = []
    for index, row in enumerate(reader, start=2):
        try:
            code = row["part_code"].strip()
            qty = float(row["forecast_qty"])
            if not code or not row["snapshot_date"] or not row["need_period"]:
                raise ValueError("missing field")
            conn.execute("""
                INSERT INTO forecast_snapshots(part_code,snapshot_date,need_period,customer,forecast_qty,created_by)
                VALUES(?,?,?,?,?,?)
            """, (code, row["snapshot_date"].strip(), row["need_period"].strip(),
                  row.get("customer", ""), qty, session["username"]))
            count += 1
        except Exception as exc:
            errors.append({"row": index, "error": str(exc)})
    conn.commit()
    conn.close()
    audit("forecast_import", f"Imported {count} rows")
    return jsonify({"imported": count, "errors": errors})


@app.route("/api/pos", methods=["GET", "POST"])
def api_pos():
    if not require_login():
        return jsonify({"error": "Unauthorized"}), 401
    conn = db()
    if request.method == "GET":
        rows = conn.execute("SELECT * FROM purchase_orders ORDER BY confirmed_due_date,po_number").fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])

    if not role_allowed("administrator", "buyer", "planner"):
        conn.close()
        return jsonify({"error": "Forbidden"}), 403
    data = request.json or {}
    if not data.get("po_number") or not data.get("part_code") or not data.get("quantity"):
        conn.close()
        return jsonify({"error": "PO number, part code, and quantity are required"}), 400
    conn.execute("""
        INSERT INTO purchase_orders(po_number,part_code,supplier,quantity,requested_due_date,confirmed_due_date,status,created_by)
        VALUES(?,?,?,?,?,?,?,?)
    """, (data["po_number"], data["part_code"], data.get("supplier", ""), float(data["quantity"]),
          data.get("requested_due_date", ""), data.get("confirmed_due_date", ""),
          data.get("status", "Pending"), session["username"]))
    conn.commit()
    conn.close()
    audit("po_added", data["po_number"])
    return jsonify({"ok": True})


@app.route("/api/pos/<int:po_id>/confirm", methods=["POST"])
def confirm_po(po_id):
    if not role_allowed("administrator", "buyer", "supplier"):
        return jsonify({"error": "Forbidden"}), 403
    data = request.json or {}
    conn = db()
    row = conn.execute("SELECT po_number FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    conn.execute("UPDATE purchase_orders SET status='Confirmed',confirmed_due_date=COALESCE(?,confirmed_due_date) WHERE id=?",
                 (data.get("confirmed_due_date", date.today().isoformat()), po_id))
    conn.commit()
    conn.close()
    audit("po_confirmed", row["po_number"])
    return jsonify({"ok": True})


@app.route("/api/actions", methods=["GET", "POST"])
def api_actions():
    if not require_login():
        return jsonify({"error": "Unauthorized"}), 401
    conn = db()
    if request.method == "GET":
        rows = conn.execute("SELECT * FROM planning_actions ORDER BY created_at DESC").fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])

    data = request.json or {}
    required = ["part_code", "action_type"]
    if any(not data.get(k) for k in required):
        conn.close()
        return jsonify({"error": "Part code and action type are required"}), 400
    conn.execute("""
        INSERT INTO planning_actions(part_code,action_type,quantity,need_date,owner,status,supplier_response,notes,created_by)
        VALUES(?,?,?,?,?,?,?,?,?)
    """, (data["part_code"], data["action_type"], float(data.get("quantity", 0)),
          data.get("need_date", ""), data.get("owner", ""), data.get("status", "Pending"),
          data.get("supplier_response", ""), data.get("notes", ""), session["username"]))
    conn.commit()
    conn.close()
    audit("action_created", f"{data['action_type']} {data['part_code']}")
    return jsonify({"ok": True})


@app.route("/api/actions/<int:action_id>", methods=["PATCH"])
def update_action(action_id):
    if not require_login():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json or {}
    conn = db()
    row = conn.execute("SELECT id FROM planning_actions WHERE id=?", (action_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    conn.execute("""
        UPDATE planning_actions SET status=?,supplier_response=COALESCE(?,supplier_response),
        notes=COALESCE(?,notes),updated_at=CURRENT_TIMESTAMP WHERE id=?
    """, (data.get("status", "Pending"), data.get("supplier_response"), data.get("notes"), action_id))
    conn.commit()
    conn.close()
    audit("action_updated", str(action_id))
    return jsonify({"ok": True})


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if not require_login():
        return jsonify({"error": "Unauthorized"}), 401
    if request.method == "GET":
        return jsonify(get_settings())
    if not role_allowed("administrator"):
        return jsonify({"error": "Forbidden"}), 403
    conn = db()
    for key, value in (request.json or {}).items():
        conn.execute("INSERT INTO planning_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, str(value)))
    conn.commit()
    conn.close()
    audit("settings_updated", "Planning settings changed")
    return jsonify({"ok": True})


@app.route("/api/audit")
def api_audit():
    if not require_login():
        return jsonify({"error": "Unauthorized"}), 401
    conn = db()
    rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 300").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/report.csv")
def report_csv():
    if not require_login():
        return jsonify({"error": "Unauthorized"}), 401
    rows = part_rows()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["part","name","supplier","customer","need_period","forecast","available","gap","volatility","health","status"])
    for r in rows:
        writer.writerow([r["code"], r["name"], r["supplier"], r["customer"], r["need_period"], r["forecast"], r["available"], r["gap"], round(r["volatility"], 4), r["health"], r["status"]])
    audit("report_exported", "Part report CSV")
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=inventory-risk-report.csv"})


@app.route("/api/timeline/<part_code>")
def timeline(part_code):
    if not require_login():
        return jsonify({"error": "Unauthorized"}), 401
    conn = db()
    forecasts = conn.execute("""
        SELECT need_period, snapshot_date, forecast_qty
        FROM forecast_snapshots WHERE part_code=?
        ORDER BY need_period,snapshot_date
    """, (part_code,)).fetchall()

    periods = {}
    for row in forecasts:
        periods[row["need_period"]] = float(row["forecast_qty"])

    p = conn.execute("SELECT * FROM planning_parts WHERE part_code=?", (part_code,)).fetchone()
    item = conn.execute("SELECT qty FROM items WHERE id=?", (p["item_id"],)).fetchone() if p and p["item_id"] else None
    confirmed = conn.execute("""
        SELECT COALESCE(SUM(quantity),0) FROM purchase_orders
        WHERE part_code=? AND status='Confirmed'
    """, (part_code,)).fetchone()[0]
    conn.close()

    balance = max(0, (item["qty"] if item else 0) - (p["reserved_qty"] if p else 0)) + float(confirmed or 0)
    timeline_rows = []
    for period in sorted(periods):
        demand = float(periods[period])
        balance -= demand
        timeline_rows.append({"period": period, "demand": demand, "balance": balance})

    return jsonify(timeline_rows)


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
