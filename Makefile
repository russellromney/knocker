.PHONY: help build-py build-ext test-rust test-python test-node test

help:
	@echo "knocker development targets"
	@echo ""
	@echo "  make test-rust   - cargo test for knocker-core"
	@echo "  make build-py    - build/install the Python extension with maturin"
	@echo "  make build-ext   - build the SQLite loadable extension"
	@echo "  make test-python - run Python tests with uv"
	@echo "  make test-node   - run the Node contract smoke tests"
	@echo "  make test        - rust + python + node"

test-rust:
	cargo test -p knocker-core

build-py:
	uv run --group dev maturin develop --manifest-path packages/knocker/Cargo.toml

build-ext:
	cargo build -p knocker-extension --release

test-python: build-py
	uv run --group dev pytest tests/

test-node: build-ext
	npm --prefix packages/knocker-node test

test: test-rust test-python test-node
