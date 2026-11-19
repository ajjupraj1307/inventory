import csv
import io
import os
import sqlite3
from datetime import date
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("DATABASE_PATH", str(BASE_DIR / "inventory.db")))
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-before-deploy")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = connect()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS planning_parts (
      id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER UNIQUE,
      part_code TEXT UNIQUE NOT NULL, supplier TEXT DEFAULT '', customer TEXT DEFAULT '',
      lead_time_days INTEGER DEFAULT 90, reserved_qty REAL DEFAULT 0,
      safety_stock REAL DEFAULT 0, min_qty REAL DEFAULT 0, max_qty REAL DEFAULT 999999,
      substitute_part TEXT DEFAULT '', planner TEXT DEFAULT '', active INTEGER DEFAULT 1,
      updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE SET NULL
    );
    CREATE TABLE IF NOT EXISTS forecast_snapshots (
      id INTEGER PRIMARY KEY AUTOINCREMENT, part_code TEXT NOT NULL,
      snapshot_date TEXT NOT NULL, need_period TEXT NOT NULL, customer TEXT DEFAULT '',
      forecast_qty REAL NOT NULL, created_by TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY(part_code) REFERENCES planning_parts(part_code) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS purchase_orders (
      id INTEGER PRIMARY KEY AUTOINCREMENT, po_number TEXT UNIQUE NOT NULL,
      part_code TEXT NOT NULL, supplier TEXT DEFAULT '', quantity REAL NOT NULL,
      requested_due_date TEXT DEFAULT '', confirmed_due_date TEXT DEFAULT '',
      status TEXT DEFAULT 'Pending', created_by TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY(part_code) REFERENCES planning_parts(part_code) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS planning_actions (
      id INTEGER PRIMARY KEY AUTOINCREMENT, part_code TEXT NOT NULL, action_type TEXT NOT NULL,
      quantity REAL DEFAULT 0, need_date TEXT DEFAULT '', owner TEXT DEFAULT '',
      status TEXT DEFAULT 'Pending', supplier_response TEXT DEFAULT '', notes TEXT DEFAULT '',
      created_by TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
      updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY(part_code) REFERENCES planning_parts(part_code) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS planning_settings (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS audit_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, action TEXT, details TEXT,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS planner_comments (
      id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT, entity_id TEXT,
      comment TEXT, username TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)
    defaults = {"lead_time_days":"90", "supply_health_weight":"70",
                "stability_health_weight":"30", "frozen_months":"1",
                "flexible_months":"3", "planning_horizon_months":"12"}
    conn.executemany("INSERT OR IGNORE INTO planning_settings(key,value) VALUES (?,?)", defaults.items())
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "items" in tables:
        for item in conn.execute("SELECT * FROM items"):
            code = item["sku"] or f"PART-{item['id']}"
            conn.execute("""INSERT OR IGNORE INTO planning_parts
              (item_id,part_code,safety_stock,planner) VALUES(?,?,?,'Unassigned')""",
              (item["id"], code, item["reorder_level"] or 0))
    if "users" in tables and conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        conn.execute("INSERT INTO users(username,password,role) VALUES(?,?,?)",
                     ("admin", generate_password_hash("admin123"), "admin"))
        conn.execute("INSERT INTO users(username,password,role) VALUES(?,?,?)",
                     ("planner", generate_password_hash("planner123"), "planner"))
    conn.commit()
    conn.close()


def audit(action, details=""):
    conn = connect()
    conn.execute("INSERT INTO audit_log(username,action,details) VALUES(?,?,?)",
                 (session.get("username", "system"), action, details))
    conn.commit()
    conn.close()


def user_role():
    return session.get("role", "").lower()


def allowed(*roles):
    role = user_role()
    return bool(session.get("user_id") and (role in roles or role in ("admin", "administrator")))


def settings():
    conn = connect()
    out = {r["key"]: r["value"] for r in conn.execute("SELECT key,value FROM planning_settings")}
    conn.close()
    return out


def parts(search="", status=""):
    conn = connect()
    query = """SELECT pp.*,i.name,i.qty,i.reorder_level FROM planning_parts pp
               LEFT JOIN items i ON i.id=pp.item_id WHERE pp.active=1"""
    args = []
    if search:
        query += " AND (pp.part_code LIKE ? OR pp.supplier LIKE ? OR pp.customer LIKE ? OR i.name LIKE ?)"
        args += [f"%{search}%"] * 4
    query += " ORDER BY pp.part_code"
    rows = conn.execute(query, args).fetchall()
    result = []
    cfg = settings()
    for p in rows:
        hist = conn.execute("SELECT * FROM forecast_snapshots WHERE part_code=? ORDER BY snapshot_date DESC,id DESC", (p["part_code"],)).fetchall()
        latest = hist[0] if hist else None
        po_supply = conn.execute("SELECT COALESCE(SUM(quantity),0) FROM purchase_orders WHERE part_code=? AND status='Confirmed'", (p["part_code"],)).fetchone()[0]
        on_hand, reserved = float(p["qty"] or 0), float(p["reserved_qty"] or 0)
        available = max(0, on_hand-reserved) + float(po_supply or 0)
        by_period = {}
        for f in reversed(hist):
            by_period.setdefault(f["need_period"], []).append(float(f["forecast_qty"]))
        changes = [abs(v[i]-v[i-1])/v[i-1] for vals in by_period.values() for v in [vals] for i in range(1,len(v)) if v[i-1]]
        volatility = sum(changes)/len(changes) if changes else 0
        forecast = float(latest["forecast_qty"]) if latest else 0
        safety = float(p["safety_stock"] or 0) or forecast*volatility
        gap = max(0, forecast-available)
        state = "Shortage" if forecast and available<forecast else "Safety stock risk" if forecast and available<forecast+safety else "Covered" if latest else "No forecast"
        protected = forecast+safety
        health = round((min(1,available/protected) if protected else 1)*float(cfg.get("supply_health_weight",70)) + (1-min(1,volatility))*float(cfg.get("stability_health_weight",30)))
        if status and status != state:
            continue
        result.append({"id":p["id"],"item_id":p["item_id"],"code":p["part_code"],"name":p["name"] or p["part_code"],"supplier":p["supplier"] or "Unassigned","customer":p["customer"] or "Unassigned","lead_time":p["lead_time_days"],"on_hand":on_hand,"reserved":reserved,"safety_stock":safety,"min_qty":p["min_qty"],"max_qty":p["max_qty"],"substitute":p["substitute_part"],"planner":p["planner"] or "Unassigned","forecast":forecast,"need_period":latest["need_period"] if latest else "-","available":available,"gap":gap,"volatility":volatility,"health":health,"status":state})
    conn.close()
    return result


@app.before_request
def ensure_schema():
    if not getattr(app, "_schema_ready", False):
        init_db()
        app._schema_ready = True


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return redirect(url_for("index")) if session.get("user_id") else render_template("login.html")
    username, password = request.form.get("username", "").strip(), request.form.get("password", "")
    conn = connect()
    user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    valid = False
    if user:
        try: valid = check_password_hash(user["password"], password)
        except (ValueError, TypeError): valid = user["password"] == password
    if not valid:
        return render_template("login.html", error="Invalid username or password"), 401
    session.clear()
    session.update(user_id=user["id"], username=user["username"], role=user["role"])
    audit("login", "Logged in")
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    if session.get("user_id"): audit("logout", "Logged out")
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    if not session.get("user_id"): return redirect(url_for("login"))
    conn = connect(); user = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone(); conn.close()
    return render_template("index.html", user=dict(user) if user else {})


@app.route("/api/dashboard")
def dashboard():
    if not session.get("user_id"): return jsonify(error="Unauthorized"), 401
    rows = parts(request.args.get("search", ""), request.args.get("status", ""))
    return jsonify(parts=rows, parts_at_risk=sum(p["status"] in ("Shortage","Safety stock risk") for p in rows), pull_in_units=sum(p["gap"] for p in rows), average_volatility=sum(p["volatility"] for p in rows)/len(rows) if rows else 0)


@app.route("/api/parts", methods=["GET","POST"])
def api_parts():
    if not session.get("user_id"): return jsonify(error="Unauthorized"), 401
    if request.method == "GET": return jsonify(parts(request.args.get("search", "")))
    if not allowed("planner","buyer"): return jsonify(error="Forbidden"), 403
    d=request.get_json(silent=True) or {}; code=(d.get("part_code") or "").strip()
    if not code: return jsonify(error="part_code is required"),400
    conn=connect(); conn.execute("""INSERT INTO planning_parts(part_code,supplier,customer,lead_time_days,reserved_qty,safety_stock,min_qty,max_qty,substitute_part,planner,active) VALUES(?,?,?,?,?,?,?,?,?,?,1) ON CONFLICT(part_code) DO UPDATE SET supplier=excluded.supplier,customer=excluded.customer,lead_time_days=excluded.lead_time_days,reserved_qty=excluded.reserved_qty,safety_stock=excluded.safety_stock,min_qty=excluded.min_qty,max_qty=excluded.max_qty,substitute_part=excluded.substitute_part,planner=excluded.planner,active=1,updated_at=CURRENT_TIMESTAMP""",(code,d.get("supplier",""),d.get("customer",""),d.get("lead_time_days",90),d.get("reserved_qty",0),d.get("safety_stock",0),d.get("min_qty",0),d.get("max_qty",999999),d.get("substitute_part",""),d.get("planner",""))); conn.commit();conn.close();audit("part_saved",code);return jsonify(ok=True)


@app.route("/api/parts/<int:part_id>",methods=["DELETE"])
def delete_part(part_id):
    if not allowed("planner"):return jsonify(error="Forbidden"),403
    conn=connect();row=conn.execute("SELECT part_code FROM planning_parts WHERE id=?",(part_id,)).fetchone()
    if not row:conn.close();return jsonify(error="Not found"),404
    conn.execute("UPDATE planning_parts SET active=0 WHERE id=?",(part_id,));conn.commit();conn.close();audit("part_deactivated",row["part_code"]);return jsonify(ok=True)


@app.route("/api/forecasts",methods=["GET","POST"])
def forecasts():
    if not session.get("user_id"):return jsonify(error="Unauthorized"),401
    conn=connect()
    if request.method=="GET":
        rows=conn.execute("SELECT * FROM forecast_snapshots ORDER BY part_code,snapshot_date,need_period").fetchall();conn.close();return jsonify([dict(r) for r in rows])
    if not allowed("planner","manager"):conn.close();return jsonify(error="Forbidden"),403
    d=request.get_json(silent=True) or {};required=("part_code","snapshot_date","need_period","forecast_qty")
    if any(d.get(k) in (None,"") for k in required):conn.close();return jsonify(error="Missing required forecast fields"),400
    try:qty=float(d["forecast_qty"])
    except (ValueError,TypeError):conn.close();return jsonify(error="Forecast quantity must be numeric"),400
    try:conn.execute("INSERT INTO forecast_snapshots(part_code,snapshot_date,need_period,customer,forecast_qty,created_by) VALUES(?,?,?,?,?,?)",(d["part_code"],d["snapshot_date"],d["need_period"],d.get("customer",""),qty,session["username"]));conn.commit()
    except sqlite3.IntegrityError as e:conn.close();return jsonify(error=str(e)),400
    conn.close();audit("forecast_added",f"{d['part_code']} {d['need_period']} {qty}");return jsonify(ok=True)


@app.route("/api/import/forecasts",methods=["POST"])
def import_forecasts():
    if not allowed("planner","manager"):return jsonify(error="Forbidden"),403
    file=request.files.get("file")
    if not file:return jsonify(error="CSV file required"),400
    reader=csv.DictReader(io.StringIO(file.stream.read().decode("utf-8-sig")))
    if not reader.fieldnames:return jsonify(error="CSV is empty"),400
    reader.fieldnames=[h.strip().lower() for h in reader.fieldnames]
    required={"part_code","snapshot_date","need_period","forecast_qty"}
    if not required.issubset(reader.fieldnames):return jsonify(error="CSV must contain part_code,snapshot_date,need_period,forecast_qty"),400
    conn=connect();count=0;errors=[]
    for n,row in enumerate(reader,2):
        try:
            conn.execute("INSERT INTO forecast_snapshots(part_code,snapshot_date,need_period,customer,forecast_qty,created_by) VALUES(?,?,?,?,?,?)",(row["part_code"].strip(),row["snapshot_date"].strip(),row["need_period"].strip(),row.get("customer", ""),float(row["forecast_qty"]),session["username"]));count+=1
        except Exception as exc:errors.append({"row":n,"error":str(exc)})
    conn.commit();conn.close();audit("forecast_import",f"Imported {count} rows");return jsonify(imported=count,errors=errors)


@app.route("/api/pos",methods=["GET","POST"])
def pos():
    if not session.get("user_id"):return jsonify(error="Unauthorized"),401
    conn=connect()
    if request.method=="GET":rows=conn.execute("SELECT * FROM purchase_orders ORDER BY confirmed_due_date,po_number").fetchall();conn.close();return jsonify([dict(r) for r in rows])
    if not allowed("buyer","planner"):conn.close();return jsonify(error="Forbidden"),403
    d=request.get_json(silent=True) or {}
    if not d.get("po_number") or not d.get("part_code") or not d.get("quantity"):conn.close();return jsonify(error="PO number, part code, and quantity are required"),400
    try:conn.execute("INSERT INTO purchase_orders(po_number,part_code,supplier,quantity,requested_due_date,confirmed_due_date,status,created_by) VALUES(?,?,?,?,?,?,?,?)",(d["po_number"],d["part_code"],d.get("supplier",""),float(d["quantity"]),d.get("requested_due_date",""),d.get("confirmed_due_date",""),d.get("status","Pending"),session["username"]));conn.commit()
    except (sqlite3.IntegrityError,ValueError) as e:conn.close();return jsonify(error=str(e)),400
    conn.close();audit("po_added",d["po_number"]);return jsonify(ok=True)


@app.route("/api/pos/<int:po_id>/confirm",methods=["POST"])
def confirm_po(po_id):
    if not allowed("buyer","supplier"):return jsonify(error="Forbidden"),403
    d=request.get_json(silent=True) or {};conn=connect();row=conn.execute("SELECT po_number FROM purchase_orders WHERE id=?",(po_id,)).fetchone()
    if not row:conn.close();return jsonify(error="Not found"),404
    conn.execute("UPDATE purchase_orders SET status='Confirmed',confirmed_due_date=? WHERE id=?",(d.get("confirmed_due_date") or date.today().isoformat(),po_id));conn.commit();conn.close();audit("po_confirmed",row["po_number"]);return jsonify(ok=True)


@app.route("/api/actions",methods=["GET","POST"])
def actions():
    if not session.get("user_id"):return jsonify(error="Unauthorized"),401
    conn=connect()
    if request.method=="GET":rows=conn.execute("SELECT * FROM planning_actions ORDER BY created_at DESC").fetchall();conn.close();return jsonify([dict(r) for r in rows])
    d=request.get_json(silent=True) or {}
    if not d.get("part_code") or not d.get("action_type"):conn.close();return jsonify(error="Part code and action type are required"),400
    try:conn.execute("INSERT INTO planning_actions(part_code,action_type,quantity,need_date,owner,status,supplier_response,notes,created_by) VALUES(?,?,?,?,?,?,?,?,?)",(d["part_code"],d["action_type"],float(d.get("quantity",0)),d.get("need_date",""),d.get("owner",""),d.get("status","Pending"),d.get("supplier_response",""),d.get("notes",""),session["username"]));conn.commit()
    except (sqlite3.IntegrityError,ValueError) as e:conn.close();return jsonify(error=str(e)),400
    conn.close();audit("action_created",f"{d['action_type']} {d['part_code']}");return jsonify(ok=True)


@app.route("/api/actions/<int:action_id>",methods=["PATCH"])
def update_action(action_id):
    if not session.get("user_id"):return jsonify(error="Unauthorized"),401
    d=request.get_json(silent=True) or {};conn=connect();row=conn.execute("SELECT id FROM planning_actions WHERE id=?",(action_id,)).fetchone()
    if not row:conn.close();return jsonify(error="Not found"),404
    conn.execute("UPDATE planning_actions SET status=?,supplier_response=COALESCE(?,supplier_response),notes=COALESCE(?,notes),updated_at=CURRENT_TIMESTAMP WHERE id=?",(d.get("status","Pending"),d.get("supplier_response"),d.get("notes"),action_id));conn.commit();conn.close();audit("action_updated",str(action_id));return jsonify(ok=True)


@app.route("/api/settings",methods=["GET","POST"])
def api_settings():
    if not session.get("user_id"):return jsonify(error="Unauthorized"),401
    if request.method=="GET":return jsonify(settings())
    if not allowed():return jsonify(error="Forbidden"),403
    conn=connect()
    for key,value in (request.get_json(silent=True) or {}).items():conn.execute("INSERT INTO planning_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,str(value)))
    conn.commit();conn.close();audit("settings_updated","Planning settings changed");return jsonify(ok=True)


@app.route("/api/audit")
def api_audit():
    if not session.get("user_id"):return jsonify(error="Unauthorized"),401
    conn=connect();rows=conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 300").fetchall();conn.close();return jsonify([dict(r) for r in rows])


@app.route("/api/report.csv")
def report_csv():
    if not session.get("user_id"):return jsonify(error="Unauthorized"),401
    output=io.StringIO();writer=csv.writer(output);writer.writerow(["part","name","supplier","customer","need_period","forecast","available","gap","volatility","health","status"])
    for p in parts():writer.writerow([p["code"],p["name"],p["supplier"],p["customer"],p["need_period"],p["forecast"],p["available"],p["gap"],round(p["volatility"],4),p["health"],p["status"]])
    audit("report_exported","Part report CSV");return Response(output.getvalue(),mimetype="text/csv",headers={"Content-Disposition":"attachment; filename=inventory-risk-report.csv"})


@app.route("/api/timeline/<part_code>")
def timeline(part_code):
    if not session.get("user_id"):return jsonify(error="Unauthorized"),401
    conn=connect();forecasts=conn.execute("SELECT need_period,MAX(snapshot_date) AS snapshot_date FROM forecast_snapshots WHERE part_code=? GROUP BY need_period ORDER BY need_period",(part_code,)).fetchall();rows=[]
    p=conn.execute("SELECT * FROM planning_parts WHERE part_code=?",(part_code,)).fetchone()
    if not p:conn.close();return jsonify(error="Part not found"),404
    item=conn.execute("SELECT qty FROM items WHERE id=?",(p["item_id"],)).fetchone() if p["item_id"] else None
    balance=max(0,float(item["qty"] if item else 0)-float(p["reserved_qty"] or 0))
    for period in forecasts:
        f=conn.execute("SELECT forecast_qty FROM forecast_snapshots WHERE part_code=? AND need_period=? AND snapshot_date=? ORDER BY id DESC LIMIT 1",(part_code,period["need_period"],period["snapshot_date"])).fetchone()
        incoming=conn.execute("SELECT COALESCE(SUM(quantity),0) FROM purchase_orders WHERE part_code=? AND status='Confirmed' AND substr(confirmed_due_date,1,7)<=?",(part_code,period["need_period"])).fetchone()[0]
        demand=float(f["forecast_qty"] if f else 0);balance+=float(incoming or 0)-demand
        rows.append({"period":period["need_period"],"demand":demand,"balance":balance})
    conn.close();return jsonify(rows)


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
