import os
import re
import math
import json
import time
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st

APP_TZ = timezone(timedelta(hours=9))
DB_PATH = "pokeca_dashboard.db"
DEFAULT_FX = 150.0
DEFAULT_DOMESTIC_FEE = 0.10
DEFAULT_OVERSEAS_FEE = 0.15
DEFAULT_INTL_SHIPPING = 1800.0
DEFAULT_CUSTOMS = 0.0
DEFAULT_LOCAL_SHIPPING = 230.0
EBAY_MODE_DEFAULT = "simulated"

RARITY_BUCKETS = [
    "PSA10", "PROMO", "MUR", "SAR", "UR", "AR", "SR", "MASTER BALL", "GX", "EX", "VINTAGE"
]

MARKET_SCAN_QUERIES = [
    "pokemon card psa 10",
    "pokemon card promo",
    "pokemon card sar",
    "pokemon card ur",
    "pokemon card mur",
    "pokemon card poncho",
    "pokemon card vintage",
    "pokemon card japanese exclusive",
]


def now_jst() -> datetime:
    return datetime.now(APP_TZ)


def fmt_ts(dt: Optional[datetime] = None) -> str:
    dt = dt or now_jst()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def normalize_query(text: str) -> str:
    s = re.sub(r"\s+", " ", (text or "").strip())
    return s


def keyword_seed(text: str) -> int:
    text = normalize_query(text).lower()
    return sum((i + 1) * ord(c) for i, c in enumerate(text)) % 10_000_000


def classify_rarity(name: str) -> str:
    upper = (name or "").upper()
    for token in RARITY_BUCKETS:
        if token in upper:
            return token
    if "PROMO" in upper or "プロモ" in upper:
        return "PROMO"
    if "旧裏" in name:
        return "VINTAGE"
    return "OTHER"


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name TEXT NOT NULL,
            purchase_price_jpy REAL NOT NULL,
            qty INTEGER NOT NULL,
            card_condition TEXT NOT NULL,
            notes TEXT,
            added_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inventory_id INTEGER,
            product_name TEXT NOT NULL,
            source TEXT NOT NULL,
            ts TEXT NOT NULL,
            price_usd REAL NOT NULL,
            fx_rate REAL NOT NULL,
            price_jpy REAL NOT NULL,
            delta_jpy REAL,
            delta_pct REAL,
            metadata_json TEXT,
            FOREIGN KEY (inventory_id) REFERENCES inventory(id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS market_scan (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query_text TEXT NOT NULL,
            ts TEXT NOT NULL,
            momentum_score REAL NOT NULL,
            avg_price_usd REAL NOT NULL,
            avg_price_jpy REAL NOT NULL,
            delta_pct REAL NOT NULL,
            rarity_hint TEXT NOT NULL,
            note TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def load_inventory() -> pd.DataFrame:
    conn = db_conn()
    df = pd.read_sql_query("SELECT * FROM inventory ORDER BY id DESC", conn)
    conn.close()
    return df


def insert_inventory(product_name: str, purchase_price_jpy: float, qty: int, card_condition: str, notes: str) -> None:
    ts = fmt_ts()
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO inventory (product_name, purchase_price_jpy, qty, card_condition, notes, added_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (product_name, purchase_price_jpy, qty, card_condition, notes, ts, ts),
    )
    conn.commit()
    conn.close()


def delete_inventory(item_id: int) -> None:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM price_history WHERE inventory_id = ?", (item_id,))
    cur.execute("DELETE FROM inventory WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()


def upsert_price_point(inventory_id: Optional[int], product_name: str, source: str, price_usd: float, fx_rate: float, metadata: Optional[Dict] = None) -> Dict:
    price_jpy = price_usd * fx_rate
    history = load_price_history_for_item(inventory_id=inventory_id, product_name=product_name)
    delta_jpy = None
    delta_pct = None
    if not history.empty:
        prev = float(history.iloc[-1]["price_jpy"])
        delta_jpy = price_jpy - prev
        delta_pct = ((price_jpy / prev) - 1.0) * 100 if prev else None
    record = {
        "inventory_id": inventory_id,
        "product_name": product_name,
        "source": source,
        "ts": fmt_ts(),
        "price_usd": float(price_usd),
        "fx_rate": float(fx_rate),
        "price_jpy": float(price_jpy),
        "delta_jpy": delta_jpy,
        "delta_pct": delta_pct,
        "metadata_json": json.dumps(metadata or {}, ensure_ascii=False),
    }
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO price_history (
            inventory_id, product_name, source, ts, price_usd, fx_rate,
            price_jpy, delta_jpy, delta_pct, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record["inventory_id"],
            record["product_name"],
            record["source"],
            record["ts"],
            record["price_usd"],
            record["fx_rate"],
            record["price_jpy"],
            record["delta_jpy"],
            record["delta_pct"],
            record["metadata_json"],
        ),
    )
    conn.commit()
    conn.close()
    return record


def load_price_history_for_item(inventory_id: Optional[int] = None, product_name: Optional[str] = None) -> pd.DataFrame:
    conn = db_conn()
    if inventory_id is not None:
        df = pd.read_sql_query(
            "SELECT * FROM price_history WHERE inventory_id = ? ORDER BY ts ASC, id ASC",
            conn,
            params=(inventory_id,),
        )
    elif product_name is not None:
        df = pd.read_sql_query(
            "SELECT * FROM price_history WHERE product_name = ? ORDER BY ts ASC, id ASC",
            conn,
            params=(product_name,),
        )
    else:
        df = pd.read_sql_query("SELECT * FROM price_history ORDER BY ts ASC, id ASC", conn)
    conn.close()
    return df


def load_latest_prices() -> pd.DataFrame:
    conn = db_conn()
    query = """
    SELECT ph.*
    FROM price_history ph
    INNER JOIN (
        SELECT inventory_id, MAX(id) AS max_id
        FROM price_history
        WHERE inventory_id IS NOT NULL
        GROUP BY inventory_id
    ) latest ON ph.id = latest.max_id
    ORDER BY ph.id DESC
    """
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df


def load_market_scan() -> pd.DataFrame:
    conn = db_conn()
    df = pd.read_sql_query("SELECT * FROM market_scan ORDER BY id DESC", conn)
    conn.close()
    return df


def append_market_scan(rows: List[Dict]) -> None:
    if not rows:
        return
    conn = db_conn()
    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO market_scan (
            query_text, ts, momentum_score, avg_price_usd, avg_price_jpy, delta_pct, rarity_hint, note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                r["query_text"],
                r["ts"],
                r["momentum_score"],
                r["avg_price_usd"],
                r["avg_price_jpy"],
                r["delta_pct"],
                r["rarity_hint"],
                r["note"],
            )
            for r in rows
        ],
    )
    conn.commit()
    conn.close()


