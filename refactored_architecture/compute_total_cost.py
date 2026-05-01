import math

c_obs_per_mb = 2 * math.pow(10, -5)

lat_p95_baseline = 2.665

c_obs_per_req_baseline = 37.5 * c_obs_per_mb

# inputs
lat_p95 = lat_p95_baseline + 2.799
n_llm_token = 7.89 * math.pow(10, 6)
c_obs_per_req = (37.5 + 6.05 + 0.24 + 0.21) * c_obs_per_mb


c_infra = 0  # math.pow(10, -4)
c_llm_token = math.pow(10, -7)

delta_q_lat_p95 = lat_p95 - lat_p95_baseline
delta_c = (c_obs_per_req - c_obs_per_req_baseline + c_llm_token * n_llm_token)

print(f'delta_q_lat_p95 = {delta_q_lat_p95} , delta_c = {delta_c},  '
      f' \nInfra term: {100 * delta_q_lat_p95 * c_infra / delta_c} , '
      f' \nLLM term: {c_llm_token * n_llm_token * 100 / delta_c}'
      f' \nObs term: {(c_obs_per_req - c_obs_per_req_baseline) * 100 / delta_c} ')
