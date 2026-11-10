# Corpus enrichment: process log and reflections

Capstone "How sparse are genre metadata, and does it hinder music recommenders?" · stage 1 → stage 2 · 2 Nov 2026

This document records what was done to enrich the stage-1 candidate corpus, in the order it happened. It covers why each step was designed the way it was, what went wrong and how it was fixed, and my assessment of how the result can and cannot be used. The short version for the supervisor is [Capstone_corpus_stage2_UA.md](Capstone_corpus_stage2_UA.md).

---

## 0. Starting point

**Inputs.** Three files:
- `capstone_corpus_stage1_tracks.csv`: 602 candidate tracks across five cases, each named in a retrieved source, with `source_id` and `source_url`.
- `capstone_corpus_stage1_interim.xlsx`: line-up sheets, vocabulary, the empty `Fill_in_manual` template and the source list.
- `Capstone_stage1_summary_UA.md`.

**State.** No gold-A, no metadata, no density figures. Every case was below the plan's 200-track target.

**Constraints from the plan** (`D:\Capstone_plan_EN.docx`) that shaped everything below:
- **Seeds must be independent of platform genre tags (§3).** Otherwise E2 measures what the platform already knows.
- **Gold-A needs ≥2 independent sources** (label, line-up, expert/press). Gold must not rest on tags or on texts LLMs were trained on.
- **The unit of analysis is the artist (MBID), each with several tracks.**
- **Density is a set of separate indicators**, not one index: tags and genres, MB relations, Wikidata statements, Wikipedia languages. Deezer fans are the popularity control.
- **Failure taxonomy:** absent / colliding / coarse / untranslated.

**The key discovery.** The stage-1 summary said MusicBrainz, Deezer, iTunes and Last.fm were blocked. That was true of the previous workspace. From this machine, MusicBrainz, ListenBrainz, Deezer, iTunes, Wikidata and Discogs (public endpoints) all respond. So steps 3–4 of the plan could be automated instead of filled in by hand.

---

## 1. Pilot probe: what the data looks like before trusting it

Before building anything I queried the top 4 artists of each case (20 rows) against MusicBrainz, Deezer and ListenBrainz, then Arabic-script names and scene labels. The pilot changed the design more than any later step did.

| Finding | Example | Consequence |
|---|---|---|
| A search score of 100 can point to the wrong artist | "Sadat" → Sadat X (US rapper), score 100. "عصام صاصا" → a different عصام, score 100. Deezer "Pogulyay" → Pulyay | Name search alone is not a match. A match needs **track evidence** |
| Matching errors pile up in sparse cases | Mahraganat by Latin name: 0 of 4 correct | Errors would imitate the H1 effect (low density, low popularity), so this is a validity threat, not just noise |
| Script matters | حمو بيكا and حسن شاكوش are in MB, "Hamo Bika" is not | "Untranslated" already happens when matching artists, before any genre label comes in |
| Temporary failures look like absence | DJ Maphorisa missing on the first run, found on the second | Retries plus a cache, and "absent" only after a successful lookup |
| "Rich" depends on the source | Kabza De Small: 227k Deezer fans, 409 ListenBrainz users, **0** similar artists | Density must be reported per source |
| Scene labels exist as MB entities | Outcaste 53 releases, Nation 79, Terror Negro 5, 100Copies 7, Standard Deviation 8. Bheja Fry, Muscut, Cxema absent | Label catalogues are a tag-independent seed and gold source |
| The primary-artist split is lossy | "Tomasa del Real" / "Tomasa Del Real" | Normalise names before counting artists |

---

## 2. Protocol decisions (fixed before any experiment results)

These were agreed with the author and written into `Capstone_stage1_summary_UA.md`, sections 3–4.

