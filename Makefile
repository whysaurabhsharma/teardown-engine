.PHONY: test smoke backup

test:
	uv run pytest

smoke:
	uv run te smoke

backup:
	uv run te backup
