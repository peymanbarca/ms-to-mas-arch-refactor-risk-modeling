import networkx as nx
import numpy as np


# -----------------------

G = nx.Graph([
    ("api_gateway", "compose_post"), 
    ("home_timeline", "social_graph"),
    ("write_ht", "home_timeline"),
    ("api_gateway", "user_service"),
    ("api_gateway", "user_timeline"),
    ("api_gateway", "home_timeline"),
    ("api_gateway", "social_graph"),
    ("compose_post", "id"),
    ("compose_post", "media"),
    ("compose_post", "text"),
    ("compose_post", "post_storage"),
    ("compose_post", "write_ht"),
    ("compose_post", "user_timeline"),
    ("text", "url_short"),
    ("text", "user_mention"),
    ])

# Compute betweenness centrality
centrality = nx.betweenness_centrality(G)
print(centrality)

sum = 0
normalized_centrality = {}
for node, cent in centrality.items(): 
    if cent > 0 and node not in ["api_gateway"]:
        sum += cent
        normalized_centrality[node] = cent

# Normalize the centrality values
for node in normalized_centrality:
    normalized_centrality[node] /= sum

print(normalized_centrality)