from datetime import datetime, timedelta
import hashlib
import hmac
import os
import secrets
import sqlite3
import urllib.parse
import io
import logging
import secrets
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# ---------------------------------------------------------
# PAGE CONFIGURATION
# ---------------------------------------------------------
st.set_page_config(
    page_title="Enterprise AI Inventory & BI Command Centre",
    page_icon="🌍",
    layout="wide",
    initial_sidebar_state="expanded",
)

AUTH_DB_PATH = os.environ.get("ENTERPRISE_AUTH_DB", "enterprise_auth.db")
MAX_LOGIN_ATTEMPTS = 5


# ---------------------------------------------------------
# AUTHENTICATION & SECURITY HELPERS (PBKDF2 + SQLITE)
# ---------------------------------------------------------
def hash_password(password: str, salt_hex: str | None = None) -> tuple[str, str]:
  """Hash a password using PBKDF2-HMAC-SHA256 and a random salt."""
  salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
  digest = hashlib.pbkdf2_hmac(
      "sha256",
      password.encode("utf-8"),
      salt,
      200_000,
  )
  return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: str, expected_hash_hex: str) -> bool:
  """Verify a password in constant time."""
  if not password or not salt_hex or not expected_hash_hex:
    return False
  _, actual_hash_hex = hash_password(password, salt_hex)
  return hmac.compare_digest(actual_hash_hex, expected_hash_hex)


def validate_password_strength(password: str) -> list[str]:
  """Return a list of unmet password rules for clear user guidance."""
  issues = []
  if len(password) < 8:
    issues.append("At least 8 characters long")
  if not any(ch.isupper() for ch in password):
    issues.append("At least one uppercase letter (A-Z)")
  if not any(ch.islower() for ch in password):
    issues.append("At least one lowercase letter (a-z)")
  if not any(ch.isdigit() for ch in password):
    issues.append("At least one number (0-9)")
  return issues


def init_auth_db() -> sqlite3.Connection:
  """Initialize SQLite auth store and seed a default demo user if empty."""
  conn = sqlite3.connect(AUTH_DB_PATH, check_same_thread=False)
  conn.execute("""
      CREATE TABLE IF NOT EXISTS users (
          username TEXT PRIMARY KEY,
          full_name TEXT NOT NULL,
          email TEXT NOT NULL,
          role TEXT NOT NULL DEFAULT 'Analyst',
          salt_hex TEXT NOT NULL,
          password_hash TEXT NOT NULL,
          reset_token TEXT,
          reset_expires_at TEXT,
          failed_attempts INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL
      )
      """)
  cur = conn.execute("SELECT COUNT(*) FROM users")
  count = cur.fetchone()[0]
  if count == 0:
    salt_h, pwd_h = hash_password("RogersBI#2026")
    now_iso = datetime.utcnow().isoformat()
    conn.execute(
        """
        INSERT INTO users (
            username, full_name, email, role, salt_hex, password_hash,
            reset_token, reset_expires_at, failed_attempts, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, 0, ?)
        """,
        (
            "admin",
            "Rogers Lead Administrator",
            "admin@rogersenterprises.local",
            "Admin",
            salt_h,
            pwd_h,
            now_iso,
        ),
    )
    conn.commit()
  return conn


def get_user(conn: sqlite3.Connection, identifier: str) -> dict | None:
  """Fetch user by username or email (case-insensitive)."""
  clean_id = (identifier or "").strip().lower()
  if not clean_id:
    return None
  row = conn.execute(
      """
      SELECT username, full_name, email, role, salt_hex, password_hash,
             reset_token, reset_expires_at, failed_attempts, created_at
      FROM users
      WHERE LOWER(username) = ? OR LOWER(email) = ?
      """,
      (clean_id, clean_id),
  ).fetchone()
  if not row:
    return None
  return {
      "username": row[0],
      "full_name": row[1],
      "email": row[2],
      "role": row[3],
      "salt_hex": row[4],
      "password_hash": row[5],
      "reset_token": row[6],
      "reset_expires_at": row[7],
      "failed_attempts": int(row[8] or 0),
      "created_at": row[9],
  }


def register_user(
    conn: sqlite3.Connection,
    username: str,
    full_name: str,
    email: str,
    role: str,
    password: str,
) -> tuple[bool, str]:
  """Register a new user account."""
  u = username.strip()
  fn = full_name.strip()
  em = email.strip()
  if len(u) < 3 or not u.replace("_", "").replace(".", "").isalnum():
    return False, "Username must be at least 3 characters (letters, numbers, _, .)."
  if not fn:
    return False, "Please enter your full name."
  if "@" not in em or "." not in em.split("@")[-1]:
    return False, "Please enter a valid email address."
  strength_issues = validate_password_strength(password)
  if strength_issues:
    return False, "Password requirements not met: " + ", ".join(strength_issues) + "."
  if get_user(conn, u) or get_user(conn, em):
    return False, "An account with that username or email already exists."

  salt_h, pwd_h = hash_password(password)
  conn.execute(
      """
      INSERT INTO users (
          username, full_name, email, role, salt_hex, password_hash,
          reset_token, reset_expires_at, failed_attempts, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, 0, ?)
      """,
      (
          u,
          fn,
          em,
          role,
          salt_h,
          pwd_h,
          datetime.utcnow().isoformat(),
      ),
  )
  conn.commit()
  return True, f"Account created for '{u}' ({role}). You can now log in."


def authenticate_user(
    conn: sqlite3.Connection, identifier: str, password: str
) -> tuple[bool, str, dict | None]:
  """Authenticate user credentials with attempt tracking."""
  user = get_user(conn, identifier)
  if not user:
    return False, "No account found matching that username or email.", None

  if user["failed_attempts"] >= MAX_LOGIN_ATTEMPTS:
    return (
        False,
        f"Account locked after {MAX_LOGIN_ATTEMPTS} failed attempts. Use 'Forgot / Reset Password' below to recover access.",
        None,
    )

  if verify_password(password, user["salt_hex"], user["password_hash"]):
    conn.execute(
        "UPDATE users SET failed_attempts = 0 WHERE username = ?",
        (user["username"],),
    )
    conn.commit()
    return True, f"Welcome back, {user['full_name']}!", user

  new_attempts = user["failed_attempts"] + 1
  conn.execute(
      "UPDATE users SET failed_attempts = ? WHERE username = ?",
      (new_attempts, user["username"]),
  )
  conn.commit()
  remaining = max(0, MAX_LOGIN_ATTEMPTS - new_attempts)
  return (
      False,
      f"Incorrect password. {remaining} attempt(s) remaining before temporary lock.",
      None,
  )


def issue_recovery_token(
    conn: sqlite3.Connection, identifier: str
) -> tuple[bool, str, str | None]:
  """Generate a 15-minute password reset token for an existing user."""
  user = get_user(conn, identifier)
  if not user:
    return False, "No account found for that username or email.", None
  token = secrets.token_urlsafe(8)
  expires_at = (datetime.utcnow() + timedelta(minutes=15)).isoformat()
  conn.execute(
      """
      UPDATE users
      SET reset_token = ?, reset_expires_at = ?
      WHERE username = ?
      """,
      (token, expires_at, user["username"]),
  )
  conn.commit()
  return (
      True,
      f"Recovery token generated for @{user['username']} (valid for 15 minutes).",
      token,
  )