def fetch_usd_jpy() -> float:
    candidates = [
        "https://api.frankfurter.app/latest?from=USD&to=JPY",
        "https://open.er-api.com/v6/latest/USD",
    ]
    for url in candidates:
        try:
            r = requests.get(url, timeout=6)
            r.raise_for_status()
            data = r.json()
            if "rates" in data and "JPY" in data["rates"]:
                return float(data["rates"]["JPY"])
        except Exception:
            continue
    return DEFAULT_FX


def safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return float(default)


def simulated_ebay_price(query: str) -> Dict:
    """Deterministic pseudo-live generator for sold-price-like datapoints."""
    q = normalize_query(query)
    seed = keyword_seed(q)
    minute_bucket = int(time.time() // 60)
    rng = random.Random(seed + minute_bucket)

    base = max(12.0, (seed % 1800) / 10.0)
    wave = 1 + 0.09 * math.sin(minute_bucket / 11 + seed / 97)
    rarity_boost = {
        "PSA10": 3.6,
        "PROMO": 2.0,
        "MUR": 2.7,
        "SAR": 1.9,
        "UR": 1.6,
        "AR": 1.2,
        "SR": 1.3,
        "MASTER BALL": 1.7,
        "GX": 1.4,
        "EX": 1.35,
        "VINTAGE": 2.1,
        "OTHER": 1.0,
    }
    rarity = classify_rarity(q)
    price = base * wave * rarity_boost.get(rarity, 1.0)

    if "poncho" in q.lower() or "ポンチョ" in q:
        price *= 2.8
    if "charizard" in q.lower() or "リザードン" in q:
        price *= 1.9
    if "pikachu" in q.lower() or "ピカチュウ" in q:
        price *= 1.7
    if "mew" in q.lower() or "ミュウ" in q:
        price *= 1.25
    if "greninja" in q.lower() or "ゲッコウガ" in q:
        price *= 1.45

    price *= rng.uniform(0.96, 1.05)

    previous_rng = random.Random(seed + (minute_bucket - 1))
    prev_price = max(5.0, price / rng.uniform(0.96, 1.06) * previous_rng.uniform(0.95, 1.04))
    delta_pct = ((price / prev_price) - 1.0) * 100

    return {
        "source": "eBay simulated",
        "query": q,
        "price_usd": round(price, 2),
        "avg_recent_usd": round((price * rng.uniform(0.97, 1.03)), 2),
        "delta_pct_hint": round(delta_pct, 2),
        "sold_count_hint": int(2 + (seed % 17)),
        "rarity": rarity,
    }


def try_live_ebay_price(query: str) -> Optional[Dict]:
    app_token = st.secrets.get("EBAY_BEARER_TOKEN", None) if hasattr(st, "secrets") else None
    if not app_token:
        return None
    headers = {
        "Authorization": f"Bearer {app_token}",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }
    params = {
        "q": query,
        "limit": 20,
        "filter": "buyingOptions:{FIXED_PRICE}",
    }
    try:
        r = requests.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers=headers,
            params=params,
            timeout=8,
        )
        r.raise_for_status()
        data = r.json()
        items = data.get("itemSummaries", []) or []
        prices = []
        for item in items:
            price_info = item.get("price") or {}
            value = price_info.get("value")
            if value is not None:
                prices.append(float(value))
        if not prices:
            return None
        avg_price = sum(prices) / len(prices)
        return {
            "source": "eBay Browse API",
            "query": query,
            "price_usd": round(avg_price, 2),
            "avg_recent_usd": round(avg_price, 2),
            "delta_pct_hint": 0.0,
            "sold_count_hint": len(prices),
            "rarity": classify_rarity(query),
        }
    except Exception:
        return None


