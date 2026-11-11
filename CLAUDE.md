# Releasing Studio Sync

1. Bump VERSION in web_app.py (and the FL script VERSION/PROTOCOL if it changed), update README and CHANGELOG, run `CI=1 python3 -m pytest -q`.
2. Push, then `gh workflow run build.yml --ref main -f mac_runner=macos-15-intel` (use `macos-15` for an Apple silicon build when GitHub has capacity). The workflow tags vVERSION and makes a release on this private repo.
3. Publish to the public downloads repo so installed apps update themselves:
   `python3 packaging/publish_public.py VERSION /path/to/studio-sync-releases` (a clone of milanszel/studio-sync-releases).
   The app reads latest.json there; every download is checked against its SHA-256.
