# published/

Curated result records that the Pages site is built from (`python -m sitegen.build --results
published --out _site`). Records are copied here and committed deliberately at publication time,
in the same `<run_id>/<system>/<measure>-sf<sf>-<item>.json` layout the campaign writes. Nothing
else belongs here. While it holds no records the site is a preview (what is compared, system pages,
methodology; no numbers). The workflow refuses to deploy a site marked synthetic.
