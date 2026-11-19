import hashlib
import io
import logging
import secrets
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import plotly.express as px
import psycopg2
from psycopg2 import InterfaceError, OperationalError, extras
import streamlit as st

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("businessFlow")

st.set_page_config(
    page_title="Enterprise AI Inventory & BI Command Centre",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ==============================================================================
# 1. BULLETPROOF DATABASE CONNECTION & RETRY ENGINE
# ==============================================================================
def get_neon_url() -> str:
    """Resolve the Neon Postgres connection URL safely from Streamlit secrets."""
    try:
        if "neon" in st.secrets and "database_url" in st.secrets["neon"]:
            url = str(st.secrets["neon"]["database_url"]).strip()
            if url:
                return url
        if "database_url" in st.secrets:
            url = str(st.secrets["database_url"]).strip()
            if url:
                return url
    except Exception as exc:
        raise RuntimeError(f"Unable to read Streamlit secrets: {exc}") from exc

    raise RuntimeError(
        "Missing Neon connection string. Set [neon] database_url in Streamlit Secrets."
    )


def _normalize_neon_url(url: str) -> str:
    cleaned = url.strip()
    if "sslmode=" not in cleaned:
        sep = "&" if "?" in cleaned else "?"
        cleaned = f"{cleaned}{sep}sslmode=require"
    if "connect_timeout=" not in cleaned:
        sep = "&" if "?" in cleaned else "?"
        cleaned = f"{cleaned}{sep}connect_timeout=10"
    return cleaned


def open_connection(retries: int = 2):
    """Open a Postgres connection with retry support for transient Neon cold-starts."""
    url = _normalize_neon_url(get_neon_url())
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return psycopg2.connect(url)
        except (OperationalError, InterfaceError) as exc:
            last_exc = exc
            logger.warning("Postgres connection attempt %s failed: %s", attempt + 1, exc)
    raise last_exc  # type: ignore[misc]


def run_read_query(sql: str, params: tuple | list | None = None) -> pd.DataFrame:
    """Execute a read query safely and always close the connection."""
    conn = None
    try:
        conn = open_connection(retries=2)
        with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
            cur.execute(sql, params or ())
            rows = cur.fetchall()
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame([dict(r) for r in rows])
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def run_write_tx(fn):
    """Execute a transactional write callback with automatic commit/rollback and retry on disconnect."""
    last_exc = None
    for attempt in range(2):
        conn = None
        try:
            conn = open_connection(retries=1)
            with conn:
                with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
                    result = fn(cur)
            return result
        except (OperationalError, InterfaceError) as exc:
            last_exc = exc
            logger.warning("Transient DB write error on attempt %s: %s", attempt + 1, exc)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    raise last_exc  # type: ignore[misc]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def safe_float(val, default: float = 0.0) -> float:
    try:
        if val is None or pd.isna(val):
            return default
        return float(val)
    except Exception:
        return default


def safe_int(val, default: int = 0) -> int:
    try:
        if val is None or pd.isna(val):
            return default
        return int(float(val))
    except Exception:
        return default


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


def verify_password(password: str, stored_hash: str | None) -> bool:
    if not stored_hash or "$" not in stored_hash:
        return False
    try:
        salt, expected_digest = stored_hash.split("$", 1)
        actual_digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            120_000,
        ).hex()
        return secrets.compare_digest(actual_digest, expected_digest)
    except Exception:
        return False


