from fastapi import FastAPI, HTTPException, Request, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import select
from .database import init_db, get_session
from .models import Material
from .schemas import MaterialCreate, EntryCreate, MaterialOut
from . import crud
import asyncio
import logging
from pathlib import Path
import json
from datetime import datetime
import tempfile
from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
import os

logger = logging.getLogger("uvicorn.error")

app = FastAPI(title="Inventory API")
templates = Jinja2Templates(directory="app/templates")

# Paths to your uploaded files (you already uploaded these to /mnt/data/)
FICHA_PATH = Path("/mnt/data/ficha_tecnica.json")
MATERIAL_IDS_PATH = Path("/mnt/data/material_ids.json")

# In-memory mappings loaded at startup:
# product_name -> list of {material, quantidade}
BOM_MAP = {}
# material_name -> material_sku (MAT-0001...)
MATERIAL_NAME_TO_SKU = {}

# ------------------------------------------------------------
# Startup / load mappings / DB init
# ------------------------------------------------------------
@app.on_event("startup")
async def startup_event():
    init_db()
    load_mappings()
    app.state._lowstock_task = asyncio.create_task(low_stock_watcher())

def load_mappings():
    global BOM_MAP, MATERIAL_NAME_TO_SKU
    try:
        if FICHA_PATH.exists():
            with open(FICHA_PATH, "r", encoding="utf-8") as f:
                BOM_MAP = json.load(f)
            logger.info(f"Loaded BOM map with {len(BOM_MAP)} products from {FICHA_PATH}")
        else:
            BOM_MAP = {}
            logger.warning(f"ficha_tecnica.json not found at {FICHA_PATH}")

        if MATERIAL_IDS_PATH.exists():
            with open(MATERIAL_IDS_PATH, "r", encoding="utf-8") as f:
                MATERIAL_NAME_TO_SKU = json.load(f)
            logger.info(f"Loaded material_ids with {len(MATERIAL_NAME_TO_SKU)} entries from {MATERIAL_IDS_PATH}")
        else:
            MATERIAL_NAME_TO_SKU = {}
            logger.warning(f"material_ids.json not found at {MATERIAL_IDS_PATH}")

    except Exception as e:
        logger.exception("Erro ao carregar mappings: %s", e)
        BOM_MAP = {}
        MATERIAL_NAME_TO_SKU = {}

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
                    # Aqui podemos enviar email/webhook; por enquanto apenas log
                    logger.warning(f"[LOW STOCK] SKU={m.sku} name={m.name} qty={m.quantity} min={m.min_quantity}")
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception("Erro no watcher de estoque baixo: %s", e)
            await asyncio.sleep(10)

