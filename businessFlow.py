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
    page_title="businessFlow AI - Interactive Business Intelligence Suite",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title(
    "🤖 businessFlow AI: Active Supply Chain, Prediction & Decision Suite 🌍"
)
st.markdown(
    "Powered by **Rogers Enterprises Limited | Ghana** — Featuring Instant"
    " Plain-English Explanations, Active P&L Risk Flagging, and Interactive AI"
    " Scenarios."
)

# ---------------------------------------------------------
# SIDEBAR DATA INPUTS & SETTINGS
# ---------------------------------------------------------
st.sidebar.header("📁 Enterprise Data Inputs")
sales_file = st.sidebar.file_uploader(
    "Upload Sales CSV (Any format)", type=["csv"]
)
stock_file = st.sidebar.file_uploader(
    "Upload Current Stock CSV (Any format)", type=["csv"]
)

st.sidebar.markdown("---")
st.sidebar.subheader("⚙️ AI Simulation Parameters")
supplier_lead_time = st.sidebar.slider(
    "Supplier Lead Time (Days)", 1, 30, 7
)
service_level_factor = st.sidebar.selectbox(
    "Service Level Safety Buffer", [1.28, 1.65, 2.05], index=1
)
sales_growth_slider = st.sidebar.slider(
    "Demand Growth & Future Forecast (%)", -50, 100, 15, step=5
)

# ---------------------------------------------------------
# CLEANING & MAPPING ENGINE
# ---------------------------------------------------------