# ==============================================================================
# 3. SCHEMA INITIALIZATION, SELF-HEALING & AUDIT LOGGING
# ==============================================================================
def _apply_schema_and_seed(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(100) UNIQUE NOT NULL,
            full_name VARCHAR(150) NOT NULL DEFAULT 'User',
            role VARCHAR(30) NOT NULL DEFAULT 'staff',
            password_hash TEXT NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            reset_token TEXT,
            reset_expires_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    # Inspect existing columns on `users` so migrations adapt safely
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'users';
        """
    )
    existing_cols = {r["column_name"] for r in cur.fetchall()}

    if "username" not in existing_cols:
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS username VARCHAR(100);")
        if "email" in existing_cols:
            cur.execute(
                "UPDATE users SET username = LOWER(TRIM(email)) WHERE username IS NULL AND email IS NOT NULL;"
            )
        elif "user_name" in existing_cols:
            cur.execute(
                "UPDATE users SET username = LOWER(TRIM(user_name)) WHERE username IS NULL AND user_name IS NOT NULL;"
            )
        elif "login" in existing_cols:
            cur.execute(
                "UPDATE users SET username = LOWER(TRIM(login)) WHERE username IS NULL AND login IS NOT NULL;"
            )
        if "id" in existing_cols:
            cur.execute(
                "UPDATE users SET username = 'user_' || id::text WHERE username IS NULL OR TRIM(username) = '';"
            )
        else:
            cur.execute(
                "UPDATE users SET username = 'staff_user' WHERE username IS NULL OR TRIM(username) = '';"
            )
        cur.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'users_username_unique'
                ) THEN
                    ALTER TABLE users ADD CONSTRAINT users_username_unique UNIQUE (username);
                END IF;
            END$$;
            """
        )

    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name VARCHAR(150) NOT NULL DEFAULT 'User';")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(30) NOT NULL DEFAULT 'staff';")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token TEXT;")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_expires_at TIMESTAMPTZ;")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;")

    cur.execute(
        """
        UPDATE users
        SET full_name = username
        WHERE (full_name IS NULL OR TRIM(full_name) = '' OR full_name = 'User')
          AND username IS NOT NULL AND TRIM(username) <> '';
        """
    )
    cur.execute("UPDATE users SET role = 'staff' WHERE role IS NULL OR TRIM(role) = '';")
    cur.execute("UPDATE users SET is_active = TRUE WHERE is_active IS NULL;")

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

    cur.execute("SELECT COUNT(*) AS cnt FROM users;")
    row = cur.fetchone()
    user_count = safe_int(row["cnt"] if row else 0)
    if user_count == 0:
        cur.execute(
            """
            INSERT INTO users (username, full_name, role, password_hash)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (username) DO NOTHING;
            """,
            ("admin", "System Administrator", "admin", hash_password("Admin@1234")),
        )

    cur.execute("SELECT COUNT(*) AS cnt FROM products;")
    prow = cur.fetchone()
    prod_count = safe_int(prow["cnt"] if prow else 0)
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


@st.cache_resource(show_spinner="Connecting to database and verifying schema...")
def init_database_schema() -> bool:
    """Initialize schema once per process; clear cache automatically on failure."""
    try:
        run_write_tx(_apply_schema_and_seed)
        return True
    except Exception:
        init_database_schema.clear()
        raise


def log_audit(username: str, action: str, details: str = ""):
    """Best-effort audit logging that never crashes the main user workflow."""
    try:
        clean_actor = (username or "system").strip().lower()[:100]
        clean_action = str(action)[:120]
        clean_details = str(details)[:1000]
        now_ts = utc_now()

        def _op(cur):
            cur.execute(
                """
                INSERT INTO audit_logs (username, action, details, created_at)
                VALUES (%s, %s, %s, %s);
                """,
                (clean_actor, clean_action, clean_details, now_ts),
            )

        run_write_tx(_op)
    except Exception as exc:
        logger.warning("Audit log write skipped: %s", exc)


