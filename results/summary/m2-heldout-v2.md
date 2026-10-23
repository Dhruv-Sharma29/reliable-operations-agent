| incident | cases | llm_correct | baseline_correct | citations_valid | invalid_calls | median_model_calls | median_seconds | llm_answers |
|---|---|---|---|---|---|---|---|---|
| bad_config | 1 | 1 | 1 | 1 | 0 | 7 | 115.5 | bad_config |
| bad_deploy | 1 | 0 | 1 | 1 | 0 | 6 | 99.4 | worker_crashed |
| db_connection_exhaustion | 1 | 1 | 1 | 1 | 0 | 10 | 150.2 | db_connection_exhaustion |
| disk_fill | 1 | 0 | 1 | 0 | 0 | 9 | 223.0 | none |
| expired_credential | 1 | 1 | 1 | 1 | 0 | 10 | 161.7 | expired_credential |
| false_alarm_client_errors | 1 | 0 | 1 | 1 | 0 | 7 | 142.1 | unknown |
| queue_backlog | 1 | 0 | 1 | 0 | 0 | 9 | 209.0 | none |
| slow_dependency | 1 | 1 | 1 | 1 | 0 | 5 | 78.8 | slow_dependency |
| worker_crashed | 1 | 0 | 1 | 1 | 0 | 10 | 127.9 | worker_hung |
| worker_hung | 1 | 1 | 1 | 1 | 0 | 10 | 127.7 | worker_hung |

**Total:** LLM correct 5/10 · baseline 10/10 · valid citations 8/10 · invalid tool calls 0 · median model calls 9.0 · median tokens 16883.5
