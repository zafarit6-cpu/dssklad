import os
import sqlite3
from datetime import date

from flask import Flask, jsonify, request, render_template, g

app = Flask(__name__)
DATABASE = os.path.join(os.path.dirname(__file__), "inventory.db")

AUTO_STATUSES = {"Выдал", "Выдано"}


# Установка зависимостей: pip install flask
# Запуск: python main.py
# База данных создаётся автоматически при первом запуске.


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS warehouse (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            price REAL NOT NULL DEFAULT 0,
            quantity INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            position TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 0,
            price REAL NOT NULL DEFAULT 0,
            total REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'Новый',
            comment TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            type TEXT NOT NULL,
            status TEXT NOT NULL,
            reference TEXT NOT NULL DEFAULT ''
        );
        """
    )
    db.commit()


@app.before_request
def ensure_db():
    init_db()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/items")
def api_items():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM items ORDER BY id DESC"
    ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.route("/api/items", methods=["POST"])
def add_item():
    payload = request.get_json(silent=True) or request.form or {}
    data = normalize_item_payload(payload)
    db = get_db()
    cursor = db.execute(
        """
        INSERT INTO items (date, position, quantity, price, total, status, comment)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data["date"],
            data["position"],
            data["quantity"],
            data["price"],
            data["total"],
            data["status"],
            data["comment"],
        ),
    )
    db.commit()
    new_row = db.execute("SELECT * FROM items WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return jsonify(dict(new_row))


@app.route("/api/items/<int:item_id>", methods=["PUT"])
def update_item(item_id):
    payload = request.get_json(silent=True) or request.form or {}
    data = normalize_item_payload(payload)
    db = get_db()
    existing = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not existing:
        return jsonify({"error": "Item not found"}), 404

    old_status = existing["status"]
    new_status = data["status"]

    db.execute(
        """
        UPDATE items
        SET date = ?, position = ?, quantity = ?, price = ?, total = ?, status = ?, comment = ?
        WHERE id = ?
        """,
        (
            data["date"],
            data["position"],
            data["quantity"],
            data["price"],
            data["total"],
            new_status,
            data["comment"],
            item_id,
        ),
    )
    db.commit()

    if new_status in AUTO_STATUSES and old_status not in AUTO_STATUSES:
        create_auto_transaction(item_id, data["total"])

    updated = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return jsonify(dict(updated))


@app.route("/api/items/<int:item_id>", methods=["DELETE"])
def delete_item(item_id):
    db = get_db()
    db.execute("DELETE FROM items WHERE id = ?", (item_id,))
    db.commit()
    return jsonify({"success": True})


@app.route("/api/transactions")
def api_transactions():
    db = get_db()
    rows = db.execute("SELECT * FROM transactions ORDER BY id DESC").fetchall()
    return jsonify([dict(row) for row in rows])


@app.route("/api/transactions", methods=["POST"])
def add_transaction():
    payload = request.get_json(silent=True) or request.form or {}
    amount = float(payload.get("amount", 0) or 0)
    db = get_db()
    cursor = db.execute(
        """
        INSERT INTO transactions (date, amount, type, status, reference)
        VALUES (?, ?, ?, ?, ?)
        """,
        (today_string(), amount, "Пополнение", "В обработке", "Ручное пополнение"),
    )
    db.commit()
    new_row = db.execute("SELECT * FROM transactions WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return jsonify(dict(new_row))


@app.route("/api/transactions/<int:transaction_id>", methods=["PUT"])
def update_transaction(transaction_id):
    payload = request.get_json(silent=True) or request.form or {}
    db = get_db()
    transaction = db.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
    if not transaction:
        return jsonify({"error": "Transaction not found"}), 404

    if transaction["type"] == "Списание" and transaction["reference"].startswith("item:"):
        return jsonify({"error": "Автоматическое списание нельзя редактировать"}), 400

    new_status = payload.get("status", transaction["status"])
    
    db.execute("UPDATE transactions SET status = ? WHERE id = ?", (new_status, transaction_id))
    db.commit()
    updated = db.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
    return jsonify(dict(updated))


@app.route("/api/balance")
def api_balance():
    db = get_db()
    completed_replenishments = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM transactions WHERE type = 'Пополнение' AND status = 'Выполнено'"
    ).fetchone()["total"]
    completed_spendings = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM transactions WHERE type = 'Списание' AND status = 'Выполнено'"
    ).fetchone()["total"]
    balance = float(completed_replenishments) - float(completed_spendings)
    return jsonify({"balance": round(balance, 2)})


