# CA Personalized Plate Checker

A small desktop tool that checks whether a California personalized license
plate configuration is available, by asking the DMV's own online plate
application the same question its "Check Availability" button asks.

- Python 3 standard library only (tkinter, urllib). No pip installs.
- Batch mode: paste a list of plates, one per line, and get a colour-coded
  results table. Export to CSV or copy the available ones.
- All 13 plate types (Environmental, Kids, Breast Cancer Awareness, ...),
  all four vehicle types, Kids symbols, half spaces.
- A `--cli` mode for scripts and quick checks.

## Run

```bash
python ca_plate_checker.py
```

Terminal mode:

```bash
python ca_plate_checker.py --cli LUVCATS 2FAST4U
python ca_plate_checker.py --cli --type Q PINK1          # Breast Cancer Awareness (6 chars)
python ca_plate_checker.py --cli --type K --symbol star "QZ*XJ7V"   # Kids plate, symbol in slot 3
python ca_plate_checker.py --help                        # plate type codes
```

## Typing rules

| You type | Meaning |
|----------|---------|
| letters / digits | plate characters (case does not matter) |
| space | a full space (an empty slot on the plate) |
| `/` | a half space (on 7-character types it gets an extra slot) |
| `*` | where the Kids-plate symbol goes (Kids plate only; default is the first slot) |

Plates are limited to the slot count the DMV page enforces: 7 for
Environmental-style types (8 with a half space), 6 for the 6-character types,
and 1 symbol plus up to 6 characters for Kids.

## How it works

The DMV page at `https://www.dmv.ca.gov/wasapp/ipp2/` posts the plate
configuration to `checkPers.do` and gets back JSON such as
`{"success":true,"code":"AVAILABLE"}` or `{"code":"NOT_AVAILABLE"}`.
The script opens a session the same way a browser does (load the terms page,
post the acknowledgement, then post the configuration) and translates the
reply. "Taken" also covers configurations the DMV has reserved or refused;
the endpoint does not distinguish them.

Checks run one at a time with a configurable delay (default 1.5 s). Please
keep it polite; this is the DMV's public site.

## Disclaimer

Not affiliated with the California DMV. Availability is whatever the DMV
reports at the moment of the check; only an actual order reserves a plate.
