import networkx as nx
import numpy as np


# -----------------------

G = nx.Graph([
    ("api_gateway", "product_catalog"), 
    ("api_gateway", "recommendation"),
    ("recommendation", "product_catalog"),
    ("api_gateway", "ad"),
    ("api_gateway", "shipping"),
    ("api_gateway", "currency"),
    ("api_gateway", "shopping_cart"),
    ("api_gateway", "checkout"),
    ("checkout", "shopping_cart"),
    ("checkout", "currency"),
    ("checkout", "payment"),
    ("checkout", "shipment"),
    ("checkout", "notification"),
    ("checkout", "product_catalog"),
    
    ])

# Compute betweenness centrality
centrality = nx.betweenness_centrality(G)
print(centrality)