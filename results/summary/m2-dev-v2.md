| incident | cases | llm_correct | baseline_correct | citations_valid | invalid_calls | median_model_calls | median_seconds | llm_answers |
|---|---|---|---|---|---|---|---|---|
| bad_config | 2 | 1 | 2 | 2 | 0 | 6.5 | 126.65 | bad_config,bad_deploy |
| bad_deploy | 2 | 2 | 2 | 2 | 0 | 6.0 | 99.3 | bad_deploy |
| db_connection_exhaustion | 2 | 1 | 2 | 2 | 0 | 9.0 | 144.8 | db_connection_exhaustion,worker_hung |
| disk_fill | 2 | 0 | 2 | 2 | 0 | 7.0 | 106.35 | false_alarm_client_errors |
| expired_credential | 2 | 2 | 2 | 2 | 0 | 7.0 | 128.15 | expired_credential |
| false_alarm_client_errors | 2 | 2 | 2 | 2 | 0 | 7.0 | 123.1 | false_alarm_client_errors |
| queue_backlog | 2 | 0 | 2 | 2 | 0 | 9.0 | 137.1 | worker_crashed |
| slow_dependency | 2 | 2 | 2 | 2 | 0 | 6.0 | 93.6 | slow_dependency |
| worker_crashed | 2 | 0 | 2 | 2 | 0 | 7.5 | 114.69999999999999 | worker_hung |
| worker_hung | 2 | 2 | 2 | 2 | 0 | 9.5 | 149.7 | worker_hung |

**Total:** LLM correct 12/20 · baseline 20/20 · valid citations 20/20 · invalid tool calls 0 · median model calls 7.0 · median tokens 13084.5
