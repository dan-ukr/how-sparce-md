"""Gold-A review: an artist is in the corpus if >=2 independent sources connect them to the scene.

Independent = different institution / publisher. Keys below collapse sources that report the same
fact (e.g. Cxema line-ups, Cxema NTS shows and press about Cxema events are one key: "Cxema").
Excluded as gold evidence: MB tags and Wikidata genre statements (plan: gold is independent of
labels), Reddit (weak), tag-seeded sources, the artist's own pages/albums, playlists or events
not tied to the scene.

Inputs: enrichment/enriched_<date>.xlsx, capstone_corpus_stage1_interim.xlsx,
enrichment/goldA_manual_evidence.csv (web press found by hand; optional).
Output: enrichment/goldA_evidence.csv
"""
import re, sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from enrich import Http, MIN_INTERVAL, OUT, norm, log  # noqa: E402

MIN_INTERVAL["api.discogs.com"] = 2.6  # 25 req/min without a token
for w in ("en", "es", "ar", "uk", "ru", "pt", "fr", "de"):
    MIN_INTERVAL[f"{w}.wikipedia.org"] = 0.2
ENR = sorted(OUT.glob("enriched_20*.xlsx"))[-1]
INTERIM = Path(__file__).parent / "capstone_corpus_stage1_interim.xlsx"
MANUAL = OUT / "goldA_manual_evidence.csv"

# stage-1 source_id -> independence key (None = not gold evidence)
SOURCE_KEY = {
    "S01": "comp:Together for Ukraine", "S02": "comp:From Ukraine, For Ukraine", "S03": "label:Standard Deviation",
    "S04": "label:Standard Deviation", "S13": "label:Standard Deviation", "S05": "label:Pure Tool",
    "S07": "label:Mystictrax", "S11": "label:NECHTO", "S14": "comp:BITE Shedding Skin",
    **{s: "Cxema" for s in ["S06", "S20", "S21", "S22", "S23", "S24", "S25", "S26", "S27", "S28"]},
    "A01": None, "A02": None,  # Scorpion Kings' own albums
    "A03": "press:Gray Area", "A04": "press:Music In Africa", "A05": "press:Music In Africa",
    "A06": "press:Music In Africa", "A09": "press:Music In Africa", "A07": "press:OkayAfrica",
    "A08": None,  # Serato DJ playlist not named for the genre
    "N01": "dj:Serato 'neoperreo vol 1'", "N02": None, "N03": None, "N06": None,  # artist's own pages
    "N04": "press:Billboard", "N05": "press:Bandcamp Daily", "N07": "Wikipedia", "N08": "press:Hypebot/Chartmetric",
    "N09": "press:Hypebot/Chartmetric", "N10": "press:Grammy.com", "N11": None, "N12": "editorial:SoundCloud Stories",
    "N13": "Neoperreo collective", "N14": "Neoperreo collective", "N15": None, "N16": "press:Remezcla",
    "M01": "dj:Ma3azef (NTS show)", "M02": "dj:Mahraganat Podcast (wah6ah)", "M03": "label:100Copies",
    "M04": None, "M05": "Wikipedia", "M06": None, "M07": "press:The Quietus", "M08": "dj:Cairo Liberation Front",
    "M09": "Wikipedia", "M10": None, "M11": "press:Al Jazeera", "M12": "curator:Amina Kaabi (Apple Music)",
    "U01": "Anokha", "U02": None, "U03": None, "U04": None, "U05": "comp:Rough Guide to the Asian Underground",
    "U06": None, "U07": "Wikipedia", "U08": "Swaraj", "U09": "label:Bheja Fry", "U10": "label:Outcaste",
    "U11": "label:Outcaste",
}
TAG_SEEDED = {"N15", "U06"}

