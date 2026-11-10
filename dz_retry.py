"""Second Deezer pass over stage-2 tracks with no Deezer match (dz_preview != "Y").

Stage 2 searched Deezer once per stage-1 track (corpus spelling only) and took each artist's Deezer
top-100; the iTunes-sourced tracks were never searched on Deezer. Per track, first hit wins:
  1. catalog  - full album catalogue of the artist's Deezer page(s); title match on any title variant
                (as is, brackets/feat. stripped, Cyrillic transliterated) on either side
  2. isrc     - ISRCs of the matched MB recording -> Deezer /track/isrc:<ISRC>
  3. search   - Deezer search over artist-name variants (corpus, gold variants, MB name + aliases,
                Deezer name, transliteration) x title variants
  4. skeleton - cross-script / Arabizi spellings (e.g. Arabic on iTunes, Latin on Deezer): consonant
                skeletons similar (>= 0.75) AND duration within 2 s of the iTunes/MB length, inside the
                artist's own Deezer catalogue; only for Arabic-script/Arabizi titles. Without a known
                length: identical skeletons of >= 4 consonants. Flagged for review.
  5. album_duration - translated/re-spelled titles: Deezer album title ~ iTunes collection name and
                length within 1 s, exactly one candidate. Flagged for review.
  6. album_search - V.A. compilations/soundtracks (absent from the artist's album list): Deezer
                album search by the iTunes collection name, then title + artist inside the album.
Fuzzy titles (not equal after bracket stripping) in 1/3/6 must agree in length within 3 s when the
iTunes/MB length is known: the first full run showed substring matches like "Bota" -> "Bota Bota".
level: same recording (length within 3 s, or exact title with no length); other version (title differs
by a remix/edit/mix qualifier and length differs); same title, length differs (album vs single edit, or a
different track with a generic title such as "Intro"); unverified (fuzzy title, no length).
Review flags: isrc_review (no Deezer contributor matches the artist), search_xscript_review (title-only
search, artist matched by name skeleton across scripts), skeleton_review; title_exact=N marks a
different version (remix/edit) or a substring match.
Deezer artist pages: the one from enrichment, plus same-name pages (any name variant) whose catalogue
shares >= 2 titles with the artist's stage-2 tracks (the dz_artist_by_overlap rule).
Writes enrichment/deezer_retry.csv; the stage-2 corpus is not modified.
"""
import argparse, json, re, sys, unicodedata
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from enrich import Http, OUT, NAME_OK, TITLE_OK, norm, sim, lq, log, FAILS  # noqa: E402

HERE = Path(__file__).parent
DZ = "https://api.deezer.com"
SKEL_OK, DUR_OK, FUZZY_DUR = 0.75, 2, 3
# Checked by hand after the first full run (title rule passes, but a different song):
MANUAL_REJECT = {("uLazi", "ULAZI Infinity MusiQ (yey)"): "Deezer 'ULAZI INFINITY MUSIQ (PAIN)' is another track, 114 s longer"}
ARABIC = re.compile(r"[؀-ۿ]|[a-z][2357]|[2357][a-z]", re.I)  # Arabic script or Arabizi digits

CYR = dict(zip("абвгґдезиіїйклмнопрстуфьыэъё", "abvhgdezyiiiklmnoprstuf'yee'e")) | {
    "є": "ie", "ж": "zh", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ю": "iu", "я": "ia"}
AR = {**dict.fromkeys("اأإآءئؤوىيةـ", ""), **dict.fromkeys("ثتط", "t"), **dict.fromkeys("سشص", "s"),
      **dict.fromkeys("ذدض", "d"), **dict.fromkeys("زظ", "z"), **dict.fromkeys("حه", "h"),
      **dict.fromkeys("خقك", "k"), **dict.fromkeys("جغ", "g"), "ب": "b", "ر": "r", "ع": "", "ف": "f",
      "ل": "l", "م": "m", "ن": "n"}
LAT = [("5", "k"), ("7", "h"), ("8", "g"), ("9", "k"), ("6", "t"), ("kh", "k"), ("gh", "g"), ("sh", "s"),
       ("ch", "s"), ("th", "t"), ("dh", "d"), ("j", "g"), ("q", "k"), ("c", "k"), ("x", "ks"), ("p", "b"),
       ("v", "f")]


def translit(s):
    return "".join(CYR.get(ch, ch) for ch in str(s).casefold())


def skeleton(s):
    """Coarse consonant skeleton, comparable across Arabic script, Arabizi and Latin spellings."""
    s = norm(translit(s))
    s = "".join(AR.get(ch, ch) for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))
    for a, b in LAT:
        s = s.replace(a, b)
    s = re.sub(r"[^a-z]|[aeiouwy]", "", s)
    return re.sub(r"(.)\1+", r"\1", s)


