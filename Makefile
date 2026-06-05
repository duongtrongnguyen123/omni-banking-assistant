.PHONY: install backend frontend smoke test-nlu dev clean

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
	cd frontend && npm install

backend:
	cd backend && .venv/bin/python -m uvicorn app.main:app --reload --port 8000

frontend:
	cd frontend && npm run dev

smoke:
	cd backend && .venv/bin/python scripts/smoke.py

# Adversarial NLU corpus — exercises the rule-based fallback that runs
# when Groq/Gemini are rate-limited. See backend/tests/test_nlu_corpus.py.
# `-s` lets the end-of-session per-category accuracy report reach stdout.
test-nlu:
	cd backend && .venv/bin/python -m pytest tests/test_nlu_corpus.py -v -s

dev:
	@echo "Run 'make backend' and 'make frontend' in two terminals."

clean:
	rm -rf backend/.venv frontend/node_modules frontend/dist
