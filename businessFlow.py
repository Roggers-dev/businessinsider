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
    page_title="Enterprise AI Inventory & BI Command Centre",
    page_icon="🌍",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("🌍 Enterprise-Grade Supply Chain & BI Intelligence Suite")
st.markdown(
    "Inspired by **Amazon, Jumia, and Alibaba** operational architectures —"
    " Featuring ABC Pareto Classification, Safety Stock Buffers, and SKU Tiering."
    " Empowered by **Rogers Enterprises Limited | Ghana**."
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
    "Upload Current Stock CSV (Columns: ItemName, CurrentStock, CostPrice, SellingPrice, SupplierPhone)",
    type=["csv"],
)

st.sidebar.markdown("---")
st.sidebar.subheader("⚙️ Supply Chain Parameters")
supplier_lead_time = st.sidebar.slider(
    "Supplier Lead Time (Days)", 1, 30, 7
)
service_level_factor = st.sidebar.selectbox(
    "Service Level Safety Buffer", [1.28, 1.65, 2.05], index=1
)  # 1.65 = 95% stockout protection
sales_growth_slider = st.sidebar.slider(
    "Demand Volatility / Growth Simulation (%)", -50, 100, 0, step=5
)

if sales_file and stock_file:
  try:
    df_sales = pd.read_csv(sales_file)
    df_stock = pd.read_csv(stock_file)

    # ---------------------------------------------------------
    # ENTERPRISE DATA PROCESSING ENGINE
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

    # Financial Metrics per SKU
    if "SellingPrice" not in df_analysis.columns:
      df_analysis["SellingPrice"] = df_analysis["CostPrice"] * 1.3  # Default 30% markup fallback

    df_analysis["TotalPotentialRevenue"] = (
        df_analysis["CurrentStock"] * df_analysis["SellingPrice"]
    )
    df_analysis["CapitalTiedUp"] = (
        df_analysis["CurrentStock"] * df_analysis["CostPrice"]
    )
    df_analysis["PotentialProfit"] = (
        df_analysis["TotalPotentialRevenue"] - df_analysis["CapitalTiedUp"]
    )

    # Demand Velocity & Simulation
    days_analyzed = 30
    base_velocity = df_analysis["QuantitySold"] / days_analyzed
    df_analysis["SimulatedVelocity"] = base_velocity * (
        1 + (sales_growth_slider / 100.0)
    )

    # ---------------------------------------------------------
    # ADVANCED FEATURE 1: SAFETY STOCK & REORDER POINT (ROP)
    # ---------------------------------------------------------
    # Safety Stock = ServiceFactor * StdDev * sqrt(LeadTime) [Approximated with velocity variance]
    df_analysis["SafetyStock"] = (
        df_analysis["SimulatedVelocity"] * 0.2 * supplier_lead_time * service_level_factor
    ).apply(lambda x: max(1, int(round(x))))

    # Reorder Point (ROP) = (Daily Velocity * Lead Time) + Safety Stock
    df_analysis["ReorderPoint"] = (
        df_analysis["SimulatedVelocity"] * supplier_lead_time
        + df_analysis["SafetyStock"]
    ).apply(lambda x: int(round(x)))

    # Days Until Stockout
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

    # ---------------------------------------------------------
    # ADVANCED FEATURE 2: ABC PARETO INVENTORY CLASSIFICATION
    # ---------------------------------------------------------
    df_analysis = df_analysis.sort_values(by="TotalRevenue", ascending=False).reset_index(drop=True)
    df_analysis["CumulativeRevenue"] = df_analysis["TotalRevenue"].cumsum()
    total_rev_sum = df_analysis["TotalRevenue"].sum()

    if total_rev_sum > 0:
      df_analysis["CumulativePct"] = df_analysis["CumulativeRevenue"] / total_rev_sum
    else:
      df_analysis["CumulativePct"] = 0.0

    def classify_abc(pct):
      if pct <= 0.80:
        return "Class A (High Value - Strict Control)"
      elif pct <= 0.95:
        return "Class B (Moderate Value)"
      else:
        return "Class C (Long-Tail / Bulk)"

    df_analysis["ABC_Category"] = df_analysis["CumulativePct"].apply(classify_abc)

    # ---------------------------------------------------------
    # ADVANCED FEATURE 3: JUMIA/ALIBABA SKU PERFORMANCE TIERING
    # ---------------------------------------------------------
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

    # Reorder Recommendation Quantity
    df_analysis["RecommendedOrderQty"] = df_analysis.apply(
        lambda row: max(0, int(round((row["SimulatedVelocity"] * 30 + row["SafetyStock"]) - row["CurrentStock"]))),
        axis=1,
    )
    df_analysis["EstimatedOrderCost"] = (
        df_analysis["RecommendedOrderQty"] * df_analysis["CostPrice"]
    )

    # ---------------------------------------------------------
    # EXECUTIVE KPI DASHBOARD
    # ---------------------------------------------------------
    st.markdown("### 📊 Enterprise Executive Performance Metrics")
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Total Stock Capital", f"GHS {df_analysis['CapitalTiedUp'].sum():,.2f}")
    col2.metric("Projected Gross Profit", f"GHS {df_analysis['PotentialProfit'].sum():,.2f}")
    col3.metric("Total Sales Revenue", f"GHS {total_rev_sum:,.2f}")
    col4.metric("Class A Core Items", len(df_analysis[df_analysis["ABC_Category"].str.contains("Class A")]))
    col5.metric("Dead Stock Traps", len(df_analysis[df_analysis["SKU_Tier"].str.contains("Dead Stock")]))

    st.markdown("---")

    # ---------------------------------------------------------
    # PLOTLY ENTERPRISE CHARTS
    # ---------------------------------------------------------
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

    # ---------------------------------------------------------
    # MASTER ENTERPRISE INVENTORY TABLE
    # ---------------------------------------------------------
    st.subheader("🔮 Master Enterprise Supply Chain & Replenishment Dashboard")
    st.markdown(
        "Items below incorporate **Safety Stock Buffers**, **Reorder Points (ROP)**,"
        " and **ABC Pareto classification**:"
    )

    def style_enterprise(val):
      if "URGENT" in str(val) or "Dead Stock" in str(val):
        return "background-color: #ffcccc; color: #900; font-weight: bold;"
      elif "Class A" in str(val):
        return "background-color: #e2f0d9; color: #274e13; font-weight: bold;"
      return ""

    display_cols = [
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

    st.dataframe(df_analysis[display_cols], use_container_width=True)

    # ---------------------------------------------------------
    # WHATSAPP ENTERPRISE PURCHASE ORDER DISPATCHER
    # ---------------------------------------------------------
    st.markdown("---")
    st.subheader("📱 Automated Enterprise WhatsApp Supplier Dispatcher")

    reorder_needed_df = df_analysis[df_analysis["CurrentStock"] <= df_analysis["ReorderPoint"]]
    if not reorder_needed_df.empty:
      selected_sku = st.selectbox(
          "Select SKU to Dispatch Purchase Order", reorder_needed_df["ItemName"].tolist()
      )
      sku_row = reorder_needed_df[reorder_needed_df["ItemName"] == selected_sku].iloc[0]

      sup_phone = (
          str(sku_row["SupplierPhone"])
          if "SupplierPhone" in sku_row and pd.notna(sku_row["SupplierPhone"])
          else "233000000000"
      )

      order_msg = (
          f"Hello Supplier, automated reorder generated by AlufaTanaah Enterprise Suite. "
          f"Item: *{sku_row['ItemName']}* | "
          f"ABC Class: *{sku_row['ABC_Category'].split()[0]}* | "
          f"Restock Quantity Required: *{sku_row['RecommendedOrderQty']} units* | "
          f"Estimated PO Cost: *GHS {sku_row['EstimatedOrderCost']:,.2f}*. Please confirm fulfillment."
      )
      encoded_po = urllib.parse.quote(order_msg)
      po_wa_url = f"https://wa.me/{sup_phone}?text={encoded_po}"

      col_p1, col_p2 = st.columns([2, 1])
      with col_p1:
        st.info(
            f"**Item:** {sku_row['ItemName']} | **Current Stock:** {sku_row['CurrentStock']} | "
            f"**Reorder Point (ROP):** {sku_row['ReorderPoint']} | **Order Qty:** {sku_row['RecommendedOrderQty']} units"
        )
      with col_p2:
        st.markdown(
            f'<a href="{po_wa_url}" target="_blank" style="display:block; text-align:center; background-color:#25D366; color:white; padding:12px 15px; border-radius:6px; text-decoration:none; font-weight:bold; font-size:14px;">💬 Send WhatsApp PO</a>',
            unsafe_allow_html=True,
        )
    else:
      st.success("🎉 All inventory SKUs are safely above their Reorder Points (ROP)!")
      
      
      # ---------------------------------------------------------
    # EXPORT REPORT
    # ---------------------------------------------------------
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