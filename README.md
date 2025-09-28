# Inventory API

Projeto de exemplo - API de controle de estoque (entrada de matéria-prima, consulta de saldo, alerta de estoque baixo).

## Como rodar (local)

1. Crie e ative virtualenv:
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
```

2. Instale dependências:
```bash
pip install -r requirements.txt
```

3. Rode:
```bash
uvicorn app.main:app --reload --port 8000
```

Abra http://localhost:8000/
