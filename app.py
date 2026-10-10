import os
import smtplib
import sqlite3
from datetime import datetime, timedelta
from email.message import EmailMessage
from functools import wraps
from io import BytesIO

from flask import Flask, flash, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-this")

DATABASE = os.environ.get("DATABASE_PATH", "ebill.db")


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_name TEXT NOT NULL,
        username TEXT UNIQUE NOT NULL,
        email TEXT UNIQUE NOT NULL,
        phone TEXT,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS stores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER UNIQUE NOT NULL,
        store_name TEXT NOT NULL,
        address TEXT,
        phone TEXT,
        email TEXT,
        pan TEXT,
        tax_id TEXT,
        logo_path TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS sales (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        invoice_number TEXT UNIQUE NOT NULL,
        customer_name TEXT NOT NULL,
        customer_phone TEXT,
        customer_email TEXT NOT NULL,
        subtotal REAL NOT NULL,
        discount REAL NOT NULL DEFAULT 0,
        tax REAL NOT NULL DEFAULT 0,
        total REAL NOT NULL,
        payment_method TEXT NOT NULL,
        email_status TEXT NOT NULL DEFAULT 'NOT_SENT',
        email_error TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS sale_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sale_id INTEGER NOT NULL,
        product_name TEXT NOT NULL,
        quantity REAL NOT NULL,
        unit_price REAL NOT NULL,
        total REAL NOT NULL,
        FOREIGN KEY(sale_id) REFERENCES sales(id) ON DELETE CASCADE
    );
    """)
    conn.commit()
    conn.close()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def get_store(user_id):
    conn = get_db()
    store = conn.execute("SELECT * FROM stores WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return store


def next_invoice_number(user_id):
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) AS c FROM sales WHERE user_id = ?", (user_id,)).fetchone()["c"]
    conn.close()
    return f"EB-{count + 1:05d}"


def send_bill_email(sale_id):
    """Send the customer's bill using SMTP settings from environment variables."""
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_username = os.environ.get("SMTP_USERNAME")
    smtp_password = os.environ.get("SMTP_PASSWORD")
    sender_email = os.environ.get("SENDER_EMAIL") or smtp_username

    if not all([smtp_host, smtp_username, smtp_password, sender_email]):
        return False, "SMTP settings are not configured."

    conn = get_db()
    sale = conn.execute("""
        SELECT sales.*, stores.store_name, stores.address, stores.phone AS store_phone,
               stores.email AS store_email, stores.pan, stores.tax_id
        FROM sales
        JOIN stores ON stores.user_id = sales.user_id
        WHERE sales.id = ?
    """, (sale_id,)).fetchone()

    items = conn.execute(
        "SELECT * FROM sale_items WHERE sale_id = ?", (sale_id,)
    ).fetchall()
    conn.close()

    if not sale:
        return False, "Sale not found."

    pdf_bytes = create_bill_pdf(sale, items)

    msg = EmailMessage()
    msg["Subject"] = f"E-Bill {sale['invoice_number']} from {sale['store_name']}"
    msg["From"] = sender_email
    msg["To"] = sale["customer_email"]
    msg.set_content(
        f"""Hello {sale['customer_name']},

Thank you for shopping with {sale['store_name']}.

Your E-Bill {sale['invoice_number']} is attached to this email.

Subtotal: ${sale['subtotal']:.2f}
Tax (13%): ${sale['tax']:.2f}
Total before discount: ${(sale['subtotal'] + sale['tax']):.2f}
Discount: ${sale['discount']:.2f}
Final total: ${sale['total']:.2f}
Payment method: {sale['payment_method']}

Thank you for your business!
"""
    )
    msg.add_attachment(
        pdf_bytes,
        maintype="application",
        subtype="pdf",
        filename=f"{sale['invoice_number']}.pdf"
    )

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
            server.starttls()
            server.login(smtp_username, smtp_password)
            server.send_message(msg)
        return True, None
    except Exception as exc:
        return False, str(exc)


