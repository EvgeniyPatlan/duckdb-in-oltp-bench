# published/

Curated result records that the Pages site is built from (`python -m sitegen.build --results
published --out _site`). Records are copied here and committed deliberately at publication time,
in the same `<run_id>/<system>/<measure>-sf<sf>-<item>.json` layout the campaign writes. Nothing
else belongs here; the workflow refuses to deploy when this directory holds no records or when the
built site is marked synthetic.