1. **New condition E5b: listening-based kNN from ListenBrainz.** It uses precision@k on gold-A over ListenBrainz similar artists. The primary decision rule (labels and audio) is unchanged; E5b is secondary and qualifies the conclusion. Empty similar-artist lists are themselves a density indicator. precision@k is computed two ways: over artists that have a list, and over all artists with empty = 0.
2. **Artists found through MB tag searches form a separate group** (`seed_method = mb_tag`). They are re-evaluated with the same gold-A criteria after the search, and never pooled with independent seeds in E2 or E6. Reason: an artist found through a tag has that tag by construction.
3. **No paid or keyed APIs.** Last.fm and authenticated Discogs are not used; Discogs is used only through its public endpoints.
4. **Asian Underground has two sub-cases**: historical (1993–2003, institutions Outcaste / Nation / Anokha / Swaraj) and contemporary (Daytimers / No Nazar / Mishti Dance / Hundred Colours). The pair gives a dense/sparse contrast within one tradition. 91 of the 92 dated stage-1 rows are from 2003 or earlier, so the contemporary pool still has to be collected.
5. **Matching rule:**
   - an artist ID needs track evidence: the title matches and the artist name matches, or the Deezer ISRC resolves in MB;
   - "not in MB" and "not matched" are recorded as different values;
   - the data is a dated snapshot;
   - nobody edits MB or Wikidata during the measurement.

---

## 3. Architecture

| File | Role |
|---|---|
| `enrich.py` | Stage-1 tracks → MB / Deezer / iTunes / Wikidata / ListenBrainz metadata. Writes `enrichment/enriched_2026-11-02.xlsx` |
| `review.py` | Gold-A evidence per artist; merges identities. Writes `enrichment/goldA_evidence.csv` |
| `enrichment/goldA_manual_evidence.csv` | 92 web press findings (case, artist, key, URL) added by hand |
| `tracks.py` | Builds the balanced track corpus. Writes `capstone_corpus_stage2.xlsx` |
| `describe.py` | Prints the descriptive statistics |
| `enrichment/cache.sqlite` | Every HTTP response (URL + params → body + timestamp). This is the snapshot, and it makes re-runs resumable and deterministic |
| `enrichment/*.log` | Run logs, including the rejected iterations |

**Engineering choices**:
- **Rate limits per host.** MusicBrainz 1.05 s, Deezer 0.12 s, iTunes 3.1 s, Discogs 2.6 s, Wikidata/Wikipedia 0.2 s.
- **Retries with backoff** on 403/429/5xx and on Deezer's in-body quota error.
- **Failed requests are not cached**, so a re-run retries them. They are listed in `failed_requests.txt` and counted in `Run_meta`.
- **One normalisation function** (NFKC, casefold, brackets and "feat." stripped) and **one similarity function** (difflib ratio; substring counts as 0.9 if the shorter string has ≥4 characters). Thresholds: names ≥0.85, titles ≥0.80.
- **Every judgment call is in code or in a CSV**, never only in my head.

---

## 4. Step 1: metadata enrichment (`enrich.py`)

**Per track:**
1. MB recording search `recording:"title" AND artist:"primary"`. The match is accepted only if the title similarity and an artist-credit similarity both pass.
2. Deezer search `artist:"…" track:"…"`, then a free-text search; then `/track/{id}` for ISRC and preview.
3. If MB failed and Deezer gave an ISRC: MB `/isrc/{isrc}`. This bridges scripts, e.g. a Latin-letter Deezer entry to an Arabic MB entry.
4. iTunes search, only where Deezer had no preview (at this stage).

**Per artist:**
- Status:
  - `confirmed_title` or `confirmed_isrc`;
  - `candidate_unconfirmed`: an MB artist with the same name exists, but there is no track evidence;
  - `not_found`.
- MB lookup with aliases, tags, genres and URL/artist/label/event relations.
- Wikidata (statements, Wikipedia sitelinks, genre statements with the number of label languages).
- Deezer artist (fans). The fallback, used only if no track matched on Deezer: a same-name Deezer artist whose top tracks share ≥2 titles with the artist's MB recordings.
- ListenBrainz popularity, and similar artists under a fixed algorithm string.

