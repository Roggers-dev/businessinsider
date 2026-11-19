from datetime import datetime, timedelta
import hashlib
import os
import sqlite3
import urllib.parse
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

try:
  import psycopg2
  from psycopg2.extras import RealDictCursor

  PSYCOPG2_AVAILABLE = True
except ImportError:
  PSYCOPG2_AVAILABLE = False

# ---------------------------------------------------------
# DEVELOPER & PAGE CONFIGURATION
# ---------------------------------------------------------
DEVELOPER_ORG = "Rogers Enterprise Limited | Ghana"
DEVELOPER_EMAIL = "koffieroggers@gmail.com"
LOCAL_AUTH_DB = "rogers_bi_auth.db"

st.set_page_config(
    page_title="Enterprise AI Inventory & BI Command Centre",
    page_icon="🌍",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------------------------------------------------------
# NEON POSTGRES + SAFE LOCAL FALLBACK LAYER
# ---------------------------------------------------------
def get_neon_url() -> str:
  try:
    if "neon" in st.secrets and st.secrets["neon"].get("database_url"):
      return str(st.secrets["neon"]["database_url"]).strip()
  except Exception:
    pass
  try:
    if st.secrets.get("DATABASE_URL"):
      return str(st.secrets["DATABASE_URL"]).strip()
  except Exception:
    pass
  for env_key in ("NEON_DATABASE_URL", "DATABASE_URL"):
    val = os.getenv(env_key, "").strip()
    if val:
      return val
  return ""


def using_neon() -> bool:
  return bool(get_neon_url()) and PSYCOPG2_AVAILABLE


def get_local_sqlite_conn():
  conn = sqlite3.connect(LOCAL_AUTH_DB, check_same_thread=False)
  conn.row_factory = sqlite3.Row
  return conn


def init_auth_and_audit_storage():
  if using_neon():
    conn = psycopg2.connect(get_neon_url(), sslmode="require")
    try:
      with conn.cursor() as cur:
        cur.execute("""
                    CREATE TABLE IF NOT EXISTS app_users (
                        id SERIAL PRIMARY KEY,
                        full_name VARCHAR(150) NOT NULL,
                        email VARCHAR(180) UNIQUE NOT NULL,
                        organization VARCHAR(180) DEFAULT 'Rogers Enterprise Limited | Ghana',
                        password_hash TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_login TIMESTAMP
                    );
                """)
        cur.execute("""
                    CREATE TABLE IF NOT EXISTS clean_audit_logs (
                        id SERIAL PRIMARY KEY,
                        user_email VARCHAR(180) NOT NULL,
                        file_name VARCHAR(255),
                        overview_summary TEXT,
                        conclusion_text TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
      conn.commit()
    finally:
      conn.close()
  else:
    conn = get_local_sqlite_conn()
    try:
      conn.execute("""
                CREATE TABLE IF NOT EXISTS app_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    full_name TEXT NOT NULL,
                    email TEXT UNIQUE NOT NULL,
                    organization TEXT DEFAULT 'Rogers Enterprise Limited | Ghana',
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_login TEXT
                )
            """)
      conn.execute("""
                CREATE TABLE IF NOT EXISTS clean_audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_email TEXT NOT NULL,
                    file_name TEXT,
                    overview_summary TEXT,
                    conclusion_text TEXT,
                    created_at TEXT NOT NULL
                )
            """)
      conn.commit()
    finally:
      conn.close()


def hash_password(password: str, salt: str = None) -> str:
  if salt is None:
    salt = os.urandom(16).hex()
  dk = hashlib.pbkdf2_hmac(
      "sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000
  )
  return f"{salt}${dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
  if not stored_hash or "$" not in stored_hash:
    return False
  salt, expected_hex = stored_hash.split("$", 1)
  dk = hashlib.pbkdf2_hmac(
      "sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000
  )
  return dk.hex() == expected_hex


def register_user(full_name: str, email: str, password: str, org: str):
  norm_email = email.strip().lower()
  pw_hash = hash_password(password)
  now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  if using_neon():
    conn = psycopg2.connect(get_neon_url(), sslmode="require")
    try:
      with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app_users WHERE LOWER(email) = %s", (norm_email,)
        )
        if cur.fetchone():
          return False, "An account with this email already exists in Neon."
        cur.execute(
            """
                        INSERT INTO app_users (full_name, email, organization, password_hash, last_login)
                        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                    """,
            (
                full_name.strip(),
                norm_email,
                org.strip() or DEVELOPER_ORG,
                pw_hash,
            ),
        )
      conn.commit()
      return True, "Account created in Neon Postgres. You are now signed in."
    finally:
      conn.close()
  else:
    conn = get_local_sqlite_conn()
    try:
      existing = conn.execute(
          "SELECT id FROM app_users WHERE LOWER(email) = ?", (norm_email,)
      ).fetchone()
      if existing:
        return False, "An account with this email already exists."
      conn.execute(
          """
                INSERT INTO app_users (full_name, email, organization, password_hash, created_at, last_login)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
          (
              full_name.strip(),
              norm_email,
              org.strip() or DEVELOPER_ORG,
              pw_hash,
              now_str,
              now_str,
          ),
      )
      conn.commit()
      return (
          True,
          "Account created (local fallback active). You are now signed in.",
      )
    finally:
      conn.close()


