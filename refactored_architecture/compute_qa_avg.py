import math
import numpy as np

# ------------ consistency ---------
qa_avg_ranked = [0, 0, 0, 0, 0, 0, 0, 1, 2]
qa_avg_reverse = [0, 0, 0, 2, 1, 2, 0, 2, 2]
qa_avg_random = [0, 0, 0, 0, 2, 2, 0, 0, 1]


print(f'qa_avg_ranked_sum = {np.sum(qa_avg_ranked)} , qa_avg_ranked_sum_avg = {np.average(qa_avg_ranked)}')
print(f'qa_avg_random_sum = {np.sum(qa_avg_random)} , qa_avg_random_sum_avg = {np.average(qa_avg_random)}')
print(f'qa_avg_reverse_sum = {np.sum(qa_avg_reverse)} , qa_avg_reverse_sum_avg = {np.average(qa_avg_reverse)}')

print(f'qa_ranked_rev_improve = { (np.sum(qa_avg_reverse) -  np.sum(qa_avg_ranked))}')
print(f'qa_ranked_rand_improve = { (np.sum(qa_avg_random) -  np.sum(qa_avg_ranked))}')

# ------------ latency ---------
print('\n\n\n')

qa_avg_ranked = [0.114, 0.417, 0.659, 0.825, 1.059, 1.387, 2.282, 2.799, 4.616]
qa_avg_reverse = [2.042, 2.539, 2.854, 2.357, 2.300, 2.238, 2.317, 2.405, 2.178]
qa_avg_random = [0.157, 0.484, 1.362, 1.892, 2.051, 2.127, 6.061, 2.659, 2.461]


print(f'qa_avg_ranked_sum = {np.sum(qa_avg_ranked)} , qa_avg_ranked_sum_avg = {np.average(qa_avg_ranked)}')
print(f'qa_avg_random_sum = {np.sum(qa_avg_random)} , qa_avg_random_sum_avg = {np.average(qa_avg_random)}')
print(f'qa_avg_reverse_sum = {np.sum(qa_avg_reverse)} , qa_avg_reverse_sum_avg = {np.average(qa_avg_reverse)}')

print(f'qa_ranked_rev_improve = { 100 * (np.sum(qa_avg_ranked) -  np.sum(qa_avg_reverse)) / np.sum(qa_avg_reverse)}')
print(f'qa_ranked_rand_improve = {  100 * (np.sum(qa_avg_ranked) -  np.sum(qa_avg_random)) / np.sum(qa_avg_random)}')