.PHONY: test-fast test-integration test-ui test-browser test-release test-hardware test-mutations release check-js check-version check-lock quality

PYTHON ?= $(if $(BASSWIESN_PYTHON),$(BASSWIESN_PYTHON),python3)

check-js:
	bash tools/check_js.sh

check-version:
	"$(PYTHON)" tools/check_version.py

check-lock:
	"$(PYTHON)" tools/check_dependency_lock.py

quality: check-version check-lock check-js
	"$(PYTHON)" -m compileall -q basswiesn tests tools
	git diff --check
	"$(PYTHON)" -m ruff check .
	"$(PYTHON)" -m mypy

test-mutations:
	"$(PYTHON)" tools/check_safety_mutations.py

test-fast:
	bash tools/test_fast.sh

test-integration:
	bash tools/test_integration.sh

test-ui:
	bash tools/test_ui.sh

# Backwards-compatible alias. Human/browser QA documentation uses test-ui.
test-browser:
	bash tools/test_ui.sh

test-release:
	bash tools/test_release.sh

test-hardware:
	bash tools/test_hardware.sh

release:
	bash tools/package_release.sh