def authenticate_user(email: str, password: str):
  norm_email = email.strip().lower()
  now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  if using_neon():
    conn = psycopg2.connect(get_neon_url(), sslmode="require")
    try:
      with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM app_users WHERE LOWER(email) = %s", (norm_email,)
        )
        user = cur.fetchone()
        if user and verify_password(password, user["password_hash"]):
          cur.execute(
              "UPDATE app_users SET last_login = CURRENT_TIMESTAMP WHERE id = %s",
              (user["id"],),
          )
          conn.commit()
          return True, dict(user)
      return False, None
    finally:
      conn.close()
  else:
    conn = get_local_sqlite_conn()
    try:
      row = conn.execute(
          "SELECT * FROM app_users WHERE LOWER(email) = ?", (norm_email,)
      ).fetchone()
      if row and verify_password(password, row["password_hash"]):
        conn.execute(
            "UPDATE app_users SET last_login = ? WHERE id = ?",
            (now_str, row["id"]),
        )
        conn.commit()
        return True, dict(row)
      return False, None
    finally:
      conn.close()


def record_clean_audit(
    user_email: str, file_name: str, overview_text: str, conclusion_text: str
):
  now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  try:
    if using_neon():
      conn = psycopg2.connect(get_neon_url(), sslmode="require")
      try:
        with conn.cursor() as cur:
          cur.execute(
              """
                            INSERT INTO clean_audit_logs (user_email, file_name, overview_summary, conclusion_text)
                            VALUES (%s, %s, %s, %s)
                        """,
              (user_email, file_name, overview_text, conclusion_text),
          )
        conn.commit()
      finally:
        conn.close()
    else:
      conn = get_local_sqlite_conn()
      try:
        conn.execute(
            """
                    INSERT INTO clean_audit_logs (user_email, file_name, overview_summary, conclusion_text, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """,
            (user_email, file_name, overview_text, conclusion_text, now_str),
        )
        conn.commit()
      finally:
        conn.close()
  except Exception:
    pass


# ---------------------------------------------------------
# SHARED SAFE DATA HELPERS
# ---------------------------------------------------------
def clean_text_cell(v):
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


def find_col_case_insensitive(df: pd.DataFrame, target: str):
  target_low = target.strip().lower()
  for col in df.columns:
    if str(col).strip().lower() == target_low:
      return col
  return None


def load_any_tabular_file(uploaded_file, key_prefix="lab"):
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
  else:
    uploaded_file.seek(0)
    xls = pd.ExcelFile(uploaded_file)
    sheet_name = st.selectbox(
        "Select Excel Sheet", xls.sheet_names, key=f"{key_prefix}_excel_sheet"
    )
    uploaded_file.seek(0)
    return pd.read_excel(uploaded_file, sheet_name=sheet_name)