def reset_password_with_token(
    conn: sqlite3.Connection,
    identifier: str,
    token: str,
    new_password: str,
) -> tuple[bool, str]:
  """Validate recovery token and update user password."""
  user = get_user(conn, identifier)
  if not user:
    return False, "Account not found."
  if not user["reset_token"] or not user["reset_expires_at"]:
    return False, "No active recovery token found. Request a new token first."
  if datetime.utcnow() > datetime.fromisoformat(user["reset_expires_at"]):
    return False, "Recovery token has expired. Request a new token."
  if not hmac.compare_digest(token.strip(), user["reset_token"].strip()):
    return False, "Invalid recovery token."

  issues = validate_password_strength(new_password)
  if issues:
    return False, "New password does not meet rules: " + ", ".join(issues) + "."

  salt_h, pwd_h = hash_password(new_password)
  conn.execute(
      """
      UPDATE users
      SET salt_hex = ?, password_hash = ?, reset_token = NULL,
          reset_expires_at = NULL, failed_attempts = 0
      WHERE username = ?
      """,
      (salt_h, pwd_h, user["username"]),
  )
  conn.commit()
  return True, "Password reset successful. You may now log in with your new password."


def render_auth_gate(conn: sqlite3.Connection) -> bool:
  """Render Sign In, Sign Up, and Password Recovery UI with explicit prompts."""
  if st.session_state.get("auth_user"):
    return True

  st.title("🔐 Enterprise Access & Account Portal")
  st.caption(
      "Rogers Enterprises Limited | Secure Sign-In, Account Registration & Password Recovery"
  )

  st.info(
      "**What to do here:**\n"
      "- **Existing Users (Login):** Enter your *Username or Email* and *Password*. "
      "Demo admin credentials: `admin` / `RogersBI#2026`.\n"
      "- **New Users (Sign Up):** Switch to **Create Account**, choose your workspace role "
      "(*Analyst* for analytics & reporting, *Admin* for full operational controls), and set a strong password.\n"
      "- **Forgot Password (Recovery):** Switch to **Recover Password** to generate a 15-minute reset token and set a new password."
  )

  tab_login, tab_signup, tab_reset = st.tabs(
      ["🔑 Login", "📝 Create Account (Sign Up)", "♻️ Recover / Reset Password"]
  )

  with tab_login:
    st.subheader("Sign In to Your Workspace")
    st.markdown(
        "**Required right now:** Provide your registered **Username or Email** and **Password**, then click **Sign In**."
    )
    with st.form("login_form"):
      login_id = st.text_input(
          "Username or Email",
          placeholder="e.g., admin or analyst@company.com",
      )
      login_pwd = st.text_input(
          "Password",
          type="password",
          placeholder="Enter your password",
      )
      submitted_login = st.form_submit_button("🔓 Sign In", use_container_width=True)

    if submitted_login:
      if not login_id.strip() or not login_pwd:
        st.warning("Please enter both your Username/Email and Password before signing in.")
      else:
        ok, msg, user_rec = authenticate_user(conn, login_id, login_pwd)
        if ok and user_rec:
          st.session_state["auth_user"] = user_rec
          st.success(msg)
          st.rerun()
        else:
          st.error(msg)

  with tab_signup:
    st.subheader("Register a New Enterprise Account")
    st.markdown(
        "**Required right now:** Fill in all fields below. Your password must have **8+ characters**, "
        "**1 uppercase letter**, **1 lowercase letter**, and **1 digit**."
    )
    with st.form("signup_form"):
      s_col1, s_col2 = st.columns(2)
      new_fullname = s_col1.text_input(
          "Full Name", placeholder="e.g., Kwame Mensah"
      )
      new_username = s_col2.text_input(
          "Choose Username", placeholder="e.g., kwame_bi"
      )
      s_col3, s_col4 = st.columns(2)
      new_email = s_col3.text_input(
          "Work Email", placeholder="e.g., kwame@rogersenterprises.com"
      )
      new_role = s_col4.selectbox(
          "Select Access Role",
          ["Analyst", "Inventory Planner", "Admin"],
          help="Analyst & Inventory Planner can run BI & forecasting; Admin manages full operational workflows.",
      )
      p_col1, p_col2 = st.columns(2)
      new_pwd = p_col1.text_input(
          "Create Password", type="password", placeholder="Min 8 chars, Aa1"
      )
      confirm_pwd = p_col2.text_input(
          "Confirm Password", type="password", placeholder="Re-enter password"
      )
      submitted_signup = st.form_submit_button(
          "✅ Create Account", use_container_width=True
      )

    if submitted_signup:
      if new_pwd != confirm_pwd:
        st.error("Passwords do not match. Please re-enter both password fields.")
      else:
        ok, msg = register_user(
            conn, new_username, new_fullname, new_email, new_role, new_pwd
        )
        if ok:
          st.success(msg)
        else:
          st.error(msg)

  with tab_reset:
    st.subheader("Password Recovery & Reset")
    st.markdown(
        "**Step 1:** Enter your **Username or Email** and click **Generate Recovery Token**.\n"
        "**Step 2:** Copy the token shown and enter your **New Password** below, then click **Reset Password**."
    )
    with st.form("request_token_form"):
      rec_id = st.text_input(
          "Account Username or Email",
          placeholder="Enter username or email to recover",
      )
      req_token_btn = st.form_submit_button("🎟️ Generate Recovery Token")

    if req_token_btn:
      ok, msg, token_val = issue_recovery_token(conn, rec_id)
      if ok and token_val:
        st.session_state["last_issued_token"] = token_val
        st.session_state["last_recovery_id"] = rec_id.strip()
        st.success(msg)
        st.code(token_val, language="text")
      else:
        st.error(msg)

    st.markdown("---")
    with st.form("apply_reset_form"):
      r_col1, r_col2 = st.columns(2)
      reset_id = r_col1.text_input(
          "Confirm Username or Email",
          value=st.session_state.get("last_recovery_id", ""),
      )
      reset_token = r_col2.text_input(
          "Recovery Token",
          value=st.session_state.get("last_issued_token", ""),
          placeholder="Paste 15-minute recovery token",
      )
      r_col3, r_col4 = st.columns(2)
      reset_new_pwd = r_col3.text_input("New Password", type="password")
      reset_confirm_pwd = r_col4.text_input("Confirm New Password", type="password")
      apply_reset_btn = st.form_submit_button(
          "🔁 Reset Password", use_container_width=True
      )

    if apply_reset_btn:
      if reset_new_pwd != reset_confirm_pwd:
        st.error("New password and confirmation do not match.")
      else:
        ok, msg = reset_password_with_token(
            conn, reset_id, reset_token, reset_new_pwd
        )
        if ok:
          st.session_state.pop("last_issued_token", None)
          st.success(msg)
        else:
          st.error(msg)

  return False


