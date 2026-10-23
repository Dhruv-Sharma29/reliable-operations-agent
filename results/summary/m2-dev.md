| incident | cases | llm_correct | baseline_correct | citations_valid | invalid_calls | median_model_calls | median_seconds | llm_answers |
|---|---|---|---|---|---|---|---|---|
| bad_config | 2 | 0 | 2 | 2 | 0 | 7.0 | 100.1 | db_connection_exhaustion,slow_dependency |
| bad_deploy | 2 | 0 | 2 | 2 | 0 | 6.5 | 59.5 | bad_config,unknown |
| db_connection_exhaustion | 2 | 1 | 2 | 2 | 0 | 6.5 | 109.35000000000001 | db_connection_exhaustion,worker_crashed |
| disk_fill | 2 | 2 | 2 | 2 | 0 | 7.0 | 103.19999999999999 | disk_fill |
| expired_credential | 2 | 0 | 2 | 2 | 0 | 6.5 | 97.55 | bad_config,bad_deploy |
| false_alarm_client_errors | 2 | 0 | 2 | 2 | 0 | 7.0 | 96.6 | bad_config,bad_deploy |
| queue_backlog | 2 | 0 | 2 | 2 | 0 | 7.0 | 99.1 | unknown,worker_hung |
| slow_dependency | 2 | 1 | 2 | 2 | 0 | 5.5 | 70.15 | queue_backlog,slow_dependency |
| worker_crashed | 2 | 1 | 2 | 2 | 0 | 7.5 | 105.35 | worker_crashed,worker_hung |
| worker_hung | 2 | 2 | 2 | 2 | 0 | 6.5 | 82.6 | worker_hung |

**Total:** LLM correct 7/20 · baseline 20/20 · valid citations 20/20 · invalid tool calls 0 · median model calls 7.0 · median tokens 11379.5