def tvars(title):
    return list(dict.fromkeys(v for v in (title, norm(title), translit(norm(title))) if v))


def tsim(a, b):
    return max(sim(x, y) for x in tvars(a) for y in tvars(b))


def nsim(names, n):
    return max(max(sim(x, n), sim(x, translit(n)), sim(translit(x), n)) for x in names)


def ssim(a, b):
    a, b = skeleton(a), skeleton(b)
    return SequenceMatcher(None, a, b).ratio() if len(a) >= 2 and len(b) >= 2 else 0.0


def dz_list(h, url, params=None):
    out, params = [], dict(params or {}, limit=100, index=0)
    while True:
        res = h.get(url, params) or {}
        out += res.get("data", [])
        if not res.get("next") or not res.get("data"):
            return out
        params = dict(params, index=params["index"] + 100)


def catalogue(h, aid):
    """All tracks on the albums of Deezer artist aid (album title attached)."""
    tracks = []
    for al in dz_list(h, f"{DZ}/artist/{aid}/albums"):
        for t in dz_list(h, f"{DZ}/album/{al['id']}/tracks"):
            tracks.append(dict(t, album_title=al["title"]))
    return tracks


def itunes_records(h):
    """trackId -> iTunes record, from every cached iTunes search response."""
    out = {}
    for (body,) in h.db.execute("select body from r where k like '[\"https://itunes.apple.com/search%' and status=200"):
        for t in json.loads(body).get("results", []):
            if "trackId" in t:
                out.setdefault(t["trackId"], t)
    return out


def full(s):
    """Title with version brackets kept (remix/edit/mixed count), only feat. credits and punctuation dropped."""
    s = unicodedata.normalize("NFKC", str(s)).casefold()
    s = re.sub(r"[\(\[]\s*(feat|ft|with)\b[^\)\]]*[\)\]]", "", s)
    s = re.sub(r"\b(feat|ft|featuring)\b\.?.*", "", s)
    return " ".join(translit(re.sub(r"[^\w\s]", " ", s)).split())


