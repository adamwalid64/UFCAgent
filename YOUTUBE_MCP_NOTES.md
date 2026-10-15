# YouTube coverage and MCP notes

## Actual inspection, 2026-10-11

No YouTube transcript MCP tools are exposed in this Codex session. There is no
project `.mcp.json`, `mcp.json`, `.vscode/mcp.json` or project Codex configuration.
The user-level Codex configuration contains `node_repl`, not a YouTube transcript
server. The user subsequently confirmed that no server is configured.

**Actual YouTube MCP tool inventory: none available to inspect.** No server tool
names, argument schemas, responses or live transcript calls have been verified.
The installed Python MCP SDK is a client dependency, not a transcript server.
No third-party server was silently selected or installed. The primary layer will
record `mcp: not configured or disabled` and continue to the configured fallbacks.

Before using a server, configure its stdio command/arguments or Streamable HTTP
endpoint, then run:

```powershell
.\.venv\Scripts\python.exe -m fight_coverage inspect-mcp
```

This performs MCP initialization and actual, paginated `tools/list`. It writes the
tool names, descriptions, input and output schemas into this file. It does not
download a transcript. Configure `mcp.tool`, `mcp.video_argument`, optional fixed
`arguments` and `result_path` using that inventory. Every transcript call checks
the live inventory and validates arguments against its actual JSON Schema.
The adapter never invents a tool name. Unknown/failed responses fall through.

Example **configuration shape**, not a verified server contract:

```json
{
  "transport": "stdio",
  "command": "C:/path/to/installed-server.exe",
  "args": [],
  "env": {},
  "tool": null,
  "video_argument": "video_id",
  "video_as_url": false,
  "arguments": {},
  "result_path": []
}
```

For remote MCP use `transport: "streamable_http"`, `url_env: "YOUTUBE_MCP_URL"`
and optionally `token_env: "YOUTUBE_MCP_TOKEN"`. HTTP is allowed only on loopback;
remote endpoints require HTTPS. Stdio `env` maps child variable names to parent
environment variable names. Put credentials in `.env`; never in launch arguments.
Legacy SSE transport is not implemented. Choose a trusted server with authorized
transcript access; connecting MCP does not itself establish permission.

## Registered channels

The source registry is `config/sentiment_sources.yaml`. Retrieval settings remain
in `config/youtube_coverage.json`, which references that YAML through `sources_path`.
Sources are never selected from a hardcoded Python list. All seven approved sources
are active in the registry; collection itself remains disabled until configured.
Each entry includes name, verified channel ID, type, active flag and initial
credibility weight 1.0. Inactive entries are skipped before discovery, including
cached content. Additional sources have a commented, inactive template for approval.

On 2026-10-11, each handle page below returned HTTP 200. Its YouTube page title and
canonical `/channel/UC...` link were checked together. This verified White Piece
MMA's previously unresolved numeric ID and the two newly requested breakdown
channels. This was a one-time identity check; recurring upload discovery still uses
only the official Data API.

