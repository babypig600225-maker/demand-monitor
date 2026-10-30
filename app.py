"""全廠需量監控 — Streamlit 版（富強醫材 設備課）

架構：管理者上傳圖控事件歷史CSV → 只保留電力點位、依月份存成JSON到「私人」GitHub資料repo
      → 檢視頁從資料repo讀取所有月份 → 注入 template.html，由瀏覽器以已驗證的JS邏輯計算與繪圖。

Streamlit Secrets 需設定：
  GITHUB_TOKEN   = "github_pat_..."   （只授權資料repo的 Contents 讀寫）
  DATA_REPO      = "帳號/demand-data"  （私人repo）
  ADMIN_PASSWORD = "自訂密碼"
"""
import base64
import io
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="全廠需量監控", page_icon="⚡", layout="wide")
st.markdown(
    "<style>.block-container{padding-top:1rem;padding-bottom:1rem;max-width:1280px}</style>",
    unsafe_allow_html=True,
)

TW = timezone(timedelta(hours=8))
POINT_RE = re.compile(r"^(MVCB|HM|VCB1|VCB2)_(KWH|有效功率|視在功率)$")
DEFAULT_SETTINGS = {"contract": 940, "warnPct": 90, "holidays": "2026-09-25,2026-09-28,2026-10-10"}


# ---------------- GitHub ----------------
def _cfg():
    return st.secrets["GITHUB_TOKEN"].strip(), st.secrets["DATA_REPO"].strip()


def _headers():
    token, _ = _cfg()
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}


def gh_list(folder: str):
    _, repo = _cfg()
    r = requests.get(f"https://api.github.com/repos/{repo}/contents/{folder}", headers=_headers(), timeout=30)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return [x for x in r.json() if x["type"] == "file"]


def gh_get(path: str):
    """回傳 (內容文字, sha)；檔案不存在回傳 (None, None)。使用 raw 格式以支援 >1MB 檔案。"""
    _, repo = _cfg()
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    meta = requests.get(url, headers=_headers(), timeout=30)
    if meta.status_code == 404:
        return None, None
    meta.raise_for_status()
    sha = meta.json()["sha"]
    raw = requests.get(url, headers={**_headers(), "Accept": "application/vnd.github.raw"}, timeout=60)
    raw.raise_for_status()
    return raw.content.decode("utf-8"), sha


def gh_put(path: str, text: str, message: str, sha=None):
    _, repo = _cfg()
    body = {"message": message, "content": base64.b64encode(text.encode("utf-8")).decode("ascii")}
    if sha:
        body["sha"] = sha
    r = requests.put(f"https://api.github.com/repos/{repo}/contents/{path}", headers=_headers(), json=body, timeout=60)
    r.raise_for_status()


def gh_delete(path: str, sha: str, message: str):
    _, repo = _cfg()
    r = requests.delete(
        f"https://api.github.com/repos/{repo}/contents/{path}",
        headers=_headers(), json={"message": message, "sha": sha}, timeout=30,
    )
    r.raise_for_status()


# ---------------- 資料處理 ----------------
def parse_csv(file_bytes: bytes) -> pd.DataFrame:
    """讀圖控事件歷史CSV（Big5或UTF-8），只保留計算需要的電力點位。"""
    for enc in ("utf-8-sig", "cp950"):
        try:
            text = file_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = file_bytes.decode("cp950", errors="replace")
    df = pd.read_csv(io.StringIO(text), dtype=str)
    df.columns = [c.strip() for c in df.columns]
    need = {"序號", "條件", "觸發狀態", "點位名稱", "觸發時間"}
    if not need.issubset(df.columns):
        raise ValueError("欄位不符，請確認是圖控事件歷史CSV（需含：" + "、".join(sorted(need)) + "）")
    df["點位名稱"] = df["點位名稱"].str.strip()
    df = df[(df["條件"].str.strip() == ">-999") & df["點位名稱"].str.match(POINT_RE.pattern, na=False)]
    df = df.drop_duplicates("序號")
    # 圖控匯出的時間格式不一（2026/9/1 00:20 或 2026-08-01 00:11:06），逐筆解析
    df["t"] = pd.to_datetime(df["觸發時間"].str.strip(), format="mixed", errors="coerce")
    df["v"] = pd.to_numeric(df["觸發狀態"], errors="coerce")
    df = df.dropna(subset=["t", "v"])
    # 以「秒」儲存（當地時間視為無時區）；秒數不可捨去，否則15分鐘需量會有數%誤差
    df["sec"] = (df["t"] - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)
    df["month"] = df["t"].dt.strftime("%Y-%m")
    return df[["點位名稱", "sec", "v", "month"]]


def to_month_payloads(df: pd.DataFrame) -> dict:
    out = {}
    for month, g in df.groupby("month"):
        pts = {}
        for pt, gg in g.groupby("點位名稱"):
            gg = gg.drop_duplicates("sec", keep="last").sort_values("sec")
            pts[pt] = [[int(t), float(v) if v % 1 else int(v)] for t, v in zip(gg["sec"], gg["v"])]
        out[month] = {"month": month, "points": pts}
    return out