def snapshot_metrics(df: pd.DataFrame) -> dict:
  rows_cnt = int(len(df))
  cols_cnt = int(len(df.columns))
  missing_cnt = int(df.isna().sum().sum()) if rows_cnt > 0 else 0
  try:
    dup_cnt = int(df.duplicated().sum()) if rows_cnt > 0 else 0
  except Exception:
    dup_cnt = 0

  numeric_ready_cols = 0
  for c in df.columns:
    num_s = clean_numeric_series(df[c])
    non_blank = df[c].apply(clean_text_cell).notna().sum()
    if non_blank > 0 and (num_s.notna().sum() / non_blank) >= 0.7:
      numeric_ready_cols += 1

  return {
      "rows": rows_cnt,
      "cols": cols_cnt,
      "missing_cells": missing_cnt,
      "duplicates": dup_cnt,
      "numeric_ready_cols": numeric_ready_cols,
  }


# ---------------------------------------------------------
# INITIALIZE STORAGE & AUTH GATE
# ---------------------------------------------------------
init_auth_and_audit_storage()

if "auth_user" not in st.session_state:
  st.session_state.auth_user = None

if st.session_state.auth_user is None:
  st.title("🔐 Sign In / Sign Up — Enterprise BI Command Centre")
  st.markdown(
      f"**{DEVELOPER_ORG}** • Contact: `{DEVELOPER_EMAIL}`  \n"
      "Please **Sign In** or **Sign Up** to access supply chain analytics,"
      " universal CSV/Excel cleaning, regression, and AI future diagnosis."
  )
  db_badge = (
      "🟢 Connected to **Neon Postgres** (`sslmode=require`)"
      if using_neon()
      else (
          "🟡 Local SQLite Fallback Active (add `[neon] database_url` in"
          " `.streamlit/secrets.toml` to connect Neon)"
      )
  )
  st.info(db_badge)

  tab_login, tab_signup = st.tabs(["🔑 Sign In", "📝 Sign Up (Create Account)"])

  with tab_login:
    with st.form("login_form"):
      login_email = st.text_input("Email Address")
      login_pw = st.text_input("Password", type="password")
      submitted_login = st.form_submit_button("Sign In")
      if submitted_login:
        if not login_email.strip() or not login_pw:
          st.warning("Please enter both your email address and password.")
        else:
          try:
            ok, user_data = authenticate_user(login_email, login_pw)
            if ok and user_data:
              st.session_state.auth_user = user_data
              st.success(f"Welcome back, {user_data['full_name']}!")
              st.rerun()
            else:
              st.error("Invalid email or password.")
          except Exception as exc:
            st.error(f"Sign-in error: {exc}")

  with tab_signup:
    with st.form("signup_form"):
      reg_name = st.text_input("Full Name")
      reg_email = st.text_input("Email Address")
      reg_org = st.text_input("Business / Organization", value=DEVELOPER_ORG)
      reg_pw = st.text_input("Create Password (min. 6 characters)", type="password")
      reg_pw2 = st.text_input("Confirm Password", type="password")
      submitted_reg = st.form_submit_button("Create Account & Sign In")
      if submitted_reg:
        if not reg_name.strip() or not reg_email.strip() or len(reg_pw) < 6:
          st.warning(
              "Please enter your full name, a valid email, and a password of at"
              " least 6 characters."
          )
        elif reg_pw != reg_pw2:
          st.error("Passwords do not match.")
        else:
          try:
            ok, msg = register_user(reg_name, reg_email, reg_pw, reg_org)
            if ok:
              _, user_data = authenticate_user(reg_email, reg_pw)
              st.session_state.auth_user = user_data
              st.success(msg)
              st.rerun()
            else:
              st.error(msg)
          except Exception as exc:
            st.error(f"Sign-up error: {exc}")

  st.stop()


# ---------------------------------------------------------
# AUTHENTICATED WORKSPACE HEADER & SIDEBAR
# ---------------------------------------------------------
active_user = st.session_state.auth_user

with st.sidebar:
  st.success(
      f"👤 **{active_user['full_name']}**  \n`{active_user['email']}`  \n"
      + (
          "🟢 Neon Postgres Synced"
          if using_neon()
          else "🟡 Local Auth Fallback"
      )
  )
  if st.button("🚪 Sign Out", use_container_width=True):
    st.session_state.auth_user = None
    st.rerun()

