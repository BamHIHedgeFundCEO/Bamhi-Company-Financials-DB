"""
Excel 生成服務。
POST /generate  { cacheKey, financials, segments? }  →  { url }（R2）
R2 未設定（本地開發）時直接串流 .xlsx 回傳。
本服務不打 SEC——數據由 web 層準備好帶進來。
"""
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

import r2
from workbook import build_workbook

app = FastAPI(title="BamHI Excel Service")


class GeneratePayload(BaseModel):
    cacheKey: str
    financials: dict
    # ⚠️ 沒宣告在這裡的欄位，Pydantic 會「靜默丟棄」——不報錯，是無聲消失。
    # 新增 payload 欄位時務必同步加在這裡；而且測試要打 /generate，不能只直接
    # 呼叫 build_workbook()，否則繞過本層、本機全綠但線上少東西。
    segments: dict | None = None


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/generate")
def generate(payload: GeneratePayload):
    if not payload.financials.get("periods"):
        raise HTTPException(status_code=422, detail="無任何季度資料")
    data = build_workbook(payload.model_dump())
    url = r2.upload(payload.cacheKey, data)
    if url:
        return {"url": url}
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{payload.cacheKey}"'},
    )