def create_bill_pdf(sale, items):
    """Create a narrow, vertical 3.5-inch receipt PDF."""
    store = get_store(sale["user_id"])
    buffer = BytesIO()
    page_width = 3.5 * inch
    # Dynamic height keeps the receipt vertical and gives every item room.
    page_height = max(360, (300 + len(items) * 34))
    pdf = canvas.Canvas(buffer, pagesize=(page_width, page_height))
    left, right = 12, page_width - 12
    y = page_height - 22

    def wrapped(text, max_chars=39, font="Helvetica", size=8, leading=11):
        nonlocal y
        text = str(text or "")
        words = text.split()
        lines, line = [], ""
        for word in words:
            candidate = (line + " " + word).strip()
            if len(candidate) > max_chars and line:
                lines.append(line)
                line = word
            else:
                line = candidate
        if line:
            lines.append(line)
        pdf.setFont(font, size)
        for part in lines:
            pdf.drawString(left, y, part)
            y -= leading

    pdf.setFont("Helvetica-Bold", 12)
    wrapped(store["store_name"], 31, "Helvetica-Bold", 12, 15)
    wrapped(store["address"], 39)
    wrapped("Phone: " + (store["phone"] or ""))
    wrapped("Email: " + (store["email"] or ""))
    wrapped("PAN: " + (store["pan"] or ""))
    wrapped("Tax ID: " + (store["tax_id"] or ""))
    y -= 4
    pdf.setLineWidth(0.6)
    pdf.line(left, y, right, y)
    y -= 16
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawCentredString(page_width / 2, y, "E-BILL / INVOICE")
    y -= 17
    wrapped("Bill: " + sale["invoice_number"], 39, "Helvetica-Bold", 8)
    wrapped("Date: " + sale["created_at"])
    wrapped("Customer: " + sale["customer_name"])
    wrapped("Phone: " + (sale["customer_phone"] or ""))
    wrapped("Email: " + sale["customer_email"])
    wrapped("Payment: " + sale["payment_method"])
    y -= 4
    pdf.line(left, y, right, y)
    y -= 14
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(left, y, "Item / Qty x Unit")
    pdf.drawRightString(right, y, "Amount")
    y -= 13
    pdf.setFont("Helvetica", 8)
    for item in items:
        wrapped(item["product_name"], 31, "Helvetica-Bold", 8, 10)
        pdf.drawString(left + 4, y, f'{item["quantity"]:g} x ${item["unit_price"]:.2f}')
        pdf.drawRightString(right, y, f'${item["total"]:.2f}')
        y -= 13
    y -= 3
    pdf.line(left, y, right, y)
    y -= 14
    before_discount = sale["subtotal"] + sale["tax"]
    discount_percent = (sale["discount"] / before_discount * 100) if before_discount else 0
    for label, value, bold in [
        ("Subtotal", sale["subtotal"], False),
        ("Tax (13%)", sale["tax"], False),
        ("Before discount", before_discount, False),
        (f"Discount ({discount_percent:.2f}%)", sale["discount"], False),
        ("FINAL TOTAL", sale["total"], True),
    ]:
        pdf.setFont("Helvetica-Bold" if bold else "Helvetica", 8)
        pdf.drawString(left, y, label)
        pdf.drawRightString(right, y, f"${value:.2f}")
        y -= 14
    y -= 5
    pdf.setFont("Helvetica-Oblique", 8)
    pdf.drawCentredString(page_width / 2, y, "Thank you for your business!")
    pdf.save()
    buffer.seek(0)
    return buffer.getvalue()


