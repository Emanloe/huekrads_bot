# Text migration audit

This is a read-only audit of production Python sources. It excludes SQL, callback
payloads, regular expressions, state identifiers, configuration values, logging,
and comments. Counts are approximate message/template groups, not string-literal
counts. `handlers/hyperborean_event.py` is the only domain migrated in this step.

| File | Symbols / category | Approx. groups | Placeholders | HTML | RNG | Recommended YAML |
| --- | --- | ---: | --- | --- | --- | --- |
| `bot.py` | Application wiring; no independently sent user text | 0 | — | — | — | — |
| `database.py` | `format_user_title` fallback display name | 1 | no | no | no | `common.yaml` |
| `config.py` | Configuration only | 0 | — | — | — | — |
| `handlers/commands.py` | Admin errors, top list, command confirmations | ~12 | yes | yes | no | `commands.yaml` |
| `handlers/game.py` | Pidor-of-the-day progress and result messages | ~4 | yes | no | no | `commands.yaml` |
| `handlers/past_pizda.py` | Event announcement | ~1 | no | no | no | `events.yaml` |
| `handlers/triggers.py` | Trigger replies and birthday messages | ~7 | yes | no | no | `events.yaml` |
| `handlers/utils.py` | Media file-id reply | ~1 | yes | yes | no | `common.yaml` |
| `handlers/weather.py` | Inline weather descriptions, errors, button label | ~20 | yes | yes | no | `events.yaml` |
| `handlers/duel.py` | Duel admission, actions, results, callbacks, button labels | ~100+ | yes | yes | yes | `duel.yaml` |
| `handlers/duel_text.py` | Duel hit/block/miss phrase catalogs and composers | ~30+ | yes | yes | yes | `duel.yaml` |
| `handlers/duel_formatting.py` | Duel status and player presentation templates | ~10 | yes | yes | no | `duel.yaml` |
| `handlers/duel_messaging.py` | Message delivery helpers; presentation passed in | ~0 | — | — | no | — |
| `handlers/duel_input.py` | Duel input validation answers | ~10 | yes | no | no | `duel.yaml` |
| `handlers/duel_state.py` | State transitions; no independent user prose found | 0 | — | — | no | — |
| `handlers/boss_state.py` | Boss state transitions; no independent user prose found | 0 | — | — | yes | — |
| `handlers/boss_registration.py` | Registration validation and join messages | ~12 | yes | yes | no | `boss.yaml` |
| `handlers/boss_presentation.py` | Boss phase, result, and leaderboard presentation | ~35 | yes | yes | no | `boss.yaml` |
| `handlers/hyperborean_event.py` | Spawn, buttons, callback answers, Arthur/Hyperborean outcomes | ~21 | yes | yes | yes | `hyperborean.yaml` (migrated) |
| `handlers/__init__.py` | Package marker | 0 | — | — | — | — |

## Migration order notes

`hyperborean_event.py` was selected first because it is isolated and its random
selection remains in Python. A future migration of duel phrase catalogs must retain
the exact number and order of `random.choice` / `random.random` calls. YAML should
store only the phrase data; existing public composer functions should remain as the
compatibility API.

Existing JSON content files are outside this Python-source audit and were not
changed in this step.