def hit(t, method, detail="", track=""):
    exact = full(t["title"]) == full(track)
    return dict(dz_track=t["id"], dz_title=t["title"], title_exact="Y" if exact else "N", dz_artist_name=(t.get("artist") or {}).get("name"),
                dz_album=t.get("album_title") or (t.get("album") or {}).get("title"),
                dz_preview="Y" if t.get("preview") else "N", dz_duration=t.get("duration"),
                method=method, detail=detail)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, help="first N unmatched tracks per case (test run)")
    args = ap.parse_args()
    h = Http(OUT / "cache.sqlite")
    C = pd.read_csv(HERE / "capstone_corpus_stage2_tracks.csv")
    gold = pd.read_csv(OUT / "goldA_evidence.csv")
    A = pd.read_excel(OUT / "enriched_2026-11-02.xlsx", sheet_name="Artists")
    todo = C[C.dz_preview != "Y"]
    if args.sample:
        todo = todo.groupby("case").head(args.sample)
    log("tracks to retry:", len(todo), todo.groupby("case").size().to_dict())
    itr = itunes_records(h)
    log("cached iTunes records:", len(itr))
    # an iTunes ID shared by corpus rows with different titles is a bad stage-2 match: don't trust its length
    bad_it = {i for i, g in C[C.itunes_id.notna()].groupby("itunes_id")
              if g.track.nunique() > 1 and any(tsim(x, y) < TITLE_OK for x in g.track for y in g.track)}
    log("iTunes IDs shared by different titles (length ignored):", sorted(int(i) for i in bad_it))

    # ---- per artist: name variants and vetted Deezer pages with their catalogues
    artists = {}
    for (case, artist), g in todo.groupby(["case", "artist"]):
        gv = gold[(gold.case == case) & (gold.artist == artist)]
        variants = gv.variants.iloc[0].split(" | ") if len(gv) else [artist]
        av = A[(A.case == case) & A.artist.isin(variants)]
        mbid = gv.mb_artist.iloc[0] if len(gv) and pd.notna(gv.mb_artist.iloc[0]) else None
        names = [artist, *variants, *av.mb_name.dropna(), *av.dz_name.dropna()]
        if mbid:
            mb = h.get(f"https://musicbrainz.org/ws/2/artist/{mbid}",
                       {"fmt": "json", "inc": "aliases+tags+genres+url-rels+artist-rels+label-rels+event-rels"}) or {}
            names += [x["name"] for x in mb.get("aliases", [])]
        names += [translit(n) for n in names if translit(n) != n.casefold()]
        seen = {}
        for n in names:
            seen.setdefault(norm(n), n)
        names = [n for k, n in seen.items() if k]
        titles = C[(C.case == case) & (C.artist == artist)].track.tolist()
        known = {int(x) for x in av.dz_artist.dropna()}
        pages = {aid: catalogue(h, aid) for aid in known}
        for n in names[:6]:
            for c in (h.get(f"{DZ}/search/artist", {"q": n, "limit": 5}) or {}).get("data", []):
                if c["id"] in pages or nsim([n], c["name"]) < NAME_OK:
                    continue
                cat = catalogue(h, c["id"])
                overlap = sum(any(tsim(k, x["title"]) >= TITLE_OK for x in cat) for k in set(titles))
                if overlap >= min(2, len(set(titles))):
                    pages[c["id"]] = cat
                    log(f"  + page {c['name']} ({c['id']}) for {artist}: {overlap} shared titles")
        artists[(case, artist)] = dict(names=names, pages=pages)
        log(f"{case[:10]} | {artist}: {len(names)} names, pages {list(pages)}, "
            f"{sum(map(len, pages.values()))} catalogue tracks")

    # ---- per track
    out = []
    for idx, r in todo.iterrows():
        a = artists[(r.case, r.artist)]
        cat = [t for p in a["pages"].values() for t in p]
        rec = itr.get(int(r.itunes_id)) if pd.notna(r.itunes_id) and r.itunes_id not in bad_it else None
        length = rec.get("trackTimeMillis", 0) / 1000 if rec else None
        isrcs = []
        if pd.notna(r.mb_recording):
            m = h.get(f"https://musicbrainz.org/ws/2/recording/{r.mb_recording}", {"fmt": "json", "inc": "isrcs"}) or {}
            isrcs = m.get("isrcs", [])
            length = length or (m.get("length") or 0) / 1000 or None
        res = None

        def len_ok(t, tol=FUZZY_DUR):
            """A fuzzy title (not equal after bracket stripping) must agree in length when the length is known."""
            return tsim(r.track, t["title"]) >= 1 or not length or abs((t.get("duration") or 0) - length) <= tol

        # 1. catalogue, title variants
        for s_, t in sorted(((tsim(r.track, t["title"]), t) for t in cat), key=lambda p: -p[0]):
            if s_ < TITLE_OK:
                break
            if len_ok(t):
                res = hit(t, "catalog", f"title_sim={s_:.2f}", r.track)
                break
        # 2. ISRC
        for i in isrcs if not res else []:
            t = h.get(f"{DZ}/track/isrc:{i}") or {}
            if t.get("id"):
                who = [t["artist"]] + t.get("contributors", [])
                ok = any(x.get("id") in a["pages"] or nsim(a["names"], x.get("name", "")) >= NAME_OK for x in who)
                res = hit(t, "isrc" if ok else "isrc_review", f"{i}; title_sim={tsim(r.track, t['title']):.2f}", r.track)
                break
        # 3. search with name x title variants
        if not res:
            for n in a["names"][:4]:
                for tv in tvars(r.track)[:3]:
                    for q in (f"artist:{lq(n)} track:{lq(tv)}", f"{n} {tv}"):
                        for t in (h.get(f"{DZ}/search", {"q": q, "limit": 10}) or {}).get("data", []):
                            ok_artist = t["artist"]["id"] in a["pages"] or nsim(a["names"], t["artist"]["name"]) >= NAME_OK
                            if ok_artist and tsim(r.track, t["title"]) >= TITLE_OK and len_ok(t):
                                res = hit(t, "search", f"q={q}", r.track)
                                break
                        if res:
                            break
                    if res:
                        break
                if res:
                    break
        # 3b. Arabic title alone; artist accepted by name skeleton across scripts
        if not res and ARABIC.search(r.track):
            for t in (h.get(f"{DZ}/search", {"q": f"track:{lq(r.track)}", "limit": 25}) or {}).get("data", []):
                s = max(ssim(n, t["artist"]["name"]) for n in a["names"])
                if tsim(r.track, t["title"]) >= TITLE_OK and s >= SKEL_OK and len_ok(t):
                    res = hit(t, "search_xscript_review", f"artist_skel={s:.2f}", r.track)
                    break
        # 4. cross-script skeleton, own catalogue only: + duration when the length is known,
        #    else the skeletons must be identical and >= 4 consonants
        xs = [t for t in cat if ARABIC.search(r.track) or ARABIC.search(t["title"])]
        if not res and length:
            cands = [(ssim(r.track, t["title"]), abs(t.get("duration", 0) - length), t) for t in xs]
            cands = [c for c in cands if c[0] >= SKEL_OK and c[1] <= DUR_OK]
            if cands:
                s, d, t = max(cands, key=lambda c: (c[0], -c[1]))
                res = hit(t, "skeleton_review", f"skel={s:.2f} ({skeleton(r.track)}~{skeleton(t['title'])}); dur_diff={d:.0f}s", r.track)
        if not res and not length and len(skeleton(r.track)) >= 4:
            same = [t for t in xs if skeleton(t["title"]) == skeleton(r.track)]
            if same:
                res = hit(same[0], "skeleton_review", f"identical skeleton {skeleton(r.track)}; no length to compare", r.track)
        # 5. translated / re-spelled title: same album name (iTunes collection) and length within 1 s
        if not res and rec and length:
            cands = [t for t in cat if abs(t.get("duration", 0) - length) <= 1
                     and tsim(rec.get("collectionName", ""), t["album_title"]) >= TITLE_OK]
            if len(cands) == 1:
                t = cands[0]
                res = hit(t, "album_duration_review", f"album {rec.get('collectionName')} ~ {t['album_title']}", r.track)
        # 6. V.A. compilations / soundtracks (not on the artist's album list): Deezer album search
        #    by the iTunes collection name, then title + artist inside the album
        if not res and rec and rec.get("collectionName"):
            coll = re.sub(r"\s+-\s+(Single|EP)$", "", rec["collectionName"])
            for al in (h.get(f"{DZ}/search/album", {"q": coll, "limit": 10}) or {}).get("data", []):
                if tsim(coll, al["title"]) < TITLE_OK:
                    continue
                for t in dz_list(h, f"{DZ}/album/{al['id']}/tracks"):
                    ok_artist = t["artist"]["id"] in a["pages"] or nsim(a["names"], t["artist"]["name"]) >= NAME_OK
                    if ok_artist and tsim(r.track, t["title"]) >= TITLE_OK and len_ok(t):
                        res = hit(dict(t, album_title=al["title"]), "album_search", f"album={al['title']}", r.track)
                        break
                if res:
                    break
        if res and (r.artist, r.track) in MANUAL_REJECT:
            res = dict(method="not found", detail="manual reject: " + MANUAL_REJECT[(r.artist, r.track)])
        if res and res.get("dz_duration") and length:
            res["dur_diff"] = round(abs(res["dz_duration"] - length), 1)
        if res and res["method"] != "not found":
            d = res.get("dur_diff")
            res["level"] = ("same recording" if (d is not None and d <= FUZZY_DUR) or (d is None and res["title_exact"] == "Y")
                            else "unverified (no length)" if d is None
                            else "same title, length differs" if res["title_exact"] == "Y" else "other version")
        out.append(dict(row=idx, case=r.case, artist=r.artist, track=r.track, origin=r.origin,
                        old_status=r.dz_preview, **(res or dict(method="not found"))))
    R = pd.DataFrame(out)
    path = OUT / ("deezer_retry_sample.csv" if args.sample else "deezer_retry.csv")
    R.to_csv(path, index=False, encoding="utf-8-sig")
    log("wrote", path, "| failed requests:", len(FAILS))
    print(pd.crosstab(R.case, R.method, margins=True).to_string())
    print(pd.crosstab(R.case, R.level.fillna('not found'), margins=True).to_string())


if __name__ == "__main__":
    main()
