import math

w1 = w2 = w3 = w4 = 1/4


c_cog = [8, 9, 3, 10, 9, 4, 6, 5, 2]
c_cyc = [6, 4, 3, 8, 6, 4, 4, 4, 2]
fan_out = [1, 0, 0, 7, 1, 1, 1, 1, 1]
bc = [0.2, 0, 0, 1, 0.2, 0, 0, 0, 0]
services = ['Product_search', 'Pricing', 'Shopping_Cart', 'Order', 'Inventory', 'Payment', 'Procurement',
            'Shipment', 'Notification']

c_cog_norm = [(c - min(c_cog)) / (max(c_cog) - min(c_cog)) for c in c_cog]
c_cyc_norm = [(c - min(c_cyc)) / (max(c_cyc) - min(c_cyc)) for c in c_cyc]
fan_out_norm = [(c - min(fan_out)) / (max(fan_out) - min(fan_out)) for c in fan_out]
bc_norm = [(c - min(bc)) / (max(bc) - min(bc)) for c in bc]

print(c_cyc_norm, c_cog_norm, fan_out_norm, bc_norm)

rs = []
for i in range(len(c_cog)):
    rs.append( w1 * (c_cog_norm[i])  + w2 * ( c_cyc_norm[i]) + w3 * (fan_out_norm[i]) + w4 * bc_norm[i])

print('Final Ranking Scores: ')
print(rs)

rs_sorted = sorted(rs)
for rs_ in rs_sorted:
    i = rs.index(rs_)
    print(services[i], rs_)