# ---------------------------------------------------------
# SHARED SAFE DATA & ANALYTICS HELPERS
# ---------------------------------------------------------
def clean_text_cell(v):
  """Normalize text cells and map null-like strings to np.nan."""
  if v is None:
    return np.nan
  try:
    if pd.isna(v):
      return np.nan
  except Exception:
    pass
  cleaned = " ".join(str(v).strip().split())
  return (
      np.nan
      if cleaned in {"", "nan", "NaN", "None", "none", "NULL", "null", "<NA>"}
      else cleaned
  )


def clean_numeric_series(s: pd.Series) -> pd.Series:
  """Safely convert currency/comma strings and numbers into float64."""
  if s is None or len(s) == 0:
    return pd.Series([], dtype="float64")
  if pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s):
    return pd.to_numeric(s, errors="coerce")
  cleaned_str = (
      s.apply(clean_text_cell)
      .dropna()
      .astype(str)
      .str.replace(r"[,\sGHS$€£¥₦]", "", regex=True)
      .str.replace(r"^\((.*)\)$", r"-\1", regex=True)
      .str.strip()
  )
  out = pd.Series(np.nan, index=s.index, dtype="float64")
  parsed = pd.to_numeric(cleaned_str, errors="coerce")
  out.loc[parsed.index] = parsed
  return out


def find_col_case_insensitive(df: pd.DataFrame, target: str) -> str | None:
  """Find a DataFrame column matching target ignoring case and outer spaces."""
  target_low = target.strip().lower()
  for col in df.columns:
    if str(col).strip().lower() == target_low:
      return col
  return None


def load_any_tabular_file(uploaded_file, key_prefix: str = "lab") -> pd.DataFrame:
  """Load CSV or Excel workbook safely with encoding fallback."""
  name = str(uploaded_file.name).lower()
  if name.endswith(".csv"):
    for enc in ["utf-8", "utf-8-sig", "latin-1", "cp1252"]:
      try:
        uploaded_file.seek(0)
        return pd.read_csv(uploaded_file, encoding=enc)
      except Exception:
        continue
    uploaded_file.seek(0)
    return pd.read_csv(uploaded_file, sep=None, engine="python")
  uploaded_file.seek(0)
  xls = pd.ExcelFile(uploaded_file)
  sheet_name = st.selectbox(
      "Select Excel Sheet", xls.sheet_names, key=f"{key_prefix}_excel_sheet"
  )
  uploaded_file.seek(0)
  return pd.read_excel(uploaded_file, sheet_name=sheet_name)


def compute_exponential_forecast(
    series: pd.Series, steps: int = 7, alpha: float = 0.45, beta: float = 0.25
) -> np.ndarray:
  """Holt's linear trend exponential smoothing for modern short-horizon forecasting."""
  vals = series.dropna().astype(float).values
  if len(vals) == 0:
    return np.zeros(steps, dtype=float)
  if len(vals) == 1:
    return np.full(steps, max(0.0, float(vals[0])), dtype=float)

  level = float(vals[0])
  trend = float(vals[1] - vals[0])
  for y in vals[1:]:
    prev_level = level
    level = alpha * float(y) + (1.0 - alpha) * (level + trend)
    trend = beta * (level - prev_level) + (1.0 - beta) * trend

  forecasts = [
      max(0.0, level + h * trend) for h in range(1, steps + 1)
  ]
  return np.array(forecasts, dtype=float)


def detect_abnormalities(df: pd.DataFrame) -> pd.DataFrame:
  """Detect duplicates, nulls, negative numbers, zeros, IQR outliers, and bad dates."""
  flags = []
  if df is None or df.empty:
    return pd.DataFrame(flags)

  try:
    dup_count = int(df.duplicated().sum())
    if dup_count > 0:
      flags.append({
          "Severity": "🚨 Danger",
          "Type": "Duplicate Rows",
          "Target": "All Columns",
          "Detail": f"{dup_count} exact duplicate row(s) detected.",
      })
  except Exception:
    pass

  for col in df.columns:
    s_col = df[col]
    missing_cnt = int(s_col.isna().sum())
    cleaned_cells = s_col.apply(clean_text_cell)
    blank_str_cnt = max(0, int(cleaned_cells.isna().sum()) - missing_cnt)

    if missing_cnt > 0 or blank_str_cnt > 0:
      flags.append({
          "Severity": "⚠️ Warning",
          "Type": "Missing / Blank Values",
          "Target": str(col),
          "Detail": (
              f"{missing_cnt} null(s) and {blank_str_cnt} blank text entry(ies)."
          ),
      })

    num_try = clean_numeric_series(s_col)
    non_blank_cnt = int(cleaned_cells.notna().sum())
    valid_ratio = (
        (num_try.notna().sum() / non_blank_cnt) if non_blank_cnt > 0 else 0.0
    )
    if valid_ratio >= 0.6:
      neg_cnt = int((num_try < 0).sum())
      zero_cnt = int((num_try == 0).sum())
      if neg_cnt > 0:
        flags.append({
            "Severity": "🚨 Danger",
            "Type": "Negative Values",
            "Target": str(col),
            "Detail": (
                f"{neg_cnt} negative value(s) found (check for losses, returns,"
                " or entry errors)."
            ),
        })
      if zero_cnt > 0:
        flags.append({
            "Severity": "⚠️ Notice",
            "Type": "Zero Values",
            "Target": str(col),
            "Detail": f"{zero_cnt} zero value(s) detected.",
        })
      non_null_num = num_try.dropna()
      if len(non_null_num) >= 4:
        q1, q3 = np.percentile(non_null_num, [25, 75])
        iqr = float(q3 - q1)
        if iqr > 0:
          outliers = non_null_num[
              (non_null_num < q1 - 1.5 * iqr)
              | (non_null_num > q3 + 1.5 * iqr)
          ]
          if len(outliers) > 0:
            flags.append({
                "Severity": "🚨 Abnormality",
                "Type": "Statistical Outliers (IQR)",
                "Target": str(col),
                "Detail": (
                    f"{len(outliers)} extreme value(s) outside"
                    f" [{q1 - 1.5 * iqr:,.2f}, {q3 + 1.5 * iqr:,.2f}]."
                ),
            })

    if any(k in str(col).lower() for k in ["date", "time", "dt", "day", "month"]):
      parsed_dates = pd.to_datetime(cleaned_cells, errors="coerce")
      failed_dates = max(
          0, int(parsed_dates.isna().sum()) - int(cleaned_cells.isna().sum())
      )
      if failed_dates > 0:
        flags.append({
            "Severity": "⚠️ Format Issue",
            "Type": "Unstandardized / Invalid Dates",
            "Target": str(col),
            "Detail": (
                f"{failed_dates} entry(ies) could not be cleanly parsed as"
                " dates."
            ),
        })

  return pd.DataFrame(flags)


# ---------------------------------------------------------
# AUTHENTICATION GATE
# ---------------------------------------------------------
auth_conn = init_auth_db()
if not render_auth_gate(auth_conn):
  st.stop()

current_user = st.session_state["auth_user"]

