# Event calendar (kingshot.net)

The bot shows a kingdom's in-game event calendar with `/calendar`, posts a daily summary to
servers that asked for it, and lets the chat model answer calendar questions. The data comes
from the public kingshot.net API, the same source as <https://kingshot.net/calendar>.

## Source API

`GET https://kingshot.net/api/events?kingdom=<n>&week=<1-4>`
(specification: <https://kingshot.net/api-docs/openapi.json>, operation `getEvents`)

- No API key. Limited per IP address: 120 requests per window, reported in the
  `X-RateLimit-*` headers; `429` carries `Retry-After`. Responses are cached upstream for 60 s.
- `kingdom` defaults to 1. `400` means an invalid number, `404` a kingdom the site does not track.
- `week` is a week of the four-week cycle that started on 2025-12-08. It defaults to the
  current week; values above 4 are treated as 4. **There is no way to ask for a calendar date**:
  the site's own pages use `?week=YYYY-MM-DD`, the API does not.
- The response is not wrapped in the `status`/`data` envelope the other kingshot.net endpoints use.

Fields the bot reads:

| Field | Meaning |
| --- | --- |
| `timestamp` | Server time of the response (UTC). |
| `kvk`, `transfer` | The running or next KvK / transfer: `phase`, `phaseName`, `eventNumber`, `icon`, `timeLeft.total` (milliseconds). KvK phases: `preparation`, `battle`, `countdown`; transfer phases: `pre-transfer`, `invitational`, `open`, `countdown`. |
| `calendar.currentWeek` | Current week of the cycle (1–4). |
| `calendar.events[]` | `title`, `event` (stable id), `type`, `startUtc`, `endUtc`, `isMainEvent`, `confidence`, `phase`, `precision`. |

Normalization (`services/kingshot_calendar_service.py`):

- `endUtc` is inclusive (`…T23:59:59.999Z`). It is rounded up to the boundary, so `end` is
  exclusive and an event that "ends Friday" has `end` = Saturday 00:00 UTC.
- `type` is one of `WAR`, `ALLIANCE`, `GROWTH`, `DEALS`, `SPECIAL`, `PACK`. The specification
  omits `PACK`, the API returns it. Unknown types are shown as `SPECIAL`.
- `confidence: "probable"` marks a date that follows the usual rotation but is not confirmed;
  the bot shows it as *(est.)*, like "Estimated" on the site.
- `phase: "battle"` is the fighting window of a longer event (KvK, Tri-Alliance Clash); it is
  shown as "<title> battle". `precision: "day"` means the hour is unknown.
- `kvk`/`transfer` carry only a remaining time, so the moment the phase changes is computed as
  `timestamp + timeLeft.total`.
- Long events appear in every week they touch; weeks are merged and duplicates dropped by
  (`event`, `phase`, start, end).

## Loading and caching

`KingshotCalendarService.get_calendar(kingdom)` loads the current week and the one after it
(two requests) and merges them into one `KingdomCalendar`.

- Cached per kingdom for 10 minutes; concurrent requests for a kingdom share one load; at most
  three requests run at once; at most 256 kingdoms are kept.
- If next week fails to load, this week is still returned (`includes_next_week=False`) and the
  Next week tab says so.
- If the site is unavailable, a cached calendar up to 6 hours old is served instead of an
  error. Event times are absolute, so an older copy stays correct unless the site changed a date.
- Errors: `KingdomNotTrackedError` (400/404 or a number outside 1–99999) and
  `CalendarUnavailableError` (timeout, network error, other status, unreadable body).

Known limitation: in week 4 of the cycle the "next week" request asks for week 1. If the site
answers with the week 1 that already passed, the Next week tab is empty ("no dates for this
week yet") until Monday. The views select events by time, so stale weeks never show as current.

## `/calendar`

`/calendar [kingdom]` posts a public panel. Without `kingdom` it uses the server's kingdom
from `/configure`; without either it explains how to give one. Works in direct
messages with an explicit kingdom. Cooldown: 4 uses per 30 s per user.

Tabs (`handlers/calendar_handler.py`). Each is an embed with the KvK and transfer lines
(Discord timestamps, so they count down live in each reader's time zone) and picture cards:

- **Today** — a *Running now* card (two-column grid: artwork, time left, when it ends, a
  progress bar in the category color, and **NEW** / **LAST DAY** / **EST.** marks), and a
  second card with *Up next* (the next three days and anything that starts later today) and
  *Packs*.
- **This week** / **Next week** — a timeline, Monday to Sunday in game time (UTC): one row
  per event, one bar per run, today's column and the current moment marked. Hatched bars are
  estimated dates, dim bars are over, hours are written only when an event does not run reset
  to reset. Packs get a timeline of their own.

## Pictures

`services/calendar_card_renderer.py` draws the cards with Pillow, styled after the calendar on
kingshot.net (its dark theme colors and its heading font, Space Grotesk).

- **One card per embed.** Discord fits an embed image into a small box (about 400 × 300
  points, not confirmed against a current client). One tall picture with everything on it is
  scaled down until the text is unreadable, so each card is its own image in its own embed,
  wider than tall where the content allows. The footer sits under the last card.
- Cards are laid out 440 points wide and drawn at 2.5× (1100 px), so they stay sharp on
  high-density screens and when opened full size. A card takes roughly 80 ms to draw and is
  100–160 KB.
- Lists are capped (14 running, 6 upcoming, 6 packs, 20 timeline rows) with an "and N more" note, so a card cannot grow without bound.
- Times on the pictures are a snapshot in game time: the *Running now* header carries the
  moment it was drawn. The KvK and transfer lines in the embed text stay live.
- **Artwork** is bundled in `images/events/<event id>.png` (50 events, copied from
  `https://kingshot.net/images/events/<event id>.png`; the API's `event` id is the file
  name). For an event that is not bundled, the bot downloads the picture from that address
  once and keeps it in memory (misses are retried after 6 hours); with no picture at all the
  event gets a lettered tile in its category color. To bundle a new one, save the PNG under
  the event id.
- **Font:** `assets/fonts/SpaceGrotesk.ttf` (variable weight, SIL Open Font License, see
  `assets/fonts/OFL.txt`). It covers Latin only; event titles come from the API in English.
- Drawing runs in a worker thread behind a lock (FreeType fonts are not thread-safe) and
  results are cached per calendar, view, category filter and minute, so a daily post for many
  servers of one kingdom draws once.
- **Fallback to text:** if a card cannot be drawn, or the bot lacks **Attach Files** in the
  calendar channel, the same information is posted as embed fields (`build_today_embed` /
  `build_week_embed`). `/configure` warns about the missing permission.

New dependency: `pillow` (wheels exist for the Raspberry Pi's aarch64).

A category menu filters every tab. The panel belongs to whoever opened it; when someone else
presses a control they get their own private copy instead of a refusal. Controls expire after
14 minutes.

The posts carry no link or credit to the source: no button, no footer line, no title link.
Only error messages name kingshot.net, so a manager can tell where a problem comes from.

## Daily post

A server gets the Today view every day at **08:00 UTC** once a manager picked both an
**event calendar channel** and set the server's **kingdom** in `/configure`. Picking a channel
before a kingdom opens a form for the kingdom and saves both. A manager who runs `/calendar`
in a server without a calendar channel is offered "Post the daily calendar here" once.

- Settings live in `guild_configurations`: `kingdom_id` (the kingdom the server plays in, one
  per server; a general setting shown as **Kingdom** in `/configure`, so other features can
  default to it later), `calendar_channel_id` and `calendar_last_posted_on`
  (migration `20261112120000`).
- The loop wakes every ten minutes on the clock (so exactly at 08:00) and posts for every
  connected server whose `calendar_last_posted_on` is not today. This is what makes it
  restart-safe: a bot that was down at 08:00 posts when it is back the same UTC day, and a
  restart never posts a day twice. The first pass also runs right after start-up.
- Changing the channel or kingdom in `/configure` posts today's calendar immediately as a
  visible check and counts as that day's post; the panel reports the result or the problem
  (missing permissions, deleted channel, unknown kingdom).
- A failed post is retried on the next ten-minute mark. The reason is logged once per change,
  not every ten minutes.
- Posts are sent without mentions. Old posts are left in the channel.
- The buttons under a post (**This week**, **Next week**) are persistent `DynamicItem`s with
  the kingdom in the `custom_id` (`calendar:<tab>:<kingdom>`), so buttons of old posts keep
  opening the right kingdom after restarts. They open a private panel for whoever presses them.
- `/status` shows the loop as **Calendar Posts**.

The post time is the constant `DAILY_POST_TIME` in `handlers/calendar_handler.py`.

## Chat tool

`get_event_calendar` (`services/chat_tools.py`) lets a direct mention answer questions such as
"when is the next KvK?". It returns the KvK and transfer status, events running now and events
starting within 14 days (packs left out), for the server's kingdom or a kingdom number
given in the question.

## Verification

- `tests/test_kingshot_calendar_service.py`, `tests/test_calendar_card_renderer.py`,
  `tests/test_calendar_handler.py`, and the calendar cases in
  `tests/test_guild_config_handler.py` and `tests/test_chat_tools.py`.
- The cards were rendered from live data and checked by eye at full size and scaled to
  400 × 300. They have not been checked inside a real Discord client.
- Checked against the live API for kingdom 795 on 2026-11-11: the Today view listed the same
  running events as the site's "Running now" block.
