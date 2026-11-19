import csv
import hashlib
import io
import logging
import math
import secrets
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import plotly.express as px
import psycopg2
from psycopg2 import InterfaceError, OperationalError, extras
import streamlit as st

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("businessFlow")

RECOVERY_TOKEN_TTL_MINUTES = 2
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "Admin@1234"
CSV_CHUNK_SIZE = 5000
DB_BATCH_PAGE_SIZE = 1000

DEVELOPER_COMPANY = "Roggers Enterprises Limited | Ghana"
DEVELOPER_EMAIL = "koffieroggers@gmail.com"
DEVELOPER_HANDLE = "ROG3RS"
DEVELOPER_GITHUB = "Roggers-dev"

st.set_page_config(
    page_title="Enterprise AI Inventory & BI Command Centre",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="auto",
)


# ==============================================================================
# 1. RESPONSIVE UI STYLING (ALL DEVICES: DESKTOP, TABLET, MOBILE)
# ==============================================================================
def inject_responsive_styles():
    st.markdown(
        """
        <style>
        .block-container {
            padding-top: 1.25rem;
            padding-bottom: 2rem;
            max-width: 100%;
        }
        div[data-testid="stMetric"] {
            border: 1px solid rgba(128, 128, 128, 0.22);
            border-radius: 10px;
            padding: 0.65rem 0.85rem;
        }
        div[data-testid="stDataFrame"] {
            width: 100%;
        }
        @media (max-width: 768px) {
            .block-container {
                padding-top: 0.8rem;
                padding-left: 0.65rem;
                padding-right: 0.65rem;
            }
            h1 {
                font-size: 1.45rem !important;
                line-height: 1.25 !important;
            }
            h2, h3 {
                font-size: 1.15rem !important;
            }
            div[data-testid="stMetricValue"] {
                font-size: 1.25rem !important;
            }
            button[kind="primary"], button[kind="secondary"], .stDownloadButton > button {
                width: 100% !important;
                min-height: 42px;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ==============================================================================
# 2. FAST DATABASE CONNECTION & QUERY ENGINE
# ==============================================================================
@st.cache_resource
def _get_normalized_neon_url() -> str:
    try:
        if "neon" in st.secrets and "database_url" in st.secrets["neon"]:
            url = str(st.secrets["neon"]["database_url"]).strip()
        elif "database_url" in st.secrets:
            url = str(st.secrets["database_url"]).strip()
        else:
            url = ""
    except Exception as exc:
        raise RuntimeError(f"Unable to read Streamlit secrets: {exc}") from exc

    if not url:
        raise RuntimeError(
            "Missing Neon connection string. Set [neon] database_url in Streamlit Secrets."
        )

    cleaned = url
    if "sslmode=" not in cleaned:
        sep = "&" if "?" in cleaned else "?"
        cleaned = f"{cleaned}{sep}sslmode=require"
    if "connect_timeout=" not in cleaned:
        sep = "&" if "?" in cleaned else "?"
        cleaned = f"{cleaned}{sep}connect_timeout=8"
    return cleaned


def open_connection(retries: int = 1):
    """Open a Postgres connection quickly with minimal retry overhead."""
    url = _get_normalized_neon_url()
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return psycopg2.connect(url)
        except (OperationalError, InterfaceError) as exc:
            last_exc = exc
            logger.warning("Postgres connection attempt %s failed: %s", attempt + 1, exc)
    raise last_exc  # type: ignore[misc]


def run_read_query(sql: str, params: tuple | list | None = None) -> pd.DataFrame:
    conn = None
    try:
        conn = open_connection(retries=1)
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


def _try_step(cur, sql: str, params: tuple | list | None = None, step_name: str = "step"):
    sp = "sp_schema_guard"
    cur.execute(f"SAVEPOINT {sp};")
    try:
        cur.execute(sql, params or ())
        cur.execute(f"RELEASE SAVEPOINT {sp};")
    except Exception as exc:
        cur.execute(f"ROLLBACK TO SAVEPOINT {sp};")
        logger.warning("Schema step '%s' skipped/adjusted: %s", step_name, exc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def safe_float(val, default: float = 0.0) -> float:
    try:
        if val is None or pd.isna(val):
            return default
        if isinstance(val, str):
            cleaned = (
                val.replace(",", "")
                .replace("GHS", "")
                .replace("₵", "")
                .replace("$", "")
                .strip()
            )
            if not cleaned:
                return default
            num = float(cleaned)
        else:
            num = float(val)
        return default if (math.isnan(num) or math.isinf(num)) else num
    except Exception:
        return default


def safe_int(val, default: int = 0) -> int:
    try:
        if val is None or pd.isna(val):
            return default
        if isinstance(val, str):
            cleaned = val.replace(",", "").strip()
            if not cleaned:
                return default
            num = float(cleaned)
        else:
            num = float(val)
        if math.isnan(num) or math.isinf(num):
            return default
        return int(num)
    except Exception:
        return default


def invalidate_data_caches():
    fetch_products_cached.clear()
    fetch_transactions_cached.clear()
    fetch_all_users_cached.clear()
    fetch_audit_logs_cached.clear()


# ==============================================================================
# 3. UNIVERSAL CSV & EXCEL (MULTI-LAYER / MULTI-COLUMN / CHUNKED) PARSER
# ==============================================================================
def _read_raw_bytes(uploaded_file) -> bytes:
    try:
        if hasattr(uploaded_file, "seek"):
            uploaded_file.seek(0)
        raw = uploaded_file.getvalue() if hasattr(uploaded_file, "getvalue") else uploaded_file.read()
        if isinstance(raw, str):
            return raw.encode("utf-8", errors="replace")
        return bytes(raw or b"")
    except Exception as exc:
        raise ValueError(f"Unable to read uploaded file: {exc}") from exc


def _decode_csv_sample(raw_bytes: bytes) -> tuple[str, str]:
    text = None
    used_enc = "utf-8"
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw_bytes.decode(enc)
            used_enc = enc
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw_bytes.decode("utf-8", errors="replace")

    sample_lines = "\n".join(text.splitlines()[:25])
    sep = ","
    try:
        dialect = csv.Sniffer().sniff(sample_lines, delimiters=[",", ";", "\t", "|"])
        if dialect and dialect.delimiter:
            sep = dialect.delimiter
    except Exception:
        counts = {d: sample_lines.count(d) for d in (",", ";", "\t", "|")}
        best_sep = max(counts, key=counts.get)
        if counts[best_sep] > 0:
            sep = best_sep
    return used_enc, sep


def _flatten_multi_layer_columns(df: pd.DataFrame) -> pd.DataFrame:
    seen: dict[str, int] = {}
    new_cols: list[str] = []

    for idx, col in enumerate(df.columns):
        if isinstance(col, tuple):
            parts = []
            for part in col:
                p_str = "" if part is None or pd.isna(part) else str(part).strip()
                if not p_str or p_str.lower().startswith("unnamed"):
                    continue
                if not parts or parts[-1].lower() != p_str.lower():
                    parts.append(p_str)
            base_name = " / ".join(parts) if parts else f"col_{idx + 1}"
        else:
            c_str = "" if col is None or pd.isna(col) else str(col).strip()
            base_name = c_str if (c_str and not c_str.lower().startswith("unnamed")) else f"col_{idx + 1}"

        key = base_name.lower()
        if key in seen:
            seen[key] += 1
            unique_name = f"{base_name}_{seen[key]}"
        else:
            seen[key] = 1
            unique_name = base_name
        new_cols.append(unique_name)

    df.columns = new_cols
    return df


def list_excel_sheets(uploaded_file) -> list[str]:
    raw_bytes = _read_raw_bytes(uploaded_file)
    try:
        xl = pd.ExcelFile(io.BytesIO(raw_bytes))
        return list(xl.sheet_names)
    except Exception as exc:
        raise ValueError(f"Could not inspect Excel workbook sheets: {exc}") from exc


def parse_tabular_chunks(
    uploaded_file,
    header_layers: int = 1,
    sheet_name: str | int | None = 0,
    chunksize: int = CSV_CHUNK_SIZE,
) -> tuple[list[pd.DataFrame], dict]:
    """
    Parse CSV or Excel (.xlsx / .xls) with 1–3 header layers and many columns
    into cleaned chunks.
    """
    raw_bytes = _read_raw_bytes(uploaded_file)
    if not raw_bytes:
        raise ValueError("Uploaded file is empty.")

    fname = str(getattr(uploaded_file, "name", "") or "").lower()
    is_excel = fname.endswith(".xlsx") or fname.endswith(".xls")
    layers = max(1, min(safe_int(header_layers, 1), 3))
    header_arg: int | list[int] = 0 if layers == 1 else list(range(layers))

    chunks: list[pd.DataFrame] = []
    total_rows = 0

    if is_excel:
        try:
            full_df = pd.read_excel(
                io.BytesIO(raw_bytes),
                sheet_name=0 if sheet_name is None else sheet_name,
                header=header_arg,
            )
            if isinstance(full_df, dict):
                full_df = next(iter(full_df.values()), pd.DataFrame())
            if full_df is None or full_df.empty:
                raise ValueError("Selected Excel sheet is empty.")
            full_df = _flatten_multi_layer_columns(full_df.copy()).dropna(how="all")
            full_df = full_df.replace([np.inf, -np.inf], np.nan)
            if full_df.empty:
                raise ValueError("No valid data rows found in Excel sheet.")

            step = max(500, safe_int(chunksize, CSV_CHUNK_SIZE))
            for start in range(0, len(full_df), step):
                part = full_df.iloc[start : start + step].copy()
                if not part.empty:
                    chunks.append(part)
                    total_rows += len(part)

            meta = {
                "format": "Excel",
                "sheet": sheet_name,
                "header_layers": layers,
                "total_rows": total_rows,
                "total_columns": len(chunks[0].columns),
                "chunk_count": len(chunks),
            }
            return chunks, meta
        except Exception as exc:
            raise ValueError(f"Failed parsing Excel file: {exc}") from exc

    # CSV path
    encoding, sep = _decode_csv_sample(raw_bytes)
    try:
        reader = pd.read_csv(
            io.BytesIO(raw_bytes),
            sep=sep,
            encoding=encoding,
            header=header_arg,
            chunksize=max(500, safe_int(chunksize, CSV_CHUNK_SIZE)),
            low_memory=False,
            on_bad_lines="skip",
        )
        for chunk in reader:
            if chunk is None or chunk.empty:
                continue
            cleaned = _flatten_multi_layer_columns(chunk.copy()).dropna(how="all")
            if cleaned.empty:
                continue
            cleaned = cleaned.replace([np.inf, -np.inf], np.nan)
            total_rows += len(cleaned)
            chunks.append(cleaned)
    except Exception as exc:
        raise ValueError(f"Failed parsing CSV (encoding={encoding}, sep='{sep}'): {exc}") from exc

    if not chunks:
        raise ValueError("No valid data rows found in CSV file.")

    meta = {
        "format": f"CSV ({encoding}, sep='{sep}')",
        "sheet": None,
        "header_layers": layers,
        "total_rows": total_rows,
        "total_columns": len(chunks[0].columns),
        "chunk_count": len(chunks),
    }
    return chunks, meta


def _resolve_mapped_column(columns: list[str], selected_col: str | None, aliases: tuple[str, ...]) -> str | None:
    if selected_col and selected_col != "<Auto / None>" and selected_col in columns:
        return selected_col
    norm_map = {str(c).strip().lower(): c for c in columns}
    for alias in aliases:
        if alias in norm_map:
            return norm_map[alias]
    for col in columns:
        c_low = str(col).strip().lower()
        for alias in aliases:
            if c_low.endswith(f" / {alias}") or c_low.endswith(f"_{alias}") or f" {alias}" in c_low:
                return col
    return None

# ==============================================================================
# 4. PASSWORD HASHING & AUTHENTICATION (FIXED PASSWORD PERSISTENCE)
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
# 5. SCHEMA INITIALIZATION (RUNS ONCE PER PROCESS) & AUDIT LOGGING
# ==============================================================================
def _get_table_columns(cur, table_name: str) -> set[str]:
    try:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s;
            """,
            (table_name,),
        )
        return {str(r["column_name"]) for r in cur.fetchall() if r.get("column_name")}
    except Exception as exc:
        logger.warning("Could not inspect %s columns: %s", table_name, exc)
        return set()