**Bugs found by checking results, and fixed:**
- **11 temporary MB failures** in the first full run. Re-run from the cache: 0 failures.
- **Co-credit attribution.** Omar Kamal had been given Hassan Shakosh's Deezer page (318k fans) via a joint track. Fix: artist IDs come only from the credit that matches the *primary* artist; a co-credit match keeps the recording but not the artist ID. The same rule now applies to ISRC: a cross-script name mismatch becomes a review candidate, not an ID.
- **Talvin Singh had no Deezer fans** because none of his corpus tracks matched. This prompted the artist-level fallback above.

**Results** (290 normalised artists):

| Case | Matched (track evidence) | Candidate only | Not found |
|---|---|---|---|
| Amapiano | 33 | 34 | 9 |
| Asian Underground | 46 | 2 | 4 |
| Kyiv | 25 | 17 | 5 |
| Mahraganat | 4 | 14 | 27 |
| Neoperreo | 31 | 28 | 11 |

Tracks matched: 289/602 in MB (285 by title, 4 by ISRC) and 339/602 on Deezer. The `Fill_in_manual` template is filled automatically; the gold and failure-type columns stay manual.

---

## 5. Step 2: gold-A review (`review.py` + web evidence)

**Rule (user instruction):** an artist enters the corpus if ≥2 independent sources connect them to the scene.

### 5.1 What "independent" means here

I operationalised independence as *different institution or publisher*. Several reports of one underlying fact collapse into one **key**:
- **Cxema.** Its line-ups, its NTS show and press about Cxema events → one key, `Cxema`.
- **Neoperreo collective.** Its parties and its NTS radio show → one key.
- **Labels.** A release on label X, whether found in MB, Discogs, Deezer or the label's own compilation → `label:X`. MB and Discogs are not independent of each other, because MB often imports from Discogs.
- **Publishers.** Music In Africa's four yearly lists → one key. Wikipedia in any language, plus its mirrors → `Wikipedia`.
- **Syndication** collapsed: IOL titles (The Post, The Mercury, Sunday Tribune, Pretoria News) → `press:IOL`; Hypebot = Chartmetric (same article); CairoScene / SceneNow / SceneNoise → `press:CairoScene`.

**Not evidence:**
- MB tags and Wikidata genre statements (plan: gold is independent of labels).
- Reddit (weak by the stage-1 rule).
- Tag-seeded sources N15 and U06.
- The artist's own pages and albums (N02, N03, N06, U02–U04, A01, A02).
- Playlists or events not tied to the scene: Serato "Savanna Dry 2", Audiomack "wegz", SceneNoise "Zee3", Motherland, Red Bull Unlocked, Vibras Fest.

Each of the 67 stage-1 sources was mapped to a key by hand (`SOURCE_KEY` in `review.py`), and each line-up event too (`LINEUP_KEY`).

### 5.2 Automated evidence

1. **Stage-1 sources** whose track credits name the artist (word-boundary match on normalised strings).
2. **Line-up sheets** (`Lineups_evidence`, `Cxema_lineups_UA`).
3. **Releases on scene labels.** The scene labels per case were fixed from the stage-1 sources and the vocabulary, *not* from tags. Release lists came from MB (`artist` and `track_artist` browse, so compilation appearances count), Discogs artist releases (via the MB URL link) and Deezer album labels.
4. **Wikipedia.** The artist's own article in the relevant languages, checked for case keywords. Then full-text Wikipedia search for any page naming both the artist and the scene.

Progress (artists with ≥2 keys): **100** after steps 1–3, **138** after the Wikipedia search.

### 5.3 Identity repair

Reviewing the one-key list showed that the stage-1 primary-artist split had broken identities, so they were merged (`ALIASES`):
- **Same act, several spellings or scripts:** El Madfaagya / El Madfaageya / Madf3gya; الدخلاوية / El Dakhlaweya; Figo / DJ Figo; Oka W Ortega / Oka W Ortega W Shehta; T.J. Rehmi / TJ Rehmi; Team Amr Haha / Amr Haha.
- **Alias:** Mahatma T = Talvin Singh (found during the web search).
- **Remix credited to the original artist** where the scene artist is the remixer: Craig David (Rishi Rich remix) → Rishi Rich; Trilok Gurtu (T.J. Rehmi remix) → TJ Rehmi; two Juttla mixes → Juttla.