# ==============================================================================
# 4. USER ACCOUNT, CHANGE PASSWORD & RECOVERY OPERATIONS
# ==============================================================================
def register_staff_user(username: str, full_name: str, password: str) -> tuple[bool, str]:
    """Create a standard 'staff' account. Admin promotion is restricted to admins."""
    clean_user = (username or "").strip().lower()
    clean_name = (full_name or "").strip()
    if len(clean_user) < 3:
        return False, "Username must be at least 3 characters."
    if len(clean_name) < 2:
        return False, "Full name is required."
    if len(password or "") < 6:
        return False, "Password must be at least 6 characters."

    pwd_hash = hash_password(password)
    now_ts = utc_now()

    def _op(cur):
        cur.execute(
            """
            INSERT INTO users (username, full_name, role, password_hash, is_active, created_at)
            VALUES (%s, %s, 'staff', %s, TRUE, %s)
            ON CONFLICT (username) DO NOTHING
            RETURNING id;
            """,
            (clean_user, clean_name, pwd_hash, now_ts),
        )
        return cur.fetchone()

    try:
        row = run_write_tx(_op)
        if row is None:
            return False, f"Username '{clean_user}' already exists."
        log_audit(clean_user, "STAFF_SIGNUP", "Self-registered with role 'staff'")
        return True, f"Staff account created for @{clean_user}. You can sign in now."
    except Exception as exc:
        logger.exception("Error registering user %s", clean_user)
        return False, f"Error creating account: {exc}"


def authenticate_user(username: str, password: str) -> dict | None:
    clean_user = (username or "").strip().lower()
    if not clean_user or not password:
        return None

    try:
        df = run_read_query(
            """
            SELECT id, username, full_name, role, password_hash, is_active
            FROM users
            WHERE username = %s
            LIMIT 1;
            """,
            (clean_user,),
        )
        if df.empty:
            return None
        rec = df.iloc[0].to_dict()
        if not bool(rec.get("is_active", False)):
            return None
        if verify_password(password, rec.get("password_hash")):
            return {
                "id": safe_int(rec.get("id")),
                "username": str(rec.get("username") or clean_user),
                "full_name": str(rec.get("full_name") or rec.get("username") or clean_user),
                "role": str(rec.get("role") or "staff"),
            }
        return None
    except Exception as exc:
        logger.exception("Authentication failed for %s", clean_user)
        st.error(f"Authentication error: {exc}")
        return None


def change_user_password(username: str, current_password: str, new_password: str) -> tuple[bool, str]:
    clean_user = (username or "").strip().lower()
    if len(new_password or "") < 6:
        return False, "New password must be at least 6 characters."
    if current_password == new_password:
        return False, "New password must be different from your current password."

    try:
        df = run_read_query(
            "SELECT id, password_hash, is_active FROM users WHERE username = %s LIMIT 1;",
            (clean_user,),
        )
        if df.empty:
            return False, "Account not found."
        rec = df.iloc[0].to_dict()
        if not bool(rec.get("is_active", False)):
            return False, "Account is inactive."
        if not verify_password(current_password, rec.get("password_hash")):
            log_audit(clean_user, "PASSWORD_CHANGE_FAILED", "Incorrect current password")
            return False, "Current password is incorrect."

        user_id = safe_int(rec.get("id"))
        new_hash = hash_password(new_password)

        def _op(cur):
            cur.execute(
                """
                UPDATE users
                SET password_hash = %s, reset_token = NULL, reset_expires_at = NULL
                WHERE id = %s;
                """,
                (new_hash, user_id),
            )

        run_write_tx(_op)
        log_audit(clean_user, "PASSWORD_CHANGED", "Updated password via account settings")
        return True, "Password changed successfully."
    except Exception as exc:
        logger.exception("Password change failed for %s", clean_user)
        return False, f"Failed to change password: {exc}"


def issue_password_reset_token(username: str, ttl_minutes: int = 30) -> tuple[bool, str | None]:
    clean_user = (username or "").strip().lower()
    if not clean_user:
        return False, None

    token = secrets.token_urlsafe(24)
    expires_at = utc_now() + timedelta(minutes=ttl_minutes)

    def _op(cur):
        cur.execute(
            """
            UPDATE users
            SET reset_token = %s, reset_expires_at = %s
            WHERE username = %s AND is_active = TRUE
            RETURNING username;
            """,
            (token, expires_at, clean_user),
        )
        return cur.fetchone()

    try:
        updated = run_write_tx(_op)
        if not updated:
            log_audit(clean_user, "PASSWORD_RESET_REQUEST", "Requested for non-existent or inactive user")
            return False, None
        log_audit(clean_user, "PASSWORD_RESET_TOKEN_ISSUED", f"Expires in {ttl_minutes} minutes")
        return True, token
    except Exception as exc:
        logger.exception("Failed to issue reset token for %s", clean_user)
        return False, None