def _relax_extra_not_null_columns(cur, table_name: str, managed_cols: set[str]):
    try:
        cur.execute(
            """
            SELECT column_name, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s;
            """,
            (table_name,),
        )
        for col in cur.fetchall():
            col_name = str(col.get("column_name") or "")
            if not col_name or col_name in managed_cols:
                continue
            if str(col.get("is_nullable") or "").upper() == "NO" and col.get("column_default") is None:
                _try_step(
                    cur,
                    f'ALTER TABLE "{table_name}" ALTER COLUMN "{col_name}" DROP NOT NULL;',
                    step_name=f"drop_not_null_{table_name}_{col_name}",
                )
    except Exception as exc:
        logger.warning("Could not relax extra columns on %s: %s", table_name, exc)


def _heal_users_table(cur):
    _try_step(
        cur,
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
        """,
        step_name="create_users",
    )

    existing_cols = _get_table_columns(cur, "users")
    if "username" not in existing_cols:
        _try_step(cur, "ALTER TABLE users ADD COLUMN IF NOT EXISTS username VARCHAR(100);", step_name="add_username")
        if "id" in existing_cols:
            _try_step(
                cur,
                "UPDATE users SET username = 'user_' || id::text WHERE username IS NULL OR TRIM(username) = '';",
                step_name="backfill_username_from_id",
            )

    for ddl, label in [
        ("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT;", "add_password_hash"),
        ("ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name VARCHAR(150) NOT NULL DEFAULT 'User';", "add_full_name"),
        ("ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(30) NOT NULL DEFAULT 'staff';", "add_role"),
        ("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;", "add_is_active"),
        ("ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token TEXT;", "add_reset_token"),
        ("ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_expires_at TIMESTAMPTZ;", "add_reset_expires_at"),
        ("ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;", "add_created_at"),
    ]:
        _try_step(cur, ddl, step_name=label)

    _relax_extra_not_null_columns(
        cur,
        "users",
        {"id", "username", "full_name", "role", "password_hash", "is_active", "reset_token", "reset_expires_at", "created_at"},
    )
    _try_step(cur, "UPDATE users SET role = 'staff' WHERE role IS NULL OR TRIM(role) = '';", step_name="sync_role")
    _try_step(cur, "UPDATE users SET is_active = TRUE WHERE is_active IS NULL;", step_name="sync_is_active")

    # Only seed default admin if no admin user exists at all
    _try_step(
        cur,
        """
        INSERT INTO users (username, full_name, role, password_hash, is_active)
        SELECT 'admin', 'System Administrator', 'admin', %s, TRUE
        WHERE NOT EXISTS (
            SELECT 1 FROM users WHERE LOWER(TRIM(username)) = 'admin'
        );
        """,
        (hash_password(DEFAULT_ADMIN_PASSWORD),),
        step_name="seed_default_admin_if_missing",
    )


def _heal_products_table(cur):
    _try_step(
        cur,
        """
        CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            sku VARCHAR(120) UNIQUE NOT NULL,
            name VARCHAR(250) NOT NULL,
            category VARCHAR(120) NOT NULL DEFAULT 'General',
            unit_cost NUMERIC(14, 2) NOT NULL DEFAULT 0.00,
            selling_price NUMERIC(14, 2) NOT NULL DEFAULT 0.00,
            stock_qty INTEGER NOT NULL DEFAULT 0,
            reorder_level INTEGER NOT NULL DEFAULT 10,
            updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        );
        """,
        step_name="create_products",
    )
    for ddl, label in [
        ("ALTER TABLE products ADD COLUMN IF NOT EXISTS sku VARCHAR(120);", "prod_add_sku"),
        ("ALTER TABLE products ADD COLUMN IF NOT EXISTS name VARCHAR(250) NOT NULL DEFAULT 'Unnamed Item';", "prod_add_name"),
        ("ALTER TABLE products ADD COLUMN IF NOT EXISTS category VARCHAR(120) NOT NULL DEFAULT 'General';", "prod_add_cat"),
        ("ALTER TABLE products ADD COLUMN IF NOT EXISTS unit_cost NUMERIC(14, 2) NOT NULL DEFAULT 0.00;", "prod_add_cost"),
        ("ALTER TABLE products ADD COLUMN IF NOT EXISTS selling_price NUMERIC(14, 2) NOT NULL DEFAULT 0.00;", "prod_add_price"),
        ("ALTER TABLE products ADD COLUMN IF NOT EXISTS stock_qty INTEGER NOT NULL DEFAULT 0;", "prod_add_qty"),
        ("ALTER TABLE products ADD COLUMN IF NOT EXISTS reorder_level INTEGER NOT NULL DEFAULT 10;", "prod_add_reorder"),
        ("ALTER TABLE products ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;", "prod_add_updated"),
    ]:
        _try_step(cur, ddl, step_name=label)

    _relax_extra_not_null_columns(
        cur,
        "products",
        {"id", "sku", "name", "category", "unit_cost", "selling_price", "stock_qty", "reorder_level", "updated_at"},
    )


def _heal_transactions_table(cur):
    _try_step(
        cur,
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            tx_type VARCHAR(20) NOT NULL DEFAULT 'SALE',
            product_id INTEGER,
            sku VARCHAR(120) NOT NULL DEFAULT '',
            product_name VARCHAR(250) NOT NULL DEFAULT '',
            quantity INTEGER NOT NULL DEFAULT 0,
            unit_price NUMERIC(14, 2) NOT NULL DEFAULT 0.00,
            total_amount NUMERIC(14, 2) NOT NULL DEFAULT 0.00,
            recorded_by VARCHAR(100) NOT NULL DEFAULT 'system',
            notes TEXT,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        );
        """,
        step_name="create_transactions",
    )
    for ddl, label in [
        ("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS tx_type VARCHAR(20) NOT NULL DEFAULT 'SALE';", "tx_add_tx_type"),
        ("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS product_id INTEGER;", "tx_add_product_id"),
        ("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS sku VARCHAR(120) NOT NULL DEFAULT '';", "tx_add_sku"),
        ("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS product_name VARCHAR(250) NOT NULL DEFAULT '';", "tx_add_prod_name"),
        ("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS quantity INTEGER NOT NULL DEFAULT 0;", "tx_add_qty"),
        ("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS unit_price NUMERIC(14, 2) NOT NULL DEFAULT 0.00;", "tx_add_unit_price"),
        ("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS total_amount NUMERIC(14, 2) NOT NULL DEFAULT 0.00;", "tx_add_total"),
        ("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS recorded_by VARCHAR(100) NOT NULL DEFAULT 'system';", "tx_add_recorded_by"),
        ("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS notes TEXT;", "tx_add_notes"),
        ("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;", "tx_add_created_at"),
    ]:
        _try_step(cur, ddl, step_name=label)

    _relax_extra_not_null_columns(
        cur,
        "transactions",
        {"id", "tx_type", "product_id", "sku", "product_name", "quantity", "unit_price", "total_amount", "recorded_by", "notes", "created_at"},
    )


