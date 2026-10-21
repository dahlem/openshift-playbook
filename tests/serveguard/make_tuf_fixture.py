"""Write a minimal consistent-snapshot TUF repository layout for test_trust_config.yml.

Only the metadata fields the export reads are present (no signatures): timestamp ->
snapshot -> targets -> hashed target files. Usage: make_tuf_fixture.py DIR [--with-signing-config] [--corrupt]
"""

import hashlib
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
(out / "targets").mkdir(parents=True, exist_ok=True)
files = {"trusted_root.json": json.dumps({"mediaType": "application/vnd.dev.sigstore.trustedroot+json;version=0.1",
                                          "tlogs": [{"baseUrl": "https://rekor.apps.example"}]})}
if "--with-signing-config" in sys.argv:
    files["signing_config.v0.2.json"] = json.dumps({"mediaType": "application/vnd.dev.sigstore.signingconfig.v0.2+json",
                                                    "caUrls": [{"url": "https://fulcio.apps.example"}]})
targets = {}
for name, text in files.items():
    digest = hashlib.sha256(text.encode()).hexdigest()
    targets[name] = {"length": len(text), "hashes": {"sha256": digest}}
    body = text + (" " if "--corrupt" in sys.argv and name == "trusted_root.json" else "")
    (out / "targets" / f"{digest}.{name}").write_text(body)
(out / "3.targets.json").write_text(json.dumps({"signed": {"version": 3, "targets": targets}}))
(out / "2.snapshot.json").write_text(json.dumps({"signed": {"meta": {"targets.json": {"version": 3}}}}))
(out / "timestamp.json").write_text(json.dumps({"signed": {"meta": {"snapshot.json": {"version": 2}}}}))