# ---------------------------------------------------------
# HEADER & LOGGED-IN SESSION CONTROLS
# ---------------------------------------------------------
st.title("🌍 Enterprise-Grade Supply Chain & BI Intelligence Suite")
st.markdown(
    "Inspired by **Amazon, Jumia, and Alibaba** operational architectures —"
    " Featuring ABC-XYZ Classification, Safety Stock Buffers, SKU Tiering,"
    " Universal CSV/Excel Multitask Analysis, Regression & AI Future Diagnosis."
    " Empowered by **Rogers Enterprises Limited | Ghana**."
)

# ---------------------------------------------------------
# SIDEBAR DATA INPUTS, USER PROFILE & SETTINGS
# ---------------------------------------------------------
st.sidebar.success(
    f"👤 **{current_user['full_name']}** (@{current_user['username']})\n"
    f"Role: `{current_user['role']}`"
)
if st.sidebar.button("🚪 Log Out", use_container_width=True):
  st.session_state.pop("auth_user", None)
  st.rerun()

st.sidebar.markdown("---")
st.sidebar.header("📁 Enterprise Data Inputs")
sales_file = st.sidebar.file_uploader(
    "Upload Sales CSV (Columns: Date, ItemName, QuantitySold, UnitPrice)",
    type=["csv"],
)
stock_file = st.sidebar.file_uploader(
    "Upload Current Stock CSV (Columns: ItemName, CurrentStock, CostPrice,"
    " SellingPrice, SupplierPhone)",
    type=["csv"],
)

st.sidebar.markdown("---")
st.sidebar.subheader("⚙️ Supply Chain Parameters")
supplier_lead_time = st.sidebar.slider("Supplier Lead Time (Days)", 1, 30, 7)
service_level_factor = st.sidebar.selectbox(
    "Service Level Safety Buffer", [1.28, 1.65, 2.05], index=1
)
sales_growth_slider = st.sidebar.slider(
    "Demand Volatility / Growth Simulation (%)", -50, 100, 0, step=5
)

