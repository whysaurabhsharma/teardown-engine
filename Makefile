.PHONY: test smoke signal backup verify

test:
	uv run pytest

smoke:
	uv run te smoke

BOARDS ?= data/boards.txt
signal:
	uv run te signal collect --boards-file $(BOARDS)

backup:
	uv run te backup

verify:
	uv run te verify