st.title("🌍 Enterprise-Grade Supply Chain & BI Intelligence Suite")
st.markdown(
    "Inspired by **Amazon, Jumia, and Alibaba** operational architectures —"
    " Featuring ABC Pareto Classification, Safety Stock Buffers, SKU Tiering,"
    " Universal CSV/Excel Multitask Analysis, and AI Future Diagnosis.  \n"
    f"Empowered by **{DEVELOPER_ORG}** • `{DEVELOPER_EMAIL}`."
)

# ---------------------------------------------------------
# SIDEBAR DATA INPUTS & SETTINGS
# ---------------------------------------------------------
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
          .agg(QuantitySold=("_qty", "sum"), TotalRevenue=("TotalRevenue", "sum"))
          .rename(columns={"_item": "ItemName"})
      )

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

      def classify_abc(pct):
        if pct <= 0.80:
          return "Class A (High Value - Strict Control)"
        elif pct <= 0.95:
          return "Class B (Moderate Value)"
        else:
          return "Class C (Long-Tail / Bulk)"

      df_analysis["ABC_Category"] = df_analysis["CumulativePct"].apply(
          classify_abc
      )

      def sku_tier(row):
        if row["SimulatedVelocity"] >= 5:
          return "🔥 Best Seller (Fast Moving)"
        elif row["SimulatedVelocity"] >= 1:
          return "⚡ Steady Mover"
        elif row["CurrentStock"] > 10 and row["SimulatedVelocity"] < 0.2:
          return "⚠️ Dead Stock / Capital Trap"
        else:
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
            hover_data=["ItemName", "DaysLeft"],
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
            f" BI Suite. Item: *{sku_row['ItemName']}* | ABC Class:"
            f" *{str(sku_row['ABC_Category']).split()[0]}* | Restock Quantity"
            f" Required: *{sku_row['RecommendedOrderQty']} units* | Estimated PO"
            f" Cost: *GHS {sku_row['EstimatedOrderCost']:,.2f}*. Please confirm"
            " fulfillment."
        )
        encoded_po = urllib.parse.quote(order_msg)
        po_wa_url = f"https://wa.me/{sup_phone}?text={encoded_po}"
        col_p1, col_p2 = st.columns([2, 1])
        with col_p1:
          st.info(
              f"**Item:** {sku_row['ItemName']} | **Current Stock:**"
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
          label="Download Enterprise Inventory & ABC Report (CSV)",
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

    # =========================================================
# EXTRA FEATURE: UNIVERSAL CSV / EXCEL MULTITASK ANALYZER,
# ANOMALY FLAGGER, COMMANDED CLEANER, DATE FORMATTER,
# SWIFT MEASUREMENTS + REGRESSION & AI FUTURE DIAGNOSIS
# =========================================================
st.markdown("---")
st.header("🧪 Universal CSV & Excel Multitask Data Lab")
st.markdown(
    "Upload **any CSV or Excel file** (`.csv`, `.xlsx`, `.xls`) to multitask"
    " analysis, flag dangers/abnormalities, command cleaning or keep raw"
    " formatting, inspect a **post-clean overview**, compare **Before vs."
    " After formatting with conclusions**, format dates on button click, run"
    " swift statistical & regression calculations, and get **AI Data Diagnosis"
    " for Future Outlook**."
)


def detect_abnormalities(df: pd.DataFrame):
  flags = []
  if df.empty:
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


lab_file = st.file_uploader(
    "Upload Any CSV or Excel File for Multitask Analysis",
    type=["csv", "xlsx", "xls"],
    key="universal_lab_file",
)

if lab_file is not None:
  file_sig = f"{lab_file.name}_{lab_file.size}"
  if st.session_state.get("lab_file_sig") != file_sig:
    st.session_state["lab_file_sig"] = file_sig
    loaded_df = load_any_tabular_file(lab_file, key_prefix="lab_main")
    st.session_state["lab_working_df"] = loaded_df
    st.session_state["lab_initial_metrics"] = snapshot_metrics(loaded_df)
    st.session_state["lab_fix_highlights"] = []
    st.session_state["lab_clean_report"] = None

  if "lab_working_df" not in st.session_state:
    loaded_df = load_any_tabular_file(lab_file, key_prefix="lab_main")
    st.session_state["lab_working_df"] = loaded_df
    st.session_state["lab_initial_metrics"] = snapshot_metrics(loaded_df)
    st.session_state["lab_fix_highlights"] = []
    st.session_state["lab_clean_report"] = None

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

  # 1. DANGERS & ABNORMALITIES SCANNER
  st.subheader("🚨 Dangers & Abnormalities Scanner")
  df_flags = detect_abnormalities(work_df)
  if not df_flags.empty:
    st.dataframe(df_flags, use_container_width=True)
  else:
    st.success(
        "✅ No major dangers, outliers, duplicates, or missing-value"
        " abnormalities detected."
    )

  # 2. PROMPT AREAS NEEDING FORMATTING + CLEAN / DO NOT CLEAN COMMANDS
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
    before_snap = snapshot_metrics(work_df)
    applied_notes = []
    actions_taken = []

    if col_headers_dirty and cmd_headers.startswith("Clean"):
      rename_map = {
          c: " ".join(str(c).strip().split()) for c in col_headers_dirty
      }
      work_df = work_df.rename(columns=rename_map)
      msg = (
          f"Trimmed/normalized {len(rename_map)} header(s):"
          f" {list(rename_map.values())}"
      )
      applied_notes.append(msg)
      actions_taken.append(msg)

    if cmd_text_trim.startswith("Clean"):
      text_touched = 0
      for c in work_df.columns:
        if (
            work_df[c].dtype == object
            or pd.api.types.is_string_dtype(work_df[c])
        ):
          work_df[c] = work_df[c].apply(clean_text_cell)
          text_touched += 1
      msg = (
          f"Standardized whitespace and converted empty/placeholder strings to"
          f" true nulls across {text_touched} text column(s)."
      )
      applied_notes.append(msg)
      actions_taken.append(msg)

    if numeric_candidate_cols and cmd_num_parse.startswith("Clean"):
      num_converted = []
      for c in numeric_candidate_cols:
        if c in work_df.columns:
          work_df[c] = clean_numeric_series(work_df[c])
          num_converted.append(str(c))
      if num_converted:
        msg = (
            "Stripped currency/comma symbols and parsed numeric values in:"
            f" {', '.join(num_converted)}."
        )
        applied_notes.append(msg)
        actions_taken.append(msg)

    if cmd_dedup.startswith("Clean & Remove"):
      before_len = len(work_df)
      work_df = work_df.drop_duplicates().reset_index(drop=True)
      removed_cnt = before_len - len(work_df)
      msg = f"Removed {removed_cnt} exact duplicate row(s)."
      applied_notes.append(msg)
      actions_taken.append(msg)

    after_snap = snapshot_metrics(work_df)

    # Draw conclusions from Before vs. After
    conclusions = []
    if after_snap["duplicates"] < before_snap["duplicates"]:
      conclusions.append(
          f"Duplicate inflation was eliminated ({before_snap['duplicates']} →"
          f" {after_snap['duplicates']}), preventing double-counting in totals"
          " and averages."
      )
    if after_snap["numeric_ready_cols"] > before_snap["numeric_ready_cols"]:
      conclusions.append(
          f"Numeric readiness improved ({before_snap['numeric_ready_cols']} →"
          f" {after_snap['numeric_ready_cols']} numeric columns), enabling"
          " reliable statistical summaries, regression, and forecasting."
      )
    if after_snap["missing_cells"] > before_snap["missing_cells"]:
      conclusions.append(
          f"Hidden text placeholders ('nan'/'None'/blanks) were surfaced as"
          f" explicit missing values ({before_snap['missing_cells']} →"
          f" {after_snap['missing_cells']}), making data gaps transparent"
          " rather than disguised as text."
      )
    if not conclusions:
      conclusions.append(
          "Dataset structure and integrity remain consistent; selected rules"
          " were applied without distorting record counts."
      )

    overview_str = (
        "; ".join(actions_taken)
        if actions_taken
        else "No cleaning rules were selected ('Do Not Clean' chosen)."
    )
    conclusion_str = " ".join(conclusions)

    st.session_state["lab_working_df"] = work_df
    st.session_state["lab_fix_highlights"] = (
        st.session_state.get("lab_fix_highlights", []) + applied_notes
    )
    st.session_state["lab_clean_report"] = {
        "before": before_snap,
        "after": after_snap,
        "overview": overview_str,
        "conclusions": conclusions,
    }
    record_clean_audit(
        active_user["email"], lab_file.name, overview_str, conclusion_str
    )
    st.rerun()

  # BRIEF OVERVIEW + BEFORE VS AFTER COMPARISON + CONCLUSIONS
  if st.session_state.get("lab_clean_report"):
    rep = st.session_state["lab_clean_report"]
    st.markdown("### 📋 Post-Clean Overview, Before vs. After & Conclusions")
    st.info(f"**Brief Overview of What Formatting Did:** {rep['overview']}")

    b_m = rep["before"]
    a_m = rep["after"]
    comp_df = pd.DataFrame([
        {
            "Metric": "Row Count",
            "Before Formatting": b_m["rows"],
            "After Formatting": a_m["rows"],
            "Change": a_m["rows"] - b_m["rows"],
        },
        {
            "Metric": "Column Count",
            "Before Formatting": b_m["cols"],
            "After Formatting": a_m["cols"],
            "Change": a_m["cols"] - b_m["cols"],
        },
        {
            "Metric": "Missing / Null Cells",
            "Before Formatting": b_m["missing_cells"],
            "After Formatting": a_m["missing_cells"],
            "Change": a_m["missing_cells"] - b_m["missing_cells"],
        },
        {
            "Metric": "Exact Duplicate Rows",
            "Before Formatting": b_m["duplicates"],
            "After Formatting": a_m["duplicates"],
            "Change": a_m["duplicates"] - b_m["duplicates"],
        },
        {
            "Metric": "Numeric-Ready Columns",
            "Before Formatting": b_m["numeric_ready_cols"],
            "After Formatting": a_m["numeric_ready_cols"],
            "Change": a_m["numeric_ready_cols"] - b_m["numeric_ready_cols"],
        },
    ])
    st.dataframe(comp_df, use_container_width=True)

    st.markdown("**🔍 Key Conclusions:**")
    for c_line in rep["conclusions"]:
      st.success(f"• {c_line}")

  # 3. ON-DEMAND DATE FORMATTER BUTTON
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
    record_clean_audit(
        active_user["email"],
        lab_file.name,
        note,
        f"Date column '{selected_date_col}' standardized to {target_date_fmt} for chronological accuracy.",
    )
    st.rerun()

  # 4. HIGHLIGHT AREAS FIXED & FORMATTED
  if st.session_state.get("lab_fix_highlights"):
    st.markdown("#### ✨ Highlighted Fixes & Formatting Applied")
    for item_note in st.session_state["lab_fix_highlights"]:
      st.warning(f"🎯 **Fixed & Formatted:** {item_note}")

  st.dataframe(work_df, use_container_width=True)

    # 5. SWIFT MEASUREMENTS & REGRESSION CALCULATOR
  st.markdown("---")
  st.subheader(
      "📐 Swift Measurements & Regression Calculator (Mean, Average, Median,"
      " COUNT, COUNTA, COUNTIFS)"
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

    # 6. AI DATA DIAGNOSIS TO PREDICT FUTURE LOOK AT DATA
    st.markdown("---")
    st.subheader("🔮 AI Data Diagnosis & Future Trajectory Outlook")
    st.caption(
        "AI inspects data quality, trend momentum, and volatility to diagnose"
        " future behavior across your dataset."
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
      future_t = np.arange(
          len(diag_series), len(diag_series) + horizon_steps, dtype=float
      )
      future_vals = np.maximum(0.0, m_trend * future_t + b_trend)
      avg_future = float(np.mean(future_vals))
      end_future = float(future_vals[-1])
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
          "Value": future_vals,
          "Series": "AI Future Forecast",
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

# ---------------------------------------------------------
# FOOTER
# ---------------------------------------------------------
st.markdown("---")
st.caption(
    f"**{DEVELOPER_ORG}** • Support & Inquiries: `{DEVELOPER_EMAIL}`"
)
