.PHONY: install backend frontend smoke dev clean test-nlu

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
	cd frontend && npm install

backend:
	cd backend && .venv/bin/python -m uvicorn app.main:app --reload --port 8000

frontend:
	cd frontend && npm run dev

smoke:
	cd backend && .venv/bin/python scripts/smoke.py

# Pytest run gated to the NLU / response-translation suites. Used in CI and
# whenever the bilingual surface area changes.
test-nlu:
	cd backend && .venv/bin/python -m pytest tests/test_bilingual.py -v

dev:
	@echo "Run 'make backend' and 'make frontend' in two terminals."

clean:
	rm -rf backend/.venv frontend/node_modules frontend/dist
