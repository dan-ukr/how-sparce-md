"""
For every track it takes up to 5 windows of 30 s (start, 25%, middle, 75%, end)
and computes, per window:
  - MERT-v1 (m-a-p/MERT-v1-95M): time-mean of ALL hidden layers   -> [L, D]
  - LAION-CLAP (music checkpoint): audio embedding                -> [512]
  - Essentia Discogs-EffNet (optional): embedding [1280] + 400 Discogs genre activations

Only the function `load_audio` depends on where the audio comes from.
Deezer: replace the body of `load_audio` with the internal loader.
Locally: the default finds the file in --audio-dir (<track_id>.* or <isrc>.*) or via the `path` column.

Output (in --out):
  tracks/<track_id>.npz   one file per track (its existence is the checkpoint)
  coverage.csv            track_id, found, duration, n_windows, status
  errors.csv              track_id, error
  run_meta.json           versions, parameters, timestamps

IF MODELS ARE NOT PRESENT IN THE ENV, PLEASE DO THE FOLLOWING IN A SEPARATE TERMINAL/PY:
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
pip install transformers librosa soundfile soxr numpy nnAudio
pip install laion-clap
pip install essentia-tensorflow
"""
import argparse, csv, json, os, sys, time, traceback, platform
import numpy as np

WIN_SEC = 30.0
N_WIN = 5
SR = {"mert": 24000, "clap": 48000, "effnet": 16000}


# 1. AUDIO LOADING: NEEDS TO BE MODIFIED BY DEEZER
AUDIO_EXT = (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".opus", ".aac", ".aif", ".aiff")
AUDIO_INDEX = {} 

def index_audio_dir(d):
    idx = {}
    for root, _, files in os.walk(d):
        for fn in sorted(files):
            stem, ext = os.path.splitext(fn)
            if ext.lower() in AUDIO_EXT:
                idx.setdefault(stem.lower(), os.path.join(root, fn))
    return idx


def resolve_path(row):
    """1) `path` column of the CSV (absolute, or relative to --audio-dir);
       2) file in --audio-dir named <track_id>.* or <isrc>.*"""
    p = row.get("path")
    if p:
        if os.path.exists(p):
            return p
        base = row.get("_audio_dir")
        if base and os.path.exists(os.path.join(base, p)):
            return os.path.join(base, p)
    for key in (row.get("track_id"), row.get("isrc")):
        if key and str(key).lower() in AUDIO_INDEX:
            return AUDIO_INDEX[str(key).lower()]
    return None


def load_audio(row):
    """Return (waveform: float32 1-D mono numpy array, sample_rate: int).
    row is a dict with the columns of the input CSV (track_id, isrc, ...).
    Return None if the track is not available.
    Deezer: replace this body with the internal loader."""
    path = resolve_path(row)
    if path is None:
        return None
    import librosa
    y, sr = librosa.load(path, sr=None, mono=True)
    return y.astype(np.float32), int(sr)


# 2. WINDOWS
def window_starts(duration, win=WIN_SEC, n=N_WIN):
    if duration <= win:
        return np.array([0.0])
    return np.linspace(0.0, duration - win, n)


def cut(y, sr, start, win=WIN_SEC):
    a = int(round(start * sr))
    b = a + int(round(win * sr))
    return y[a:b]


def resample(x, sr_in, sr_out):
    if sr_in == sr_out:
        return x
    import librosa
    y = librosa.resample(x, orig_sr=sr_in, target_sr=sr_out, res_type="soxr_hq")
    n = int(round(len(x) * sr_out / sr_in))
    return y[:n]


# 3. MODELS
class Models:
    def __init__(self, args, device):
        import torch
        self.torch = torch
        self.device = device
        self.use_effnet = bool(args.effnet_dir)

        # MERT
        from transformers import AutoModel, Wav2Vec2FeatureExtractor
        self.mert = AutoModel.from_pretrained(args.mert, trust_remote_code=True).to(device).eval()
        self.mert_fe = Wav2Vec2FeatureExtractor.from_pretrained(args.mert, trust_remote_code=True)
        assert self.mert_fe.sampling_rate == SR["mert"], "MERT sampling rate changed"

        # CLAP
        import laion_clap
        self.clap = laion_clap.CLAP_module(enable_fusion=False, amodel="HTSAT-base", device=device)
        self.clap.load_ckpt(args.clap_ckpt)
        self.clap.eval()

        # Essentia Discogs-EffNet
        if self.use_effnet:
            from essentia.standard import TensorflowPredictEffnetDiscogs, TensorflowPredict2D
            d = args.effnet_dir
            self.eff_emb = TensorflowPredictEffnetDiscogs(
                graphFilename=os.path.join(d, "discogs-effnet-bs64-1.pb"), output="PartitionedCall:1")
            self.eff_genre = TensorflowPredict2D(
                graphFilename=os.path.join(d, "genre_discogs400-discogs-effnet-1.pb"),
                input="serving_default_model_Placeholder", output="PartitionedCall:0")

    def mert_embed(self, x24):
        torch = self.torch
        inp = self.mert_fe(x24, sampling_rate=SR["mert"], return_tensors="pt")
        inp = {k: v.to(self.device) for k, v in inp.items()}
        with torch.no_grad():
            out = self.mert(**inp, output_hidden_states=True)
        hs = torch.stack(out.hidden_states, dim=0).squeeze(1)   # [L, T, D]
        return hs.mean(dim=1).float().cpu().numpy()              # [L, D]

    def clap_embed(self, x48):
        with self.torch.no_grad():
            e = self.clap.get_audio_embedding_from_data(x=x48[None, :].astype(np.float32), use_tensor=False)
        return np.asarray(e)[0]                                  # [512]

    def effnet_embed(self, x16):
        emb = self.eff_emb(x16.astype(np.float32))               # [patches, 1280]
        gen = self.eff_genre(emb)                                # [patches, 400]
        return emb.mean(axis=0), gen.mean(axis=0)


