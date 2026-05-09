import networkx as nx
import numpy as np

# Example directed edges from the diagram (use real trace-derived edges if available)
edges = [
    ("api_gateway", "product_catalog", 100),
    ("product_catalog", "pricing", 100),
    ("api_gateway", "shopping_cart", 40),
    ("api_gateway", "order", 40),
        ("order", "shopping_cart", 40),
    ("order", "pricing", 40),
    ("order", "inventory", 40),
    ("order", "payment", 10),
    ("order", "shipment", 10),
    ("order", "notification", 10),
    ("inventory", "procurement", 10)
]

G = nx.DiGraph()
G.add_weighted_edges_from(edges)  # if you have weights

# --- Option A: unweighted betweenness (treat every edge equally) ---
bc_unweighted = nx.betweenness_centrality(G.to_undirected(), normalized=True)
print(bc_unweighted)

# --- Option B: weighted betweenness using edge weights as 'cost' ---
# Note: betweenness in networkx expects 'weight' to be interpreted as path-cost.
# If weight represents frequency (higher = more traffic), convert to cost = 1/freq to use weight as path cost.
G_cost = nx.DiGraph()
for u,v,w in edges:
    # convert frequency to cost
    cost = 1/w
    G_cost.add_edge(u, v, weight=cost)

bc_weighted = nx.betweenness_centrality(G_cost, weight='weight', normalized=True)
print(bc_weighted)

# For large graphs, approximate (k sampled sources)
# bc_approx = nx.betweenness_centrality(G_cost, k=50, weight='weight', normalized=True)

# Convert dict to normalized 0..1 vector (if not already normalized)
def normalize_map(m):
    vals = np.array(list(m.values()), dtype=float)
    mn, mx = vals.min(), vals.max()
    if mx == mn:
        return {k: 0.0 for k in m}
    return {k: (v - mn)/(mx - mn) for k,v in m.items()}

bc_norm = normalize_map(bc_weighted)  # or bc_unweighted
print("Betweenness weighted (normalized):", bc_norm)

# bc_norm = normalize_map(bc_unweighted)  # or bc_unweighted
# print("Betweenness unweighted (normalized):", bc_norm)

# RS(s) = wcomp × CNormcog (s)
#                 1 + CNormcyc (s)
# −wdep × Fan_OutNorm(s) + BCnorm(s)