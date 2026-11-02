# Routing evaluation: simplified-current-combined

Measurement status: **incomplete**
Decisions received: 11/12
Cases attempted: 12/12
Cases not run: 0
Some selected cases did not return a decision; the displayed scores do not measure model quality.
Scope: dry-run routing and arguments only; geocoding, forecast and final weather answers are not measured.
Scoring applies the same city/date defaults to expected and predicted arguments; raw and effective model arguments are retained in JSON.

Routing: 10/12 (83.3%)
Arguments: 7/9 (77.8%)

| Case | Expected | Predicted | Route | Arguments | Error |
| --- | --- | --- | --- | --- | --- |
| weather_1 | get_weather | get_weather | ✓ | ✓ |  |
| weather_2 | get_weather | get_weather | ✓ | ✓ |  |
| weather_3 | get_weather | get_weather | ✓ | ✓ |  |
| schedule_1 | get_schedule | get_schedule | ✓ | ✓ |  |
| schedule_2 | get_schedule | get_schedule | ✓ | ✓ |  |
| schedule_3 | get_schedule | error | ✗ | ✗ | LLMServiceError |
| reminder_1 | add_reminder | add_reminder | ✓ | ✓ |  |
| reminder_2 | add_reminder | add_reminder | ✓ | ✓ |  |
| reminder_3 | add_reminder | respond | ✗ | ✗ |  |
| respond_1 | respond | respond | ✓ | — |  |
| respond_2 | respond | respond | ✓ | — |  |
| clarify_1 | clarify | clarify | ✓ | — |  |

Combined from three sequential parts: **13 model requests for 12 distinct cases**. Full original reports are embedded in `source_runs` in JSON; successful cases were not repeated.

The first HTTP 503 (weather_3) was retried once successfully. The second HTTP 503 (schedule_3) was not retried. Availability handling and diagnostic fields were updated between parts; scoring did not change.

The model returned a textual confirmation proposal for reminder_3 instead of calling add_reminder. No pending action or confirmation button was created. **The argument threshold of 8/9 is not met in this measurement.**