def reset_password_with_token(token: str, new_password: str) -> tuple[bool, str]:
    clean_token = (token or "").strip()
    if not clean_token:
        return False, "Recovery token is required."
    if len(new_password or "") < 6:
        return False, "New password must be at least 6 characters."

    now_ts = utc_now()

    def _op(cur):
        cur.execute(
            """
            SELECT id, username, reset_expires_at, is_active
            FROM users
            WHERE reset_token = %s
            LIMIT 1;
            """,
            (clean_token,),
        )
        user = cur.fetchone()
        if not user or not bool(user.get("is_active", False)):
            return False, "Invalid recovery token."

        expires_at = user.get("reset_expires_at")
        if expires_at is None:
            return False, "Invalid or already used recovery token."
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        if getattr(expires_at, "tzinfo", None) is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < now_ts:
            return False, "Recovery token has expired. Request a new one."

        cur.execute(
            """
            UPDATE users
            SET password_hash = %s, reset_token = NULL, reset_expires_at = NULL
            WHERE id = %s;
            """,
            (hash_password(new_password), user["id"]),
        )
        return True, f"Password reset successful for @{user['username']}. You can now sign in."

    try:
        ok, msg = run_write_tx(_op)
        if ok:
            log_audit("recovery", "PASSWORD_RESET_COMPLETED", "Password reset via recovery token")
        return ok, msg
    except Exception as exc:
        logger.exception("Password reset with token failed")
        return False, f"Password reset failed: {exc}"


def fetch_all_users() -> pd.DataFrame:
    try:
        return run_read_query(
            "SELECT id, username, full_name, role, is_active, created_at FROM users ORDER BY id;"
        )
    except Exception as exc:
        st.error(f"Failed to load users: {exc}")
        return pd.DataFrame()


def update_user_role_or_status(
    user_id: int, role: str, is_active: bool, actor_role: str, actor: str
) -> tuple[bool, str]:
    if actor_role != "admin":
        return False, "Only administrators can change user roles or status."
    if role not in ("staff", "admin"):
        return False, "Invalid role selection."

    def _op(cur):
        cur.execute(
            """
            UPDATE users
            SET role = %s, is_active = %s
            WHERE id = %s
            RETURNING username;
            """,
            (role, bool(is_active), int(user_id)),
        )
        return cur.fetchone()

    try:
        updated = run_write_tx(_op)
        if not updated:
            return False, "User not found."
        target_name = updated.get("username", "unknown")
        log_audit(actor, "USER_ROLE_UPDATED", f"Target @{target_name} -> role={role}, active={is_active}")
        return True, f"Updated @{target_name} (Role: {role}, Active: {is_active})."
    except Exception as exc:
        logger.exception("Failed to update user %s", user_id)
        return False, f"Failed to update user: {exc}"


