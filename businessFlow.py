import hashlib
import io
import secrets
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import plotly.express as px
import psycopg2
from psycopg2 import extras
import streamlit as st

st.set_page_config(
    page_title="Enterprise AI Inventory & BI Command Centre",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ==============================================================================
# 1. DATABASE CONNECTION & URL RESOLUTION
# ==============================================================================
def get_neon_url() -> str:
    """Resolve the Neon Postgres connection URL from Streamlit secrets."""
    if "neon" in st.secrets and "database_url" in st.secrets["neon"]:
        return st.secrets["neon"]["database_url"]
    if "database_url" in st.secrets:
        return st.secrets["database_url"]
    raise KeyError(
        "Missing Neon connection string. Define [neon] database_url in .streamlit/secrets.toml or Streamlit Cloud Secrets."
    )


def get_connection():
    """Open a new Postgres connection with SSL required for Neon."""
    url = get_neon_url()
    if "sslmode=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=require"
    return psycopg2.connect(url)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ==============================================================================
# 2. PASSWORD HASHING & AUTHENTICATION
# ==============================================================================
def hash_password(password: str, salt: str | None = None) -> str:
    if salt is None:
        salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        120_000,
    ).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt, expected_digest = stored_hash.split("$", 1)
    except ValueError:
        return False
    actual_digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        120_000,
    ).hex()
    return secrets.compare_digest(actual_digest, expected_digest)


