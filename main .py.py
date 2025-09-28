from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
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

# ------------------------------------------------------------
# Startup e Shutdown
# ------------------------------------------------------------
@app.on_event("startup")
async def startup_event():
    init_db()
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

async def low_stock_watcher():
    while True:
        try:
            lows = crud.low_stock_materials()
            if lows:
                for m in lows:
                    logger.warning(f"[LOW STOCK] SKU={m.sku} name={m.name} qty={m.quantity} min={m.min_quantity}")
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception("Erro no watcher de estoque baixo: %s", e)
            await asyncio.sleep(10)

# ------------------------------------------------------------
# Rotas HTML
# ------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def homepage(request: Request):
    # mostra dashboard
    with get_session() as session:
        total_materials = session.exec(select(Material)).count()
        lows = crud.low_stock_materials()
    metrics = {
        "total_materials": total_materials,
        "low_stock_count": len(lows),
        "pedidos_hoje_count": 0,
        "valor_total_estoque": 0,  # pode calcular depois
    }
    return templates.TemplateResponse("dashboard.html", {"request": request, "metrics": metrics})

@app.get("/entrada", response_class=HTMLResponse)
def entrada_page(request: Request):
    # lista nomes dos materiais para o <select>
    with get_session() as session:
        materiais = session.exec(select(Material)).all()
    return templates.TemplateResponse("entrada.html", {"request": request, "materiais": materiais})

@app.post("/entrada")
async def entrada_submit(nome: str = Form(...), quantidade: int = Form(...)):
    try:
        # Buscar material pelo nome
        with get_session() as session:
            material = session.exec(select(Material).where(Material.name == nome)).first()

        if not material:
            raise HTTPException(status_code=404, detail=f"Material '{nome}' não encontrado")

        # Atualizar quantidade
        updated = crud.update_material_quantity(material, quantidade)

        # Registrar entrada no histórico
        crud.create_entry(sku=material.sku, quantity=quantidade, note="Entrada manual")

        return RedirectResponse(url="/", status_code=303)

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# ------------------------------------------------------------
# Rotas API JSON
# ------------------------------------------------------------
@app.post("/api/materials", response_model=MaterialOut)
async def create_material(item: MaterialCreate):
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

@app.post("/api/entries/json")
def create_entry_json(payload: EntryCreate):
    try:
        entry, material = crud.create_entry(sku=payload.sku, quantity=payload.quantity, note=payload.note)
        return {"entry_id": entry.id, "sku": material.sku, "new_quantity": material.quantity}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/stock")
def stock_list():
    with get_session() as session:
        materials = session.exec(select(Material)).all()
        return [{"sku": m.sku, "name": m.name, "quantity": m.quantity, "min_quantity": m.min_quantity} for m in materials]

@app.get("/api/stock/low")
def stock_low():
    lows = crud.low_stock_materials()
    return [{"sku": m.sku, "name": m.name, "quantity": m.quantity, "min_quantity": m.min_quantity} for m in lows]