# ==============================================================================
# 5. INVENTORY & TRANSACTION OPERATIONS
# ==============================================================================
def fetch_products() -> pd.DataFrame:
    try:
        df = run_read_query(
            """
            SELECT
                id,
                sku,
                name,
                category,
                unit_cost,
                selling_price,
                stock_qty,
                reorder_level,
                updated_at
            FROM products
            ORDER BY id DESC;
            """
        )
        if df.empty:
            return pd.DataFrame(
                columns=[
                    "id", "sku", "name", "category", "unit_cost", "selling_price",
                    "stock_qty", "reorder_level", "updated_at", "stock_value",
                    "retail_value", "status",
                ]
            )

        df["unit_cost"] = df["unit_cost"].apply(safe_float)
        df["selling_price"] = df["selling_price"].apply(safe_float)
        df["stock_qty"] = df["stock_qty"].apply(safe_int)
        df["reorder_level"] = df["reorder_level"].apply(safe_int)
        df["stock_value"] = df["stock_qty"] * df["unit_cost"]
        df["retail_value"] = df["stock_qty"] * df["selling_price"]
        df["status"] = df.apply(
            lambda r: "Out of Stock"
            if r["stock_qty"] <= 0
            else ("Low Stock" if r["stock_qty"] <= r["reorder_level"] else "Healthy"),
            axis=1,
        )
        return df
    except Exception as exc:
        st.error(f"Failed to load products: {exc}")
        return pd.DataFrame()


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
    clean_sku = (sku or "").strip().upper()
    clean_name = (name or "").strip()
    clean_cat = (category or "").strip() or "General"
    if not clean_sku or not clean_name:
        return False, "SKU and Product Name are required."

    c_cost = max(0.0, safe_float(unit_cost))
    s_price = max(0.0, safe_float(selling_price))
    s_qty = max(0, safe_int(stock_qty))
    r_lvl = max(0, safe_int(reorder_level))
    now_ts = utc_now()

    def _op(cur):
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
            (clean_sku, clean_name, clean_cat, c_cost, s_price, s_qty, r_lvl, now_ts),
        )
        saved_row = cur.fetchone()
        prod_id = saved_row["id"] if saved_row else None

        if s_qty > 0:
            cur.execute(
                """
                INSERT INTO transactions (tx_type, product_id, sku, product_name, quantity, unit_price, total_amount, recorded_by, notes, created_at)
                VALUES ('STOCK_IN', %s, %s, %s, %s, %s, %s, %s, 'Initial / catalog upsert', %s);
                """,
                (prod_id, clean_sku, clean_name, s_qty, c_cost, round(s_qty * c_cost, 2), actor, now_ts),
            )

    try:
        run_write_tx(_op)
        log_audit(actor, "PRODUCT_UPSERT", f"{clean_sku} ({clean_name}) qty={s_qty}")
        return True, f"Saved product {clean_sku} — {clean_name}."
    except Exception as exc:
        logger.exception("Failed to save product %s", clean_sku)
        return False, f"Failed to save product: {exc}"


def record_stock_movement(
    product_id: int,
    tx_type: str,
    quantity: int,
    custom_price: float | None,
    notes: str,
    actor: str,
) -> tuple[bool, str]:
    qty = safe_int(quantity)
    if qty <= 0:
        return False, "Quantity must be greater than zero."

    now_ts = utc_now()
    clean_notes = (notes or "").strip()

    def _op(cur):
        cur.execute("SELECT * FROM products WHERE id = %s FOR UPDATE;", (int(product_id),))
        prod = cur.fetchone()
        if not prod:
            return False, "Selected product was not found."

        current_qty = safe_int(prod.get("stock_qty"))
        if tx_type == "SALE":
            if current_qty < qty:
                return False, f"Insufficient stock. Available: {current_qty}, requested: {qty}."
            new_qty = current_qty - qty
            unit_price = safe_float(custom_price) if custom_price is not None else safe_float(prod.get("selling_price"))
        elif tx_type == "RESTOCK":
            new_qty = current_qty + qty
            unit_price = safe_float(custom_price) if custom_price is not None else safe_float(prod.get("unit_cost"))
        else:
            return False, "Unsupported movement type."

        total_amount = round(qty * unit_price, 2)

        cur.execute(
            """
            UPDATE products
            SET stock_qty = %s, updated_at = %s
            WHERE id = %s;
            """,
            (new_qty, now_ts, int(product_id)),
        )

        cur.execute(
            """
            INSERT INTO transactions (tx_type, product_id, sku, product_name, quantity, unit_price, total_amount, recorded_by, notes, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
            """,
            (
                tx_type,
                int(product_id),
                prod.get("sku", ""),
                prod.get("name", ""),
                qty,
                unit_price,
                total_amount,
                actor,
                clean_notes,
                now_ts,
            ),
        )
        return True, f"{tx_type} recorded: {qty} × {prod.get('name', '')} (Total: {total_amount:,.2f})."

    try:
        ok, msg = run_write_tx(_op)
        if ok:
            log_audit(actor, f"TX_{tx_type}", f"product_id={product_id} qty={qty}")
        return ok, msg
    except Exception as exc:
        logger.exception("Stock movement failed for product %s", product_id)
        return False, f"Transaction failed: {exc}"


