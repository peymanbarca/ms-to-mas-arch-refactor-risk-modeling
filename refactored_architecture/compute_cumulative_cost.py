import math
import numpy as np

delta_c_ranked = [0.011, 0.152, 0.198, 0.236, 0.259, 0.579, 0.675, 0.789, 1.453]
delta_c_reverse = [0.794, 0.908, 0.890, 1.114, 0.816, 0.831, 0.840, 0.933, 0.805]
delta_c_random = [0.141, 0.461, 0.558, 0.676, 0.688, 0.699, 1.465, 0.725, 0.726]


print(f'delta_c_ranked_sum = {np.sum(delta_c_ranked)} , delta_c_ranked_sum_avg = {np.average(delta_c_ranked)}')
print(f'delta_c_random_sum = {np.sum(delta_c_random)} , delta_c_random_sum_avg = {np.average(delta_c_random)}')
print(f'delta_c_reverse_sum = {np.sum(delta_c_reverse)} , delta_c_reverse_sum_avg = {np.average(delta_c_reverse)}')

print(f'c_ranked_rev_improve = { 100 * (np.sum(delta_c_reverse) -  np.sum(delta_c_ranked)) / np.sum(delta_c_reverse)}')
print(f'c_ranked_rand_improve = { 100 * (np.sum(delta_c_random) -  np.sum(delta_c_ranked)) / np.sum(delta_c_random)}')