def _heal_audit_logs_table(cur):
    _try_step(
        cur,
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id SERIAL PRIMARY KEY,
            username VARCHAR(100) NOT NULL DEFAULT 'system',
            action VARCHAR(120) NOT NULL DEFAULT 'EVENT',
            details TEXT,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        );
        """,
        step_name="create_audit_logs",
    )
    for ddl, label in [
        ("ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS username VARCHAR(100) NOT NULL DEFAULT 'system';", "audit_add_user"),
        ("ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS action VARCHAR(120) NOT NULL DEFAULT 'EVENT';", "audit_add_action"),
        ("ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS details TEXT;", "audit_add_details"),
        ("ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;", "audit_add_created"),
    ]:
        _try_step(cur, ddl, step_name=label)
    _relax_extra_not_null_columns(cur, "audit_logs", {"id", "username", "action", "details", "created_at"})


def _apply_schema_and_seed(cur):
    _heal_users_table(cur)
    _heal_products_table(cur)
    _heal_transactions_table(cur)
    _heal_audit_logs_table(cur)

    cur.execute("SELECT COUNT(*) AS cnt FROM products;")
    prow = cur.fetchone()
    if safe_int(prow["cnt"] if prow else 0) == 0:
        starter_products = [
            ("SKU-1001", "Wireless Barcode Scanner", "Hardware", 180.00, 260.00, 24, 8),
            ("SKU-1002", "Thermal Receipt Paper (Roll)", "Supplies", 6.50, 12.00, 120, 30),
            ("SKU-1003", "POS Cash Drawer", "Hardware", 240.00, 340.00, 6, 5),
            ("SKU-1004", "Inventory Label Stickers", "Supplies", 9.00, 16.50, 14, 15),
        ]
        _try_step(cur, "SAVEPOINT sp_seed_prods;", step_name="sp_seed_prods_init")
        try:
            extras.execute_values(
                cur,
                """
                INSERT INTO products (sku, name, category, unit_cost, selling_price, stock_qty, reorder_level)
                VALUES %s
                ON CONFLICT (sku) DO NOTHING;
                """,
                starter_products,
                page_size=100,
            )
            cur.execute("RELEASE SAVEPOINT sp_seed_prods;")
        except Exception as exc:
            cur.execute("ROLLBACK TO SAVEPOINT sp_seed_prods;")
            logger.warning("Seed products skipped: %s", exc)


@st.cache_resource(show_spinner=False)
def init_database_schema() -> bool:
    try:
        run_write_tx(_apply_schema_and_seed)
        return True
    except Exception:
        init_database_schema.clear()
        raise


def log_audit(username: str, action: str, details: str = ""):
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
        fetch_audit_logs_cached.clear()
    except Exception as exc:
        logger.warning("Audit log write skipped: %s", exc)

# ==============================================================================
# 6. USER ACCOUNT, CHANGE PASSWORD & RECOVERY OPERATIONS
# ==============================================================================
def register_staff_user(username: str, full_name: str, password: str) -> tuple[bool, str]:
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
        fetch_all_users_cached.clear()
        log_audit(clean_user, "STAFF_SIGNUP", "Self-registered with role 'staff'")
        return True, f"Staff account created for @{clean_user}. You can sign in now."
    except Exception as exc:
        logger.exception("Error registering user %s", clean_user)
        return False, f"Error creating account: {exc}"


def _ensure_missing_default_admin(password: str) -> dict:
    """Create default admin ONLY when no admin record exists or its hash is empty/invalid."""
    fresh_hash = hash_password(password)
    now_ts = utc_now()

    def _op(cur):
        cur.execute(
            "SELECT id, username, full_name, role, password_hash FROM users WHERE LOWER(TRIM(username)) = %s LIMIT 1;",
            (DEFAULT_ADMIN_USERNAME,),
        )
        existing = cur.fetchone()
        if existing:
            cur.execute(
                "UPDATE users SET is_active = TRUE, role = COALESCE(NULLIF(role, ''), 'admin') WHERE id = %s RETURNING id, username, full_name, role;",
                (existing["id"],),
            )
            upd = cur.fetchone()
            return dict(upd) if upd else dict(existing)

        cur.execute(
            """
            INSERT INTO users (username, full_name, role, password_hash, is_active, created_at)
            VALUES (%s, 'System Administrator', 'admin', %s, TRUE, %s)
            RETURNING id, username, full_name, role;
            """,
            (DEFAULT_ADMIN_USERNAME, fresh_hash, now_ts),
        )
        inserted = cur.fetchone()
        return dict(inserted) if inserted else {
            "id": 1,
            "username": DEFAULT_ADMIN_USERNAME,
            "full_name": "System Administrator",
            "role": "admin",
        }

    saved = run_write_tx(_op)
    fetch_all_users_cached.clear()
    return {
        "id": safe_int(saved.get("id"), 1),
        "username": str(saved.get("username") or DEFAULT_ADMIN_USERNAME),
        "full_name": str(saved.get("full_name") or "System Administrator"),
        "role": str(saved.get("role") or "admin"),
    }


def authenticate_user(username: str, password: str) -> dict | None:
    clean_user = (username or "").strip().lower()
    raw_password = password or ""
    if not clean_user or not raw_password:
        return None

    try:
        df = run_read_query(
            """
            SELECT id, username, full_name, role, password_hash, is_active
            FROM users
            WHERE LOWER(TRIM(username)) = %s
            LIMIT 1;
            """,
            (clean_user,),
        )

        # Only bootstrap default admin when no admin row exists yet
        if clean_user == DEFAULT_ADMIN_USERNAME and df.empty and raw_password == DEFAULT_ADMIN_PASSWORD:
            boot = _ensure_missing_default_admin(raw_password)
            return boot

        if df.empty:
            return None

        rec = df.iloc[0].to_dict()
        if not bool(rec.get("is_active", False)):
            return None

        stored = rec.get("password_hash")
        if verify_password(raw_password, stored):
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
            "SELECT id, password_hash, is_active FROM users WHERE LOWER(TRIM(username)) = %s LIMIT 1;",
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
        fetch_all_users_cached.clear()
        log_audit(clean_user, "PASSWORD_CHANGED", "Updated password via account settings")
        return True, "Password changed successfully. Use your new password next time you sign in."
    except Exception as exc:
        logger.exception("Password change failed for %s", clean_user)
        return False, f"Failed to change password: {exc}"


def issue_password_reset_token(
    username: str, ttl_minutes: int = RECOVERY_TOKEN_TTL_MINUTES
) -> tuple[bool, str | None]:
    clean_user = (username or "").strip().lower()
    if not clean_user:
        return False, None

    ttl = max(1, safe_int(ttl_minutes, RECOVERY_TOKEN_TTL_MINUTES))
    token = secrets.token_urlsafe(24)
    expires_at = utc_now() + timedelta(minutes=ttl)

    def _op(cur):
        cur.execute(
            """
            UPDATE users
            SET reset_token = %s, reset_expires_at = %s
            WHERE LOWER(TRIM(username)) = %s AND is_active = TRUE
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
        log_audit(clean_user, "PASSWORD_RESET_TOKEN_ISSUED", f"Expires in {ttl} minutes")
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
            fetch_all_users_cached.clear()
            log_audit("recovery", "PASSWORD_RESET_COMPLETED", "Password reset via recovery token")
        return ok, msg
    except Exception as exc:
        logger.exception("Password reset with token failed")
        return False, f"Password reset failed: {exc}"


