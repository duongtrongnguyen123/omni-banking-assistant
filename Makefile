.PHONY: install backend frontend smoke check reset test-nlu test verify dev clean

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
	cd frontend && npm install

backend:
	cd backend && .venv/bin/python -m uvicorn app.main:app --reload --port 8000

frontend:
	cd frontend && npm run dev

smoke:
	cd backend && .venv/bin/python scripts/smoke.py

# Pre-demo green-light: exits non-zero if any KB scenario, endpoint, or
# safety contract would break in front of judges. Runs in ~5s.
check:
	cd backend && .venv/bin/python scripts/check.py

# Panic button for pitch day. Wipes runtime DB, re-seeds, pre-trains the
# suggester, verifies all KB scenarios route correctly. ~10s warm, ~30s cold.
reset:
	cd backend && .venv/bin/python scripts/reset_demo.py

# Adversarial NLU corpus — exercises the rule-based fallback that runs
# when Groq/Gemini are rate-limited. See backend/tests/test_nlu_corpus.py.
# `-s` lets the end-of-session per-category accuracy report reach stdout.
test-nlu:
	cd backend && .venv/bin/python -m pytest tests/test_nlu_corpus.py -v -s

# Full backend test suite — NLU corpus + multi-turn integration + sessions.
test:
	cd backend && .venv/bin/python -m pytest tests/ -v

# Full pre-pitch verification — check + tests + frontend build, in order.
# Exits non-zero on first red. Use this before any demo, before any merge,
# before any push. Total wall time: ~45s warm.
verify:
	@echo "[1/3] make check (KB scenarios + safety contract)"
	@$(MAKE) -s check
	@echo ""
	@echo "[2/3] make test (NLU corpus + multi-turn integration)"
	@cd backend && GROQ_API_KEY= GEMINI_API_KEY= .venv/bin/python -m pytest tests/ -q --tb=line
	@echo ""
	@echo "[3/3] frontend build"
	@cd frontend && npm run build --silent
	@echo ""
	@echo "All checks green. Safe to demo."

dev:
	@echo "Run 'make backend' and 'make frontend' in two terminals."

clean:
	rm -rf backend/.venv frontend/node_modules frontend/dist
