.PHONY: company seed lint test conformance

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
