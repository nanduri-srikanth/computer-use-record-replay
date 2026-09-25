PY := .venv/bin/python
export PYTHONPATH := src:.

.PHONY: setup test test-live mock diagrams replay-demo evidence evidence-live eval eval-live scorecard walkthrough

setup:            ## create venv, install deps and Chromium
	uv venv -p 3.12 && uv pip install -e ".[dev]"
	chflags nohidden .venv/lib/python3.12/site-packages/*.pth 2>/dev/null || true
	.venv/bin/playwright install chromium
	npm install

test:             ## full offline suite (no LLM, no API key)
	.venv/bin/pytest -q

test-live:        ## real-model discovery test; key comes from the Keychain (scripts/store_api_key.sh)
	scripts/with_api_key.sh .venv/bin/pytest -q -m live

mock:             ## run the mock bank on :5055
	$(PY) -m cua.cli serve-mock

diagrams:         ## re-render docs/diagrams from docs/WORKFLOW.md
	python3 scripts/render_diagrams.py

replay-demo:      ## replay get_savings_balance against a running mock
	$(PY) -m cua.cli replay get_savings_balance --inputs '{"member_id": "M1001"}' --unattended

evidence:         ## stress matrix + flakiness + determinism into evidence/stress (no key needed)
	$(PY) scripts/stress.py --repeats 20 && python3 scripts/evidence_index.py

evidence-live:    ## real Claude discovery runs + replays into evidence/ (needs the Keychain key)
	scripts/with_api_key.sh $(PY) scripts/live_evidence.py && python3 scripts/evidence_index.py

eval:             ## deterministic replay eval + scorecard (no key)
	$(PY) -m cua.cli eval replay && $(PY) -m cua.cli eval report replay; $(PY) -m cua.cli eval scorecard --include-evals

eval-live:        ## judge calibration + live discovery eval (needs the Keychain key); pilot unless YES=1
	scripts/with_api_key.sh $(PY) -m cua.cli eval calibrate-judge
	scripts/with_api_key.sh $(PY) -m cua.cli eval discovery $(if $(YES),--yes,)

scorecard:        ## rebuild evals/SCORECARD.md from eval results + runtime ledgers (production and eval traffic)
	$(PY) -m cua.cli eval scorecard --include-evals

walkthrough:      ## record clips (Anthropic key) and build docs/walkthrough/walkthrough.mp4 with OpenAI narration (scripts/store_openai_key.sh) and music; needs ffmpeg
	scripts/with_api_key.sh $(PY) scripts/record_walkthrough.py --discovery
	scripts/with_openai_key.sh $(PY) scripts/make_walkthrough.py
