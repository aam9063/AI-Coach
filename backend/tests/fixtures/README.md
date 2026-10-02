# Test fixtures

## garmin-fenix-5-bike.fit

- **Origin**: `tests/files/garmin-fenix-5-bike.fit` from the
  [python-fitparse](https://github.com/dtcooper/python-fitparse) repository,
  downloaded on 2026-07-23 from
  `https://raw.githubusercontent.com/dtcooper/python-fitparse/master/tests/files/garmin-fenix-5-bike.fit`
- **License**: MIT (the python-fitparse repository license; test data is
  redistributed under the same license)
- **Size**: 4,664 bytes
- **sha256**: `9206d34ba9c6283eb24337410c66d9d2f9b134d709ba2cf97bb5e20ae28f6314`
- **Content**: real-world Garmin Fenix 5 cycling activity (19 `record`
  messages with `timestamp`, `heart_rate`, `speed`/`enhanced_speed`,
  `altitude`/`enhanced_altitude`, `distance`; no power or cadence fields,
  which exercises the missing-field `None`-fill path). Contains
  `device_info` and unknown message types, exercising the
  ignore-unknown-types path. No swim (`length`) messages.

Used by `backend/tests/ingest/test_fit_parsing.py`.
