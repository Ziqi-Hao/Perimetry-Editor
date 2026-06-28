# Perimetry Editor

**A double-click desktop app for correcting Humphrey 24-2 Total-Deviation
perimetry data.** Open the report image on the left, fix the OCR-extracted
numbers on the right — every edit auto-saves to a clean CSV. No install, no
Python, no cloud.

<p align="center">
  <img src="docs/screenshot.png" alt="Perimetry Editor: the uploaded Humphrey report on the left, the editable colour-coded 24-2 Total Deviation grid on the right" width="100%">
</p>

## Download

Grab the app for your OS from the
[**latest release**](https://github.com/Ziqi-Hao/Perimetry-Editor/releases/latest).
It's a small command-line program that **opens the editor in your web browser**
and saves everything to a `PerimetryEditor/` folder in your home directory
(click **📁 Data folder** in the app to find your CSV).

The app is free and **unsigned**, so your OS warns on first launch — here's how
to get past it on each platform:

**Windows** — `PerimetryEditor-Windows.exe`
Double-click → on the blue *"Windows protected your PC"* prompt click
**More info → Run anyway**. A small console window opens (that's the app
running); the editor is in your browser. Close the console window to quit.

**macOS (Apple Silicon)** — `PerimetryEditor-macOS-AppleSilicon`
It's a command-line binary, so run it from **Terminal** (not double-click):
```bash
cd ~/Downloads                                       # wherever you saved it
chmod +x PerimetryEditor-macOS-AppleSilicon          # add run permission (downloads lose it)
xattr -dr com.apple.quarantine PerimetryEditor-macOS-AppleSilicon   # clear Gatekeeper flag
./PerimetryEditor-macOS-AppleSilicon                 # run — your browser opens
```
Quit with **Ctrl+C** in the Terminal.

**Linux** — `PerimetryEditor-Linux`
```bash
chmod +x PerimetryEditor-Linux && ./PerimetryEditor-Linux
```

> Runs entirely on your machine — nothing is uploaded. Use coded subject IDs,
> not patient names. **Want to skip the security prompts entirely?** Run from
> source (below) — `python3 app/desktop.py`, no binary, no warnings.

## Run from source

```bash
git clone https://github.com/Ziqi-Hao/Perimetry-Editor.git
cd Perimetry-Editor
python3 app/desktop.py        # opens your browser automatically
```

Pure Python standard library — no `pip install` needed.
(`python3 app/server.py` runs it as a plain server on `:8766`.)

## Using it

Upload a report, then fill the 54-point grid. It's keyboard-first:

| Key | Action |
| :-- | :-- |
| Click / start typing | Edit the focused cell |
| <kbd>Enter</kbd> / <kbd>Tab</kbd> | Save + next cell |
| <kbd>↑</kbd> <kbd>↓</kbd> | Save + move up / down |
| <kbd>Esc</kbd> | Cancel the edit |
| <kbd>←</kbd> <kbd>→</kbd> | Previous / next subject |
| Type `BS` / `B` | Blind spot · `?` or empty = missing |

Cells colour-code by severity as you type: green ≥ 0, yellow −5…−1,
orange −15…−6, red < −15.

## Output

Every edit writes `extracted/td_54point.csv` (and `td_grids.json`) into your
data folder. The CSV is the canonical artifact — one row per tested point:

| column | meaning |
| :-- | :-- |
| `subject`, `eye`, `age`, `sex` | subject metadata |
| `row`, `col` | grid position |
| `x_vf_deg`, `y_vf_deg` | visual-field coordinates (degrees) |
| `eccentricity_deg` | distance from fixation |
| `quadrant` | anatomical quadrant (`SN`/`ST`/`IN`/`IT`) |
| `td_dB` | value in dB, the literal `BS`, or empty |

## Build the executables

```bash
pip install -r requirements-dev.txt   # PyInstaller (build tooling only)
./build.sh                            # → dist/PerimetryEditor
```

Pushing a version tag (`git tag v1.0.4 && git push origin v1.0.4`) builds
Windows / macOS / Linux binaries via GitHub Actions, smoke-tests each one, and
publishes them to a release — see
[`.github/workflows/build.yml`](.github/workflows/build.yml).

## License

MIT — see [`LICENSE`](LICENSE). Made at the McConnell Brain Imaging Centre,
McGill University.