@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        owner_name = request.form["owner_name"].strip()
        username = request.form["username"].strip()
        email = request.form["email"].strip().lower()
        phone = request.form.get("phone", "").strip()
        password = request.form["password"]

        store_name = request.form["store_name"].strip()
        address = request.form.get("address", "").strip()
        store_phone = request.form.get("store_phone", "").strip()
        store_email = request.form.get("store_email", "").strip()
        pan = request.form.get("pan", "").strip()
        tax_id = request.form.get("tax_id", "").strip()

        if not owner_name or not username or not email or not password or not store_name:
            flash("Please complete all required fields.")
            return render_template("signup.html")

        conn = get_db()
        try:
            cur = conn.execute("""
                INSERT INTO users
                (owner_name, username, email, phone, password_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                owner_name, username, email, phone,
                generate_password_hash(password),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ))
            user_id = cur.lastrowid

            conn.execute("""
                INSERT INTO stores
                (user_id, store_name, address, phone, email, pan, tax_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id, store_name, address, store_phone,
                store_email, pan, tax_id,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            conn.close()
            flash("Username or email already exists.")
            return render_template("signup.html")

        conn.close()
        flash("Account created. Please log in.")
        return redirect(url_for("login"))

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        identifier = request.form["identifier"].strip()
        password = request.form["password"]

        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE lower(email) = lower(?) OR lower(username) = lower(?)",
            (identifier, identifier)
        ).fetchone()
        conn.close()

        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["owner_name"] = user["owner_name"]
            return redirect(url_for("dashboard"))

        flash("Invalid username/email or password.")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    user_id = session["user_id"]
    conn = get_db()

    sales = conn.execute("""
        SELECT * FROM sales
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 20
    """, (user_id,)).fetchall()

    today = datetime.now().strftime("%Y-%m-%d")
    stats = conn.execute("""
        SELECT COUNT(*) AS count, COALESCE(SUM(total), 0) AS revenue
        FROM sales
        WHERE user_id = ? AND substr(created_at, 1, 10) = ?
    """, (user_id, today)).fetchone()

    month = datetime.now().strftime("%Y-%m")
    monthly = conn.execute("""
        SELECT COUNT(*) AS count, COALESCE(SUM(total), 0) AS revenue
        FROM sales
        WHERE user_id = ? AND substr(created_at, 1, 7) = ?
    """, (user_id, month)).fetchone()

    conn.close()
    store = get_store(user_id)

    return render_template(
        "dashboard.html",
        sales=sales, store=store, stats=stats, monthly=monthly
    )


@app.route("/sale/new", methods=["GET", "POST"])
@login_required
def new_sale():
    if request.method == "POST":
        user_id = session.get("user_id")
        check_conn = get_db()
        try:
            existing_user = check_conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        finally:
            check_conn.close()
        if not existing_user:
            session.clear()
            flash("Your session expired or the account database changed. Please log in again.")
            return redirect(url_for("login"))
        customer_name = request.form["customer_name"].strip()
        customer_phone = request.form.get("customer_phone", "").strip()
        customer_email = request.form["customer_email"].strip()
        payment_method = request.form.get("payment_method", "").strip()

        if not payment_method:
            flash("Please select a payment method.")
            return render_template("new_sale.html")

        product_names = request.form.getlist("product_name")
        quantities = request.form.getlist("quantity")
        prices = request.form.getlist("unit_price")

        items = []
        subtotal = 0.0

        for name, qty_text, price_text in zip(product_names, quantities, prices):
            name = name.strip()
            if not name:
                continue
            try:
                qty = float(qty_text)
                price = float(price_text)
            except ValueError:
                flash("Quantity and price must be numbers.")
                return render_template("new_sale.html")

            if qty <= 0 or price < 0:
                flash("Quantity must be greater than 0 and price cannot be negative.")
                return render_template("new_sale.html")

            line_total = qty * price
            subtotal += line_total
            items.append((name, qty, price, line_total))

        if not customer_name or not customer_email or not items:
            flash("Customer name, customer email, and at least one item are required.")
            return render_template("new_sale.html")

        try:
            discount_percent = float(request.form.get("discount_percent", "0") or 0)
        except ValueError:
            flash("Discount must be a number.")
            return render_template("new_sale.html")

        if discount_percent < 0 or discount_percent > 100:
            flash("Discount must be between 0% and 100%.")
            return render_template("new_sale.html")

        # E-Bill calculation:
        # 1. Subtotal = quantity × unit price
        # 2. Tax = 13% of subtotal
        # 3. Total before discount = subtotal + tax
        # 4. Discount = discount percentage of total before discount
        # 5. Final total = total before discount - discount
        tax_rate = 13.0
        tax = subtotal * (tax_rate / 100)
        total_before_discount = subtotal + tax
        discount = total_before_discount * (discount_percent / 100)
        total = max(0, total_before_discount - discount)
        now = datetime.now()
        expires = now + timedelta(days=30)
        invoice = next_invoice_number(user_id)

        conn = get_db()
        try:
            cur = conn.execute("""
                INSERT INTO sales
                (user_id, invoice_number, customer_name, customer_phone, customer_email,
                 subtotal, discount, tax, total, payment_method, email_status, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'NOT_SENT', ?, ?)
            """, (
                user_id, invoice, customer_name, customer_phone, customer_email,
                subtotal, discount, tax, total, payment_method,
                now.strftime("%Y-%m-%d %H:%M:%S"),
                expires.strftime("%Y-%m-%d %H:%M:%S")
            ))
            sale_id = cur.lastrowid
            conn.executemany("""
                INSERT INTO sale_items
                (sale_id, product_name, quantity, unit_price, total)
                VALUES (?, ?, ?, ?, ?)
            """, [(sale_id, *item) for item in items])
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            app.logger.exception("Could not save new sale")
            flash("Could not save the bill because of a database error. Please log in again and retry.")
            return render_template("new_sale.html")
        finally:
            conn.close()

        flash(f"Bill {invoice} created. Choose Print Receipt or Send to Email below.")
        return redirect(url_for("view_sale", sale_id=sale_id))

    return render_template("new_sale.html")


@app.route("/sale/<int:sale_id>")
@login_required
def view_sale(sale_id):
    conn = get_db()
    sale = conn.execute("""
        SELECT * FROM sales WHERE id = ? AND user_id = ?
    """, (sale_id, session["user_id"])).fetchone()

    if not sale:
        conn.close()
        return "Sale not found", 404

    items = conn.execute(
        "SELECT * FROM sale_items WHERE sale_id = ? ORDER BY id", (sale_id,)
    ).fetchall()
    conn.close()

    store = get_store(session["user_id"])
    return render_template("sale.html", sale=sale, items=items, store=store)


@app.route("/sale/<int:sale_id>/pdf")
@login_required
def sale_pdf(sale_id):
    conn = get_db()
    sale = conn.execute("""
        SELECT * FROM sales WHERE id = ? AND user_id = ?
    """, (sale_id, session["user_id"])).fetchone()
    items = conn.execute(
        "SELECT * FROM sale_items WHERE sale_id = ?", (sale_id,)
    ).fetchall()
    conn.close()

    if not sale:
        return "Sale not found", 404

    pdf_bytes = create_bill_pdf(sale, items)
    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"{sale['invoice_number']}.pdf"
    )


