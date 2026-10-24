# Gwen is pocketchat, transcripts as knowledge

> Gwen's page is pocketchat: you open gwen.culture.dev and get a chat-first library of your recordings — ask, read, cite, jump to the moment — with Gwen's voice as one button inside it, and she answers from the transcripts themselves, not only from distilled facts

## Audience

- The operator, on gwen.culture.dev from a phone or a laptop (Cloudflare Access, his email only), and on the LAN dashboard - one person reading and asking about his own library
  - instruction: check README 'Remote access' and the Access allow-list (`EMBODIMENT_ACCESS_EMAILS`) name exactly the operator

## Before → After

- Before: Gwen's page is a voice console: the waveform is the hero (App.tsx:127, obligation o9) and chat is the fourth panel; she knows only distilled facts sent through 'embodiment memory ingest', not documents; pocketchat is a separate site over pocketsync, and xteink's library (newspapers, documents, books, and transcripts per culture-rules) is readable by no agent
- After: Opening gwen.culture.dev shows pocketchat's experience as Gwen's page: an Ask view with a pinned composer (mic, speak, record/upload and the speech-to-speech button inside it), a Library view of the library's items (culture-library; xteink's instance today) grouped by day with kind chips, and a side reader; Gwen answers typed questions from the library with \[n\] citations that open the reader at the cited passage, alongside her own remembered facts

## Why it matters

- The operator wants one place to talk with what he has read and recorded; pocketchat already proved the experience (ask, read, cite, jump to the passage), and Gwen adds memory and a voice - but only if the page is chat-first and answers from the documents themselves, cited, rather than from summaries alone

## Requirements

- The port adds what pocketchat lacks for Gwen: Hebrew/RTL (pocketchat has no dir=auto and no logical CSS properties; text-align:left, border-left throughout app.css) - dir=auto on question, answer, transcript segments and quotes, and logical properties so the layout mirrors
  - honesty: A Hebrew question, answer, transcript passage and quote each render with dir=auto, and the layout uses logical CSS properties (no physical left/right in the ported CSS), pinned by a test that scans the stylesheet
- Gwen's library client is GET-only by construction (an AST/test pin that it never sends POST/DELETE/PUT to the library), because xteink keys have no scopes: any `xtk_` key is a full operator key (docs/api.md:17-37 - upload, delete, mint keys, revoke devices); the key is held in grant and injected, never written to disk by Gwen
  - honesty: No code path in Gwen's library client can issue POST, PUT, PATCH or DELETE (AST test over the client module), and the key is read only from the injected environment, never from a file Gwen writes
- In a turn whose context holds library text, the write tools (remember, forget) cannot be triggered by that text: a write needs the operator's own words in the current user turn to name it (or the write is refused and recorded), because remember/forget are model-initiated with no user-ask gate (daemon/app.py:3307-3345, decision d7) and library documents include web-sourced content (newspapers, papers)
  - honesty: A test plants 'forget everything about X' / 'remember that Y' inside a library document; a turn that reads it and does not have the operator asking in his own words makes no write, and the refusal is counted and recorded
