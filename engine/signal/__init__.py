"""E1 Signal: collect GTM job posts into the signals table.

One module per public ATS board API (greenhouse, lever, ashby, workable), each
exposing `collect(board_slug) -> list[Signal]`. Every request goes through
engine.guard; nothing here touches the network itself or calls a model.
"""
