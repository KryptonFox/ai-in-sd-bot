# Routing evaluation: weather-description-final

Measurement status: **completed**
Decisions received: 12/12
Cases attempted: 12/12
Cases not run: 0
All selected cases returned a decision.

Routing: 11/12 (91.7%)
Arguments: 6/9 (66.7%)

| Case | Expected | Predicted | Route | Arguments | Error |
| --- | --- | --- | --- | --- | --- |
| weather_1 | get_weather | get_weather | ✓ | ✗ |  |
| weather_2 | get_weather | get_weather | ✓ | ✗ |  |
| weather_3 | get_weather | get_weather | ✓ | ✓ |  |
| schedule_1 | get_schedule | get_schedule | ✓ | ✓ |  |
| schedule_2 | get_schedule | get_schedule | ✓ | ✓ |  |
| schedule_3 | get_schedule | get_schedule | ✓ | ✓ |  |
| reminder_1 | add_reminder | clarify | ✗ | ✗ |  |
| reminder_2 | add_reminder | add_reminder | ✓ | ✓ |  |
| reminder_3 | add_reminder | add_reminder | ✓ | ✓ |  |
| respond_1 | respond | respond | ✓ | — |  |
| respond_2 | respond | respond | ✓ | — |  |
| clarify_1 | clarify | clarify | ✓ | — |  |