# (case prefix, substring of event name, key); events not listed are not gold evidence
LINEUP_KEY = [
    ("Kyiv", "Boiler Room Kyiv 2019", "event:Boiler Room Kyiv 2019"), ("Kyiv", "Cxema", "Cxema"),
    ("Kyiv", "CXEMA", "Cxema"), ("Kyiv", "Calvert", "press:Calvert Journal"), ("Kyiv", "Strichka", "Strichka festival"),
    ("Amapiano", "Amapiano Fest, Cape Town", "event:Amapiano Fest Cape Town"),
    ("Amapiano", "Amapiano Festival, Kempton", "event:Amapiano Festival Kempton Park"),
    ("Amapiano", "Amapiano Music Festival, Windhoek", "event:Amapiano Music Festival Windhoek"),
    ("Neoperreo", "NEOPERREO", "Neoperreo collective"), ("Neoperreo", "Neoperreo", "Neoperreo collective"),
    ("Mahraganat", "Cairo Calling", "label:100Copies"), ("Mahraganat", "Electro Chaabi (film", "film:Electro Chaabi"),
    ("Mahraganat", "Cairo Jazz Club", "event:Cairo Jazz Club electro-shaabi night"),
    ("Asian", "Rohan Rakhit", "event:Brick Lane Jazz Festival"), ("Asian", "New wave collectives", "press:Global Indian"),
]

# scene labels fixed from the stage-1 sources and the Vocab sheet (not from tags)
SCENE_LABELS = {
    "Kyiv": [(r"standard deviation", "label:Standard Deviation"), (r"pure tool", "label:Pure Tool"),
             (r"mystictrax", "label:Mystictrax"), (r"nechto", "label:NECHTO")],
    "Neoperreo": [(r"terror negro", "label:Terror Negro")],
    "Mahraganat": [(r"100 ?copies", "label:100Copies")],
    "Asian": [(r"outcaste", "label:Outcaste"), (r"^nation( records?)?$", "label:Nation"), (r"^omni records?$", "label:Omni"),
              (r"^nasha( records?)?$", "label:Nasha"), (r"bheja fry", "label:Bheja Fry"),
              (r"hundred colou?rs", "label:Hundred Colours"), (r"anokha", "Anokha")],
}

WIKI_KW = {
    "Kyiv": (r"kyiv|kiev|київ|києв|киев|ukrain|україн|украин", r"cxema|схема|techno|техно|rave|рейв|club|клуб"),
    "Amapiano": (r"amapiano|амапіано|амапиано", None),
    "Neoperreo": (r"neo[- ]?perreo", None),
    "Mahraganat": (r"mahragan|electro[- ]?c?h?aa?bi|electro[- ]?sha+bi|مهرجان", None),
    "Asian": (r"asian underground|asian massive|british asian|outcaste|anokha|nation records", None),
}


# one act under several stage-1 spellings, or a remix credited to the original artist instead of
# the scene remixer: (case prefix, stage-1 primary artist) -> canonical artist
ALIASES = {
    ("Mahraganat", "El Madfaageya"): "El Madfaagya", ("Mahraganat", "Madf3gya"): "El Madfaagya",
    ("Mahraganat", "Oka W Ortega W Shehta"): "Oka W Ortega",
    ("Asian", "Craig David (Rishi Rich remix)"): "Rishi Rich", ("Asian", "Trilok Gurtu (T.J. Rehmi remix)"): "TJ Rehmi",
    ("Asian", "Wayward Soul (Juttla mix)"): "Juttla", ("Asian", "Banco de Gaia (Juttla mix)"): "Juttla",
    ("Asian", "Bhangra Knights vs Husan"): "Bhangra Knights", ("Asian", "Mahatma T"): "Talvin Singh", ("Asian", "T.J. Rehmi"): "TJ Rehmi", ("Mahraganat", "Figo"): "DJ Figo",
    ("Mahraganat", "الدخلاوية"): "El Dakhlaweya", ("Mahraganat", "Team Amr Haha"): "Amr Haha",  # aliases
}

WIKI_SEARCH_LANGS = {"Kyiv": ["en", "uk", "ru"], "Amapiano": ["en"], "Neoperreo": ["en", "es"],
                     "Mahraganat": ["en", "ar"], "Asian": ["en"]}
WIKI_SEARCH_WORD = {"Kyiv": "techno", "Amapiano": "amapiano", "Neoperreo": "perreo",
                    "Mahraganat": "mahraganat OR مهرجانات", "Asian": "Asian Underground"}


