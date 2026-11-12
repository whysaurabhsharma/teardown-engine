.PHONY: test smoke

test:
	uv run pytest

smoke:
	uv run te smoke
