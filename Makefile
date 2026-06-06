.PHONY: install backend frontend smoke dev clean redis

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
	cd frontend && npm install

backend:
	cd backend && .venv/bin/python -m uvicorn app.main:app --reload --port 8000

frontend:
	cd frontend && npm run dev

smoke:
	cd backend && .venv/bin/python scripts/smoke.py

redis:
	docker compose up -d

dev:
	@echo "Run 'make backend' and 'make frontend' in two terminals."

clean:
	rm -rf backend/.venv frontend/node_modules frontend/dist
