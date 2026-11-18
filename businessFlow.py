from datetime import datetime, timedelta
import urllib.parse
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# ---------------------------------------------------------
# PAGE CONFIGURATION
# ---------------------------------------------------------
st.set_page_config(
    page_title="businessFlow - Enterprise BI & Inventory Suite",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title(
    "⚡ businessFlow: Enterprise Supply Chain, Anomaly & BI Intelligence Suite"
    " 🌍"
)
st.markdown(
    "Powered by **Rogers Enterprises Limited | Ghana** — Featuring Index-Agnostic"
    " Data Cleaning, Anomaly Flagging, Predictive Forecasting, and Actionable"
    " Executive Options."
)

# ---------------------------------------------------------
# SIDEBAR DATA INPUTS & SETTINGS
# ---------------------------------------------------------
st.sidebar.header("📁 Enterprise Data Inputs")
sales_file = st.sidebar.file_uploader(
    "Upload Sales CSV (Any Index / Column naming format)", type=["csv"]
)
stock_file = st.sidebar.file_uploader(
    "Upload Current Stock CSV (Any Index / Column naming format)", type=["csv"]
)

st.sidebar.markdown("---")
st.sidebar.subheader("⚙️ Supply Chain Parameters")
supplier_lead_time = st.sidebar.slider(
    "Supplier Lead Time (Days)", 1, 30, 7
)
service_level_factor = st.sidebar.selectbox(
    "Service Level Safety Buffer", [1.28, 1.65, 2.05], index=1
)  # 1.65 = 95% protection
sales_growth_slider = st.sidebar.slider(
    "Demand Volatility / Positive Growth Forecast (%)", -50, 100, 10, step=5
)

# ---------------------------------------------------------
# INDEX-AGNOSTIC DATA CLEANING & MAPPING ENGINE
# ---------------------------------------------------------


def smart_clean_and_map_data(df_sales_raw, df_stock_raw):
  """Cleans raw files, strips whitespace, handles any index or header naming

  variations, and normalizes columns for enterprise BI processing.
  """
  # Clean column headers (strip spaces, lowercase for matching)
  df_sales_raw.columns = df_sales_raw.columns.str.strip()
  df_stock_raw.columns = df_stock_raw.columns.str.strip()

  # Flexible column mapping dictionaries
  sales_map = {}
  stock_map = {}

  for col in df_sales_raw.columns:
    col_lower = col.lower()
    if "date" in col_lower:
      sales_map[col] = "Date"
    elif "item" in col_lower or "product" in col_lower or "name" in col_lower:
      sales_map[col] = "ItemName"
    elif (
        "qty" in col_lower
        or "sold" in col_lower
        or "quantity" in col_lower
        or "sales" in col_lower
    ):
      sales_map[col] = "QuantitySold"
    elif (
        "price" in col_lower
        or "unit" in col_lower
        or "amount" in col_lower
        or "cost" in col_lower
    ):
      if "unit" in col_lower or "selling" in col_lower:
        sales_map[col] = "UnitPrice"

  for col in df_stock_raw.columns:
    col_lower = col.lower()
    if "item" in col_lower or "product" in col_lower or "name" in col_lower:
      stock_map[col] = "ItemName"
    elif (
        "stock" in col_lower
        or "inventory" in col_lower
        or "qty" in col_lower
        or "quantity" in col_lower
    ):
      stock_map[col] = "CurrentStock"
    elif "cost" in col_lower:
      stock_map[col] = "CostPrice"
    elif "sell" in col_lower or "price" in col_lower or "retail" in col_lower:
      stock_map[col] = "SellingPrice"
    elif "phone" in col_lower or "sup" in col_lower or "contact" in col_lower:
      stock_map[col] = "SupplierPhone"

  df_sales = df_sales_raw.rename(columns=sales_map)
  df_stock = df_stock_raw.rename(columns=stock_map)

  # Fallback checks if mapping missed primary keys
  if "ItemName" not in df_sales.columns and len(df_sales.columns) > 1:
    df_sales.rename(columns={df_sales.columns[1]: "ItemName"}, inplace=True)
  if "QuantitySold" not in df_sales.columns and len(df_sales.columns) > 2:
    df_sales.rename(
        columns={df_sales.columns[2]: "QuantitySold"}, inplace=True
    )
  if "UnitPrice" not in df_sales.columns and len(df_sales.columns) > 3:
    df_sales.rename(columns={df_sales.columns[3]: "UnitPrice"}, inplace=True)

  if "ItemName" not in df_stock.columns and len(df_stock.columns) > 0:
    df_stock.rename(columns={df_stock.columns[0]: "ItemName"}, inplace=True)
  if "CurrentStock" not in df_stock.columns and len(df_stock.columns) > 1:
    df_stock.rename(
        columns={df_stock.columns[1]: "CurrentStock"}, inplace=True
    )
  if "CostPrice" not in df_stock.columns and len(df_stock.columns) > 2:
    df_stock.rename(columns={df_stock.columns[2]: "CostPrice"}, inplace=True)

  # Fill mandatory missing columns if completely absent
  if "SellingPrice" not in df_stock.columns and "CostPrice" in df_stock.columns:
    df_stock["SellingPrice"] = df_stock["CostPrice"] * 1.35
  if "SupplierPhone" not in df_stock.columns:
    df_stock["SupplierPhone"] = "233240000000"

  # Clean string whitespace
  df_sales["ItemName"] = df_sales["ItemName"].astype(str).str.strip()
  df_stock["ItemName"] = df_stock["ItemName"].astype(str).str.strip()

  # Clean numerics
  for col in ["QuantitySold", "UnitPrice"]:
    if col in df_sales.columns:
      df_sales[col] = pd.to_numeric(
          df_sales[col].astype(str).str.replace(r"[^\d.]", "", regex=True),
          errors="coerce",
      ).fillna(0)

  for col in ["CurrentStock", "CostPrice", "SellingPrice"]:
    if col in df_stock.columns:
      df_stock[col] = pd.to_numeric(
          df_stock[col].astype(str).str.replace(r"[^\d.]", "", regex=True),
          errors="coerce",
      ).fillna(0)

  return df_sales, df_stock


if sales_file and stock_file:
  try:
    raw_sales = pd.read_csv(sales_file)
    raw_stock = pd.read_csv(stock_file)

    df_sales, df_stock = smart_clean_and_map_data(raw_sales, raw_stock)
    st.success(
        f"✅ Data successfully indexed, mapped, and cleaned! Processed"
        f" {len(df_sales)} sales rows and {len(df_stock)} stock SKUs."
    )

    # ---------------------------------------------------------
    # ANALYTICS ENGINE & ENTERPRISE CALCULATIONS
    # ---------------------------------------------------------
    df_sales["TotalRevenue"] = (
        df_sales["QuantitySold"] * df_sales["UnitPrice"]
    )
    item_sales = (
        df_sales.groupby("ItemName")
        .agg({"QuantitySold": "sum", "TotalRevenue": "sum"})
        .reset_index()
    )

    df_analysis = pd.merge(df_stock, item_sales, on="ItemName", how="left")
    df_analysis["QuantitySold"] = df_analysis["QuantitySold"].fillna(0)
    df_analysis["TotalRevenue"] = df_analysis["TotalRevenue"].fillna(0)

    df_analysis["TotalPotentialRevenue"] = (
        df_analysis["CurrentStock"] * df_analysis["SellingPrice"]
    )
    df_analysis["CapitalTiedUp"] = (
        df_analysis["CurrentStock"] * df_analysis["CostPrice"]
    )
    df_analysis["PotentialProfit"] = (
        df_analysis["TotalPotentialRevenue"] - df_analysis["CapitalTiedUp"]
    )

    # Velocity & Positive Forecast Simulation
    days_analyzed = 30
    base_velocity = df_analysis["QuantitySold"] / days_analyzed
    df_analysis["SimulatedVelocity"] = base_velocity * (
        1 + (sales_growth_slider / 100.0)
    )

    # Safety Stock & Reorder Point (ROP)
    df_analysis["SafetyStock"] = (
        df_analysis["SimulatedVelocity"]
        * 0.2
        * supplier_lead_time
        * service_level_factor
    ).apply(lambda x: max(1, int(round(x))))
    df_analysis["ReorderPoint"] = (
        df_analysis["SimulatedVelocity"] * supplier_lead_time
        + df_analysis["SafetyStock"]
    ).apply(lambda x: int(round(x)))

    # Days Until Stockout & Forecast Date
    df_analysis["DaysLeft"] = df_analysis.apply(
        lambda row: round(row["CurrentStock"] / row["SimulatedVelocity"], 1)
        if row["SimulatedVelocity"] > 0
        else 999,
        axis=1,
    )
    today = datetime.now()
    df_analysis["StockoutDate"] = df_analysis["DaysLeft"].apply(
        lambda d: (today + timedelta(days=int(d))).strftime("%Y-%m-%d")
        if d < 999
        else "No Risk"
    )

    # ABC Pareto Classification
    df_analysis = df_analysis.sort_values(
        by="TotalRevenue", ascending=False
    ).reset_index(drop=True)
    df_analysis["CumulativeRevenue"] = df_analysis["TotalRevenue"].cumsum()
    total_rev_sum = df_analysis["TotalRevenue"].sum()
    df_analysis["CumulativePct"] = (
        df_analysis["CumulativeRevenue"] / total_rev_sum
        if total_rev_sum > 0
        else 0
    )

    def classify_abc(pct):
      if pct <= 0.80:
        return "Class A (Core Revenue)"
      elif pct <= 0.95:
        return "Class B (Moderate)"
      else:
        return "Class C (Long-Tail)"

    df_analysis["ABC_Category"] = df_analysis["CumulativePct"].apply(
        classify_abc
    )

    # Anomaly, Danger & SKU Tiering Engine
    def detect_anomalies_and_tier(row):
      if row["CurrentStock"] <= row["ReorderPoint"]:
        if row["CurrentStock"] <= (row["ReorderPoint"] * 0.5):
          return "🚨 CRITICAL STOCK DANGER"
        return "⚠️ Below Reorder Point"
      elif (
          row["CurrentStock"] > 100
          and row["SimulatedVelocity"] < 0.1
          and row["CostPrice"] > 10
      ):
        return "🛑 ANOMALY: Dead Stock Capital Trap"
      elif row["SellingPrice"] <= row["CostPrice"]:
        return "❌ ANOMALY: Negative Margin Pricing"
      elif row["SimulatedVelocity"] >= 4:
        return "🔥 High-Velocity Best Seller"
      else:
        return "✅ Balanced SKU"

    df_analysis["SKU_Status_Anomaly"] = df_analysis.apply(
        detect_anomalies_and_tier, axis=1
    )

    # Recommended Order Quantity & Costs
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

    # ---------------------------------------------------------
    # EXECUTIVE KPI DASHBOARD & DANGER FLAGS
    # ---------------------------------------------------------
    st.markdown("### 📊 Executive KPI Command & Danger Flags")
    danger_count = len(
        df_analysis[
            df_analysis["SKU_Status_Anomaly"].str.contains(
                "CRITICAL|Danger|Trap|Negative", case=False
            )
        ]
    )

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Total Stock Capital", f"GHS {df_analysis['CapitalTiedUp'].sum():,.2f}")
    col2.metric(
        "Projected Gross Profit",
        f"GHS {df_analysis['PotentialProfit'].sum():,.2f}",
        delta=f"+{sales_growth_slider}% Growth Forecast",
    )
    col3.metric("Total Sales Revenue", f"GHS {total_rev_sum:,.2f}")
    col4.metric(
        "🚨 Critical Dangers & Anomalies",
        danger_count,
        delta_color="inverse" if danger_count > 0 else "normal",
    )
    col5.metric(
        "Inventory Health Score",
        f"{max(0, int(100 - (danger_count / max(1, len(df_analysis)) * 100)))}%",
    )

    st.markdown("---")

    # ---------------------------------------------------------
    # STRATEGIC OPTIONS & GREATER INSIGHTS
    # ---------------------------------------------------------
    st.markdown("### 💡 Greater Options & Strategic Recommendations")
    col_opt1, col_opt2, col_opt3 = st.columns(3)

    with col_opt1:
      st.info(
          "**🚀 Growth & Scaling Option:**\nCapitalize on positive demand"
          " forecasts (+% slider) by increasing stock allocation on **Class A**"
          " items to capture 100% of market share without stockouts."
      )
    with col_opt2:
      st.warning(
          "**⚠️ Dead Stock Mitigation:**\nFor items flagged as *Capital Traps*,"
          " execute bundle promotions or clearance sales to free up locked cash"
          " for high-velocity goods."
      )
    with col_opt3:
      st.success(
          "**🛡️ Supply Chain Resilience:**\nMaintain calculated **Safety"
          " Stock** buffers to absorb supplier delivery delays during peak"
          " retail seasons."
      )

    st.markdown("---")

    # ---------------------------------------------------------
    # PLOTLY VISUALIZATIONS
    # ---------------------------------------------------------
    col_ch1, col_ch2 = st.columns(2)
    with col_ch1:
      st.subheader("🌍 ABC Revenue Pareto Distribution")
      fig_abc = px.pie(
          df_analysis,
          names="ABC_Category",
          values="TotalRevenue",
          color="ABC_Category",
          color_discrete_map={
              "Class A (Core Revenue)": "#FF4B4B",
              "Class B (Moderate)": "#FFA500",
              "Class C (Long-Tail)": "#2ECC71",
          },
      )
      st.plotly_chart(fig_abc, use_container_width=True)

    with col_ch2:
      st.subheader("⚡ SKU Velocity vs Stock Level Matrix")
      fig_matrix = px.scatter(
          df_analysis,
          x="CurrentStock",
          y="SimulatedVelocity",
          color="SKU_Status_Anomaly",
          size="CapitalTiedUp",
          hover_data=["ItemName", "DaysLeft", "StockoutDate"],
      )
      st.plotly_chart(fig_matrix, use_container_width=True)

    st.markdown("---")

    # ---------------------------------------------------------
    # MASTER INVENTORY & ANOMALY TABLE
    # ---------------------------------------------------------
    st.subheader(
        "🔮 Master Enterprise Replenishment, Anomaly & Forecast Table"
    )

    def style_anomalies(val):
      if "CRITICAL" in str(val) or "Negative" in str(val):
        return "background-color: #ffcccc; color: #900; font-weight: bold;"
      elif "Anomaly" in str(val) or "Trap" in str(val):
        return "background-color: #ffe599; color: #b45f06; font-weight: bold;"
      elif "Below" in str(val):
        return "background-color: #fff3cd; color: #856404;"
      return "background-color: #d4edda; color: #155724;"

    display_cols = [
        "ItemName",
        "ABC_Category",
        "SKU_Status_Anomaly",
        "CurrentStock",
        "SafetyStock",
        "ReorderPoint",
        "DaysLeft",
        "StockoutDate",
        "CapitalTiedUp",
        "RecommendedOrderQty",
        "EstimatedOrderCost",
    ]

    st.dataframe(
        df_analysis[display_cols].style.applymap(
            style_anomalies, subset=["SKU_Status_Anomaly"]
        ),
        use_container_width=True,
    )

      # ---------------------------------------------------------
    # WHATSAPP ENTERPRISE PURCHASE ORDER DISPATCHER
    # ---------------------------------------------------------
    st.markdown("---")
    st.subheader("📱 Automated WhatsApp Supplier Purchase Order Dispatcher")

    risk_df = df_analysis[
        df_analysis["SKU_Status_Anomaly"].str.contains(
            "CRITICAL|Below", case=False
        )
    ]
    if not risk_df.empty:
      selected_sku = st.selectbox(
          "Select SKU for Urgent Restock PO", risk_df["ItemName"].tolist()
      )
      sku_row = risk_df[risk_df["ItemName"] == selected_sku].iloc[0]

      sup_phone = (
          str(sku_row["SupplierPhone"])
          if "SupplierPhone" in sku_row
          and pd.notna(sku_row["SupplierPhone"])
          else "233240000000"
      )

      order_msg = (
          f"Hello Supplier, urgent PO generated by businessFlow Enterprise. "
          f"Item: *{sku_row['ItemName']}* | ABC Class: *{sku_row['ABC_Category']}* | "
          f"Restock Qty Required: *{sku_row['RecommendedOrderQty']} units* | "
          f"Estimated PO Cost: *GHS {sku_row['EstimatedOrderCost']:,.2f}*. Please confirm delivery."
      )
      encoded_po = urllib.parse.quote(order_msg)
      po_wa_url = f"https://wa.me/{sup_phone}?text={encoded_po}"

      col_p1, col_p2 = st.columns([2, 1])
      with col_p1:
        st.info(
            f"**Selected Item:** {sku_row['ItemName']} | **Current Stock:**"
            f" {sku_row['CurrentStock']} | **Recommended Order:**"
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
          "🎉 Outstanding! No critical stockout risks or restock warnings"
          " detected under current parameters."
      )

    # ---------------------------------------------------------
    # EXPORT REPORT
    # ---------------------------------------------------------
    st.markdown("---")
    st.subheader("📥 Export Master Enterprise BI Report")
    csv_bytes = df_analysis.to_csv(index=False).encode("utf-8")
    st.download_button(
        label="Download Full Enterprise Anomaly & Forecast Report (CSV)",
        data=csv_bytes,
        file_name="businessflow_enterprise_master_report.csv",
        mime="text/csv",
    )

  except Exception as e:
    st.error(
        f"Error processing files. Please verify your CSV format or headers: {e}"
    )
else:
  st.info(
      "👈 Please upload your Sales CSV and Current Stock CSV in the sidebar to"
      " launch the master intelligence suite."
  )
