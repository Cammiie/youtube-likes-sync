"""Resolve Windows packaged-app virtualization once for all scheduled processes."""
import json
from pathlib import Path
from ytlikes.common import data_dir, atomic_write
from ytlikes.state import State

project = Path(__file__).resolve().parents[1]
root = data_dir()
state = State(root)
state.close()
physical = (root / 'state.sqlite3').resolve().parent
atomic_write(project / 'runtime-path.json', json.dumps({'data_dir':str(physical)}, indent=2).encode())
print('Runtime directory shared by interactive and scheduled commands.')
