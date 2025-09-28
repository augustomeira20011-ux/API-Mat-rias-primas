# main.py
import os
import json
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, HTTPException, Header, Request, Form
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, Integer, Float, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import sessionmaker, declarative_base, relationship
from dotenv import load_dotenv
import pandas as pd
import matplotlib.pyplot as plt

# --- Load env ---
load_dotenv()
DEFAULT_LOW_STOCK_THRESHOLD = int(os.getenv("DEFAULT_LOW_STOCK_THRESHOLD", "5"))
TOKEN_PEDIDOK = os.getenv("TOKEN_PEDIDOK", "")
PEDIDOS_OK_BASE_URL = os.getenv("PEDIDOS_OK_BASE_URL", "")
MOCK_API = os.getenv("MOCK_API", "false").lower() == "true"

# --- Load files ---
BASE_DIR = Path(__file__).parent
MATERIAL_IDS_PATH = BASE_DIR / "material_ids.json"
FICHA_TEC_PATH = BASE_DIR / "ficha_tecnica.json"

with open(MATERIAL_IDS_PATH, encoding="utf-8") as f:
    MATERIAL_IDS = json.load(f)

with open(FICHA_TEC_PATH, encoding="utf-8") as f:
    FICHA_TEC = json.load(f)

# --- DB setup ---
DATABASE_URL = "sqlite:///./inventory.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

class Material(Base):
    __tablename__ = "materials"
    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    quantity = Column(Float, default=0.0)
    low_threshold = Column(Integer, default=DEFAULT_LOW_STOCK_THRESHOLD)
    low = Column(Boolean, default=False)