def cprefix(case):
    return next(p for p in ("Kyiv", "Amapiano", "Neoperreo", "Mahraganat", "Asian") if case.startswith(p))


def mentions(text, name):
    n = norm(name)
    return len(n) >= 2 and re.search(rf"(?<!\w){re.escape(n)}(?!\w)", norm(text)) is not None


def labels_for(h, mbid, discogs_id, dz_id):
    names = set()
    if isinstance(mbid, str):
        for by in ("artist", "track_artist"):
            res = h.get("https://musicbrainz.org/ws/2/release", {by: mbid, "inc": "labels", "limit": 100, "fmt": "json"})
            for r in (res or {}).get("releases", []):
                names |= {li["label"]["name"] for li in r.get("label-info", []) if li.get("label")}
    if discogs_id:
        res = h.get(f"https://api.discogs.com/artists/{discogs_id}/releases", {"per_page": 100})
        names |= {r["label"] for r in (res or {}).get("releases", []) if r.get("label")}
    if pd.notna(dz_id):
        for al in (h.get(f"https://api.deezer.com/artist/{int(dz_id)}/albums", {"limit": 100}) or {}).get("data", []):
            names.add((h.get(f"https://api.deezer.com/album/{al['id']}") or {}).get("label") or "")
    return {n for n in names if n}