def admin_reset_user_password(
    target_user_id: int, new_password: str, actor_role: str, actor: str
) -> tuple[bool, str]:
    if actor_role != "admin":
        return False, "Only administrators can reset user passwords."
    if len(new_password or "") < 6:
        return False, "New password must be at least 6 characters."

    new_hash = hash_password(new_password)

    def _op(cur):
        cur.execute(
            """
            UPDATE users
            SET password_hash = %s, reset_token = NULL, reset_expires_at = NULL
            WHERE id = %s
            RETURNING username;
            """,
            (new_hash, int(target_user_id)),
        )
        return cur.fetchone()

    try:
        updated = run_write_tx(_op)
        if not updated:
            return False, "User not found."
        fetch_all_users_cached.clear()
        target_name = updated.get("username", "unknown")
        log_audit(actor, "ADMIN_PASSWORD_RESET", f"Reset password for @{target_name}")
        return True, f"Password reset for @{target_name}."
    except Exception as exc:
        logger.exception("Admin password reset failed for user %s", target_user_id)
        return False, f"Failed to reset password: {exc}"


@st.cache_data(ttl=15, show_spinner=False)
def fetch_all_users_cached() -> pd.DataFrame:
    return run_read_query(
        "SELECT id, username, full_name, role, is_active, created_at FROM users ORDER BY id;"
    )


def fetch_all_users() -> pd.DataFrame:
    try:
        return fetch_all_users_cached()
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
        fetch_all_users_cached.clear()
        target_name = updated.get("username", "unknown")
        log_audit(actor, "USER_ROLE_UPDATED", f"Target @{target_name} -> role={role}, active={is_active}")
        return True, f"Updated @{target_name} (Role: {role}, Active: {is_active})."
    except Exception as exc:
        logger.exception("Failed to update user %s", user_id)
        return False, f"Failed to update user: {exc}"


# ==============================================================================
# 7. CACHED READS & BATCH WRITE OPERATIONS
# ==============================================================================
@st.cache_data(ttl=15, show_spinner=False)
def fetch_products_cached() -> pd.DataFrame:
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
    df["status"] = np.where(
        df["stock_qty"] <= 0,
        "Out of Stock",
        np.where(df["stock_qty"] <= df["reorder_level"], "Low Stock", "Healthy"),
    )
    return df


def fetch_products() -> pd.DataFrame:
    try:
        return fetch_products_cached()
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
    clean_sku = (sku or "").strip().upper()[:120]
    clean_name = (name or "").strip()[:250]
    clean_cat = ((category or "").strip() or "General")[:120]
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
        invalidate_data_caches()
        log_audit(actor, "PRODUCT_UPSERT", f"{clean_sku} ({clean_name}) qty={s_qty}")
        return True, f"Saved product {clean_sku} — {clean_name}."
    except Exception as exc:
        logger.exception("Failed to save product %s", clean_sku)
        return False, f"Failed to save product: {exc}"


