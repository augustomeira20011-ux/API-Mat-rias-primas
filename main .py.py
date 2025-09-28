from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlmodel import select
from .database import init_db, get_session
from .models import Material
from .schemas import MaterialCreate, EntryCreate, MaterialOut
from . import crud
import asyncio
import logging

logger = logging.getLogger("uvicorn.error")

app = FastAPI(title="Inventory API")

templates = Jinja2Templates(directory="app/templates")
# if you want static files:
# app.mount("/static", StaticFiles(directory="app/static"), name="static")

@app.on_event("startup")
async def startup_event():
    init_db()
    # start background task for low-stock check
    app.state._lowstock_task = asyncio.create_task(low_stock_watcher())

@app.on_event("shutdown")
async def shutdown_event():
    task = getattr(app.state, "_lowstock_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

# simple background watcher — adapta para enviar e-mail/Slack etc.
async def low_stock_watcher():
    while True:
        try:
            lows = crud.low_stock_materials()
            if lows:
                for m in lows:
                    logger.warning(f"[LOW STOCK] SKU={m.sku} name={m.name} qty={m.quantity} min={m.min_quantity}")
                    # aqui você pode integrar envio de alerta (email, webhook, push)
            await asyncio.sleep(60)  # checa a cada 60s (ajuste conforme necessidade)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception("Erro no watcher de estoque baixo: %s", e)
            await asyncio.sleep(10)

# Web UI
@app.get("/", response_class=HTMLResponse)
def homepage(request: Request):
    return templates.TemplateResponse("entry_form.html", {"request": request})

# CRUD materiais
@app.post("/api/materials", response_model=MaterialOut)
def create_material(item: MaterialCreate = None, request: Request = None):
    # aceitar tanto application/json quanto form
    if request and request.headers.get("content-type","").startswith("application/x-www-form-urlencoded"):
        form = request._form if hasattr(request, "_form") else None
    try:
        material = crud.create_material(item)
        return material
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/materials", response_model=list[MaterialOut])
def list_materials(skip: int = 0, limit: int = 100):
    return crud.list_materials(skip=skip, limit=limit)

@app.get("/api/materials/{sku}", response_model=MaterialOut)
def get_material_by_sku(sku: str):
    m = crud.get_material_by_sku(sku)
    if not m:
        raise HTTPException(status_code=404, detail="Material não encontrado")
    return m

# Entrada de estoque
@app.post("/api/entries")
async def create_entry(sku: str = Form(...), quantity: int = Form(...), note: str | None = Form(None)):
    # endpoint aceita form (do HTML) e também JSON via POST normal (use curl)
    try:
        entry, material = crud.create_entry(sku=sku, quantity=quantity, note=note)
        # se for form request vindo do browser, redirecione para /
        return RedirectResponse(url="/", status_code=303)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

# API JSON-friendly para entrada
@app.post("/api/entries/json")
def create_entry_json(payload: EntryCreate):
    try:
        entry, material = crud.create_entry(sku=payload.sku, quantity=payload.quantity, note=payload.note)
        return {"entry_id": entry.id, "sku": material.sku, "new_quantity": material.quantity}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

# consulta de saldo geral
@app.get("/api/stock")
def stock_list():
    with get_session() as session:
        materials = session.exec(select(Material)).all()
        return [{"sku": m.sku, "name": m.name, "quantity": m.quantity, "min_quantity": m.min_quantity} for m in materials]

# materiais com estoque baixo
@app.get("/api/stock/low")
def stock_low():
    lows = crud.low_stock_materials()
    return [{"sku": m.sku, "name": m.name, "quantity": m.quantity, "min_quantity": m.min_quantity} for m in lows]
