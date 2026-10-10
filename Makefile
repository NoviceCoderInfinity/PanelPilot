# Setup in 3 commands:   make setup && make data && make run      (then open http://127.0.0.1:8000)
PY      := .venv/bin/python
# first working JDK 17+: Homebrew openjdk@21 (keg-only, not visible to java_home), then java_home, then PATH
JAVA     = $(shell for j in "$$(brew --prefix openjdk@21 2>/dev/null)/bin/java" "$$(/usr/libexec/java_home -v 17+ 2>/dev/null)/bin/java" "$$(command -v java)"; do \
             [ -x "$$j" ] && "$$j" -version >/dev/null 2>&1 && echo "$$j" && break; done)
JAR     := synthea/synthea-with-dependencies.jar
SYNTHEA := synthea/output/csv
ASOF    ?= $(shell date +%Y-%m-%d)
N       ?= 10000

.PHONY: setup data run nightly app test dq extract-dry extract eval demo-reset scale fhir-import fhir-parity

setup:  ## Python env + Java + Synthea jar
	command -v uv >/dev/null || brew install uv
	uv venv --python 3.12 .venv -q --allow-existing
	uv pip install -q --python $(PY) -r requirements.txt
	test -n "$(JAVA)" || brew install openjdk@21
	mkdir -p synthea && (test -f $(JAR) || curl -fL -o $(JAR) https://github.com/synthetichealth/synthea/releases/download/master-branch-latest/synthea-with-dependencies.jar)

data:   ## seeded synthetic panel -> messy export (+ defect log) -> clean & messy DuckDB -> first nightly run
	test -n "$(JAVA)" || (echo "no JDK 17+ found: run make setup" && exit 1)
	test -f $(SYNTHEA)/patients.csv || (cd synthea && "$(JAVA)" -Xmx12g -jar synthea-with-dependencies.jar -s 42 -cs 42 -p $(N) \
	  --exporter.csv.export=true --exporter.fhir.export=false --exporter.years_of_history=20 \
	  --exporter.baseDirectory=./output Texas "Corpus Christi")
	$(PY) -m panelpilot.messify $(SYNTHEA) data/messy_csv
	$(PY) -m panelpilot.ingest $(SYNTHEA) data/clean.duckdb --as-of $(ASOF)
	$(PY) -m panelpilot.ingest data/messy_csv data/messy.duckdb --as-of $(ASOF)
	$(PY) -m panelpilot.nightly --as-of $(ASOF)

run:    ## the care manager's app (serves the latest nightly panel)
	test -f data/panel.duckdb || $(PY) -m panelpilot.nightly --as-of $(ASOF)
	$(PY) -m panelpilot.app

nightly:  ## re-run the nightly job (ASOF=YYYY-MM-DD; add REPLAY=1 to replay the export as of that date)
	$(PY) -m panelpilot.nightly --as-of $(ASOF) $(if $(REPLAY),--replay,)

test:   ## structural trust guarantees
	$(PY) -m pytest -q tests

dq:     ## clean-vs-messy honesty check: silent_flips must be 0
	$(PY) -m panelpilot.engine data/clean.duckdb rules/calibration --my 2025
	$(PY) -m panelpilot.engine data/messy.duckdb rules/calibration --my 2025
	$(PY) -m panelpilot.dq_eval data/clean.duckdb data/messy.duckdb data/messy_csv/_defects.csv --my 2025

extract-dry:  ## token + cost estimate, no API key needed
	$(PY) panelpilot/extract.py --dry-run

extract:      ## compile rulebooks with the LLM (needs ANTHROPIC_API_KEY in env or .env)
	$(PY) panelpilot/extract.py --budget 20

eval:         ## extraction quality vs gold
	$(PY) panelpilot/eval_extraction.py

demo-reset:   ## fresh messy export, no outreach history, replay clock at 2026-08-01
	$(PY) -m panelpilot.messify $(SYNTHEA) data/messy_csv
	rm -f data/ops.sqlite data/arrivals.csv
	$(PY) -m panelpilot.nightly --as-of 2026-08-01 --replay

scale:        ## measured nightly cost at 10k vs 100k (needs ~35 GB free temporarily)
	$(PY) -m panelpilot.scale_test

fhir-import:  ## data arriving as FHIR R4 bundles: FHIR=<dir> -> same six tables -> nightly on them
	$(PY) -m panelpilot.fhir $(or $(FHIR),synthea/output/fhir) data/fhir_csv
	$(PY) -m panelpilot.nightly --src data/fhir_csv --as-of $(ASOF)

fhir-parity:  ## same patients via FHIR and via CSV must give identical verdicts (seeded sample)
	$(PY) -m panelpilot.fhir_parity --sample $(or $(SAMPLE),2000)