# ==============================================================================
# 3. SCHEMA INITIALIZATION & AUDIT LOGGING
# ==============================================================================
@st.cache_resource
def init_database_schema():
    """Create all required tables and seed starter data on first run."""
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        username VARCHAR(100) UNIQUE NOT NULL,
                        full_name VARCHAR(150) NOT NULL,
                        role VARCHAR(30) NOT NULL DEFAULT 'staff',
                        password_hash TEXT NOT NULL,
                        is_active BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS products (
                        id SERIAL PRIMARY KEY,
                        sku VARCHAR(60) UNIQUE NOT NULL,
                        name VARCHAR(200) NOT NULL,
                        category VARCHAR(100) NOT NULL DEFAULT 'General',
                        unit_cost NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
                        selling_price NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
                        stock_qty INTEGER NOT NULL DEFAULT 0,
                        reorder_level INTEGER NOT NULL DEFAULT 10,
                        updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS transactions (
                        id SERIAL PRIMARY KEY,
                        tx_type VARCHAR(20) NOT NULL,
                        product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
                        sku VARCHAR(60) NOT NULL,
                        product_name VARCHAR(200) NOT NULL,
                        quantity INTEGER NOT NULL,
                        unit_price NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
                        total_amount NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
                        recorded_by VARCHAR(100) NOT NULL,
                        notes TEXT,
                        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS audit_logs (
                        id SERIAL PRIMARY KEY,
                        username VARCHAR(100) NOT NULL,
                        action VARCHAR(120) NOT NULL,
                        details TEXT,
                        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )

                # Seed initial admin only if the users table is completely empty
                cur.execute("SELECT COUNT(*) FROM users;")
                user_count = cur.fetchone()[0]
                if user_count == 0:
                    cur.execute(
                        """
                        INSERT INTO users (username, full_name, role, password_hash)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (username) DO NOTHING;
                        """,
                        ("admin", "System Administrator", "admin", hash_password("Admin@1234")),
                    )

                # Seed starter catalog if products table is empty
                cur.execute("SELECT COUNT(*) FROM products;")
                prod_count = cur.fetchone()[0]
                if prod_count == 0:
                    starter_products = [
                        ("SKU-1001", "Wireless Barcode Scanner", "Hardware", 180.00, 260.00, 24, 8),
                        ("SKU-1002", "Thermal Receipt Paper (Roll)", "Supplies", 6.50, 12.00, 120, 30),
                        ("SKU-1003", "POS Cash Drawer", "Hardware", 240.00, 340.00, 6, 5),
                        ("SKU-1004", "Inventory Label Stickers", "Supplies", 9.00, 16.50, 14, 15),
                    ]
                    extras.execute_batch(
                        cur,
                        """
                        INSERT INTO products (sku, name, category, unit_cost, selling_price, stock_qty, reorder_level)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (sku) DO NOTHING;
                        """,
                        starter_products,
                    )
    finally:
        conn.close()


def log_audit(username: str, action: str, details: str = ""):
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO audit_logs (username, action, details, created_at)
                    VALUES (%s, %s, %s, %s);
                    """,
                    (username, action, details, utc_now()),
                )
    except Exception:
        pass
    finally:
        conn.close()


# ==============================================================================
# 4. USER ACCOUNT OPERATIONS (STRICT 'staff' DEFAULT ON SELF-SIGNUP)
# ==============================================================================
def register_staff_user(username: str, full_name: str, password: str) -> tuple[bool, str]:
    """Create a standard 'staff' account. Admin promotion is restricted to admins."""
    clean_user = username.strip().lower()
    clean_name = full_name.strip()
    if len(clean_user) < 3:
        return False, "Username must be at least 3 characters."
    if len(clean_name) < 2:
        return False, "Full name is required."
    if len(password) < 6:
        return False, "Password must be at least 6 characters."

    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (username, full_name, role, password_hash, is_active, created_at)
                    VALUES (%s, %s, 'staff', %s, TRUE, %s)
                    ON CONFLICT (username) DO NOTHING
                    RETURNING id;
                    """,
                    (clean_user, clean_name, hash_password(password), utc_now()),
                )
                row = cur.fetchone()
                if row is None:
                    return False, f"Username '{clean_user}' already exists."
        log_audit(clean_user, "STAFF_SIGNUP", "Self-registered with role 'staff'")
        return True, f"Staff account created for @{clean_user}. You can sign in now."
    except Exception as exc:
        return False, f"Error creating account: {exc}"
    finally:
        conn.close()


def authenticate_user(username: str, password: str) -> dict | None:
    clean_user = username.strip().lower()
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, username, full_name, role, password_hash, is_active
                FROM users
                WHERE username = %s;
                """,
                (clean_user,),
            )
            user = cur.fetchone()
            if not user or not user["is_active"]:
                return None
            if verify_password(password, user["password_hash"]):
                return {
                    "id": user["id"],
                    "username": user["username"],
                    "full_name": user["full_name"],
                    "role": user["role"],
                }
        return None
    finally:
        conn.close()


def fetch_all_users() -> pd.DataFrame:
    conn = get_connection()
    try:
        return pd.read_sql_query(
            "SELECT id, username, full_name, role, is_active, created_at FROM users ORDER BY id;",
            conn,
        )
    finally:
        conn.close()


def update_user_role_or_status(user_id: int, role: str, is_active: bool, actor_role: str, actor: str) -> tuple[bool, str]:
    if actor_role != "admin":
        return False, "Only administrators can change user roles or status."
    if role not in ("staff", "admin"):
        return False, "Invalid role selection."

    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE users
                    SET role = %s, is_active = %s
                    WHERE id = %s
                    RETURNING username;
                    """,
                    (role, is_active, user_id),
                )
                updated = cur.fetchone()
        if not updated:
            return False, "User not found."
        log_audit(actor, "USER_ROLE_UPDATED", f"Target @{updated[0]} -> role={role}, active={is_active}")
        return True, f"Updated @{updated[0]} (Role: {role}, Active: {is_active})."
    except Exception as exc:
        return False, f"Failed to update user: {exc}"
    finally:
        conn.close()


# ==============================================================================
# 5. INVENTORY & TRANSACTION OPERATIONS
# ==============================================================================
def fetch_products() -> pd.DataFrame:
    conn = get_connection()
    try:
        df = pd.read_sql_query(
            """
            SELECT
                id,
                sku,
                name,
                category,
                unit_cost::float AS unit_cost,
                selling_price::float AS selling_price,
                stock_qty,
                reorder_level,
                updated_at
            FROM products
            ORDER BY id DESC;
            """,
            conn,
        )
        if not df.empty:
            df["stock_value"] = df["stock_qty"] * df["unit_cost"]
            df["retail_value"] = df["stock_qty"] * df["selling_price"]
            df["status"] = df.apply(
                lambda r: "Out of Stock"
                if r["stock_qty"] <= 0
                else ("Low Stock" if r["stock_qty"] <= r["reorder_level"] else "Healthy"),
                axis=1,
            )
        return df
    finally:
        conn.close()


