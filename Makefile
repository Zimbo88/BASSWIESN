.PHONY: test-fast test-integration test-ui test-browser test-release test-hardware release

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
