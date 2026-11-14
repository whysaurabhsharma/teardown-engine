.PHONY: test smoke signal careers resolve qualify capture extract people judge build queue sync metrics backup verify

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
	uv run te resolve dedupe
	uv run te resolve segment --seeds $(SEEDS)

qualify:
	uv run te qualify

capture:
	uv run te capture --pilot

extract:
	uv run te extract text --pilot
	uv run te extract facts --pilot

people:
	uv run te people list --pilot
	uv run te people check --pilot

judge:
	uv run te judge --pilot

build:
	uv run te teardown check --pilot
	uv run te teardown render --pilot

# Gmail drafts only; a person reads and sends each one (te queue sent <id>).
queue:
	uv run te queue draft --pilot

# HubSpot is the view; SQLite stays the system of record (Steps 67, 76).
sync:
	uv run te sync
	uv run te backup

# From the database alone, no network (Step 78, Gate 6.2).
metrics:
	uv run te metrics