def get_market_price(query: str, mode: str = EBAY_MODE_DEFAULT) -> Dict:
    if mode == "live":
        live = try_live_ebay_price(query)
        if live:
            return live
    return simulated_ebay_price(query)


def compute_profit_metrics(purchase_price_jpy: float, qty: int, market_price_jpy: float, overseas_fee_rate: float,
                           intl_shipping_jpy: float, customs_jpy: float) -> Dict:
    gross = market_price_jpy * qty
    fees = gross * overseas_fee_rate
    shipping = intl_shipping_jpy * qty
    customs = customs_jpy * qty
    cost = purchase_price_jpy * qty
    net_payout = gross - fees - shipping - customs
    true_profit = net_payout - cost
    roi = (true_profit / cost * 100.0) if cost else 0.0
    margin = (true_profit / gross * 100.0) if gross else 0.0
    return {
        "gross_sales_jpy": gross,
        "fees_jpy": fees,
        "shipping_jpy": shipping,
        "customs_jpy": customs,
        "cost_jpy": cost,
        "net_payout_jpy": net_payout,
        "true_profit_jpy": true_profit,
        "roi_pct": roi,
        "margin_pct": margin,
    }


def build_inventory_view(inv_df: pd.DataFrame, latest_df: pd.DataFrame, fx_rate: float, overseas_fee_rate: float,
                         intl_shipping_jpy: float, customs_jpy: float) -> pd.DataFrame:
    if inv_df.empty:
        return pd.DataFrame()

    latest_map = latest_df.set_index("inventory_id").to_dict("index") if not latest_df.empty else {}
    rows = []
    for _, row in inv_df.iterrows():
        latest = latest_map.get(row["id"])
        current_jpy = latest["price_jpy"] if latest else 0.0
        current_usd = latest["price_usd"] if latest else 0.0
        last_delta_pct = latest["delta_pct"] if latest else None
        metrics = compute_profit_metrics(
            purchase_price_jpy=safe_float(row["purchase_price_jpy"]),
            qty=int(row["qty"]),
            market_price_jpy=safe_float(current_jpy),
            overseas_fee_rate=overseas_fee_rate,
            intl_shipping_jpy=intl_shipping_jpy,
            customs_jpy=customs_jpy,
        )
        rows.append({
            "ID": int(row["id"]),
            "商品名": row["product_name"],
            "状態": row["card_condition"],
            "個数": int(row["qty"]),
            "仕入れ単価(円)": safe_float(row["purchase_price_jpy"]),
            "最新eBay想定(USD)": current_usd,
            "最新eBay想定(円)": current_jpy,
            "前回比(%)": last_delta_pct,
            "売上総額(円)": metrics["gross_sales_jpy"],
            "手数料(円)": metrics["fees_jpy"],
            "国際送料(円)": metrics["shipping_jpy"],
            "関税等(円)": metrics["customs_jpy"],
            "手残り(円)": metrics["net_payout_jpy"],
            "真の利益(円)": metrics["true_profit_jpy"],
            "ROI(%)": metrics["roi_pct"],
            "レアリティ推定": classify_rarity(row["product_name"]),
            "追加日": row["added_at"],
        })
    return pd.DataFrame(rows)


