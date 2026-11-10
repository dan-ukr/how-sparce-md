"""Stage-1 corpus enrichment: MusicBrainz, Wikidata, Deezer, iTunes, ListenBrainz.

Usage: python enrich.py [--sample N]
Reads capstone_corpus_stage1_tracks.csv, writes enrichment/enriched_<date>.xlsx.
Every HTTP response is cached in enrichment/cache.sqlite: the cache is the dated
snapshot, and a re-run resumes from it.

Matching rule (summary, sec. 3 item 8): an artist ID is accepted only with track
evidence: the MB recording search matches both title and artist name, or the
Deezer ISRC resolves to an MB recording. Everything else goes to Match_review.
"""
import argparse, datetime as dt, json, re, sqlite3, sys, time, unicodedata
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd
import requests

HERE = Path(__file__).parent
OUT = HERE / "enrichment"
UA = "capstone-density/0.1 (daniil.swed@gmail.com)"
LB_ALGO = "session_based_days_7500_session_300_contribution_5_threshold_10_limit_100_filter_True_skip_30"
MIN_INTERVAL = {"musicbrainz.org": 1.05, "api.deezer.com": 0.12, "itunes.apple.com": 3.1,
                "www.wikidata.org": 0.2, "api.listenbrainz.org": 0.3, "labs.api.listenbrainz.org": 0.3}
NAME_OK, TITLE_OK = 0.85, 0.8
NON_WP = {"commonswiki", "specieswiki", "metawiki", "mediawikiwiki", "wikidatawiki", "sourceswiki",
          "outreachwiki", "wikimaniawiki", "incubatorwiki", "foundationwiki", "wikifunctionswiki"}
PA = "primary_artist (heuristic)"
FAILS = []


