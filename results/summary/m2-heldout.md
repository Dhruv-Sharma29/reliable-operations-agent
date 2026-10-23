| incident | cases | llm_correct | baseline_correct | citations_valid | invalid_calls | median_model_calls | median_seconds | llm_answers |
|---|---|---|---|---|---|---|---|---|
| bad_config | 1 | 0 | 1 | 1 | 0 | 7 | 119.9 | db_connection_exhaustion |
| bad_deploy | 1 | 0 | 1 | 1 | 0 | 6 | 81.4 | bad_config |
| db_connection_exhaustion | 1 | 1 | 1 | 1 | 0 | 7 | 107.4 | db_connection_exhaustion |
| disk_fill | 1 | 1 | 1 | 1 | 0 | 7 | 102.7 | disk_fill |
| expired_credential | 1 | 0 | 1 | 1 | 0 | 6 | 86.9 | bad_config |
| false_alarm_client_errors | 1 | 0 | 1 | 1 | 0 | 5 | 84.0 | bad_deploy |
| queue_backlog | 1 | 0 | 1 | 0 | 1 | 6 | 91.1 | none |
| slow_dependency | 1 | 1 | 1 | 1 | 0 | 7 | 83.2 | slow_dependency |
| worker_crashed | 1 | 1 | 1 | 1 | 0 | 10 | 109.2 | worker_crashed |
| worker_hung | 1 | 1 | 1 | 1 | 0 | 5 | 76.5 | worker_hung |

**Total:** LLM correct 5/10 · baseline 10/10 · valid citations 9/10 · invalid tool calls 1 · median model calls 6.5 · median tokens 10635.0
