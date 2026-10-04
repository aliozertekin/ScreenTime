.PHONY: windows windows-clean test
windows:
	./scripts/build-windows.sh
windows-clean:
	./scripts/build-windows.sh --clean
test:
	python3 -m pytest -q