def style_profit_table(df: pd.DataFrame):
    if df.empty:
        return df

    def color_profit(v):
        try:
            v = float(v)
        except Exception:
            return ""
        return "color: #00d27a; font-weight: 700;" if v >= 0 else "color: #ff5c8a; font-weight: 700;"

    def color_roi(v):
        try:
            v = float(v)
        except Exception:
            return ""
        return "color: #65b7ff; font-weight: 700;" if v >= 0 else "color: #ff8aa8; font-weight: 700;"

    return df.style.format({
        "仕入れ単価(円)": "{:,.0f}",
        "最新eBay想定(USD)": "{:.2f}",
        "最新eBay想定(円)": "{:,.0f}",
        "前回比(%)": "{:+.2f}",
        "売上総額(円)": "{:,.0f}",
        "手数料(円)": "{:,.0f}",
        "国際送料(円)": "{:,.0f}",
        "関税等(円)": "{:,.0f}",
        "手残り(円)": "{:,.0f}",
        "真の利益(円)": "{:,.0f}",
        "ROI(%)": "{:+.2f}",
    }).map(color_profit, subset=["真の利益(円)", "手残り(円)"]).map(color_roi, subset=["ROI(%)", "前回比(%)"])


def scan_market(fx_rate: float, mode: str) -> pd.DataFrame:
    rows = []
    ts = fmt_ts()
    for q in MARKET_SCAN_QUERIES:
        price = get_market_price(q, mode=mode)
        momentum = abs(price["delta_pct_hint"]) * (1 + (price["sold_count_hint"] / 10))
        rarity_hint = price.get("rarity") or classify_rarity(q)
        note = f"{rarity_hint} / sold≈{price['sold_count_hint']}"
        rows.append({
            "query_text": q,
            "ts": ts,
            "momentum_score": round(momentum, 2),
            "avg_price_usd": price["avg_recent_usd"],
            "avg_price_jpy": round(price["avg_recent_usd"] * fx_rate, 0),
            "delta_pct": price["delta_pct_hint"],
            "rarity_hint": rarity_hint,
            "note": note,
        })
    append_market_scan(rows)
    return pd.DataFrame(rows).sort_values(["momentum_score", "delta_pct"], ascending=False)


def latest_scan_summary(scan_df: pd.DataFrame) -> Tuple[str, str]:
    if scan_df.empty:
        return "市場スキャン待ち", "まだ市場スキャン結果がありません。"
    top = scan_df.sort_values(["momentum_score", "delta_pct"], ascending=False).head(3)
    top_rarity = top.iloc[0]["rarity_hint"]
    rising = top[top["delta_pct"] > 0]
    if rising.empty:
        headline = f"{top_rarity} は監視継続"
        body = "海外全体では明確な一方向トレンドは弱め。無理に追いかけず、国内の安い在庫だけ拾う守りの局面。"
    else:
        headline = f"{top_rarity} 需要が先行上昇"
        names = " / ".join(rising["query_text"].head(3).tolist())
        body = (
            f"海外の先行指標では {names} 周辺が上向き。"
            f"とくに {top_rarity} 系統はモメンタムが強いので、日本側で割安在庫があれば先回り確保を検討。"
        )
    return headline, body