### 5.4 Web press search

For artists still one key short I ran one web search each, roughly 120 artists, using `"artist" + scene term`. A hit counted only if it was a press, editorial, festival or label page from a publisher not already counted. Streaming and aggregator pages (Shazam, Apple Music, Viberate, Chartmetric profiles, Mixgraph, Beatport) never counted.

For neoperreo I switched to a systematic check. I fetched ten genre-defining articles (Wikipedia, Boiler Room, MIDiA, Latinolife, The FADER, Bandcamp Daily, Remezcla, Hypebot/Chartmetric, INTO) and listed which corpus artists each names, with context, for manual acceptance. That turned up the Hypebot/Chartmetric duplicate, and it confirmed Jamez Manuel, Arca and MC Buzz.

### 5.5 Results

| Case | Artists (after merges) | Gold-A | Pass rate | Excluded: 0 keys / 1 key |
|---|---|---|---|---|
| Amapiano | 76 | 68 | 89% | 6 / 2 |
| Asian Underground | 46 | 30 | 65% | 0 / 16 |
| Kyiv | 47 | 34 | 72% | 0 / 13 |
| Mahraganat | 39 | 19 | 49% | 4 / 16 |
| Neoperreo | 70 | 36 | 51% | 5 / 29 |

Of the 8 tag-seeded artists, 2 passed (Rosa Perreo, and one in Asian Underground). They remain a separate stratum.

**Evidence mix among gold-A artists** (key occurrences by kind) shows how differently the scenes are documented:

| Case | press | Wikipedia | label | comp | scene institution | dj / editorial | event / film |
|---|---|---|---|---|---|---|---|
| Amapiano | 128 | 46 | 0 | 0 | 0 | 0 | 2 |
| Asian Underground | 1 | 22 | 34 | 11 | 15 (Anokha, Swaraj) | 0 | 0 |
| Kyiv | 8 | 2 | 44 | 21 | 21 (Cxema, Strichka) | 0 | 3 |
| Mahraganat | 21 | 17 | 5 | 0 | 0 | 11 | 6 |
| Neoperreo | 34 | 27 | 3 | 0 | 17 (collective) | 21 | 0 |

- Amapiano is documented by **press**.
- Kyiv and Asian Underground by **institutions** (labels, compilations, club nights).
- Neoperreo by **press plus its own collective**.
- Mahraganat by a thin mix.

This is a substantive finding in itself: the kind of documentation differs between scenes, not just the amount.

---

## 6. Step 3: building the track corpus (`tracks.py`)

**User instructions:**
- No 5-track cap: if an artist has more tracks and represents the genre, add them.
- Cases must stay roughly equal in size, for comparability.

### 6.1 Final design

1. **Pool per gold-A artist:**
   - stage-1 tracks (always kept, because a source grounds them);
   - Deezer top tracks that have a preview;
   - iTunes songs (lower priority).
   Versions and remixes of one title collapse to one track.
2. **Representativeness gate.** Only a representative artist gets tracks beyond stage 1. That means at least one of:
   - ≥3 keys;
   - artist-level evidence (press, film, event, scene line-up or collective);
   - being listed in the scene's own Wikipedia article;
   - their own Wikipedia article naming the scene in strict terms ("Asian Underground/Asian Massive", "amapiano", "neoperreo", "mahraganat/electro-shaabi/مهرجانات", "Cxema/Kyiv techno").

   Sixteen gold-A artists who appear only on compilations keep just their stage-1 tracks: 11 in Asian Underground (e.g. 4 Hero, Swami), 2 in Kyiv, 2 in Mahraganat, 1 in Neoperreo.
3. **Scene period for added tracks:** Asian Underground 1993–2003, Kyiv ≥2013, amapiano ≥2012, neoperreo ≥2014, mahraganat ≥2005.
   - **Year source:** the MB first-release date, by recording title first and then by release-group title. Deezer's date is a fallback only, because it dates albums by their digital (re)release.
   - Stage-1 tracks dated only by a playlist or list date get MB or Deezer release years.