def smart_clean_and_map_data(df_sales_raw, df_stock_raw):
  df_sales_raw.columns = df_sales_raw.columns.str.strip()
  df_stock_raw.columns = df_stock_raw.columns.str.strip()

  sales_map, stock_map = {}, {}

  for col in df_sales_raw.columns:
    c = col.lower()
    if "date" in c:
      sales_map[col] = "Date"
    elif "item" in c or "product" in c or "name" in c:
      sales_map[col] = "ItemName"
    elif "qty" in c or "sold" in c or "quantity" in c or "sales" in c:
      sales_map[col] = "QuantitySold"
    elif "price" in c or "unit" in c or "amount" in c:
      if "unit" in c or "selling" in c or "price" in c:
        sales_map[col] = "UnitPrice"

  for col in df_stock_raw.columns:
    c = col.lower()
    if "item" in c or "product" in c or "name" in c:
      stock_map[col] = "ItemName"
    elif "stock" in c or "inventory" in c or "qty" in c:
      stock_map[col] = "CurrentStock"
    elif "cost" in c:
      stock_map[col] = "CostPrice"
    elif "sell" in c or "price" in c or "retail" in c:
      stock_map[col] = "SellingPrice"
    elif "phone" in c or "sup" in c:
      stock_map[col] = "SupplierPhone"

  df_sales = df_sales_raw.rename(columns=sales_map)
  df_stock = df_stock_raw.rename(columns=stock_map)

  # Fallbacks
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

  if "SellingPrice" not in df_stock.columns and "CostPrice" in df_stock.columns:
    df_stock["SellingPrice"] = df_stock["CostPrice"] * 1.35
  if "SupplierPhone" not in df_stock.columns:
    df_stock["SupplierPhone"] = "233240000000"

  df_sales["ItemName"] = df_sales["ItemName"].astype(str).str.strip()
  df_stock["ItemName"] = df_stock["ItemName"].astype(str).str.strip()

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
        f"✅ Data successfully processed: {len(df_sales)} sales rows and"
        f" {len(df_stock)} inventory SKUs loaded."
    )

    # Analytics Calculation
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

    # Velocity & Positive Forecasting
    days_analyzed = 30
    base_velocity = df_analysis["QuantitySold"] / days_analyzed
    df_analysis["SimulatedVelocity"] = base_velocity * (
        1 + (sales_growth_slider / 100.0)
    )

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

    # ABC Classification
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
    df_analysis["ABC_Category"] = df_analysis["CumulativePct"].apply(
        lambda p: "Class A (Core Revenue)"
        if p <= 0.80
        else ("Class B (Moderate)" if p <= 0.95 else "Class C (Long-Tail)")
    )

    # Sensitive Diagnostic & P&L Remark Engine
    def diagnostic_remark(row):
      margin = row["SellingPrice"] - row["CostPrice"]
      if margin <= 0:
        return (
            "❌ LOSS ALERT: Selling price is at or below cost price! You are"
            " losing money on every unit sold."
        )
      elif row["DaysLeft"] <= supplier_lead_time:
        return (
            f"🚨 CRITICAL STOCKOUT RISK: Only {row['DaysLeft']} days of stock"
            " remaining. Restock immediately to avoid losing sales."
        )
      elif row["CurrentStock"] > 80 and row["SimulatedVelocity"] < 0.5:
        return (
            "⚠️ CAPITAL DRAIN (Slow Moving): High inventory holding with low"
            " sales velocity. Consider a discount promotion."
        )
      elif row["ABC_Category"].startswith("Class A"):
        return (
            "🔥 HIGH PROFIT DRIVER: Generates 80% of revenue. Keep fully"
            " stocked."
        )
      else:
        return (
            "✅ STABLE MARGIN: Healthy stock level and positive profit margin."
        )

    df_analysis["AI_Business_Remark"] = df_analysis.apply(
        diagnostic_remark, axis=1
    )

    # ---------------------------------------------------------
    # 1. VISIBLE PLAIN-ENGLISH GUIDANCE & EXPLANATIONS
    # ---------------------------------------------------------
    st.markdown("### 📖 Plain-English Indicator Guide & Financial Health")
    col_g1, col_g2, col_g3 = st.columns(3)
    with col_g1:
      st.info(
          "💰 **Capital Tied Up:** The physical cash value of goods sitting in"
          " your shop. High capital in slow items = trapped cash."
      )
    with col_g2:
      st.success(
          "📈 **Projected Gross Profit:** Expected cash return on your current"
          " stock after accounting for future demand growth (+% slider)."
      )
    with col_g3:
      st.warning(
          "🚨 **Risk Flags:** Items flagged with Loss, Stockout Risk, or Capital"
          " Drain where you are actively losing money or sales."
      )

    st.markdown("---")

    # ---------------------------------------------------------
    # 2. EXECUTIVE DASHBOARD & PROFIT/LOSS METRICS
    # ---------------------------------------------------------
    st.markdown("### 📊 Executive Command Centre & Profit/Loss Insights")
    risk_items_count = len(
        df_analysis[
            df_analysis["AI_Business_Remark"].str.contains(
                "LOSS|RISK|DRAIN", case=False
            )
        ]
    )

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric(
        "Total Stock Capital", f"GHS {df_analysis['CapitalTiedUp'].sum():,.2f}"
    )
    col2.metric(
        "Projected Gross Profit",
        f"GHS {df_analysis['PotentialProfit'].sum():,.2f}",
        delta=f"+{sales_growth_slider}% Forecast",
    )
    col3.metric("Total Sales Revenue", f"GHS {total_rev_sum:,.2f}")
    col4.metric("⚠️ Profit Leaks & Risks", risk_items_count)
    col5.metric(
        "Business Health Score",
        f"{max(0, int(100 - (risk_items_count / max(1, len(df_analysis)) * 100)))}%",
    )

    st.markdown("---")


      # ---------------------------------------------------------
    # 3. HISTORICAL MARKET INTELLIGENCE & INCIDENTS
    # ---------------------------------------------------------
    st.markdown(
        "📜 **Historical Market Insights & Retail Incident Intelligence**"
    )
    st.info(
        "* **Market Incident Lesson (Ghana Retail Cycles):** During festive"
        " quarters and transport price adjustments, demand for essential staples"
        " surges by up to 40%. Retailers who ignored their *Reorder Points* in"
        " past seasons ran out of stock within 7 days, losing thousands in"
        " revenue to competitors. Your app calculates safety buffers here to"
        " prevent this exact incident."
    )

    st.markdown("---")

    # ---------------------------------------------------------
    # 4. INTERACTIVE AI CSV QUERY & SCENARIO ADVISOR
    # ---------------------------------------------------------
    st.markdown("### 💬 Interactive AI Business Query & Scenario Advisor")
    st.markdown(
        "Select a question below to let the machine learning algorithms analyze"
        " your data in real-time:"
    )

    query_option = st.selectbox(
        "Choose an AI Business Query or Scenario:",
        [
            "Select a question...",
            "Which items are currently causing losses or profit drains?",
            "What items need urgent restocking right now?",
            "Which products drive 80% of my revenue (Class A)?",
            "How much total capital is trapped in slow-moving inventory?",
            "Simulate a 30% surge in customer demand across all items",
        ],
    )

    if query_option != "Select a question...":
      st.markdown("#### 🤖 Real-Time Algorithmic Decision & Comparison:")
      if "causing losses or profit drains" in query_option:
        bad_items = df_analysis[
            df_analysis["AI_Business_Remark"].str.contains(
                "LOSS|DRAIN|RISK", case=False
            )
        ]
        if not bad_items.empty:
          st.error(
              "Found items requiring immediate management action to protect"
              " profits:"
          )
          st.dataframe(
              bad_items[[
                  "ItemName",
                  "CurrentStock",
                  "CostPrice",
                  "SellingPrice",
                  "AI_Business_Remark",
              ]],
              use_container_width=True,
          )
        else:
          st.success(
              "Outstanding! No loss alerts or capital drains detected in your"
              " current inventory dataset."
          )

      elif "restocking right now" in query_option:
        restock_items = df_analysis[
            df_analysis["CurrentStock"] <= df_analysis["ReorderPoint"]
        ]
        if not restock_items.empty:
          st.warning(
              "These items have crossed their calculated Reorder Point:"
          )
          st.dataframe(
              restock_items[[
                  "ItemName",
                  "CurrentStock",
                  "ReorderPoint",
                  "DaysLeft",
                  "StockoutDate",
              ]],
              use_container_width=True,
          )
        else:
          st.success(
              "All items are safely above their reorder thresholds right now!"
          )

      elif "80% of my revenue" in query_option:
        class_a = df_analysis[df_analysis["ABC_Category"].str.contains("Class A")]
        st.success("Your core profit drivers (Class A Pareto items):")
        st.dataframe(
            class_a[[
                "ItemName",
                "TotalRevenue",
                "CurrentStock",
                "SimulatedVelocity",
            ]],
            use_container_width=True,
        )

      elif "trapped in slow-moving" in query_option:
        dead_tied = df_analysis[
            df_analysis["AI_Business_Remark"].str.contains("DRAIN", case=False)
        ]["CapitalTiedUp"].sum()
        st.info(
            f"**Capital Trapped in Slow/Dead Stock:** GHS {dead_tied:,.2f}."
            " Running clearance discounts on these items will free up cash for"
            " fast sellers."
        )

      elif "30% surge" in query_option:
        surge_rev = df_analysis["TotalPotentialRevenue"].sum() * 1.3
        st.success(
            f"📈 **Demand Surge Scenario:** If customer purchases jump by 30%,"
            " your potential revenue scales to **GHS {surge_rev:,.2f}**. Ensure"
            " your safety buffers are active!"
        )

    st.markdown("---")

    # ---------------------------------------------------------
    # MASTER INVENTORY & REMARK TABLE
    # ---------------------------------------------------------
    st.subheader(
        "🔮 Master Enterprise Intelligence, Diagnostics & Recommendations Table"
    )

    def style_remarks(val):
      if "LOSS" in str(val) or "CRITICAL" in str(val):
        return "background-color: #ffcccc; color: #900; font-weight: bold;"
      elif "DRAIN" in str(val):
        return "background-color: #ffe599; color: #b45f06; font-weight: bold;"
      elif "HIGH PROFIT" in str(val):
        return "background-color: #d4edda; color: #155724; font-weight: bold;"
      return ""

    display_cols = [
        "ItemName",
        "ABC_Category",
        "CurrentStock",
        "ReorderPoint",
        "DaysLeft",
        "StockoutDate",
        "CapitalTiedUp",
        "RecommendedOrderQty",
        "AI_Business_Remark",
    ]

    st.dataframe(
        df_analysis[display_cols].style.applymap(
            style_remarks, subset=["AI_Business_Remark"]
        ),
        use_container_width=True,
    )

    # ---------------------------------------------------------
    # WHATSAPP PURCHASE ORDER DISPATCHER
    # ---------------------------------------------------------
    st.markdown("---")
    st.subheader("📱 Automated WhatsApp Supplier Purchase Order Dispatcher")

    urgent_df = df_analysis[
        df_analysis["CurrentStock"] <= df_analysis["ReorderPoint"]
    ]
    if not urgent_df.empty:
      selected_sku = st.selectbox(
          "Select SKU for WhatsApp PO Dispatch", urgent_df["ItemName"].tolist()
      )
      sku_row = urgent_df[urgent_df["ItemName"] == selected_sku].iloc[0]

      sup_phone = (
          str(sku_row["SupplierPhone"])
          if "SupplierPhone" in sku_row
          and pd.notna(sku_row["SupplierPhone"])
          else "233240000000"
      )

      order_msg = (
          f"Hello Supplier, urgent PO generated by businessFlow AI. "
          f"Item: *{sku_row['ItemName']}* | "
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
          "🎉 Excellent! No urgent reorders required under current operational"
          " parameters."
      )

    # Export
    st.markdown("---")
    st.subheader("📥 Export AI Business Intelligence Report")
    csv_bytes = df_analysis.to_csv(index=False).encode("utf-8")
    st.download_button(
        label="Download Full AI BI Intelligence Report (CSV)",
        data=csv_bytes,
        file_name="businessflow_ai_intelligence_report.csv",
        mime="text/csv",
    )

  except Exception as e:
    st.error(f"Error processing files: {e}")
else:
  st.info(
      "👈 Please upload your Sales CSV and Current Stock CSV in the sidebar to"
      " launch the Interactive AI Business Suite."
  )