# 4. MAIN
def process_track(row, models):
    got = load_audio(row)
    if got is None:
        return None, "audio_not_found"
    y, sr = got
    duration = len(y) / sr
    if duration < 1.0:
        return None, "too_short"
    starts = window_starts(duration)
    mert, clap, emb, gen, rms = [], [], [], [], []
    for s in starts:
        w = cut(y, sr, s)
        rms.append(float(np.sqrt(np.mean(w ** 2))))
        mert.append(models.mert_embed(resample(w, sr, SR["mert"])))
        clap.append(models.clap_embed(resample(w, sr, SR["clap"])))
        if models.use_effnet:
            e, g = models.effnet_embed(resample(w, sr, SR["effnet"]))
            emb.append(e); gen.append(g)
    rec = dict(
        track_id=str(row["track_id"]), isrc=str(row.get("isrc", "")),
        source=str(row.get("source", "")),
        duration=np.float32(duration), starts=starts.astype(np.float32),
        rel_pos=(starts / duration).astype(np.float32), rms=np.array(rms, dtype=np.float32),
        mert=np.stack(mert).astype(np.float16),                     # [W, L, D]
        clap=np.stack(clap).astype(np.float16),                     # [W, 512]
    )
    if models.use_effnet:
        rec["effnet_emb"] = np.stack(emb).astype(np.float16)        # [W, 1280]
        rec["effnet_genre"] = np.stack(gen).astype(np.float16)      # [W, 400]
    return rec, "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="", help="CSV: track_id,isrc,case,artist_id[,path][,source]. "
                    "If omitted, every audio file in --audio-dir is processed (track_id = file name)")
    ap.add_argument("--audio-dir", default="", help="local folder with audio files (searched recursively); "
                    "files are matched to the CSV by <track_id>.* or <isrc>.*, or by the `path` column")
    ap.add_argument("--out", required=True)
    ap.add_argument("--mert", default="m-a-p/MERT-v1-95M", help="HF id or local dir (offline: local dir)")
    ap.add_argument("--clap-ckpt", required=True, help="LAION-CLAP music checkpoint file")
    ap.add_argument("--effnet-dir", default="", help="dir with Essentia Discogs-EffNet .pb models; empty = skip")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    import torch
    torch.manual_seed(args.seed); np.random.seed(args.seed)
    torch.backends.cudnn.deterministic = True
    device = "cuda" if torch.cuda.is_available() else "cpu"

    os.makedirs(os.path.join(args.out, "tracks"), exist_ok=True)
    models = Models(args, device)

    if args.audio_dir:
        AUDIO_INDEX.update(index_audio_dir(args.audio_dir))
        print(f"indexed {len(AUDIO_INDEX)} audio files in {args.audio_dir}", file=sys.stderr)
    if args.input:
        with open(args.input, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    elif args.audio_dir:
        rows = [dict(track_id=stem, isrc="", case="", artist_id="", source="own")
                for stem in sorted(AUDIO_INDEX)]
    else:
        ap.error("give --input and/or --audio-dir")
    for r in rows:
        r["_audio_dir"] = args.audio_dir

    cov_path = os.path.join(args.out, "coverage.csv")
    err_path = os.path.join(args.out, "errors.csv")
    new_cov = not os.path.exists(cov_path)
    cov = open(cov_path, "a", newline="", encoding="utf-8")
    err = open(err_path, "a", newline="", encoding="utf-8")
    cw, ew = csv.writer(cov), csv.writer(err)
    if new_cov:
        cw.writerow(["track_id", "isrc", "case", "found", "duration", "n_windows", "status"])

    t0 = time.time()
    for i, row in enumerate(rows, 1):
        tid = str(row["track_id"])
        outp = os.path.join(args.out, "tracks", f"{tid}.npz")
        if os.path.exists(outp):
            continue    #checkpoint
        try:
            rec, status = process_track(row, models)
            if rec is None:
                cw.writerow([tid, row.get("isrc", ""), row.get("case", ""), 0, "", 0, status])
            else:
                np.savez_compressed(outp + ".tmp.npz", **rec)
                os.replace(outp + ".tmp.npz", outp)
                cw.writerow([tid, row.get("isrc", ""), row.get("case", ""), 1,
                             f"{float(rec['duration']):.1f}", len(rec["starts"]), status])
        except Exception as e:                         
            ew.writerow([tid, repr(e) + " | " + traceback.format_exc().splitlines()[-2]])
            cw.writerow([tid, row.get("isrc", ""), row.get("case", ""), "", "", 0, "error"])
        cov.flush(); err.flush()
        if i % 25 == 0:
            print(f"{i}/{len(rows)}  {time.time()-t0:.0f}s", file=sys.stderr)

    import transformers
    meta = dict(
        finished=time.strftime("%Y-%m-%dT%H:%M:%S"), device=device, seed=args.seed,
        win_sec=WIN_SEC, n_win=N_WIN, sample_rates=SR, mert=args.mert, clap_ckpt=os.path.basename(args.clap_ckpt),
        effnet=bool(args.effnet_dir), python=platform.python_version(),
        torch=torch.__version__, transformers=transformers.__version__, numpy=np.__version__,
        window_rule="starts=linspace(0, max(dur-30,0), 5); dur<=30s -> one window",
    )
    with open(os.path.join(args.out, "run_meta.json"), "w") as f:
        json.dump(meta, f, indent=2)
    print("done", file=sys.stderr)


if __name__ == "__main__":
    main()