def delete_product(product_id: int, actor_role: str, actor: str) -> tuple[bool, str]:
    if actor_role != "admin":
        return False, "Only administrators can delete products."

    def _op(cur):
        cur.execute("DELETE FROM products WHERE id = %s RETURNING sku, name;", (int(product_id),))
        return cur.fetchone()

    try:
        deleted = run_write_tx(_op)
        if not deleted:
            return False, "Product not found."
        log_audit(actor, "PRODUCT_DELETED", f"{deleted.get('sku')} ({deleted.get('name')})")
        return True, f"Deleted {deleted.get('sku')} — {deleted.get('name')}."
    except Exception as exc:
        logger.exception("Delete product %s failed", product_id)
        return False, f"Delete failed: {exc}"


def fetch_transactions(days: int = 90) -> pd.DataFrame:
    try:
        cutoff = utc_now() - timedelta(days=max(1, safe_int(days, 90)))
        df = run_read_query(
            """
            SELECT
                id,
                tx_type,
                sku,
                product_name,
                quantity,
                unit_price,
                total_amount,
                recorded_by,
                notes,
                created_at
            FROM transactions
            WHERE created_at >= %s
            ORDER BY created_at DESC, id DESC;
            """,
            (cutoff,),
        )
        if df.empty:
            return pd.DataFrame(
                columns=[
                    "id", "tx_type", "sku", "product_name", "quantity",
                    "unit_price", "total_amount", "recorded_by", "notes", "created_at",
                ]
            )
        df["quantity"] = df["quantity"].apply(safe_int)
        df["unit_price"] = df["unit_price"].apply(safe_float)
        df["total_amount"] = df["total_amount"].apply(safe_float)
        return df
    except Exception as exc:
        st.error(f"Failed to load transactions: {exc}")
        return pd.DataFrame()


def fetch_audit_logs(limit: int = 100) -> pd.DataFrame:
    try:
        return run_read_query(
            """
            SELECT id, username, action, details, created_at
            FROM audit_logs
            ORDER BY created_at DESC, id DESC
            LIMIT %s;
            """,
            (max(1, min(safe_int(limit, 100), 1000)),),
        )
    except Exception as exc:
        st.error(f"Failed to load audit logs: {exc}")
        return pd.DataFrame()


# ==============================================================================
# 6. AUTHENTICATION & PASSWORD RECOVERY UI
# ==============================================================================
def render_auth_screen():
    st.title("Enterprise AI Inventory & BI Command Centre")
    st.caption("Sign in, register a staff account, or recover a forgotten password.")

    _, col_center, _ = st.columns([1, 2, 1])
    with col_center:
        tab_login, tab_register, tab_recover = st.tabs(["Sign In", "Sign Up (Staff)", "Recover Password"])

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
                        log_audit((username or "").strip().lower() or "unknown", "LOGIN_FAILED", "Invalid credentials")
                        st.error("Invalid username or password, or account is inactive.")

        with tab_register:
            with st.form("register_form", clear_on_submit=True):
                full_name = st.text_input("Full Name", placeholder="e.g. Kwame Mensah")
                new_username = st.text_input("Username", placeholder="Lowercase, min 3 chars")
                new_password = st.text_input("Password", type="password", placeholder="Min 6 characters")
                confirm_password = st.text_input("Confirm Password", type="password")
                st.caption("New sign-ups default to `staff`. Admin privileges are assigned only by an administrator.")
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

        with tab_recover:
            st.markdown("#### Step 1: Request a Reset Token")
            with st.form("request_token_form", clear_on_submit=False):
                rec_username = st.text_input("Account Username", placeholder="Enter username to recover")
                req_token = st.form_submit_button("Generate Recovery Token", use_container_width=True)
                if req_token:
                    found, token = issue_password_reset_token(rec_username, ttl_minutes=30)
                    if found and token:
                        st.session_state["recovery_token_preview"] = token
                        st.success("Recovery token generated (valid for 30 minutes). Copy it below to reset the password:")
                        st.code(token, language="text")
                    else:
                        st.info("If an active account matches that username, a recovery token has been logged.")

            st.markdown("#### Step 2: Set a New Password With Token")
            default_tok = st.session_state.get("recovery_token_preview", "")
            with st.form("reset_with_token_form", clear_on_submit=True):
                token_input = st.text_input("Recovery Token", value=default_tok, placeholder="Paste your recovery token")
                rec_new_pw = st.text_input("New Password", type="password", placeholder="Min 6 characters")
                rec_confirm_pw = st.text_input("Confirm New Password", type="password")
                reset_btn = st.form_submit_button("Reset Password", type="primary", use_container_width=True)
                if reset_btn:
                    if rec_new_pw != rec_confirm_pw:
                        st.error("New passwords do not match.")
                    else:
                        ok, msg = reset_password_with_token(token_input, rec_new_pw)
                        if ok:
                            st.session_state.pop("recovery_token_preview", None)
                            st.success(msg)
                        else:
                            st.error(msg)


