"""Descriptive statistics of the stage-2 corpus (capstone_corpus_stage2.xlsx), printed per case."""
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent


def main():
    x = pd.read_excel(HERE / "capstone_corpus_stage2.xlsx", sheet_name=None)
    C, G, E = x["Tracks"], x["Artists_goldA"], x["Artists_excluded"]
    for d in (C, G, E):
        d["c"] = d.case.str.split(" ").str[0]
    pd.set_option("display.width", 220)

    print("== tracks")
    C["origin_kind"] = C.origin.str.split(":").str[0]
    C["any_preview"] = (C.dz_preview == "Y") | (C.itunes_preview == "Y")
    t = C.groupby("c").agg(tracks=("track", "size"), artists=("artist", "nunique"),
                           year_min=("year", "min"), year_med=("year", "median"), year_max=("year", "max"),
                           year_missing=("year", lambda s: int(s.isna().sum())),
                           dz_preview=("dz_preview", lambda s: int((s == "Y").sum())),
                           itunes_preview=("itunes_preview", lambda s: int((s == "Y").sum())),
                           any_preview=("any_preview", "sum"))
    print(t.to_string())
    print(pd.crosstab(C.c, C.origin_kind, margins=True).to_string())
    per = C.groupby(["c", "artist"]).size().groupby("c").describe()[["min", "25%", "50%", "75%", "max"]]
    print("tracks per artist:\n" + per.to_string())
    top = C.groupby(["c", "artist"]).size().groupby("c", group_keys=False).nlargest(3)
    print("top artists by tracks:\n" + top.to_string())

    print("\n== gold-A artists: density and popularity (medians; shares in %)")
    num = lambda s: pd.to_numeric(s, errors="coerce")
    g = G.groupby("c").agg(
        artists=("artist", "size"),
        mbid_pct=("mb_artist", lambda s: round(100 * s.notna().mean())),
        mb_tags_med=("mb_n_tags", lambda s: num(s).fillna(0).median()),
        mb_genre_pct=("mb_n_genres", lambda s: round(100 * (num(s).fillna(0) > 0).mean())),
        mb_rels_med=("mb_n_rels", lambda s: num(s).fillna(0).median()),
        wikidata_pct=("wikidata_qid", lambda s: round(100 * s.notna().mean())),
        wp_langs_med=("wp_n_languages", lambda s: num(s).fillna(0).median()),
        lb_sim_pct=("lb_n_similar", lambda s: round(100 * (num(s).fillna(0) > 0).mean())),
        dz_artist_pct=("dz_artist", lambda s: round(100 * s.notna().mean())),
        dz_fans_med=("dz_fans", lambda s: num(s).median()),
        lb_users_med=("lb_users", lambda s: num(s).median()),
        keys_med=("n_keys", "median"))
    print(g.to_string())
    kinds = G.assign(k=G["keys"].str.split(" \\| ")).explode("k")
    kinds["kind"] = kinds.k.str.split(":").str[0].where(kinds.k.str.contains(":"), kinds.k)
    print(pd.crosstab(kinds.c, kinds.kind).to_string())

    print("\n== excluded (fewer than 2 independent sources)")
    print(pd.crosstab(E.c, E.n_keys).to_string())
    s = pd.concat([G.assign(s=G.seed_method + "/in"), E.assign(s=E.seed_method + "/out")], ignore_index=True)
    print("seed group x outcome:\n" + pd.crosstab(s.c, s.s).to_string())


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
