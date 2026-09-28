# OCR comparison, 2026-09-28

The default remains RapidOCR with small PP-OCRv6 detection and recognition models,
using PyTorch on the AMD GPU. Larger models tested here did not improve the
balance of player-name accuracy and speed.

## Sample and measurement

Twelve real screenshots from six channels, spanning 720p through 4K, contain
97 manually checked visible player-name occurrences. Three screenshots have
no detected panel, and one detection covers a battlefield without player names.
The sample includes dim colored text, rank icons, underscores, clan tags,
resource statistics, and a narrow two-player panel. Names were transcribed from
the images and their spelling checked against the local player database.

The [sample manifest](../benchmarks/ocr_samples.json) contains video URLs,
timestamps, formats, and expected names. This is a small development sample
used to tune the settings, not an independent accuracy benchmark. Repeated
players appear at different timestamps. Results will vary on other footage.

Hardware: AMD Radeon RX 9070 XT, 16 GB. Software: RapidOCR 3.9.2,
PyTorch 2.14.0+rocm10.1.0a20260906, matching Triton
3.8.0+git675c5987.rocm10.1.0a20260906, Ultralytics 8.4.135.
The local catalog contains 189,255 player names.

## Complete pipeline

Both runs processed the same twelve images and separate copies of the existing
6,066-video metadata JSON. Production metadata and screenshots were not changed.
Timing includes process startup, model loading, image decoding, YOLO, OCR,
name cleanup, and JSON persistence; downloaded models and disk caches were warm.

| Pipeline | Exact names recovered | Incorrect extra names | Wall time |
| --- | ---: | ---: | ---: |
| Previous code, four GPU worker processes | 68 / 97 | 28 | 7.79 s |
| Updated code, one GPU model owner | 96 / 97 | 1 | 7.10 s |

Matching is case-sensitive and compares complete names, including underscores,
digits, and clan tags. This matters because replay matching uses exact player
names. The remaining error confuses lowercase L and uppercase I in
`NebuchadnezzarII`; the catalog correction leaves ambiguous matches unresolved.

These are individual timing runs, not statistically established speedups.
An earlier cold GPU run of the new pipeline spent 12.6 seconds in processing
alone; first-use kernel setup can dominate short runs.

## OCR settings and model comparisons

The selected configuration uses a detection side length of 384 instead of 736,
a detection box threshold of 0.4, recognition batches of 32 instead of 6,
and no orientation classifier. Text recognition retains its 0.5 confidence
threshold. Smaller detection input recovered dim text that the original
resizing missed. Model versions and sizes are explicit in the code.

For the nine detected panels, warm OCR with the original settings took 1.55 s.
The final settings and name cleanup took 1.04 s, approximately 33% less.
These timings exclude image loading, YOLO, and JSON writes. The final batched
YOLO crops differ slightly from the original single-image crops, so this is
an indicative component comparison.

With the same final crops, detector settings, batching, and catalog cleanup:

| Recognizer | Exact names recovered | Incorrect extra names | Warm OCR time |
| --- | ---: | ---: | ---: |
| PP-OCRv6 small (selected) | 96 / 97 | 1 | 1.04 s |
| PP-OCRv6 medium | 95 / 97 | 3 | 1.56 s |
| PP-OCRv5 server | 92 / 97 | 6 | 1.60 s |

Increasing model size was not an improvement on this sample.

[PaddleOCR-VL 1.6](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6)
was also tested locally through its documented Transformers API, using
bfloat16, SDPA, the `OCR:` prompt, and a 512-token output cap. It ran on the AMD
GPU without installing PaddlePaddle or changing the ROCm stack.
Four difficult/negative panels took 8.41, 13.12, 3.91, and 1.47 seconds.
It misread underscores and names, and on the 720p player list it repeated
numeric statistics until the token cap, omitting half the players.
The first timing includes first-use inference overhead.
This was a direct panel-OCR test, not an evaluation of its full document
parsing pipeline or an optimized inference server.

PaddleOCR-VL is therefore not enabled as a fallback: this experiment did not
show a reliable accuracy gain for its additional latency. Its downloaded
weights remain in the local Hugging Face cache for further experiments;
the production pipeline does not load them.

## Reliability and reproduction

Images are decoded in bounded CPU batches. A single process owns YOLO and
RapidOCR, and YOLO detects panels in batches of eight. Name cleanup uses
exact catalog matches first and only accepts fuzzy corrections with a clear
winner. Unknown names are retained rather than forced into the catalog.

Checkpoint writes are atomic. Invalid images and inference errors are reported
and do not become successful empty results. Completed work is checkpointed on
interruption; a failed run raises before the enclosing updater can delete
the screenshots. A missing/corrupt model or corrupt metadata stops the run.

Place the manifest's screenshots in a separate directory, then run:

```bash
python processScreenshotsRapidOCR.py \
  --screenshots-dir data/ocr_benchmark/screenshots \
  --output-file data/ocr_benchmark/evaluation.json
python -m unittest discover -s tests -v
```

Use `--reprocess` to rerun available images against an existing evaluation
file. Local raw model outputs, timing logs, and comparison scripts are saved
under the ignored `data/ocr_benchmark/` directory.

## Streaming update regression

The concurrent update runner was also exercised with these same twelve images,
real GPU inference, the full player catalog, and recorded inputs replacing
YouTube/API requests. Channel workers published frames while OCR was active;
the shared metadata file retained all video titles and OCR results. It again
recovered 96 / 97 names with one incorrect extra. This verifies the streaming
integration, not the speedup of a complete live update. Its local output is
saved under `data/update_benchmark/`.
