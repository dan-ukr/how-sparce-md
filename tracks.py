"""Stage-2 corpus: all tracks of gold-A artists, balanced across cases.

Pool per gold-A artist: stage-1 tracks (always kept) + Deezer top tracks with a preview + iTunes
tracks (for artists not on Deezer; the iTunes artist is accepted only if it shares >=1 title with
the artist's known tracks). Added tracks must fall inside the scene period (PERIOD); versions and
remixes of one title are collapsed. Each case is filled round-robin over its artists (stage-1 tracks
first, then Deezer popularity order) up to one common target, so cases are equal in size and no
single artist dominates. Every selected track is then checked for an iTunes preview.

Output: capstone_corpus_stage2.xlsx, capstone_corpus_stage2_tracks.csv (the Tracks sheet)
"""
import re, sys
from collections import deque
from pathlib import Path
from urllib.parse import unquote

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from enrich import Http, OUT, NAME_OK, TITLE_OK, norm, sim, itunes_track, log  # noqa: E402

HERE = Path(__file__).parent
ENR = sorted(OUT.glob("enriched_20*.xlsx"))[-1]
# scene periods for ADDED tracks (release year); stage-1 tracks are source-grounded and kept as is
PERIOD = {"Kyiv": (2013, 2026), "Amapiano": (2012, 2026), "Neoperreo": (2014, 2026),
          "Mahraganat": (2005, 2026), "Asian": (1993, 2003)}
PA = "primary_artist (heuristic)"


def cprefix(case):
    return next(p for p in PERIOD if case.startswith(p))


def year_of(s):
    try:
        return int(str(s)[:4])
    except ValueError:
        return None


def mb_years(h, mbid):
    """Original release years from MB: by recording title, and by release-group (album) title.
    Deezer dates albums by their digital (re)release, so remasters of 1990s records read as 2010s."""
    rec, rg = {}, {}
    if not isinstance(mbid, str):
        return rec, rg
    for offset in (0, 100):
        res = h.get("https://musicbrainz.org/ws/2/recording", {"artist": mbid, "limit": 100, "offset": offset, "fmt": "json"}) or {}
        for r in res.get("recordings", []):
            y = year_of(r.get("first-release-date") or "")
            if y:
                rec[norm(r["title"])] = min(y, rec.get(norm(r["title"]), y))
        if len(res.get("recordings", [])) < 100:
            break
    res = h.get("https://musicbrainz.org/ws/2/release-group", {"artist": mbid, "limit": 100, "fmt": "json"}) or {}
    for g in res.get("release-groups", []):
        y = year_of(g.get("first-release-date") or "")
        if y:
            rg[norm(g["title"])] = min(y, rg.get(norm(g["title"]), y))
    return rec, rg


# artist is "representative" (gets tracks beyond stage 1) if >=3 keys, artist-level evidence
# (press / film / event / scene line-up or collective), or their own Wikipedia article names the scene
STRICT = {"Kyiv": r"cxema|схема|kyiv techno|kiev techno|kyiv rave|kiev rave", "Amapiano": r"amapiano",
          "Neoperreo": r"neo[- ]?perreo", "Mahraganat": r"mahragan|electro[- ]?c?h?aa?bi|electro[- ]?sha+bi|مهرجان",
          "Asian": r"asian underground|asian massive"}
ARTIST_LEVEL = ("press:", "film:", "event:", "Cxema", "Neoperreo collective", "Strichka festival")
GENRE_PAGES = {"Neoperreo", "Amapiano", "Mahraganat", "Asian Underground", "Cxema"}


def representative(h, a):
    keys = a.keys.split(" | ")
    if len(keys) >= 3 or any(k.startswith(ARTIST_LEVEL) for k in keys):
        return True
    for ev in a.evidence.split(" || "):
        if not ev.startswith("Wikipedia:"):
            continue
        m = (re.search(r"(\w+)\.wikipedia: (.+?)(?: \(mentions artist\))?$", ev)
             or re.search(r"//(\w+)\.wikipedia\.org/wiki/(\S+)", ev))
        if not m:
            continue
        lang, title = m.group(1), unquote(m.group(2)).replace("_", " ")
        if title in GENRE_PAGES:  # listed in the scene's own article
            return True
        if "(mentions artist)" in ev:  # a mention on some other artist's page is not enough
            continue
        res = h.get(f"https://{lang}.wikipedia.org/w/api.php", {"action": "query", "prop": "extracts",
                    "explaintext": 1, "titles": title, "format": "json", "redirects": 1})
        text = " ".join(pg.get("extract", "") for pg in (res or {}).get("query", {}).get("pages", {}).values())
        if re.search(STRICT[cprefix(a.case)], text, re.I):
            return True
    return False