# ------------------------------------------------------------
# NEW ENDPOINT: Materiais primas a partir do material_ids.json
# ------------------------------------------------------------
@app.get("/api/materias_primas")
def get_materias_primas():
    if MATERIAL_IDS_PATH.exists():
        with open(MATERIAL_IDS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Exemplo: {nome: sku}
        return [{"name": n, "sku": sku} for n, sku in data.items()]
    return []

# ------------------------------------------------------------
# HTML pages
# ------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def homepage(request: Request):
    with get_session() as session:
        materials = session.exec(select(Material)).all()
    # build metrics
    total_materials = len(materials)
    low_count = sum(1 for m in materials if m.quantity <= m.min_quantity)
    valor_total = 0.0  # se você tiver preço por material, calcule aqui
    metrics = {
        "total_materials": total_materials,
        "low_stock_count": low_count,
        "pedidos_hoje_count": 0,
        "valor_total_estoque": valor_total,
    }
    return templates.TemplateResponse("dashboard.html", {"request": request, "metrics": metrics, "materials": materials})

@app.get("/entrada", response_class=HTMLResponse)
def entrada_page(request: Request):
    with get_session() as session:
        materiais = session.exec(select(Material)).all()
    return templates.TemplateResponse("entrada.html", {"request": request, "materiais": materiais})

@app.post("/entrada")
async def entrada_submit(nome: str = Form(...), quantidade: int = Form(...)):
    if quantidade <= 0:
        raise HTTPException(status_code=400, detail="Quantidade deve ser maior que zero")
    # Buscar material pelo nome (exato)
    with get_session() as session:
        material = session.exec(select(Material).where(Material.name == nome)).first()
    if not material:
        # tentar match case-insensitive
        with get_session() as session:
            material = session.exec(select(Material).where(Material.name.ilike(f"%{nome}%"))).first()
    if not material:
        raise HTTPException(status_code=404, detail=f"Material '{nome}' não encontrado")
    # Atualiza quantidade (positivo = entrada)
    updated = crud.update_material_quantity(material, quantidade)
    # Registrar entrada (histórico). criamos entry com quantidade positiva
    crud.create_entry(sku=material.sku, quantity=quantidade, note="Entrada manual")
    return RedirectResponse(url="/", status_code=303)

# ------------------------------------------------------------
# API endpoints (JSON)
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

# ------------------------------------------------------------
# Webhook: receber pedidos do PedidoOK e processar baixa automática
# Espera payload exemplo (flexível):
# {
#   "order_id": "12345",
#   "items": [
#       {"product_code": "1130 - TORNEIRA METAL JARDIM LONGA AMARELA", "qty": 2},
#       {"product_code": "1158 - TORN. PIA RETA METAL ...", "qty": 1}
#   ]
# }
# ------------------------------------------------------------
@app.post("/webhook/pedidook")
async def webhook_pedidook(payload: dict, background_tasks: BackgroundTasks):
    try:
        items = payload.get("items") or []
        if not items:
            return JSONResponse({"ok": False, "error": "payload sem items"}, status_code=400)

        # process asynchronously in background to responder rápido
        background_tasks.add_task(process_pedidook_items, items, payload.get("order_id"))
        return {"ok": True, "message": "Pedido recebido e sendo processado em background"}
    except Exception as e:
        logger.exception("Erro no webhook: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

def process_pedidook_items(items, order_id=None):
    """
    items: list of dicts with product_code/name and qty
    Usa ficheiro BOM_MAP e MATERIAL_NAME_TO_SKU para deduzir materiais.
    """
    logger.info(f"Processando pedido {order_id} com {len(items)} items")
    for it in items:
        # vários formatos possíveis: 'product_code' ou 'sku' ou 'product_name'
        prod_key = it.get("product_code") or it.get("product_name") or it.get("sku") or it.get("code")
        qty = int(it.get("qty") or it.get("quantity") or 1)
        if not prod_key:
            logger.warning("Item sem product identifier, pulando: %s", it)
            continue

        # Tentar obter BOM entry pelo key exato
        bom_entry = BOM_MAP.get(prod_key)
        if not bom_entry:
            # tentar contains / case-insensitive match
            for k in BOM_MAP.keys():
                if prod_key.lower() in k.lower() or k.lower() in prod_key.lower():
                    bom_entry = BOM_MAP[k]
                    break

        if not bom_entry:
            logger.warning("Produto não encontrado na ficha técnica: %s", prod_key)
            continue

        # bom_entry é lista de dicts {material, quantidade}
        for comp in bom_entry:
            mat_name = comp.get("material")
            mat_qty_per_product = float(comp.get("quantidade") or 1)
            required = int(mat_qty_per_product * qty)

            # mapear nome do material para SKU (MAT-XXXX) se existir no material_ids
            sku = MATERIAL_NAME_TO_SKU.get(mat_name) or None

            # localizar no DB pelo sku (preferível) ou pelo nome
            material = None
            with get_session() as session:
                if sku:
                    material = session.exec(select(Material).where(Material.sku == sku)).first()
                if not material:
                    # tentar por nome exato
                    material = session.exec(select(Material).where(Material.name == mat_name)).first()
                if not material:
                    # tentar contains (case-insensitive)
                    material = session.exec(select(Material).where(Material.name.ilike(f"%{mat_name}%"))).first()

            if not material:
                logger.warning("Componente '%s' não cadastrado no estoque — não foi possível debitar %d unidades", mat_name, required)
                continue

            # Debitar estoque (usar delta negativo)
            try:
                crud.update_material_quantity(material, -required)
                # registrar entry negativa como saída
                crud.create_entry(sku=material.sku, quantity=-required, note=f"Saída por pedido {order_id}")
                logger.info("Debitado %d de %s (sku=%s) para pedido %s", required, material.name, material.sku, order_id)
            except Exception as e:
                logger.exception("Erro ao debitar material %s: %s", material.name, e)

# ------------------------------------------------------------
# Export: gera um Excel com o estoque atual e um gráfico de barras
# Retorna o arquivo .xlsx para download
# ------------------------------------------------------------
@app.get("/export/report")
def export_report():
    with get_session() as session:
        materials = session.exec(select(Material)).all()

    # criar workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Estoque"

    # cabeçalho
    ws.append(["SKU", "Nome", "Quantidade", "Estoque Mínimo", "Última Atualização"])

    for m in materials:
        updated_at = getattr(m, "updated_at", None)
        updated_str = updated_at.strftime("%Y-%m-%d %H:%M:%S") if updated_at else ""
        ws.append([m.sku, m.name, m.quantity, m.min_quantity, updated_str])

    # criar gráfico de barras com as 20 maiores quantidades para ficar legível
    # ordenar por quantidade descendente
    materials_sorted = sorted(materials, key=lambda x: x.quantity, reverse=True)[:20]
    start_row = 2
    # escrever seção para gráfico (nome e quantidade)
    chart_sheet = wb.create_sheet("ChartData")
    chart_sheet.append(["Nome", "Quantidade"])
    for mat in materials_sorted:
        chart_sheet.append([mat.name, mat.quantity])

    chart = BarChart()
    chart.title = "Top 20 Materiais por Quantidade"
    chart.y_axis.title = "Quantidade"
    chart.x_axis.title = "Material"

    data = Reference(chart_sheet, min_col=2, min_row=1, max_row=1 + len(materials_sorted))
    cats = Reference(chart_sheet, min_col=1, min_row=2, max_row=1 + len(materials_sorted))
    chart.add_data(data, titles_from_data=True)
    chart.set_categories(cats)
    chart.height = 12
    chart.width = 24
    chart_sheet.add_chart(chart, "D2")

    # gravar em temp file e retornar
    tmpdir = tempfile.gettempdir()
    filename = f"estoque_report_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.xlsx"
    file_path = os.path.join(tmpdir, filename)
    wb.save(file_path)

    return FileResponse(path=file_path, filename=filename, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
