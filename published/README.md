# published/

Curated result records that the Pages site is built from (`python -m sitegen.build --results
published --out _site`). Records are copied here and committed deliberately at publication time,
in the same `<run_id>/<system>/<measure>-sf<sf>-<item>.json` layout the campaign writes. The only
other files are optional author's notes, `notes/<run_id>.md`, shown on that report's overview,
and `runs.yaml`, which marks test runs: each is published as its own report, labelled as a test
run with the reason given, and left out of the site overview and of the comparison across reports.
Nothing else belongs here. While it holds no records the site is a preview (what is compared, system pages,
methodology; no numbers). The workflow refuses to deploy a site marked synthetic.