def itunes_pool(h, names, known_titles, mb_titles):
    """iTunes songs of the artist. iTunes merges same-name artists into one page, so for MB-matched
    artists a song must match one of their MB recording titles; otherwise the iTunes artist must
    share >=2 titles with the artist's known tracks."""
    by_artist = {}
    for name in dict.fromkeys(n for n in names if isinstance(n, str)):
        res = h.get("https://itunes.apple.com/search",
                    {"term": name, "entity": "song", "attribute": "artistTerm", "limit": 200})
        for t in (res or {}).get("results", []):
            if sim(name, t.get("artistName", "")) >= NAME_OK:
                by_artist.setdefault(t["artistId"], {})[t["trackId"]] = t
    every = [s for songs in by_artist.values() for s in songs.values()]  # for title-matched preview lookup only
    if mb_titles:
        return every, [s for s in every if norm(s.get("trackName", "")) in mb_titles]
    vetted = []
    for songs in by_artist.values():
        if sum(any(sim(k, s.get("trackName", "")) >= TITLE_OK for s in songs.values()) for k in set(known_titles)) >= 2:
            vetted += songs.values()
    return every, vetted


def main():
    h = Http(OUT / "cache.sqlite")
    gold = pd.read_csv(OUT / "goldA_evidence.csv")
    A = pd.read_excel(ENR, sheet_name="Artists")
    T = pd.read_excel(ENR, sheet_name="Tracks")
    # one row per canonical artist (review.py merges spellings / remix credits); IDs and density
    # columns come from the variant that has an MB match, else the first variant
    rows = []
    for g in gold.itertuples(index=False):
        vs = A[(A.case == g.case) & A.artist.isin(g.variants.split(" | "))]
        base = vs.sort_values("mb_artist", na_position="last").iloc[0].to_dict()
        for c in ("dz_artist", "dz_name", "dz_fans", "mb_name"):  # fill from any variant
            if pd.isna(base.get(c)) and vs[c].notna().any():
                base[c] = vs[c].dropna().iloc[0]
        base.update(g._asdict())
        rows.append(base)
    A = pd.DataFrame(rows)
    G = A[A.goldA].reset_index(drop=True)
    log("gold-A artists:", G.groupby("case").size().to_dict())

    pools, reps = {}, {}  # (case, artist) -> list of track dicts in priority order / representative flag
    for n, a in enumerate(G.itertuples(index=False), 1):
        p = cprefix(a.case)
        lo, hi = PERIOD[p]
        own = T[(T.case == a.case) & T[PA].map(norm).isin({norm(v) for v in a.variants.split(" | ")})]
        rec_y, rg_y = mb_years(h, a.mb_artist)

        def stage1_year(r):
            if r.year_basis == "release" and pd.notna(r.year):
                return int(r.year)
            if pd.notna(r.mb_first_release) and year_of(r.mb_first_release):
                return year_of(r.mb_first_release)
            if pd.notna(r.dz_track):
                return year_of((h.get(f"https://api.deezer.com/track/{int(r.dz_track)}") or {}).get("release_date") or "")
        items = [dict(case=a.case, artist=a.artist, track=r.track, origin=f"stage1:{r.source_id}",
                      year=stage1_year(r), dz_track=r.dz_track, dz_preview=r.dz_preview, itunes_id=r.itunes_id,
                      itunes_preview=r.itunes_preview, mb_recording=r.mb_recording, in_period=None)
                 for r in own.itertuples(index=False)]
        top = []
        rep = representative(h, a)
        reps[(a.case, a.artist)] = rep
        if rep and pd.notna(a.dz_artist):
            top = (h.get(f"https://api.deezer.com/artist/{int(a.dz_artist)}/top", {"limit": 100}) or {}).get("data", [])
            for t in top:
                if not t.get("preview"):
                    continue
                y = rec_y.get(norm(t["title"])) or rg_y.get(norm(t["album"]["title"])) or year_of(
                    (h.get(f"https://api.deezer.com/album/{t['album']['id']}") or {}).get("release_date") or "")
                items.append(dict(case=a.case, artist=a.artist, track=t["title"], origin="deezer_top", year=y,
                                  dz_track=t["id"], dz_preview="Y", dz_rank=t.get("rank"),
                                  in_period=y is not None and lo <= y <= hi))
        known = [i["track"] for i in items]
        itp, vetted = itunes_pool(h, [a.artist, a.mb_name, a.dz_name], known, set(rec_y))
        if rep:  # iTunes songs after Deezer ones (lower priority); dedup below drops titles already in
            for t in vetted:
                y = rec_y.get(norm(t["trackName"])) or year_of(t.get("releaseDate") or "")
                items.append(dict(case=a.case, artist=a.artist, track=t["trackName"], origin="itunes_artist", year=y,
                                  itunes_id=t["trackId"], itunes_preview="Y" if t.get("previewUrl") else "N",
                                  in_period=y is not None and lo <= y <= hi))
        # dedupe versions; keep stage-1 rows and in-period additions
        seen, kept = set(), []
        for i in items:
            k = norm(i["track"])
            if k in seen or (not i["origin"].startswith("stage1") and not i["in_period"]):
                continue
            seen.add(k)
            # iTunes preview check against the artist's iTunes songs
            if pd.isna(i.get("itunes_id")):
                m = next((t for t in itp if sim(i["track"], t.get("trackName", "")) >= TITLE_OK), None)
                if m:
                    i.update(itunes_id=m["trackId"], itunes_preview="Y" if m.get("previewUrl") else "N")
            kept.append(i)
        pools[(a.case, a.artist)] = kept
        if n % 20 == 0:
            log(f"artists {n}/{len(G)}")

    sizes = {c: len({(norm(i['track'])) for (cc, _), v in pools.items() if cc == c for i in v}) for c in G.case.unique()}
    target = min(sizes.values())
    log("pool sizes:", sizes, "-> target per case:", target)

    rows = []
    for c in G.case.unique():
        q = {k: deque(v) for k, v in sorted(pools.items()) if k[0] == c}
        sel, seen = [], set()
        while len(sel) < target and any(q.values()):
            for k in q:
                while q[k]:
                    i = q[k].popleft()
                    if norm(i["track"]) not in seen:
                        seen.add(norm(i["track"]))
                        sel.append(i)
                        break
                if len(sel) >= target:
                    break
        rows += sel
    C = pd.DataFrame(rows)

    # tracks without any preview yet: one direct iTunes search each
    for idx, r in C[(C.dz_preview != "Y") & C.itunes_id.isna()].iterrows():
        m = itunes_track(h, [r.artist], r.track)
        if m:
            C.loc[idx, ["itunes_id", "itunes_preview"]] = [m["itunes_id"], m["itunes_preview"]]
    C["itunes_preview"] = C.itunes_preview.fillna("not found")
    C["dz_preview"] = C.dz_preview.fillna(C.origin.map(lambda o: "not matched on Deezer" if o.startswith("stage1") else "not checked"))
    for col in ("dz_track", "itunes_id", "year"):
        C[col] = pd.to_numeric(C[col], errors="coerce").astype("Int64")

    G["n_tracks_selected"] = G.apply(lambda a: int(((C.case == a.case) & (C.artist == a.artist)).sum()), axis=1)
    G["n_tracks_pool"] = G.apply(lambda a: len(pools[(a.case, a.artist)]), axis=1)
    G["representative"] = G.apply(lambda a: reps[(a.case, a.artist)], axis=1)
    excluded = A[~A.goldA][["case", "artist", "variants", "seed_method", "n_keys", "keys", "evidence", "match_status"]]
    meta = pd.DataFrame([("goldA_rule", ">=2 independent keys; see review.py docstring"),
                         ("period_for_added_tracks", str(PERIOD)), ("pool_sizes", str(sizes)),
                         ("target_per_case", target), ("selection", "round-robin over artists; stage-1 first, then Deezer rank, then iTunes"),
                         ("representative_rule", "expand beyond stage-1 only if >=3 keys, artist-level evidence (press/film/event/scene line-up), or own Wikipedia article names the scene"),
                         ("itunes_vetting", "MB-matched artists: song title must match an MB recording; else iTunes artist shares >=2 titles with known tracks"),
                         ("added_tracks_require", "Deezer preview (Deezer artists) or iTunes listing (artists not on Deezer)")],
                        columns=["key", "value"])
    with pd.ExcelWriter(HERE / "capstone_corpus_stage2.xlsx") as xw:
        C.to_excel(xw, sheet_name="Tracks", index=False)
        G.to_excel(xw, sheet_name="Artists_goldA", index=False)
        excluded.to_excel(xw, sheet_name="Artists_excluded", index=False)
        meta.to_excel(xw, sheet_name="Run_meta", index=False)
    C.to_csv(HERE / "capstone_corpus_stage2_tracks.csv", index=False, encoding="utf-8-sig")
    log("wrote capstone_corpus_stage2.xlsx", C.groupby("case").size().to_dict())


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