def bulk_upsert_products_batch(records: list[dict], actor: str) -> tuple[int, list[str]]:
    if not records:
        return 0, ["No valid product rows to import."]

    now_ts = utc_now()
    deduped: dict[str, tuple] = {}
    errors: list[str] = []

    for i, rec in enumerate(records):
        sku = str(rec.get("sku") or "").strip().upper()[:120]
        name = str(rec.get("name") or "").strip()[:250]
        if not sku or not name:
            errors.append(f"Record {i + 1}: Missing SKU or Name.")
            continue
        cat = (str(rec.get("category") or "General").strip() or "General")[:120]
        cost = max(0.0, safe_float(rec.get("unit_cost"), 0.0))
        price = max(0.0, safe_float(rec.get("selling_price"), 0.0))
        qty = max(0, safe_int(rec.get("stock_qty"), 0))
        reorder = max(0, safe_int(rec.get("reorder_level"), 10))
        deduped[sku] = (sku, name, cat, cost, price, qty, reorder, now_ts)

    if not deduped:
        return 0, errors or ["No valid rows after validation."]

    rows_list = list(deduped.values())

    def _op(cur):
        extras.execute_values(
            cur,
            """
            INSERT INTO products (sku, name, category, unit_cost, selling_price, stock_qty, reorder_level, updated_at)
            VALUES %s
            ON CONFLICT (sku) DO UPDATE SET
                name = EXCLUDED.name,
                category = EXCLUDED.category,
                unit_cost = EXCLUDED.unit_cost,
                selling_price = EXCLUDED.selling_price,
                stock_qty = EXCLUDED.stock_qty,
                reorder_level = EXCLUDED.reorder_level,
                updated_at = EXCLUDED.updated_at
            RETURNING id, sku, name, stock_qty, unit_cost;
            """,
            rows_list,
            page_size=DB_BATCH_PAGE_SIZE,
        )
        upserted = cur.fetchall() or []
        tx_rows = [
            (
                "STOCK_IN",
                safe_int(r.get("id")),
                str(r.get("sku") or ""),
                str(r.get("name") or ""),
                safe_int(r.get("stock_qty")),
                safe_float(r.get("unit_cost")),
                round(safe_int(r.get("stock_qty")) * safe_float(r.get("unit_cost")), 2),
                actor,
                "Bulk catalog import",
                now_ts,
            )
            for r in upserted
            if safe_int(r.get("stock_qty")) > 0
        ]
        if tx_rows:
            extras.execute_values(
                cur,
                """
                INSERT INTO transactions (tx_type, product_id, sku, product_name, quantity, unit_price, total_amount, recorded_by, notes, created_at)
                VALUES %s;
                """,
                tx_rows,
                page_size=DB_BATCH_PAGE_SIZE,
            )
        return len(upserted)

    try:
        imported_count = run_write_tx(_op)
        invalidate_data_caches()
        log_audit(actor, "BULK_CATALOG_IMPORT", f"Batch imported/updated {imported_count} SKUs")
        return safe_int(imported_count), errors
    except Exception as exc:
        logger.exception("Batch product import failed")
        return 0, [f"Batch import error: {exc}"]


