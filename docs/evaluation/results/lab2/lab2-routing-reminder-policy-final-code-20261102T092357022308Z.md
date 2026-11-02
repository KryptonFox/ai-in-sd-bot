# Routing evaluation: reminder-policy-final-code

Measurement status: **completed**
Decisions received: 12/12
Cases attempted: 12/12
Cases not run: 0
All selected cases returned a decision.
Scope: dry-run routing and arguments only; geocoding, forecast and final weather answers are not measured.
Scoring applies the same city/date defaults to expected and predicted arguments; raw and effective model arguments are retained in JSON.

Routing: 11/12 (91.7%)
Arguments: 9/9 (100.0%)

| Case | Expected | Predicted | Route | Arguments | Error |
| --- | --- | --- | --- | --- | --- |
| weather_1 | get_weather | get_weather | ✓ | ✓ |  |
| weather_2 | get_weather | get_weather | ✓ | ✓ |  |
| weather_3 | get_weather | get_weather | ✓ | ✓ |  |
| schedule_1 | get_schedule | get_schedule | ✓ | ✓ |  |
| schedule_2 | get_schedule | get_schedule | ✓ | ✓ |  |
| schedule_3 | get_schedule | get_schedule | ✓ | ✓ |  |
| reminder_1 | add_reminder | add_reminder | ✓ | ✓ |  |
| reminder_2 | add_reminder | add_reminder | ✓ | ✓ |  |
| reminder_3 | add_reminder | add_reminder | ✓ | ✓ |  |
| respond_1 | respond | respond | ✓ | — |  |
| respond_2 | respond | respond | ✓ | — |  |
| clarify_1 | clarify | respond | ✗ | — |  |

**Both Lab 2 thresholds are met: routing >=10/12 and arguments >=8/9.**

This is one complete run on the final source code: all 12 cases returned decisions, without API errors or retries. All three reminder cases used add_reminder with correct arguments.

clarify_1 is counted as a route error: the model asked clarifying questions in plain text instead of using finish_response(action=clarify). This result is retained without rescoring.

Changed the reminder preparation instruction: exact relative intervals supply the time; known text, time and timezone require add_reminder. The application persists the proposal and shows confirmation controls before the actual mutation. Dataset, model settings and scoring are unchanged.

The full test suite exposed insufficient output space with the enlarged prompt in the fallback context window. ContextBuilder now reduces the output reserve when the base input fits; it requires at least 64 output tokens. The complete run above was performed after this fix.

Earlier validation is embedded in `earlier_policy_validation` in JSON: two disjoint parts (12 requests), plus one successful month-boundary/timezone probe. Those records retain their original source hashes and inputs. **Total model requests in this task: 25 (12 earlier baseline + 1 probe + 12 final baseline).**

Automated checks: **474 tests passed**, including PostgreSQL. The reminder integration test verifies pending preparation, no mutation before confirmation, and one record after repeated confirmation. ty, Ruff check and formatting passed. Routing evaluation performs no actual MCP mutation or Telegram delivery.