def add_or_update_product(
    sku: str,
    name: str,
    category: str,
    unit_cost: float,
    selling_price: float,
    stock_qty: int,
    reorder_level: int,
    actor: str,
) -> tuple[bool, str]:
    clean_sku = sku.strip().upper()
    clean_name = name.strip()
    clean_cat = category.strip() or "General"
    if not clean_sku or not clean_name:
        return False, "SKU and Product Name are required."

    now_ts = utc_now()
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO products (sku, name, category, unit_cost, selling_price, stock_qty, reorder_level, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (sku) DO UPDATE SET
                        name = EXCLUDED.name,
                        category = EXCLUDED.category,
                        unit_cost = EXCLUDED.unit_cost,
                        selling_price = EXCLUDED.selling_price,
                        stock_qty = EXCLUDED.stock_qty,
                        reorder_level = EXCLUDED.reorder_level,
                        updated_at = EXCLUDED.updated_at
                    RETURNING id;
                    """,
                    (clean_sku, clean_name, clean_cat, unit_cost, selling_price, stock_qty, reorder_level, now_ts),
                )
                prod_id = cur.fetchone()[0]

                if stock_qty > 0:
                    cur.execute(
                        """
                        INSERT INTO transactions (tx_type, product_id, sku, product_name, quantity, unit_price, total_amount, recorded_by, notes, created_at)
                        VALUES ('STOCK_IN', %s, %s, %s, %s, %s, %s, %s, 'Initial / catalog upsert', %s);
                        """,
                        (
                            prod_id,
                            clean_sku,
                            clean_name,
                            stock_qty,
                            unit_cost,
                            round(stock_qty * unit_cost, 2),
                            actor,
                            now_ts,
                        ),
                    )
        log_audit(actor, "PRODUCT_UPSERT", f"{clean_sku} ({clean_name}) qty={stock_qty}")
        return True, f"Saved product {clean_sku} — {clean_name}."
    except Exception as exc:
        return False, f"Failed to save product: {exc}"
    finally:
        conn.close()


def record_stock_movement(
    product_id: int,
    tx_type: str,
    quantity: int,
    custom_price: float | None,
    notes: str,
    actor: str,
) -> tuple[bool, str]:
    if quantity <= 0:
        return False, "Quantity must be greater than zero."

    now_ts = utc_now()
    conn = get_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM products WHERE id = %s FOR UPDATE;", (product_id,))
                prod = cur.fetchone()
                if not prod:
                    return False, "Selected product was not found."

                current_qty = int(prod["stock_qty"])
                if tx_type == "SALE":
                    if current_qty < quantity:
                        return False, f"Insufficient stock. Available: {current_qty}, requested: {quantity}."
                    new_qty = current_qty - quantity
                    unit_price = float(custom_price) if custom_price is not None else float(prod["selling_price"])
                elif tx_type == "RESTOCK":
                    new_qty = current_qty + quantity
                    unit_price = float(custom_price) if custom_price is not None else float(prod["unit_cost"])
                else:
                    return False, "Unsupported movement type."

                total_amount = round(quantity * unit_price, 2)

                cur.execute(
                    """
                    UPDATE products
                    SET stock_qty = %s, updated_at = %s
                    WHERE id = %s;
                    """,
                    (new_qty, now_ts, product_id),
                )

                cur.execute(
                    """
                    INSERT INTO transactions (tx_type, product_id, sku, product_name, quantity, unit_price, total_amount, recorded_by, notes, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                    """,
                    (
                        tx_type,
                        product_id,
                        prod["sku"],
                        prod["name"],
                        quantity,
                        unit_price,
                        total_amount,
                        actor,
                        notes.strip(),
                        now_ts,
                    ),
                )
        log_audit(actor, f"TX_{tx_type}", f"{prod['sku']} qty={quantity} total={total_amount}")
        return True, f"{tx_type} recorded: {quantity} × {prod['name']} (Total: {total_amount:,.2f})."
    except Exception as exc:
        return False, f"Transaction failed: {exc}"
    finally:
        conn.close()


def delete_product(product_id: int, actor_role: str, actor: str) -> tuple[bool, str]:
    if actor_role != "admin":
        return False, "Only administrators can delete products."

    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM products WHERE id = %s RETURNING sku, name;", (product_id,))
                deleted = cur.fetchone()
        if not deleted:
            return False, "Product not found."
        log_audit(actor, "PRODUCT_DELETED", f"{deleted[0]} ({deleted[1]})")
        return True, f"Deleted {deleted[0]} — {deleted[1]}."
    except Exception as exc:
        return False, f"Delete failed: {exc}"
    finally:
        conn.close()


def fetch_transactions(days: int = 90) -> pd.DataFrame:
    conn = get_connection()
    try:
        cutoff = utc_now() - timedelta(days=days)
        return pd.read_sql_query(
            """
            SELECT
                id,
                tx_type,
                sku,
                product_name,
                quantity,
                unit_price::float AS unit_price,
                total_amount::float AS total_amount,
                recorded_by,
                notes,
                created_at
            FROM transactions
            WHERE created_at >= %s
            ORDER BY created_at DESC, id DESC;
            """,
            conn,
            params=(cutoff,),
        )
    finally:
        conn.close()


def fetch_audit_logs(limit: int = 100) -> pd.DataFrame:
    conn = get_connection()
    try:
        return pd.read_sql_query(
            """
            SELECT id, username, action, details, created_at
            FROM audit_logs
            ORDER BY created_at DESC, id DESC
            LIMIT %s;
            """,
            conn,
            params=(limit,),
        )
    finally:
        conn.close()


# ==============================================================================
# 6. AUTHENTICATION UI (NO ADMIN ROLE SELECTION ON SIGN-UP)
# ==============================================================================
def render_auth_screen():
    st.title("Enterprise AI Inventory & BI Command Centre")
    st.caption("Sign in with your account or register a staff account.")

    _, col_center, _ = st.columns([1, 2, 1])
    with col_center:
        tab_login, tab_register = st.tabs(["Sign In", "Sign Up (Staff)"])

        with tab_login:
            with st.form("login_form", clear_on_submit=False):
                username = st.text_input("Username", placeholder="Enter your username")
                password = st.text_input("Password", type="password", placeholder="Enter your password")
                submitted = st.form_submit_button("Sign In", type="primary", use_container_width=True)
                if submitted:
                    user = authenticate_user(username, password)
                    if user:
                        st.session_state["auth_user"] = user
                        log_audit(user["username"], "USER_LOGIN", "Successful sign-in")
                        st.rerun()
                    else:
                        log_audit(username.strip().lower() or "unknown", "LOGIN_FAILED", "Invalid credentials")
                        st.error("Invalid username or password, or account is inactive.")

            st.caption("Default bootstrap admin on first run: `admin` / `Admin@1234` (update password after signing in).")

        with tab_register:
            with st.form("register_form", clear_on_submit=True):
                full_name = st.text_input("Full Name", placeholder="e.g. Kwame Mensah")
                new_username = st.text_input("Username", placeholder="Lowercase, min 3 chars")
                new_password = st.text_input("Password", type="password", placeholder="Min 6 characters")
                confirm_password = st.text_input("Confirm Password", type="password")
                st.caption("New sign-ups are assigned the `staff` role automatically. Admin access is granted only by an administrator.")
                created = st.form_submit_button("Create Staff Account", use_container_width=True)
                if created:
                    if new_password != confirm_password:
                        st.error("Passwords do not match.")
                    else:
                        ok, msg = register_staff_user(new_username, full_name, new_password)
                        if ok:
                            st.success(msg)
                        else:
                            st.error(msg)


# ==============================================================================
# 7. MAIN APPLICATION MODULES & ROLE-LOCKED TABS
# ==============================================================================
def render_overview_tab(products_df: pd.DataFrame, tx_df: pd.DataFrame):
    st.subheader("Executive KPIs & Alerts")

    total_skus = len(products_df)
    total_units = int(products_df["stock_qty"].sum()) if not products_df.empty else 0
    inventory_cost_val = float(products_df["stock_value"].sum()) if not products_df.empty else 0.0
    low_stock_df = (
        products_df[products_df["status"].isin(["Low Stock", "Out of Stock"])]
        if not products_df.empty
        else pd.DataFrame()
    )

    sales_df = tx_df[tx_df["tx_type"] == "SALE"] if not tx_df.empty else pd.DataFrame()
    total_sales_rev = float(sales_df["total_amount"].sum()) if not sales_df.empty else 0.0

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Active SKUs", f"{total_skus:,}")
    c2.metric("Total Stock Units", f"{total_units:,}")
    c3.metric("Stock Valuation (Cost)", f"{inventory_cost_val:,.2f}")
    c4.metric("Recorded Sales Revenue", f"{total_sales_rev:,.2f}")
    c5.metric("Low / Out of Stock", f"{len(low_stock_df):,}")

    st.divider()

    if not low_stock_df.empty:
        st.warning("Attention: The following items are at or below their reorder threshold:")
        st.dataframe(
            low_stock_df[["sku", "name", "category", "stock_qty", "reorder_level", "status"]],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.success("All inventory items are above their reorder thresholds.")

    st.subheader("Recent Transactions")
    if not tx_df.empty:
        st.dataframe(tx_df.head(15), use_container_width=True, hide_index=True)
    else:
        st.info("No transactions recorded yet.")


def render_inventory_tab(products_df: pd.DataFrame, user: dict):
    st.subheader("Inventory Catalog & Stock Movements")

    col_left, col_right = st.columns([1, 1.4])

    with col_left:
        st.markdown("#### Record Sale or Restock")
        if products_df.empty:
            st.info("Add products first to record stock movements.")
        else:
            product_options = {
                f"{row['sku']} — {row['name']} (Qty: {row['stock_qty']})": int(row["id"])
                for _, row in products_df.iterrows()
            }
            with st.form("movement_form", clear_on_submit=True):
                selected_label = st.selectbox("Product", list(product_options.keys()))
                tx_type = st.selectbox("Movement Type", ["SALE", "RESTOCK"])
                qty = st.number_input("Quantity", min_value=1, step=1, value=1)
                override_price = st.number_input(
                    "Custom Unit Price (0 = use default price/cost)",
                    min_value=0.0,
                    step=0.5,
                    value=0.0,
                )
                notes = st.text_input("Notes / Reference", placeholder="e.g. Receipt #104 or Supplier delivery")
                submitted = st.form_submit_button("Submit Movement", type="primary", use_container_width=True)
                if submitted:
                    prod_id = product_options[selected_label]
                    price_val = None if override_price <= 0 else float(override_price)
                    ok, msg = record_stock_movement(
                        product_id=prod_id,
                        tx_type=tx_type,
                        quantity=int(qty),
                        custom_price=price_val,
                        notes=notes,
                        actor=user["username"],
                    )
                    if ok:
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)

        st.markdown("#### Add or Update Product")
        with st.form("product_form", clear_on_submit=True):
            sku = st.text_input("SKU", placeholder="e.g. SKU-1005")
            name = st.text_input("Product Name", placeholder="e.g. Barcode Label Printer")
            category = st.text_input("Category", value="General")
            col_c, col_p = st.columns(2)
            unit_cost = col_c.number_input("Unit Cost", min_value=0.0, step=0.5, value=0.0)
            selling_price = col_p.number_input("Selling Price", min_value=0.0, step=0.5, value=0.0)
            col_q, col_r = st.columns(2)
            stock_qty = col_q.number_input("Stock Quantity", min_value=0, step=1, value=0)
            reorder_level = col_r.number_input("Reorder Level", min_value=0, step=1, value=10)
            saved = st.form_submit_button("Save Product", use_container_width=True)
            if saved:
                ok, msg = add_or_update_product(
                    sku=sku,
                    name=name,
                    category=category,
                    unit_cost=float(unit_cost),
                    selling_price=float(selling_price),
                    stock_qty=int(stock_qty),
                    reorder_level=int(reorder_level),
                    actor=user["username"],
                )
                if ok:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)

    with col_right:
        st.markdown("#### Current Stock Table")
        if products_df.empty:
            st.info("No products found.")
        else:
            search_q = st.text_input("Filter by SKU, Name, or Category", placeholder="Type to filter...")
            filtered = products_df.copy()
            if search_q.strip():
                q = search_q.strip().lower()
                filtered = filtered[
                    filtered["sku"].str.lower().str.contains(q, na=False)
                    | filtered["name"].str.lower().str.contains(q, na=False)
                    | filtered["category"].str.lower().str.contains(q, na=False)
                ]

            st.dataframe(filtered, use_container_width=True, hide_index=True)

            csv_buf = io.StringIO()
            filtered.to_csv(csv_buf, index=False)
            st.download_button(
                label="Download Filtered Inventory CSV",
                data=csv_buf.getvalue(),
                file_name=f"inventory_{date.today().isoformat()}.csv",
                mime="text/csv",
            )

            if user.get("role") == "admin":
                st.markdown("##### Admin: Delete Product")
                del_map = {f"{r['sku']} — {r['name']}": int(r["id"]) for _, r in products_df.iterrows()}
                del_choice = st.selectbox("Select product to remove", list(del_map.keys()), key="del_prod_select")
                if st.button("Delete Selected Product", type="secondary"):
                    ok, msg = delete_product(
                        del_map[del_choice],
                        actor_role=user.get("role", "staff"),
                        actor=user["username"],
                    )
                    if ok:
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)


def render_bi_analytics_tab(products_df: pd.DataFrame, tx_df: pd.DataFrame):
    st.subheader("Business Intelligence & Analytics")

    if not products_df.empty:
        st.markdown("#### Stock Value by Category")
        cat_summary = (
            products_df.groupby("category", as_index=False)[["stock_qty", "stock_value", "retail_value"]]
            .sum()
            .sort_values("stock_value", ascending=False)
        )
        fig_cat = px.bar(
            cat_summary,
            x="category",
            y="stock_value",
            hover_data=["stock_qty", "retail_value"],
            title="Inventory Cost Valuation by Category",
        )
        st.plotly_chart(fig_cat, use_container_width=True)

    if not tx_df.empty:
        sales_only = tx_df[tx_df["tx_type"] == "SALE"].copy()
        if not sales_only.empty:
            sales_only["date"] = pd.to_datetime(sales_only["created_at"]).dt.date
            daily_sales = sales_only.groupby("date", as_index=False)["total_amount"].sum()

            st.markdown("#### Daily Sales Trend")
            fig_sales = px.line(
                daily_sales,
                x="date",
                y="total_amount",
                markers=True,
                title="Daily Revenue From Recorded Sales",
            )
            st.plotly_chart(fig_sales, use_container_width=True)

            st.markdown("#### Top Selling Products by Revenue")
            top_prods = (
                sales_only.groupby(["sku", "product_name"], as_index=False)[["quantity", "total_amount"]]
                .sum()
                .sort_values("total_amount", ascending=False)
                .head(10)
            )
            st.dataframe(top_prods, use_container_width=True, hide_index=True)
        else:
            st.info("Record SALE movements in the Inventory tab to populate sales charts.")
    else:
        st.info("No transaction data available for analytics yet.")


def render_bulk_import_tab(user: dict):
    st.subheader("Bulk CSV Product Import")
    st.caption("Upload a CSV with columns: `sku`, `name`, `category`, `unit_cost`, `selling_price`, `stock_qty`, `reorder_level`.")

    sample_csv = (
        "sku,name,category,unit_cost,selling_price,stock_qty,reorder_level\n"
        "SKU-2001,USB-C Hub 7-Port,Accessories,45.00,75.00,30,10\n"
        "SKU-2002,A4 Thermal Labels,Supplies,12.00,22.00,50,15\n"
    )
    st.download_button(
        label="Download Sample CSV Template",
        data=sample_csv,
        file_name="sample_products_import.csv",
        mime="text/csv",
    )

    uploaded = st.file_uploader("Choose CSV file", type=["csv"])
    if uploaded is not None:
        try:
            df_up = pd.read_csv(uploaded)
            st.dataframe(df_up.head(20), use_container_width=True, hide_index=True)

            if st.button("Import Rows Into Neon", type="primary"):
                required_cols = {"sku", "name"}
                if not required_cols.issubset(set(df_up.columns.str.lower().str.strip())):
                    st.error("CSV must contain at least 'sku' and 'name' columns.")
                    return

                df_up.columns = [c.strip().lower() for c in df_up.columns]
                imported = 0
                errors = []
                for idx, row in df_up.iterrows():
                    ok, msg = add_or_update_product(
                        sku=str(row.get("sku", "")),
                        name=str(row.get("name", "")),
                        category=str(row.get("category", "General")) if pd.notna(row.get("category")) else "General",
                        unit_cost=float(row.get("unit_cost", 0.0)) if pd.notna(row.get("unit_cost")) else 0.0,
                        selling_price=float(row.get("selling_price", 0.0)) if pd.notna(row.get("selling_price")) else 0.0,
                        stock_qty=int(row.get("stock_qty", 0)) if pd.notna(row.get("stock_qty")) else 0,
                        reorder_level=int(row.get("reorder_level", 10)) if pd.notna(row.get("reorder_level")) else 10,
                        actor=user["username"],
                    )
                    if ok:
                        imported += 1
                    else:
                        errors.append(f"Row {idx + 1}: {msg}")

                log_audit(user["username"], "CSV_BULK_IMPORT", f"Imported {imported} rows")
                st.success(f"Imported/updated {imported} products.")
                if errors:
                    st.warning("\n".join(errors))
                st.rerun()
        except Exception as exc:
            st.error(f"Could not read CSV: {exc}")


def render_admin_tab(user: dict):
    st.subheader("User Management & Audit Trail")

    if user.get("role") != "admin":
        st.error("Access denied: Administrator privileges are required.")
        return

    users_df = fetch_all_users()
    st.markdown("#### Registered Users")
    st.dataframe(users_df, use_container_width=True, hide_index=True)

    if not users_df.empty:
        user_map = {f"@{r['username']} — {r['full_name']} ({r['role']})": int(r["id"]) for _, r in users_df.iterrows()}
        with st.form("manage_user_form"):
            target_label = st.selectbox("Select User to Manage", list(user_map.keys()))
            new_role = st.selectbox("Assign Role", ["staff", "admin"])
            new_active = st.checkbox("Active Account", value=True)
            apply_btn = st.form_submit_button("Save User Role & Status", type="primary")
            if apply_btn:
                ok, msg = update_user_role_or_status(
                    user_id=user_map[target_label],
                    role=new_role,
                    is_active=new_active,
                    actor_role=user.get("role", "staff"),
                    actor=user["username"],
                )
                if ok:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)

    st.markdown("#### Recent Audit Logs")
    logs_df = fetch_audit_logs(limit=100)
    if not logs_df.empty:
        st.dataframe(logs_df, use_container_width=True, hide_index=True)
    else:
        st.info("No audit entries recorded yet.")


def render_main_app(user: dict):
    """Render the main dashboard with role-gated tabs (staff: 2 tabs, admin: 5 tabs)."""
    is_admin = user.get("role") == "admin"

    with st.sidebar:
        st.markdown(f"### {user['full_name']}")
        st.caption(f"@{user['username']} • Role: `{user['role']}`")
        days_window = st.selectbox("Analytics History Window", [7, 30, 90, 365], index=2)
        if st.button("Sign Out", use_container_width=True):
            log_audit(user["username"], "USER_LOGOUT", "Signed out")
            st.session_state.pop("auth_user", None)
            st.rerun()

    st.title("Enterprise AI Inventory & BI Command Centre")

    products_df = fetch_products()
    tx_df = fetch_transactions(days=int(days_window))

    tab_labels = ["Overview & KPIs", "Inventory & Sales"]
    if is_admin:
        tab_labels.extend(["BI Analytics", "CSV Import", "Users & Audit Log"])

    tabs = st.tabs(tab_labels)

    with tabs[0]:
        render_overview_tab(products_df, tx_df)

    with tabs[1]:
        render_inventory_tab(products_df, user)

    if is_admin:
        with tabs[2]:
            render_bi_analytics_tab(products_df, tx_df)

        with tabs[3]:
            render_bulk_import_tab(user)

        with tabs[4]:
            render_admin_tab(user)


# ==============================================================================
# 8. ENTRY POINT
# ==============================================================================
def main():
    try:
        init_database_schema()
    except Exception as exc:
        st.error("Database connection or schema initialization failed.")
        st.exception(exc)
        st.stop()

    if "auth_user" not in st.session_state:
        render_auth_screen()
    else:
        render_main_app(st.session_state["auth_user"])


if __name__ == "__main__":
    main()