def record_stock_movement(
    product_id: int | None,
    tx_type: str,
    quantity: int,
    custom_price: float | None,
    notes: str,
    actor: str,
    sku_fallback: str = "",
) -> tuple[bool, str]:
    qty = safe_int(quantity)
    if qty <= 0:
        return False, "Quantity must be greater than zero."

    now_ts = utc_now()
    clean_notes = (notes or "").strip()[:500]
    clean_sku = (sku_fallback or "").strip().upper()[:120]

    def _op(cur):
        if product_id is not None:
            cur.execute("SELECT * FROM products WHERE id = %s FOR UPDATE;", (int(product_id),))
        else:
            cur.execute("SELECT * FROM products WHERE UPPER(TRIM(sku)) = %s FOR UPDATE;", (clean_sku,))
        prod = cur.fetchone()
        if not prod:
            return False, f"Product '{clean_sku or product_id}' was not found in stock."

        pid = safe_int(prod.get("id"))
        current_qty = safe_int(prod.get("stock_qty"))
        if tx_type == "SALE":
            if current_qty < qty:
                return False, f"Insufficient stock for {prod.get('sku', '')}. Available: {current_qty}, requested: {qty}."
            new_qty = current_qty - qty
            unit_price = safe_float(custom_price) if custom_price is not None else safe_float(prod.get("selling_price"))
        elif tx_type == "RESTOCK":
            new_qty = current_qty + qty
            unit_price = safe_float(custom_price) if custom_price is not None else safe_float(prod.get("unit_cost"))
        else:
            return False, "Unsupported movement type."

        total_amount = round(qty * unit_price, 2)

        cur.execute(
            "UPDATE products SET stock_qty = %s, updated_at = %s WHERE id = %s;",
            (new_qty, now_ts, pid),
        )
        cur.execute(
            """
            INSERT INTO transactions (tx_type, product_id, sku, product_name, quantity, unit_price, total_amount, recorded_by, notes, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
            """,
            (
                tx_type,
                pid,
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
            invalidate_data_caches()
            log_audit(actor, f"TX_{tx_type}", f"product_id={product_id} sku={clean_sku} qty={qty}")
        return ok, msg
    except Exception as exc:
        logger.exception("Stock movement failed for product %s", product_id)
        return False, f"Transaction failed: {exc}"


def bulk_sales_against_stock_batch(sale_rows: list[dict], actor: str) -> tuple[int, list[str]]:
    if not sale_rows:
        return 0, ["No sale rows provided."]

    now_ts = utc_now()
    aggregated_qty: dict[str, int] = {}
    events: list[tuple[str, int, float | None, str]] = []
    errors: list[str] = []

    for idx, r in enumerate(sale_rows):
        sku = str(r.get("sku") or "").strip().upper()[:120]
        qty = safe_int(r.get("quantity"), 0)
        if not sku:
            errors.append(f"Row {idx + 1}: Missing SKU.")
            continue
        if qty <= 0:
            errors.append(f"Row {idx + 1} ({sku}): Quantity must be > 0.")
            continue

        raw_price = r.get("unit_price")
        u_price = safe_float(raw_price, -1.0) if (raw_price is not None and not pd.isna(raw_price)) else -1.0
        custom_p = u_price if u_price >= 0 else None
        note = (str(r.get("notes") or "").strip() or "Bulk sales against stock")[:500]

        aggregated_qty[sku] = aggregated_qty.get(sku, 0) + qty
        events.append((sku, qty, custom_p, note))

    if not events:
        return 0, errors

    def _op(cur):
        skus_needed = list(aggregated_qty.keys())
        cur.execute(
            """
            SELECT id, sku, name, stock_qty, selling_price
            FROM products
            WHERE UPPER(TRIM(sku)) = ANY(%s)
            FOR UPDATE;
            """,
            (skus_needed,),
        )
        stock_map: dict[str, dict] = {}
        for p in cur.fetchall() or []:
            stock_map[str(p.get("sku") or "").strip().upper()] = dict(p)

        blocked_skus: set[str] = set()
        for sku, req_qty in aggregated_qty.items():
            prod = stock_map.get(sku)
            if not prod:
                errors.append(f"SKU '{sku}' not found in products table.")
                blocked_skus.add(sku)
            else:
                avail = safe_int(prod.get("stock_qty"))
                if avail < req_qty:
                    errors.append(
                        f"SKU '{sku}' ({prod.get('name', '')}): Insufficient stock ({avail} available, {req_qty} requested)."
                    )
                    blocked_skus.add(sku)

        running_stock = {
            k: safe_int(v.get("stock_qty")) for k, v in stock_map.items() if k not in blocked_skus
        }
        tx_inserts: list[tuple] = []
        valid_count = 0

        for sku, qty, custom_p, note in events:
            if sku in blocked_skus:
                continue
            prod = stock_map[sku]
            unit_p = custom_p if custom_p is not None else safe_float(prod.get("selling_price"))
            tot_amt = round(qty * unit_p, 2)
            running_stock[sku] = running_stock[sku] - qty
            tx_inserts.append(
                (
                    "SALE",
                    safe_int(prod.get("id")),
                    sku,
                    str(prod.get("name") or ""),
                    qty,
                    unit_p,
                    tot_amt,
                    actor,
                    note,
                    now_ts,
                )
            )
            valid_count += 1

        if running_stock:
            update_tuples = [(stock_map[sku]["id"], new_q, now_ts) for sku, new_q in running_stock.items()]
            extras.execute_values(
                cur,
                """
                UPDATE products AS p
                SET stock_qty = v.new_qty,
                    updated_at = v.upd_at
                FROM (VALUES %s) AS v(pid, new_qty, upd_at)
                WHERE p.id = v.pid::integer;
                """,
                update_tuples,
                page_size=DB_BATCH_PAGE_SIZE,
            )

        if tx_inserts:
            extras.execute_values(
                cur,
                """
                INSERT INTO transactions (tx_type, product_id, sku, product_name, quantity, unit_price, total_amount, recorded_by, notes, created_at)
                VALUES %s;
                """,
                tx_inserts,
                page_size=DB_BATCH_PAGE_SIZE,
            )

        return valid_count

    try:
        processed = run_write_tx(_op)
        if processed > 0:
            invalidate_data_caches()
            log_audit(actor, "BULK_SALES_AGAINST_STOCK", f"Processed {processed} sale rows against stock")
        return safe_int(processed), errors
    except Exception as exc:
        logger.exception("Batch sales against stock failed")
        return 0, [f"Batch sale transaction failed: {exc}"]


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
        invalidate_data_caches()
        log_audit(actor, "PRODUCT_DELETED", f"{deleted.get('sku')} ({deleted.get('name')})")
        return True, f"Deleted {deleted.get('sku')} — {deleted.get('name')}."
    except Exception as exc:
        logger.exception("Delete product %s failed", product_id)
        return False, f"Delete failed: {exc}"


@st.cache_data(ttl=15, show_spinner=False)
def fetch_transactions_cached(days: int) -> pd.DataFrame:
    empty_cols = [
        "id", "tx_type", "sku", "product_name", "quantity",
        "unit_price", "total_amount", "recorded_by", "notes", "created_at",
    ]
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
        return pd.DataFrame(columns=empty_cols)
    df["quantity"] = df["quantity"].apply(safe_int)
    df["unit_price"] = df["unit_price"].apply(safe_float)
    df["total_amount"] = df["total_amount"].apply(safe_float)
    return df
def fetch_transactions(days: int = 90) -> pd.DataFrame:
        empty_cols = [
            "id",
            "tx_type",
            "sku",
            "product_name",
            "quantity",
            "unit_price",
            "total_amount",
            "recorded_by",
            "notes",
            "created_at",
        ]
        try:
            return fetch_transactions_cached(max(1, safe_int(days, 90)))
        except Exception as exc:
            st.error(f"Failed to load transactions: {exc}")
            return pd.DataFrame(columns=empty_cols)
@st.cache_data(ttl=15, show_spinner=False)
def fetch_audit_logs_cached(limit: int) -> pd.DataFrame:
    return run_read_query(
        """
        SELECT id, username, action, details, created_at
        FROM audit_logs
        ORDER BY created_at DESC, id DESC
        LIMIT %s;
        """,
        (max(1, min(safe_int(limit, 100), 1000)),),
    )


def fetch_audit_logs(limit: int = 100) -> pd.DataFrame:
    try:
        return fetch_audit_logs_cached(max(1, min(safe_int(limit, 100), 1000)))
    except Exception as exc:
        st.error(f"Failed to load audit logs: {exc}")
        return pd.DataFrame()


# ==============================================================================
# 8. DEVELOPER DETAILS & DAILY LOGIN WELCOME HELPERS
# ==============================================================================
def _copyright_span() -> str:
    curr_year = date.today().year
    return "2025" if curr_year <= 2025 else f"2025–{curr_year}"


def render_developer_footer():
    st.divider()
    st.caption(
        f"© {_copyright_span()} **{DEVELOPER_COMPANY}** • "
        f"Contact: `{DEVELOPER_EMAIL}` • Dev: **{DEVELOPER_HANDLE}** (`@{DEVELOPER_GITHUB}`) • "
        "All rights reserved."
    )


def maybe_show_daily_welcome(user: dict):
    today_str = date.today().isoformat()
    uname = str(user.get("username") or "user").lower()
    seen_key = f"daily_welcome_shown_{uname}"
    if st.session_state.get(seen_key) != today_str:
        st.session_state[seen_key] = today_str
        display_name = user.get("full_name") or user.get("username") or "Team Member"
        role_label = str(user.get("role") or "staff").capitalize()
        st.success(
            f"Welcome back, **{display_name}** (`@{uname}` • {role_label})! "
            f"Wishing you a smooth and productive day on {date.today().strftime('%A, %B %d, %Y')}."
        )


# ==============================================================================
# 9. AUTHENTICATION & PASSWORD RECOVERY UI
# ==============================================================================
def render_auth_screen():
    st.title("Enterprise AI Inventory & BI Command Centre")
    st.caption(
        f"{DEVELOPER_COMPANY} • {DEVELOPER_EMAIL} • "
        "Sign in, register a staff account, or recover a forgotten password."
    )

    _, col_center, _ = st.columns([0.2, 2.6, 0.2])
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
                    found, token = issue_password_reset_token(rec_username, ttl_minutes=RECOVERY_TOKEN_TTL_MINUTES)
                    if found and token:
                        st.session_state["recovery_token_preview"] = token
                        unit_label = "minute" if RECOVERY_TOKEN_TTL_MINUTES == 1 else "minutes"
                        st.success(
                            f"Recovery token generated (valid for {RECOVERY_TOKEN_TTL_MINUTES} {unit_label}). "
                            "Copy it below to reset the password:"
                        )
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

        render_developer_footer()


# ==============================================================================
# 10. MAIN APPLICATION MODULES & ROLE-LOCKED TABS
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
        st.dataframe(tx_df.head(25), use_container_width=True, hide_index=True)
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
                f"{row.get('sku', '')} — {row.get('name', '')} (Qty: {safe_int(row.get('stock_qty'))})": safe_int(row.get("id"))
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
            st.info("Record SALE movements or import Sales Against Stock files to populate sales charts.")
    else:
        st.info("No transaction data available for analytics yet.")


def _sheet_selector_if_excel(uploaded_file, key_prefix: str) -> str | int:
    fname = str(getattr(uploaded_file, "name", "") or "").lower()
    if fname.endswith(".xlsx") or fname.endswith(".xls"):
        try:
            sheets = list_excel_sheets(uploaded_file)
            if sheets:
                return st.selectbox("Excel Sheet", sheets, index=0, key=f"{key_prefix}_sheet")
        except Exception:
            pass
    return 0


def render_bulk_import_tab(user: dict):
    st.subheader("Heavy-Duty Upload Hub: Catalog, Sales Against Stock & Any CSV / Excel Analyzer")
    st.caption(
        "Supports CSV, XLSX, and XLS files with multi-layer headers (1–3 header rows), wide multi-column layouts, and chunked batch processing."
    )

    sub_catalog, sub_sales_stock, sub_any_analyze = st.tabs(
        [
            "1. Catalog Import (CSV / Excel)",
            "2. Sales Against Stock (CSV / Excel)",
            "3. Analyze Sales, Stock & Any File (CSV / Excel)",
        ]
    )

# --------------------------------------------------------------------------
    # SUB-TAB 1: BULK CATALOG IMPORT (CSV / EXCEL)
    # --------------------------------------------------------------------------
    with sub_catalog:
        sample_prod_csv = (
            "sku,name,category,unit_cost,selling_price,stock_qty,reorder_level\n"
            "SKU-2001,USB-C Hub 7-Port,Accessories,45.00,75.00,30,10\n"
            "SKU-2002,A4 Thermal Labels,Supplies,12.00,22.00,50,15\n"
        )
        st.download_button(
            label="Download Sample Product CSV",
            data=sample_prod_csv,
            file_name="sample_products_import.csv",
            mime="text/csv",
            key="dl_sample_prod_csv",
        )

        c_opt1, c_opt2 = st.columns([1, 2])
        prod_layers = c_opt1.selectbox("Header Layers (Rows)", [1, 2, 3], index=0, key="prod_header_layers")
        uploaded_prod = c_opt2.file_uploader(
            "Upload Product Catalog File (CSV, XLSX, XLS)",
            type=["csv", "xlsx", "xls"],
            key="upload_prod_file",
        )

        if uploaded_prod is not None:
            try:
                sheet_sel = _sheet_selector_if_excel(uploaded_prod, "prod")
                chunks, meta = parse_tabular_chunks(uploaded_prod, header_layers=prod_layers, sheet_name=sheet_sel)
                preview_df = chunks[0]
                all_cols = list(preview_df.columns)

                st.caption(
                    f"Source: `{meta['format']}` • `{meta['total_rows']:,}` rows × `{meta['total_columns']}` cols "
                    f"across `{meta['chunk_count']}` chunk(s)."
                )
                st.dataframe(preview_df.head(15), use_container_width=True, hide_index=True)

                map_opts = ["<Auto / None>"] + all_cols
                m1, m2, m3 = st.columns(3)
                col_sku = m1.selectbox("SKU Column", map_opts, index=0, key="p_map_sku")
                col_name = m2.selectbox("Name Column", map_opts, index=0, key="p_map_name")
                col_cat = m3.selectbox("Category Column", map_opts, index=0, key="p_map_cat")

                m4, m5, m6, m7 = st.columns(4)
                col_cost = m4.selectbox("Unit Cost", map_opts, index=0, key="p_map_cost")
                col_price = m5.selectbox("Selling Price", map_opts, index=0, key="p_map_price")
                col_qty = m6.selectbox("Stock Qty", map_opts, index=0, key="p_map_qty")
                col_reord = m7.selectbox("Reorder Level", map_opts, index=0, key="p_map_reord")

                if st.button("Import Catalog Into Neon", type="primary", key="btn_import_prod_file"):
                    res_sku = _resolve_mapped_column(all_cols, col_sku, ("sku", "product_sku", "item_sku", "code", "item_code"))
                    res_name = _resolve_mapped_column(all_cols, col_name, ("name", "product_name", "item_name", "title", "description"))

                    if not res_sku or not res_name:
                        st.error("Could not resolve required 'SKU' and 'Name' columns. Select them manually above.")
                    else:
                        res_cat = _resolve_mapped_column(all_cols, col_cat, ("category", "cat", "group", "department"))
                        res_cost = _resolve_mapped_column(all_cols, col_cost, ("unit_cost", "cost", "cost_price", "buy_price"))
                        res_price = _resolve_mapped_column(all_cols, col_price, ("selling_price", "price", "sell_price", "retail_price"))
                        res_qty = _resolve_mapped_column(all_cols, col_qty, ("stock_qty", "quantity", "qty", "stock", "balance"))
                        res_reord = _resolve_mapped_column(all_cols, col_reord, ("reorder_level", "reorder", "min_stock", "threshold"))

                        total_imported = 0
                        all_errors: list[str] = []
                        progress = st.progress(0.0, text="Importing catalog chunks...")

                        for c_idx, ch in enumerate(chunks):
                            batch_records = [
                                {
                                    "sku": row.get(res_sku),
                                    "name": row.get(res_name),
                                    "category": row.get(res_cat) if res_cat else "General",
                                    "unit_cost": row.get(res_cost) if res_cost else 0.0,
                                    "selling_price": row.get(res_price) if res_price else 0.0,
                                    "stock_qty": row.get(res_qty) if res_qty else 0,
                                    "reorder_level": row.get(res_reord) if res_reord else 10,
                                }
                                for _, row in ch.iterrows()
                            ]
                            imp, errs = bulk_upsert_products_batch(batch_records, actor=user["username"])
                            total_imported += imp
                            all_errors.extend(errs)
                            progress.progress(min(1.0, (c_idx + 1) / max(1, len(chunks))))

                        st.success(f"Imported/updated {total_imported:,} products.")
                        if all_errors:
                            st.warning(f"{len(all_errors)} row issue(s):\n" + "\n".join(all_errors[:25]))
            except Exception as exc:
                st.error(f"Could not process product file: {exc}")

    # --------------------------------------------------------------------------
    # SUB-TAB 2: IMPORT SALES AGAINST STOCK (CSV / EXCEL)
    # --------------------------------------------------------------------------
    with sub_sales_stock:
        sample_sales_csv = (
            "sku,quantity,unit_price,notes\n"
            "SKU-1001,2,260.00,Batch Sale #1\n"
            "SKU-1002,10,12.00,Batch Sale #2\n"
        )
        st.download_button(
            label="Download Sample Sales Against Stock CSV",
            data=sample_sales_csv,
            file_name="sample_sales_against_stock.csv",
            mime="text/csv",
            key="dl_sample_sales_csv",
        )

        s_opt1, s_opt2 = st.columns([1, 2])
        sales_layers = s_opt1.selectbox("Header Layers (Rows)", [1, 2, 3], index=0, key="sales_header_layers")
        uploaded_sales = s_opt2.file_uploader(
            "Upload Sales File to Deduct Against Stock (CSV, XLSX, XLS)",
            type=["csv", "xlsx", "xls"],
            key="upload_sales_file",
        )

        if uploaded_sales is not None:
            try:
                s_sheet = _sheet_selector_if_excel(uploaded_sales, "sales")
                s_chunks, s_meta = parse_tabular_chunks(uploaded_sales, header_layers=sales_layers, sheet_name=s_sheet)
                s_preview = s_chunks[0]
                s_cols = list(s_preview.columns)

                st.caption(
                    f"Source: `{s_meta['format']}` • `{s_meta['total_rows']:,}` rows × `{s_meta['total_columns']}` cols "
                    f"across `{s_meta['chunk_count']}` chunk(s)."
                )
                st.dataframe(s_preview.head(15), use_container_width=True, hide_index=True)

                s_map_opts = ["<Auto / None>"] + s_cols
                sm1, sm2, sm3, sm4 = st.columns(4)
                s_col_sku = sm1.selectbox("SKU Column", s_map_opts, index=0, key="s_map_sku")
                s_col_qty = sm2.selectbox("Sold Qty", s_map_opts, index=0, key="s_map_qty")
                s_col_price = sm3.selectbox("Unit Price", s_map_opts, index=0, key="s_map_price")
                s_col_notes = sm4.selectbox("Notes / Ref", s_map_opts, index=0, key="s_map_notes")

                if st.button("Process Sales Against Stock", type="primary", key="btn_import_sales_file"):
                    res_s_sku = _resolve_mapped_column(s_cols, s_col_sku, ("sku", "product_sku", "item_sku", "code", "item_code"))
                    res_s_qty = _resolve_mapped_column(s_cols, s_col_qty, ("quantity", "qty", "sold_qty", "sold", "units"))

                    if not res_s_sku or not res_s_qty:
                        st.error("Could not resolve required 'SKU' and 'Sold Qty' columns. Select them manually above.")
                    else:
                        res_s_price = _resolve_mapped_column(s_cols, s_col_price, ("unit_price", "price", "selling_price", "amount"))
                        res_s_notes = _resolve_mapped_column(s_cols, s_col_notes, ("notes", "note", "reference", "ref", "remarks"))

                        total_processed = 0
                        all_s_errors: list[str] = []
                        s_progress = st.progress(0.0, text="Processing sales against stock...")

                        for c_idx, ch in enumerate(s_chunks):
                            sale_batch = [
                                {
                                    "sku": row.get(res_s_sku),
                                    "quantity": row.get(res_s_qty),
                                    "unit_price": row.get(res_s_price) if res_s_price else None,
                                    "notes": row.get(res_s_notes) if res_s_notes else "Bulk sales against stock",
                                }
                                for _, row in ch.iterrows()
                            ]
                            proc, s_errs = bulk_sales_against_stock_batch(sale_batch, actor=user["username"])
                            total_processed += proc
                            all_s_errors.extend(s_errs)
                            s_progress.progress(min(1.0, (c_idx + 1) / max(1, len(s_chunks))))

                        st.success(f"Recorded {total_processed:,} sales against stock.")
                        if all_s_errors:
                            st.warning(f"{len(all_s_errors)} issue(s):\n" + "\n".join(all_s_errors[:25]))
            except Exception as exc:
                st.error(f"Could not process sales file: {exc}")

    # --------------------------------------------------------------------------
    # SUB-TAB 3: UNIVERSAL SALES, STOCK & ANY FILE ANALYZER (CSV / EXCEL)
    # --------------------------------------------------------------------------
    with sub_any_analyze:
        st.markdown("#### Upload Any Sales, Stock, or Business CSV / Excel File to Analyze")
        st.caption(
            "Works on any dataset—sales logs, stock sheets, multi-layer reports, or wide tables—without forcing database writes."
        )

        a_opt1, a_opt2 = st.columns([1, 2])
        any_layers = a_opt1.selectbox("Header Layers (Rows)", [1, 2, 3], index=0, key="any_header_layers")
        uploaded_any = a_opt2.file_uploader(
            "Upload Any Sales, Stock, or Data File (CSV, XLSX, XLS)",
            type=["csv", "xlsx", "xls"],
            key="upload_any_file",
        )

        if uploaded_any is not None:
            try:
                a_sheet = _sheet_selector_if_excel(uploaded_any, "any")
                a_chunks, a_meta = parse_tabular_chunks(uploaded_any, header_layers=any_layers, sheet_name=a_sheet)
                df_all = pd.concat(a_chunks, ignore_index=True)

                st.caption(
                    f"Loaded `{len(df_all):,}` rows × `{len(df_all.columns):,}` columns from `{a_meta['format']}`."
                )

                numeric_cols = [
                    c for c in df_all.columns
                    if pd.to_numeric(df_all[c], errors="coerce").notna().sum() > 0
                ]
                cat_cols = [c for c in df_all.columns if c not in numeric_cols]

                k1, k2, k3, k4 = st.columns(4)
                k1.metric("Total Rows", f"{len(df_all):,}")
                k2.metric("Total Columns", f"{len(df_all.columns):,}")
                k3.metric("Numeric Columns", f"{len(numeric_cols):,}")
                k4.metric("Missing Cells", f"{int(df_all.isna().sum().sum()):,}")

                if cat_cols:
                    f_col = st.selectbox("Filter by Column (Optional)", ["<All Rows>"] + cat_cols, key="any_filter_col")
                    if f_col != "<All Rows>":
                        vals = ["<All>"] + sorted(df_all[f_col].dropna().astype(str).unique().tolist())[:200]
                        f_val = st.selectbox("Filter Value", vals, key="any_filter_val")
                        if f_val != "<All>":
                            df_all = df_all[df_all[f_col].astype(str) == f_val]

                st.dataframe(df_all.head(200), use_container_width=True, hide_index=True)

                if numeric_cols:
                    st.markdown("##### Column Statistical Summary")
                    num_frame = pd.DataFrame({c: pd.to_numeric(df_all[c], errors="coerce") for c in numeric_cols})
                    summary_stats = pd.DataFrame(
                        {
                            "count": num_frame.count(),
                            "sum": num_frame.sum(),
                            "mean": num_frame.mean(),
                            "median": num_frame.median(),
                            "std": num_frame.std(),
                            "min": num_frame.min(),
                            "max": num_frame.max(),
                        }
                    ).round(2)
                    st.dataframe(summary_stats, use_container_width=True)

                    ch1, ch2 = st.columns(2)
                    group_choices = ["<None>"] + list(df_all.columns)
                    grp_col = ch1.selectbox("Group / X-Axis Column", group_choices, key="any_grp_col")
                    val_col = ch2.selectbox("Metric / Y-Axis Column", numeric_cols, key="any_val_col")

                    y_series = pd.to_numeric(df_all[val_col], errors="coerce")
                    if grp_col != "<None>":
                        chart_df = pd.DataFrame({"group": df_all[grp_col].astype(str), "value": y_series}).dropna()
                        agg_df = chart_df.groupby("group", as_index=False)["value"].sum().sort_values("value", ascending=False).head(25)
                        fig_any = px.bar(agg_df, x="group", y="value", title=f"Total {val_col} by {grp_col}")
                        st.plotly_chart(fig_any, use_container_width=True)
                    else:
                        fig_hist = px.histogram(y_series.dropna(), title=f"Distribution of {val_col}")
                        st.plotly_chart(fig_hist, use_container_width=True)

                out_csv = io.StringIO()
                df_all.to_csv(out_csv, index=False)
                st.download_button(
                    label="Download Analyzed Dataset (CSV)",
                    data=out_csv.getvalue(),
                    file_name=f"analyzed_{date.today().isoformat()}.csv",
                    mime="text/csv",
                    key="dl_analyzed_csv",
                )
            except Exception as exc:
                st.error(f"Could not analyze file: {exc}")


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
            col_role, col_reset = st.columns(2)

            with col_role:
                st.markdown("##### Role & Account Status")
                with st.form("manage_user_form"):
                    target_label = st.selectbox("Select User to Manage", list(user_map.keys()), key="role_target_user")
                    new_role = st.selectbox("Assign Role", ["staff", "admin"])
                    new_active = st.checkbox("Active Account", value=True)
                    apply_btn = st.form_submit_button("Save User Role & Status", type="primary", use_container_width=True)
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

            with col_reset:
                st.markdown("##### Reset User Password")
                with st.form("admin_reset_password_form", clear_on_submit=True):
                    reset_target_label = st.selectbox("Select User", list(user_map.keys()), key="reset_target_user")
                    temp_pw = st.text_input("New / Temporary Password", type="password", placeholder="Min 6 characters")
                    confirm_temp_pw = st.text_input("Confirm Password", type="password")
                    reset_user_btn = st.form_submit_button("Reset Password", use_container_width=True)
                    if reset_user_btn:
                        if temp_pw != confirm_temp_pw:
                            st.error("Passwords do not match.")
                        else:
                            ok, msg = admin_reset_user_password(
                                target_user_id=user_map[reset_target_label],
                                new_password=temp_pw,
                                actor_role=user.get("role", "staff"),
                                actor=user["username"],
                            )
                            if ok:
                                st.success(msg)
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

        st.divider()
        st.caption(
            f"**{DEVELOPER_COMPANY}**\n\n"
            f"`{DEVELOPER_EMAIL}`\n\n"
            f"© {_copyright_span()} {DEVELOPER_COMPANY}"
        )

    st.title("Enterprise AI Inventory & BI Command Centre")
    maybe_show_daily_welcome(user)

    products_df = fetch_products()
    tx_df = fetch_transactions(days=int(days_window))

    tab_labels = ["Overview & KPIs", "Inventory & Sales"]
    if is_admin:
        tab_labels.extend(["BI Analytics", "Upload & Analyze", "Users & Audit Log"])

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

    render_developer_footer()


# ==============================================================================
# 11. ENTRY POINT
# ==============================================================================
def main():
    inject_responsive_styles()
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
