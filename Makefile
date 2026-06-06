.PHONY: install backend frontend smoke check reset test-nlu test verify docker-build docker-run docker-redis dev clean redis

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

# Portable demo image (see backend/Dockerfile). Build time ~90s cold,
# ~5s warm (pip wheel cache).
docker-build:
	cd backend && docker build -t omni-backend:latest .

# Run the built image standalone (memory-backed sessions, no Redis).
# Surfaces /docs at http://localhost:8000/docs, frontend Vite at :5173.
docker-run:
	docker run --rm -p 8000:8000 \
	    -e OMNI_SKIP_EMBED_BACKFILL=1 \
	    -e OFFLINE_DEMO=$${OFFLINE_DEMO:-0} \
	    --name omni-backend \
	    omni-backend:latest

# Start the optional Redis container so the backend can use OMNI_SESSION_BACKEND=redis.
# See docker-compose.yml at the repo root.
docker-redis:
	docker compose up -d redis
	@echo "Redis on localhost:6379. Start backend with OMNI_SESSION_BACKEND=redis."

# Bring up the full docker-compose stack (Postgres + Redis). From the
# hien branch — used when the backend is configured with
# OMNI_STORE_BACKEND=postgres.
redis:
	docker compose up -d

dev:
	@echo "Run 'make backend' and 'make frontend' in two terminals."

clean:
	rm -rf backend/.venv frontend/node_modules frontend/dist