4. **iTunes vetting.** iTunes merges same-name artists into one page. So for MB-matched artists a song is accepted only if its title is one of their MB recordings. Otherwise the iTunes artist must share ≥2 titles with the artist's known tracks.
5. **Balancing.** Each case is filled one track per artist per pass, in priority order (stage 1 → Deezer popularity → iTunes). The target is the smallest pool, i.e. the size every case can reach.
6. **iTunes preview check for every selected track.** First the artist's iTunes catalogue is matched by title; for the rest, one direct search each.

### 6.2 Iterations (all logged in `enrichment/tracks*.log`)

| Run | Change | Pools (Kyiv / Amapiano / Neoperreo / Mahraganat / AU) | Target | Verdict |
|---|---|---|---|---|
| 1 | Deezer top + iTunes only where Deezer had no artist; Deezer years | 658 / 480 / 533 / 229 / **124** | 124 | **Rejected.** Smaller than stage 1: remaster dates pushed 1990s AU tracks out of the period |
| 2 | MB original years | 658 / 480 / 525 / 229 / 152 | 152 | **Rejected.** AU still binding, and too small |
| 3 | iTunes for all artists | 1046 / 3008 / 1140 / 935 / 532 | 532 | **Rejected.** "Joi" got choral tracks (iTunes artist merge); 4 Hero contributed 57 drum & bass tracks to AU |
| 4 | Representativeness gate + iTunes vetting | 421 / 2085 / 898 / 593 / 313 | 313 | Close. But manual Wikipedia evidence and genre-article listings weren't parsed, and Ma3azef was mislabelled as press |
| 5 | Fixed Wikipedia parsing; Ma3azef NTS show reclassified as DJ evidence; Figo / Dakhlaweya / Amr Haha merges | 421 / 2085 / 927 / 593 / **350** | **350** | **Accepted** |
| 6 | Tracks taken from iTunes relabelled `not checked` on Deezer instead of "not on Deezer" | same | 350 | Final |

What these iterations show: each rejected version looked plausible from its top-level counts. The defects only appeared when I looked at samples (which tracks, which artists, which years). I expect the same to hold for the remaining defects.

### 6.3 Final corpus

| Case | Artists | Tracks | Stage 1 / Deezer / iTunes | Any preview | Deezer preview | iTunes preview | Years (min · median · max) | Top-3 artist share |
|---|---|---|---|---|---|---|---|---|
| Amapiano | 68 | 350 | 116 / 183 / 51 | 344 | 290 | 289 | 2014 · 2023 · 2026 | 6% |
| Asian Underground | 30 | 350 | 85 / 61 / 204 | 331 | 122 | 320 | 1993 · 1999 · 2017 | 42% |
| Kyiv | 34 | 350 | 90 / 11 / 249 | 310 | 28 | 308 | 2013 · 2022 · 2026 | 34% |
| Mahraganat | 19 | 350 | 59 / 27 / 264 | 315 | 41 | 310 | 2011 · 2020 · 2026 | 49% |
| Neoperreo | 36 | 350 | 104 / 184 / 62 | 328 | 263 | 214 | 2015 · 2022 · 2026 | 14% |
| **Total** | **187** | **1750** | 454 / 466 / 830 | 1628 | 744 | 1441 | | |

### 6.4 Density of gold-A artists (descriptive, not yet E1)

| Case | MBID | Median MB tags | ≥1 MB genre | Median MB relations | Wikidata | Median Wikipedia langs | In ListenBrainz similar | Deezer artist | Median Deezer fans | Median ListenBrainz users |
|---|---|---|---|---|---|---|---|---|---|---|
| Amapiano | 47% | 0 | 12% | 0 | 12% | 0 | 6% | 93% | 8,100 | 147 |
| Asian Underground | 97% | 0.5 | 40% | 9 | 57% | 1 | 83% | 67% | 4,454 | 3,804 |
| Kyiv | 74% | 0 | 12% | 5 | 0% | 0 | 9% | 38% | 53 | 9.5 |
| Mahraganat | 21% | 0 | 0% | 0 | 5% | 0 | 5% | 42% | 121,455 | 185 |
| Neoperreo | 64% | 0 | 25% | 4 | 22% | 0 | 44% | 78% | 749 | 401 |

