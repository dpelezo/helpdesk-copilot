.PHONY: company seed lint test conformance tickets resolve publish

# Regenerate the mock company fixture (fixtures/company.json).
company:
	python -m app.mock.company

# Download and profile the seed dataset (needs network access to the Hugging Face Hub).
seed:
	python -m app.knowledge.seed

lint:
	ruff check app/ tests/ --ignore E501

test:
	pytest tests/ -q

# Run the MCP conformance suite on the six mock systems (needs npx, Node 22).
conformance:
	bash scripts/conformance.sh

# Regenerate the eval tickets (eval ground truth: change only in its own PR).
tickets:
	python -m app.mock.tickets

# Build the draft knowledge release and its validation report from the committed extraction (no model calls).
resolve:
	python -m app.knowledge.resolve

# Publish an approved release to Neo4j: make publish RELEASE=knowledge/releases/<id>.json
publish:
	python -m app.knowledge.publish publish $(RELEASE)
