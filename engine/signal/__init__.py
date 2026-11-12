"""E1 Signal: collect GTM job posts into the signals table.

One module per public ATS board API (greenhouse, lever, ashby, workable), each
exposing `collect(board_slug) -> list[Signal]`; careers.py for the seed
companies' own careers pages (data/seeds.csv, loaded by seeds.py), handing off
to those collectors when a page links to a board; manual.py for `te signal add`.
Every request goes through engine.guard; nothing here touches the network itself or calls a model.
"""