| User's channel | Registered identity | Identity reference |
| --- | --- | --- |
| MMA Guru | `UCIhQvpinmS8Eq6PrQ021DKQ` | [THE MMA GURU on YouTube](https://www.youtube.com/channel/UCIhQvpinmS8Eq6PrQ021DKQ) |
| White Piece MMA | `UCOb2TSO5mwGdkwzYNtjlAlA` | [Verified handle page](https://www.youtube.com/@white-piece-mma) |
| HorseBeef MMA | `UCNQClXZDN0QWliPqyHNfLWw` | [HorseBeef MMA on YouTube](https://www.youtube.com/channel/UCNQClXZDN0QWliPqyHNfLWw) |
| Robin Black | `UCTXp35WW-53vg66Y_IM952g` | [Verified handle page](https://www.youtube.com/@RobinBlack) |
| Luke Thomas | `UC2EuJ9xTs0XkDZI9YGx7QZA` | [Verified handle page](https://www.youtube.com/@lukethomas) |
| Lucas Tracy MMA | `UC7LzaJA-R2E52qzd5GW-kpg` | [Verified handle page](https://www.youtube.com/@LucasTracyMMA1) |
| MMA Che | `UC8qJ5LhpoW1jeqx5yLAiC_w` | [MMA Che on YouTube](https://www.youtube.com/channel/UC8qJ5LhpoW1jeqx5yLAiC_w) |

The similarly named `@mmaguru667` is a different channel and is not registered.
The initial inspection had no YouTube API key. A later configured discovery run
populated the official uploads cache for all seven channels; that cache was checked
on 2026-10-11 while improving full-card matching. This verifies discovery separately
from transcript access, which still depends on the available providers.

## Pipeline contract and setup

```python
from fight_coverage import get_fight_coverage
coverage = get_fight_coverage({
    "fighter_a": "Brendan Allen", "fighter_b": "Chris Duncan",
    "event_name": "UFC Fight Night: Allen vs. Duncan",
    "event_aliases": ["Allen vs Duncan"],
    "aliases": {"Brendan Allen": ["Allen"], "Chris Duncan": ["Duncan"]},
}, "2026-10-10")
public_sources_and_caveats = coverage.public()
# coverage.for_analysis() is private model input, not a report or export.
```

1. Install `requirements-youtube.txt` in the existing virtual environment.
2. Enable YouTube Data API v3 in your Google Cloud project and put its API key in
   `.env` as `YOUTUBE_API_KEY`. The OpenAI key cannot be used for discovery.
3. Review permissions below. For channels where the necessary access and storage
   permissions actually exist, set `transcript_access_authorized: true` on that
   YAML entry. This is
   a declaration, not a license or a way to acquire permission.
4. Set `enabled: true`. SQL predictions automatically collect coverage; missing
   providers produce caveats and leave database analysis available. Config is
   loaded once per agent; restart after changing settings.
5. Configure and inspect an MCP server as above when available. Without one, the
   first authorized retrieval attempt is the direct Python transcript library.

```powershell
.\.venv\Scripts\python.exe -m fight_coverage check
.\.venv\Scripts\python.exe -m fight_coverage --verbose preview "Fighter A" "Fighter B" --event-date 2026-11-01 --event-name "UFC event name"
```

Preview emits source links and caveats only. It makes no OpenAI calls. Use the real
fighter names and event date. Pass `--event-name` for the card and repeat
`--event-alias` for verified alternate names, such as a UFC Vegas number.
For a final check the day before the card, add `--refresh-discovery`. It bypasses
the six-hour metadata cache once per request in that preview run, respects the
same API quota, and preserves cached transcripts. This is an on-demand check;
the app does not poll in the background. An empty preview earlier in the week
does not establish that the channels will skip the card.
Full-card videos match the event name, numbered event prefix, or main-event pairing
(including reversed order and `versus`, `v`, or `&` spellings). Their titles do not
need to name an undercard fight. Recent generic full-card predictions, UFC picks,
and weekend previews are also provisional candidates. Identified fight/card matches
take priority; recap, results, reaction, and fight-companion titles are excluded.

For a card video, matching description chapters select that bout's timestamp range.
Without usable chapters, nearby mentions of both fighters are required in captions,
and the analysis receives only relevant passages within the existing character cap.
A generic card preview discussing another event is skipped if it does not identify
the requested matchup. The transcript is still cached once per video and reused
across all fights on the card. No extra model request is needed for discovery or
section selection. Verbose output reports uploads scanned, publication-window
counts, identified matches, provisional card candidates, and latest upload date.
The page-limit caveat means older playlist pages remain unscanned; it does not mean
the latest uploads were missed or that a relevant video exists on a later page.

Card predictions supply the event name automatically. For a single SQL-agent fight,
`--event-name "UFC Fight Night: Buckley vs. Malott"` supplies the same context.

## Ordered retrieval and cache behavior

- Discovery: `channels.list` -> uploads playlist -> bounded `playlistItems.list`
  pages (50 uploads/page). Title and description matching uses a configurable
  pre-event lookback. Each request and retry consumes one unit in a local SQLite
  quota ledger, resetting at Pacific midnight. A remote quota exhaustion stops
  discovery for that day. The local cap does not know about other apps using the
  same Google project. Metadata caches are keyed by API request, shared across
  fights, and refreshed every six hours by default. Pagination truncation is explicit.
- Transcript order: inspected MCP -> `youtube-transcript-api` -> yt-dlp automatic
  captions only -> optional audio plus local Whisper. Library network timeouts and
  transient failures get bounded exponential backoff; permanent denials do not.
  Download workers have hard process timeouts. The optional fixed proxy is never
  rotated to bypass an access denial. SDK/library/server behavior can still fail.
- Successful transcripts are cached by video ID across fights, languages and runs.
  A success short-circuits every provider on reruns. File locks prevent duplicate
  retrieval across processes. A six-hour negative cache limits repeated failures.
  Each success logs its actual layer and cache status, without transcript text or
  secrets. Failures record safe exception classes, not credential-bearing errors.
- Retention is at most 30 days. Expired/deleted transcripts leave a video-ID marker
  and **are not automatically fetched again**. This reconciles no repeated downloads
  with retention; indefinite transcript storage is not promised. Clearing the whole
  cache also removes markers and should not be part of routine runs.
- Expiry cleanup runs on coverage access and `python -m fight_coverage prune-cache`.
  Schedule that command daily if you retain a cache while the predictor is idle.
  `python -m fight_coverage forget-video VIDEO_ID` deletes local transcript/failure
  data and preserves the no-refetch marker; it does not delete anything on YouTube.
  Cache files are private, ignored by Git and unencrypted; restrict local access.

Whisper is deliberately off. It additionally requires actual audio-download
permission recorded as `audio_download_authorized: true`, `whisper_enabled: true`,
an FFmpeg executable on PATH and optional `pip install openai-whisper`. Only an
audio-only format is downloaded, in temporary storage removed after use. Live,
unknown-duration and over-limit audio are rejected. Whisper runs locally on CPU;
its first opt-in run may download model weights. No hosted transcription API is used.

## Evaluation integrity and publication

Date-only cutoffs mean **strictly before midnight in `event_timezone` on that
date**. This project's setting is `America/New_York`, allowing late Friday
Eastern uploads for a Saturday card; daylight-saving changes are handled by the
timezone database. Configurations omitting the setting retain the legacy UTC
behavior. SQL continues to exclude fights on the entire target calendar day.
The standalone interface also accepts an explicit timezone-aware timestamp for
a known event start; its supplied offset is preserved. Future publication
dates, metadata observed after the cutoff and transcripts first fetched after
the cutoff are excluded. A currently visible publication date cannot establish
that today's edited text existed historically. Historical replays therefore may
have no eligible YouTube coverage, and should report that limitation.
Date-only previews must collect their evidence before that local midnight; a
first collection on Saturday will not qualify for a Saturday date-only replay.
Accepted YouTube sources record their exact UTC evidence cutoff in report data.

Only bounded, relevant passages enter a separate analyst extraction stage. When
coverage is available and at least two model requests remain, it makes one batched
request with the configured analysis model, counted in the same UsageMeter and
request budget. This is an additional paid step when enabled. A one-request direct
run skips extraction with a caveat to preserve its prediction budget. Failures do
not cause raw text to bypass extraction into SQL analysis. No paid extraction is
made for empty/disabled coverage. Full-card excerpts without identifiable matchup
discussion are skipped and truncation is explicit.

The extraction schema records source identity, nullable explicit pick and original
technical signals. Pick evidence is checked against the source privately and never
published. Favorable analysis, conditional outcomes and unverifiable selections
keep `pick: null`. Signals with invalid evidence references or copied text are
discarded. Credibility weights come from configuration, not model output. Repeated
videos from one source cannot multiply its vote; conflicting picks and null picks
are excluded from the weighted analyst pick summary. Weights are starting priors,
not measured accuracies. The SQL model and optional reviewer receive only the
structured analyst signals, never raw source passages or crowd/odds data.

Commentary remains untrusted opinion, separate from historical statistics. Reports
and ledgers contain source links, weights, explicit picks/null and paraphrased
signals. Raw transcripts, private pick evidence and provider error bodies are
excluded. Do not enable tracing that republishes model prompts. Coverage and source
weights do not establish improved prediction accuracy.

## YouTube access constraints and primary documentation

Analysis-only use does not automatically authorize scraping or downloading.
YouTube's developer policies restrict undocumented APIs/non-API retrieval and
audio downloads, and impose refresh/deletion limits on stored API data. These
requested third-party adapters must only be enabled where the applicable YouTube
and content permissions cover access, analysis and caching. Creator permission
alone does not override platform restrictions. The app records permission flags
but cannot verify grants. No restriction-bypass cookies, proxy rotation or public
transcript endpoint is provided. Default configuration performs no retrieval.

- [YouTube developer policies](https://developers.google.com/youtube/terms/developer-policies)
- [channels.list and handle resolution](https://developers.google.com/youtube/v3/docs/channels/list)
- [playlistItems.list and request quota](https://developers.google.com/youtube/v3/docs/playlistItems/list)
- [youtube-transcript-api: fetch, proxies and undocumented API caveat](https://github.com/jdepoix/youtube-transcript-api)
- [yt-dlp: subtitle-only and audio formats](https://github.com/yt-dlp/yt-dlp)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
