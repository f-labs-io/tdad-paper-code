.PHONY: install test-visible compile-supportops

install:
	pip install -e .

test-visible:
	pytest -q tests_visible -m visible

compile-supportops:
	python scripts/compile_prompt.py --spec specs/core/supportops/v1/spec.yaml --prompt agent_artifacts/core/supportops/system_prompt.txt --test-cmd "pytest -q tests_visible/core/supportops -m visible"
