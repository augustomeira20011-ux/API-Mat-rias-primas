import os
import json
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, HTTPException, Header, Request, Form
from fastapi.responses import RedirectResponse, FileResponse # Adicionado FileResponse
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
# Use este token para proteger a rota do PedidosOK
TOKEN_PEDIDOK = os.getenv("TOKEN_PEDIDOK", "") 
MOCK_API = os.getenv("MOCK_API", "false").lower() == "true"

# --- Paths ---
BASE_DIR = Path(__file__).parent
MATERIAL_IDS_PATH = BASE_DIR / "material_ids.json"
FICHA_TEC_PATH = BASE_DIR / "ficha_tecnica.json"
EXPORT_DIR = BASE_DIR / "exports"
EXPORT_DIR.mkdir(exist_ok=True)

# --- Load JSON ---
try:
    with open(MATERIAL_IDS_PATH, encoding="utf-8") as f:
        MATERIAL_IDS = json.load(f)

    with open(FICHA_TEC_PATH, encoding="utf-8") as f:
        FICHA_TEC = json.load(f)
except FileNotFoundError as e:
    print(f"Erro: Arquivo de configuração não encontrado: {e}")
    # Cria dicionários vazios para evitar erro fatal, mas a API não será funcional
    MATERIAL_IDS = {}
    FICHA_TEC = {}

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
    # Novo campo para alertar estoque baixo
    low = Column(Boolean, default=False) 