@app.route("/api/warehouse")
def api_warehouse():
    db = get_db()
    rows = db.execute("SELECT * FROM warehouse ORDER BY name").fetchall()
    return jsonify([dict(row) for row in rows])


@app.route("/api/warehouse", methods=["POST"])
def add_warehouse():
    payload = request.get_json(silent=True) or request.form or {}
    name = (payload.get("name") or "").strip()
    price = float(payload.get("price", 0) or 0)
    quantity = int(payload.get("quantity", 0) or 0)
    
    if not name:
        return jsonify({"error": "Наименование обязательно"}), 400
    
    db = get_db()
    try:
        cursor = db.execute(
            "INSERT INTO warehouse (name, price, quantity) VALUES (?, ?, ?)",
            (name, price, quantity)
        )
        db.commit()
        new_row = db.execute("SELECT * FROM warehouse WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return jsonify(dict(new_row))
    except sqlite3.IntegrityError:
        return jsonify({"error": "Товар с таким наименованием уже существует"}), 400


@app.route("/api/warehouse/<int:warehouse_id>", methods=["PUT"])
def update_warehouse(warehouse_id):
    payload = request.get_json(silent=True) or request.form or {}
    db = get_db()
    existing = db.execute("SELECT * FROM warehouse WHERE id = ?", (warehouse_id,)).fetchone()
    if not existing:
        return jsonify({"error": "Товар не найден"}), 404
    
    name = payload.get("name", existing["name"]).strip()
    price = float(payload.get("price", existing["price"]) or 0)
    quantity = int(payload.get("quantity", existing["quantity"]) or 0)
    
    db.execute(
        "UPDATE warehouse SET name = ?, price = ?, quantity = ? WHERE id = ?",
        (name, price, quantity, warehouse_id)
    )
    db.commit()
    updated = db.execute("SELECT * FROM warehouse WHERE id = ?", (warehouse_id,)).fetchone()
    return jsonify(dict(updated))


@app.route("/api/warehouse/<int:warehouse_id>", methods=["DELETE"])
def delete_warehouse(warehouse_id):
    db = get_db()
    db.execute("DELETE FROM warehouse WHERE id = ?", (warehouse_id,))
    db.commit()
    return jsonify({"success": True})


def normalize_item_payload(payload):
    position = (payload.get("position") or "").strip()
    quantity = int(payload.get("quantity", 0) or 0)
    
    db = get_db()
    warehouse_item = db.execute("SELECT price FROM warehouse WHERE name = ?", (position,)).fetchone()
    price = float(payload.get("price", 0) or 0)
    if price == 0 and warehouse_item:
        price = warehouse_item["price"]
    
    total = quantity * price
    return {
        "date": payload.get("date") or today_string(),
        "position": position,
        "quantity": quantity,
        "price": price,
        "total": total,
        "status": payload.get("status") or "Новый",
        "comment": payload.get("comment") or "",
    }


def create_auto_transaction(item_id, total_amount):
    db = get_db()
    existing = db.execute(
        "SELECT id FROM transactions WHERE reference = ? AND type = 'Списание' LIMIT 1",
        (f"item:{item_id}",),
    ).fetchone()
    if existing:
        return None

    db.execute(
        """
        INSERT INTO transactions (date, amount, type, status, reference)
        VALUES (?, ?, ?, ?, ?)
        """,
        (today_string(), total_amount, "Списание", "Выполнено", f"item:{item_id}"),
    )
    db.commit()
    return True


def today_string():
    return date.today().isoformat()


if __name__ == "__main__":
    app.run(debug=True)
