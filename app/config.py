import json
from pathlib import Path

class ConfigManager:
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.exists():
            self.path.write_text("{}")

    def read(self):
        return json.loads(self.path.read_text())

    def update(self, new_data):
        data = self.read()
        data.update(new_data)
        self.path.write_text(json.dumps(data, indent=2))
