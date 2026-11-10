"""Track manifest for the Deezer audio-embedding run (input CSV of embed_tracks.py).

One row per stage-2 corpus track (all 1750, so coverage per case can be reported):
  track_id         T + 0-based row number in capstone_corpus_stage2_tracks.csv (the join key)
  isrc             Deezer's ISRC for the matched track; else the first ISRC of the MB recording
  deezer_track_id  stage-2 match, else the deezer_retry.csv match (blank if not on Deezer)
  artist, title, case   as in the corpus
  artist_id        own ID per (case, artist): <case prefix>-<nnn>, artists sorted by name
  deezer_artist_id the track's Deezer contributor that is the corpus artist; else the artist's
                   Deezer page from enrichment; blank if neither
  album            Deezer album title; else the iTunes collection name
  deezer_match     stage2 | level from deezer_retry.csv (same recording / other version / ...)
  isrc_source      deezer | musicbrainz
Writes deezer_tracks_manifest.csv.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from enrich import Http, OUT, NAME_OK, log, FAILS  # noqa: E402
from dz_retry import DZ, nsim, itunes_records  # noqa: E402
from tracks import cprefix  # noqa: E402

HERE = Path(__file__).parent


def main():
    h = Http(OUT / "cache.sqlite")
    C = pd.read_csv(HERE / "capstone_corpus_stage2_tracks.csv")
    R = pd.read_csv(OUT / "deezer_retry.csv").set_index("row")
    gold = pd.read_csv(OUT / "goldA_evidence.csv")
    A = pd.read_excel(OUT / "enriched_2026-11-02.xlsx", sheet_name="Artists")
    itr = itunes_records(h)

    aid, names, page = {}, {}, {}
    for case, g in C.groupby("case", sort=False):
        for n, artist in enumerate(sorted(g.artist.unique(), key=str.casefold), 1):
            aid[(case, artist)] = f"{cprefix(case)[:3].upper()}-{n:03d}"
            gv = gold[(gold.case == case) & (gold.artist == artist)]
            variants = gv.variants.iloc[0].split(" | ") if len(gv) else [artist]
            av = A[(A.case == case) & A.artist.isin(variants)]
            names[(case, artist)] = [artist, *variants, *av.mb_name.dropna(), *av.dz_name.dropna()]
            page[(case, artist)] = int(av.dz_artist.dropna().iloc[0]) if av.dz_artist.notna().any() else None

    rows = []
    for idx, r in C.iterrows():
        key = (r.case, r.artist)
        dz, match = (r.dz_track, "stage2") if pd.notna(r.dz_track) else (None, None)
        if dz is None and idx in R.index and R.loc[idx, "method"] != "not found":
            dz, match = R.loc[idx, "dz_track"], R.loc[idx, "level"]
        isrc = src = album = dz_artist = None
        if dz is not None:
            t = h.get(f"{DZ}/track/{int(dz)}") or {}
            isrc, src = t.get("isrc") or None, "deezer" if t.get("isrc") else None
            album = (t.get("album") or {}).get("title")
            who = [t["artist"]] + t.get("contributors", []) if t.get("artist") else []
            dz_artist = next((x["id"] for x in who if x.get("id") == page[key]
                              or nsim(names[key], x.get("name", "")) >= NAME_OK), None)
        if not isrc and pd.notna(r.mb_recording):
            m = h.get(f"https://musicbrainz.org/ws/2/recording/{r.mb_recording}", {"fmt": "json", "inc": "isrcs"}) or {}
            if m.get("isrcs"):
                isrc, src = m["isrcs"][0], "musicbrainz"
        if not album and pd.notna(r.itunes_id):
            album = itr.get(int(r.itunes_id), {}).get("collectionName")
        rows.append(dict(track_id=f"T{idx:04d}", isrc=isrc, deezer_track_id=int(dz) if dz is not None else None,
                         artist=r.artist, title=r.track, case=r.case, artist_id=aid[key],
                         deezer_artist_id=dz_artist or page[key], album=album, deezer_match=match,
                         isrc_source=src))
        if idx % 250 == 0:
            log(idx, "/", len(C))
    M = pd.DataFrame(rows)
    for c in ("deezer_track_id", "deezer_artist_id"):
        M[c] = M[c].astype("Int64")
    path = HERE / "deezer_tracks_manifest.csv"
    M.to_csv(path, index=False, encoding="utf-8-sig")
    log("wrote", path, "| failed requests:", len(FAILS))
    M["c"] = M.case.map(cprefix)
    print(M.groupby("c").agg(tracks=("track_id", "size"), deezer_id=("deezer_track_id", "count"),
                             isrc=("isrc", "count"), deezer_artist=("deezer_artist_id", "count"),
                             artists=("artist_id", "nunique")).to_string())
    print(M.isrc_source.value_counts(dropna=False).to_string())


if __name__ == "__main__":
    main()