# ---------------------------------------------------------
# MODULE 1: ENTERPRISE SUPPLY CHAIN & ABC-XYZ BI ENGINE
# ---------------------------------------------------------
if sales_file and stock_file:
  try:
    df_sales = pd.read_csv(sales_file)
    df_stock = pd.read_csv(stock_file)

    item_col_sales = find_col_case_insensitive(df_sales, "ItemName")
    qty_col_sales = find_col_case_insensitive(df_sales, "QuantitySold")
    price_col_sales = find_col_case_insensitive(df_sales, "UnitPrice")

    item_col_stock = find_col_case_insensitive(df_stock, "ItemName")
    curr_stock_col = find_col_case_insensitive(df_stock, "CurrentStock")
    cost_price_col = find_col_case_insensitive(df_stock, "CostPrice")
    sell_price_col = find_col_case_insensitive(df_stock, "SellingPrice")
    phone_col_stock = find_col_case_insensitive(df_stock, "SupplierPhone")

    if not all([item_col_sales, qty_col_sales, price_col_sales]):
      st.error(
          "Sales CSV must include ItemName, QuantitySold, and UnitPrice columns."
      )
    elif not all([item_col_stock, curr_stock_col, cost_price_col]):
      st.error(
          "Stock CSV must include ItemName, CurrentStock, and CostPrice"
          " columns."
      )
    else:
      df_sales = df_sales.copy()
      df_stock = df_stock.copy()
      df_sales["_item"] = df_sales[item_col_sales].apply(clean_text_cell)
      df_sales["_qty"] = clean_numeric_series(df_sales[qty_col_sales]).fillna(0.0)
      df_sales["_price"] = clean_numeric_series(
          df_sales[price_col_sales]
      ).fillna(0.0)
      df_sales["TotalRevenue"] = df_sales["_qty"] * df_sales["_price"]

      item_sales = (
          df_sales.dropna(subset=["_item"])
          .groupby("_item", as_index=False)
          .agg(
              QuantitySold=("_qty", "sum"),
              OrderCount=("_qty", "count"),
              QtyStd=("_qty", "std"),
              TotalRevenue=("TotalRevenue", "sum"),
          )
          .rename(columns={"_item": "ItemName"})
      )
      item_sales["QtyStd"] = item_sales["QtyStd"].fillna(0.0)

      df_stock["ItemName"] = df_stock[item_col_stock].apply(clean_text_cell)
      df_stock["CurrentStock"] = clean_numeric_series(
          df_stock[curr_stock_col]
      ).fillna(0.0)
      df_stock["CostPrice"] = clean_numeric_series(
          df_stock[cost_price_col]
      ).fillna(0.0)
      if sell_price_col:
        df_stock["SellingPrice"] = clean_numeric_series(
            df_stock[sell_price_col]
        ).fillna(df_stock["CostPrice"] * 1.3)
      else:
        df_stock["SellingPrice"] = df_stock["CostPrice"] * 1.3
      if phone_col_stock:
        df_stock["SupplierPhone"] = df_stock[phone_col_stock].apply(
            clean_text_cell
        )

      df_analysis = pd.merge(df_stock, item_sales, on="ItemName", how="left")
      df_analysis["QuantitySold"] = df_analysis["QuantitySold"].fillna(0.0)
      df_analysis["OrderCount"] = df_analysis["OrderCount"].fillna(0.0)
      df_analysis["QtyStd"] = df_analysis["QtyStd"].fillna(0.0)
      df_analysis["TotalRevenue"] = df_analysis["TotalRevenue"].fillna(0.0)

      df_analysis["TotalPotentialRevenue"] = (
          df_analysis["CurrentStock"] * df_analysis["SellingPrice"]
      )
      df_analysis["CapitalTiedUp"] = (
          df_analysis["CurrentStock"] * df_analysis["CostPrice"]
      )
      df_analysis["PotentialProfit"] = (
          df_analysis["TotalPotentialRevenue"] - df_analysis["CapitalTiedUp"]
      )

      days_analyzed = 30.0
      base_velocity = df_analysis["QuantitySold"] / days_analyzed
      df_analysis["SimulatedVelocity"] = base_velocity * (
          1.0 + (sales_growth_slider / 100.0)
      )

      # Coefficient of variation (CV) for XYZ demand stability classification
      mean_qty = np.where(
          df_analysis["OrderCount"] > 0,
          df_analysis["QuantitySold"] / np.maximum(df_analysis["OrderCount"], 1.0),
          0.0,
      )
      df_analysis["DemandCV"] = np.where(
          mean_qty > 0, df_analysis["QtyStd"] / mean_qty, 0.0
      )

      def classify_xyz(cv: float) -> str:
        if cv <= 0.5:
          return "X (Steady Demand)"
        if cv <= 1.0:
          return "Y (Variable Demand)"
        return "Z (Erratic / Lumpy)"

      df_analysis["XYZ_Class"] = df_analysis["DemandCV"].apply(classify_xyz)

      df_analysis["SafetyStock"] = (
          df_analysis["SimulatedVelocity"]
          * 0.2
          * supplier_lead_time
          * float(service_level_factor)
      ).apply(lambda x: max(1, int(round(x))) if pd.notna(x) else 1)

      df_analysis["ReorderPoint"] = (
          df_analysis["SimulatedVelocity"] * supplier_lead_time
          + df_analysis["SafetyStock"]
      ).apply(lambda x: int(round(x)) if pd.notna(x) else 0)

      df_analysis["DaysLeft"] = df_analysis.apply(
          lambda row: round(row["CurrentStock"] / row["SimulatedVelocity"], 1)
          if row["SimulatedVelocity"] > 0
          else 999,
          axis=1,
      )
      today = datetime.now()
      df_analysis["StockoutDate"] = df_analysis["DaysLeft"].apply(
          lambda d: (today + timedelta(days=int(min(d, 3650)))).strftime(
              "%Y-%m-%d"
          )
          if pd.notna(d) and d < 999
          else "No Risk"
      )

      df_analysis = df_analysis.sort_values(
          by="TotalRevenue", ascending=False
      ).reset_index(drop=True)
      df_analysis["CumulativeRevenue"] = df_analysis["TotalRevenue"].cumsum()
      total_rev_sum = float(df_analysis["TotalRevenue"].sum())
      if total_rev_sum > 0:
        df_analysis["CumulativePct"] = (
            df_analysis["CumulativeRevenue"] / total_rev_sum
        )
      else:
        df_analysis["CumulativePct"] = 0.0

      def classify_abc(pct: float) -> str:
        if pct <= 0.80:
          return "Class A (High Value - Strict Control)"
        if pct <= 0.95:
          return "Class B (Moderate Value)"
        return "Class C (Long-Tail / Bulk)"

      df_analysis["ABC_Category"] = df_analysis["CumulativePct"].apply(
          classify_abc
      )
      df_analysis["ABC_XYZ_Matrix"] = (
          df_analysis["ABC_Category"].str.slice(0, 7)
          + " + "
          + df_analysis["XYZ_Class"].str.slice(0, 1)
      )

      def sku_tier(row) -> str:
        if row["SimulatedVelocity"] >= 5:
          return "🔥 Best Seller (Fast Moving)"
        if row["SimulatedVelocity"] >= 1:
          return "⚡ Steady Mover"
        if row["CurrentStock"] > 10 and row["SimulatedVelocity"] < 0.2:
          return "⚠️ Dead Stock / Capital Trap"
        return "💤 Slow Moving"

      df_analysis["SKU_Tier"] = df_analysis.apply(sku_tier, axis=1)

      df_analysis["RecommendedOrderQty"] = df_analysis.apply(
          lambda row: max(
              0,
              int(
                  round(
                      (row["SimulatedVelocity"] * 30 + row["SafetyStock"])
                      - row["CurrentStock"]
                  )
              ),
          ),
          axis=1,
      )
      df_analysis["EstimatedOrderCost"] = (
          df_analysis["RecommendedOrderQty"] * df_analysis["CostPrice"]
      )

      st.markdown("### 📊 Enterprise Executive Performance Metrics")
      col1, col2, col3, col4, col5 = st.columns(5)
      col1.metric(
          "Total Stock Capital",
          f"GHS {df_analysis['CapitalTiedUp'].sum():,.2f}",
      )
      col2.metric(
          "Projected Gross Profit",
          f"GHS {df_analysis['PotentialProfit'].sum():,.2f}",
      )
      col3.metric("Total Sales Revenue", f"GHS {total_rev_sum:,.2f}")
      col4.metric(
          "Class A Core Items",
          len(
              df_analysis[df_analysis["ABC_Category"].str.contains("Class A")]
          ),
      )
      col5.metric(
          "Dead Stock Traps",
          len(df_analysis[df_analysis["SKU_Tier"].str.contains("Dead Stock")]),
      )
      st.markdown("---")

      col_ch1, col_ch2 = st.columns(2)
      with col_ch1:
        st.subheader("🌍 ABC Inventory Classification Breakdown")
        fig_abc = px.pie(
            df_analysis,
            names="ABC_Category",
            values="TotalRevenue",
            color="ABC_Category",
            color_discrete_map={
                "Class A (High Value - Strict Control)": "#FF4B4B",
                "Class B (Moderate Value)": "#FFA500",
                "Class C (Long-Tail / Bulk)": "#2ECC71",
            },
        )
        st.plotly_chart(fig_abc, use_container_width=True)
      with col_ch2:
        st.subheader("⚡ SKU Velocity vs Stock Level (Jumia Matrix)")
        fig_jumia = px.scatter(
            df_analysis,
            x="CurrentStock",
            y="SimulatedVelocity",
            color="SKU_Tier",
            size="CapitalTiedUp",
            hover_data=["ItemName", "DaysLeft", "ABC_XYZ_Matrix"],
        )
        st.plotly_chart(fig_jumia, use_container_width=True)
      st.markdown("---")

      st.subheader(
          "🔮 Master Enterprise Supply Chain & Replenishment Dashboard"
      )
      display_cols = [
          c
          for c in [
              "ItemName",
              "ABC_Category",
              "XYZ_Class",
              "ABC_XYZ_Matrix",
              "SKU_Tier",
              "CurrentStock",
              "SafetyStock",
              "ReorderPoint",
              "DaysLeft",
              "StockoutDate",
              "CapitalTiedUp",
              "RecommendedOrderQty",
              "EstimatedOrderCost",
          ]
          if c in df_analysis.columns
      ]
      st.dataframe(df_analysis[display_cols], use_container_width=True)

      st.markdown("---")
      st.subheader("📱 Automated Enterprise WhatsApp Supplier Dispatcher")
      reorder_needed_df = df_analysis[
          df_analysis["CurrentStock"] <= df_analysis["ReorderPoint"]
      ]
      if not reorder_needed_df.empty:
        selected_sku = st.selectbox(
            "Select SKU to Dispatch Purchase Order",
            reorder_needed_df["ItemName"].dropna().astype(str).tolist(),
        )
        sku_row = reorder_needed_df[
            reorder_needed_df["ItemName"] == selected_sku
        ].iloc[0]
        raw_phone = (
            sku_row["SupplierPhone"]
            if "SupplierPhone" in sku_row and pd.notna(sku_row["SupplierPhone"])
            else "233000000000"
        )
        digits_only = "".join(ch for ch in str(raw_phone) if ch.isdigit())
        sup_phone = digits_only if digits_only else "233000000000"
        order_msg = (
            "Hello Supplier, automated reorder generated by Rogers Enterprise"
            f" Suite. Item: *{sku_row['ItemName']}* | Matrix:"
            f" *{sku_row['ABC_XYZ_Matrix']}* | Restock Quantity Required:"
            f" *{sku_row['RecommendedOrderQty']} units* | Estimated PO Cost:"
            f" *GHS {sku_row['EstimatedOrderCost']:,.2f}*. Please confirm"
            " fulfillment."
        )
        encoded_po = urllib.parse.quote(order_msg)
        po_wa_url = f"https://wa.me/{sup_phone}?text={encoded_po}"
        col_p1, col_p2 = st.columns([2, 1])
        with col_p1:
          st.info(
              f"**Item:** {sku_row['ItemName']} | **Matrix:**"
              f" {sku_row['ABC_XYZ_Matrix']} | **Current Stock:**"
              f" {sku_row['CurrentStock']} | **Reorder Point (ROP):**"
              f" {sku_row['ReorderPoint']} | **Order Qty:**"
              f" {sku_row['RecommendedOrderQty']} units"
          )
        with col_p2:
          st.markdown(
              f'<a href="{po_wa_url}" target="_blank" style="display:block;'
              " text-align:center; background-color:#25D366; color:white;"
              " padding:12px 15px; border-radius:6px; text-decoration:none;"
              ' font-weight:bold; font-size:14px;">💬 Send WhatsApp PO</a>',
              unsafe_allow_html=True,
          )
      else:
        st.success(
            "🎉 All inventory SKUs are safely above their Reorder Points (ROP)!"
        )

      st.markdown("---")
      st.subheader("📥 Export Enterprise Intelligence Report")
      csv_bytes = df_analysis.to_csv(index=False).encode("utf-8")
      st.download_button(
          label="Download Enterprise Inventory & ABC-XYZ Report (CSV)",
          data=csv_bytes,
          file_name="enterprise_bi_inventory_report.csv",
          mime="text/csv",
      )
  except Exception as e:
    st.error(f"Error processing files: {e}")