class StockMovement(Base):
    __tablename__ = "stock_movements"
    id = Column(Integer, primary_key=True, autoincrement=True)
    material_id = Column(String, ForeignKey("materials.id"))
    delta = Column(Float)
    type = Column(String)
    reference = Column(String, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    material = relationship("Material")

Base.metadata.create_all(bind=engine)

# --- Ensure materials exist in DB ---
def ensure_materials():
    db = SessionLocal()
    try:
        for name, matid in MATERIAL_IDS.items():
            if not db.get(Material, matid):
                m = Material(id=matid, name=name, quantity=0.0, low_threshold=DEFAULT_LOW_STOCK_THRESHOLD)
                db.add(m)
        db.commit()
    finally:
        db.close()

ensure_materials()

# --- App ---
app = FastAPI(title="Inventory API - PedidosOK integration")

# --- Static files and templates ---
EXPORT_DIR = BASE_DIR / "exports"
EXPORT_DIR.mkdir(exist_ok=True)

if not (BASE_DIR / "static").exists():
    os.mkdir(BASE_DIR / "static")
if not (BASE_DIR / "templates").exists():
    os.mkdir(BASE_DIR / "templates")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# --- Schemas ---
class StockIn(BaseModel):
    material_name: str
    quantity: float
    reference: str | None = None

class PedidoItem(BaseModel):
    sku: str
    quantity: int = 1

class PedidoOK(BaseModel):
    id: str
    items: list[PedidoItem]

# --- Helpers ---
def get_matid_by_name(name: str) -> str | None:
    return MATERIAL_IDS.get(name)

def check_and_mark_low(db, material: Material):
    material.low = material.quantity <= (material.low_threshold or DEFAULT_LOW_STOCK_THRESHOLD)
    db.add(material)

# --- Endpoints API ---
@app.post("/stock/in")
def stock_in(payload: StockIn):
    db = SessionLocal()
    try:
        matid = get_matid_by_name(payload.material_name)
        if not matid:
            raise HTTPException(status_code=400, detail="Material name not recognized")
        mat = db.get(Material, matid)
        mat.quantity += payload.quantity
        mv = StockMovement(material_id=matid, delta=payload.quantity, type="entrada", reference=payload.reference)
        db.add(mv)
        check_and_mark_low(db, mat)
        db.commit()
        return {"material_id": matid, "new_quantity": mat.quantity}
    finally:
        db.close()

@app.get("/stock/{material_id}")
def get_stock(material_id: str):
    db = SessionLocal()
    try:
        mat = db.get(Material, material_id)
        if not mat:
            raise HTTPException(status_code=404, detail="Material not found")
        return {"id": mat.id, "name": mat.name, "quantity": mat.quantity, "low": mat.low}
    finally:
        db.close()

@app.get("/stock")
def list_stock(below_threshold: bool | None = None):
    db = SessionLocal()
    try:
        q = db.query(Material)
        if below_threshold:
            q = q.filter(Material.low == True)
        items = q.all()
        return [{"id": m.id, "name": m.name, "quantity": m.quantity, "low": m.low} for m in items]
    finally:
        db.close()

@app.post("/webhook/pedidook")
async def pedidook_webhook(req: Request, x_token: str | None = Header(None)):
    if TOKEN_PEDIDOK and x_token != TOKEN_PEDIDOK:
        raise HTTPException(status_code=401, detail="Invalid token")
    payload = await req.json()
    try:
        pedido = PedidoOK(**payload)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid payload format")
    db = SessionLocal()
    insufficient = []
    try:
        totals = {}
        for item in pedido.items:
            components = FICHA_TEC.get(item.sku)
            if not components:
                raise HTTPException(status_code=400, detail=f"SKU {item.sku} not found in ficha_tecnica")
            for comp in components:
                matid = get_matid_by_name(comp["material"])
                totals[matid] = totals.get(matid, 0.0) + float(comp["quantidade"]) * item.quantity
        for matid, amount in totals.items():
            mat = db.get(Material, matid)
            if mat.quantity < amount:
                insufficient.append({"material_id": matid, "needed": amount, "available": mat.quantity})
        if insufficient:
            return {"status": "insufficient_stock", "details": insufficient}
        for matid, amount in totals.items():
            mat = db.get(Material, matid)
            mat.quantity -= amount
            mv = StockMovement(material_id=matid, delta=-amount, type="pedido", reference=pedido.id)
            db.add(mv)
            check_and_mark_low(db, mat)
        db.commit()
        return {"status": "ok", "pedido_id": pedido.id, "debited": totals}
    finally:
        db.close()

# --- Frontend routes ---
@app.get("/")
def home(request: Request, below_threshold: bool | None = None):
    db = SessionLocal()
    try:
        q = db.query(Material)
        if below_threshold:
            q = q.filter(Material.low == True)
        materials = q.all()
        return templates.TemplateResponse("index.html", {"request": request, "materials": materials})
    finally:
        db.close()

@app.get("/stock/in/form")
def stock_in_form(request: Request):
    return templates.TemplateResponse("stock_in.html", {"request": request, "materials": MATERIAL_IDS.keys()})

@app.post("/stock/in/form")
def stock_in_submit(request: Request, material_name: str = Form(...), quantity: float = Form(...), reference: str = Form("")):
    db = SessionLocal()
    try:
        matid = get_matid_by_name(material_name)
        if not matid:
            return templates.TemplateResponse("stock_in.html", {"request": request, "error": "Material não encontrado", "materials": MATERIAL_IDS.keys()})
        mat = db.get(Material, matid)
        mat.quantity += quantity
        mv = StockMovement(material_id=matid, delta=quantity, type="entrada", reference=reference)
        db.add(mv)
        check_and_mark_low(db, mat)
        db.commit()
        return RedirectResponse("/", status_code=303)
    finally:
        db.close()

@app.get("/pedido/form")
def pedido_form(request: Request):
    return templates.TemplateResponse("pedido.html", {"request": request, "skus": FICHA_TEC.keys()})

@app.post("/pedido/form")
def pedido_submit(request: Request, sku: str = Form(...), quantity: int = Form(...), pedido_id: str = Form(...)):
    db = SessionLocal()
    insufficient = []
    try:
        totals = {}
        components = FICHA_TEC.get(sku)
        if not components:
            return templates.TemplateResponse("pedido.html", {"request": request, "error": f"SKU {sku} não encontrado", "skus": FICHA_TEC.keys()})
        for comp in components:
            matid = get_matid_by_name(comp["material"])
            totals[matid] = totals.get(matid, 0.0) + float(comp["quantidade"]) * quantity
        for matid, amount in totals.items():
            mat = db.get(Material, matid)
            if mat.quantity < amount:
                insufficient.append({"material_id": matid, "needed": amount, "available": mat.quantity})
        if insufficient:
            return templates.TemplateResponse("pedido.html", {"request": request, "error": "Estoque insuficiente", "details": insufficient, "skus": FICHA_TEC.keys()})
        for matid, amount in totals.items():
            mat = db.get(Material, matid)
            mat.quantity -= amount
            mv = StockMovement(material_id=matid, delta=-amount, type="pedido", reference=pedido_id)
            db.add(mv)
            check_and_mark_low(db, mat)
        db.commit()
        return RedirectResponse("/", status_code=303)
    finally:
        db.close()

@app.get("/report/view")
def report_view(request: Request):
    db = SessionLocal()
    try:
        mats = db.query(Material).all()
        df = pd.DataFrame([{"id": m.id, "name": m.name, "quantity": m.quantity, "low": m.low} for m in mats])
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        excel_path = EXPORT_DIR / f"stock_report_{timestamp}.xlsx"
        png_path = EXPORT_DIR / f"stock_chart_{timestamp}.png"

        with pd.ExcelWriter(excel_path) as writer:
            df.to_excel(writer, index=False, sheet_name="stock")

        top = df.sort_values("quantity", ascending=False).head(20)
        plt.figure(figsize=(10,6))
        plt.bar(top["name"], top["quantity"])
        plt.xticks(rotation=70, ha="right")
        plt.tight_layout()
        plt.savefig(png_path)
        plt.close()

        return templates.TemplateResponse("report.html", {"request": request, "excel": excel_path.name, "chart": png_path.name})
    finally:
        db.close()
