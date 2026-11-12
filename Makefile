.PHONY: test smoke backup verify

test:
	uv run pytest

smoke:
	uv run te smoke

backup:
	uv run te backup

verify:
	uv run te verify
