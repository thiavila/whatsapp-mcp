.PHONY: ci ci\:local
ci:
	python3 scripts/ci/check.py
ci\:local:
	bash scripts/ci/local-ci.sh
