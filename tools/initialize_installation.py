"""Initialize portable defaults without changing an existing installation."""
import json
from ytlikes.common import data_dir, atomic_write
from ytlikes.onboarding import installation_settings

root=data_dir()
if not (root/'config.json').exists():
    atomic_write(root/'config.json',json.dumps(installation_settings(root),indent=2).encode())
print('Download settings ready; existing connections and libraries preserved.')