else:
  st.info(
      "👈 Please upload your Sales CSV and Current Stock CSV in the sidebar to"
      " launch the Enterprise BI Suite."
  )

# ---------------------------------------------------------
# MODULE 2: UNIVERSAL CSV / EXCEL MULTITASK DATA LAB
# ---------------------------------------------------------
st.markdown("---")
st.header("🧪 Universal CSV & Excel Multitask Data Lab")
st.markdown(
    "Upload **any CSV or Excel file** (`.csv`, `.xlsx`, `.xls`) to multitask"
    " analysis, flag dangers/abnormalities, command cleaning or keep raw"
    " formatting, format dates on button click, highlight fixed areas, run"
    " swift statistical, correlation & regression calculations, and get **AI Data"
    " Diagnosis for Future Outlook**."
)

lab_file = st.file_uploader(
    "Upload Any CSV or Excel File for Multitask Analysis",
    type=["csv", "xlsx", "xls"],
    key="universal_lab_file",
)

if lab_file is not None:
  file_sig = f"{lab_file.name}_{lab_file.size}"
  if st.session_state.get("lab_file_sig") != file_sig:
    st.session_state["lab_file_sig"] = file_sig
    st.session_state["lab_working_df"] = load_any_tabular_file(
        lab_file, key_prefix="lab_main"
    )
    st.session_state["lab_fix_highlights"] = []

  if "lab_working_df" not in st.session_state:
    st.session_state["lab_working_df"] = load_any_tabular_file(
        lab_file, key_prefix="lab_main"
    )
    st.session_state["lab_fix_highlights"] = []

  work_df = st.session_state["lab_working_df"].copy()

  k1, k2, k3, k4 = st.columns(4)
  k1.metric("Rows", f"{len(work_df):,}")
  k2.metric("Columns", f"{len(work_df.columns):,}")
  k3.metric("Missing Cells", f"{int(work_df.isna().sum().sum()):,}")
  try:
    dup_total = int(work_df.duplicated().sum())
  except Exception:
    dup_total = 0
  k4.metric("Duplicate Rows", f"{dup_total:,}")

  st.subheader("🚨 Dangers & Abnormalities Scanner")
  df_flags = detect_abnormalities(work_df)
  if not df_flags.empty:
    st.dataframe(df_flags, use_container_width=True)
  else:
    st.success(
        "✅ No major dangers, outliers, duplicates, or missing-value"
        " abnormalities detected."
    )

  st.subheader("🧭 Formatting Prompts & Action Commands")
  st.caption(
      "Review areas prompted for formatting and choose **Clean & Format** or"
      " **Do Not Clean (Skip)** for each item before executing."
  )

  col_headers_dirty = [
      c for c in work_df.columns if str(c) != " ".join(str(c).strip().split())
  ]
  text_cols = [
      c
      for c in work_df.columns
      if work_df[c].dtype == object or pd.api.types.is_string_dtype(work_df[c])
  ]
  numeric_candidate_cols = []
  for c in text_cols:
    num_s = clean_numeric_series(work_df[c])
    non_blank = work_df[c].apply(clean_text_cell).dropna()
    if len(non_blank) > 0 and (num_s.notna().sum() / len(non_blank)) >= 0.7:
      numeric_candidate_cols.append(c)

  with st.form("format_command_form"):
    cmd_headers = (
        st.radio(
            "1. Column Headers Whitespace & Casing:",
            [
                "Clean & Format (Trim spaces)",
                "Do Not Clean (Keep as-is)",
            ],
            index=0 if col_headers_dirty else 1,
            horizontal=True,
        )
        if col_headers_dirty
        else "Do Not Clean (Keep as-is)"
    )

    cmd_text_trim = st.radio(
        "2. Text Fields Extra Spaces & Blank Strings ('nan'/'None'):",
        ["Clean & Format", "Do Not Clean (Skip)"],
        index=0 if text_cols else 1,
        horizontal=True,
    )

    cmd_num_parse = (
        st.radio(
            "3. Currency / Comma-Formatted Numeric Text"
            f" ({', '.join(str(x) for x in numeric_candidate_cols[:4])}):",
            ["Clean & Format to Numbers", "Do Not Clean (Keep as text)"],
            index=0,
            horizontal=True,
        )
        if numeric_candidate_cols
        else "Do Not Clean (Keep as text)"
    )

    cmd_dedup = st.radio(
        "4. Exact Duplicate Rows:",
        ["Do Not Clean (Keep duplicates)", "Clean & Remove Duplicates"],
        index=0,
        horizontal=True,
    )

    apply_clean_cmd = st.form_submit_button("⚡ Execute Selected Clean Commands")

  if apply_clean_cmd:
    applied_notes = []
    if col_headers_dirty and cmd_headers.startswith("Clean"):
      rename_map = {
          c: " ".join(str(c).strip().split()) for c in col_headers_dirty
      }
      work_df = work_df.rename(columns=rename_map)
      applied_notes.append(
          f"Formatted {len(rename_map)} column header(s):"
          f" {list(rename_map.values())}"
      )

    if cmd_text_trim.startswith("Clean"):
      for c in work_df.columns:
        if (
            work_df[c].dtype == object
            or pd.api.types.is_string_dtype(work_df[c])
        ):
          work_df[c] = work_df[c].apply(clean_text_cell)
      applied_notes.append(
          "Trimmed extra whitespace and standardized blank text markers across"
          " text columns."
      )

    if numeric_candidate_cols and cmd_num_parse.startswith("Clean"):
      for c in numeric_candidate_cols:
        if c in work_df.columns:
          work_df[c] = clean_numeric_series(work_df[c])
      applied_notes.append(
          "Converted currency/symbol numeric strings to clean numbers in:"
          f" {numeric_candidate_cols}"
      )

    if cmd_dedup.startswith("Clean & Remove"):
      before_len = len(work_df)
      work_df = work_df.drop_duplicates().reset_index(drop=True)
      removed_cnt = before_len - len(work_df)
      applied_notes.append(f"Removed {removed_cnt} exact duplicate row(s).")

    st.session_state["lab_working_df"] = work_df
    st.session_state["lab_fix_highlights"] = (
        st.session_state.get("lab_fix_highlights", []) + applied_notes
    )
    st.rerun()

  st.subheader("📅 On-Demand Date Formatter")
  date_col_candidates = list(work_df.columns)
  default_date_idx = next(
      (
          i
          for i, c in enumerate(date_col_candidates)
          if any(k in str(c).lower() for k in ["date", "time", "dt", "day"])
      ),
      0,
  )
  d_col1, d_col2, d_col3 = st.columns([2, 2, 1])
  selected_date_col = d_col1.selectbox(
      "Select Column to Format as Date",
      date_col_candidates,
      index=default_date_idx,
      key="lab_date_col",
  )
  target_date_fmt = d_col2.selectbox(
      "Output Date Format",
      ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%b %d, %Y", "%Y-%m-%d %H:%M"],
      index=0,
      key="lab_date_fmt",
  )
  with d_col3:
    st.markdown("<br>", unsafe_allow_html=True)
    format_date_btn = st.button(
        "🗓️ Format Dates", key="btn_format_dates", use_container_width=True
    )

  if format_date_btn:
    cleaned_d = work_df[selected_date_col].apply(clean_text_cell)
    parsed = pd.to_datetime(cleaned_d, errors="coerce")
    valid_parsed = int(parsed.notna().sum())
    formatted_series = cleaned_d.astype(object)
    valid_mask = parsed.notna()
    formatted_series.loc[valid_mask] = parsed.loc[valid_mask].dt.strftime(
        target_date_fmt
    )
    work_df[selected_date_col] = formatted_series
    st.session_state["lab_working_df"] = work_df
    note = (
        f"Formatted column **'{selected_date_col}'** to `{target_date_fmt}`"
        f" ({valid_parsed:,} date value(s) standardized)."
    )
    st.session_state["lab_fix_highlights"] = (
        st.session_state.get("lab_fix_highlights", []) + [note]
    )
    st.rerun()

  if st.session_state.get("lab_fix_highlights"):
    st.markdown("#### ✨ Highlighted Fixes & Formatting Applied")
    for item_note in st.session_state["lab_fix_highlights"]:
      st.warning(f"🎯 **Fixed & Formatted:** {item_note}")

  st.dataframe(work_df, use_container_width=True)

  st.markdown("---")
  st.subheader(
      "📐 Swift Measurements, Correlation & Regression Calculator (Mean,"
      " Average, Median, COUNT, COUNTA, COUNTIFS)"
  )

  numeric_cols_now = [
      c
      for c in work_df.columns
      if (
          pd.api.types.is_numeric_dtype(work_df[c])
          and not pd.api.types.is_bool_dtype(work_df[c])
      )
      or clean_numeric_series(work_df[c]).notna().sum() > 0
  ]

  if numeric_cols_now:
    swift_col = st.selectbox(
        "Select Target Column for Swift Measurements",
        numeric_cols_now,
        key="swift_target_col",
    )
    num_series = clean_numeric_series(work_df[swift_col])
    non_empty_counta = int(work_df[swift_col].apply(clean_text_cell).notna().sum())
    numeric_count = int(num_series.notna().sum())
    mean_val = float(num_series.mean()) if numeric_count > 0 else 0.0
    median_val = float(num_series.median()) if numeric_count > 0 else 0.0

    st.markdown("**COUNTIFS Multi-Condition Counter**")
    cf1, cf2, cf3, cf4 = st.columns(4)
    cond1_col = cf1.selectbox(
        "Condition 1 Column", work_df.columns, key="cifs_col1"
    )
    cond1_val = cf2.text_input(
        "Condition 1 Value (equals)", key="cifs_val1"
    ).strip()
    cond2_col = cf3.selectbox(
        "Condition 2 Column", work_df.columns, key="cifs_col2"
    )
    cond2_val = cf4.text_input(
        "Condition 2 Value (optional)", key="cifs_val2"
    ).strip()

    mask_cifs = pd.Series(True, index=work_df.index)
    if cond1_val:
      mask_cifs = mask_cifs & (
          work_df[cond1_col]
          .apply(clean_text_cell)
          .fillna("")
          .astype(str)
          .str.lower()
          == cond1_val.lower()
      )
    if cond2_val:
      mask_cifs = mask_cifs & (
          work_df[cond2_col]
          .apply(clean_text_cell)
          .fillna("")
          .astype(str)
          .str.lower()
          == cond2_val.lower()
      )
    countifs_result = int(mask_cifs.sum()) if (cond1_val or cond2_val) else 0

    s1, s2, s3, s4, s5, s6 = st.columns(6)
    s1.metric("Mean", f"{mean_val:,.2f}")
    s2.metric("Average", f"{mean_val:,.2f}")
    s3.metric("Median", f"{median_val:,.2f}")
    s4.metric("COUNT (Numeric)", f"{numeric_count:,}")
    s5.metric("COUNTA (Non-Empty)", f"{non_empty_counta:,}")
    s6.metric("COUNTIFS", f"{countifs_result:,}")

    st.markdown("---")
    st.markdown("**📈 Linear Regression Calculator ($Y = mX + b$)**")
    if len(numeric_cols_now) >= 2:
      rc1, rc2, rc3 = st.columns(3)
      x_col = rc1.selectbox(
          "Independent Variable (X)",
          numeric_cols_now,
          index=0,
          key="reg_x_col",
      )
      y_col = rc2.selectbox(
          "Dependent Variable (Y)",
          numeric_cols_now,
          index=min(1, len(numeric_cols_now) - 1),
          key="reg_y_col",
      )
      x_vals = clean_numeric_series(work_df[x_col])
      y_vals = clean_numeric_series(work_df[y_col])
      reg_df = pd.DataFrame({"x": x_vals, "y": y_vals}).dropna()

      if len(reg_df) >= 2 and reg_df["x"].nunique() > 1:
        slope, intercept = np.polyfit(reg_df["x"], reg_df["y"], 1)
        y_pred = slope * reg_df["x"] + intercept
        ss_res = np.sum((reg_df["y"] - y_pred) ** 2)
        ss_tot = np.sum((reg_df["y"] - reg_df["y"].mean()) ** 2)
        r_squared = float(1.0 - (ss_res / ss_tot)) if ss_tot > 0 else 0.0

        predict_x = rc3.number_input(
            f"Predict {y_col} for {x_col} =",
            value=float(reg_df["x"].mean()),
            key="reg_pred_x",
        )
        predicted_y = float(slope * predict_x + intercept)

        rm1, rm2, rm3, rm4 = st.columns(4)
        rm1.metric("Slope (m)", f"{slope:,.4f}")
        rm2.metric("Intercept (b)", f"{intercept:,.4f}")
        rm3.metric("R² Score", f"{r_squared:,.4f}")
        rm4.metric(f"Predicted {y_col}", f"{predicted_y:,.2f}")

        fig_reg = go.Figure()
        fig_reg.add_trace(
            go.Scatter(
                x=reg_df["x"],
                y=reg_df["y"],
                mode="markers",
                name="Actual Data",
            )
        )
        sorted_x = np.sort(reg_df["x"])
        fig_reg.add_trace(
            go.Scatter(
                x=sorted_x,
                y=slope * sorted_x + intercept,
                mode="lines",
                name=f"Fit: y = {slope:.2f}x + {intercept:.2f}",
            )
        )
        fig_reg.update_layout(
            xaxis_title=str(x_col),
            yaxis_title=str(y_col),
            title=f"Regression Trend: {y_col} vs {x_col}",
        )
        st.plotly_chart(fig_reg, use_container_width=True)

        # Pairwise correlation ranking across numeric features
        clean_num_frame = pd.DataFrame(
            {str(c): clean_numeric_series(work_df[c]) for c in numeric_cols_now}
        ).dropna(how="all")
        if len(clean_num_frame.columns) >= 2 and len(clean_num_frame) >= 2:
          corr_matrix = clean_num_frame.corr(numeric_only=True)
          st.markdown("**🔗 Numeric Correlation Matrix**")
          st.dataframe(corr_matrix.round(3), use_container_width=True)
      else:
        st.info(
            "Need at least 2 valid numeric pairs with variation in X to fit a"
            " regression line."
        )
    else:
      st.info(
          "Upload or clean at least two numeric columns to run linear"
          " regression."
      )

    st.markdown("---")
    st.subheader("🔮 AI Data Diagnosis & Future Trajectory Outlook")
    st.caption(
        "Combines data health scoring, linear momentum, and Holt trend"
        " smoothing to diagnose future behavior across your dataset."
    )

    ai_col1, ai_col2 = st.columns([2, 1])
    diag_target_col = ai_col1.selectbox(
        "Select Metric Column for Future Diagnosis",
        numeric_cols_now,
        key="ai_diag_col",
    )
    horizon_steps = ai_col2.slider(
        "Future Periods Ahead",
        min_value=1,
        max_value=30,
        value=7,
        key="ai_horizon",
    )

    diag_series = (
        clean_numeric_series(work_df[diag_target_col])
        .dropna()
        .reset_index(drop=True)
    )
    total_cells = max(1, len(work_df) * max(1, len(work_df.columns)))
    missing_pct = (int(work_df.isna().sum().sum()) / total_cells) * 100.0
    dup_pct = (dup_total / max(1, len(work_df))) * 100.0

    health_score = (
        100.0 - min(40.0, missing_pct * 1.5) - min(25.0, dup_pct * 2.0)
    )
    if len(diag_series) >= 4:
      q1_d, q3_d = np.percentile(diag_series, [25, 75])
      iqr_d = float(q3_d - q1_d)
      if iqr_d > 0:
        out_ratio = (
            (diag_series < q1_d - 1.5 * iqr_d)
            | (diag_series > q3_d + 1.5 * iqr_d)
        ).mean() * 100.0
        health_score -= min(25.0, float(out_ratio) * 1.5)
    health_score = max(0.0, min(100.0, health_score))

    if len(diag_series) >= 2:
      t_idx = np.arange(len(diag_series), dtype=float)
      m_trend, b_trend = np.polyfit(t_idx, diag_series.values, 1)
      holt_vals = compute_exponential_forecast(
          diag_series, steps=horizon_steps
      )
      linear_future = np.maximum(
          0.0,
          m_trend
          * np.arange(
              len(diag_series), len(diag_series) + horizon_steps, dtype=float
          )
          + b_trend,
      )
      blended_future = 0.5 * holt_vals + 0.5 * linear_future
      avg_future = float(np.mean(blended_future))
      end_future = float(blended_future[-1])
      current_last = float(diag_series.iloc[-1])

      if m_trend > 0.01:
        outlook_label = "📈 Upward Growth Trend"
      elif m_trend < -0.01:
        outlook_label = "📉 Downward / Depleting Trend"
      else:
        outlook_label = "➡️ Flat / Stable Trend"

      d1, d2, d3, d4 = st.columns(4)
      d1.metric("AI Data Health Score", f"{health_score:.0f} / 100")
      d2.metric("Future Direction", outlook_label)
      d3.metric(f"Avg Next {horizon_steps} Periods", f"{avg_future:,.2f}")
      d4.metric(
          f"Period +{horizon_steps} Forecast",
          f"{end_future:,.2f}",
          delta=f"{end_future - current_last:+,.2f} vs latest",
      )

      st.markdown("**🧠 AI Diagnostic Findings & Future Watchpoints:**")
      if missing_pct > 5:
        st.warning(
            f"• **Data Completeness Risk:** {missing_pct:.1f}% of cells are"
            " missing—future forecasts may shift once blanks are resolved."
        )
      if dup_pct > 0:
        st.warning(
            f"• **Duplicate Inflation Risk:** {dup_pct:.1f}% duplicate rows"
            " detected—duplicate records can artificially inflate future"
            " averages."
        )
      if m_trend < -0.01:
        st.error(
            f"• **Future Contraction / Depletion Alert:** `{diag_target_col}`"
            f" is trending downward by `{m_trend:,.2f}` per record. Review"
            " stock, cashflow, or demand drivers early."
        )
      elif m_trend > 0.01:
        st.success(
            f"• **Future Expansion Opportunity:** `{diag_target_col}` is"
            f" trending upward by `+{m_trend:,.2f}` per record. Prepare"
            " capacity, stock buffers, or budget ahead of demand."
        )
      else:
        st.info(
            f"• **Steady Baseline:** `{diag_target_col}` shows minimal slope"
            " change; expect near-term figures to stay close to recent"
            " averages."
        )

      hist_df = pd.DataFrame({
          "Step": np.arange(1, len(diag_series) + 1),
          "Value": diag_series.values,
          "Series": "Historical",
      })
      fut_df = pd.DataFrame({
          "Step": np.arange(
              len(diag_series) + 1, len(diag_series) + horizon_steps + 1
          ),
          "Value": blended_future,
          "Series": "AI Blended Forecast (Holt + Linear)",
      })
      plot_outlook_df = pd.concat([hist_df, fut_df], ignore_index=True)
      fig_outlook = px.line(
          plot_outlook_df,
          x="Step",
          y="Value",
          color="Series",
          markers=True,
          title=(
              f"AI Future Trajectory for '{diag_target_col}' (+{horizon_steps}"
              " Periods)"
          ),
      )
      st.plotly_chart(fig_outlook, use_container_width=True)
    else:
      st.info(
          "Need at least 2 numeric values in the selected column to project a"
          " future trend."
      )
  else:
    st.info(
        "No numeric columns detected yet. Use the formatting commands above if"
        " numbers are stored as text."
    )

  st.download_button(
      label="📥 Download Cleaned & Formatted Data (CSV)",
      data=work_df.to_csv(index=False).encode("utf-8"),
      file_name=f"cleaned_{str(lab_file.name).rsplit('.', 1)[0]}.csv",
      mime="text/csv",
      key="lab_download_cleaned",
  )