def merge_payload(old: dict, new: dict) -> dict:
    merged = {"month": new["month"], "points": {}}
    for pt in set(old.get("points", {})) | set(new["points"]):
        d = {m: v for m, v in old.get("points", {}).get(pt, [])}
        d.update({m: v for m, v in new["points"].get(pt, [])})
        merged["points"][pt] = [[m, d[m]] for m in sorted(d)]
    return merged


@st.cache_data(ttl=600, show_spinner="讀取資料中…")
def load_all():
    months = []
    for f in sorted(gh_list("months"), key=lambda x: x["name"]):
        if f["name"].endswith(".json"):
            text, _ = gh_get(f"months/{f['name']}")
            months.append(json.loads(text))
    s_text, _ = gh_get("settings.json")
    settings = {**DEFAULT_SETTINGS, **(json.loads(s_text) if s_text else {})}
    return months, settings


def build_html(months, settings) -> str:
    tpl = (Path(__file__).parent / "template.html").read_text(encoding="utf-8")
    def js(obj):  # 避免資料中的 "</" 提前結束 script 標籤
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")

    inject = f"<script>window.__RAW__={js(months)};window.__SETTINGS__={js(settings)};</script>"
    return tpl.replace("<!--EMBED-->", inject, 1)


# ---------------- 檢視頁 ----------------
try:
    months, settings = load_all()
except Exception as e:  # noqa: BLE001
    st.error(f"無法讀取資料，請檢查 Secrets 設定（GITHUB_TOKEN、DATA_REPO）。錯誤：{e}")
    st.stop()

if months:
    components.html(build_html(months, settings), height=2350, scrolling=True)
else:
    st.info("尚無資料，請由下方管理區上傳圖控事件歷史CSV。")

# ---------------- 管理區 ----------------
with st.expander("🔧 管理區（設備課）", expanded=not months):
    pw = st.text_input("管理密碼", type="password")
    if pw != st.secrets.get("ADMIN_PASSWORD", ""):
        if pw:
            st.error("密碼錯誤")
        st.stop()

    st.subheader("上傳圖控CSV")
    st.caption("可一次上傳多個檔案；同月份資料會合併，重複紀錄自動排除。壓差等非電力點位不會被儲存。")
    files = st.file_uploader("事件歷史CSV", type=["csv"], accept_multiple_files=True)
    if files and st.button("上傳並更新", type="primary"):
        try:
            df = pd.concat([parse_csv(f.getvalue()) for f in files], ignore_index=True)
            if df.empty:
                st.error("檔案中找不到電力點位（MVCB/HM/VCB1/VCB2 的電度與功率）。")
                st.stop()
            for month, payload in to_month_payloads(df).items():
                old_text, sha = gh_get(f"months/{month}.json")
                if old_text:
                    payload = merge_payload(json.loads(old_text), payload)
                n = sum(len(v) for v in payload["points"].values())
                gh_put(f"months/{month}.json", json.dumps(payload, separators=(",", ":")), f"更新 {month}（{n} 筆）", sha)
                st.write(f"✅ {month}：{n:,} 筆")
            s_text, s_sha = gh_get("settings.json")
            s = {**DEFAULT_SETTINGS, **(json.loads(s_text) if s_text else {})}
            s["updated"] = datetime.now(TW).strftime("%Y-%m-%d %H:%M")
            gh_put("settings.json", json.dumps(s, ensure_ascii=False, indent=2), "更新時間", s_sha)
            load_all.clear()
            st.success("更新完成，重新整理頁面即可看到。")
        except Exception as e:  # noqa: BLE001
            st.error(f"更新失敗：{e}")

    st.subheader("設定")
    with st.form("settings"):
        c1, c2 = st.columns(2)
        contract = c1.number_input("契約容量（kW）", min_value=1, value=int(settings["contract"]), step=1)
        warn = c2.number_input("警戒線（契約容量的%）", min_value=50, max_value=100, value=int(settings["warnPct"]), step=1)
        hol = st.text_input("國定假日（YYYY-MM-DD，逗號分隔；週六日自動判定）", value=settings["holidays"])
        if st.form_submit_button("儲存設定"):
            s_text, s_sha = gh_get("settings.json")
            s = {**DEFAULT_SETTINGS, **(json.loads(s_text) if s_text else {})}
            s.update({"contract": int(contract), "warnPct": int(warn), "holidays": hol})
            gh_put("settings.json", json.dumps(s, ensure_ascii=False, indent=2), "更新設定", s_sha)
            load_all.clear()
            st.success("設定已儲存。")

    st.subheader("已儲存月份")
    stored = [f for f in gh_list("months") if f["name"].endswith(".json")]
    if stored:
        for f in sorted(stored, key=lambda x: x["name"]):
            c1, c2 = st.columns([4, 1])
            c1.write(f"{f['name'][:-5]}（{f['size']/1024:,.0f} KB）")
            if c2.button("刪除", key="del_" + f["name"]):
                gh_delete(f"months/{f['name']}", f["sha"], f"刪除 {f['name']}")
                load_all.clear()
                st.rerun()
    else:
        st.caption("尚無資料")