def build_action_plan(scan_df: pd.DataFrame, inventory_view: pd.DataFrame) -> str:
    if scan_df.empty:
        return "市場スキャンがまだないため、まずは『市場スキャン更新』を押して全体トレンドを取得してください。"
    top = scan_df.sort_values(["momentum_score", "delta_pct"], ascending=False).head(5)
    rare_counts = top["rarity_hint"].value_counts().to_dict()
    strongest = top.iloc[0]
    rarity = strongest["rarity_hint"]
    inv_note = ""
    if not inventory_view.empty:
        losing = inventory_view[inventory_view["真の利益(円)"] < 0]
        winning = inventory_view[inventory_view["ROI(%)"] > 15]
        if not losing.empty:
            inv_note += f"保有銘柄では逆ザヤ候補が {len(losing)} 件あるので、更新頻度を上げて損切りラインを明確化。 "
        if not winning.empty:
            inv_note += f"一方でROI 15%超の候補が {len(winning)} 件あり、利確・再投資の原資化も可能。"
    text = (
        f"AIアクションプラン: 現在の海外市場全体では {rarity} 系の需要が最も強く、"
        f"先行モメンタム上位は『{strongest['query_text']}』付近です。 "
        f"上位5クエリのレアリティ分布は {rare_counts}。 "
        f"結論として、国内では {rarity}・限定系・状態良好個体を優先監視し、"
        f"海外価格が1日で+5%を超えた銘柄は日本側の在庫確認→確保→即時再評価の順で回すのが有効です。 "
        f"{inv_note}".strip()
    )
    return text


def ensure_demo_data(mode: str, fx_rate: float):
    inv = load_inventory()
    if not inv.empty:
        return
    demo_items = [
        ("メガゲッコウガex MUR 120/083", 42000, 1, "Raw", "初期デモ"),
        ("ピカチュウ プロモ PSA10", 78000, 1, "PSA10", "初期デモ"),
        ("ブラッキー 旧裏", 25000, 1, "Raw", "初期デモ"),
    ]
    for item in demo_items:
        insert_inventory(*item)
    inv = load_inventory()
    for _, row in inv.iterrows():
        point = get_market_price(row["product_name"], mode=mode)
        upsert_price_point(
            inventory_id=int(row["id"]),
            product_name=row["product_name"],
            source=point["source"],
            price_usd=point["price_usd"],
            fx_rate=fx_rate,
            metadata=point,
        )