class StockMovement(Base):
    __tablename__ = "stock_movements"
    id = Column(Integer, primary_key=True, autoincrement=True)
    material_id = Column(String, ForeignKey("materials.id"))
    delta = Column(Float)
    type = Column(String) # Ex: 'entrada', 'pedido', 'ajuste'
    reference = Column(String, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    material = relationship("Material")

Base.metadata.create_all(bind=engine)

# --- Ensure materials exist ---
def ensure_materials():
    db = SessionLocal()
    try:
        for name, matid in MATERIAL_IDS.items():
            if not db.get(Material, matid):
                m = Material(id=matid, name=name, quantity=0.0,
                             low_threshold=DEFAULT_LOW_STOCK_THRESHOLD)
                db.add(m)
        db.commit()
    finally:
        db.close()

ensure_materials()

app = FastAPI(title="Inventory API - PedidosOK integration")

# --- Static & templates ---
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# --- Schemas ---
class StockIn(BaseModel):
    """Schema para entrada de estoque via API"""
    material_name: str
    quantity: float
    reference: str | None = None

class PedidoItem(BaseModel):
    """Componente de um pedido do PedidosOK"""
    sku: str
    quantity: int = 1

class PedidoOK(BaseModel):
    """Estrutura do Webhook do PedidosOK"""
    id: str
    items: list[PedidoItem]

# --- Helpers ---
def get_matid_by_name(name: str) -> str | None:
    return MATERIAL_IDS.get(name)

def check_and_mark_low(db, material: Material):
    """Verifica e atualiza o status 'low' do material"""
    material.low = material.quantity <= (material.low_threshold or DEFAULT_LOW_STOCK_THRESHOLD)
    db.add(material)

# --- Endpoints ---

# Rota de Home (Interface Web)
@app.get("/")
def home(request: Request, below_threshold: bool | None = None):
    db = SessionLocal()
    try:
        q = db.query(Material)
        # Filtra para mostrar apenas itens com estoque baixo, se solicitado
        if below_threshold:
            q = q.filter(Material.low == True)
        materials = q.all()
        return templates.TemplateResponse("index.html", {"request": request, "materials": materials})
    finally:
        db.close()

# --- 1. ENTRADA DE ESTOQUE (API e Formulário) ---

# Formulário Web
@app.get("/stock/in/form")
def stock_in_form(request: Request):
    return templates.TemplateResponse("stock_in.html", {"request": request, "materials": MATERIAL_IDS.keys()})

@app.post("/stock/in/form")
def stock_in_submit(request: Request,
                    material_name: str = Form(...),
                    quantity: float = Form(...),
                    reference: str = Form("")):
    db = SessionLocal()
    try:
        matid = get_matid_by_name(material_name)
        if not matid:
            return templates.TemplateResponse("stock_in.html", {"request": request, "error": "Material não encontrado", "materials": MATERIAL_IDS.keys()})
        mat = db.get(Material, matid)
        mat.quantity += quantity
        mv = StockMovement(material_id=matid, delta=quantity, type="entrada_form", reference=reference)
        db.add(mv)
        check_and_mark_low(db, mat)
        db.commit()
        return RedirectResponse("/", status_code=303)
    finally:
        db.close()

# Rota API para Entrada de Estoque Externa
@app.post("/api/stock/in", status_code=201)
def api_stock_in(data: StockIn):
    """
    Registra a entrada de estoque de matéria-prima via API externa.
    """
    db = SessionLocal()
    try:
        matid = get_matid_by_name(data.material_name)
        if not matid:
            raise HTTPException(status_code=404, detail=f"Material '{data.material_name}' não encontrado.")
        
        mat = db.get(Material, matid)
        mat.quantity += data.quantity
        
        mv = StockMovement(
            material_id=matid, 
            delta=data.quantity, 
            type="entrada_api", 
            reference=data.reference
        )
        db.add(mv)
        check_and_mark_low(db, mat)
        db.commit()
        return {"message": f"Entrada de {data.quantity} de {data.material_name} registrada com sucesso."}
    finally:
        db.close()


# --- 2. CONTROLE DE ESTOQUE (Consulta de Saldo) ---

# Rota API para Consulta de Todo o Estoque
@app.get("/api/stock/all")
def api_get_stock_all():
    """
    Consulta o saldo atual de todas as matérias-primas.
    """
    db = SessionLocal()
    try:
        materials = db.query(Material).all()
        stock_data = [
            {
                "id": m.id,
                "name": m.name,
                "quantity": m.quantity,
                "low_threshold": m.low_threshold,
                "is_low": m.low
            } for m in materials
        ]
        return {"inventory": stock_data}
    finally:
        db.close()

# Rota API para Consulta de Estoque Baixo (ALERTA)
@app.get("/api/stock/low")
def api_get_low_stock():
    """
    Retorna a lista de todas as matérias-primas com estoque abaixo do limite (alerta).
    """
    db = SessionLocal()
    try:
        materials = db.query(Material).filter(Material.low == True).all()
        low_stock_data = [
            {
                "id": m.id,
                "name": m.name,
                "quantity": m.quantity,
                "low_threshold": m.low_threshold
            } for m in materials
        ]
        
        if not low_stock_data:
            return {"message": "Nenhuma matéria-prima em alerta de estoque baixo."}
            
        return {"alert_items": low_stock_data, "count": len(low_stock_data)}
    finally:
        db.close()


# --- 3. INTEGRAÇÃO COM PEDIDOS OK (Webhook) ---

# Formulário Web para simulação (existente)
@app.get("/pedido/form")
def pedido_form(request: Request):
    return templates.TemplateResponse("pedido.html", {"request": request, "skus": FICHA_TEC.keys()})

@app.post("/pedido/form")
def pedido_submit(request: Request,
                  sku: str = Form(...),
                  quantity: int = Form(...),
                  pedido_id: str = Form(...)):
    # Lógica de baixa de estoque via formulário (mantida)
    # ... (Seu código existente aqui)
    db = SessionLocal()
    insufficient = []
    try:
        totals = {}
        components = FICHA_TEC.get(sku)
        if not components:
            return templates.TemplateResponse("pedido.html", {"request": request, "error": f"SKU {sku} não encontrado", "skus": FICHA_TEC.keys()})
        for comp in components:
            mat_name = comp["material"]
            per_unit = float(comp["quantidade"])
            matid = get_matid_by_name(mat_name)
            totals[matid] = totals.get(matid, 0.0) + per_unit * quantity
        for matid, amount in totals.items():
            mat = db.get(Material, matid)
            if mat.quantity < amount:
                insufficient.append({"material_id": matid, "needed": amount, "available": mat.quantity})
        if insufficient:
            return templates.TemplateResponse("pedido.html", {"request": request, "error": "Estoque insuficiente", "details": insufficient, "skus": FICHA_TEC.keys()})
        for matid, amount in totals.items():
            mat = db.get(Material, matid)
            mat.quantity -= amount
            mv = StockMovement(material_id=matid, delta=-amount, type="pedido_form", reference=pedido_id)
            db.add(mv)
            check_and_mark_low(db, mat)
        db.commit()
        return RedirectResponse("/", status_code=303)
    finally:
        db.close()

# Rota API para Webhook (principal para o PedidosOK)
@app.post("/api/pedido/webhook", status_code=200)
def pedido_webhook(
    pedido_data: PedidoOK,
    # Header de segurança, PedidosOK deve enviá-lo
    x_api_token: str = Header(None, alias="X-PedidoOK-Token") 
):
    """
    Recebe um pedido do PedidoOK via Webhook/API e dá baixa automática no estoque.
    Requer o header 'X-PedidoOK-Token' para autenticação.
    """
    
    # 1. Autenticação/Segurança
    if TOKEN_PEDIDOK and x_api_token != TOKEN_PEDIDOK:
        raise HTTPException(status_code=401, detail="Token de segurança inválido ou ausente.")
    
    db = SessionLocal()
    insufficient = []
    
    try:
        # 2. Cálculo do Total de Consumo por Matéria-Prima
        totals = {}
        for item in pedido_data.items:
            sku = item.sku
            quantity = item.quantity
            
            components = FICHA_TEC.get(sku)
            if not components:
                # SKUs sem ficha técnica são ignorados para a baixa de estoque
                continue 

            for comp in components:
                mat_name = comp["material"]
                per_unit = float(comp["quantidade"])
                matid = get_matid_by_name(mat_name)
                
                if matid:
                    totals[matid] = totals.get(matid, 0.0) + per_unit * quantity
        
        # 3. Verificação de Estoque (Pré-cheque)
        for matid, amount_needed in totals.items():
            mat = db.get(Material, matid)
            if not mat or mat.quantity < amount_needed:
                insufficient.append({
                    "material_id": matid, 
                    "material_name": mat.name if mat else "Desconhecido",
                    "needed": amount_needed, 
                    "available": mat.quantity if mat else 0.0
                })

        if insufficient:
            # Retorna 409 (Conflict) informando que o estoque não permite a conclusão
            raise HTTPException(
                status_code=409, 
                detail="Estoque insuficiente para completar o pedido.", 
                headers={"Insufficient-Materials": json.dumps(insufficient)}
            )

        # 4. Baixa de Estoque e Registro de Movimento
        for matid, amount in totals.items():
            mat = db.get(Material, matid)
            if mat: 
                mat.quantity -= amount
                
                mv = StockMovement(
                    material_id=matid, 
                    delta=-amount, 
                    type="pedido_ok", 
                    reference=pedido_data.id
                )
                db.add(mv)
                check_and_mark_low(db, mat) # Atualiza o status de alerta

        db.commit()
        return {"message": f"Baixa de estoque para o Pedido OK ID {pedido_data.id} efetuada com sucesso."}

    except HTTPException as e:
        # Repassa 401/409/etc
        raise e
        
    except Exception as e:
        # Erro interno
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Erro interno ao processar pedido: {str(e)}")
        
    finally:
        db.close()


# --- EXPORTAÇÃO E RELATÓRIO COM GRÁFICOS ---

@app.get("/export/data")
def export_data_and_charts():
    """
    Gera o relatório de estoque (Excel) e o gráfico (PNG) e retorna os links para download.
    """
    db = SessionLocal()
    try:
        mats = db.query(Material).all()
        # Converte dados do DB para DataFrame
        df = pd.DataFrame([{"id": m.id, "name": m.name, "quantity": m.quantity, "low": m.low} for m in mats])
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        
        # 1. Geração do Excel
        excel_filename = f"stock_report_{timestamp}.xlsx"
        excel_path = EXPORT_DIR / excel_filename
        with pd.ExcelWriter(excel_path) as writer:
            df.to_excel(writer, index=False, sheet_name="stock")
            
        # 2. Geração do Gráfico PNG
        png_filename = f"stock_chart_{timestamp}.png"
        png_path = EXPORT_DIR / png_filename
        
        # Gráfico: Top 20 itens em estoque
        top = df.sort_values("quantity", ascending=False).head(20)
        plt.figure(figsize=(10,6))
        plt.bar(top["name"], top["quantity"], color='skyblue')
        plt.title("Top 20 Matérias-Primas por Quantidade em Estoque")
        plt.ylabel("Quantidade")
        plt.xlabel("Matéria-Prima")
        plt.xticks(rotation=70, ha="right")
        plt.grid(axis='y', alpha=0.7)
        plt.tight_layout()
        plt.savefig(png_path)
        plt.close()
        
        # Retorna o link para download via API
        return {
            "message": "Relatório e gráfico gerados com sucesso.",
            "excel_file": excel_filename,
            "chart_file": png_filename,
            "excel_download_url": f"/download/{excel_filename}",
            "chart_download_url": f"/download/{png_filename}"
        }
    finally:
        db.close()


@app.get("/download/{filename}")
def download_file(filename: str):
    """
    Permite o download dos arquivos de exportação gerados.
    """
    file_path = EXPORT_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado.")
    
    # Define o tipo de mídia (MIME type)
    if filename.endswith(".xlsx"):
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    elif filename.endswith(".png"):
        media_type = "image/png"
    else:
        media_type = "application/octet-stream"
    
    return FileResponse(
        path=file_path,
        filename=filename,
        media_type=media_type
    )

@app.get("/report/view")
def report_view(request: Request):
    """
    Gera o relatório e exibe links para download na interface web.
    """
    # Reusa a lógica de geração de arquivos da rota /export/data
    result = export_data_and_charts()
    if 'excel_file' in result:
        return templates.TemplateResponse(
            "report.html", 
            {"request": request, "excel": result["excel_file"], "chart": result["chart_file"]}
        )
    raise HTTPException(status_code=500, detail="Erro ao gerar relatório.")
