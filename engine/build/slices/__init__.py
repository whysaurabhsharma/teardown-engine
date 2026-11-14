"""Proof-slice builders (docs/PLAN.md Stage 5.2, Steps 60-63).

One module per builder: icp_accounts (G1, G3, G4), funnel_compare (G2, G5),
role_funnel (the fallback for any gap) and dns_inventory (provisional G6,
protocol/HAND_NOTES.md). common.py holds the evidence rule every slice is
checked against before storage; store.py the slices tables; run.py the gap ->
slice mapping, the fallback chain and `te slice`.

No builder calls a model. role_funnel and dns_inventory make no request;
icp_accounts and funnel_compare reach the network only through engine/guard/.
"""