def main():
    h = Http(OUT / "cache.sqlite")
    A = pd.read_excel(ENR, sheet_name="Artists")
    T = pd.read_excel(ENR, sheet_name="Tracks")
    lineups = pd.read_excel(INTERIM, sheet_name="Lineups_evidence")
    cxema = pd.read_excel(INTERIM, sheet_name="Cxema_lineups_UA")
    manual = pd.read_csv(MANUAL) if MANUAL.exists() else pd.DataFrame(columns=["case", "artist", "key", "url"])
    src_url = pd.read_excel(INTERIM, sheet_name="Sources").set_index("source_id").url

    # Discogs artist IDs and Wikipedia sitelinks come via MB url-rels / Wikidata
    def discogs_id(mbid):
        a = h.get(f"https://musicbrainz.org/ws/2/artist/{mbid}",
                  {"fmt": "json", "inc": "aliases+tags+genres+url-rels+artist-rels+label-rels+event-rels"}) or {}
        for r in a.get("relations", []):
            m = re.search(r"discogs\.com/artist/(\d+)", (r.get("url") or {}).get("resource", ""))
            if m:
                return m.group(1)
    qids = sorted(A.wikidata_qid.dropna().unique())
    sitelinks = {}
    for i in range(0, len(qids), 50):
        res = h.get("https://www.wikidata.org/w/api.php", {"action": "wbgetentities", "ids": "|".join(qids[i:i + 50]),
                                                             "props": "sitelinks", "format": "json"})
        for q, e in (res or {}).get("entities", {}).items():
            sitelinks[q] = {k[:-4]: v["title"] for k, v in e.get("sitelinks", {}).items()
                            if k[:-4] in ("en", "es", "ar", "uk", "ru", "pt", "fr", "de") and k.endswith("wiki")}

    rows = []
    for n, a in enumerate(A.itertuples(index=False), 1):
        p = cprefix(a.case)
        ev = []  # (key, detail)
        ct = T[T.case == a.case]
        own = ct[ct[ "primary_artist (heuristic)"].map(norm) == norm(a.artist)]
        seed_ids = set(own.source_id)
        for sid in set(ct[ct.artist_credit.map(lambda c: mentions(c, a.artist))].source_id) | seed_ids:
            for s in str(sid).split(";"):
                s = s.strip()
                if SOURCE_KEY.get(s):
                    ev.append((SOURCE_KEY[s], f"{s} {str(src_url.get(s, ''))[:80]}"))
        for r in lineups[lineups.case.str.startswith(p)].itertuples(index=False):
            key = next((k for c, sub, k in LINEUP_KEY if c == p and sub in r[1]), None)
            if key and mentions(r[2], a.artist):
                ev.append((key, f"line-up: {r[1]}"))
        if p == "Kyiv":
            for r in cxema.itertuples(index=False):
                if mentions(r[1], a.artist):
                    ev.append(("Cxema", f"line-up: {r[0]}"))
        if p in SCENE_LABELS and (isinstance(a.mb_artist, str) or pd.notna(a.dz_artist)):
            for lab in labels_for(h, a.mb_artist, discogs_id(a.mb_artist) if isinstance(a.mb_artist, str) else None,
                                  a.dz_artist):
                for rx, key in SCENE_LABELS[p]:
                    if re.search(rx, lab.casefold().strip()):
                        ev.append((key, f"release on {lab}"))
        if isinstance(a.wikidata_qid, str):
            kw, kw2 = WIKI_KW[p]
            for lang, title in sitelinks.get(a.wikidata_qid, {}).items():
                res = h.get(f"https://{lang}.wikipedia.org/w/api.php", {"action": "query", "prop": "extracts",
                            "explaintext": 1, "titles": title, "format": "json", "redirects": 1})
                text = " ".join(pg.get("extract", "") for pg in (res or {}).get("query", {}).get("pages", {}).values())
                if re.search(kw, text, re.I) and (kw2 is None or re.search(kw2, text, re.I)):
                    ev.append(("Wikipedia", f"{lang}.wikipedia: {title}"))
        if "Wikipedia" not in {k for k, _ in ev}:  # any Wikipedia page naming both the artist and the scene
            kw, kw2 = WIKI_KW[p]
            names = [a.artist] + ([a.mb_name] if isinstance(a.mb_name, str) and a.mb_name != a.artist else [])
            for lang in WIKI_SEARCH_LANGS[p]:
                for name in names:
                    res = h.get(f"https://{lang}.wikipedia.org/w/api.php", {"action": "query", "list": "search",
                                "srsearch": f'"{name}" {WIKI_SEARCH_WORD[p]}', "srlimit": 5, "format": "json"})
                    for hit in (res or {}).get("query", {}).get("search", []):
                        ex = h.get(f"https://{lang}.wikipedia.org/w/api.php", {"action": "query", "prop": "extracts",
                                   "explaintext": 1, "titles": hit["title"], "format": "json", "redirects": 1})
                        text = " ".join(pg.get("extract", "") for pg in (ex or {}).get("query", {}).get("pages", {}).values())
                        if mentions(text, name) and re.search(kw, text, re.I) and (kw2 is None or re.search(kw2, text, re.I)):
                            ev.append(("Wikipedia", f"{lang}.wikipedia: {hit['title']} (mentions artist)"))
                            break
        indep_seed = any(s.strip() not in TAG_SEEDED for sid in seed_ids for s in str(sid).split(";"))
        rows.append(dict(case=a.case, artist=ALIASES.get((p, a.artist), a.artist), variant=a.artist,
                         mb_artist=a.mb_artist, match_status=a.status, indep_seed=indep_seed, ev=ev))
        if n % 25 == 0:
            log(f"artists {n}/{len(A)}")

    merged = []
    for (case, artist), g in pd.DataFrame(rows).groupby(["case", "artist"], sort=False):
        ev = [e for es in g.ev for e in es]
        ev += [(r.key, f"web: {r.url}") for r in
               manual[(manual.case == case) & (manual.artist == artist)].itertuples(index=False)]
        keys = sorted({k for k, _ in ev})
        merged.append(dict(case=case, artist=artist, variants=" | ".join(g.variant),
                           mb_artist=g.mb_artist.dropna().iloc[0] if g.mb_artist.notna().any() else None,
                           match_status=" | ".join(g.match_status.unique()),
                           seed_method="independent" if g.indep_seed.any() else "mb_tag",
                           n_keys=len(keys), goldA=len(keys) >= 2, keys=" | ".join(keys),
                           evidence=" || ".join(sorted({f"{k}: {d}" for k, d in ev}))))
    out = pd.DataFrame(merged)
    out.to_csv(OUT / "goldA_evidence.csv", index=False, encoding="utf-8-sig")
    log("goldA by case:\n" + pd.crosstab(out.case, out.n_keys.clip(upper=2).map({0: "0", 1: "1", 2: ">=2"})).to_string())


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
