.PHONY: test smoke signal careers resolve backup verify

test:
	uv run pytest

smoke:
	uv run te smoke

BOARDS ?= data/boards.txt
signal:
	uv run te signal collect --boards-file $(BOARDS)

SEEDS ?= data/seeds.csv
careers:
	uv run te signal careers --seeds $(SEEDS)

backup:
	uv run te backup

verify:
	uv run te verify

resolve:
	uv run te resolve --seeds $(SEEDS)