---

## 7. What the process already says about the hypotheses (observations, not results)

- **H1 (density).** Only historical Asian Underground is dense across the open sources.
  - Amapiano, the planned "rich" control, is rich only on Deezer. In MB, Wikidata and ListenBrainz it looks as sparse as Kyiv. If "rich" is defined by open metadata, the control is not a control. I'd keep it, but call it "rich in platform presence, sparse in open metadata".
  - Mahraganat separates popularity from density: huge Deezer audiences, almost no open metadata. That is exactly why the plan's popularity covariate is needed, and this case will make it work.
- **H2 (failure taxonomy).** Concrete examples have already appeared:
  - **colliding:** the Serato "neoperreo vol 1" playlist labels mainstream reggaeton (Young Miko, Natti Natasha, Maldy, El Jordan 23, Standly) as neoperreo; the MB `neoperreo` tag spans Björk / HEALTH (stage 1);
  - **coarse / contested:** Cornershop fails Asian Underground membership; the term is an umbrella;
  - **untranslated:** Arabic/Latin entity splits; mahraganat MBID coverage is 21%.
- **E5b (ListenBrainz).** The similar-artists graph is close to empty for amapiano (6%), Kyiv (9%) and mahraganat (5%). Under the fixed algorithm, E5b will mostly measure *absence* for three of five cases. That is a finding in itself, and the reason the two-way precision@k rule was fixed in advance.

---

## 8. My thoughts: validity, judgment calls, and what I would watch

### 8.1 The biggest threat: gold-A rewards visibility

The ≥2-sources rule is sound as a precision device, but it is not neutral with respect to the thing being measured. An artist needs to be *documented* to pass. Documentation is what "metadata density" is about. So:
- **Pass rates track documentation:** Amapiano 89%, Kyiv 72%, Asian Underground 65%, Neoperreo 51%, Mahraganat 49%. The sparse scenes lose more members at the gate.
- **The survivors of a sparse scene are its best-documented members.** This **compresses the density contrast** that H1 tests. The measured gap between cases will be a lower bound.

**Mitigation I'd recommend:**
- Report E1 density for the excluded artists as well. They are in `Artists_excluded` with their metadata.
- Run E6 as a sensitivity analysis under a relaxed rule (≥1 key). If the density curve has the same shape under both, the conclusion is robust to the gate.

### 8.2 LLM involvement in gold-A

The plan forbids LLMs from forming *seeds*, so that gold doesn't inherit their training biases. I did not form seeds. But I did run the web verification, and the search tool returns LLM-written summaries alongside the result list.
- **What is solid:** every accepted piece of evidence is a real URL recorded in the CSV. I counted only hits whose title or URL already showed the connection. For example, "amapiano duo say winning two SAMAs is surreal" connects 2Point1 to amapiano from the headline alone.
- **What remains:** the decision to accept was still mine, and partly informed by summaries. This is a *dependency on an LLM judge*, which the plan's spirit warns about.
- **The fix** is the κ step the plan already has: a human second annotator on 20% of gold-A, sampled from the manually evidenced artists. If κ ≥ 0.6, the dependency is acceptable and can be stated as such.

### 8.3 Judgment calls that could reasonably go the other way