def inject_css():
    st.markdown(
        """
        <style>
        .stApp {
            background: radial-gradient(circle at top, #111827 0%, #070b14 45%, #03050a 100%);
            color: #f3f6fb;
        }
        .block-container {
            padding-top: 1rem;
            padding-bottom: 4rem;
            max-width: 1100px;
        }
        .metric-card {
            background: linear-gradient(180deg, rgba(20,26,42,.95), rgba(10,13,22,.92));
            border: 1px solid rgba(101,183,255,.16);
            border-radius: 20px;
            padding: 14px 16px;
            box-shadow: 0 10px 30px rgba(0,0,0,.25);
            margin-bottom: 12px;
        }
        .metric-label {
            color: #93a7c3;
            font-size: 12px;
            letter-spacing: .04em;
            text-transform: uppercase;
        }
        .metric-value {
            font-size: 30px;
            font-weight: 800;
            color: #ffffff;
            line-height: 1.1;
            margin-top: 6px;
        }
        .tiny {
            color: #8fa5c8;
            font-size: 12px;
        }
        .card-shell {
            border-radius: 24px;
            background: linear-gradient(180deg, rgba(16,20,30,.98), rgba(8,12,18,.98));
            border: 1px solid rgba(101,183,255,.18);
            padding: 14px;
            margin-bottom: 14px;
            box-shadow: 0 12px 28px rgba(0,0,0,.22);
        }
        .good {color:#00d27a;font-weight:800;}
        .bad {color:#ff5c8a;font-weight:800;}
        .neutral {color:#9fb2cd;font-weight:700;}
        .headline {
            font-size: 1.1rem;
            font-weight: 800;
            margin-bottom: .25rem;
        }
        div[data-testid="stDataFrame"] {
            border-radius: 18px;
            overflow: hidden;
            border: 1px solid rgba(101,183,255,.10);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_top_metrics(inv_view: pd.DataFrame, fx_rate: float):
    total_cost = inv_view["仕入れ単価(円)"].mul(inv_view["個数"]).sum() if not inv_view.empty else 0
    total_profit = inv_view["真の利益(円)"].sum() if not inv_view.empty else 0
    total_value = inv_view["売上総額(円)"].sum() if not inv_view.empty else 0
    avg_roi = inv_view["ROI(%)"].mean() if not inv_view.empty else 0
    cols = st.columns(4)
    metrics = [
        ("総仕入れ", f"¥{total_cost:,.0f}", f"保有銘柄: {len(inv_view)}"),
        ("総売上想定", f"¥{total_value:,.0f}", f"USD/JPY: {fx_rate:.2f}"),
        ("総真の利益", f"¥{total_profit:,.0f}", "海外手数料・送料控除後"),
        ("平均ROI", f"{avg_roi:+.2f}%", "全保有ベース"),
    ]
    for col, (label, value, note) in zip(cols, metrics):
        col.markdown(
            f"<div class='metric-card'><div class='metric-label'>{label}</div>"
            f"<div class='metric-value'>{value}</div><div class='tiny'>{note}</div></div>",
            unsafe_allow_html=True,
        )


def render_item_card(item_row: pd.Series, latest_row: Optional[pd.Series], mode: str, fx_rate: float,
                     overseas_fee_rate: float, intl_shipping_jpy: float, customs_jpy: float):
    item_id = int(item_row["id"])
    name = item_row["product_name"]
    current_jpy = safe_float(latest_row["price_jpy"]) if latest_row is not None else 0.0
    delta_pct = latest_row["delta_pct"] if latest_row is not None else None
    metrics = compute_profit_metrics(
        purchase_price_jpy=safe_float(item_row["purchase_price_jpy"]),
        qty=int(item_row["qty"]),
        market_price_jpy=current_jpy,
        overseas_fee_rate=overseas_fee_rate,
        intl_shipping_jpy=intl_shipping_jpy,
        customs_jpy=customs_jpy,
    )
    delta_class = "neutral"
    delta_text = "---"
    if delta_pct is not None:
        delta_class = "good" if delta_pct >= 0 else "bad"
        delta_text = f"{delta_pct:+.2f}%"

    st.markdown(f"<div class='card-shell'><div class='headline'>{name}</div>", unsafe_allow_html=True)
    a, b, c, d = st.columns(4)
    a.markdown(f"<div class='tiny'>状態</div><div>{item_row['card_condition']}</div>", unsafe_allow_html=True)
    b.markdown(f"<div class='tiny'>仕入れ</div><div>¥{safe_float(item_row['purchase_price_jpy']):,.0f}</div>", unsafe_allow_html=True)
    c.markdown(f"<div class='tiny'>個数</div><div>{int(item_row['qty'])}</div>", unsafe_allow_html=True)
    d.markdown(f"<div class='tiny'>前回比</div><div class='{delta_class}'>{delta_text}</div>", unsafe_allow_html=True)

    e, f, g = st.columns(3)
    e.metric("最新eBay想定(円)", f"¥{current_jpy:,.0f}")
    f.metric("真の利益", f"¥{metrics['true_profit_jpy']:,.0f}")
    g.metric("ROI", f"{metrics['roi_pct']:+.2f}%")

    left, mid, right = st.columns([1.2, 1.2, 1])
    if left.button(f"更新 #{item_id}", key=f"refresh_{item_id}", use_container_width=True):
        point = get_market_price(name, mode=mode)
        upsert_price_point(
            inventory_id=item_id,
            product_name=name,
            source=point["source"],
            price_usd=point["price_usd"],
            fx_rate=fx_rate,
            metadata=point,
        )
        st.success(f"{name} を更新しました")
        st.rerun()
    if mid.button(f"履歴追加(ダミー) #{item_id}", key=f"append_{item_id}", use_container_width=True):
        for _ in range(3):
            point = get_market_price(name, mode=mode)
            upsert_price_point(
                inventory_id=item_id,
                product_name=name,
                source=point["source"],
                price_usd=point["price_usd"],
                fx_rate=fx_rate,
                metadata=point,
            )
        st.success("時系列を伸ばしました")
        st.rerun()
    if right.button(f"削除 #{item_id}", key=f"delete_{item_id}", use_container_width=True):
        delete_inventory(item_id)
        st.warning(f"{name} を削除しました")
        st.rerun()

    hist = load_price_history_for_item(inventory_id=item_id)
    if not hist.empty:
        chart_df = hist[["ts", "price_jpy"]].copy()
        chart_df["ts"] = pd.to_datetime(chart_df["ts"])
        chart_df = chart_df.set_index("ts")
        st.line_chart(chart_df, y="price_jpy", use_container_width=True, height=220)
        tail = hist.tail(6)[["ts", "price_usd", "price_jpy", "delta_pct"]].copy()
        st.dataframe(
            tail.rename(columns={"ts": "時刻", "price_usd": "USD", "price_jpy": "円", "delta_pct": "前回比%"}),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("まだ価格履歴がありません。")
    st.markdown("</div>", unsafe_allow_html=True)


def inventory_csv_download(inv_view: pd.DataFrame):
    if inv_view.empty:
        return
    csv_bytes = inv_view.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "CSVダウンロード",
        data=csv_bytes,
        file_name=f"pokeca_inventory_{now_jst().strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv",
        use_container_width=True,
    )


def main():
    st.set_page_config(page_title="ポケカ・グローバル・戦略ダッシュボード", page_icon="🃏", layout="wide")
    inject_css()
    init_db()

    with st.sidebar:
        st.title("⚙️ 戦略設定")
        auto_fx = st.toggle("USD/JPY 自動取得", value=True)
        fx_rate = fetch_usd_jpy() if auto_fx else DEFAULT_FX
        fx_rate = st.slider("USD/JPY", min_value=100.0, max_value=200.0, value=float(round(fx_rate, 2)), step=0.1)
        overseas_fee_rate = st.slider("海外販売手数料", 0.0, 0.30, DEFAULT_OVERSEAS_FEE, 0.01)
        intl_shipping_jpy = st.number_input("国際送料/枚(円)", min_value=0, value=int(DEFAULT_INTL_SHIPPING), step=100)
        customs_jpy = st.number_input("関税等/枚(円)", min_value=0, value=int(DEFAULT_CUSTOMS), step=100)
        ebay_mode = st.radio("価格取得モード", options=["simulated", "live"], index=0)
        st.caption("live は EBAY_BEARER_TOKEN が secrets にある時だけ利用")
        if st.button("デモデータ初期化", use_container_width=True):
            ensure_demo_data(mode=ebay_mode, fx_rate=fx_rate)
            st.success("デモデータを入れました")
            st.rerun()

    ensure_demo_data(mode=ebay_mode, fx_rate=fx_rate)

    st.title("🃏 ポケカ・グローバル・戦略ダッシュボード")
    st.caption("iPhone向け縦画面を意識した Streamlit Cloud 用の単一ファイル版。自由入力で全銘柄を管理し、eBay想定価格・為替・真の利益を一気に監視。")

    inv_df = load_inventory()
    latest_df = load_latest_prices()
    inv_view = build_inventory_view(
        inv_df=inv_df,
        latest_df=latest_df,
        fx_rate=fx_rate,
        overseas_fee_rate=overseas_fee_rate,
        intl_shipping_jpy=float(intl_shipping_jpy),
        customs_jpy=float(customs_jpy),
    )

    render_top_metrics(inv_view, fx_rate)

    st.markdown("### 追加・監視登録")
    with st.form("add_inventory_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        product_name = c1.text_input("商品名", placeholder="例: メガゲッコウガex MUR 120/083")
        purchase_price = c2.number_input("仕入れ値(円)", min_value=0, value=0, step=100)
        c3, c4, c5 = st.columns(3)
        qty = c3.number_input("個数", min_value=1, value=1, step=1)
        card_condition = c4.selectbox("状態", ["Raw", "PSA10", "PSA9", "BGS10", "CGC10", "Other"])
        notes = c5.text_input("メモ", placeholder="国内相場・購入元など")
        submitted = st.form_submit_button("在庫に追加", use_container_width=True)
        if submitted:
            name = normalize_query(product_name)
            if not name:
                st.error("商品名を入れてください")
            else:
                insert_inventory(name, float(purchase_price), int(qty), card_condition, notes)
                new_inv = load_inventory()
                latest_item = new_inv.iloc[0]
                point = get_market_price(name, mode=ebay_mode)
                upsert_price_point(
                    inventory_id=int(latest_item["id"]),
                    product_name=name,
                    source=point["source"],
                    price_usd=point["price_usd"],
                    fx_rate=fx_rate,
                    metadata=point,
                )
                st.success(f"{name} を追加しました")
                st.rerun()

    st.markdown("### 海外市場スクリーニング")
    s1, s2, s3 = st.columns([1.1, 1.1, 1.2])
    if s1.button("市場スキャン更新", use_container_width=True):
        scan_df = scan_market(fx_rate=fx_rate, mode=ebay_mode)
        st.session_state["latest_scan"] = scan_df
        st.success("市場スキャンを更新しました")
    if s2.button("全銘柄まとめて更新", use_container_width=True):
        for _, row in inv_df.iterrows():
            point = get_market_price(row["product_name"], mode=ebay_mode)
            upsert_price_point(
                inventory_id=int(row["id"]),
                product_name=row["product_name"],
                source=point["source"],
                price_usd=point["price_usd"],
                fx_rate=fx_rate,
                metadata=point,
            )
        st.success("保有銘柄を一括更新しました")
        st.rerun()
    inventory_csv_download(inv_view)

    latest_scan_df = st.session_state.get("latest_scan")
    if latest_scan_df is None:
        hist_scan = load_market_scan()
        latest_scan_df = hist_scan.head(8).copy() if not hist_scan.empty else pd.DataFrame()

    h, b = latest_scan_summary(latest_scan_df if isinstance(latest_scan_df, pd.DataFrame) else pd.DataFrame())
    st.markdown(f"<div class='metric-card'><div class='headline'>{h}</div><div>{b}</div></div>", unsafe_allow_html=True)

    if isinstance(latest_scan_df, pd.DataFrame) and not latest_scan_df.empty:
        show_scan = latest_scan_df.rename(columns={
            "query_text": "監視ワード",
            "momentum_score": "モメンタム",
            "avg_price_usd": "平均USD",
            "avg_price_jpy": "平均円",
            "delta_pct": "変動率%",
            "rarity_hint": "レアリティ",
            "note": "メモ",
            "ts": "時刻",
        })
        st.dataframe(show_scan, use_container_width=True, hide_index=True)

    st.markdown("### AI仕入れアドバイス")
    st.info(build_action_plan(latest_scan_df if isinstance(latest_scan_df, pd.DataFrame) else pd.DataFrame(), inv_view))

    st.markdown("### 在庫ERP / 利益管理")
    if inv_view.empty:
        st.warning("まだ在庫がありません。上のフォームから追加してください。")
    else:
        st.dataframe(style_profit_table(inv_view), use_container_width=True, hide_index=True)

    st.markdown("### 個別トラッキング")
    latest_map = latest_df.set_index("inventory_id") if not latest_df.empty else pd.DataFrame()
    search = st.text_input("絞り込み", placeholder="カード名で検索")
    filtered = inv_df.copy()
    if search.strip():
        filtered = filtered[filtered["product_name"].str.contains(search.strip(), case=False, na=False)]
    for _, row in filtered.iterrows():
        latest_row = None
        if not latest_df.empty and int(row["id"]) in latest_map.index:
            latest_row = latest_map.loc[int(row["id"])]
        render_item_card(
            item_row=row,
            latest_row=latest_row,
            mode=ebay_mode,
            fx_rate=fx_rate,
            overseas_fee_rate=overseas_fee_rate,
            intl_shipping_jpy=float(intl_shipping_jpy),
            customs_jpy=float(customs_jpy),
        )

    st.markdown("### 導入メモ")
    st.markdown(
        """
        - そのまま `app.py` として保存すれば起動可能。
        - Streamlit Cloud に載せる場合は、必要に応じて secrets に `EBAY_BEARER_TOKEN` を設定。
        - この版は **想定データモード** で即動作し、ライブ価格は eBay トークン設定時だけ試行。
        - 永続保存は SQLite です。より堅牢にしたい場合は Supabase / PostgreSQL へ差し替え可能です。
        """
    )


if __name__ == "__main__":
    main()
