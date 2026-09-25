# Backlog

## Repository

- **`miniapp` is a bare gitlink without `.gitmodules`.** `git submodule status`
  fails with "no submodule mapping found", and a fresh clone gets an empty
  `miniapp/` directory. Not needed for the public copy; fix only if the
  submodule has to be cloned from this repo.

## Search (`/search`)

- The regex fallback never extracts a merchant, so a bare store name
  ("Kaufland") matches everything when the LLM is unavailable (the result
  carries the "simplified rules" warning).
- Merchant matching is `ILIKE '%…%'`: diacritics must match exactly
  ("Żabka" does not find "ZABKA").

## Noticed during i18n

- `stats_period_keyboard()` (7/30/365-day buttons) is never used; its
  `stats:<days>` handler is kept as a legacy callback.
- Rolling-period stats use `date >= today - N days`, i.e. N+1 calendar days.
- Wrapped image: the title touches the top edge and the bottom ~20% of the
  canvas is empty (same in every language); the transaction count is not
  locale-formatted ("1234 transactions").