| Call | My choice | Effect if reversed |
|---|---|---|
| Cxema line-ups + Cxema NTS + Cxema press = 1 key | Conservative | More Kyiv artists would pass (e.g. Bakunn, E03, Pogulyay) |
| Media groups collapsed (IOL, Scene Media) | Conservative | A few amapiano / mahraganat artists would pass more easily |
| Arca accepted for neoperreo (Wikipedia + Hypebot) | Followed the rule | Stage 1 listed Arca as a *collision* example, so the rule and the domain intuition disagree here. Flag for κ |
| Wegz and Rozzma accepted for mahraganat (press ties them to it) | Followed the rule | They contribute many trap tracks; mahraganat's audio coherence drops, which matters for H3 |
| Representativeness gate | Added after seeing 4 Hero's catalogue | This is a post-hoc rule. It was fixed before any experiment, but after seeing the corpus. It must be reported as such |
| Scene periods | My cut-offs (e.g. amapiano ≥2012, mahraganat ≥2005) | Moving them by a few years changes pool sizes but not the design |
| Ma3azef NTS show as DJ evidence | Treated as track-level | As "press", Abyusif would count as representative |

### 8.4 Confounds I'd address before the audio experiments

1. **Preview source is correlated with case.** Deezer previews cover 83% of amapiano but 8% of Kyiv and 12% of mahraganat. If audio-kNN uses whatever preview exists, codec and excerpt differences between Deezer and iTunes become a *case-correlated* signal. iTunes covers every case more evenly (61–91%). **Recommendation:** use iTunes as the primary audio source for all cases, Deezer as fallback, and record the source as a covariate.
2. **Within-case concentration.** The top-3 artist share is 42% in Asian Underground and 49% in mahraganat, against 6% in amapiano. Track-level precision@k would be dominated by artist identity. **Recommendation:** average embeddings per artist; make the artist the unit of E5/E5b, as the plan says.
3. **Popularity ordering within artists.** Deezer top tracks are popularity-ordered, so the additions lean towards each artist's hits. That's acceptable for representativeness, but popularity must stay a covariate at track level too.
4. **Deezer duplicate artist pages.** Mohamed Ramadan's matched page has 35 fans. The popularity covariate needs a manual check for the sparse cases.

### 8.5 What went right

- **The pilot was the best investment.** It exposed matching errors with a score of 100 before any number was computed. Without it, the mahraganat "density" would partly have been a matching-failure rate.
- **Caching everything** made the six track iterations cost seconds rather than hours, and the snapshot is reproducible.
- **Checking samples, not totals**, caught every serious defect: co-credits, remaster dates, iTunes merges, compilation guests, mislabelled evidence.

### 8.6 What I would do differently

- **Fix the primary-artist split before enrichment.** About a dozen identity merges were discovered late. A first pass that splits credits properly and clusters names by script and alias would have saved two review cycles.
- **Fix the representativeness gate in the protocol up front**, together with the period cut-offs, instead of introducing them after a rejected run.
- **For neoperreo, the genre-article scan was faster and more systematic** than one search per artist. I'd use it from the start for every case: fetch the scene's defining articles, then extract artist mentions.

---

## 9. Reproducibility

```bash
python enrich.py          # metadata snapshot (reads/writes enrichment/cache.sqlite)
python review.py          # gold-A evidence (uses goldA_manual_evidence.csv)
python tracks.py          # balanced corpus -> capstone_corpus_stage2.xlsx
python describe.py        # statistics
```

- **Deterministic from the cache.** Deleting `cache.sqlite` re-queries live APIs, which can change results; that is the reason it is kept.
- **Not reproducible automatically:** the web searches. Their output is the evidence CSV, which is the record of that step.
- **`Run_meta` sheets** hold the thresholds, rules, algorithm strings and timestamps.

---

## 10. Open items

1. κ on a 20% gold-A sample, a second human annotator. Prioritise the web-evidenced artists and the boundary cases (Arca, Wegz, Rozzma, Oscar Mbo, Ntate Stunna).
2. Collect the contemporary Asian Underground pool from independent seeds (collectives' line-ups, Hundred Colours and Bheja Fry catalogues).
3. Arabic names for mahraganat artists, then re-run matching (cached, minutes).
4. Deezer lookup for tracks taken from iTunes, then decide the single audio source (see 8.4).
5. Compute E1 per source, including the excluded artists (see 8.1).
6. Fill the `failure_type` column of `Fill_in_manual` using the H2 examples in section 7 as seeds.