@app.route("/sale/<int:sale_id>/resend-email", methods=["POST"])
@login_required
def resend_email(sale_id):
    conn = get_db()
    sale = conn.execute(
        "SELECT * FROM sales WHERE id = ? AND user_id = ?",
        (sale_id, session["user_id"])
    ).fetchone()
    conn.close()

    if not sale:
        return "Sale not found", 404

    ok, error = send_bill_email(sale_id)

    conn = get_db()
    if ok:
        conn.execute(
            "UPDATE sales SET email_status='SENT', email_error=NULL WHERE id=?",
            (sale_id,)
        )
        flash("Email sent successfully.")
    else:
        conn.execute(
            "UPDATE sales SET email_status='FAILED', email_error=? WHERE id=?",
            (error, sale_id)
        )
        flash(f"Email failed: {error}")
    conn.commit()
    conn.close()

    return redirect(url_for("view_sale", sale_id=sale_id))


@app.route("/settings/store", methods=["GET", "POST"])
@login_required
def store_settings():
    user_id = session["user_id"]
    conn = get_db()

    if request.method == "POST":
        conn.execute("""
            UPDATE stores SET
            store_name=?, address=?, phone=?, email=?, pan=?, tax_id=?
            WHERE user_id=?
        """, (
            request.form["store_name"].strip(),
            request.form.get("address", "").strip(),
            request.form.get("phone", "").strip(),
            request.form.get("email", "").strip(),
            request.form.get("pan", "").strip(),
            request.form.get("tax_id", "").strip(),
            user_id
        ))
        conn.commit()
        conn.close()
        flash("Store information updated.")
        return redirect(url_for("dashboard"))

    store = conn.execute(
        "SELECT * FROM stores WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return render_template("store_settings.html", store=store)


@app.cli.command("init-db")
def init_db_command():
    init_db()
    print("Database initialized.")


init_db()

if __name__ == "__main__":
    app.run(debug=True)