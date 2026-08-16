#!/usr/bin/env python3
"""
EdgarTools 種子擴充 —— 自動找出 xbrl_zh_map.json 漏掉的 us-gaap 標籤。

原理：EdgarTools 內建把 32k 公司異質標籤標準化（standard_concept）。
本工具跨多家公司聚合「每個標準科目 → 實際用到的原始標籤」，對映到我的 concept id，
再揪出「多家公司在用、但我的 map 沒有」的標籤 → 產出建議補丁（人工審後貼進 map）。

不換架構：繁中/指標/估值層全保留，只補 tags 陣列。

用法：
  python tools/edgar_patch.py --scan tools/russell1000.txt          # 全量掃
  python tools/edgar_patch.py AMZN AAPL MSFT ... --min 3            # 少量測試
  → 產出 tools/map_patch.json（建議補丁）+ 主控台摘要
"""
import argparse
import io
import json
import os
import sys
import warnings
from collections import Counter, defaultdict

warnings.filterwarnings("ignore")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from edgar import Company, set_identity  # noqa: E402

set_identity(os.environ.get("SEC_USER_AGENT", "BamHI frank940702@gmail.com"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP_PATH = os.path.join(ROOT, "config", "xbrl_zh_map.json")

# EdgarTools standard_concept → 我的 concept id
STD_MAP = {
    "Revenue": "revenue", "RevenueFromContractWithCustomer": "revenue",
    "CostOfGoodsAndServicesSold": "cogs", "CostOfRevenue": "cogs",
    "GrossProfit": "gross_profit",
    "ResearchAndDevelopmentExpense": "rnd", "ResearchDevelopmentExpense": "rnd",
    "MarketingExpenses": "sales_marketing", "SellingAndMarketingExpense": "sales_marketing",
    "SellingGeneralAndAdminExpenses": "sgna", "GeneralAndAdministrativeExpense": "sgna",
    "CostsSubtotal": "opex_total", "OperatingExpenses": "opex_total",
    "OperatingIncomeLoss": "operating_income",
    "InterestExpense": "interest_expense",
    "PretaxIncomeLoss": "pretax_income",
    "IncomeTaxes": "income_tax", "IncomeTaxExpenseBenefit": "income_tax",
    "NetIncome": "net_income",
    "EarningsPerShareBasic": "eps_basic", "EPSBasic": "eps_basic",
    "EarningsPerShareDiluted": "eps_diluted", "EPSDiluted": "eps_diluted",
    "SharesAverage": "shares_basic",
    "SharesFullyDilutedAverage": "shares_diluted",
    "CashAndMarketableSecurities": "cash", "CashAndCashEquivalents": "cash",
    "ShortTermInvestments": "short_term_investments",
    "TradeReceivables": "accounts_receivable",
    "Inventories": "inventory",
    "CurrentAssetsTotal": "current_assets",
    "PlantPropertyEquipmentNet": "ppe_net",
    "OperatingLeaseRightOfUseAsset": "operating_lease_rou",
    "Goodwill": "goodwill",
    "IntangibleAssets": "intangibles",
    "Assets": "total_assets",
    "TradePayables": "accounts_payable",
    "ContractWithCustomerLiability": "deferred_revenue", "DeferredRevenue": "deferred_revenue",
    "CurrentLiabilitiesTotal": "current_liabilities",
    "LongTermDebt": "long_term_debt",
    "OperatingLeaseLiabilityNoncurrent": "operating_lease_liab_nc",
    "Liabilities": "total_liabilities",
    "RetainedEarnings": "retained_earnings",
    "AllEquityBalance": "equity", "StockholdersEquity": "equity",
    "DepreciationExpense": "dna", "DepreciationDepletionAndAmortization": "dna",
    "StockBasedCompensationExpense": "sbc",
    "NetCashFromOperatingActivities": "cfo",
    "CapitalExpenses": "capex",
    "NetCashFromInvestingActivities": "cfi",
    "NetCashFromFinancingActivities": "cff",
    "DebtProceeds": "debt_issued",
    "DebtRepayment": "debt_repaid",
    "DividendsPaid": "dividends_paid",
    "RepurchaseOfStock": "buyback",
}


def statements(tk: str):
    c = Company(tk)
    f = c.get_financials()
    for st in (f.income_statement(), f.balance_sheet(), f.cashflow_statement()):
        if st is None:
            continue
        for _, r in st.to_dataframe().iterrows():
            if r.get("abstract"):
                continue
            sc, con = r.get("standard_concept"), r.get("concept")
            if sc and con:
                yield str(sc), str(con).replace("us-gaap_", "").replace("us-gaap:", "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--scan")
    ap.add_argument("--min", type=int, default=3, help="至少幾家公司在用才建議")
    args = ap.parse_args()
    tks = list(args.tickers)
    if args.scan:
        tks += [ln.strip() for ln in open(args.scan) if ln.strip()]
    if not tks:
        ap.error("給 ticker 或 --scan")

    # 降噪：EdgarTools 把子項捲到總額標準科目、且含銀行/現金流出入雜訊
    NOISE = ("Other", "Bank", "Deposit", "HeldToMaturity", "IncurredButNotYetPaid",
             "Paid", "DueFrom", "InterestBearing", "AccruedInterest", "Restricted")
    # 總額/聚合科目已有正規標籤，子項會污染 → 不對這些建議
    SKIP_AGG = {"cfo", "cfi", "cff", "opex_total", "total_assets", "total_liabilities",
                "current_assets", "current_liabilities", "cash", "net_change_cash"}

    m = json.load(open(MAP_PATH, encoding="utf-8"))
    have = {c["id"]: set(c.get("tags", [])) for c in m["concepts"]}
    found = defaultdict(Counter)   # my_id -> Counter(tag)
    unmapped = Counter()           # 未對映的 standard_concept（供擴 STD_MAP）
    ok = 0
    for i, tk in enumerate(tks, 1):
        try:
            seen = set()
            for sc, tag in statements(tk):
                mid = STD_MAP.get(sc)
                if mid:
                    key = (mid, tag)
                    if key not in seen:
                        found[mid][tag] += 1
                        seen.add(key)
                else:
                    unmapped[sc] += 1
            ok += 1
        except Exception:
            pass
        if i % 25 == 0:
            print(f"  ...{i}/{len(tks)}")

    # 建議：多家在用、我 map 沒有的標籤
    patch = {}
    print(f"\n{'='*56}\n建議補丁（{ok} 家；≥{args.min} 家在用且我 map 缺）：")
    for mid in sorted(found):
        if mid in SKIP_AGG:
            continue
        miss = [(t, n) for t, n in found[mid].most_common()
                if n >= args.min and t not in have.get(mid, set())
                and not any(nz in t for nz in NOISE)]
        if miss:
            patch[mid] = [t for t, _ in miss]
            zh = next((c["zh"] for c in m["concepts"] if c["id"] == mid), mid)
            print(f"\n  {mid} ({zh}):")
            for t, n in miss:
                print(f"    + {t}  ({n} 家)")

    json.dump(patch, open(os.path.join(ROOT, "tools", "map_patch.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"\n補丁寫入 tools/map_patch.json（{sum(len(v) for v in patch.values())} 個新標籤）")
    print("\n未對映的 standard_concept（可擴 STD_MAP，前 15）：")
    for sc, n in unmapped.most_common(15):
        print(f"  {sc}  ({n})")


if __name__ == "__main__":
    main()