# ==============================================================================
# 7. MAIN APPLICATION MODULES & ROLE-LOCKED TABS
# ==============================================================================
def render_overview_tab(products_df: pd.DataFrame, tx_df: pd.DataFrame):
    st.subheader("Executive KPIs & Alerts")

    total_skus = len(products_df)
    total_units = safe_int(products_df["stock_qty"].sum()) if not products_df.empty and "stock_qty" in products_df else 0
    inventory_cost_val = safe_float(products_df["stock_value"].sum()) if not products_df.empty and "stock_value" in products_df else 0.0

    low_stock_df = pd.DataFrame()
    if not products_df.empty and "status" in products_df:
        low_stock_df = products_df[products_df["status"].isin(["Low Stock", "Out of Stock"])]

    sales_df = pd.DataFrame()
    if not tx_df.empty and "tx_type" in tx_df:
        sales_df = tx_df[tx_df["tx_type"] == "SALE"]

    total_sales_rev = safe_float(sales_df["total_amount"].sum()) if not sales_df.empty and "total_amount" in sales_df else 0.0

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Active SKUs", f"{total_skus:,}")
    c2.metric("Total Stock Units", f"{total_units:,}")
    c3.metric("Stock Valuation (Cost)", f"{inventory_cost_val:,.2f}")
    c4.metric("Recorded Sales Revenue", f"{total_sales_rev:,.2f}")
    c5.metric("Low / Out of Stock", f"{len(low_stock_df):,}")

    st.divider()

    if not low_stock_df.empty:
        st.warning("Attention: The following items are at or below their reorder threshold:")
        cols = [c for c in ["sku", "name", "category", "stock_qty", "reorder_level", "status"] if c in low_stock_df.columns]
        st.dataframe(low_stock_df[cols], use_container_width=True, hide_index=True)
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
            product_options = {}
            for _, row in products_df.iterrows():
                label = f"{row.get('sku', '')} — {row.get('name', '')} (Qty: {safe_int(row.get('stock_qty'))})"
                product_options[label] = safe_int(row.get("id"))

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
            if search_q and search_q.strip():
                q = search_q.strip().lower()
                mask = pd.Series(False, index=filtered.index)
                for col in ["sku", "name", "category"]:
                    if col in filtered.columns:
                        mask = mask | filtered[col].fillna("").astype(str).str.lower().str.contains(q, regex=False)
                filtered = filtered[mask]

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
                del_map = {
                    f"{r.get('sku', '')} — {r.get('name', '')}": safe_int(r.get("id"))
                    for _, r in products_df.iterrows()
                }
                if del_map:
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

    if not products_df.empty and {"category", "stock_value"}.issubset(products_df.columns):
        st.markdown("#### Stock Value by Category")
        agg_cols = [c for c in ["stock_qty", "stock_value", "retail_value"] if c in products_df.columns]
        cat_summary = (
            products_df.groupby("category", as_index=False)[agg_cols]
            .sum()
            .sort_values("stock_value", ascending=False)
        )
        hover_cols = [c for c in ["stock_qty", "retail_value"] if c in cat_summary.columns]
        fig_cat = px.bar(
            cat_summary,
            x="category",
            y="stock_value",
            hover_data=hover_cols,
            title="Inventory Cost Valuation by Category",
        )
        st.plotly_chart(fig_cat, use_container_width=True)

    if not tx_df.empty and {"tx_type", "created_at", "total_amount"}.issubset(tx_df.columns):
        sales_only = tx_df[tx_df["tx_type"] == "SALE"].copy()
        if not sales_only.empty:
            sales_only["date"] = pd.to_datetime(sales_only["created_at"], utc=True, errors="coerce").dt.date
            sales_only = sales_only.dropna(subset=["date"])
            if not sales_only.empty:
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

            group_cols = [c for c in ["sku", "product_name"] if c in sales_only.columns]
            metric_cols = [c for c in ["quantity", "total_amount"] if c in sales_only.columns]
            if group_cols and metric_cols:
                st.markdown("#### Top Selling Products by Revenue")
                top_prods = (
                    sales_only.groupby(group_cols, as_index=False)[metric_cols]
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
            if df_up.empty:
                st.warning("Uploaded CSV is empty.")
                return

            st.dataframe(df_up.head(20), use_container_width=True, hide_index=True)

            if st.button("Import Rows Into Neon", type="primary"):
                normalized_cols = {str(c).strip().lower() for c in df_up.columns}
                if not {"sku", "name"}.issubset(normalized_cols):
                    st.error("CSV must contain at least 'sku' and 'name' columns.")
                    return

                df_up.columns = [str(c).strip().lower() for c in df_up.columns]
                imported = 0
                errors = []
                for idx, row in df_up.iterrows():
                    ok, msg = add_or_update_product(
                        sku=str(row.get("sku", "") if pd.notna(row.get("sku")) else ""),
                        name=str(row.get("name", "") if pd.notna(row.get("name")) else ""),
                        category=str(row.get("category", "General")) if pd.notna(row.get("category")) else "General",
                        unit_cost=safe_float(row.get("unit_cost"), 0.0),
                        selling_price=safe_float(row.get("selling_price"), 0.0),
                        stock_qty=safe_int(row.get("stock_qty"), 0),
                        reorder_level=safe_int(row.get("reorder_level"), 10),
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
    if not users_df.empty:
        st.dataframe(users_df, use_container_width=True, hide_index=True)

        user_map = {
            f"@{r.get('username', '')} — {r.get('full_name', '')} ({r.get('role', 'staff')})": safe_int(r.get("id"))
            for _, r in users_df.iterrows()
        }
        if user_map:
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
    else:
        st.info("No users found.")

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
        st.markdown(f"### {user.get('full_name') or user.get('username')}")
        st.caption(f"@{user.get('username')} • Role: `{user.get('role', 'staff')}`")
        days_window = st.selectbox("Analytics History Window", [7, 30, 90, 365], index=2)

        with st.expander("Change Password"):
            with st.form("change_password_form", clear_on_submit=True):
                curr_pw = st.text_input("Current Password", type="password")
                next_pw = st.text_input("New Password", type="password", placeholder="Min 6 characters")
                confirm_next_pw = st.text_input("Confirm New Password", type="password")
                chg_btn = st.form_submit_button("Update Password", use_container_width=True)
                if chg_btn:
                    if next_pw != confirm_next_pw:
                        st.error("New passwords do not match.")
                    else:
                        ok, msg = change_user_password(user["username"], curr_pw, next_pw)
                        if ok:
                            st.success(msg)
                        else:
                            st.error(msg)

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
        if st.button("Retry Database Connection"):
            init_database_schema.clear()
            st.rerun()
        st.stop()

    auth_user = st.session_state.get("auth_user")
    if not isinstance(auth_user, dict) or not auth_user.get("username"):
        st.session_state.pop("auth_user", None)
        render_auth_screen()
    else:
        render_main_app(auth_user)


if __name__ == "__main__":
    main()