class Http:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("create table if not exists r (k text primary key, status int, body text, ts text)")
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.last = {}

    def get(self, url, params=None, body=None):
        """GET (or POST when body is given), cached. Returns parsed JSON or None."""
        k = json.dumps([url, params, body], sort_keys=True, ensure_ascii=False)
        row = self.db.execute("select status, body from r where k=?", (k,)).fetchone()
        if row:
            return json.loads(row[1]) if row[0] == 200 else None
        host = url.split("/")[2]
        for attempt in range(6):
            wait = MIN_INTERVAL.get(host, 0.5) - (time.time() - self.last.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            self.last[host] = time.time()
            try:
                r = (self.s.post(url, json=body, timeout=30) if body is not None
                     else self.s.get(url, params=params, timeout=30))
            except requests.RequestException:
                time.sleep(2 ** attempt)
                continue
            quota = host == "api.deezer.com" and re.search(r'"code":\s*4\b', r.text)
            if r.status_code in (403, 429, 500, 502, 503, 504) or quota:
                time.sleep(2 ** attempt + 1)
                continue
            if r.status_code in (200, 400, 404):
                self.db.execute("insert or replace into r values (?,?,?,?)",
                                (k, r.status_code, r.text, dt.datetime.now(dt.timezone.utc).isoformat()))
                self.db.commit()
                return r.json() if r.status_code == 200 else None
            break
        FAILS.append(f"{url} {params or body}")  # not cached: a re-run retries it
        return None


def norm(s):
    s = unicodedata.normalize("NFKC", str(s)).casefold()
    s = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", s)
    s = re.sub(r"\b(feat|ft|featuring)\b\.?.*", "", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return " ".join(s.split())


def sim(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if (a in b or b in a) and min(len(a), len(b)) >= 4:
        return 0.9
    return SequenceMatcher(None, a, b).ratio()


def lq(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def mb_track(h, names, title):
    res = h.get("https://musicbrainz.org/ws/2/recording",
                {"query": f"recording:{lq(title)} AND artist:{lq(names[0])}", "fmt": "json", "limit": 10})
    best = None
    for rec in (res or {}).get("recordings", []):
        ts = sim(title, rec["title"])
        acs = rec.get("artist-credit", [])
        cred = lambda n, ac: max(sim(n, ac.get("name", "")), sim(n, ac["artist"]["name"]))
        if ts < TITLE_OK or not any(cred(n, ac) >= NAME_OK for n in names for ac in acs):
            continue
        # the artist ID comes only from the credit matching the primary artist, never a co-credit
        ps, pa = max(((cred(names[0], ac), ac["artist"]) for ac in acs), key=lambda p: p[0])
        if best is None or ts + ps > best[0]:
            best = (ts + ps, rec, pa if ps >= NAME_OK else None)
    if best:
        _, rec, a = best
        d = dict(mb_recording=rec["id"], mb_rec_tags="; ".join(t["name"] for t in rec.get("tags", [])),
                 mb_first_release=rec.get("first-release-date"),
                 match_method="mb_title" if a else "mb_title_primary_not_credited")
        if a:
            d.update(mb_artist=a["id"], mb_artist_name=a["name"])
        return d


def mb_isrc(h, isrc, names):
    res = h.get(f"https://musicbrainz.org/ws/2/isrc/{isrc}", {"fmt": "json", "inc": "artist-credits+tags"})
    recs = (res or {}).get("recordings", [])
    if not recs or not recs[0].get("artist-credit"):
        return None
    rec, acs = recs[0], recs[0]["artist-credit"]
    s, ac = max(((max(sim(n, x.get("name", "")) for n in names), x) for x in acs), key=lambda p: p[0])
    d = dict(mb_recording=rec["id"], mb_rec_tags="; ".join(t["name"] for t in rec.get("tags", [])),
             mb_first_release=rec.get("first-release-date"), match_method="isrc" if s >= NAME_OK else "isrc_name_differs")
    if s >= NAME_OK:
        d.update(mb_artist=ac["artist"]["id"], mb_artist_name=ac["artist"]["name"])
    else:  # e.g. Latin name in the corpus, Arabic in MB: a candidate for manual review, not an ID
        d["mb_isrc_candidate"] = " / ".join(f"{x['artist']['name']} {x['artist']['id']}" for x in acs)
    return d


def dz_track(h, names, title):
    for qq in (f"artist:{lq(names[0])} track:{lq(title)}", f"{names[0]} {title}"):
        for t in (h.get("https://api.deezer.com/search", {"q": qq, "limit": 10}) or {}).get("data", []):
            ts = max(sim(title, t["title"]), sim(title, t.get("title_short", "")))
            if ts >= TITLE_OK and max(sim(n, t["artist"]["name"]) for n in names) >= NAME_OK:
                d = h.get(f"https://api.deezer.com/track/{t['id']}") or {}
                out = dict(dz_track=t["id"], dz_isrc=d.get("isrc"), dz_preview="Y" if t.get("preview") else "N",
                           dz_rank=t.get("rank"))
                if sim(names[0], t["artist"]["name"]) >= NAME_OK:  # primary artist, not a co-credit
                    out.update(dz_artist=t["artist"]["id"], dz_artist_name=t["artist"]["name"])
                return out


def dz_artist_by_overlap(h, mbid, names, titles):
    """Deezer artist for an MB-confirmed artist none of whose corpus tracks matched on Deezer:
    accept a same-name Deezer artist whose top tracks share >=2 titles with the MB recordings."""
    recs = (h.get("https://musicbrainz.org/ws/2/recording",
                  {"artist": mbid, "limit": 100, "fmt": "json"}) or {}).get("recordings", [])
    known = set(titles) | {r["title"] for r in recs}
    need = min(2, len(known))
    for name in dict.fromkeys(n for n in names if n):
        for c in (h.get("https://api.deezer.com/search/artist", {"q": name, "limit": 5}) or {}).get("data", []):
            if sim(name, c["name"]) < NAME_OK:
                continue
            top = (h.get(f"https://api.deezer.com/artist/{c['id']}/top", {"limit": 100}) or {}).get("data", [])
            if sum(any(sim(k, x["title"]) >= TITLE_OK for x in top) for k in known) >= need:
                return c["id"]


def itunes_track(h, names, title):
    res = h.get("https://itunes.apple.com/search", {"term": f"{names[0]} {title}", "entity": "song", "limit": 10})
    for t in (res or {}).get("results", []):
        if sim(title, t.get("trackName", "")) >= TITLE_OK and max(sim(n, t.get("artistName", "")) for n in names) >= NAME_OK:
            return dict(itunes_id=t["trackId"], itunes_preview="Y" if t.get("previewUrl") else "N")


def mb_candidates(h, name):
    res = h.get("https://musicbrainz.org/ws/2/artist",
                {"query": f"artist:{lq(name)} OR alias:{lq(name)}", "fmt": "json", "limit": 5})
    out = []
    for a in (res or {}).get("artists", []):
        s = max([sim(name, a["name"])] + [sim(name, x["name"]) for x in a.get("aliases", [])])
        if s >= NAME_OK:
            out.append(f"{a['name']} [{a.get('country') or '?'}; {a.get('disambiguation', '')}] {a['id']}")
    return out


def mb_artist(h, mbid):
    a = h.get(f"https://musicbrainz.org/ws/2/artist/{mbid}",
              {"fmt": "json", "inc": "aliases+tags+genres+url-rels+artist-rels+label-rels+event-rels"}) or {}
    rels = a.get("relations", [])
    tags = sorted(a.get("tags", []), key=lambda t: -t["count"])
    row = dict(mb_name=a.get("name"), mb_type=a.get("type"), mb_country=a.get("country"),
               mb_area=(a.get("area") or {}).get("name"), mb_begin=(a.get("life-span") or {}).get("begin"),
               mb_n_tags=len(tags), mb_tags="; ".join(f"{t['name']}:{t['count']}" for t in tags),
               mb_n_genres=len(a.get("genres", [])), mb_genres="; ".join(g["name"] for g in a.get("genres", [])),
               mb_n_aliases=len(a.get("aliases", [])), mb_n_rels=len(rels))
    row.update({f"mb_rels_{k}": v for k, v in Counter(r["target-type"] for r in rels).items()})
    row["wikidata_qid"] = next((r["url"]["resource"].rstrip("/").rsplit("/", 1)[-1]
                                for r in rels if r.get("type") == "wikidata"), None)
    return row


def wikidata(h, qids):
    out = {}
    for i in range(0, len(qids), 50):
        res = h.get("https://www.wikidata.org/w/api.php", {"action": "wbgetentities", "ids": "|".join(qids[i:i + 50]),
                                                             "props": "claims|sitelinks", "format": "json"})
        for qid, e in (res or {}).get("entities", {}).items():
            claims = e.get("claims", {})
            wiki = sorted(k[:-4] for k in e.get("sitelinks", {}) if k.endswith("wiki") and k not in NON_WP)
            genres = [c["mainsnak"].get("datavalue", {}).get("value", {}).get("id") for c in claims.get("P136", [])]
            out[qid] = dict(wd_n_statements=sum(len(v) for v in claims.values()), wd_n_properties=len(claims),
                            wp_n_languages=len(wiki), wp_languages=",".join(wiki), wd_genres=[g for g in genres if g])
    gids = sorted({g for v in out.values() for g in v["wd_genres"]})
    glab = {}
    for i in range(0, len(gids), 50):
        res = h.get("https://www.wikidata.org/w/api.php", {"action": "wbgetentities", "ids": "|".join(gids[i:i + 50]),
                                                             "props": "labels", "format": "json"})
        for gid, e in (res or {}).get("entities", {}).items():
            labels = e.get("labels", {})
            glab[gid] = ((labels.get("en") or next(iter(labels.values()), {"value": gid}))["value"], len(labels))
    for v in out.values():
        v["wd_genres"] = "; ".join(f"{glab.get(g, (g, 0))[0]} ({g}, {glab.get(g, (g, 0))[1]} langs)" for g in v["wd_genres"])
    return out


def lb_popularity(h, kind, ids):
    out = {}
    for i in range(0, len(ids), 50):
        for p in h.get(f"https://api.listenbrainz.org/1/popularity/{kind}", body={f"{kind}_mbids": ids[i:i + 50]}) or []:
            out[p.get(f"{kind}_mbid")] = (p.get("total_listen_count"), p.get("total_user_count"))
    return out


def log(*a):
    print(dt.datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, help="first N tracks per case (test run)")
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    h = Http(OUT / "cache.sqlite")
    started = dt.datetime.now(dt.timezone.utc)

    tracks = pd.read_csv(HERE / "capstone_corpus_stage1_tracks.csv")
    tracks["track_row"] = tracks.index + 2  # row number on the Tracks / Fill_in_manual sheets
    if args.sample:
        tracks = tracks.groupby("case").head(args.sample)
    tracks["akey"] = tracks[PA].map(norm)

    # 1. tracks: MB title match, Deezer, ISRC fallback, iTunes fallback
    rows = []
    for n, (primary, credit, title) in enumerate(tracks[[PA, "artist_credit", "track"]].itertuples(index=False, name=None), 1):
        names = [primary, credit]
        r = mb_track(h, names, title) or {}
        dz = dz_track(h, names, title) or {}
        r.update(dz)
        if "mb_recording" not in r and dz.get("dz_isrc"):
            r.update(mb_isrc(h, dz["dz_isrc"], [primary]) or {})
        if dz.get("dz_preview") != "Y":
            r.update(itunes_track(h, names, title) or {})
        rows.append(r)
        if n % 25 == 0:
            log(f"tracks {n}/{len(tracks)}  mb={sum('mb_artist' in x for x in rows)}  dz={sum('dz_track' in x for x in rows)}")
    tracks = pd.concat([tracks.reset_index(drop=True), pd.DataFrame(rows)], axis=1)
    for c in ("dz_track", "dz_artist", "dz_rank", "itunes_id"):
        if c in tracks:
            tracks[c] = tracks[c].astype("Int64")

    # 2. artists: aggregate track evidence, candidates for the unmatched
    arts = []
    for (case, akey), g in tracks.groupby(["case", "akey"], sort=False):
        mbids = Counter(g["mb_artist"].dropna()) if "mb_artist" in g else Counter()
        dzs = Counter(g["dz_artist"].dropna()) if "dz_artist" in g else Counter()
        methods = set(g.get("match_method", pd.Series(dtype=object)).dropna())
        a = dict(case=case, artist=g[PA].iloc[0], akey=akey, n_tracks=len(g),
                 n_tracks_mb=int(g.get("mb_artist", pd.Series(dtype=object)).notna().sum()),
                 n_tracks_dz=int(g.get("dz_track", pd.Series(dtype=object)).notna().sum()),
                 mb_artist=mbids.most_common(1)[0][0] if mbids else None, mb_conflict=len(mbids) > 1,
                 dz_artist=dzs.most_common(1)[0][0] if dzs else None, dz_conflict=len(dzs) > 1,
                 dz_artist_method="track" if dzs else None)
        if "mb_title" in methods:
            a["status"] = "confirmed_title"
        elif "isrc" in methods:
            a["status"] = "confirmed_isrc"
        elif "isrc_name_differs" in methods:
            a["status"] = "isrc_name_differs"
            a["mb_candidates"] = " | ".join(g["mb_isrc_candidate"].dropna().unique())
        else:
            cands = mb_candidates(h, a["artist"])
            a["status"] = "candidate_unconfirmed" if cands else "not_found"
            a["mb_candidates"] = " | ".join(cands)
        arts.append(a)
    arts = pd.DataFrame(arts)
    log("artists", len(arts), arts.status.value_counts().to_dict())

    # 3. MB artist detail, Wikidata, Deezer artist, ListenBrainz
    mb_ids = sorted(arts.mb_artist.dropna().unique())
    detail = {m: mb_artist(h, m) for m in mb_ids}
    log("mb artist detail done")
    for i, a in arts[arts.mb_artist.notna() & arts.dz_artist.isna()].iterrows():
        titles = tracks.loc[(tracks.case == a.case) & (tracks.akey == a.akey), "track"].tolist()
        dz = dz_artist_by_overlap(h, a.mb_artist, [a.artist, detail[a.mb_artist].get("mb_name")], titles)
        if dz:
            arts.loc[i, ["dz_artist", "dz_artist_method"]] = [dz, "artist_title_overlap"]
    log("deezer artist fallback done", arts.dz_artist_method.value_counts().to_dict())
    wd = wikidata(h, sorted({d["wikidata_qid"] for d in detail.values() if d.get("wikidata_qid")}))
    dz_art = {d: h.get(f"https://api.deezer.com/artist/{int(d)}") or {} for d in arts.dz_artist.dropna().unique()}
    lb_art = lb_popularity(h, "artist", mb_ids)
    rec_ids = sorted(tracks.get("mb_recording", pd.Series(dtype=object)).dropna().unique())
    lb_rec = lb_popularity(h, "recording", rec_ids)
    sim_rows = []
    for m in mb_ids:
        res = h.get("https://labs.api.listenbrainz.org/similar-artists/json", {"artist_mbids": m, "algorithm": LB_ALGO})
        for rank, x in enumerate([x for x in (res or []) if isinstance(x, dict) and "artist_mbid" in x], 1):
            sim_rows.append(dict(artist_mbid=m, rank=rank, similar_mbid=x["artist_mbid"],
                                 similar_name=x.get("name"), score=x.get("score")))
    sim_df = pd.DataFrame(sim_rows, columns=["artist_mbid", "rank", "similar_mbid", "similar_name", "score"])
    n_sim = sim_df.groupby("artist_mbid").size()
    log("wikidata/deezer/listenbrainz done")

    def enrich_artist(a):
        d = dict(detail.get(a.mb_artist, {}))
        d.update(wd.get(d.get("wikidata_qid"), {}))
        if pd.notna(a.dz_artist):
            z = dz_art.get(a.dz_artist, {})
            d.update(dz_name=z.get("name"), dz_fans=z.get("nb_fan"), dz_n_albums=z.get("nb_album"))
        if pd.notna(a.mb_artist):
            listens, users = lb_art.get(a.mb_artist, (None, None))
            d.update(lb_listens=listens, lb_users=users, lb_n_similar=int(n_sim.get(a.mb_artist, 0)))
        return pd.Series(d, dtype=object)
    arts = pd.concat([arts, arts.apply(enrich_artist, axis=1)], axis=1)
    if "mb_recording" in tracks:
        tracks["lb_rec_listens"] = tracks.mb_recording.map(lambda m: lb_rec.get(m, (None, None))[0])

    # 4. review sheet: everything without title evidence, or with conflicting IDs
    reasons = pd.DataFrame({"no MB track evidence": ~arts.status.isin(["confirmed_title", "confirmed_isrc"]),
                            "MB id conflict": arts.mb_conflict, "Deezer id conflict": arts.dz_conflict,
                            "no Deezer artist": arts.dz_artist.isna()})
    arts["review_reason"] = reasons.apply(lambda r: "; ".join(k for k, v in r.items() if v), axis=1)
    titles = tracks.groupby(["case", "akey"]).track.apply(lambda s: " | ".join(s.astype(str).head(3)))
    review = arts[arts.review_reason != ""].copy()
    review["sample_titles"] = [titles.get((c, k)) for c, k in zip(review.case, review.akey)]
    review = review[[c for c in ["case", "artist", "status", "review_reason", "mb_artist", "mb_name",
                                 "mb_country", "mb_candidates", "dz_artist", "dz_name", "dz_fans", "sample_titles"]
                     if c in review]]

    # 5. Fill_in_manual template, filled from the above
    fill = pd.read_excel(HERE / "capstone_corpus_stage1_interim.xlsx", sheet_name="Fill_in_manual")
    if args.sample:
        fill = fill[fill.track_row.isin(tracks.track_row)]
    t = tracks.set_index("track_row")
    a = arts.set_index(["case", "akey"])
    fill = fill.astype(object)
    for i, f in fill.iterrows():
        tr = t.loc[f.track_row]
        assert tr.artist_credit == f.artist_credit, f"row mismatch at {f.track_row}"
        ar = a.loc[(tr.case, tr.akey)]
        g = lambda s, k: s.get(k) if pd.notna(s.get(k)) else None
        tagstr = "; ".join(x for x in [f"artist: {g(ar, 'mb_tags')}" if g(ar, "mb_tags") else "",
                                       f"recording: {g(tr, 'mb_rec_tags')}" if g(tr, "mb_rec_tags") else ""] if x)
        fill.loc[i, ["mbid_artist", "mbid_recording", "mb_tags_genres", "mb_relations_count", "wikidata_qid",
                     "wikipedia_languages", "deezer_id", "itunes_id", "itunes_preview_found(Y/N)",
                     "lastfm_or_deezer_popularity"]] = [
            g(ar, "mb_artist"), g(tr, "mb_recording"), tagstr or None, g(ar, "mb_n_rels"), g(ar, "wikidata_qid"),
            g(ar, "wp_n_languages"), g(tr, "dz_track"), g(tr, "itunes_id"), g(tr, "itunes_preview"), g(ar, "dz_fans")]
        if pd.isna(f.notes):
            fill.loc[i, "notes"] = f"auto {started:%Y-%m-%d}: artist {ar.status}; track {g(tr, 'match_method') or 'no MB match'}"

    meta = pd.DataFrame([
        ("run_started_utc", started.isoformat()), ("run_finished_utc", dt.datetime.now(dt.timezone.utc).isoformat()),
        ("sample", args.sample or "full"), ("user_agent", UA), ("listenbrainz_similar_algorithm", LB_ALGO),
        ("thresholds", f"name>={NAME_OK}, title>={TITLE_OK} (difflib ratio on normalised strings)"),
        ("mb_relations_counted", "url, artist, label, event (artist lookup inc=url-rels+artist-rels+label-rels+event-rels)"),
        ("wikipedia_languages", "Wikidata sitelinks ending in 'wiki', minus non-Wikipedia projects"),
        ("itunes", "queried only for tracks without a Deezer preview"),
        ("deezer_artist", "from matched tracks; else same-name Deezer artist whose top tracks share >=2 titles with MB recordings"),
        ("deezer_preview", "Y/N only; preview URLs expire and are not stored"),
        ("failed_requests_not_cached", len(FAILS)),
    ], columns=["key", "value"])

    path = OUT / (f"enriched_sample.xlsx" if args.sample else f"enriched_{started:%Y-%m-%d}.xlsx")
    with pd.ExcelWriter(path) as xw:
        for name, d in [("Tracks", tracks.drop(columns="akey")), ("Artists", arts.drop(columns="akey")),
                        ("Match_review", review), ("LB_similar", sim_df), ("Fill_in_manual", fill), ("Run_meta", meta)]:
            d.to_excel(xw, sheet_name=name, index=False)
    (OUT / "failed_requests.txt").write_text("\n".join(FAILS), encoding="utf-8")
    log("wrote", path, "| failed requests:", len(FAILS))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
