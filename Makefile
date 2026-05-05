.PHONY: help build-py build-ext test-rust test-python test-node test-bun test-ruby test-go test-elixir test-elixir-setup test

help:
	@echo "knocker development targets"
	@echo ""
	@echo "  make test-rust   - cargo test for knocker-core"
	@echo "  make build-py    - build/install the Python extension with maturin"
	@echo "  make build-ext   - build the SQLite loadable extension"
	@echo "  make test-python - run Python tests with uv"
	@echo "  make test-node   - run the Node contract smoke tests"
	@echo "  make test-bun    - run the Bun contract smoke test"
	@echo "  make test-ruby   - run the Ruby contract smoke test"
	@echo "  make test-go     - run the Go contract smoke test"
	@echo "  make test-elixir-setup - fetch Elixir deps once"
	@echo "  make test-elixir - run the Elixir contract smoke test"
	@echo "  make test        - rust + python + node + bun + ruby + go + elixir"

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

test-bun: build-ext
	cd packages/knocker-bun && bun test

test-ruby: build-ext
	ruby packages/knocker-ruby/test/basic_test.rb

test-go: build-ext
	cd packages/knocker-go && go test ./...

test-elixir-setup:
	cd packages/knocker-elixir && mix deps.get

test-elixir: build-ext
	cd packages/knocker-elixir && mix test

test: test-rust test-python test-node test-bun test-ruby test-go test-elixir
